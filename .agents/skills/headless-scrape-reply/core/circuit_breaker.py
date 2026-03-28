"""
circuit_breaker.py — 令牌桶 + 熔断器 + 合规限速
Author: RussellCooper

设计目标：
  1. 熔断器（Circuit Breaker）：防止雪崩，连续失败 N 次后自动断路
  2. 令牌桶（Token Bucket）：精确控制 QPS，对目标系统友好，合规
  3. 人工兜底钩子：熔断触发时回调通知，支持钉钉/企微/邮件

状态机（三态）：
  CLOSED ──失败≥threshold──▶ OPEN ──等待 recovery_s──▶ HALF_OPEN
    ▲                                                        │
    └────────────────────────成功──────────────────────────┘

复杂度：
  acquire()  O(1)  — 令牌桶纯数学计算
  record()   O(1)  — 滑动窗口用 deque，O(1) 摊销
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 熔断器状态
# ---------------------------------------------------------------------------

class BreakerState(Enum):
    CLOSED    = auto()   # 正常通行
    OPEN      = auto()   # 熔断，拒绝所有请求
    HALF_OPEN = auto()   # 探测恢复，放行一个请求


# ---------------------------------------------------------------------------
# 令牌桶（Token Bucket）
# ---------------------------------------------------------------------------

class TokenBucket:
    """
    令牌桶限速器。

    数学模型：
        令 t = 当前时间，t0 = 上次补充时间
        新增令牌 = (t - t0) * rate
        当前令牌 = min(capacity, tokens + 新增令牌)

    acquire() 是纯数学计算，O(1)，无锁（asyncio 单线程模型保证原子性）。
    """

    def __init__(self, rate: float, capacity: float) -> None:
        """
        Args:
            rate:     每秒补充令牌数（QPS 上限）
            capacity: 桶容量（允许的瞬时突发量）
        """
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity          # 初始满桶
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        """按时间差补充令牌（惰性计算，仅在 acquire 时触发）。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """
        消耗 tokens 个令牌。若不足则等待直到令牌充足。
        等待时间 = (tokens - available) / rate，精确无忙等。
        """
        while True:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return
            # 精确计算等待时间，避免忙等
            wait = (tokens - self._tokens) / self._rate
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

@dataclass
class BreakerConfig:
    """熔断器配置，所有参数均有合理默认值，开箱即用。"""
    failure_threshold: int   = 5      # 连续失败 N 次触发熔断
    recovery_s: float        = 30.0   # OPEN → HALF_OPEN 等待时间（秒）
    success_threshold: int   = 2      # HALF_OPEN 连续成功 N 次恢复 CLOSED
    # 令牌桶参数
    qps: float               = 1.0    # 每秒最大请求数（合规限速）
    burst: float             = 3.0    # 允许的瞬时突发量
    # 人工兜底回调（熔断触发时调用）
    on_open: Optional[Callable[[str], Awaitable[None]]] = field(
        default=None, repr=False
    )


class CircuitBreaker:
    """
    令牌桶 + 熔断器的组合实现。

    用法::

        cb = CircuitBreaker(BreakerConfig(failure_threshold=3, qps=0.5))

        async with cb.guard("fetch_messages"):
            data = await scrape_messages(page)
            cb.record_success()

    熔断后 guard() 会抛出 BreakerOpenError，调用方捕获后触发人工兜底。
    """

    def __init__(self, config: BreakerConfig) -> None:
        self._cfg = config
        self._state = BreakerState.CLOSED
        self._failure_streak = 0       # 连续失败计数
        self._half_open_successes = 0  # HALF_OPEN 成功计数
        self._opened_at: float = 0.0   # 进入 OPEN 的时间戳
        self._bucket = TokenBucket(config.qps, config.burst)
        # 滑动窗口失败记录（用于监控，非熔断判断）
        self._recent_failures: deque[float] = deque(maxlen=100)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def guard(self, operation: str = "") -> None:
        """
        进入受保护区域前调用。
        - OPEN 状态：检查是否可以转 HALF_OPEN，否则抛出 BreakerOpenError
        - 通过后：从令牌桶消耗一个令牌（限速）
        """
        self._maybe_recover()

        if self._state == BreakerState.OPEN:
            raise BreakerOpenError(
                f"circuit breaker OPEN op={operation!r} "
                f"retry_after={self._retry_after():.1f}s"
            )

        # 令牌桶限速（合规核心）
        await self._bucket.acquire()

    def record_success(self) -> None:
        """操作成功后调用，重置失败计数。"""
        self._failure_streak = 0
        if self._state == BreakerState.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= self._cfg.success_threshold:
                self._transition(BreakerState.CLOSED)

    def record_failure(self) -> None:
        """操作失败后调用，累积失败计数，达阈值则熔断。"""
        self._failure_streak += 1
        self._recent_failures.append(time.monotonic())
        if (self._state in (BreakerState.CLOSED, BreakerState.HALF_OPEN)
                and self._failure_streak >= self._cfg.failure_threshold):
            self._transition(BreakerState.OPEN)

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def failure_streak(self) -> int:
        return self._failure_streak

    # ------------------------------------------------------------------
    # 内部状态机
    # ------------------------------------------------------------------

    def _maybe_recover(self) -> None:
        """OPEN → HALF_OPEN 的时间驱动转换。"""
        if (self._state == BreakerState.OPEN
                and time.monotonic() - self._opened_at >= self._cfg.recovery_s):
            self._transition(BreakerState.HALF_OPEN)

    def _transition(self, new_state: BreakerState) -> None:
        old = self._state
        self._state = new_state
        logger.warning(
            "circuit breaker transition %s → %s failures=%d",
            old.name, new_state.name, self._failure_streak,
        )
        if new_state == BreakerState.OPEN:
            self._opened_at = time.monotonic()
            self._half_open_successes = 0
            # 触发人工兜底回调（异步，不阻塞主流程）
            if self._cfg.on_open:
                asyncio.create_task(
                    self._cfg.on_open(
                        f"熔断触发：连续失败 {self._failure_streak} 次，"
                        f"将在 {self._cfg.recovery_s}s 后尝试恢复"
                    )
                )
        elif new_state == BreakerState.CLOSED:
            self._failure_streak = 0
            self._half_open_successes = 0

    def _retry_after(self) -> float:
        """距离下次 HALF_OPEN 尝试的剩余秒数。"""
        return max(0.0, self._cfg.recovery_s - (time.monotonic() - self._opened_at))


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class BreakerOpenError(RuntimeError):
    """熔断器处于 OPEN 状态时抛出，调用方应触发人工兜底。"""
