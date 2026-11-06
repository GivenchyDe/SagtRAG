"""S05 定向测试：严格 JSON、控制字段、摘录定位、事实冲突、局部保留和修复预算。"""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app import answer_validation, observability, rag
from app.rag import validate_answer
from app.retrieval import Candidate


def source(content="报销期限为 30 天。补贴比例为 1.5%。", title="虚构制度"):
    return Candidate(uuid4(), uuid4(), uuid4(), title, content, 0)


def draft(item, *, claim="报销期限为30天。", quote="报销期限为30天", chunk_id=None):
    return {"refused": False, "claims": [{"text": claim, "citations": [
        {"chunk_id": str(chunk_id or item.id), "quote": quote},
    ]}]}


def test_strict_json_rejects_ambiguous_or_non_finite_output():
    assert answer_validation.decode_json_object('{"a": 1}') == {"a": 1}
    assert answer_validation.decode_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert answer_validation.decode_json_object('```\n{"a": 1}\n```') == {"a": 1}
    bad = [
        '{"a": 1}{"b": 2}',
        '{"a": 1, "a": 2}',
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": 1e999}',
        '[]',
        '说明 {"a": 1}',
        '```json\n{"a": 1}\n```\n多余文字',
        '{"a": 1',
    ]
    for text in bad:
        with pytest.raises(answer_validation.OutputDecodeError) as result:
            answer_validation.decode_json_object(text)
        assert result.value.code == "json_invalid"


class _NoStopSSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"{\\"refused\\": true, \\"claims\\": []}"}}]}\n\n'
        yield b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_model_json_distinguishes_incomplete_stream(monkeypatch):
    settings = SimpleNamespace(generation_base_url="https://fixture.invalid/v1",
                               generation_model="fixture-model", embedding_api_key=SecretStr("fixture"))
    monkeypatch.setattr(rag, "get_settings", lambda: settings)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=_NoStopSSE(), headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(rag.http_clients, "get_client", lambda: original(transport=transport))
    with pytest.raises(answer_validation.OutputDecodeError) as result:
        await rag.model_json("fixture system", {"question": "fixture"})
    assert result.value.code == "generation_incomplete"


def test_control_fields_are_strict_and_harmless_metadata_is_limited():
    item = source()
    for refused in ("false", 0, 1, None):
        raw = {**draft(item), "refused": refused}
        answer = validate_answer(raw, [item])
        assert answer.outcome == "invalid_citation"
        assert answer.diagnostic_code == observability.FIELD_TYPE_INVALID

    extra = validate_answer({**draft(item), "extra_note": "无害"}, [item])
    assert extra.outcome == "answered"
    assert extra.detail["ignored_metadata_fields"] == ["extra_note"]

    control = validate_answer({**draft(item), "control_flag": "不允许"}, [item])
    assert control.outcome == "invalid_citation"
    assert control.diagnostic_code == observability.SCHEMA_INVALID

    conflict = {**draft(item), "refused": True}
    assert validate_answer(conflict, [item]).diagnostic_code == observability.INVALID_CONTROL_COMBINATION
    with_reason = {**draft(item), "refusal_reason": "不应出现"}
    assert validate_answer(with_reason, [item]).diagnostic_code == observability.INVALID_CONTROL_COMBINATION

    too_long = draft(item, claim="长" * 151)
    assert validate_answer(too_long, [item]).diagnostic_code == observability.SCHEMA_INVALID


def test_refusal_reason_is_bounded_and_fact_assertions_fall_back():
    item = source()
    safe = validate_answer({"refused": True, "claims": [], "refusal_reason": "缺少差旅补贴标准"}, [item])
    assert safe.text == "当前资料未覆盖：缺少差旅补贴标准"
    unsafe = validate_answer({"refused": True, "claims": [],
                              "refusal_reason": "所有员工都可以报销900元"}, [item])
    assert unsafe.text == rag.NO_ANSWER


def test_quote_matching_tolerates_markdown_table_and_list_markers():
    content = "| 1 日以内（含 1 日） | 直属主管审批 |\n1. 员工在考勤系统提交请假申请"
    assert answer_validation.match_quote("1 日以内（含 1 日）直属主管审批", content) is not None
    assert answer_validation.match_quote("员工在考勤系统提交请假申请", content) is not None


