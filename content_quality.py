"""주간 수집 결과의 저장 전 후처리: 발행일 검증, 중복 제거, URL 검증, 병합 저장.

기존 항목은 수정·삭제하지 않는다. 새 항목만 걸러서 추가한다.
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from article_keys import key_kind, make_article_key, short_key
from dates import USABLE_CONFIDENCE, WeekPeriod, date_failure_reason, parse_published

SYNDICATION_RATIO = 0.92
SYNDICATION_DAYS = 3
SIMILAR_LOG_RATIO = 0.80

_TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid"}
_WEEKLY_FILE_RE = re.compile(r"^\d{4}-\d{2}-W\d+_.+\.json$")
_DATE_RE = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")


def parse_date(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    m = _DATE_RE.search(value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", (title or "").casefold())


def is_valid_url_format(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme in ("http", "https") and "." in parts.netloc and " " not in url


def _http_status(url: str, timeout: float = 8.0) -> int:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MUWA-link-check)"}
    for method in ("HEAD", "GET"):
        req = Request(url, method=method, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.status
        except HTTPError as e:
            if method == "HEAD" and e.code in (400, 403, 405, 501):
                continue
            return e.code
    return 0


def check_url(url: str, fetch=_http_status) -> tuple[str, str]:
    """ok / invalid / unverified 와 상세 사유를 반환한다."""
    if not is_valid_url_format(url):
        return "invalid", "format"
    try:
        code = fetch(url)
    except Exception as e:  # 타임아웃·DNS·SSL 등은 수집 실패가 아니라 '미확인'
        return "unverified", f"{type(e).__name__}"
    if code in (404, 410):
        return "invalid", f"http_{code}"
    if 200 <= code < 400:
        return "ok", f"http_{code}"
    return "unverified", f"http_{code}"


def _collect_entries(node, out: list) -> None:
    if isinstance(node, dict):
        title = node.get("title") or node.get("name") or node.get("project")
        if isinstance(title, str) and title:
            out.append(
                {
                    "title": title,
                    "url": node.get("source_url") or "",
                    "date": node.get("date") or "",
                    "source": node.get("source") if isinstance(node.get("source"), str) else "",
                }
            )
        for v in node.values():
            _collect_entries(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_entries(v, out)


def load_existing_index(data_dir: Path) -> list[dict]:
    """data/의 주간 기사 파일(YYYY-MM-Wn_*.json)만 읽어 비교용 색인을 만든다.

    월간 보고서(*_monthly.json)·월간 global_case·coming-soon 플레이스홀더·insight_index는
    기사 데이터가 아니므로 제외한다(매물명/보고서 제목이 기사 제목과 오매칭되는 것을 방지).
    """
    entries: list[dict] = []
    for path in sorted(Path(data_dir).glob("*.json")):
        if not _WEEKLY_FILE_RE.match(path.name):
            continue
        try:
            _collect_entries(json.loads(path.read_text(encoding="utf-8")), entries)
        except (OSError, ValueError):
            continue
    return [make_index_entry(e["title"], e["url"], e["date"], source=e["source"]) for e in entries]


def proximity_date(date_str) -> date | None:
    """날짜 근접(syndication) 비교에 쓸 KST 날짜. 신뢰도가 낮은(low) 날짜는 쓰지 않는다."""
    pd = parse_published(date_str)
    return pd.date if pd is not None and pd.confidence in USABLE_CONFIDENCE else None


def make_index_entry(title: str, url: str, date_str: str, url_dedup: bool = True, source: str = "") -> dict:
    """url_dedup=False 이면 URL 일치로는 중복 판정하지 않는다(제목 기준만 적용).

    `akey`(article_key)는 내부 색인 전용이다. 현재는 shadow 검증에만 쓰이며 중복 판정(L0)에는 쓰이지 않는다.
    """
    return {
        "title": title,
        "nurl": normalize_url(url),
        "ntitle": normalize_title(title),
        "date": proximity_date(date_str),
        "url_dedup": url_dedup,
        "akey": make_article_key(url, title, parse_published(date_str), source),
    }


def load_section_index(
    data_dir: Path, section: str, week: str, excluded_weeks=(), same_week_url_dedup: bool = False
) -> list[dict]:
    """같은 섹션의 주간 파일(`YYYY-MM-Wn_{section}.json`)만 비교 대상으로 삼는다.

    다른 섹션 파일은 읽지 않는다. 현재 주차 파일의 항목은 제목 기준으로만 비교한다
    (한 기사에서 여러 항목이 나올 수 있으므로). excluded_weeks 는 월간 종합 데이터가
    주간 라벨로 저장된 주차로, 비교 대상에서 뺀다. same_week_url_dedup=True 이면 현재 주차 파일의
    항목도 URL 일치로 중복 판정한다(라이프스타일: 한 기사 = 한 항목, 재실행 멱등성).
    """
    pattern = re.compile(rf"^(\d{{4}}-\d{{2}}-W\d+)_{re.escape(section)}\.json$")
    out: list[dict] = []
    for path in sorted(Path(data_dir).glob("*.json")):
        m = pattern.match(path.name)
        if not m or m.group(1) in excluded_weeks:
            continue
        entries: list[dict] = []
        try:
            _collect_entries(json.loads(path.read_text(encoding="utf-8")), entries)
        except (OSError, ValueError):
            continue
        same_week = m.group(1) == week
        url_dedup = (not same_week) or same_week_url_dedup
        out.extend(
            make_index_entry(e["title"], e["url"], e["date"], url_dedup=url_dedup, source=e["source"])
            for e in entries
        )
    return out


def count_items(node) -> int:
    """list 이면 길이, {popular, emerging} 같은 dict 이면 값 목록 길이의 합."""
    if isinstance(node, dict):
        return sum(len(v) for v in node.values())
    return len(node or [])


def find_duplicate(index: list[dict], title: str, url: str, date_str: str):
    """(reason, matched_title, ratio) 또는 None. 유사도만으로는 'similar' 로그만 남기고 제외하지 않는다."""
    nurl, ntitle, d = normalize_url(url), normalize_title(title), proximity_date(date_str)
    similar = None
    for e in index:
        if nurl and e["nurl"] == nurl and e.get("url_dedup", True):
            return "duplicate_url", e["title"], 1.0
        if ntitle and e["ntitle"] == ntitle:
            return "duplicate_title", e["title"], 1.0
        if ntitle and e["ntitle"]:
            ratio = difflib.SequenceMatcher(None, ntitle, e["ntitle"]).ratio()
            if (
                ratio >= SYNDICATION_RATIO
                and d is not None
                and e["date"] is not None
                and abs((d - e["date"]).days) <= SYNDICATION_DAYS
            ):
                return "duplicate_syndicated", e["title"], round(ratio, 3)
            if ratio >= SIMILAR_LOG_RATIO and (similar is None or ratio > similar[2]):
                similar = ("similar_title_kept", e["title"], round(ratio, 3))
    return ("similar", similar[1], similar[2]) if similar else None


def filter_items(
    items: list[dict],
    period: WeekPeriod,
    index: list[dict],
    category: str,
    region: str,
    log: list[dict],
    fetch=_http_status,
    verify_urls: bool = True,
    title_key: str = "title",
    url_dedup_new: bool = True,
) -> list[dict]:
    """발행일 → 중복 → URL 순으로 검증해 채택할 새 항목만 반환한다. index에 채택 항목을 추가한다.

    title_key: 항목의 제목 필드명(title/name/project). url_dedup_new=False 이면 이번 실행에서
    채택한 항목은 URL 일치로 후속 항목을 제외하지 않는다(같은 URL의 서로 다른 항목 허용).
    """
    kept: list[dict] = []
    for item in items:
        title = item.get(title_key, "")
        base = {"category": category, "region": region, "title": title,
                "source_url": item.get("source_url", ""), "date": item.get("date", "")}

        pd = parse_published(item.get("date"))
        if pd is None:
            log.append({**base, "action": "excluded", "reason": date_failure_reason(item.get("date"))})
            continue
        if pd.confidence not in USABLE_CONFIDENCE:  # low: 문장 속에서 찾아낸 날짜는 주차 판정에 쓰지 않는다
            log.append({**base, "action": "excluded", "reason": "date_low_confidence",
                        "candidate_date": pd.date.isoformat()})
            continue
        d = pd.date
        if not period.contains(d):
            log.append({**base, "action": "excluded", "reason": "date_out_of_range",
                        "period": f"{period.start}~{period.end}"})
            continue

        dup = find_duplicate(index, title, item.get("source_url", ""), item.get("date", ""))
        if dup and dup[0] != "similar":
            log.append({**base, "action": "excluded", "reason": dup[0],
                        "matched_title": dup[1], "similarity": dup[2]})
            continue
        if dup:
            log.append({**base, "action": "kept", "reason": "similar_title_kept",
                        "matched_title": dup[1], "similarity": dup[2]})

        new_item = dict(item)
        url = new_item.get("source_url") or ""
        if url and verify_urls:
            status, detail = check_url(url, fetch)
            if status != "ok":
                log.append({**base, "action": "url_check", "url_status": status, "detail": detail})
            if status == "invalid":
                new_item["source_url"] = ""

        entry = make_index_entry(title, new_item.get("source_url", "") or url,
                                 new_item.get("date", ""), url_dedup=url_dedup_new,
                                 source=item.get("source") if isinstance(item.get("source"), str) else "")
        _shadow_l1(index, entry, category, region, log)
        index.append(entry)
        kept.append(new_item)
    return kept


def _shadow_l1(index: list[dict], entry: dict, category: str, region: str, log: list[dict]) -> None:
    """L1(article_key)로는 같지만 L0(nurl)로는 다른 기존 항목이 있으면 로그만 남긴다.

    채택·제외 판정에는 영향을 주지 않는다. URL 전문은 기록하지 않는다(제목과 축약 키만).
    """
    akey = entry.get("akey")
    if key_kind(akey) != "url":
        return
    for e in index:
        if e.get("akey") == akey and e.get("nurl") != entry["nurl"]:
            log.append({"category": category, "region": region, "title": entry["title"],
                        "action": "shadow", "reason": "l1_only_match",
                        "matched_title": e["title"], "key": short_key(akey)})
            return


def count_shadow(log: list[dict]) -> int:
    return sum(1 for r in log if r.get("action") == "shadow")


def load_url_locations(data_dir: Path) -> dict[str, set[str]]:
    """L0 정규화 URL → 그 URL이 들어 있는 섹션/카테고리 이름 집합 (다중 분류 로그용, 읽기 전용)."""
    pattern = re.compile(r"^\d{4}-\d{2}-W\d+_(.+)\.json$")
    out: dict[str, set[str]] = {}
    for path in sorted(Path(data_dir).glob("*.json")):
        m = pattern.match(path.name)
        if not m:
            continue
        entries: list[dict] = []
        try:
            _collect_entries(json.loads(path.read_text(encoding="utf-8")), entries)
        except (OSError, ValueError):
            continue
        for e in entries:
            nurl = normalize_url(e["url"])
            if nurl:
                out.setdefault(nurl, set()).add(m.group(1))
    return out


def merge_category_file(path: Path, week: str, key: str, range_label: str, domestic: list, global_: list) -> dict:
    """같은 주 파일이 있으면 기존 항목을 보존하고 새 항목만 뒤에 추가한다."""
    path = Path(path)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"week": week, "category": key, "range_label": range_label, "domestic": [], "global": []}
    is_new_file = not path.exists()
    data["domestic"] = _extend(data.get("domestic", []), domestic)
    data["global"] = _extend(data.get("global", []), global_)
    if count_items(domestic) or count_items(global_) or is_new_file:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _extend(existing, new):
    """list는 뒤에 추가, {popular, emerging} 같은 dict는 키별로 추가한다. 기존 항목은 건드리지 않는다."""
    if isinstance(new, dict):
        base = existing if isinstance(existing, dict) else {}
        for k, v in new.items():
            base.setdefault(k, []).extend(v)
        return base
    existing.extend(new)
    return existing


def append_run_log(log_dir: Path, week: str, run: dict) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{week}_collect_log.json"
    runs = []
    if path.exists():
        try:
            runs = json.loads(path.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, ValueError):
            runs = []
    runs.append(run)
    path.write_text(json.dumps({"week": week, "runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def now_iso() -> str:
    from dates import KST

    return datetime.now(KST).isoformat(timespec="seconds")
