"""
selector.py — 三优先级容错选择器引擎
Author: RussellCooper

设计原则（参考 SQLite 的"永不丢数据"哲学）：
  永不因 UI 变化而崩溃，三层降级，每层失败自动切换下一层。

优先级链（高 → 低）：
  P1  语义属性  data-testid / aria-label / role+name  — UI 重构时最稳定
  P2  文本内容  :has-text() / get_by_text()           — 标签变了但文字不变
  P3  网络拦截  page.on('response')                   — UI 完全变化时的最后防线

复杂度：
  locate()  O(k)  k = 选择器候选数，通常 ≤ 5
  网络拦截  O(1)  事件驱动，零轮询开销
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from playwright.async_api import Page, Response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SelectorSpec:
    """
    单个目标元素的选择器规格。
    按 priority 升序排列，值越小优先级越高。
    """
    # P1: 语义属性选择器（最稳定）
    testid: Optional[str] = None          # data-testid="submit-btn"
    aria_label: Optional[str] = None      # aria-label="发送"
    role: Optional[str] = None            # role="button"
    role_name: Optional[str] = None       # role+accessible name

    # P2: 文本内容选择器（次稳定）
    text: Optional[str] = None            # 精确文本
    text_contains: Optional[str] = None   # 包含文本

    # P3: CSS/XPath 兜底（最脆弱，最后尝试）
    css: Optional[str] = None
    xpath: Optional[str] = None

    # 超时（毫秒）
    timeout_ms: int = 5_000


@dataclass
class NetworkSpec:
    """
    网络拦截规格：当所有 DOM 选择器失败时，从响应体提取数据。
    """
    url_pattern: str                      # 正则，匹配目标 XHR/Fetch URL
    method: str = "GET"                   # HTTP 方法过滤
    json_path: Optional[str] = None       # 简单 JSONPath，如 "data.list"
    timeout_s: float = 10.0              # 等待超时（秒）


@dataclass
class LocateResult:
    """locate() 的返回值，携带降级路径信息供审计。"""
    element: Any                          # Playwright Locator 或 None
    tier: int                             # 1=语义, 2=文本, 3=CSS/XPath, 0=全部失败
    selector_used: str = ""
    fallback_count: int = 0              # 触发降级次数（监控指标）


# ---------------------------------------------------------------------------
# 核心引擎
# ---------------------------------------------------------------------------

class SelectorEngine:
    """
    三优先级容错选择器引擎。

    使用方式::

        engine = SelectorEngine(page)
        spec = SelectorSpec(
            testid="send-btn",
            text="发送",
            css="button.send",
        )
        result = await engine.locate(spec)
        if result.element:
            await result.element.click()
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._fallback_count = 0   # 全局降级计数，供熔断器读取

    @property
    def fallback_count(self) -> int:
        return self._fallback_count

    async def locate(self, spec: SelectorSpec) -> LocateResult:
        """
        按优先级链尝试定位元素，返回第一个成功的结果。
        O(k) 时间，k = 非空选择器数量。
        """
        candidates = self._build_candidates(spec)
        for tier, selector_str, locator_fn in candidates:
            try:
                loc = locator_fn()
                # 等待元素可见（不抛出则成功）
                await loc.wait_for(state="visible", timeout=spec.timeout_ms)
                if tier > 1:
                    self._fallback_count += 1
                    logger.warning(
                        "selector fallback tier=%d selector=%r fallback_total=%d",
                        tier, selector_str, self._fallback_count,
                    )
                return LocateResult(
                    element=loc,
                    tier=tier,
                    selector_used=selector_str,
                    fallback_count=self._fallback_count,
                )
            except Exception:
                continue

        logger.error("all selectors failed spec=%r", spec)
        return LocateResult(element=None, tier=0, fallback_count=self._fallback_count)

    def _build_candidates(
        self, spec: SelectorSpec
    ) -> List[tuple[int, str, Callable]]:
        """
        将 SelectorSpec 展开为 (tier, description, locator_factory) 三元组列表。
        tier 值越小优先级越高。
        """
        p = self._page
        candidates: List[tuple[int, str, Callable]] = []

        # --- P1: 语义属性（最稳定）---
        if spec.testid:
            candidates.append((1, f"testid={spec.testid!r}",
                                lambda s=spec.testid: p.get_by_test_id(s)))
        if spec.aria_label:
            candidates.append((1, f"aria-label={spec.aria_label!r}",
                                lambda s=spec.aria_label: p.get_by_label(s)))
        if spec.role and spec.role_name:
            candidates.append((1, f"role={spec.role!r} name={spec.role_name!r}",
                                lambda r=spec.role, n=spec.role_name:
                                    p.get_by_role(r, name=n)))
        elif spec.role:
            candidates.append((1, f"role={spec.role!r}",
                                lambda r=spec.role: p.get_by_role(r)))

        # --- P2: 文本内容（次稳定）---
        if spec.text:
            candidates.append((2, f"text={spec.text!r}",
                                lambda s=spec.text: p.get_by_text(s, exact=True)))
        if spec.text_contains:
            candidates.append((2, f"text~={spec.text_contains!r}",
                                lambda s=spec.text_contains: p.get_by_text(s)))

        # --- P3: CSS / XPath（最脆弱）---
        if spec.css:
            candidates.append((3, f"css={spec.css!r}",
                                lambda s=spec.css: p.locator(s)))
        if spec.xpath:
            candidates.append((3, f"xpath={spec.xpath!r}",
                                lambda s=spec.xpath: p.locator(f"xpath={s}")))

        return candidates


# ---------------------------------------------------------------------------
# 网络拦截层（P3 降级的最后防线）
# ---------------------------------------------------------------------------

class NetworkInterceptor:
    """
    基于 Playwright response 事件的网络层数据提取器。

    当所有 DOM 选择器失败时，直接从 XHR/Fetch 响应体中提取数据。
    这是对 UI 变化完全免疫的终极降级策略。

    原理：
        page.on('response', handler) 是事件驱动的，O(1) 开销，
        不需要轮询 DOM，也不依赖任何 CSS 类名或元素结构。
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._active = False

    async def __aenter__(self) -> "NetworkInterceptor":
        self._active = True
        self._page.on("response", self._on_response)
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._active = False
        self._page.remove_listener("response", self._on_response)

    async def _on_response(self, response: Response) -> None:
        """响应事件回调，在 Playwright 内部事件循环中执行。"""
        if not self._active:
            return
        try:
            body = await response.json()
            await self._queue.put({
                "url": response.url,
                "status": response.status,
                "body": body,
            })
        except Exception:
            pass  # 非 JSON 响应静默忽略

    async def wait_for_match(
        self, spec: NetworkSpec
    ) -> Optional[Dict[str, Any]]:
        """
        等待匹配 spec.url_pattern 的响应，提取 json_path 指定的字段。
        超时返回 None。
        """
        pattern = re.compile(spec.url_pattern)
        deadline = asyncio.get_event_loop().time() + spec.timeout_s

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning("network intercept timeout pattern=%r", spec.url_pattern)
                return None
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return None

            if not pattern.search(item["url"]):
                continue

            body = item["body"]
            if spec.json_path:
                body = _json_path_get(body, spec.json_path)
            return body

    def reset(self) -> None:
        """清空队列（每轮采集前调用）。"""
        while not self._queue.empty():
            self._queue.get_nowait()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _json_path_get(obj: Any, path: str) -> Any:
    """
    极简 JSONPath 实现，支持点分路径，如 "data.list.0.content"。
    O(d) 时间，d = 路径深度。
    """
    for key in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(key)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj
