"""Phase 3 必要的引用白名单、证据与融合行为检查。"""

from uuid import uuid4

from app.rag import NO_ANSWER, validate_answer
from app.retrieval import Candidate, reciprocal_rank_fusion


def source(content="虚构星河公司的报销期限为 30 天。"):
    return Candidate(uuid4(), uuid4(), uuid4(), "虚构报销制度", content, 0)


def draft(item, *, quote="报销期限为 30 天", chunk_id=None):
    return {"refused": False, "claims": [{"text": "报销期限为 30 天。", "citations": [
        {"chunk_id": str(chunk_id or item.id), "quote": quote},
    ]}]}


def test_citation_must_be_retrieved_and_have_exact_evidence():
    item = source()
    valid = validate_answer(draft(item), [item])
    assert valid.outcome == "answered"
    assert valid.sources[0]["chunk_id"] == str(item.id)
    assert "[1]" in valid.text and "资料来源" in valid.text
    assert validate_answer(draft(item, chunk_id=uuid4()), [item]).text == NO_ANSWER
    assert validate_answer(draft(item, quote="报销期限为 90 天"), [item]).text == NO_ANSWER
    assert validate_answer(draft(item, quote="   "), [item]).text == NO_ANSWER


def test_refusal_and_partial_invalid_answer_do_not_expose_claims():
    item = source()
    assert validate_answer({"refused": True, "claims": []}, [item]).outcome == "no_answer"
    raw = draft(item)
    raw["claims"].append({"text": "伪造结论", "citations": [{"chunk_id": str(uuid4()), "quote": "不在资料中"}]})
    result = validate_answer(raw, [item])
    assert result.text == NO_ANSWER and result.sources == []


def test_rrf_deduplicates_and_only_returns_authorized_inputs():
    first, second, third = source("甲"), source("乙"), source("丙")
    fused = reciprocal_rank_fusion([[first, second], [second, third]], 3)
    assert fused[0].id == second.id
    assert {item.id for item in fused} == {first.id, second.id, third.id}
    assert len(reciprocal_rank_fusion([[first], [first]], 5)) == 1
