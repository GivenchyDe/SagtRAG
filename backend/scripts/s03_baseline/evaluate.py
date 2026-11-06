"""冻结数据、记录当前行为；工程预检与真实模型质量明确分开。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).with_name("cases.json")
LOCK = Path(__file__).with_name("freeze.json")
CODE_PATHS = ["rag.py", "retrieval.py", "intent_router.py", "chitchat.py", "ingestion.py",
              "embeddings.py", "rag_worker.py", "permissions.py", "models.py", "config.py"]


def stable_id(value):
    return uuid5(NAMESPACE_URL, "sagtrag:s03-v1:" + value)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_fixture(path=FIXTURE):
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [item["id"] for item in data["knowledge"]]
    if len(ids) != 20 or len(set(ids)) != 20:
        raise ValueError("需要20个不同知识点")
    for item in data["knowledge"]:
        if item["split"] not in {"tune", "test"} or len(set(item["questions"])) != 5:
            raise ValueError("问法组或分组不合法")
        if item["evidence"] not in item["content"]:
            raise ValueError("标注引文不在原文中")
        if any(not any(term in item["content"] for term in group) for group in item["facts"]):
            raise ValueError("关键事实不在原文中")
    if len(data["boundaries"]) < 15 or len(data["missing_questions"]) < 20 or len(data["conversations"]) < 10:
        raise ValueError("子集数量不足")
    for group in data["conversations"]:
        if any(target is not None and target not in ids for _, target in group["turns"]):
            raise ValueError("多轮目标知识点不存在")
    return data


def manifest(data):
    return {
        "dataset_version": data["version"],
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "knowledge_points": len(data["knowledge"]), "paraphrases": 100,
        "split_groups": dict(Counter(item["split"] for item in data["knowledge"])),
        "repeat_groups": [item["id"] for split in ("tune", "test")
                          for item in [row for row in data["knowledge"] if row["split"] == split][:5]],
        "repeat_runs_per_group": 5, "repeated_requests": 50,
        "boundary_cases": len(data["boundaries"]) * 2,
        "missing_cases": len(data["missing_questions"]), "citation_negative_cases": 20,
        "conversations": len(data["conversations"]),
        "conversation_turns": sum(len(row["turns"]) for row in data["conversations"]),
        "synthetic_only": True,
    }


def check_freeze(data):
    frozen = json.loads(LOCK.read_text(encoding="utf-8"))
    if frozen["dataset"] != manifest(data):
        raise ValueError("数据与冻结清单不一致；新增版本后再运行，禁止静默改变分母")
    return frozen


def fingerprint():
    result = {}
    for name in CODE_PATHS:
        path = ROOT / "backend" / "app" / name
        result["backend/app/" + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
    # 只记录文件状态，不读取任何环境文件或 diff 正文。
    status = subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True, check=True)
    return {"head": head.stdout.strip(), "working_tree_status": status.stdout.splitlines(), "source_sha256": result}


def percentile(values, quantile):
    if not values:
        return None
    values = sorted(values)
    rank = (len(values) - 1) * quantile
    low, high = math.floor(rank), math.ceil(rank)
    return round(values[low] + (values[high] - values[low]) * (rank - low), 3)


def fact_precheck(text, item):
    """仅为字符串必要条件筛查，不能冒充人工事实正确率。"""
    return all(any(term in text for term in group) for group in item["facts"])


def citation_cases():
    """真实摘录配错误结论，以及来源/格式错误，均要求拒绝。"""
    content = ("正式员工报销期限为30天。单笔上限为180元。补贴比例为1.5%。"
               "合同不得在法务审查前盖章。病假超过3天须补充休息建议书。"
               "本市因公打车上限适用180元，紧急处置经值班经理事前批准的除外。")
    rows = [
        ("wrong_days", "正式员工报销期限为90天。", "正式员工报销期限为30天。"),
        ("wrong_money", "单笔上限为900元。", "单笔上限为180元。"),
        ("wrong_ratio", "补贴比例为15%。", "补贴比例为1.5%。"),
        ("wrong_unit", "单笔上限为180美元。", "单笔上限为180元。"),
        ("wrong_subject", "所有外包员工报销期限为30天。", "正式员工报销期限为30天。"),
        ("negation", "合同可以在法务审查前盖章。", "合同不得在法务审查前盖章。"),
        ("condition", "所有病假均须补充休息建议书。", "病假超过3天须补充休息建议书。"),
        ("exception", "本市因公打车无任何例外，最多180元。", "本市因公打车上限适用180元，紧急处置经值班经理事前批准的除外。"),
        ("scope", "跨城因公打车上限适用180元。", "本市因公打车上限适用180元，紧急处置经值班经理事前批准的除外。"),
        ("mixed_fact", "正式员工报销期限为30天，补贴比例为99%。", "正式员工报销期限为30天。"),
        ("fabricated_quote", "单笔上限为180元。", "单笔上限为900元。"),
        ("decimal_quote", "补贴比例为15%。", "补贴比例为15%。"),
        ("negated_quote", "合同可以在法务审查前盖章。", "合同可以在法务审查前盖章。"),
        ("joined_quote", "正式员工报销期限为30天。", "正式员工报销期限为30天。补贴比例为1.5%。"),
        ("foreign_uuid", "单笔上限为180元。", "单笔上限为180元。"),
        ("invalid_uuid", "单笔上限为180元。", "单笔上限为180元。"),
        ("missing_citation", "单笔上限为180元。", "单笔上限为180元。"),
        ("wrong_schema", "单笔上限为180元。", "单笔上限为180元。"),
        ("empty_quote", "单笔上限为180元。", ""),
        ("too_long_quote", "单笔上限为180元。", "180" * 201),
    ]
    return content, rows


def engineering(data):
    from app.ingestion import extract_paragraphs, split_into_chunks
    from app.intent_router import classify_by_rules, normalize
    from app.rag import validate_answer
    from app.retrieval import Candidate

    rules = []
    for pair, q1, e1, q2, e2 in data["boundaries"]:
        for number, question, expected in ((1, q1, e1), (2, q2, e2)):
            actual = classify_by_rules(normalize(question))
            rules.append({"id": f"{pair}-{number}", "question": question, "expected": expected,
                          "actual": actual.intent if actual else None, "rule": actual.rule_id if actual else None,
                          "status": "needs_classifier" if actual is None else "pass" if actual.intent == expected else "fail"})
    content, rows = citation_cases()
    candidate = Candidate(stable_id("negative-chunk"), stable_id("negative-doc"), stable_id("negative-index"),
                          "虚构证据负例", content, 0)
    citations = []
    for key, claim, quote in rows:
        raw = {"refused": False, "claims": [{"text": claim, "citations": [
            {"chunk_id": str(candidate.id), "quote": quote}]}], "refusal_reason": None}
        if key == "foreign_uuid":
            raw["claims"][0]["citations"][0]["chunk_id"] = str(stable_id("foreign-chunk"))
        if key == "invalid_uuid":
            raw["claims"][0]["citations"][0]["chunk_id"] = "not-a-uuid"
        if key == "missing_citation":
            raw["claims"][0]["citations"] = []
        if key == "wrong_schema":
            raw["claims"] = "not-an-array"
        answer = validate_answer(raw, [candidate])
        citations.append({"id": key, "draft": raw, "expected": "reject", "actual": answer.outcome,
                          "passed": answer.outcome != "answered"})
    valid = {"refused": False, "claims": [{"text": "正式员工报销期限为30天。", "citations": [
        {"chunk_id": str(candidate.id), "quote": "正式员工报销期限为30天。"}]}]}
    positive = validate_answer(valid, [candidate]).outcome
    spacing = json.loads(json.dumps(valid))
    spacing["claims"][0]["citations"][0]["quote"] = "正式员工报销期限为 30 天。"
    extra = {**valid, "extra_note": "无害额外字段"}
    parsed = []
    for item in data["knowledge"]:
        paragraphs = extract_paragraphs(item["id"] + ".md", item["content"].encode())
        chunks = split_into_chunks(paragraphs, revision_id=str(stable_id(item["id"] + ":revision")))
        parsed.append({"knowledge_id": item["id"], "split": item["split"], "chunks": chunks})
    return {"scope": "deterministic_engineering_only_no_models_no_database_no_delivery",
            "rules": rules, "citation_negatives": citations,
            "positive_control": positive,
            "format_controls": {"spacing": validate_answer(spacing, [candidate]).outcome,
                                "extra_field": validate_answer(extra, [candidate]).outcome},
            "parsed_corpus": parsed,
            "summary": {"rules": dict(Counter(row["status"] for row in rules)),
                        "negative_rejected": sum(row["passed"] for row in citations),
                        "negative_denominator": len(citations)}}


def requests(data, frozen):
    """相同输入的5次请求均单独执行；不复用输出冒充稳定性。"""
    result = []
    for item in data["knowledge"]:
        for index, question in enumerate(item["questions"], 1):
            result.append({"id": f"qa:{item['id']}:{index}", "suite": "paraphrases",
                           "knowledge_id": item["id"], "split": item["split"], "question": question})
    for item in data["knowledge"]:
        if item["id"] in frozen["dataset"]["repeat_groups"]:
            for index in range(1, 6):
                result.append({"id": f"repeat:{item['id']}:{index}", "suite": "stability",
                               "knowledge_id": item["id"], "split": item["split"], "question": item["questions"][0]})
    for index, question in enumerate(data["missing_questions"], 1):
        result.append({"id": f"missing:{index}", "suite": "missing", "question": question})
    for conversation in data["conversations"]:
        for index, (question, target) in enumerate(conversation["turns"], 1):
            result.append({"id": f"conversation:{conversation['id']}:{index}", "suite": "conversations",
                           "conversation_id": conversation["id"], "turn": index, "question": question,
                           "knowledge_id": target, "history": conversation["turns"][:index-1]})
    return result


def live_summary(rows, data):
    """分母固定，包括失败与拒答；未执行与未人工复核独立显示。"""
    result = {}
    for suite, denominator in (("paraphrases", 100), ("stability", 50),
                               ("missing", len(data["missing_questions"])),
                               ("conversations", sum(len(row["turns"]) for row in data["conversations"]))):
        samples = [row for row in rows if row["suite"] == suite]
        outcomes = Counter(row["outcome"] for row in samples)
        answered = [row for row in samples if row["outcome"] == "answered"]
        prechecks = sum(row.get("fact_precheck") is True and row.get("expected_evidence_in_context") is True
                        for row in samples if row["outcome"] == "answered")
        latency = [row["harness_ms"] for row in samples]
        result[suite] = {"planned": denominator, "executed": len(samples), "outcomes": dict(outcomes),
                         "fact_evidence_precheck_passed": prechecks, "answered": len(answered),
                         "human_verified_correct": None, "quality_gate_passed": None,
                         "harness_p50_ms": percentile(latency, .5), "harness_p95_ms": percentile(latency, .95),
                         "note": "harness耗时不含企微、真实排队和ASR；关键词预检不是语义正确率"}
    result["stage_latency"] = {}
    for key in ("route_ms", "retrieval_ms", "rerank_ms", "generation_ms", "rag_total_ms"):
        values = [row["diagnostics"][key] for row in rows if key in row["diagnostics"]]
        result["stage_latency"][key] = {"n": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)}
    result["paraphrase_group_precheck"] = {
        item["id"]: {"executed": len(group), "all_five_precheck_passed": len(group) == 5 and all(
            row["outcome"] == "answered" and row.get("fact_precheck") is True and
            row.get("expected_evidence_in_context") is True for row in group)}
        for item in data["knowledge"]
        for group in [[row for row in rows if row["suite"] == "paraphrases" and row["knowledge_id"] == item["id"]]]}
    return result


def main(args):
    data = load_fixture()
    frozen = check_freeze(data)
    report = {"phase": "S03", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "dataset": manifest(data), "frozen_protocol": frozen,
              "implementation": fingerprint(), "engineering": engineering(data)}
    if args.live:
        from scripts.s03_baseline.live import run_live
        asyncio.run(run_live(args, data, frozen, report))
    report["s03_exit"] = {
        "dataset_frozen": True, "engineering_recorded": True,
        "real_model_baseline_complete": bool(report.get("live", {}).get("complete")),
        "production_validated": False,
        "next_step": "S04仅在S03真实基线完整后推进；质量门禁不要求旧版本先通过"}
    write_json(args.output, report)
    print(json.dumps({"report": str(args.output.resolve()), "engineering": report["engineering"]["summary"],
                      "s03_exit": report["s03_exit"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "TEST/s03-baseline-20261105/baseline.json")
    parser.add_argument("--live", action="store_true", help="仅虚构语料：本机临时schema及真实模型；可能产生费用")
    parser.add_argument("--limit", type=int, default=220, help="本次最多执行的问答请求数，完整集为191条")
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument("--resume", action="store_true", help="仅在数据、代码和配置指纹一致时复用已完成记录")
    parser.add_argument("--routing", choices=("local", "on"), default="on", help="on仅调整本次进程，用于线上已启用路由的对照配置")
    main(parser.parse_args())
