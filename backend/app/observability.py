"""S04 可观测性：细分诊断码、阶段计时、尝试记录与策略摘要。

本模块只做记录，不改变检索、引用校验、权限或回答内容。所有摘要字段
均来自显式白名单，不包含密钥、凭据、数据库 URL 或端点完整地址。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any

# 统一诊断码：outcome 保持原有取值，诊断码只解释“为什么落到该 outcome”。
SCHEMA_INVALID = "schema_invalid"
FIELD_TYPE_INVALID = "field_type_invalid"
CHUNK_ID_INVALID = "chunk_id_invalid"
SOURCE_NOT_WHITELISTED = "source_not_whitelisted"
QUOTE_TOO_SHORT = "quote_too_short"
QUOTE_MISMATCH = "quote_mismatch"
QUOTE_OVERBROAD = "quote_overbroad"
CLAIM_SUBJECT_VALUE_CONFLICT = "claim_subject_value_conflict"
ANSWER_TOO_LONG = "answer_too_long"
MODEL_REFUSED = "model_refused"
EMPTY_CLAIMS = "empty_claims"
INVALID_CONTROL_COMBINATION = "invalid_control_combination"
ANSWER_PARTIAL = "answered_partial"
GENERATION_BUDGET_EXHAUSTED = "generation_budget_exhausted"
JSON_INVALID = "json_invalid"
GENERATION_INCOMPLETE = "generation_incomplete"
NO_CANDIDATES = "no_candidates"
BELOW_RELEVANCE = "below_relevance_threshold"
EMPTY_QUESTION = "empty_question"
PERMISSION_CHANGED = "permission_changed"
PROCESSING_DEADLINE_EXCEEDED = "processing_deadline_exceeded"
IDENTITY_REVOKED = "identity_revoked"

# 这些 outcome 属于“流程完成”，不代表系统异常；拒答也在其中。
COMPLETED_OUTCOMES = frozenset({"answered", "no_answer", "chitchat", "out_of_scope"})


def is_failure_outcome(outcome: str) -> bool:
    """区分完成（含拒答）与异常/校验失败，便于尝试记录标记状态。"""
    return outcome not in COMPLETED_OUTCOMES

# 允许写入 policy.config 的配置字段白名单；禁止加入 SecretStr、URL 或凭据字段。
CONFIG_FIELDS: tuple[str, ...] = (
    "generation_model",
    "embedding_model",
    "rerank_model",
    "rerank_target_model",
    "rerank_rollout_mode",
    "rerank_target_min_relevance",
    "rerank_target_threshold_calibrated",
    "rag_recall_limit",
    "rag_context_limit",
    "rag_generation_input_chars",
    "generation_max_tokens",
    "rag_processing_deadline_seconds",
    "rag_min_relevance",
    "rag_message_retention_days",
    "intent_routing_enabled",
    "intent_model",
    "intent_timeout_seconds",
    "intent_retry",
    "intent_confidence_threshold",
    "intent_rules_version",
    "intent_prompt_version",
    "chitchat_model",
    "chitchat_max_tokens",
    "chitchat_max_bytes",
    "asr_model",
)

MAX_RECORDED_ATTEMPTS = 10
MAX_RECORDED_SEND_ATTEMPTS = 10

# 这些字段只描述“最近一次处理尝试”。开始新尝试前清空，避免旧失败阶段残留；
# 旧值已在对应 attempt 的 timings 中保留。
ATTEMPT_TIMING_KEYS: tuple[str, ...] = (
    "processing_ms", "retrieval_ms", "embedding_ms", "dense_ms", "fts_ms", "rrf_ms",
    "rerank_ms", "generation_ms", "generation_first_content_ms", "rag_total_ms",
    "chitchat_ms", "chitchat_first_content_ms",
)
VOICE_TIMING_KEYS: tuple[str, ...] = ("voice_ms", "asr_ms", "download_ms", "convert_ms")
TASK_TIMING_KEYS: tuple[str, ...] = (
    "generation_calls", "repair_attempted", "repair_succeeded", "repair_reason",
    "initial_error_code", "final_error_code", "validation_state", "claims_dropped",
)
ATTEMPT_ARTIFACT_KEYS: tuple[str, ...] = (
    "error_code", "diagnostic_code", "diagnostic_detail", "candidates",
    "dense_order", "fts_order", "rerank_scores", "selected_chunk_ids", "cited_chunk_ids",
)

_FIELD_TYPE_ERRORS = frozenset({
    "string_type", "int_type", "float_type", "bool_type", "bytes_type",
    "decimal_type", "date_type", "datetime_type", "time_type", "timedelta_type",
    "path_type", "uuid_type", "list_type", "tuple_type", "set_type", "frozenset_type",
    "dict_type", "model_type", "url_type",
})
_UUID_ERRORS = frozenset({"uuid_type", "uuid_parsing"})


def elapsed_ms(started: datetime | None, finished: datetime | None) -> int | None:
    """返回两个时间点之间的毫秒数；任一缺失时返回 None，不用 0 冒充缺测。"""
    if started is None or finished is None:
        return None
    return max(0, round((finished - started).total_seconds() * 1000))


def iso_utc(value: datetime | None) -> str | None:
    """把时间点写为可 JSON 持久化的 ISO 字符串。"""
    return value.isoformat() if value is not None else None


def policy_snapshot(settings: Any, *, policy_version: str | None = None) -> dict[str, Any]:
    """保存用于对照的策略版本与配置摘要，不包含密钥、URL 或原始提示词。"""
    config: dict[str, Any] = {}
    for key in CONFIG_FIELDS:
        if not hasattr(settings, key):
            continue
        value = getattr(settings, key)
        if value is not None:
            config[key] = value
    version = policy_version
    if version is None:
        version = getattr(settings, "intent_policy_version", None)
    source_whitelist: dict[str, Any] = {
        "predicate": "visible_chunks",
        "active_document_only": True,
        "active_index_only": True,
        "embedding_dimension": 1024,
    }
    if "embedding_model" in config:
        source_whitelist["embedding_model"] = config["embedding_model"]
    digest = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "policy_version": version,
        "config": config,
        "config_sha256": digest,
        "source_whitelist": source_whitelist,
    }


def _error_path(location: tuple[Any, ...] | list[Any]) -> str:
    """只保留字段路径，不保存用户或模型提供的字段值。"""
    return ".".join(str(part) for part in location) or "<root>"


def classify_validation_errors(errors: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """把 Pydantic 校验错误分为结构、字段类型和 UUID 三类。"""
    typed = [(str(item.get("type")), item.get("loc") or ()) for item in errors]
    types = sorted({error_type for error_type, _ in typed})
    if any(error_type in _UUID_ERRORS or "chunk_id" in _error_path(location)
           for error_type, location in typed):
        code = CHUNK_ID_INVALID
    elif any(error_type in _FIELD_TYPE_ERRORS for error_type, _ in typed) and not any(
        error_type == "model_type" and _error_path(location) == "<root>"
        for error_type, location in typed
    ):
        code = FIELD_TYPE_INVALID
    else:
        code = SCHEMA_INVALID
    detail = {
        "validation_error_count": len(errors),
        "validation_error_types": types[:8],
        "validation_error_paths": [_error_path(location)[:80] for _, location in typed[:5]],
    }
    return code, detail


def append_attempt(
    diagnostics: dict[str, Any], *, number: int | None, kind: str, started_at: datetime | None,
) -> dict[str, Any]:
    """追加一次处理尝试；未完成的旧尝试标记为 incomplete，不覆盖历史。"""
    attempts = [dict(item) for item in diagnostics.get("attempts") or [] if isinstance(item, dict)]
    for item in attempts:
        if item.get("state") == "processing":
            item["state"] = "incomplete"
            item["incomplete_reason"] = "superseded_by_retry"
    attempts.append({
        "number": number,
        "kind": kind,
        "state": "processing",
        "started_at": iso_utc(started_at),
    })
    diagnostics["attempts"] = attempts[-MAX_RECORDED_ATTEMPTS:]
    return diagnostics


def clear_attempt_artifacts(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """开始新的处理尝试前移除旧尝试的阶段字段；历史由 attempts 保留。"""
    for key in (*ATTEMPT_TIMING_KEYS, *ATTEMPT_ARTIFACT_KEYS):
        diagnostics.pop(key, None)
    return diagnostics


def attempt_timings(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """生成写入单次尝试的 timings；分类/语音复用与未执行分开标注。"""
    timings = {key: diagnostics.get(key)
               for key in ATTEMPT_TIMING_KEYS if diagnostics.get(key) is not None}
    for key in TASK_TIMING_KEYS:
        if diagnostics.get(key) is not None:
            timings[key] = diagnostics[key]
    route_reused = bool(diagnostics.get("route_reused"))
    timings["route_reused"] = route_reused
    route_ms = diagnostics.get("route_ms")
    if route_ms is not None:
        # 复用检查点时不把首次分类耗时伪装成本次阶段耗时，另存历史值。
        timings["historical_route_ms" if route_reused else "route_ms"] = route_ms
    voice = diagnostics.get("voice")
    if isinstance(voice, dict):
        for key in VOICE_TIMING_KEYS:
            if voice.get(key) is not None:
                timings[key] = voice[key]
        if voice.get("state") is not None:
            timings["voice_state"] = voice["state"]
        if voice.get("reused"):
            timings["voice_reused"] = True
    return timings


def finish_open_attempt(
    diagnostics: dict[str, Any], *, outcome: str, error_code: str | None = None,
    duration_ms: int | None = None, finished_at: datetime | None = None,
    started_at: datetime | None = None, timings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """结束最近一次处理尝试；没有进行中的尝试时补一条缺测记录，不伪造起始时间。"""
    attempts = [dict(item) for item in diagnostics.get("attempts") or [] if isinstance(item, dict)]
    index = next((position for position in range(len(attempts) - 1, -1, -1)
                  if attempts[position].get("state") == "processing"), None)
    if index is None:
        attempts.append({
            "number": None,
            "kind": "processing",
            "state": "completed",
            "started_at": iso_utc(started_at),
        })
        index = len(attempts) - 1
    item = attempts[index]
    item["state"] = "failed" if error_code else "completed"
    item["outcome"] = outcome
    if error_code:
        item["error_code"] = error_code
    if duration_ms is not None:
        item["duration_ms"] = duration_ms
    if finished_at is not None:
        item["finished_at"] = iso_utc(finished_at)
    if timings:
        item["timings"] = {key: value for key, value in timings.items() if value is not None}
    diagnostics["attempts"] = attempts[-MAX_RECORDED_ATTEMPTS:]
    return diagnostics


def append_send_attempt(
    diagnostics: dict[str, Any], *, number: int | None, started_at: datetime | None,
    finished_at: datetime | None, duration_ms: int | None, state: str,
    error_code: str | None,
) -> dict[str, Any]:
    """记录一次发送尝试；失败重试不覆盖之前的发送记录。"""
    attempts = [dict(item) for item in diagnostics.get("send_attempts") or [] if isinstance(item, dict)]
    attempts.append({
        "number": number,
        "started_at": iso_utc(started_at),
        "finished_at": iso_utc(finished_at),
        "duration_ms": duration_ms,
        "state": state,
        "error_code": error_code,
    })
    diagnostics["send_attempts"] = attempts[-MAX_RECORDED_SEND_ATTEMPTS:]
    return diagnostics


def record_completion_time(
    diagnostics: dict[str, Any], *, received_at: datetime | None, completed_at: datetime | None,
) -> dict[str, Any]:
    """写入收到至最终完成的总时长；只在最终完成时调用。"""
    value = elapsed_ms(received_at, completed_at)
    if value is not None:
        diagnostics["end_to_end_ms"] = value
    return diagnostics


def perf_ms(started: float) -> int:
    """把 monotonic 起点转换为本次进程内的耗时（毫秒）。"""
    return max(0, round((time.perf_counter() - started) * 1000))
