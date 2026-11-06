"""只读取本地S03结果生成可阅读报告，不调用模型、不更改评分或冻结数据。"""

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.s03_baseline.evaluate import ROOT, stable_id


def render(report):
    live = report.get("live", {})
    rows = live.get("rows", [])
    engineering = report["engineering"]
    regression = report.get("engineering_regression", {})
    counts = Counter(row["outcome"] for row in rows)
    lines = ["# S03 固定评测基线结果", "",
             "本报告仅使用虚构资料。本地工程回归、真实模型行为与生产验收分别记录。", "",
             "| Phase | 工作 | 状态 |", "|---|---|---|",
             "| S00 | 线上现状核查 | 已有2026-11-04历史快照，本轮未复查 |",
             "| S01 / S02 | 诊断与能力规则修复 | 已有本地验证，本轮未发布 |",
             f"| S03 | 固定数据及当前版本基线 | {'真实运行完整' if live.get('complete') else '真实运行未完整'}，不代表质量门禁通过 |",
             "| S04及后续 | 优化与验收 | 本轮未实施 |", "",
             "## 数据与实现", "",
             f"- 数据版本：`{report['dataset']['dataset_version']}`；SHA-256：`{report['dataset']['fixture_sha256']}`。",
             f"- 业务Git HEAD：`{report['implementation']['head']}`；另存10个业务模块SHA-256，包含当前未提交改动。",
             "- 20个知识点、100种问法、50次重复、32条意图、20条无依据、20条证据负例、10组21轮对话。",
             "- 按知识点分12组调参/8组测试；本轮无调参。测试集不作为后续提示词示例。", "",
             "## 无模型工程记录", "",
             f"- 意图规则：{json.dumps(engineering['summary']['rules'], ensure_ascii=False)}。未命中规则不直接算分类失败。",
             f"- 错误证据拒绝：{engineering['summary']['negative_rejected']}/20；真实摘录配错误结论仍被放行属于已知缺陷。",
             f"- 正常证据对照：`{engineering['positive_control']}`；排版空格差异：`{engineering['format_controls']['spacing']}`。", ""]
    if regression:
        lines += [f"- 定向工程回归两批分别{regression['passed']}项、{regression['supplementary_run']['passed']}项通过，共{regression['distinct_passed_total']}项；全部为本机/替身，不计入真实模型语义质量。", ""]
    if not live:
        return "\n".join(lines + ["尚无真实模型结果。", ""])
    settings = live["profile"]["settings"]
    lines += ["## 真实模型运行", "",
              f"- 生成：`{settings['generation_model']}`；向量：`{settings['embedding_model']}`；重排：`{settings['rerank_model']}`。",
              f"- 召回/上下文：{settings['rag_recall_limit']}/{settings['rag_context_limit']}；重排阈值：{settings['rag_min_relevance']}；完整白名单配置与端点指纹见原始JSON。",
              f"- 并发：{live['profile']['concurrency']}；本机原路由开关：{live['profile']['local_routing_enabled']}；本进程：{live['profile']['settings']['intent_routing_enabled']}。",
              f"- 问答执行：{len(rows)}/191；意图边界：{len(live['boundaries'])}/32；应用ACL：{sum(r['passed'] for r in live['acl'])}/{len(live['acl'])}。",
              f"- 结果分布：`{json.dumps(dict(counts), ensure_ascii=False)}`。",
              f"- 临时schema清理：{live.get('schema_cleaned', False)}；仅验证应用谓词，未验证RLS策略。", "",
              "| 子集 | 已执行/固定分母 | answered | 关键事实与上下文预检通过 | harness P50/P95（秒） |",
              "|---|---:|---:|---:|---:|"]
    for key, summary in live.get("summary", {}).items():
        if key not in {"paraphrases", "stability", "missing", "conversations"}:
            continue
        p50, p95 = summary["harness_p50_ms"], summary["harness_p95_ms"]
        timing = f"{p50/1000:.2f}/{p95/1000:.2f}" if p50 is not None else "未测"
        lines.append(f"| {key} | {summary['executed']}/{summary['planned']} | {summary['answered']} | {summary['fact_evidence_precheck_passed']} | {timing} |")
    lines += ["", "预检是必要条件筛查，不是人工正确率。多轮预检引用整个主题的关键事实，不能替代逐轮所问内容的语义验收。answered不自动算答对。", "",
              "## 阶段诊断", "",
              "| 阶段 | 实际样本数 | P50（毫秒） | P95（毫秒） |", "|---|---:|---:|---:|"]
    for key, summary in live.get("summary", {}).get("stage_latency", {}).items():
        lines.append(f"| {key} | {summary['n']} | {summary['p50']} | {summary['p95']} |")
    qa = [row for row in rows if row["suite"] == "paraphrases"]
    lines += ["", "## 知识点级观察", "",
              "| 知识点 | 已执行 | answered | 事实+上下文预检 | Dense/FTS/RRF/上下文含目标块 |",
              "|---|---:|---:|---:|---|"]
    for key in live.get("summary", {}).get("paraphrase_group_precheck", {}):
        group = [row for row in qa if row["knowledge_id"] == key]
        target = str(stable_id(key + ":chunk:0"))
        stage = [sum(target in row["diagnostics"].get(name, []) for row in group)
                 for name in ("dense_order", "fts_order")]
        stage.append(sum(target in {x["chunk_id"] for x in row["diagnostics"].get("candidates", [])} for row in group))
        stage.append(sum(target in row["diagnostics"].get("selected_chunk_ids", []) for row in group))
        pre = sum(row.get("fact_precheck") is True and row.get("expected_evidence_in_context") is True and row["outcome"] == "answered" for row in group)
        lines.append(f"| {key} | {len(group)}/5 | {sum(row['outcome']=='answered' for row in group)} | {pre} | {' / '.join(map(str, stage))} |")
    lines += ["", "20个短文档的Dense@30易饱和；上述块覆盖不能证明多片段完整依据或大规模效果。", "",
              "## 已定位但本轮未修复", ""]
    for row in live["boundaries"]:
        if not row["passed"]:
            lines.append(f"- 意图 `{row['id']}`：期望 `{row['expected']}`，实际 `{row['actual']}`。")
    lines += ["- 证据校验仍可接受真实摘录配错误结论；由S05承接。",
              "- 排版空格差异和无害额外字段仍可能整份拒答；由S04/S05细分、修复。",
              "- 当前实现不保存多轮状态；各组原始问题已按顺序运行，不注入正确改写。由S08承接。", "",
              "## 调用、费用与适用边界", "",
              f"- 本完整运行累计HTTP请求：{live['budget']['http_requests']}；按模型：`{json.dumps(live['budget']['by_model'], ensure_ascii=False)}`。",
              f"- 供应商usage记录数：{len(live['budget']['provider_usage_records'])}；实际人民币费用未取得，不编造估价。预检另计。",
              "- 资料与回答均为虚构；未读新增生产数据、未部署、未向企微发送消息。",
              "- 本机harness时延不包含生产排队、ASR、企微接收或发送，不作为端到端SLA。",
              "- 人工语义复核、手机排版验收和生产受控时延仍待对应阶段完成。S03记录旧版本基线，不以旧版本通过最终门禁为前提。", ""]
    usage_totals = {}
    for record in live["budget"]["provider_usage_records"]:
        totals = usage_totals.setdefault(record["model"], {"records":0, "fields":{}})
        totals["records"] += 1
        for key, value in record["usage"].items():
            if type(value) in (int, float):
                field = totals["fields"].setdefault(key, {"observed_records":0,"sum":0})
                field["observed_records"] += 1
                field["sum"] += value
    lines += ["## 供应商返回用量汇总", "", "只汇总实际返回的数值字段；缺测不填0，不将不同口径字段相加。", "",
              "```json", json.dumps(usage_totals, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "MD/RAG_S03_固定评测基线_20261105.md")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    verification = args.report.parent / "verification.json"
    if verification.exists():
        report["engineering_regression"] = json.loads(verification.read_text(encoding="utf-8"))
    args.output.write_text(render(report), encoding="utf-8")
    print(str(args.output.resolve()))
