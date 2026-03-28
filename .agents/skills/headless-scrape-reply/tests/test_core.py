"""
test_core.py — headless-scrape-reply 核心模块单元测试
Author: RussellCooper

测试策略：
  - 令牌桶：数学验证（精确计算等待时间）
  - 熔断器：状态机完整路径覆盖（CLOSED→OPEN→HALF_OPEN→CLOSED）
  - 选择器引擎：Mock Playwright Page，验证降级路径
  - 内容过滤：边界条件（空字符串、禁止词大小写）
  - 审计日志：append-only 验证
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 令牌桶测试 ────────────────────────────────────────────────

class TestTokenBucket:
    def _make(self, rate: float, capacity: float):
        from core.circuit_breaker import TokenBucket
        return TokenBucket(rate, capacity)

    @pytest.mark.asyncio
    async def test_acquire_within_capacity(self):
        """桶内有令牌时，acquire 应立即返回（无等待）。"""
        bucket = self._make(rate=10.0, capacity=10.0)
        start = time.monotonic()
        await bucket.acquire(1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, f"should be instant, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_acquire_waits_when_empty(self):
        """桶空时，acquire 应等待足够时间。"""
        bucket = self._make(rate=10.0, capacity=1.0)
        await bucket.acquire(1.0)  # 清空桶
        start = time.monotonic()
        await bucket.acquire(1.0)  # 应等待 ~0.1s
        elapsed = time.monotonic() - start
        assert 0.05 < elapsed < 0.5, f"expected ~0.1s wait, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_capacity_cap(self):
        """令牌数不应超过 capacity。"""
        bucket = self._make(rate=100.0, capacity=5.0)
        # 等待足够长时间让令牌溢出
        await asyncio.sleep(0.1)
        # 消耗 5 个令牌（最大容量）
        for _ in range(5):
            await bucket.acquire(1.0)
        # 第 6 个应该需要等待
        start = time.monotonic()
        await bucket.acquire(1.0)
        elapsed = time.monotonic() - start
        assert elapsed > 0.005, "6th token should require waiting"


# ── 熔断器状态机测试 ──────────────────────────────────────────

class TestCircuitBreaker:
    def _make(self, threshold=3, recovery_s=0.1, qps=100.0):
        from core.circuit_breaker import BreakerConfig, CircuitBreaker, BreakerState
        cfg = BreakerConfig(
            failure_threshold=threshold,
            recovery_s=recovery_s,
            success_threshold=2,
            qps=qps,
            burst=100.0,
        )
        return CircuitBreaker(cfg)

    @pytest.mark.asyncio
    async def test_closed_to_open(self):
        """连续失败达阈值后应转为 OPEN。"""
        from core.circuit_breaker import BreakerState
        cb = self._make(threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == BreakerState.OPEN

    @pytest.mark.asyncio
    async def test_open_blocks_requests(self):
        """OPEN 状态下 guard() 应抛出 BreakerOpenError。"""
        from core.circuit_breaker import BreakerOpenError, BreakerState
        cb = self._make(threshold=1)
        cb.record_failure()
        assert cb.state == BreakerState.OPEN
        with pytest.raises(BreakerOpenError):
            await cb.guard("test")

    @pytest.mark.asyncio
    async def test_open_to_half_open(self):
        """等待 recovery_s 后应自动转为 HALF_OPEN。"""
        from core.circuit_breaker import BreakerState
        cb = self._make(threshold=1, recovery_s=0.05)
        cb.record_failure()
        assert cb.state == BreakerState.OPEN
        await asyncio.sleep(0.1)
        await cb.guard("probe")  # 触发状态检查
        assert cb.state == BreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_to_closed(self):
        """HALF_OPEN 连续成功 success_threshold 次后应恢复 CLOSED。"""
        from core.circuit_breaker import BreakerState
        cb = self._make(threshold=1, recovery_s=0.05)
        cb.record_failure()
        await asyncio.sleep(0.1)
        await cb.guard("probe")  # → HALF_OPEN
        cb.record_success()
        cb.record_success()
        assert cb.state == BreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_on_open_callback_called(self):
        """熔断触发时应调用 on_open 回调。"""
        from core.circuit_breaker import BreakerConfig, CircuitBreaker
        called_with = []

        async def on_open(msg: str):
            called_with.append(msg)

        cfg = BreakerConfig(
            failure_threshold=1,
            recovery_s=1.0,
            qps=100.0,
            burst=100.0,
            on_open=on_open,
        )
        cb = CircuitBreaker(cfg)
        cb.record_failure()
        await asyncio.sleep(0.05)  # 让 create_task 执行
        assert len(called_with) == 1
        assert "熔断" in called_with[0]


# ── 选择器引擎测试 ────────────────────────────────────────────

class TestSelectorEngine:
    def _make_page(self):
        """创建 Mock Playwright Page。"""
        page = MagicMock()
        return page

    @pytest.mark.asyncio
    async def test_p1_semantic_success(self):
        """P1 语义选择器成功时应返回 tier=1。"""
        from core.selector import SelectorEngine, SelectorSpec

        page = self._make_page()
        mock_locator = AsyncMock()
        mock_locator.wait_for = AsyncMock(return_value=None)
        page.get_by_test_id = MagicMock(return_value=mock_locator)

        engine = SelectorEngine(page)
        spec = SelectorSpec(testid="my-btn", css=".fallback")
        result = await engine.locate(spec)

        assert result.tier == 1
        assert result.element is mock_locator
        assert result.fallback_count == 0

    @pytest.mark.asyncio
    async def test_fallback_to_p2_text(self):
        """P1 失败时应降级到 P2 文本选择器，tier=2，fallback_count 递增。"""
        from core.selector import SelectorEngine, SelectorSpec

        page = self._make_page()

        # P1 失败
        p1_locator = AsyncMock()
        p1_locator.wait_for = AsyncMock(side_effect=Exception("not found"))
        page.get_by_test_id = MagicMock(return_value=p1_locator)

        # P2 成功
        p2_locator = AsyncMock()
        p2_locator.wait_for = AsyncMock(return_value=None)
        page.get_by_text = MagicMock(return_value=p2_locator)

        engine = SelectorEngine(page)
        spec = SelectorSpec(testid="my-btn", text="发送")
        result = await engine.locate(spec)

        assert result.tier == 2
        assert result.element is p2_locator
        assert result.fallback_count == 1

    @pytest.mark.asyncio
    async def test_all_fail_returns_none(self):
        """所有选择器失败时应返回 element=None，tier=0。"""
        from core.selector import SelectorEngine, SelectorSpec

        page = self._make_page()
        fail_locator = AsyncMock()
        fail_locator.wait_for = AsyncMock(side_effect=Exception("not found"))
        page.get_by_test_id = MagicMock(return_value=fail_locator)
        page.locator = MagicMock(return_value=fail_locator)

        engine = SelectorEngine(page)
        spec = SelectorSpec(testid="ghost", css=".ghost")
        result = await engine.locate(spec)

        assert result.element is None
        assert result.tier == 0


# ── JSONPath 工具测试 ─────────────────────────────────────────

class TestJsonPathGet:
    def test_simple_path(self):
        from core.selector import _json_path_get
        obj = {"data": {"list": [{"content": "hello"}]}}
        assert _json_path_get(obj, "data.list.0.content") == "hello"

    def test_missing_key(self):
        from core.selector import _json_path_get
        assert _json_path_get({"a": 1}, "a.b.c") is None

    def test_array_index(self):
        from core.selector import _json_path_get
        assert _json_path_get([10, 20, 30], "1") == 20

    def test_out_of_range(self):
        from core.selector import _json_path_get
        assert _json_path_get([1, 2], "5") is None


# ── 内容过滤测试 ──────────────────────────────────────────────

class TestContentFilter:
    def _make_replier(self, forbidden=None):
        from core.replier import ReplierConfig, ReplierEngine
        cfg = ReplierConfig(forbidden_words=forbidden or [])
        page = MagicMock()
        return ReplierEngine(cfg, page)

    def test_clean_text_passes(self):
        r = self._make_replier(forbidden=["退款"])
        assert r._content_safe("感谢您的反馈，我们会尽快处理。") is True

    def test_forbidden_word_blocked(self):
        r = self._make_replier(forbidden=["退款"])
        assert r._content_safe("您的退款申请已收到") is False

    def test_case_insensitive(self):
        r = self._make_replier(forbidden=["refund"])
        assert r._content_safe("Your REFUND is processed") is False

    def test_empty_text(self):
        r = self._make_replier(forbidden=["退款"])
        assert r._content_safe("") is True


# ── 审计日志测试 ──────────────────────────────────────────────

class TestAuditLog:
    def test_append_only(self):
        from core.replier import AuditLog
        with tempfile.TemporaryDirectory() as tmpdir:
            log = AuditLog(f"{tmpdir}/audit.jsonl")
            log.write({"op": "reply_sent", "content": "hello"})
            log.write({"op": "reply_sent", "content": "world"})
            records = log.tail(10)
            assert len(records) == 2
            assert records[0]["content"] == "hello"
            assert records[1]["content"] == "world"
            # 验证时间戳存在
            assert "ts" in records[0]

    def test_tail_limit(self):
        from core.replier import AuditLog
        with tempfile.TemporaryDirectory() as tmpdir:
            log = AuditLog(f"{tmpdir}/audit.jsonl")
            for i in range(10):
                log.write({"op": "test", "i": i})
            records = log.tail(3)
            assert len(records) == 3
            assert records[-1]["i"] == 9  # 最后一条


# ── 指标收集器测试 ────────────────────────────────────────────

class TestMetrics:
    def test_inc_and_snapshot(self):
        from core.monitor import Metrics
        m = Metrics()
        m.inc("scrape_total", 5)
        m.inc("scrape_success", 4)
        snap = m.snapshot()
        assert snap["counter.scrape_total"] == 5
        assert snap["success_rate"] == pytest.approx(0.8)

    def test_gauge(self):
        from core.monitor import Metrics
        m = Metrics()
        m.set("breaker_state", 2.0)
        snap = m.snapshot()
        assert snap["gauge.breaker_state"] == 2.0
