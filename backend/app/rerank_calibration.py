"""S12 重排分数阈值校准（纯函数，可用真实/shadow分数离线重复运行）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    selected: int
    relevant_selected: int
    relevant_total: int
    precision: float
    recall: float
    false_positive: int
    empty_rate: float


def evaluate_threshold(samples: list[dict], threshold: float) -> ThresholdMetrics:
    """samples: [{"scores": {id: score}, "relevant": [id]}]；只做离线阈值计算。"""
    selected = relevant_selected = relevant_total = false_positive = 0
    empty = 0
    for sample in samples:
        scores = sample.get("scores") or {}
        relevant = set(sample.get("relevant") or [])
        relevant_total += len(relevant)
        hits = {key for key, score in scores.items() if float(score) >= threshold}
        selected += len(hits)
        relevant_selected += len(hits & relevant)
        false_positive += len(hits - relevant)
        if not hits:
            empty += 1
    precision = relevant_selected / selected if selected else 0.0
    recall = relevant_selected / relevant_total if relevant_total else 0.0
    return ThresholdMetrics(
        threshold=threshold,
        selected=selected,
        relevant_selected=relevant_selected,
        relevant_total=relevant_total,
        precision=round(precision, 4),
        recall=round(recall, 4),
        false_positive=false_positive,
        empty_rate=round(empty / len(samples), 4) if samples else 0.0,
    )


def choose_threshold(
    samples: list[dict], thresholds: list[float], *, min_recall: float = 0.8,
) -> ThresholdMetrics:
    """优先满足最低召回，再取精确率/空结果率更稳健的阈值。"""
    candidates = [evaluate_threshold(samples, value) for value in thresholds]
    viable = [item for item in candidates if item.recall >= min_recall]
    pool = viable or candidates
    return max(pool, key=lambda item: (item.precision, item.recall, -item.empty_rate))
