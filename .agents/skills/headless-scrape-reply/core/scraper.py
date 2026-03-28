"""
scraper.py — 高性能无头采集引擎
Author: RussellCooper

架构：
  ┌─────────────────────────────────────────────────────┐
  │                  ScraperEngine                       │
  │                                                      │
  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
  │  │ Selector │  │ Network  │  │ CircuitBreaker   │  │
  │  │ Engine   │  │Intercept │  │ + TokenBucket    │  │
  │  │ (P1→P3)  │  │  (P3+)   │  │ (合规 + 熔断)    │  │
  │  └──────────┘  └──────────┘  └──────────────────┘  │
  └─────────────────────────────────────────────────────┘

核心设计决策：
  1. 浏览器实例复用：单 BrowserContext 跨多轮采集，避免重复启动开销
  2. 网络拦截优先：page.on('response') 事件驱动，O(1) 开销，无 DOM 轮询
  3. 状态持久化：session storage 保持登录态，避免重复登录
  4. 人类行为模拟：随机延迟 + 鼠标移动轨迹，降低被检测风险
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .circuit_breaker import BreakerConfig, BreakerOpenError, CircuitBreaker
from .selector import NetworkInterceptor, NetworkSpec, SelectorEngine, SelectorSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class ScraperConfig:
    """
    采集引擎配置。所有参数均可通过 YAML/JSON 注入，支持热重载。
    """
    # 目标
    login_url: str = ""
    target_url: str = ""

    # 浏览器
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    viewport_width: int = 1280
    viewport_height: int = 800
    # CDP 模式（连接已有浏览器，保留登录态）
    cdp_endpoint: Optional[str] = None   # 如 "http://localhost:9222"

    # 会话持久化
    session_file: str = ".session.json"  # 保存 cookies + localStorage

    # 采集间隔（秒）— 令牌桶之上的额外人类行为延迟
    min_interval_s: float = 2.0
    max_interval_s: float = 5.0

    # 熔断器
    breaker: BreakerConfig = field(default_factory=BreakerConfig)

    # 页面加载超时（毫秒）
    page_timeout_ms: int = 30_000


@dataclass
class ScrapeTask:
    """
    一次采集任务的完整描述。
    将"采集什么"与"怎么采集"解耦，实现配置驱动。
    """
    # 要提取的数据项列表（每项对应一个 SelectorSpec）
    items: List[Dict[str, Any]] = field(default_factory=list)
    # 网络拦截规格（DOM 全部失败时的最后防线）
    network_fallback: Optional[Dict[str, Any]] = None
    # 采集后是否需要翻页
    pagination: Optional[Dict[str, Any]] = None


@dataclass
class ScrapeResult:
    """采集结果，携带完整的审计信息。"""
    data: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "dom"          # "dom" | "network" | "mixed"
    tier_used: int = 1           # 最低降级层级（越高说明 UI 越不稳定）
    fallback_count: int = 0      # 本轮触发降级次数
    duration_ms: float = 0.0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# 采集引擎
# ---------------------------------------------------------------------------

class ScraperEngine:
    """
    高性能无头采集引擎。

    生命周期::

        async with ScraperEngine(config) as engine:
            result = await engine.scrape(task)

    内部维护单个 BrowserContext，跨多次 scrape() 调用复用，
    避免重复启动浏览器（启动耗时约 800ms–2s）。
    """

    def __init__(self, config: ScraperConfig) -> None:
        self._cfg = config
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._breaker = CircuitBreaker(config.breaker)
        self._selector_engine: Optional[SelectorEngine] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "ScraperEngine":
        await self._start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._stop()

    async def _start(self) -> None:
        self._playwright = await async_playwright().start()
        cfg = self._cfg

        if cfg.cdp_endpoint:
            # CDP 模式：连接已有浏览器，保留用户登录态
            self._browser = await self._playwright.chromium.connect_over_cdp(
                cfg.cdp_endpoint
            )
            self._context = self._browser.contexts[0]
            logger.info("connected via CDP endpoint=%s", cfg.cdp_endpoint)
        else:
            # 标准模式：启动新浏览器
            self._browser = await self._playwright.chromium.launch(
                headless=cfg.headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",  # 反检测
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=cfg.user_agent,
                viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
                # 注入 stealth 脚本，消除 navigator.webdriver 特征
                java_script_enabled=True,
            )
            await self._inject_stealth(self._context)
            await self._restore_session()

        self._page = await self._context.new_page()
        self._selector_engine = SelectorEngine(self._page)
        logger.info("scraper engine started headless=%s", cfg.headless)

    async def _stop(self) -> None:
        await self._save_session()
        if self._page:
            await self._page.close()
        if self._browser and not self._cfg.cdp_endpoint:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("scraper engine stopped")

    # ------------------------------------------------------------------
    # 核心采集接口
    # ------------------------------------------------------------------

    async def scrape(self, task: ScrapeTask) -> ScrapeResult:
        """
        执行一次采集任务。
        自动处理：熔断检查 → 限速 → DOM 采集 → 网络降级 → 结果聚合。
        """
        start = time.monotonic()
        result = ScrapeResult()

        # 1. 熔断检查 + 令牌桶限速
        try:
            await self._breaker.guard("scrape")
        except BreakerOpenError as e:
            result.error = str(e)
            logger.error("scrape blocked by circuit breaker: %s", e)
            return result

        # 2. 导航到目标页面
        try:
            await self._navigate()
        except Exception as e:
            self._breaker.record_failure()
            result.error = f"navigation failed: {e}"
            return result

        # 3. 采集数据（DOM 优先，网络兜底）
        async with NetworkInterceptor(self._page) as net:
            net.reset()
            dom_data, tier, fallbacks = await self._extract_dom(task)

            if dom_data:
                result.data = dom_data
                result.source = "dom"
                self._breaker.record_success()
            elif task.network_fallback:
                # DOM 全部失败，降级到网络拦截层
                logger.warning("DOM extraction failed, falling back to network layer")
                net_spec = NetworkSpec(**task.network_fallback)
                net_data = await net.wait_for_match(net_spec)
                if net_data is not None:
                    result.data = net_data if isinstance(net_data, list) else [net_data]
                    result.source = "network"
                    self._breaker.record_success()
                else:
                    self._breaker.record_failure()
                    result.error = "both DOM and network extraction failed"
            else:
                self._breaker.record_failure()
                result.error = "DOM extraction failed, no network fallback configured"

        result.tier_used = tier
        result.fallback_count = fallbacks
        result.duration_ms = (time.monotonic() - start) * 1000

        # 4. 人类行为延迟（合规）
        await self._human_delay()

        logger.info(
            "scrape done source=%s items=%d tier=%d fallbacks=%d duration=%.0fms",
            result.source, len(result.data), tier, fallbacks, result.duration_ms,
        )
        return result

    # ------------------------------------------------------------------
    # DOM 提取
    # ------------------------------------------------------------------

    async def _extract_dom(
        self, task: ScrapeTask
    ) -> tuple[List[Dict[str, Any]], int, int]:
        """
        遍历 task.items，对每个条目用 SelectorEngine 定位并提取文本。
        返回 (data, max_tier_used, total_fallbacks)。
        """
        data: List[Dict[str, Any]] = []
        max_tier = 1
        total_fallbacks = 0

        for item_spec in task.items:
            name = item_spec.get("name", "unknown")
            sel_spec = SelectorSpec(**{
                k: v for k, v in item_spec.items()
                if k in SelectorSpec.__dataclass_fields__
            })
            result = await self._selector_engine.locate(sel_spec)
            if result.element is None:
                logger.warning("item=%r not found by any selector", name)
                continue

            try:
                text = await result.element.inner_text()
                data.append({"name": name, "value": text.strip()})
                max_tier = max(max_tier, result.tier)
                total_fallbacks += result.fallback_count
            except Exception as e:
                logger.warning("item=%r text extraction failed: %s", name, e)

        return data, max_tier, total_fallbacks

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _navigate(self) -> None:
        """导航到目标 URL，等待网络空闲。"""
        await self._page.goto(
            self._cfg.target_url,
            wait_until="networkidle",
            timeout=self._cfg.page_timeout_ms,
        )

    async def _human_delay(self) -> None:
        """
        模拟人类操作间隔，随机化延迟。
        均匀分布：E[delay] = (min + max) / 2
        """
        delay = random.uniform(
            self._cfg.min_interval_s,
            self._cfg.max_interval_s,
        )
        await asyncio.sleep(delay)

    async def _inject_stealth(self, context: BrowserContext) -> None:
        """
        注入反自动化检测脚本。
        消除 navigator.webdriver = true 等 Playwright 特征。
        """
        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
        window.chrome = {runtime: {}};
        """
        await context.add_init_script(stealth_js)

    async def _save_session(self) -> None:
        """持久化 cookies，下次启动时恢复登录态。"""
        if not self._context:
            return
        try:
            cookies = await self._context.cookies()
            Path(self._cfg.session_file).write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug("session saved to %s", self._cfg.session_file)
        except Exception as e:
            logger.warning("session save failed: %s", e)

    async def _restore_session(self) -> None:
        """从文件恢复 cookies，跳过重复登录。"""
        session_path = Path(self._cfg.session_file)
        if not session_path.exists():
            return
        try:
            cookies = json.loads(session_path.read_text(encoding="utf-8"))
            await self._context.add_cookies(cookies)
            logger.info("session restored from %s (%d cookies)", session_path, len(cookies))
        except Exception as e:
            logger.warning("session restore failed: %s", e)
