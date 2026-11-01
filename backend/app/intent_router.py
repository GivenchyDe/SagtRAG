"""企业微信消息的意图路由：确定性规则优先，必要时调用轻量 LLM 分类。

路由只决定消息进入闲聊、知识库还是超范围通道，不改变认证、权限或 RLS 边界。
无法确定时一律退化为知识问答：最坏结果是一次拒答，而不是无依据的闲聊回答。
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Literal

import httpx

from app.config import Settings, get_settings

Intent = Literal["chitchat", "knowledge_query", "out_of_scope"]

INTENTS: tuple[Intent, ...] = ("chitchat", "knowledge_query", "out_of_scope")

INTENT_SYSTEM_PROMPT = """你是意图分类器。只输出一个 JSON 对象，不要输出解释或 Markdown。

意图定义：
- knowledge_query：任何需要解释、介绍、说明或查询事实的问题，包括企业内部资料和通用知识问题（例如"介绍一下操作系统""进程和线程的区别"）。
- chitchat：仅限问候、感谢、告别、寒暄、能力介绍和简短日常交流。
- out_of_scope：要求执行系统操作（运行代码、读写文件、发消息、查数据库、调用外部接口）或明显超出服务范围。

判定规则：
1. 实质性知识问题一律判 knowledge_query，即使看起来是通用常识。
2. 只要涉及企业内部事实，即使同时包含问候，也判 knowledge_query。
3. 无法确定时判 knowledge_query。
4. 不得臆测用户未提供的信息。

