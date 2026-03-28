"""
engine.py — 主调度引擎（顶层入口）
Author: RussellCooper

将 scraper + replier + monitor + circuit_breaker 串联为完整的
"采集 → 生成回复 → 人工确认 → 提交" 流水线。

使用方式（最简）::

    from core.engine import ScrapeReplyEngine, EngineConfig
    import asyncio

    async def main():
        cfg = EngineConfig.from_yaml("config/example.yaml")
        async with ScrapeReplyEngine(cfg) as engine:
            await engine.run_forever()   # 持续运行

    asyncio.run(main())

使用方式（单次）::

    async with ScrapeReplyEngine(cfg) as engine:
        result = await engine.run_once()
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import yaml

from .circuit_breaker import BreakerConfig, BreakerOpenError, CircuitBreaker
from .monitor import AlertConfig, Alerter, Metrics, Monitor
from .replier import ReplierConfig, ReplierEngine
from .scraper import ScrapeResult, ScrapeTask, ScraperConfig, ScraperEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 顶层配置（聚合所有子模块配置）
# ---------------------------------------------------------------------------

@dataclass
class EngineConfig:
    """
    完整引擎配置。支持从 YAML 文件加载，实现配置驱动。
    """
    scraper: ScraperConfig = field(default_factory=ScraperConfig)
    replier: ReplierConfig = field(default_factory=ReplierConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)

    # 调度参数
    poll_interval_s: float = 10.0    # 每轮采集间隔（秒）
    max_rounds: Optional[int] = None  # None = 无限循环

    # 采集任务描述（配置驱动，无需修改代码）
    task: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str) -> "EngineConfig":
        """从 YAML 文件加载配置。"""
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        cfg = cls()
        if "scraper" in raw:
            s = raw["scraper"]
            cfg.scraper = ScraperConfig(**{
                k: v for k, v in s.items()
                if k in ScraperConfig.__dataclass_fields__
                and k != "breaker"
            })
            if "breaker" in s:
                cfg.scraper.breaker = BreakerConfig(**s["breaker"])

        if "replier" in raw:
            cfg.replier = ReplierConfig(**{
                k: v for k, v in raw["replier"].items()
                if k in ReplierConfig.__dataclass_fields__
            })

        if "alert" in raw:
            cfg.alert = AlertConfig(**{
                k: v for k, v in raw["alert"].items()
                if k in AlertConfig.__dataclass_fields__
                and k != "custom_fn"
            })

        if "schedule" in raw:
            cfg.poll_interval_s = raw["schedule"].get("interval_s", 10.0)
            cfg.max_rounds = raw["schedule"].get("max_rounds")

        if "task" in raw:
            cfg.task = raw["task"]

        return cfg


# ---------------------------------------------------------------------------
# 主调度引擎
# ---------------------------------------------------------------------------

class ScrapeReplyEngine:
    """
    完整的"采集 → 回复"自动化引擎。

    生命周期管理：
        __aenter__ → 启动浏览器 + 监控
        run_forever / run_once → 执行业务逻辑
        __aexit__ → 优雅关闭（保存会话 + 停止监控）

    信号处理：
        SIGINT / SIGTERM → 优雅停止（完成当前轮次后退出）
    """

    def __init__(self, config: EngineConfig) -> None:
        self._cfg = config
        self._metrics = Metrics()
        self._alerter = Alerter(config.alert)
        self._monitor = Monitor(self._metrics, self._alerter)
        self._scraper: Optional[ScraperEngine] = None
        self._replier: Optional[ReplierEngine] = None
        self._running = False

    async def __aenter__(self) -> "ScrapeReplyEngine":
        # 注册熔断告警回调
        self._cfg.scraper.breaker.on_open = self._on_breaker_open

        self._scraper = ScraperEngine(self._cfg.scraper)
        await self._scraper.__aenter__()

        self._replier = ReplierEngine(self._cfg.replier, self._scraper._page)
        await self._monitor.start()

        # 注册优雅停止信号
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except (NotImplementedError, RuntimeError):
                pass  # Windows 不支持 add_signal_handler

        self._running = True
        logger.info("ScrapeReplyEngine started")
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._running = False
        if self._scraper:
            await self._scraper.__aexit__(None, None, None)
        await self._monitor.stop()
        logger.info("ScrapeReplyEngine stopped")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """持续运行，直到收到停止信号或达到 max_rounds。"""
        round_num = 0
        while self._running:
            if self._cfg.max_rounds and round_num >= self._cfg.max_rounds:
                logger.info("max_rounds=%d reached, stopping", self._cfg.max_rounds)
                break

            await self.run_once()
            round_num += 1

            if self._running:
                await asyncio.sleep(self._cfg.poll_interval_s)

        logger.info("run_forever exited after %d rounds", round_num)

    async def run_once(self) -> Optional[ScrapeResult]:
        """执行一轮采集 + 回复。"""
        self._metrics.inc("scrape_total")

        # 构建采集任务
        task = ScrapeTask(
            items=self._cfg.task.get("items", []),
            network_fallback=self._cfg.task.get("network_fallback"),
            pagination=self._cfg.task.get("pagination"),
        )

        # 采集
        result = await self._scraper.scrape(task)

        if result.error:
            self._metrics.inc("scrape_failure")
            logger.error("scrape failed: %s", result.error)
            # 更新熔断器状态指标
            self._metrics.set(
                "breaker_state",
                self._scraper._breaker.state.value,
            )
            return result

        self._metrics.inc("scrape_success")
        self._metrics.inc("fallback_total", result.fallback_count)

        # 有数据时触发回复
        if result.data:
            context = "\n".join(
                f"{item.get('name', '')}: {item.get('value', '')}"
                for item in result.data
            )
            success = await self._replier.reply(result.data, context)
            if success:
                self._metrics.inc("reply_total")
                self._metrics.inc("reply_approved")
            else:
                self._metrics.inc("reply_rejected")

        return result

    def _request_stop(self) -> None:
        """信号处理器：请求优雅停止。"""
        logger.info("stop signal received, finishing current round...")
        self._running = False

    async def _on_breaker_open(self, message: str) -> None:
        """熔断器触发时的告警回调。"""
        self._metrics.inc("breaker_open_total")
        self._metrics.set("breaker_state", 2)  # OPEN = 2
        await self._alerter.alert(
            title="🔴 熔断器触发 — 需要人工介入",
            message=message,
            event_key="breaker_open",
        )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="RCclaw headless-scrape-reply engine")
    parser.add_argument("--config", default="config/example.yaml", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只运行一轮后退出")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    cfg = EngineConfig.from_yaml(args.config)

    async with ScrapeReplyEngine(cfg) as engine:
        if args.once:
            result = await engine.run_once()
            print(f"Result: {result}")
        else:
            await engine.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
