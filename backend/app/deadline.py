"""S09 处理期限：一个处理尝试共享同一预算，排队时间不计入。"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar


class DeadlineExceeded(TimeoutError):
    """处理期限耗尽；调用方应停止继续发起外部请求。"""


class Deadline:
    """基于单调时钟的只读预算；每次外部调用取剩余值与阶段上限的较小者。"""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic):
        self.total_seconds = max(0.001, float(seconds))
        self._clock = clock
        self.started = clock()

    def remaining(self) -> float:
        return max(0.0, self.total_seconds - (self._clock() - self.started))

    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self.started)

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self, stage: str = "processing") -> None:
        if self.remaining() <= 0.01:
            raise DeadlineExceeded(f"deadline exceeded before {stage}")

    def timeout(self, cap: float | None = None) -> float:
        """返回本次调用可用的秒数；已耗尽时抛 DeadlineExceeded，避免 0 超时重试。"""
        remaining = self.remaining()
        if remaining <= 0.01:
            raise DeadlineExceeded("deadline exceeded")
        return max(0.01, min(remaining, cap) if cap is not None else remaining)


_CURRENT_DEADLINE: ContextVar[Deadline | None] = ContextVar("processing_deadline", default=None)


def set_current_deadline(deadline: Deadline):
    return _CURRENT_DEADLINE.set(deadline)


def reset_current_deadline(token) -> None:
    _CURRENT_DEADLINE.reset(token)


def current_deadline() -> Deadline | None:
    return _CURRENT_DEADLINE.get()


def call_timeout(cap: float) -> float:
    """外部调用超时：有处理期限时取剩余值与阶段上限的较小者。"""
    deadline = current_deadline()
    if deadline is None:
        return float(cap)
    return deadline.timeout(cap)


def check_current(stage: str) -> None:
    deadline = current_deadline()
    if deadline is not None:
        deadline.check(stage)
