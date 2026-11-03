"""robots.txt 파서·판정기 (RFC 9309, 순수 함수, 표준 라이브러리만 사용, 네트워크 호출 없음).

이 모듈은 robots.txt **내용**을 받아 파싱·판정하고 "이 내용을 믿어도 되는가"에 대한 신호만 제공한다. robots.txt를 가져오는 일
(조회, 상태 코드·redirect·timeout 처리, 24시간 캐시)은 RFC 9309 §2.3~2.4의 영역이며 이후 단계(SafeFetcher)의 책임이다.

범례: [RFC] = RFC 9309가 요구/허용하는 동작, [정책] = 당사가 더 보수적으로 정한 동작(차단 쪽으로 기운다).
RFC 원문은 외부 접근 제한 때문에 이번 구현에서 글자 단위로 대조하지 못했다(UNVERIFIED).

§2.2.1 User-agent 그룹
- [RFC] 하나 이상의 연속된 `user-agent` 줄 뒤에 규칙이 온다. 규칙 뒤의 `user-agent`는 새 그룹을 시작한다. 빈 줄·주석은 그룹을 끊지 않는다.
- [RFC] 제품 토큰은 영문자·`_`·`-`만, 대소문자 무시 일치. 같은 토큰의 그룹은 모두 병합. 일치 그룹이 없으면 `*` 그룹들을 병합. 둘 다 없으면 규칙 없음(허용).
- [정책] 에이전트 값은 완전 일치만 인정한다(접두사·부분 문자열 일치 없음). 첫 `user-agent` 앞의 규칙은 무시한다([RFC]).

§2.2.2 Allow / Disallow
- [RFC] 가장 구체적인(옥텟이 가장 많은) 패턴이 이긴다. 같으면 Allow. 일치하는 규칙이 없으면 허용. 빈 `Disallow:`는 규칙이 아니다(모두 허용).
- [RFC] 퍼센트 인코딩 정규화: 비예약 문자의 `%XX`는 풀고, 예약 문자 `%2F` 등은 풀지 않으며, 비ASCII는 UTF-8 `%XX`로 바꿔 비교한다.
- [RFC] `/robots.txt` URI는 규칙과 무관하게 항상 허용한다 (`Decision.reason == "implicit_robots_txt"`). 질의가 붙거나 경로가 다르면 일반 규칙을 따른다.
- [정책] 패턴 길이는 `*`·`$` 포함 정규화한 패턴의 길이로 센다(사양의 "most octets" 해석). 공백·제어 문자·`"<>\\^`{|}`처럼 URL에 날것으로 올 수 없는
  ASCII 문자는 `%XX`로 정규화해 양쪽을 같은 형태로 비교한다. 규칙 값의 UTF-8이 아닌 원시 바이트는 대체 문자로 바꾸지 않고 `%XX`로 보존한다.
- [정책] `/` 또는 `*`로 시작하지 않는 `Disallow`는 `/`를 붙여 막는 쪽으로, 같은 형태의 `Allow`는 무시한다.
- [정책] 질의 문자열은 경로의 일부로 비교한다([RFC] 예제와 같음). fragment는 제외한다.

§2.2.3 특수 문자
- [RFC] `#` 주석, `*`(0개 이상의 임의 문자), 끝의 `$`(경로 끝). 앞쪽·중간의 `$`는 일반 문자로 취급한다.
- 매칭은 정규식이 아닌 위치 기반 탐욕 매칭이라 역추적 폭발이 없다.

§2.2.4 기타 레코드
- [RFC] 알 수 없는 레코드는 무시해도 되고 정의된 레코드의 파싱을 방해하면 안 된다. 오타를 관대하게 해석하는 것은 선택(MAY)이다.
- [정책] 오타는 **명시한 별칭만** 인정한다: Disallow의 `disalow`/`dissalow`/`dissallow`/`diasllow`/`disallaw`(차단으로 해석),
  User-agent의 `user agent`/`useragent`. 그 밖의 미등록 지시어(`noindex`, `host`, `allo`, `disallow2` 등)는 추측하지 않고 무시하며 세어서 노출한다.
- [정책] `Sitemap`·`Crawl-delay`는 그룹 경계를 바꾸지 않는다(Google 파서는 `Crawl-delay`를 만나면 그룹이 끊긴다고 본다).

§2.5 한도
- [RFC] 파싱 한도는 500 KiB 이상이어야 하고 한도를 넘는 뒷부분은 무시해도 된다.
- [정책] 한도는 512 KiB이며 초과하면 일부만 쓰지 않고 **파일 전체를 거부**한다(`too_large`). 패턴 4096자 초과도 파일 전체를 거부한다
  (`pattern_too_long`). 뒤쪽의 Disallow가 잘려 허용 범위가 넓어지는 일을 막기 위함이다.

§2.1 인코딩
- [RFC] 파일은 UTF-8이어야 한다.
- [정책] 잘못된 UTF-8은 허용으로 이어지지 않게 한다: 규칙의 원시 바이트를 보존하고, 지시어 줄에 UTF-8 오류가 있으면
  `reliability == "invalid_utf8"`로 알린다(주석 안의 오류는 무시, 주석을 뺀 내용 줄이면 지시어 종류와 무관하게 센다).

신뢰도 신호 (P5-4에서 "이 응답은 robots.txt가 아니다"를 차단하기 위함)
- `Robots.info`(`ParseInfo`): 인식한 지시어 수, 무시한 줄 수, 오타 별칭 수, 첫 user-agent 앞 규칙 수, UTF-8 오류 줄 수, HTML·바이너리 여부.
- `Robots.reliability`: "ok" | "empty" | "binary" | "html" | "no_directives" | "no_groups" | "invalid_utf8". "ok"와 "empty"만 사용 가능(`usable`).
  "empty"(내용 없음·주석뿐)는 RFC상 규칙 없음 = 허용이지만 호출자가 구분할 수 있게 별도 코드로 알린다.
- `parse(data, strict=True)`는 사용할 수 없는 내용이면 `RobotsError(<reliability 코드>)`를 낸다. 호출자는 예외를 차단(미확인)으로 취급해야 한다.
- 잘못된 인자(에이전트·대상)도 예외(fail-closed)이며 "허용"으로 처리하지 않는다.
- 이 모듈의 `is_allowed`/`evaluate`는 순수 매칭이며 신뢰도를 자동으로 검사하지 않는다. 신뢰도 검사는 호출자의 책임이다.

비표준 확장
- `Crawl-delay`는 RFC 9309에 없다. 허용/차단 판정에 영향을 주지 않으며 `Robots.crawl_delay()`로만 별도 조회한다.
  값은 ASCII 숫자 형식(`5`, `2.5`)만 인정하고 0~86400 범위여야 하며, 같은 그룹의 여러 줄과 병합된 그룹 중 가장 큰 값을 쓴다.

범위 밖: 상태 코드(404/5xx 등)별 처리, redirect, 캐시, 호스트 한도는 SafeFetcher(P5-4)의 책임이다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

MAX_BYTES = 512 * 1024
MAX_PATTERN_LEN = 4096
MAX_CRAWL_DELAY = 86400.0
MAX_TARGET_LEN = 8192

IMPLICIT_ROBOTS_PATH = "/robots.txt"
USABLE_RELIABILITY = ("ok", "empty")
RELIABILITY_CODES = ("ok", "empty", "binary", "html", "no_directives", "no_groups", "invalid_utf8")

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_RESERVED = frozenset(":/?#[]@!$&'()*+,;=")
_PATH_OK = _UNRESERVED | _RESERVED  # '%' 는 별도로 처리, 나머지 ASCII 는 %XX 로 정규화한다
_HEX = frozenset("0123456789abcdefABCDEF")
_LINE_SPLIT = re.compile(r"\r\n|\n|\r")
_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]{1,64}")
_TARGET_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\\]")
_RAW_BYTE_ESCAPE = re.compile("[\udc80-\udcff]")  # surrogateescape 로 보존한 원시 바이트
_ANY_SURROGATE = re.compile("[\ud800-\udfff]")
_DELAY_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_WS_RUN = re.compile(r"\s+")
_HTML_MARKERS = ("<!doctype html", "<html", "</html", "<body", "</body", "<head>", "<?xml")

_DISALLOW_TYPOS = frozenset(("disalow", "dissalow", "dissallow", "diasllow", "disallaw"))
_AGENT_TYPOS = frozenset(("user agent", "useragent"))


class RobotsError(ValueError):
    """고정 코드만 담는 오류. 호출자는 이를 차단(미확인)으로 취급해야 한다."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str  # 퍼센트 인코딩 정규화를 마친 패턴


