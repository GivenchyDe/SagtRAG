"""Gate 1 通过后，使用锁定的 RAGAS 与 Model Studio 对虚构样本做语义评测。"""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path

# 离线评测不向框架遥测端点发送项目事件。
os.environ["RAGAS_DO_NOT_TRACK"] = "true"
os.environ["LANGSMITH_TRACING"] = "false"

from langchain_core.outputs import Generation, LLMResult
from ragas.dataset_schema import SingleTurnSample
from ragas.llms.base import BaseRagasLLM
from ragas.metrics import Faithfulness, LLMContextRecall
from ragas.run_config import RunConfig

from app.config import get_settings
from app.rag import model_json


class ModelStudioJudge(BaseRagasLLM):
    """复用业务的流式 JSON 适配器，兼容仅支持思考模式的已选模型。"""

    def generate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
        return asyncio.run(self.agenerate_text(prompt,n,temperature,stop,callbacks))

    async def agenerate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
        outputs = await asyncio.gather(*[
            model_json("你是评测员。按 evaluation_prompt 的评分任务与 JSON schema 输出 JSON；只评估给出的资料与回答。",
                       {"evaluation_prompt":prompt.to_string()}) for _ in range(n)
        ])
        return LLMResult(generations=[[Generation(text=json.dumps(item,ensure_ascii=False),
                                                 generation_info={"finish_reason":"stop"}) for item in outputs]])

    def is_finished(self, response):
        return all(item.generation_info.get("finish_reason")=="stop" for group in response.generations for item in group)


async def evaluate(cases):
    """先区分人工构造的正反例，再评分正常答案；拒答由确定性门禁检查。"""
    judge = ModelStudioJudge(run_config=RunConfig(timeout=240,max_retries=1))
    faithfulness = Faithfulness(llm=judge)
    recall = LLMContextRecall(llm=judge)
    calibration_context = ["虚构北辰公司的年假为12天。"]
    good = SingleTurnSample(user_input="虚构北辰公司的年假是多少天？",response="年假为12天。",retrieved_contexts=calibration_context)
    bad = SingleTurnSample(user_input="虚构北辰公司的年假是多少天？",response="年假为99天。",retrieved_contexts=calibration_context)
    good_score,bad_score = await asyncio.gather(faithfulness.single_turn_ascore(good),faithfulness.single_turn_ascore(bad))
    calibrated = math.isfinite(good_score) and math.isfinite(bad_score) and good_score>=0.9 and bad_score<=0.25
    if not calibrated:
        raise RuntimeError("RAGAS judge 未能区分人工正反例，停止 Gate 2")
    print("PASS RAGAS calibration positive/negative",flush=True)
    semaphore = asyncio.Semaphore(2)

    async def score(case):
        async with semaphore:
            response=case["response"].split("\n\n资料来源：",1)[0]
            sample=SingleTurnSample(user_input=case["question"],response=response,
                reference=case["reference"],retrieved_contexts=case["contexts"])
            faithful,context_recall=await asyncio.gather(faithfulness.single_turn_ascore(sample),recall.single_turn_ascore(sample))
            if not math.isfinite(faithful) or not math.isfinite(context_recall):
                raise RuntimeError("RAGAS 返回非有限分数，不能计为通过")
            print(f"RAGAS {case['id']} faithfulness={faithful:.3f} context_recall={context_recall:.3f}",flush=True)
            return {"id":case["id"],"faithfulness":faithful,"context_recall":context_recall}

    normal=[case for case in cases if case["expected"]=="answered"]
    if not normal:
        raise RuntimeError("Gate 2 缺少正常回答样本")
    scores=await asyncio.gather(*(score(case) for case in normal))
    mean={metric:sum(row[metric] for row in scores)/len(scores) for metric in ("faithfulness","context_recall")}
    thresholds={"faithfulness":0.9,"context_recall":0.8}
    return {"provider":"Model Studio","judge_model":get_settings().generation_model,"ragas_version":"0.4.3",
        "calibration":{"positive":good_score,"negative":bad_score},"scores":scores,"mean":mean,
        "thresholds":thresholds,"passed":all(mean[key]>=value for key,value in thresholds.items()),
        "scope":"少量虚构样本的 Demo 基线；不代表全量回归或生产质量认证"}


async def main(path):
    report=json.loads(path.read_text(encoding="utf-8"))
    if report.get("gate1_passed") is not True:
        raise SystemExit("请先运行 Phase 3 Gate 1 定向联调")
    report["gate2"]=await evaluate(report["cases"])
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if not report["gate2"]["passed"]:
        raise SystemExit("Gate 2 未达 Demo 基线，结果已保存")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("report",type=Path,nargs="?",default=Path("../TEST/phase3/acceptance.json"))
    asyncio.run(main(parser.parse_args().report))
