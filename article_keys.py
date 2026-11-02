"""article_key — 기사 동일성 식별용 내부 키 (순수 함수만, 입출력 없음).

- L0 = `content_quality.normalize_url` (기존 중복 판정 기준, 이 모듈이 바꾸지 않는다).
- L1 = L0 결과 + 기본 포트 제거 + 퍼센트 인코딩 정규화.
  스킴 통일, 경로 대소문자 변경, 쿼리 정렬, index/AMP/재게시 URL 통합은 하지 않는다.
  경로의 NFC/NFD 변환도 하지 않는다(서버는 서로 다른 바이트를 다른 자원으로 본다).
- `ak1:` = URL 기반 키, `tk1:` = URL이 없는 항목의 제목·발행일·출처 기반 키.
  둘 다 SHA-256 전체 64자리이며 도메인 접두가 달라 서로 같은 값이 될 수 없다.

키는 내부 색인 전용이다. 주간 JSON·insight_index.json·사이트/API 응답에 저장하거나 노출하지 않는다.
이번 단계에서 L1/키는 shadow 검증용이며 실제 중복 판정에는 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from dates import USABLE_CONFIDENCE, ParsedDate

URL_KEY_PREFIX = "ak1:"
TITLE_KEY_PREFIX = "tk1:"
MIN_TITLE_KEY_LEN = 8  # tk1 최소 정규화 제목 길이 (너무 일반적인 제목의 충돌 방지)

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")
_NETLOC_RE = re.compile(r"^(?P<user>[^@]*@)?(?P<host>\[[^\]]*\]|[^:]*)(?::(?P<port>\d*))?$")
_NON_WORD_RE = re.compile(r"[\W_]+")
_FIELD_SEP = "\x1f"  # 정규화 결과에는 나올 수 없는 구분자


def _l0(url: str) -> str:
    from content_quality import normalize_url  # 순환 임포트 방지를 위한 지연 임포트

    return normalize_url(url)


def normalize_percent_encoding(text: str) -> str:
    """퍼센트 인코딩 정규화.

    - 비예약 문자(영숫자 - . _ ~)로 디코딩되는 %XX 는 문자로 풀고, 그 외 %xx 는 hex 를 대문자로 통일한다.
    - 원문의 비ASCII 문자는 UTF-8 바이트의 %XX(대문자)로 바꾼다.
    - %2F 같은 예약 문자 인코딩은 디코딩하지 않는다. '%' 뒤가 올바른 hex 두 자리가 아니면 그대로 둔다.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "%" and i + 2 < n and text[i + 1] in _HEX and text[i + 2] in _HEX:
            decoded = chr(int(text[i + 1 : i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else "%" + text[i + 1 : i + 3].upper())
            i += 3
        elif ord(ch) > 127:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _strip_default_port(scheme: str, netloc: str) -> str:
    default = _DEFAULT_PORTS.get(scheme)
    m = _NETLOC_RE.match(netloc)
    if not m:
        return netloc
    port = m.group("port")
    if port is None:
        return netloc
    if port == "" or port == default:
        return f"{m.group('user') or ''}{m.group('host')}"
    return netloc


def normalize_url_l1(url: str) -> str:
    """L1 URL. L0(normalize_url) 결과에 기본 포트 제거와 퍼센트 인코딩 정규화만 더한다."""
    u0 = _l0(url)
    if not u0:
        return ""
    parts = urlsplit(u0)
    netloc = _strip_default_port(parts.scheme, parts.netloc)
    path = normalize_percent_encoding(parts.path)
    query = normalize_percent_encoding(parts.query)
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _sha256_hex(domain: str, payload: str) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + payload.encode("utf-8")).hexdigest()


def url_key(url: str) -> str | None:
    """`ak1:` + 64자리 hex. URL이 비어 있으면 None."""
    l1 = normalize_url_l1(url)
    if not l1:
        return None
    return URL_KEY_PREFIX + _sha256_hex("muwa/ak1", l1)


def key_text(value: str) -> str:
    """tk1 용 정규화: NFKC → casefold → 문자·숫자 외 제거."""
    return _NON_WORD_RE.sub("", unicodedata.normalize("NFKC", value or "").casefold())


def title_key(title: str, published: ParsedDate | None, source: str = "", min_len: int = MIN_TITLE_KEY_LEN) -> str | None:
    """`tk1:` + 64자리 hex. 발행일이 high/medium 신뢰도로 확정되고 정규화 제목이 min_len 이상일 때만 만든다."""
    if published is None or published.confidence not in USABLE_CONFIDENCE:
        return None
    ntitle = key_text(title)
    if len(ntitle) < min_len:
        return None
    payload = _FIELD_SEP.join((ntitle, published.date.isoformat(), key_text(source)))
    return TITLE_KEY_PREFIX + _sha256_hex("muwa/tk1", payload)


def make_article_key(url: str, title: str, published: ParsedDate | None, source: str = "") -> str | None:
    """URL이 있으면 ak1, 없으면 조건을 만족할 때 tk1, 아니면 None."""
    if url and url.strip():
        return url_key(url)
    return title_key(title, published, source)


def key_kind(key: str | None) -> str | None:
    if not key:
        return None
    if key.startswith(URL_KEY_PREFIX):
        return "url"
    if key.startswith(TITLE_KEY_PREFIX):
        return "title"
    return None


def short_key(key: str | None) -> str:
    """로그 표시용 축약값 (접두사 + 앞 12자리). 비교에는 쓰지 않는다."""
    if not key:
        return ""
    prefix, _, digest = key.partition(":")
    return f"{prefix}:{digest[:12]}"
