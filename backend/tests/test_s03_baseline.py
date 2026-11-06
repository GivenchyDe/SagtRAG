"""评测口径回归：拒答、缺测和字面命中不能冒充语义质量。"""

import json

import pytest

from scripts.s03_baseline.evaluate import (
    check_freeze, fact_precheck, live_summary, load_fixture, requests,
)


def test_frozen_corpus_has_disjoint_knowledge_groups_and_independent_repeats():
    data = load_fixture()
    frozen = check_freeze(data)
    rows = requests(data, frozen)
    assert len(rows) == 191
    assert len({row["id"] for row in rows}) == len(rows)
    for item in data["knowledge"]:
        group = [row for row in rows if row.get("knowledge_id") == item["id"] and "split" in row]
        assert {row["split"] for row in group} == {item["split"]}
    repeated = [row for row in rows if row["suite"] == "stability"]
    assert len(repeated) == 50
    for key in frozen["dataset"]["repeat_groups"]:
        assert len({row["question"] for row in repeated if row["knowledge_id"] == key}) == 1


def test_changed_fixture_cannot_silently_reuse_frozen_denominator():
    data = json.loads(json.dumps(load_fixture()))
    data["knowledge"][0]["split"] = "test"
    with pytest.raises(ValueError, match="冻结"):
        check_freeze(data)


@pytest.mark.parametrize("outcome", ["no_answer", "invalid_citation", "service_error", "harness_timeout"])
def test_refusals_and_failures_stay_in_fixed_denominator(outcome):
    data = load_fixture()
    row = {"suite":"paraphrases", "knowledge_id":data["knowledge"][0]["id"],
           "outcome":outcome,"fact_precheck":True,"expected_evidence_in_context":True,
           "harness_ms":100,"diagnostics":{}}
    summary = live_summary([row], data)
    assert summary["paraphrases"]["planned"] == 100
    assert summary["paraphrases"]["fact_evidence_precheck_passed"] == 0
    assert summary["paraphrases"]["human_verified_correct"] is None
    assert summary["stage_latency"]["generation_ms"]["n"] == 0
    assert summary["stage_latency"]["generation_ms"]["p95"] is None
    assert not summary["paraphrase_group_precheck"][row["knowledge_id"]]["all_five_precheck_passed"]


def test_word_matching_is_only_a_precheck_not_a_quality_score():
    data = load_fixture()
    item = data["knowledge"][0]
    text = "费用发生后30天和90天都可以提交。"
    assert fact_precheck(text, item) is True  # 冲突答案也能命中字面条件，必须人工复核。
    row = {"suite":"paraphrases","knowledge_id":item["id"],"outcome":"answered",
           "fact_precheck":True,"expected_evidence_in_context":True,"harness_ms":100,"diagnostics":{}}
    summary = live_summary([row], data)["paraphrases"]
    assert summary["quality_gate_passed"] is None
    assert summary["human_verified_correct"] is None
