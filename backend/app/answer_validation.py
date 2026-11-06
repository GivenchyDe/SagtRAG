"""S05 纯函数：严格 JSON 解码、摘录原文定位与确定性冲突检查。

这里不做网络调用，也不改变权限或候选范围。所有结果只用于提高校验精度；
无法确定时由调用方保守拒答或触发一次受限修复，不声称完成语义证明。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

HARMLESS_METADATA_KEYS = frozenset({
    "note", "notes", "explanation", "extra_note", "disclaimer", "comment",
})

CONFLICT_NUMERIC = "claim_numeric_conflict"
CONFLICT_UNIT = "claim_unit_conflict"
CONFLICT_NEGATION = "claim_negation_conflict"
CONFLICT_SUBJECT = "claim_subject_conflict"
CONFLICT_SUBJECT_VALUE = "claim_subject_value_conflict"
CONFLICT_SCOPE = "claim_scope_conflict"
CONFLICT_CONDITION = "claim_condition_conflict"
QUOTE_OVERBROAD = "quote_overbroad"


class OutputDecodeError(RuntimeError):
    """模型输出不能形成唯一完整 JSON 对象；code 区分语法与流不完整。"""

    def __init__(self, code: str, detail: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class _DuplicateKey(ValueError):
    pass


class _NonFiniteNumber(ValueError):
    pass


@dataclass(frozen=True)
class QuoteMatch:
    """允许的排版差异恢复后的真实原文摘录及其所在句/条款语境。"""

    original: str
    exact: bool
    formatting_repaired: bool
    context: str


def _reject_constant(value: str) -> None:
    raise _NonFiniteNumber(value)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _assert_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise _NonFiniteNumber(str(value))
    if isinstance(value, list):
        for item in value:
            _assert_finite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _assert_finite(item)


def decode_json_object(text: str) -> dict[str, Any]:
    """解码唯一完整 JSON 对象；拒绝围栏外文字、多对象、重复 key、NaN/Infinity。"""
    if not isinstance(text, str):
        raise OutputDecodeError("json_invalid", {"reason": "not_text"})
    content = text.strip()
    if not content:
        raise OutputDecodeError("json_invalid", {"reason": "empty"})
    if content.startswith("```"):
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```", content,
                              flags=re.IGNORECASE | re.DOTALL)
        if fenced is None or "```" in fenced.group(1):
            raise OutputDecodeError("json_invalid", {"reason": "fence_ambiguous"})
        content = fenced.group(1).strip()
        if not content:
            raise OutputDecodeError("json_invalid", {"reason": "empty"})
    try:
        value = json.loads(content, object_pairs_hook=_object_pairs,
                           parse_constant=_reject_constant)
    except _DuplicateKey:
        raise OutputDecodeError("json_invalid", {"reason": "duplicate_key"}) from None
    except _NonFiniteNumber:
        raise OutputDecodeError("json_invalid", {"reason": "non_finite_number"}) from None
    except json.JSONDecodeError as exc:
        reason = "multiple_objects" if "Extra data" in str(exc) else "syntax"
        raise OutputDecodeError("json_invalid", {"reason": reason}) from None
    try:
        _assert_finite(value)
    except _NonFiniteNumber:
        raise OutputDecodeError("json_invalid", {"reason": "non_finite_number"}) from None
    if not isinstance(value, dict):
        raise OutputDecodeError("json_invalid", {"reason": "root_not_object"})
    return value


def strip_harmless_metadata(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """只移除固定列出的非控制元信息；不启用全局 extra=ignore。"""
    if not isinstance(raw, dict):
        return raw, []
    cleaned = dict(raw)
    ignored = []
    for key in list(cleaned):
        if key in HARMLESS_METADATA_KEYS:
            cleaned.pop(key)
            ignored.append(str(key))
    return cleaned, ignored


def _normalize_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """去除排版空白并保留字符到原文的区间；数字之间空白不删除。"""
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            end = index
            while end < length and text[end].isspace():
                end += 1
            previous = chars[-1] if chars else ""
            following = text[end] if end < length else ""
            if previous.isdigit() and following.isdigit():
                chars.append(" ")
                spans.append((index, end))
            index = end
        else:
            chars.append(char)
            spans.append((index, index + 1))
            index += 1
    return "".join(chars), spans


def _normalize_text(text: str) -> str:
    normalized, _ = _normalize_with_spans(text)
    return normalized


_MARKDOWN_SKIP = frozenset("#|`*~>")


def _normalize_for_match(text: str) -> tuple[str, list[tuple[int, int]]]:
    """去掉 Markdown 结构符/列表编号并折叠空白，保留字符到原文的区间。

    只移除排版结构（标题 #、表格 |、强调与代码标记、行首列表编号），
    数字、百分号、符号和单位原样保留。
    """
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    at_line_start = True
    while index < length:
        char = text[index]
        if char == "\n":
            at_line_start = True
            index += 1
            continue
        if at_line_start:
            cursor = index
            while cursor < length and text[cursor] in " \t":
                cursor += 1
            marker = re.match(r"(?:[-*+]|\d+[.)])\s+", text[cursor:])
            if marker:
                index = cursor + marker.end()
                continue
        if char.isspace():
            end = index
            while end < length and text[end].isspace():
                end += 1
            previous = chars[-1] if chars else ""
            following = text[end] if end < length else ""
            if previous.isdigit() and following.isdigit():
                chars.append(" ")
                spans.append((index, end))
            index = end
            at_line_start = False
            continue
        if char in _MARKDOWN_SKIP:
            index += 1
            continue
        chars.append(char)
        spans.append((index, index + 1))
        at_line_start = False
        index += 1
    return "".join(chars), spans


def _number_boundary(text: str, start: int, end: int) -> bool:
    """引用不能是更长数字的一部分，例如不能用 130 中的 30 作为证据。"""
    if start < len(text) and text[start].isdigit():
        if start > 0 and text[start - 1].isdigit():
            return False
        if start > 1 and text[start - 1] == "." and text[start - 2].isdigit():
            return False
    if end > 0 and text[end - 1].isdigit():
        if end < len(text) and text[end].isdigit():
            return False
        if end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit():
            return False
    return True


def _quote_context(content: str, start: int, end: int) -> str:
    """取摘录所在的完整句/条款，保留同句否定、条件和例外。"""
    boundaries = []
    segment_start = 0
    for index, char in enumerate(content):
        if char in "。！？；;\n，,":
            boundaries.append((segment_start, index + 1))
            segment_start = index + 1
    if segment_start < len(content):
        boundaries.append((segment_start, len(content)))
    selected = [content[left:right].strip()
                for left, right in boundaries if right > start and left < end]
    if selected:
        return "\n".join(selected)
    # 极端情况下（无句末标点）退回有界窗口，避免只用模型选出的短 quote。
    return content[max(0, start - 80):min(len(content), end + 80)]


def match_quote(quote: str, content: str) -> QuoteMatch | None:
    """先逐字匹配；仅对空格/换行排版差异做有边界恢复并还原原文。"""
    value = quote.strip()
    if len(value) < 2:
        return None
    index = content.find(value)
    if index != -1 and _number_boundary(content, index, index + len(value)):
        end = index + len(value)
        return QuoteMatch(content[index:end], True, False, _quote_context(content, index, end))
    normalized, spans = _normalize_with_spans(content)
    wanted = _normalize_text(value)
    if not wanted:
        return None
    position = normalized.find(wanted)
    while position != -1:
        end_position = position + len(wanted)
        start = spans[position][0]
        end = spans[end_position - 1][1]
        if _number_boundary(content, start, end):
            return QuoteMatch(content[start:end], False, True,
                              _quote_context(content, start, end))
        position = normalized.find(wanted, position + 1)

    # 兼容 Markdown 标题、表格管道符、列表编号等排版差异。
    normalized, spans = _normalize_for_match(content)
    wanted, _ = _normalize_for_match(value)
    if not wanted:
        return None
    position = normalized.find(wanted)
    while position != -1:
        end_position = position + len(wanted)
        start = spans[position][0]
        end = spans[end_position - 1][1]
        if _number_boundary(content, start, end):
            return QuoteMatch(content[start:end], False, True,
                              _quote_context(content, start, end))
        position = normalized.find(wanted, position + 1)
    return None


_NUMBER_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(%|％|个工作日|工作日|日元|美元|美金|人民币|港元|港币|欧元|元|天|日|个月|小时|分钟|年|次|人|份|页)?"
)
_PERCENT_UNITS = {"%", "％"}
_CURRENCY_UNITS = {
    "元": "CNY", "人民币": "CNY", "美元": "USD", "美金": "USD",
    "港元": "HKD", "港币": "HKD", "欧元": "EUR", "日元": "JPY",
}
_DURATION_UNITS = {
    "天": "DAY", "日": "DAY", "个工作日": "WORKDAY", "工作日": "WORKDAY",
    "个月": "MONTH", "年": "YEAR", "小时": "HOUR", "分钟": "MINUTE",
}


def _number_unit(raw_unit: str | None) -> tuple[str | None, str | None]:
    if raw_unit in _PERCENT_UNITS:
        return "percent", "percent"
    if raw_unit in _CURRENCY_UNITS:
        return "currency", _CURRENCY_UNITS[raw_unit]
    if raw_unit in _DURATION_UNITS:
        return "duration", _DURATION_UNITS[raw_unit]
    if raw_unit:
        return "quantity", raw_unit
    return None, None


def _extract_numbers(text: str) -> list[tuple[Decimal, str | None, str | None]]:
    values = []
    for match in _NUMBER_RE.finditer(text):
        try:
            number = Decimal(match.group(1))
        except InvalidOperation:
            continue
        family, unit = _number_unit(match.group(2))
        values.append((number, family, unit))
    return values


_NEGATION_RE = re.compile(
    r"不(?:需要|可以|能|得|可|允许|予|再|适用|包括|超过|低于|少于|到|是|符合|一致)"
    r"|无需|无须|禁止|不得|严禁|除外|除非|取消|无效"
)
_SUBJECTS = ("正式员工", "试用期员工", "实习生", "外包员工", "外包人员", "劳务派遣",
             "部门负责人", "主管", "经理", "员工")
_UNIVERSAL_RE = re.compile(r"所有|全部|任何|一律|每位|均是|均可")
_PARTIAL_RE = re.compile(r"部分|个别|某些|仅|只|限于|限定|限额|限时")
_LOCATION_SCOPE = ("本市", "跨城", "异地", "本地", "外地", "境内", "境外", "省内", "省外")
_CONDITION_RE = re.compile(
    r"除非|除.{0,20}(?:外|之外)|例外|不适用|仅限|仅限于|适用于|满足.{0,16}(?:条件|要求)"
    r"|连续|满\d|超过\d|经.{0,12}(?:批准|审批)|需.{0,12}(?:审批|批准|同意)"
)


def claim_has_condition(text: str) -> bool:
    """判断结论是否包含条件/例外/范围限定，供局部保留安全判断。"""
    return bool(_CONDITION_RE.search(text) or _UNIVERSAL_RE.search(text)
                or _PARTIAL_RE.search(text))


def _subjects(text: str) -> set[str]:
    return {subject for subject in _SUBJECTS if subject in text}


_BINDING_SUFFIX_RE = re.compile(
    r"(?:的)?(?:上限|下限|标准|额度|比例|期限|费用|价格|金额|费率|税率|"
    r"适用|超过|不超过|不得超过|最多|最高|至少|不低于|为|是|需|须|应|可|可以|"
    r"不得|不能|每人|每次|单笔|费)$"
)


def _canonical_subject(prefix: str) -> str:
    value = re.sub(r"[\s，,。；;：:、（）()【】\[\]]", "", prefix)[-20:]
    changed = True
    while changed and len(value) > 2:
        changed = False
        new_value = _BINDING_SUFFIX_RE.sub("", value)
        if new_value != value:
            value = new_value
            changed = True
    return value


def _subject_value_bindings(
    text: str,
) -> dict[str, list[tuple[Decimal, str | None, str | None]]]:
    """提取“主体/费用项—数值—单位”绑定，防止数值集合相同但主体颠倒。"""
    result: dict[str, list[tuple[Decimal, str | None, str | None]]] = {}
    for clause in re.split(r"[，,；;。！？\n]", text):
        for match in _NUMBER_RE.finditer(clause):
            subject = _canonical_subject(clause[:match.start()])
            if len(subject) < 2:
                continue
            try:
                number = Decimal(match.group(1))
            except InvalidOperation:
                continue
            family, unit = _number_unit(match.group(2))
            result.setdefault(subject, []).append((number, family, unit))
    return result


def detect_claim_conflicts(text: str, evidence_texts: list[str], *,
                           full_evidence_texts: list[str] | None = None) -> list[str]:
    """确定性检查明显冲突；返回空列表只表示未发现，不表示已证明语义支持。

    full_evidence_texts 用于“引文只是短摘录、但数字在同一 chunk 其他位置”的情况：
    数字在该 chunk 内能定位时不再报数字冲突；单位/否定/主体/条件仍按引文语境检查。
    """
    text = unicodedata.normalize("NFKC", text)
    evidence = unicodedata.normalize("NFKC", "\n".join(evidence_texts))
    full_evidence = unicodedata.normalize(
        "NFKC", "\n".join(full_evidence_texts if full_evidence_texts is not None else evidence_texts))
    codes: list[str] = []
    claim_numbers = _extract_numbers(text)
    evidence_numbers = _extract_numbers(evidence)
    full_numbers = _extract_numbers(full_evidence)
    claim_by_family: dict[str, list[tuple[Decimal, str | None]]] = {}
    evidence_by_family: dict[str, list[tuple[Decimal, str | None]]] = {}
    full_by_family: dict[str, list[tuple[Decimal, str | None]]] = {}
    for number, family, unit in claim_numbers:
        if family:
            claim_by_family.setdefault(family, []).append((number, unit))
    for number, family, unit in evidence_numbers:
        if family:
            evidence_by_family.setdefault(family, []).append((number, unit))
    for number, family, unit in full_numbers:
        if family:
            full_by_family.setdefault(family, []).append((number, unit))
    for family, claim_values in claim_by_family.items():
        evidence_values = evidence_by_family.get(family)
        full_values = full_by_family.get(family) or []
        full_set = {number for number, _ in full_values}
        if not evidence_values:
            # 引文语境没有同族数量：若该数量能在同一 chunk 内定位，则不报数字冲突。
            if evidence_numbers and not all(number in full_set for number, _ in claim_values):
                codes.append(CONFLICT_NUMERIC)
            continue
        evidence_set = {number for number, _ in evidence_values}
        missing = [(number, unit) for number, unit in claim_values if number not in evidence_set]
        if missing:
            if not all(number in full_set for number, _ in missing):
                codes.append(CONFLICT_NUMERIC)
                continue
            # 数字在 chunk 内其他位置可定位；检查单位是否能在该 chunk 的对应数字上找到。
            for number, unit in missing:
                if not unit:
                    continue
                full_units = {candidate_unit for candidate_number, candidate_unit in full_values
                              if candidate_number == number and candidate_unit}
                if full_units and unit not in full_units:
                    codes.append(CONFLICT_UNIT)
            continue
        claim_units = {unit for _, unit in claim_values if unit}
        evidence_units = {unit for _, unit in evidence_values if unit}
        if claim_units and evidence_units and claim_units.isdisjoint(evidence_units):
            codes.append(CONFLICT_UNIT)

    claim_bindings = _subject_value_bindings(text)
    evidence_bindings = _subject_value_bindings(evidence)
    for subject, claim_values in claim_bindings.items():
        evidence_values = evidence_bindings.get(subject)
        if not evidence_values:
            continue
        evidence_set = {(number, family, unit) for number, family, unit in evidence_values}
        if any((number, family, unit) not in evidence_set
               for number, family, unit in claim_values):
            codes.append(CONFLICT_SUBJECT_VALUE)

    if bool(_NEGATION_RE.search(text)) != bool(_NEGATION_RE.search(evidence)):
        codes.append(CONFLICT_NEGATION)

    claim_subjects = _subjects(text) - {"员工"}
    evidence_subjects = _subjects(evidence) - {"员工"}
    if claim_subjects and evidence_subjects and claim_subjects.isdisjoint(evidence_subjects):
        codes.append(CONFLICT_SUBJECT)

    if _UNIVERSAL_RE.search(text) and _PARTIAL_RE.search(evidence) and not _UNIVERSAL_RE.search(evidence):
        codes.append(CONFLICT_SCOPE)

    claim_locations = {item for item in _LOCATION_SCOPE if item in text}
    evidence_locations = {item for item in _LOCATION_SCOPE if item in evidence}
    if claim_locations and evidence_locations and claim_locations.isdisjoint(evidence_locations):
        codes.append(CONFLICT_SCOPE)

    if _CONDITION_RE.search(evidence) and not _CONDITION_RE.search(text):
        codes.append(CONFLICT_CONDITION)

    return sorted(set(codes))


def quote_has_unreferenced_numeric_fact(claim: str, quote: str) -> bool:
    """多句引文中，后续句子的数量事实若未出现在结论中，视为过度引用。"""
    sentences = [item for item in re.split(r"(?<=[。！？；])", quote) if item.strip()]
    if len(sentences) < 2:
        return False
    claim_numbers = {(number, family) for number, family, _ in _extract_numbers(claim) if family}
    for sentence in sentences[1:]:
        for number, family, _ in _extract_numbers(sentence):
            if family and (number, family) not in claim_numbers:
                return True
    return False


_UNSAFE_REASON_RE = re.compile(
    r"\d|%|％|元|美元|人民币|必须|应当|不得|禁止|所有|任何|一律"
)

SAFE_MISSING_REASON_TEXT = "抱歉，我在现有资料里没有找到足够的依据。是不是问题里的名称或关键词有点偏差？可以补充具体的制度、流程或文档名称，我再帮你查。"


def safe_missing_reason(reason: str) -> str:
    """拒答说明只允许简短“缺少什么”，带数字/事实断言时退回固定话术。"""
    value = (reason or "").strip()
    if not value:
        return SAFE_MISSING_REASON_TEXT
    if value.startswith("当前资料未覆盖"):
        value = value[len("当前资料未覆盖"):].lstrip("：:，, ")
    if not value or _UNSAFE_REASON_RE.search(value):
        return SAFE_MISSING_REASON_TEXT
    return f"当前资料未覆盖：{value[:80]}"