输出格式：{"intent":"knowledge_query|chitchat|out_of_scope","confidence":0.00,"reason":"不超过20字"}
"""

# 企业领域标记：出现即按知识问答处理。宁可把通用问题保守地送进知识库，
# 也不要把企业问题交给闲聊模型自由发挥。
_KNOWLEDGE_MARKERS = (
    "制度", "流程", "规范", "政策", "手册", "文档", "产品", "说明书", "报销", "请假",
    "审批", "合同", "供应商", "客户", "项目", "权限", "密级", "工单", "保修", "退货",
    "操作步骤", "部署", "服务器", "数据库", "工资", "薪资", "考勤", "培训", "安全",
    "保密", "会议", "预约", "发票", "预算", "采购", "入职", "离职", "转正", "绩效",
    "考核", "联系方式", "地址",
)

_READ_WRITE_VERBS = r"(?:删除|删掉|删了|清空|格式化|修改|写入|读取)"
_SYSTEM_OBJECTS = r"(?:数据库|数据表|表|文件|磁盘|服务器|目录|数据)"
_EXECUTE_VERBS = r"(?:执行|运行|启动|停止|关闭|重启)"
_EXECUTE_OBJECTS = r"(?:代码|命令|脚本|程序|服务|容器|服务器)"
_SEND_VERBS = r"(?:发送|群发|发)"
_MESSAGE_OBJECTS = r"(?:邮件|短信|消息|通知)"

# 只匹配高置信度的"代做"请求；"怎么发送邮件""删除数据库的操作步骤"交给知识通道。
_SYSTEM_ACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^(?:请|帮我|帮忙|麻烦|给我|替我)\s*{_EXECUTE_VERBS}(?:一下)?{_EXECUTE_OBJECTS}"),
    re.compile(rf"^(?:请|帮我|帮忙|麻烦|给我|替我)\s*{_READ_WRITE_VERBS}(?:一下)?{_SYSTEM_OBJECTS}"),
    re.compile(rf"^(?:把|将)\s*{_SYSTEM_OBJECTS}.{{0,8}}{_READ_WRITE_VERBS}"),
    re.compile(rf"^(?:请|帮我|帮忙|麻烦|给我|替我)\s*(?:把|将)\s*{_SYSTEM_OBJECTS}.{{0,8}}{_READ_WRITE_VERBS}"),
    re.compile(rf"^(?:请|帮我|帮忙|麻烦|给我|替我)\s*(?:给|向|对).{{0,8}}{_SEND_VERBS}\s*(?:一封|一条|个)?\s*{_MESSAGE_OBJECTS}"),
    re.compile(rf"^(?:请|帮我|帮忙|麻烦|给我|替我)\s*{_SEND_VERBS}\s*(?:一封|一条|个)?\s*{_MESSAGE_OBJECTS}"),
)

_INJECTION_PATTERN = re.compile(
    r"忽略.{0,8}(?:规则|指令|提示)|系统提示词?|提示词|"
    r"(?:输出|告诉我|显示).{0,8}(?:密钥|凭据|口令|token|secret)|"
    r"(?:密钥|凭据|access_token|secret)\s*[:：]",
    re.IGNORECASE,
)

_REALTIME_PATTERN = re.compile(
    r"^(?:今天|今日|现在|当前|最近|实时).{0,10}(?:天气|气温|下雨|股票|股价|汇率|新闻|热搜|比分)|"
    r"^(?:天气|气温|股票|股价|汇率|新闻|热搜|比分)"
)

_CHITCHAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:你好|您好|hi|hello|在吗|早上好|中午好|下午好|晚上好|你好呀)[\s!！。.~～]*$", re.IGNORECASE), "greeting"),
    (re.compile(r"^(?:谢谢|多谢|感谢|辛苦了)(?:[，,\s]*(?:谢谢|多谢|感谢|辛苦了))*[\s!！。.~～]*$"), "thanks"),
    (re.compile(r"^(?:你好|您好)[，,\s]*(?:谢谢|多谢|感谢|辛苦了)[\s!！。.~～]*$"), "thanks"),
    (re.compile(r"^(?:再见|拜拜|bye|goodbye)[\s!！。.~～]*$", re.IGNORECASE), "farewell"),
    (re.compile(r"^(?:你是谁|你是什么模型|你叫什么|你叫什么名字|你能做什么|你可以做什么|你会什么|你有什么功能|介绍一下你自己)[\s?？!！。.~～]*$"), "capability"),
)

_ANAPHORA_PATTERN = re.compile(
    r"^(?:这个|那个|这条|那条|上面|刚才|前面|接着说|继续说|继续|然后呢|还有呢|这个呢|那个呢)[\s?？!！。.~～]*$"
)

# 实质性知识问题：即使没有命中企业词表，也优先进入知识通道。
# 通用技术问题也可能由企业资料覆盖，交给闲聊模型会给出无依据回答。
_SUBSTANTIVE_PATTERN = re.compile(
    r"介绍|解释|说明|什么是|是什么|为什么|为何|如何|怎么|怎样|"
    r"包括|包含|有哪些|有什么|区别|原理|流程|步骤|作用|定义|"
    r"多少|是否|能否|可不可以|怎么样"
)


def normalize(text: str) -> str:
    """只用于规则匹配的归一化；原始消息仍交给模型和存储。"""
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(" \t\r\n!！?？。.~～,，;；:：")
    return value.lower()


@dataclass(frozen=True)
class RuleMatch:
    intent: Intent
    rule_id: str
    reason: str | None = None


@dataclass(frozen=True)
class IntentResult:
    predicted_intent: Intent | None
    intent: Intent
    confidence: float | None
    source: str
    model: str | None
    rule_id: str | None
    reason: str | None
    elapsed_ms: int
    error: str | None
    policy_version: str


def build_policy_version(settings: Settings) -> str:
    """组合版本用于回放：规则、提示词和开关任一变化都能区分。"""
    mode = "on" if settings.intent_routing_enabled else "off"
    return f"{settings.intent_rules_version}|{settings.intent_prompt_version}|mode={mode}"


def classify_by_rules(normalized: str) -> RuleMatch | None:
    """确定性规则；返回 None 表示需要分类器。"""
    if not normalized:
        return RuleMatch("chitchat", "empty_input", "empty_input")
    if _ANAPHORA_PATTERN.fullmatch(normalized):
        return RuleMatch("chitchat", "clarify_anaphora", "clarify_required")
    for pattern in _SYSTEM_ACTION_PATTERNS:
        if pattern.search(normalized):
            return RuleMatch("out_of_scope", "system_action", "system_action")
    if _INJECTION_PATTERN.search(normalized):
        return RuleMatch("knowledge_query", "guard_injection", "injection_guard")
    if any(marker in normalized for marker in _KNOWLEDGE_MARKERS):
        return RuleMatch("knowledge_query", "knowledge_marker", "enterprise_marker")
    if _REALTIME_PATTERN.search(normalized):
        return RuleMatch("out_of_scope", "realtime_unavailable", "realtime_unavailable")
    for pattern, rule_id in _CHITCHAT_PATTERNS:
        if pattern.fullmatch(normalized):
            return RuleMatch("chitchat", rule_id)
    if _SUBSTANTIVE_PATTERN.search(normalized):
        return RuleMatch("knowledge_query", "substantive_question", "substantive_question")
    return None


def extract_json_object(text: str) -> dict:
    """从模型输出中找到唯一的 JSON 对象；多对象或缺失都视为失败。"""
    decoder = json.JSONDecoder()
    found: dict | None = None
    position = 0
    while True:
        index = text.find("{", position)
        if index < 0:
            break
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            position = index + 1
            continue
        if isinstance(value, dict):
            if found is not None:
                raise ValueError("multiple json objects")
            found = value
            position = index + end
        else:
            position = index + 1
    if found is None:
        raise ValueError("json object not found")
    return found


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _has_enterprise_marker(normalized: str) -> bool:
    return any(marker in normalized for marker in _KNOWLEDGE_MARKERS)


async def detect_intent(question: str, *, settings: Settings | None = None) -> IntentResult:
    """规则优先；未命中才调用分类器。任何失败都退化为知识问答。"""
    settings = settings or get_settings()
    started = time.perf_counter()
    normalized = normalize(question)
    version = build_policy_version(settings)

    if not settings.intent_routing_enabled:
        return IntentResult(None, "knowledge_query", None, "disabled", None, None, "routing_disabled",
                            _elapsed_ms(started), None, version)

    rule = classify_by_rules(normalized)
    if rule is not None:
        return IntentResult(rule.intent, rule.intent, 1.0, "rule", None, rule.rule_id, rule.reason,
                            _elapsed_ms(started), None, version)

    predicted, confidence, model, error = await _classify_with_llm(question, settings)
    if predicted is None:
        return IntentResult(None, "knowledge_query", None, "fallback", model, None, "classifier_failed",
                            _elapsed_ms(started), error, version)
    if predicted == "chitchat" and (_has_enterprise_marker(normalized) or _INJECTION_PATTERN.search(normalized)):
        return IntentResult(predicted, "knowledge_query", confidence, "llm_guard", model, "llm_guard",
                            "enterprise_or_injection", _elapsed_ms(started), None, version)
    if confidence is None or confidence < settings.intent_confidence_threshold:
        return IntentResult(predicted, "knowledge_query", confidence, "fallback", model, "low_confidence",
                            "low_confidence", _elapsed_ms(started), None, version)
    return IntentResult(predicted, predicted, confidence, "llm", model, "llm", None,
                        _elapsed_ms(started), None, version)


async def _classify_with_llm(
    question: str, settings: Settings,
) -> tuple[Intent | None, float | None, str, str | None]:
    base = str(settings.intent_base_url or settings.generation_base_url).rstrip("/")
    model = settings.intent_model or settings.generation_model
    attempts = max(1, settings.intent_retry + 1)
    last_error = "intent_provider_error"
    for _ in range(attempts):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.intent_timeout_seconds,
                                      connect=min(3.0, settings.intent_timeout_seconds)),
            ) as client:
                async with client.stream(
                    "POST", f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"},
                    json={
                        "model": model, "stream": True, "temperature": 0, "max_tokens": 128,
                        "messages": [
                            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                            {"role": "user", "content": question},
                        ],
                    },
                ) as response:
                    response.raise_for_status()
                    pieces: list[str] = []
                    async for line in response.aiter_lines():
                        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                            continue
                        event = json.loads(line[5:])
                        if "error" in event:
                            raise ValueError("provider stream error")
                        for choice in event.get("choices", []):
                            pieces.append(choice.get("delta", {}).get("content") or "")
            content = "".join(pieces).strip()
            if not content:
                raise ValueError("empty classifier output")
            data = extract_json_object(content)
            intent = data["intent"]
            confidence = data["confidence"]
            if intent not in INTENTS:
                raise ValueError("invalid intent")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValueError("invalid confidence")
            confidence = float(confidence)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("invalid confidence")
            return intent, confidence, model, None
        except httpx.TimeoutException:
            last_error = "intent_timeout"
        except httpx.HTTPError:
            last_error = "intent_http_error"
        except (ValueError, KeyError, TypeError, IndexError):
            last_error = "intent_invalid_response"
    return None, None, model, last_error