@dataclass(frozen=True)
class Group:
    agents: tuple
    rules: tuple
    crawl_delay: float | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    group: str  # "specific" | "wildcard" | "none"
    rule: Rule | None
    reason: str = ""  # "implicit_robots_txt" 등. 일반 판정은 빈 문자열


@dataclass(frozen=True)
class ParseInfo:
    """파싱 결과 통계 (허용/차단 판정에는 쓰이지 않고 신뢰도 판단용이다)."""

    bytes_len: int = 0
    content_lines: int = 0  # 주석을 지운 뒤 내용이 남은 줄
    recognized_lines: int = 0  # 인식한 지시어 줄 (오타 별칭 포함)
    user_agent_lines: int = 0
    rule_lines: int = 0  # allow/disallow 줄 (빈 값 포함)
    sitemap_lines: int = 0
    crawl_delay_lines: int = 0
    typo_alias_lines: int = 0
    ignored_lines: int = 0  # 콜론 없음, 미등록 지시어, 빈 user-agent 값
    orphan_rule_lines: int = 0  # 첫 user-agent 앞의 규칙
    invalid_utf8_lines: int = 0  # 주석을 뺀 내용 줄 중 UTF-8 오류가 있는 줄
    binary: bool = False
    looks_like_html: bool = False


# ---------------------------------------------------------------------------
# 정규화·매칭
# ---------------------------------------------------------------------------


