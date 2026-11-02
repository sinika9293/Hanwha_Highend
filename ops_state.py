"""주차별 수집 상태·정산 관리 (`ops/status/{week}.json`).

- P4의 활성 처리 단위는 AI 백엔드 1개(`ai_backend:{name}`)다. 기사 수집 소스는 P5에서 같은 `units` 목록에
  `kind="article_source"` 단위로 추가한다(스키마·판정 함수는 바뀌지 않는다). 사유 코드의 "source"는
  활성 처리 단위를 뜻한다.
- 상태 파일은 공개 저장소에 올라간다. 제목·URL·article_key·로컬 경로·키·토큰·자유 문장 오류는 기록하지 않고
  숫자 집계, 고정 코드, 예외 클래스 이름만 기록한다(자유 문장은 로컬 gitignored 로그에만 남는다).
- 상태는 데이터·인덱스 저장이 끝난 뒤 마지막에 기록한다. 도중에 중단돼도 "상태 없음 = unprocessed"로 남는다.
- 파일이 없거나 섹션이 없으면 성공이나 0건으로 추정하지 않고 unprocessed/not_run 으로 읽는다(백필 없음).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backends import mask_secrets
from dates import KST

SCHEMA_VERSION = 1
MAX_ATTEMPTS = 20
LOCK_TIMEOUT_SECONDS = 15
TEXT_LIMIT = 100

STATUS_SAVED, STATUS_EMPTY, STATUS_FAILED, STATUS_UNPROCESSED = "saved", "empty", "failed", "unprocessed"
STATUSES = (STATUS_SAVED, STATUS_EMPTY, STATUS_FAILED, STATUS_UNPROCESSED)

REASON_OK = "all_sources_ok"
REASON_PARTIAL = "partial_source_failure"
REASON_ALL_FAILED = "all_sources_failed"
REASON_NO_UNIT = "no_enabled_source"
REASON_PROCESSING = "processing_error"
REASON_LEGACY = "legacy_label_conflict"
REASON_LEGACY_AGGREGATE = "legacy_aggregate"
LEGACY_REASONS = (REASON_LEGACY, REASON_LEGACY_AGGREGATE)
REASON_NOT_RUN = "not_run"  # 저장되지 않는 파생 값 (파일·섹션 없음)
REASONS = (REASON_OK, REASON_PARTIAL, REASON_ALL_FAILED, REASON_NO_UNIT, REASON_PROCESSING,
           REASON_LEGACY, REASON_LEGACY_AGGREGATE, REASON_NOT_RUN)

KINDS = ("insight", "lifestyle")
UNIT_KINDS = ("ai_backend", "article_source")
ROLES = ("active", "check_only")
ERROR_CODES = ("collect_failed", "processing_error", "unknown")
EXCLUDED_REASONS = ("date_missing", "date_ambiguous", "date_unparseable", "date_low_confidence",
                    "date_out_of_range", "duplicate_url", "duplicate_title", "duplicate_syndicated")

_WEEK_RE = re.compile(r"^\d{4}-\d{2}-W\d+$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,60}$")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_FORBIDDEN_IN_STATUS = ("ak1:", "tk1:", "akey", "article_key", ":\\", "/home/", "/Users/")
_URL_SCHEME_RE = re.compile(r"(?i)https?://")  # 단어 속 'http'(예: 클래스 이름)가 아니라 URL 형태만 막는다


class StatusError(RuntimeError):
    """상태 파일 읽기·쓰기 오류의 공통 부모."""


class StatusReadError(StatusError):
    pass


class StatusWriteError(StatusError):
    pass


# ---------------------------------------------------------------------------
# 안전한 문자열
# ---------------------------------------------------------------------------


def safe_text(value, limit: int = TEXT_LIMIT) -> str:
    """상태 파일에 쓸 수 있는 문자열: 키·토큰·로컬 경로 마스킹, URL은 <url>, 제어문자 제거, 길이 제한."""
    s = mask_secrets(str(value))
    s = _URL_RE.sub("<url>", s)
    s = _CTRL_RE.sub(" ", s).strip()
    return s[:limit]


def safe_identifier(value, default: str = "unknown") -> str:
    """식별자(백엔드 이름, 예외 클래스 이름 등). 허용 문자만 남기고 아니면 default."""
    s = safe_text(value, 60)
    return s if _IDENT_RE.match(s) else default


def error_type_of(failure_text) -> str:
    """'ClassName: message' 형식의 실패 문자열에서 예외 클래스 이름만 꺼낸다(메시지는 버린다)."""
    head = str(failure_text or "").split(":", 1)[0].strip()
    return safe_identifier(head, "UnknownError")


def new_run_id(kind: str) -> str:
    return f"{datetime.now(KST):%Y%m%dT%H%M%S}-{kind}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# 처리 단위와 판정
# ---------------------------------------------------------------------------


def make_unit(unit_id: str, kind: str, ok: bool, role: str = "active",
              error_type: str | None = None, error_code: str | None = None) -> dict:
    if kind not in UNIT_KINDS:
        raise ValueError(f"unknown unit kind: {kind!r}")
    if role not in ROLES:
        raise ValueError(f"unknown unit role: {role!r}")
    if error_code is not None and error_code not in ERROR_CODES:
        error_code = "unknown"
    return {
        "id": safe_identifier(unit_id),
        "kind": kind,
        "role": role,
        "ok": bool(ok),
        "error_type": safe_identifier(error_type, "UnknownError") if error_type else None,
        "error_code": error_code,
    }


@dataclass(frozen=True)
class Classification:
    status: str
    settled: bool
    settled_reason: str


def classify(units, saved_total, processing_error: bool = False, guard=None) -> Classification:
    """섹션 한 번의 처리 결과를 판정한다. check_only 단위는 성공률·settled 판정에서 제외한다.

    우선순위: 레거시 가드 > 처리 예외 > 활성 단위 0개 > 전부 실패 > 전부 성공 > 일부 실패.
    settled=True 는 활성 단위가 1개 이상이고 전부 성공한 saved/empty 에만 허용된다.
    """
    if guard:  # 레지스트리 사유(legacy_label_conflict | legacy_aggregate)를 그대로 쓰고, 알 수 없는 값은 legacy_label_conflict
        return Classification(STATUS_FAILED, False, guard if guard in LEGACY_REASONS else REASON_LEGACY)
    if processing_error:
        return Classification(STATUS_FAILED, False, REASON_PROCESSING)
    active = [u for u in units if u.get("role", "active") == "active"]
    if not active:
        return Classification(STATUS_UNPROCESSED, False, REASON_NO_UNIT)
    failed = sum(1 for u in active if not u.get("ok"))
    if failed == len(active):
        return Classification(STATUS_FAILED, False, REASON_ALL_FAILED)
    has_items = bool(saved_total) and saved_total >= 1
    status = STATUS_SAVED if has_items else STATUS_EMPTY
    if failed == 0:
        return Classification(status, True, REASON_OK)
    return Classification(status, False, REASON_PARTIAL)


def units_summary(units) -> dict:
    out: dict[str, dict] = {}
    for kind in UNIT_KINDS:
        group = [u for u in units if u.get("kind") == kind]
        if kind == "article_source" and not group:
            out[kind] = {"tracked": False}  # P5 전: "소스 0개"가 아니라 "추적하지 않음"
            continue
        active = [u for u in group if u.get("role", "active") == "active"]
        out[kind] = {
            "tracked": True,
            "active": len(active),
            "check_only": len(group) - len(active),
            "ok": sum(1 for u in active if u.get("ok")),
            "failed": sum(1 for u in active if not u.get("ok")),
        }
    return out


# ---------------------------------------------------------------------------
# 집계 (filter_items 가 쌓은 log 를 읽기만 한다)
# ---------------------------------------------------------------------------


def summarize_log(log, category: str) -> dict:
    excluded = {reason: 0 for reason in EXCLUDED_REASONS}
    excluded["other"] = 0
    shortfall = 0
    url_check = {"invalid": 0, "unverified": 0}
    shadow = multi = 0
    for rec in log:
        if rec.get("category") != category:
            continue
        action = rec.get("action")
        if action == "excluded":
            reason = rec.get("reason")
            excluded[reason if reason in EXCLUDED_REASONS else "other"] += 1
        elif action == "shortfall":
            shortfall += 1
        elif action == "url_check" and rec.get("url_status") in url_check:
            url_check[rec["url_status"]] += 1
        elif action == "shadow":
            shadow += 1
        elif action == "multi_classified":
            multi += 1
    return {"excluded": excluded, "shortfall": shortfall, "url_check": url_check,
            "shadow_l1_only": shadow, "multi_classified": multi}


@dataclass
class SectionOutcome:
    """섹션 하나의 실행 결과 (run_* 가 채운다)."""

    backend: str  # AI 백엔드 이름
    collect_error: str | None = None  # 수집 단계 실패 시 예외 클래스 이름
    processing_error: str | None = None  # 필터·저장 단계 실패 시 예외 클래스 이름
    saved_new: int = 0
    saved_total: int | None = None  # 병합 후 섹션 전체 유효 항목 수. 알 수 없으면 None
    guard: str | None = None  # 레거시 레지스트리 사유 (있으면 수집하지 않음)


def build_attempt(run_id: str, run_at: str, kind: str, outcome: SectionOutcome, log, category: str) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    if outcome.guard:
        units: list[dict] = []
    else:
        name = safe_identifier(outcome.backend)
        if outcome.collect_error:
            units = [make_unit(f"ai_backend:{name}", "ai_backend", False, "active",
                               outcome.collect_error, "collect_failed")]
        elif outcome.processing_error:
            # 백엔드 호출은 성공했고 이후 처리에서 실패. 판정은 processing_error 가 우선한다.
            units = [make_unit(f"ai_backend:{name}", "ai_backend", True, "active",
                               outcome.processing_error, "processing_error")]
        else:
            units = [make_unit(f"ai_backend:{name}", "ai_backend", True)]
    failed_run = bool(outcome.collect_error or outcome.processing_error or outcome.guard)
    saved_total = None if failed_run else outcome.saved_total
    cls = classify(units, saved_total, processing_error=bool(outcome.processing_error),
                   guard=outcome.guard)
    counts = {"saved_new": 0 if failed_run else int(outcome.saved_new), "saved_total": saved_total}
    counts.update(summarize_log(log, category))
    return {
        "run_id": run_id, "run_at": run_at, "kind": kind,
        "status": cls.status, "settled": cls.settled, "settled_reason": cls.settled_reason,
        "units": units, "units_summary": units_summary(units), "counts": counts,
    }


# ---------------------------------------------------------------------------
# effective 상태
# ---------------------------------------------------------------------------


def _rank(attempt: dict) -> int:
    if attempt.get("settled"):
        return 3
    if attempt.get("status") in (STATUS_SAVED, STATUS_EMPTY):
        return 2
    if attempt.get("status") == STATUS_FAILED:
        return 1
    return 0


def pick_effective(attempts: list[dict]) -> dict | None:
    """최근 settled > 최근 saved/empty > 최근 failed > unprocessed. settled 이후의 실패는 후퇴시키지 않는다."""
    best = None
    for att in attempts:  # 시간순: 같은 순위면 더 나중 시도가 이긴다
        if best is None or _rank(att) >= _rank(best):
            best = att
    return best


def effective_record(attempt: dict) -> dict:
    return {"status": attempt["status"], "settled": attempt["settled"],
            "settled_reason": attempt["settled_reason"], "run_id": attempt["run_id"],
            "updated_at": attempt["run_at"]}


def unprocessed_state() -> dict:
    return {"status": STATUS_UNPROCESSED, "settled": False, "settled_reason": REASON_NOT_RUN,
            "run_id": None, "updated_at": None}


def trim_attempts(attempts: list[dict], effective_run_id: str | None, limit: int = MAX_ATTEMPTS) -> list[dict]:
    """최근 limit 건 + effective 가 가리키는 시도를 유지한다(시간순 보존)."""
    if len(attempts) <= limit:
        return attempts
    keep = {id(a) for a in attempts[-limit:]}
    for a in attempts:
        if a.get("run_id") == effective_run_id:
            keep.add(id(a))
    return [a for a in attempts if id(a) in keep]


# ---------------------------------------------------------------------------
# 파일 입출력 (원자적 쓰기 + 락)
# ---------------------------------------------------------------------------


def _check_week(week: str) -> str:
    if not isinstance(week, str) or not _WEEK_RE.match(week):
        raise ValueError(f"invalid week label: {week!r}")
    return week


def status_path(ops_dir: Path, week: str) -> Path:
    return Path(ops_dir) / "status" / f"{_check_week(week)}.json"


def assert_public_safe(text: str) -> None:
    """공개 저장소에 올라갈 JSON 텍스트에 URL·키·경로 흔적이 없는지 마지막으로 확인한다."""
    if _URL_SCHEME_RE.search(text):
        raise StatusWriteError("unsafe_content")
    for token in _FORBIDDEN_IN_STATUS:
        if token in text:
            raise StatusWriteError("unsafe_content")
    if mask_secrets(text, {}) != text:
        raise StatusWriteError("unsafe_content")


def write_json_atomic(path: Path, obj) -> None:
    """같은 폴더의 임시 파일에 쓰고 flush/fsync 후 os.replace 로 교체한다. 실패 시 임시 파일을 지우고 기존 파일을 보존한다."""
    path = Path(path)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        last_error: Exception | None = None
        for _ in range(5):  # Windows: 대상이 잠시 열려 있으면 PermissionError
            try:
                os.replace(tmp, path)
                last_error = None
                break
            except PermissionError as e:
                last_error = e
                time.sleep(0.05)
        if last_error is not None:
            raise last_error
    except (OSError, ValueError, TypeError) as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise StatusWriteError(f"write_failed:{type(e).__name__}") from None
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


_LOCK_POLL_SECONDS = 0.01

# 같은 프로세스의 스레드 간 배제를 위한 경로별 락. OS 잠금(msvcrt/flock)은 핸들 단위로 프로세스 간 배제를 맡는다.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(path))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _try_os_lock(fd: int) -> bool:
    """파일의 첫 1바이트에 비차단 배타 잠금을 건다. 잠금 경합이면 False, 그 외 OS 오류는 OSError 로 전파한다.

    Windows: msvcrt.locking(LK_NBLCK) — 경합은 EACCES(또는 EDEADLK). POSIX: fcntl.flock(LOCK_EX|LOCK_NB) — 경합은 EAGAIN/EACCES.
    잠금은 열어 둔 핸들에 걸리며 프로세스가 종료되면 OS 가 해제한다. 네트워크 파일시스템에서의 동작은 보장하지 않는다.
    """
    contention = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    if os.name == "nt":
        import msvcrt  # 필요한 플랫폼에서만 임포트

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in contention:
                return False
            raise
        return True
    import fcntl  # 필요한 플랫폼에서만 임포트

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in contention:
            return False
        raise
    return True


def _os_unlock(fd: int) -> None:
    """잠금만 푼다(락 파일은 지우지 않는다). 해제 오류는 이후 close 가 잠금을 해제하므로 무시한다."""
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def week_lock(locks_dir: Path, week: str, timeout: float = LOCK_TIMEOUT_SECONDS, *,
              monotonic=None, sleep=None):
    """주차별 OS 배타 잠금(`logs/.locks/{week}.lock`). timeout 초 안에 못 얻으면 StatusWriteError("lock_timeout").

    - 락 파일은 영구 파일이다: 삭제하지 않고, stale 판정·소유 토큰·파일 내용 읽기도 하지 않는다.
      잠금만 걸고 풀며, 보유 프로세스가 죽으면 OS 가 잠금을 해제한다.
    - 같은 프로세스의 스레드 경쟁은 경로별 threading.Lock 으로, 프로세스 간 경쟁은 OS 잠금으로 막는다.
    - 잠금 경합만 재시도한다(deadline 검사 + 짧은 sleep). 그 외 OS 오류(파일 열기 실패, 잠금 호출 오류 등)는
      재시도하지 않고 StatusWriteError("lock_failed:<예외 이름>") 로 끝낸다.
    - monotonic/sleep 은 테스트에서 시간을 제어하기 위한 주입 지점이다.
    """
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    locks_dir = Path(locks_dir)
    path = locks_dir / f"{_check_week(week)}.lock"
    try:
        locks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise StatusWriteError(f"lock_dir_failed:{type(e).__name__}") from None
    deadline = monotonic() + timeout
    thread_lock = _thread_lock_for(path)
    while not thread_lock.acquire(blocking=False):
        if monotonic() >= deadline:
            raise StatusWriteError("lock_timeout") from None
        sleep(_LOCK_POLL_SECONDS)
    try:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise StatusWriteError(f"lock_failed:{type(e).__name__}") from None
        try:
            while True:
                try:
                    got = _try_os_lock(fd)
                except OSError as e:
                    raise StatusWriteError(f"lock_failed:{type(e).__name__}") from None
                if got:
                    break
                if monotonic() >= deadline:
                    raise StatusWriteError("lock_timeout") from None
                sleep(_LOCK_POLL_SECONDS)
            try:
                yield path
            finally:
                _os_unlock(fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
    finally:
        thread_lock.release()


def read_week(ops_dir: Path, week: str) -> dict | None:
    """주차 상태 파일을 읽는다. 없으면 None. 깨졌거나 알 수 없는 버전이면 StatusReadError."""
    path = status_path(ops_dir, week)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise StatusReadError(f"read_failed:{type(e).__name__}") from None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(data.get("sections"), dict):
        raise StatusReadError("invalid_status_file")
    return data


def section_state(ops_dir: Path, week: str, section: str) -> dict:
    """섹션의 effective 상태. 파일·섹션이 없으면 unprocessed/not_run (성공이나 0건으로 추정하지 않는다)."""
    data = read_week(ops_dir, week)
    entry = (data or {}).get("sections", {}).get(section)
    if not entry or not entry.get("effective"):
        return unprocessed_state()
    return entry["effective"]


def load_legacy_registry(path: Path) -> dict[str, dict]:
    """승인된(approved=true) 레거시 주차만 반환한다. 파일이 없으면 빈 dict, 깨졌으면 StatusReadError(안전하게 중단)."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        weeks = data["weeks"]
        if not isinstance(weeks, dict):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise StatusReadError("invalid_legacy_registry") from None
    out: dict[str, dict] = {}
    for week, info in weeks.items():
        if isinstance(info, dict) and info.get("approved") is True and _WEEK_RE.match(str(week)):
            out[week] = {"reason": safe_identifier(info.get("reason"), REASON_LEGACY)}
    return out