def test_numeric_conflict_allows_number_elsewhere_in_cited_chunk():
    codes = answer_validation.detect_claim_conflicts(
        "报销期限为30天，单笔上限为200元。", ["报销期限为30天。"],
        full_evidence_texts=["报销期限为30天。单笔上限为200元。"])
    assert answer_validation.CONFLICT_NUMERIC not in codes


def test_numeric_conflict_still_rejects_number_absent_from_chunk():
    codes = answer_validation.detect_claim_conflicts(
        "补贴比例为15%。", ["补贴比例为1.5%。"],
        full_evidence_texts=["补贴比例为1.5%。"])
    assert answer_validation.CONFLICT_NUMERIC in codes


def test_quote_formatting_is_recovered_but_numbers_are_not_merged():
    item = source("报销期限为 30 天。补贴比例为 1.5%。")
    repaired = validate_answer(draft(item, quote="报销期限为30天"), [item])
    assert repaired.outcome == "answered"
    assert repaired.sources[0]["quote"] == "报销期限为 30 天"
    assert repaired.detail["formatting_repaired"] == 1

    boundary_item = source("单笔上限为130元。")
    boundary, _ = derive(boundary_item, claim="单笔上限为30元。", quote="30元")
    assert validate_answer(boundary, [boundary_item]).diagnostic_code == observability.QUOTE_MISMATCH

    spaced_item = source("补贴比例为15%。")
    spaced, _ = derive(spaced_item, claim="补贴比例为15%。", quote="1 5%")
    assert validate_answer(spaced, [spaced_item]).diagnostic_code == observability.QUOTE_MISMATCH


def derive(item, *, claim, quote):
    return draft(item, claim=claim, quote=quote), item


def test_source_whitelist_and_joined_sentence_are_rejected():
    item = source("报销期限为30天。补贴比例为1.5%。")
    missing = draft(item, chunk_id=uuid4())
    assert validate_answer(missing, [item]).diagnostic_code == observability.SOURCE_NOT_WHITELISTED

    joined = validate_answer(draft(item, claim="报销期限为30天。",
                                   quote="报销期限为30天。补贴比例为1.5%。"), [item])
    assert joined.outcome == "invalid_citation"
    assert joined.diagnostic_code == observability.QUOTE_OVERBROAD