def _char_bytes(ch: str) -> bytes:
    code_point = ord(ch)
    if 0xDC80 <= code_point <= 0xDCFF:  # surrogateescape 로 보존한 원시 바이트
        return bytes([code_point - 0xDC00])
    return ch.encode("utf-8", errors="surrogatepass")


def normalize_path(text: str) -> str:
    """퍼센트 인코딩 정규화 (RFC 9309 2.2.2 + 당사 보수적 확장: 날것으로 올 수 없는 ASCII 문자도 %XX 로 통일)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "%":
            pair = text[i + 1:i + 3]
            if len(pair) == 2 and pair[0] in _HEX and pair[1] in _HEX:
                decoded = chr(int(pair, 16))
                out.append(decoded if decoded in _UNRESERVED else "%" + pair.upper())
                i += 3
                continue
            out.append("%25")  # 잘못된 '%'는 문자 그대로 비교하도록 인코딩한다 (양쪽에 같은 규칙)
            i += 1
        elif ord(ch) > 127:
            out.extend("%%%02X" % b for b in _char_bytes(ch))
            i += 1
        elif ch in _PATH_OK:
            out.append(ch)
            i += 1
        else:
            out.append("%%%02X" % ord(ch))  # 공백·제어 문자·"<>\^`{|} 등
            i += 1
    return "".join(out)


def _matches(pattern: str, path: str) -> bool:
    """패턴이 경로의 앞부분과 일치하는지 (끝의 `$`는 끝 고정). 선형에 가까운 탐욕 매칭 — 정규식 역추적 폭발이 없다."""
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    parts = core.split("*")
    if len(parts) == 1:
        return path == core if anchored else path.startswith(core)
    first, last = parts[0], parts[-1]
    if not path.startswith(first):
        return False
    pos = len(first)
    for middle in parts[1:-1]:
        found = path.find(middle, pos)
        if found < 0:
            return False
        pos = found + len(middle)
    if anchored:
        return len(path) - len(last) >= pos and path.endswith(last)
    return path.find(last, pos) >= 0


def _make_rule(allow: bool, value: str):
    if not value:
        return None  # 빈 Disallow = 모두 허용(규칙 아님), 빈 Allow = 무시
    if len(value) > MAX_PATTERN_LEN:
        raise RobotsError("pattern_too_long")
    if not value.startswith(("/", "*")):
        if allow:
            return None
        value = "/" + value
    return Rule(allow, normalize_path(value))


def _product_token(agent) -> str:
    if not isinstance(agent, str):
        raise RobotsError("bad_agent")
    token = re.split(r"[/\s]", agent.strip(), maxsplit=1)[0]
    if not _PRODUCT_TOKEN.fullmatch(token):
        raise RobotsError("bad_agent")
    return token.lower()


def _target_path(target) -> str:
    if not isinstance(target, str) or not target or len(target) > MAX_TARGET_LEN or _TARGET_FORBIDDEN.search(target):
        raise RobotsError("bad_target")
    if target.startswith("//"):
        raise RobotsError("bad_target")  # 스킴 없는 URL(//host/path)을 경로로 오인해 허용하는 일을 막는다
    if target.startswith("/"):
        path = target.split("#", 1)[0]
    elif target.startswith(("https://", "http://")):
        parts = urlsplit(target)
        if not parts.netloc or "@" in parts.netloc:
            raise RobotsError("bad_target")
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    else:
        raise RobotsError("bad_target")
    return normalize_path(path)


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Robots:
    groups: tuple
    info: ParseInfo = field(default_factory=ParseInfo, compare=False)

    def _select(self, token: str):
        specific = [g for g in self.groups if token in g.agents]
        if specific:
            return specific, "specific"
        wildcard = [g for g in self.groups if "*" in g.agents]
        if wildcard:
            return wildcard, "wildcard"
        return [], "none"

    @property
    def reliability(self) -> str:
        """내용을 믿어도 되는지에 대한 신호 (`RELIABILITY_CODES` 중 하나)."""
        info = self.info
        if info.binary:
            return "binary"
        if info.looks_like_html:
            return "html"
        if info.content_lines == 0:
            return "empty"
        if info.recognized_lines == 0:
            return "no_directives"
        if info.user_agent_lines == 0 and (info.rule_lines or info.crawl_delay_lines):
            return "no_groups"
        if info.invalid_utf8_lines > 0:
            return "invalid_utf8"
        return "ok"

    @property
    def usable(self) -> bool:
        return self.reliability in USABLE_RELIABILITY

    def ensure_usable(self) -> "Robots":
        code = self.reliability
        if code not in USABLE_RELIABILITY:
            raise RobotsError(code)
        return self

    def evaluate(self, agent: str, target: str) -> Decision:
        """에이전트 제품 토큰(예: "MUWA-source-fetch" 또는 "MUWA-source-fetch/1.0")과 대상(경로+쿼리 또는 전체 URL)을 판정한다."""
        token = _product_token(agent)
        path = _target_path(target)
        groups, kind = self._select(token)
        if path == IMPLICIT_ROBOTS_PATH:
            return Decision(True, kind, None, "implicit_robots_txt")  # [RFC] /robots.txt 는 암묵적으로 허용
        best = None
        for group in groups:
            for rule in group.rules:
                if _matches(rule.pattern, path):
                    if best is None or len(rule.pattern) > len(best.pattern) \
                            or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow):
                        best = rule
        return Decision(True if best is None else best.allow, kind, best)

    def is_allowed(self, agent: str, target: str) -> bool:
        return self.evaluate(agent, target).allowed

    def crawl_delay(self, agent: str) -> float | None:
        """비표준 `Crawl-delay` (허용/차단 판정과 무관). 병합된 그룹 중 가장 큰 값, 없으면 None."""
        groups, _ = self._select(_product_token(agent))
        values = [g.crawl_delay for g in groups if g.crawl_delay is not None]
        return max(values) if values else None


# ---------------------------------------------------------------------------
# 파싱
# ---------------------------------------------------------------------------


def _parse_delay(value: str):
    if not _DELAY_RE.fullmatch(value):
        return None  # ASCII 숫자 형식만: 부호·지수·밑줄·유니코드 숫자·'5.' 모두 거부
    delay = float(value)
    if not math.isfinite(delay) or delay > MAX_CRAWL_DELAY:
        return None
    return delay


def _directive(name: str):
    """(정규 이름 | None, 오타 별칭 여부). 별칭은 명시한 목록만 인정한다."""
    key = _WS_RUN.sub(" ", name.strip().lower())
    if key in ("user-agent", "allow", "disallow", "crawl-delay", "sitemap"):
        return key, False
    if key in _DISALLOW_TYPOS:
        return "disallow", True
    if key in _AGENT_TYPOS:
        return "user-agent", True
    return None, False


_CONTROL_CODES = tuple(c for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)) + (0x7F,)  # 탭·CR·LF 는 일반 텍스트


def _looks_binary(raw: bytes | None, text: str) -> bool:
    """NUL 이 하나라도 있거나 제어 문자 비율이 1% 이상이면 바이너리로 본다 (count 는 C 로 실행되어 512 KiB 에서도 빠르다)."""
    if raw is not None:
        if not raw:
            return False
        if b"\x00" in raw:
            return True
        control = sum(raw.count(bytes((c,))) for c in _CONTROL_CODES)
        return control / len(raw) >= 0.01
    if "\x00" in text:
        return True
    control = sum(text.count(chr(c)) for c in _CONTROL_CODES)
    return bool(text) and control / len(text) >= 0.01


def parse(data, *, strict: bool = False) -> Robots:
    """robots.txt 내용(바이트 또는 문자열)을 파싱한다. 512 KiB 초과·패턴 과대 입력은 거부한다.

    strict=True 이면 `reliability`가 사용 불가(binary/html/no_directives/no_groups/invalid_utf8)일 때 `RobotsError`를 낸다.
    """
    raw: bytes | None
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if len(raw) > MAX_BYTES:
            raise RobotsError("too_large")
        text = raw.decode("utf-8", errors="surrogateescape")  # 잘못된 바이트를 대체 문자로 바꾸지 않고 보존한다
        size = len(raw)
    elif isinstance(data, str):
        raw = None
        size = len(data.encode("utf-8", errors="surrogatepass"))
        if size > MAX_BYTES:
            raise RobotsError("too_large")
        text = data
    else:
        raise RobotsError("bad_input")
    if text.startswith("\ufeff"):  # RFC 9309: UTF-8 BOM은 무시해야 한다
        text = text[1:]

    binary = _looks_binary(raw, text)
    escaped = len(_RAW_BYTE_ESCAPE.findall(text))
    if text and escaped / len(text) >= 0.10:
        binary = True
    lowered = text.lower()
    html = lowered.lstrip().startswith("<") or any(marker in lowered for marker in _HTML_MARKERS)

    groups: list[Group] = []
    agents: list[str] = []
    rules: list[Rule] = []
    delay = None
    in_agent_run = False
    counts = {"content": 0, "recognized": 0, "ua": 0, "rule": 0, "sitemap": 0, "delay": 0, "typo": 0, "ignored": 0,
              "orphan": 0, "bad_utf8": 0}

    def flush():
        if agents:
            groups.append(Group(tuple(agents), tuple(rules), delay))

    for raw_line in _LINE_SPLIT.split(text):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        counts["content"] += 1
        if _ANY_SURROGATE.search(line):
            counts["bad_utf8"] += 1
        name, sep, value = line.partition(":")
        directive, typo = _directive(name) if sep else (None, False)
        if directive is None:
            counts["ignored"] += 1
            continue
        value = value.strip()
        if directive == "user-agent":
            if not value:
                counts["ignored"] += 1
                continue
            counts["recognized"] += 1
            counts["ua"] += 1
            counts["typo"] += typo
            if not in_agent_run:
                flush()
                agents, rules, delay = [], [], None
            agents.append(value.lower())
            in_agent_run = True
        elif directive in ("allow", "disallow"):
            counts["recognized"] += 1
            counts["rule"] += 1
            counts["typo"] += typo
            in_agent_run = False
            if not agents:
                counts["orphan"] += 1
                continue
            rule = _make_rule(directive == "allow", value)
            if rule is not None:
                rules.append(rule)
        elif directive == "crawl-delay":  # 비표준: 그룹 경계에 영향을 주지 않는다
            counts["recognized"] += 1
            counts["delay"] += 1
            if agents:
                parsed = _parse_delay(value)
                if parsed is not None:
                    delay = parsed if delay is None else max(delay, parsed)
        else:  # sitemap: 인식하지만 그룹과 무관
            counts["recognized"] += 1
            counts["sitemap"] += 1
    flush()

    info = ParseInfo(
        bytes_len=size, content_lines=counts["content"], recognized_lines=counts["recognized"], user_agent_lines=counts["ua"],
        rule_lines=counts["rule"], sitemap_lines=counts["sitemap"], crawl_delay_lines=counts["delay"],
        typo_alias_lines=counts["typo"], ignored_lines=counts["ignored"], orphan_rule_lines=counts["orphan"],
        invalid_utf8_lines=counts["bad_utf8"], binary=binary, looks_like_html=html,
    )
    robots = Robots(tuple(groups), info)
    if strict:
        robots.ensure_usable()
    return robots