def record_run(base_dir: Path, period, kind: str, run_id: str, run_at: str,
               outcomes: dict[str, SectionOutcome], log,
               lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> dict:
    """한 번의 실행 결과를 주차 상태 파일에 기록한다(주차 파일 단위로 읽기-수정-쓰기 한 번).

    경로는 호출 시점의 base_dir 에서 계산한다(`ops/`, `logs/.locks/`)."""
    base_dir = Path(base_dir)
    week = _check_week(period.week_label)
    ops_dir = base_dir / "ops"
    attempts = {key: build_attempt(run_id, run_at, kind, outcome, log, key) for key, outcome in outcomes.items()}
    path = status_path(ops_dir, week)
    with week_lock(base_dir / "logs" / ".locks", week, lock_timeout):
        try:
            data = read_week(ops_dir, week)
        except StatusReadError as e:
            raise StatusWriteError(f"existing_status_unreadable:{e}") from None
        if data is None:
            data = {"schema_version": SCHEMA_VERSION, "week": week,
                    "range": {"start": period.start.isoformat(), "end": period.end.isoformat()},
                    "updated_at": run_at, "sections": {}}
        for key, attempt in attempts.items():
            entry = data["sections"].setdefault(key, {"effective": None, "attempts": []})
            entry["attempts"].append(attempt)
            best = pick_effective(entry["attempts"])
            entry["effective"] = effective_record(best)
            entry["attempts"] = trim_attempts(entry["attempts"], best["run_id"])
        data["updated_at"] = run_at
        text = json.dumps(data, ensure_ascii=False, indent=2)
        assert_public_safe(text)
        write_json_atomic(path, data)
    return data
