"""离线重排阈值校准：读取 shadow/人工整理的分数样本，不调用真实模型。"""

import argparse
import json
from pathlib import Path

from app.rerank_calibration import evaluate_threshold, choose_threshold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", type=Path, help="JSON: {samples:[{scores:{id:score},relevant:[id]}]}")
    parser.add_argument("--thresholds", default="0.05,0.10,0.15,0.20,0.25,0.30,0.35,0.40")
    parser.add_argument("--min-recall", type=float, default=0.8)
    args = parser.parse_args()
    payload = json.loads(args.samples.read_text(encoding="utf-8"))
    samples = payload.get("samples", payload)
    thresholds = [float(value) for value in args.thresholds.split(",") if value.strip()]
    report = {
        "chosen": choose_threshold(samples, thresholds, min_recall=args.min_recall).__dict__,
        "all": [evaluate_threshold(samples, value).__dict__ for value in thresholds],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
