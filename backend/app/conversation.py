"""S08 有限会话状态：按租户/员工/会话保存主题、已确认槽位与待补充字段。

会话只辅助理解用户问题，不保存历史 AI 回答或文档正文；每轮检索仍必须
重新读取当前授权资料。状态更新与消息答案在同一事务提交，重复消息和过期
Worker 不会重复推进。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConversationState, WeComInboundMessage

DEFAULT_CONVERSATION_KEY = "wecom:direct"
DEFAULT_CONVERSATION_TTL_MINUTES = 60

_LEAVE_TYPES = ("病假", "事假", "年假", "调休", "婚假", "产假", "陪产假", "丧假")
_DOMAIN_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("请假", ("请假", "休假", "病假", "事假", "年假", "调休", "婚假", "产假", "陪产假", "丧假")),
    ("报销", ("报销", "差旅", "交通费", "住宿费", "费用", "发票")),
    ("采购", ("采购", "供应商", "预算")),
    ("合同", ("合同", "法务", "盖章")),
    ("考勤", ("考勤", "打卡", "加班", "加班费")),
    ("薪资", ("薪资", "工资", "奖金", "绩效")),
    ("人事", ("入职", "离职", "转正", "试用期")),
    ("行政", ("会议", "预约", "访客", "门禁")),
    ("产品", ("产品", "保修", "退货", "物流", "发货", "说明书")),
    ("IT/安全", ("培训", "安全", "保密", "权限", "电脑", "VPN", "密码", "备份", "服务器")),
)
_CORRECTION_RE = re.compile(
    r"(?:不是|更正为|改为)\s*([^，,。；;\s]{1,20})[，,\s]*(?:是|而是|应为|改成)\s*([^，,。；;\s]{1,20})"
)
_SWITCH_RE = re.compile(r"^(?:换个话题|换一个话题|另一个问题|不说这个|说点别的)[，,。\s]*(.*)$")
_SMALLTALK_RE = re.compile(
    r"^(?:你好|您好|hi|hello|谢谢|多谢|感谢|辛苦了|再见|拜拜|在吗|早上好|中午好|下午好|晚上好)"
    r"[\s!！。.~～]*$", re.IGNORECASE
)
_FOLLOWUP_RE = re.compile(
    r"^(?:那|还有|这个|那个|它|需要|要|多久|多长时间|什么时候|怎么|如何|流程|步骤|条件|材料|时间|地点|谁|哪个|可以|能不能|是否|为什么|呢|吗)"
)


@dataclass
class ConversationSnapshot:
    topic: str | None = None
    confirmed: dict[str, str] = field(default_factory=dict)
    pending_fields: list[str] = field(default_factory=list)
    pending_prompt: str | None = None
    last_query: str | None = None
    version: int = 0
    expires_at: datetime | None = None


@dataclass
class ConversationResolution:
    query: str
    topic: str | None
    confirmed: dict[str, str]
    pending_fields: list[str]
    pending_prompt: str | None
    last_query: str | None
    source: str
    state_version: int
    correction: bool = False
    clarification: str | None = None
    should_persist: bool = True


def _first_domain(text: str) -> str | None:
    for domain, terms in _DOMAIN_TERMS:
        if any(term in text for term in terms):
            return domain
    return None


def _leave_type(text: str) -> str | None:
    return next((item for item in _LEAVE_TYPES if item in text), None)


def _topic(confirmed: dict[str, str], fallback: str | None = None) -> str | None:
    leave_type = confirmed.get("leave_type")
    domain = confirmed.get("domain")
    if leave_type:
        return f"{leave_type}{domain or '请假'}"
    if domain:
        return domain
    return fallback


def _compose(topic: str | None, text: str) -> str:
    if not topic:
        return text
    if not text:
        return topic
    if topic in text:
        return text
    if text in topic:
        return topic
    return f"{text} {topic}"


def _short_followup(text: str) -> bool:
    if len(text) > 20:
        return False
    if _leave_type(text) or _first_domain(text):
        return True
    return bool(_FOLLOWUP_RE.search(text))


def _is_smalltalk(text: str) -> bool:
    return bool(_SMALLTALK_RE.fullmatch(text.strip()))


def resolve(
    question: str, snapshot: ConversationSnapshot | None, *, now: datetime,
    ttl_minutes: int = DEFAULT_CONVERSATION_TTL_MINUTES, clarify_message: str,
) -> ConversationResolution:
    """合并当前问题与会话状态；只生成补全后的检索问题，不生成事实。"""
    text = re.sub(r"\s+", " ", question).strip()
    state = snapshot
    source = "continued" if state and state.topic else "new"
    if state is not None and state.expires_at is not None and state.expires_at <= now:
        state = None
        source = "expired"

    if _is_smalltalk(text):
        return ConversationResolution(
            text, state.topic if state else None, state.confirmed if state else {},
            state.pending_fields if state else [], state.pending_prompt if state else None,
            state.last_query if state else None, "chitchat", state.version if state else 0,
            should_persist=False)

    switch = _SWITCH_RE.match(text)
    if switch:
        state = None
        source = "topic_changed"
        text = switch.group(1).strip() or text

    correction = _CORRECTION_RE.search(text)
    confirmed = dict(state.confirmed) if state else {}
    correction_flag = False
    if correction:
        _, new_value = correction.group(1), correction.group(2)
        if _leave_type(new_value):
            confirmed["leave_type"] = _leave_type(new_value) or new_value
            confirmed.setdefault("domain", "请假")
            text = new_value
            source = "corrected"
        elif _first_domain(new_value):
            confirmed["domain"] = _first_domain(new_value) or new_value
            confirmed.pop("leave_type", None)
            text = new_value
            source = "corrected"
        correction_flag = True

    leave_type = _leave_type(text)
    domain = _first_domain(text)
    if leave_type:
        confirmed["leave_type"] = leave_type
        confirmed.setdefault("domain", "请假")
        if source == "continued":
            source = "continued_topic"
    if domain:
        confirmed["domain"] = domain
        if source == "new" and state and state.topic:
            source = "topic_changed"

    pending_fields = list(state.pending_fields) if state else []
    pending_prompt = state.pending_prompt if state else None
    if pending_fields and (leave_type or domain or correction_flag):
        pending_fields = []
        pending_prompt = None
        source = "pending_resolved"
    elif pending_fields and "topic" in pending_fields and not leave_type and not domain:
        confirmed["topic"] = text
        pending_fields = [item for item in pending_fields if item != "topic"]
        pending_prompt = None if not pending_fields else pending_prompt
        source = "pending_resolved"

    topic = _topic(confirmed, state.topic if state else None)
    if correction_flag or leave_type or domain:
        topic = _topic(confirmed, topic)

    if not leave_type and not domain and not correction_flag and state and state.topic \
            and not _short_followup(text):
        # 完整新问题没有明确主题线索时，不把旧主题强行带到新话题。
        confirmed = {}
        topic = None
        pending_fields = []
        pending_prompt = None
        source = "new"

    if topic and (_short_followup(text) or leave_type or domain):
        query = _compose(topic, text)
    else:
        query = text

    clarification = None
    if not topic and not state and _short_followup(text):
        clarification = clarify_message
        pending_fields = ["topic"]
        pending_prompt = clarify_message
        query = text
        source = "clarify"

    if not topic and not confirmed:
        topic = None

    return ConversationResolution(
        query=query,
        topic=topic,
        confirmed=confirmed,
        pending_fields=pending_fields,
        pending_prompt=pending_prompt,
        last_query=query,
        source=source,
        state_version=snapshot.version if snapshot else 0,
        correction=correction_flag,
        clarification=clarification,
        should_persist=clarification is not None or bool(topic) or snapshot is not None,
    )


async def load_snapshot(
    session: AsyncSession, *, tenant_id: UUID, user_id: UUID,
    conversation_key: str = DEFAULT_CONVERSATION_KEY,
) -> ConversationSnapshot | None:
    row = await session.scalar(select(ConversationState).where(
        ConversationState.tenant_id == tenant_id,
        ConversationState.user_id == user_id,
        ConversationState.conversation_key == conversation_key,
    ))
    if row is None:
        return None
    return ConversationSnapshot(
        topic=row.topic,
        confirmed=dict(row.confirmed_info or {}),
        pending_fields=list(row.pending_fields or []),
        pending_prompt=row.pending_prompt,
        last_query=row.last_query,
        version=row.version,
        expires_at=row.expires_at,
    )


async def apply_resolution(
    session: AsyncSession, message: WeComInboundMessage,
    resolution: ConversationResolution, *, now: datetime,
    ttl_minutes: int = DEFAULT_CONVERSATION_TTL_MINUTES,
    conversation_key: str = DEFAULT_CONVERSATION_KEY,
) -> bool:
    """在同一消息事务中推进状态；重复消息或版本过期时不覆盖较新状态。"""
    if not resolution.should_persist:
        return False
    row = await session.scalar(select(ConversationState).where(
        ConversationState.tenant_id == message.tenant_id,
        ConversationState.user_id == message.user_id,
        ConversationState.conversation_key == conversation_key,
    ).with_for_update())
    if row is None:
        row = ConversationState(
            tenant_id=message.tenant_id, user_id=message.user_id,
            conversation_key=conversation_key, version=1,
        )
        session.add(row)
    else:
        if row.last_message_id == message.id:
            return False
        if row.version != resolution.state_version:
            # 另一条消息已经推进了同一会话；不覆盖较新状态。
            return False
        row.version += 1
    row.topic = resolution.topic
    row.confirmed_info = dict(resolution.confirmed)
    row.pending_fields = list(resolution.pending_fields)
    row.pending_prompt = resolution.pending_prompt
    row.last_query = resolution.last_query
    row.last_message_id = message.id
    row.last_message_at = message.received_at
    row.expires_at = now + timedelta(minutes=max(1, ttl_minutes))
    row.updated_at = now
    return True