def test_claim_citations_keep_all_quotes_and_renumber_after_partial_drop():
    item = source("报销期限为30天。补贴比例为1.5%。")
    raw = {"refused": False, "claims": [
        {"text": "报销期限为30天。", "citations": [
            {"chunk_id": str(item.id), "quote": "报销期限为30天"},
            {"chunk_id": str(item.id), "quote": "补贴比例为1.5%"},
        ]},
    ]}
    answer = validate_answer(raw, [item])
    assert answer.outcome == "answered"
    assert len(answer.sources) == 1
    assert len(answer.claim_citations) == 2
    assert {entry["source_number"] for entry in answer.claim_citations} == {1}
    assert "[1]" in answer.text

    second = source("病假超过3天须补充休息建议书。")
    raw_two = {"refused": False, "claims": [
        {"text": "报销期限为30天。", "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
        {"text": "病假超过3天须补充休息建议书。", "citations": [
            {"chunk_id": str(second.id), "quote": "病假超过3天须补充休息建议书"}]},
    ]}
    two = validate_answer(raw_two, [item, second])
    assert two.outcome == "answered"
    assert len(two.sources) == 2
    assert {entry["source_number"] for entry in two.claim_citations} == {1, 2}
    assert "[1]" in two.text and "[2]" in two.text


def test_safe_partial_retention_drops_unrelated_claim_but_not_condition():
    item = source("报销期限为30天。")
    bad = {"refused": False, "claims": [
        {"text": "报销期限为30天。", "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
        {"text": "病假材料为虚构。", "citations": [{"chunk_id": str(item.id), "quote": "不存在的摘录"}]},
    ]}
    assert validate_answer(bad, [item]).outcome == "invalid_citation"
    partial = validate_answer(bad, [item], allow_partial=True)
    assert partial.outcome == "answered"
    assert partial.diagnostic_code == observability.ANSWER_PARTIAL
    assert partial.detail["claims_dropped"] == 1
    assert len(partial.claim_citations) == 1

    conditional = {"refused": False, "claims": [
        {"text": "正式员工报销期限为30天。", "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
        {"text": "例外情形不受限制。", "citations": [{"chunk_id": str(item.id), "quote": "不存在的摘录"}]},
    ]}
    assert validate_answer(conditional, [item], allow_partial=True).outcome == "invalid_citation"


@pytest.mark.parametrize("claim,quote,code", [
    ("正式员工报销期限为90天。", "正式员工报销期限为30天。", answer_validation.CONFLICT_NUMERIC),
    ("单笔上限为900元。", "单笔上限为180元。", answer_validation.CONFLICT_NUMERIC),
    ("补贴比例为15%。", "补贴比例为1.5%。", answer_validation.CONFLICT_NUMERIC),
    ("单笔上限为180美元。", "单笔上限为180元。", answer_validation.CONFLICT_UNIT),
    ("所有外包员工报销期限为30天。", "正式员工报销期限为30天。", answer_validation.CONFLICT_SUBJECT),
    ("合同可以在法务审查前盖章。", "合同不得在法务审查前盖章。", answer_validation.CONFLICT_NEGATION),
    ("所有病假均须补充休息建议书。", "病假超过3天须补充休息建议书。", answer_validation.CONFLICT_CONDITION),
    ("本市因公打车无任何例外，最多180元。", "本市因公打车上限适用180元，紧急处置经值班经理事前批准的除外。",
     answer_validation.CONFLICT_NEGATION),
    ("跨城因公打车上限适用180元。", "本市因公打车上限适用180元，紧急处置经值班经理事前批准的除外。",
     answer_validation.CONFLICT_SCOPE),
])
def test_obvious_fact_conflicts_are_rejected(claim, quote, code):
    content = ("正式员工报销期限为30天。单笔上限为180元。补贴比例为1.5%。"
               "合同不得在法务审查前盖章。病假超过3天须补充休息建议书。"
               "本市因公打车上限适用180元，紧急处置经值班经理事前批准的除外。")
    item = source(content)
    answer = validate_answer(draft(item, claim=claim, quote=quote), [item])
    assert answer.outcome == "invalid_citation"
    conflict_sets = [detail.get("conflict_codes", [])
                     for detail in answer.detail.get("failure_details", [])]
    assert any(code in codes for codes in conflict_sets)


def test_valid_paraphrase_with_full_condition_is_answered():
    item = source("病假超过3天须补充休息建议书，连续休假超过7天需部门负责人审批。")
    answer = validate_answer(draft(item, claim="病假超过3天要补休息建议书，连续超过7天需部门负责人审批。",
                                   quote="病假超过3天须补充休息建议书，连续休假超过7天需部门负责人审批。"), [item])
    assert answer.outcome == "answered"


def test_quote_context_keeps_negation_condition_and_subject_binding():
    negative = source("合同不得在法务审查前盖章。")
    neg_answer = validate_answer(draft(negative, claim="合同可以在法务审查前盖章。",
                                       quote="在法务审查前盖章"), [negative])
    assert neg_answer.outcome != "answered"
    assert neg_answer.diagnostic_code in {
        answer_validation.CONFLICT_NEGATION, observability.CLAIM_SUBJECT_VALUE_CONFLICT}

    conditional = source("病假超过3天须补充休息建议书。")
    cond_answer = validate_answer(draft(conditional, claim="所有病假均须补充休息建议书。",
                                        quote="须补充休息建议书"), [conditional])
    assert cond_answer.outcome != "answered"
    assert cond_answer.diagnostic_code == answer_validation.CONFLICT_CONDITION

    amounts = source("交通费上限180元，住宿费上限300元。")
    swapped = validate_answer(draft(amounts, claim="交通费上限300元，住宿费上限180元。",
                                    quote="交通费上限180元，住宿费上限300元。"), [amounts])
    assert swapped.outcome != "answered"
    assert swapped.diagnostic_code == observability.CLAIM_SUBJECT_VALUE_CONFLICT

    short = source("本市因公打车上限适用180元，紧急处置经值班经理事前批准的除外。")
    short_answer = validate_answer(draft(short, claim="本市因公打车上限适用180元。",
                                         quote="本市因公打车上限适用180元"), [short])
    assert short_answer.outcome == "answered"


def test_valid_no_number_process_question_is_answered():
    item = source("请假流程：先提交申请，再由主管审批。")
    answer = validate_answer(draft(item, claim="请假要先提交申请，再由主管审批。",
                                   quote="请假流程：先提交申请，再由主管审批。"), [item])
    assert answer.outcome == "answered"


def test_equivalent_duration_units_are_not_rejected():
    item = source("员工应在费用发生后30天内提交报销申请。")
    answer = validate_answer(draft(item, claim="员工应在费用发生后30日内提交报销申请。",
                                   quote="员工应在费用发生后30天内提交报销申请。"), [item])
    assert answer.outcome == "answered"


def test_cross_chunk_splice_is_rejected():
    first = source("报销期限为30天。")
    second = source("补贴比例为1.5%。")
    raw = {"refused": False, "claims": [
        {"text": "报销期限为30天，补贴比例为1.5%。", "citations": [
            {"chunk_id": str(first.id), "quote": "报销期限为30天。补贴比例为1.5%"}]},
    ]}
    assert validate_answer(raw, [first, second]).diagnostic_code == observability.QUOTE_MISMATCH


def test_s03_fixed_negative_corpus_is_still_rejected():
    from scripts.s03_baseline.evaluate import engineering, load_fixture

    result = engineering(load_fixture())
    assert result["summary"]["negative_rejected"] == 20
    assert result["summary"]["negative_denominator"] == 20
    assert result["positive_control"] == "answered"
    assert result["format_controls"]["spacing"] == "answered"
    assert result["format_controls"]["extra_field"] == "answered"


def test_twenty_s03_knowledge_points_have_valid_positive_answers():
    from scripts.s03_baseline.evaluate import load_fixture

    data = load_fixture()
    assert len(data["knowledge"]) == 20
    for item in data["knowledge"]:
        candidate = source(item["content"], title=item["title"])
        raw = {"refused": False, "claims": [{
            "text": item["evidence"],
            "citations": [{"chunk_id": str(candidate.id), "quote": item["evidence"]}],
        }]}
        answer = validate_answer(raw, [candidate])
        assert answer.outcome == "answered", (item["id"], answer.diagnostic_code, answer.detail)


def _settings():
    return SimpleNamespace(generation_model="fixture-gen", rerank_model="fixture-rerank",
                           rag_min_relevance=0.25)


def _patch_retrieval(monkeypatch, item):
    async def retrieve(session, principal, question, *, diagnostics=None):
        return [item]

    async def rerank(question, candidates, *, diagnostics=None):
        return candidates

    async def revalidate(session, principal, candidates):
        return True

    monkeypatch.setattr(rag, "get_settings", _settings)
    monkeypatch.setattr(rag, "retrieve", retrieve)
    monkeypatch.setattr(rag, "rerank", rerank)
    monkeypatch.setattr(rag, "revalidate_candidates", revalidate)


@pytest.mark.asyncio
async def test_repair_budget_covers_json_then_validation_failure(monkeypatch):
    item = source("报销期限为30天。")
    _patch_retrieval(monkeypatch, item)
    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        diagnostics["generation_first_content_ms"] = calls["count"]
        if calls["count"] == 1:
            raise answer_validation.OutputDecodeError("json_invalid", {"reason": "syntax"})
        return draft(item)

    monkeypatch.setattr(rag, "model_json", model)
    checkpoints = []

    async def checkpoint(snapshot):
        checkpoints.append(snapshot["generation_calls"])

    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                       diagnostics=diagnostics, checkpoint=checkpoint)
    assert answer.outcome == "answered"
    assert calls["count"] == 2
    assert diagnostics["generation_calls"] == 2
    assert diagnostics["repair_attempted"] is True
    assert diagnostics["repair_succeeded"] is True
    assert len(diagnostics["generation_attempts"]) == 2
    assert checkpoints == [1, 2]


@pytest.mark.asyncio
async def test_repair_budget_stops_after_second_failure_and_tries_safe_partial(monkeypatch):
    item = source("报销期限为30天。")
    _patch_retrieval(monkeypatch, item)
    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return draft(item, quote="不存在的摘录")
        return {"refused": False, "claims": [
            {"text": "报销期限为30天。", "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
            {"text": "无关错误。", "citations": [{"chunk_id": str(item.id), "quote": "不存在"}]},
        ]}

    monkeypatch.setattr(rag, "model_json", model)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                       diagnostics=diagnostics)
    assert calls["count"] == 2
    assert answer.outcome == "answered"
    assert answer.diagnostic_code == observability.ANSWER_PARTIAL
    assert diagnostics["generation_calls"] == 2


@pytest.mark.asyncio
async def test_repair_budget_json_then_citation_failure_stops_at_two(monkeypatch):
    item = source("报销期限为30天。")
    _patch_retrieval(monkeypatch, item)
    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise answer_validation.OutputDecodeError("json_invalid", {"reason": "syntax"})
        return draft(item, quote="不存在的摘录")

    monkeypatch.setattr(rag, "model_json", model)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                       diagnostics=diagnostics)
    assert calls["count"] == 2
    assert answer.outcome == "invalid_citation"
    assert diagnostics["repair_attempted"] is True
    assert diagnostics["initial_error_code"] == "json_invalid"
    assert diagnostics["generation_calls"] == 2


@pytest.mark.asyncio
async def test_repair_budget_citation_then_too_long_is_final(monkeypatch):
    item = source("报销期限为30天。")
    _patch_retrieval(monkeypatch, item)
    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return draft(item, quote="不存在的摘录")
        return {"refused": False, "claims": [
            {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
            {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
            {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
            {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
        ]}

    monkeypatch.setattr(rag, "model_json", model)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                       diagnostics=diagnostics)
    assert calls["count"] == 2
    assert answer.outcome == "answer_too_long"
    assert diagnostics["generation_calls"] == 2
    assert diagnostics["repair_attempted"] is True


@pytest.mark.asyncio
async def test_permission_change_after_generation_is_not_repaired(monkeypatch):
    item = source("报销期限为30天。")
    _patch_retrieval(monkeypatch, item)
    decisions = [True, True, False]

    async def revalidate(session, principal, candidates):
        return decisions.pop(0) if decisions else False

    monkeypatch.setattr(rag, "revalidate_candidates", revalidate)
    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        return draft(item)

    monkeypatch.setattr(rag, "model_json", model)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                       diagnostics=diagnostics)
    assert answer.outcome == "permission_changed"
    assert calls["count"] == 1
    assert diagnostics["generation_calls"] == 1


@pytest.mark.asyncio
async def test_normal_success_uses_one_generation_and_payload_keeps_claims(monkeypatch):
    item = source("报销期限为30天。")
    _patch_retrieval(monkeypatch, item)
    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        return draft(item)

    monkeypatch.setattr(rag, "model_json", model)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                       diagnostics=diagnostics)
    assert calls["count"] == 1
    assert answer.outcome == "answered"
    assert diagnostics["generation_calls"] == 1
    assert "repair_attempted" not in diagnostics
    payload = answer.payload()
    assert payload["claim_citations"][0]["quote"] == "报销期限为30天"
    assert payload["claim_citations"][0]["source_number"] == 1


def test_task_level_budget_survives_attempt_cleanup():
    diagnostics = {
        "generation_calls": 2,
        "repair_attempted": True,
        "generation_ms": 10,
        "generation_first_content_ms": 3,
        "retrieval_ms": 4,
    }
    observability.clear_attempt_artifacts(diagnostics)
    assert diagnostics["generation_calls"] == 2
    assert diagnostics["repair_attempted"] is True
    assert "generation_ms" not in diagnostics
    assert "retrieval_ms" not in diagnostics


def test_repair_feedback_does_not_include_invalid_output():
    first = draft(source("报销期限为30天。"), quote="不存在的摘录")
    feedback = rag._repair_feedback(observability.QUOTE_MISMATCH, first)
    assert "不存在的摘录" not in feedback
    assert "JSON" not in feedback or "schema" in feedback


@pytest.mark.asyncio
async def test_provider_error_is_not_repaired_and_budget_is_not_reset(monkeypatch):
    item = source("报销期限为30天。")
    _patch_retrieval(monkeypatch, item)
    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        raise answer_validation.OutputDecodeError("generation_provider_error")

    monkeypatch.setattr(rag, "model_json", model)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                       diagnostics=diagnostics)
    assert answer.outcome == "service_error"
    assert calls["count"] == 1
    assert diagnostics["generation_calls"] == 1

    calls["count"] = 0
    diagnostics["generation_calls"] = 2
    blocked = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题",
                                        diagnostics=diagnostics)
    assert blocked.outcome == "service_error"
    assert calls["count"] == 0
    assert blocked.diagnostic_code == observability.GENERATION_BUDGET_EXHAUSTED
