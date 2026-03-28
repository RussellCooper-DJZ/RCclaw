"""
monitor.py — 实时监控与告警模块
Author: RussellCooper

参考 BettaFish/ForumEngine/monitor.py 的监控设计，进行专家级重构：
  - 独立异步任务（不阻塞主采集流程）
  - 结构化指标（可接入 Prometheus/Grafana）
  - 多渠道告警（钉钉 / 企微 / 邮件 / 自定义回调）

指标体系：
  scrape_total        采集总次数
  scrape_success      采集成功次数
  scrape_failure      采集失败次数
  reply_total         回复总次数
  reply_approved      人工批准次数
  reply_rejected      人工拒绝次数
  fallback_total      选择器降级总次数
  breaker_open_total  熔断触发次数
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 指标收集器（轻量级，无外部依赖）
# ---------------------------------------------------------------------------

class Metrics:
    """
    原子计数器集合。
    asyncio 单线程模型保证操作原子性，无需锁。
    """

    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}
        self._start_time = time.monotonic()

    def inc(self, name: str, value: int = 1) -> None:
        """递增计数器。"""
        self._counters[name] = self._counters.get(name, 0) + value

    def set(self, name: str, value: float) -> None:
        """设置仪表盘值（如当前熔断器状态）。"""
        self._gauges[name] = value

    def snapshot(self) -> Dict[str, Any]:
        """获取当前指标快照（用于监控面板或日志）。"""
        uptime = time.monotonic() - self._start_time
        total = self._counters.get("scrape_total", 0)
        success = self._counters.get("scrape_success", 0)
        return {
            "uptime_s": round(uptime, 1),
            "success_rate": round(success / total, 3) if total > 0 else 0.0,
            **{f"counter.{k}": v for k, v in self._counters.items()},
            **{f"gauge.{k}": v for k, v in self._gauges.items()},
        }


# ---------------------------------------------------------------------------
# 告警渠道
# ---------------------------------------------------------------------------

@dataclass
class AlertConfig:
    """告警配置，支持多渠道。"""
    # 钉钉机器人
    dingtalk_webhook: Optional[str] = None
    # 企业微信机器人
    wecom_webhook: Optional[str] = None
    # 自定义回调（最灵活）
    custom_fn: Optional[Callable[[str, str], Awaitable[None]]] = field(
        default=None, repr=False
    )
    # 告警冷却时间（秒）— 防止告警风暴
    cooldown_s: float = 300.0


class Alerter:
    """
    多渠道告警发送器。
    支持冷却时间，防止同一事件重复告警。
    """

    def __init__(self, config: AlertConfig) -> None:
        self._cfg = config
        self._last_alert: Dict[str, float] = {}  # event_key → timestamp

    async def alert(self, title: str, message: str, event_key: str = "") -> None:
        """
        发送告警。同一 event_key 在冷却时间内只发送一次。
        """
        key = event_key or title
        now = time.monotonic()
        if now - self._last_alert.get(key, 0) < self._cfg.cooldown_s:
            logger.debug("alert suppressed by cooldown key=%r", key)
            return

        self._last_alert[key] = now
        logger.warning("ALERT [%s] %s", title, message)

        tasks = []
        if self._cfg.dingtalk_webhook:
            tasks.append(self._send_dingtalk(title, message))
        if self._cfg.wecom_webhook:
            tasks.append(self._send_wecom(title, message))
        if self._cfg.custom_fn:
            tasks.append(self._cfg.custom_fn(title, message))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_dingtalk(self, title: str, message: str) -> None:
        """发送钉钉机器人消息。"""
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n{message}",
            },
        }
        await self._post(self._cfg.dingtalk_webhook, payload)

    async def _send_wecom(self, title: str, message: str) -> None:
        """发送企业微信机器人消息。"""
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": f"**{title}**\n>{message}"},
        }
        await self._post(self._cfg.wecom_webhook, payload)

    async def _post(self, url: str, payload: Dict) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("alert post failed status=%d url=%s", resp.status, url)
        except Exception as e:
            logger.warning("alert post error: %s", e)


# ---------------------------------------------------------------------------
# 监控主循环
# ---------------------------------------------------------------------------

class Monitor:
    """
    独立异步监控任务。
    每隔 interval_s 秒打印指标快照，并检查告警条件。

    设计：与主采集流程完全解耦，通过共享 Metrics 对象通信，
    无锁（asyncio 单线程），零额外开销。
    """

    def __init__(
        self,
        metrics: Metrics,
        alerter: Alerter,
        interval_s: float = 30.0,
    ) -> None:
        self._metrics = metrics
        self._alerter = alerter
        self._interval = interval_s
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动后台监控任务。"""
        self._task = asyncio.create_task(self._loop(), name="monitor")
        logger.info("monitor started interval=%.0fs", self._interval)

    async def stop(self) -> None:
        """停止监控任务。"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("monitor stopped")

    async def _loop(self) -> None:
        """监控主循环。"""
        while True:
            await asyncio.sleep(self._interval)
            snap = self._metrics.snapshot()
            logger.info("metrics snapshot: %s", json.dumps(snap))

            # 告警条件检查
            await self._check_alerts(snap)

    async def _check_alerts(self, snap: Dict[str, Any]) -> None:
        """检查是否需要触发告警。"""
        # 熔断器开启告警
        if snap.get("gauge.breaker_state", 0) == 2:  # OPEN = 2
            await self._alerter.alert(
                title="⚠️ 熔断器触发",
                message=(
                    f"采集流程已熔断，连续失败 "
                    f"{snap.get('counter.scrape_failure', 0)} 次。\n"
                    f"成功率：{snap.get('success_rate', 0):.1%}"
                ),
                event_key="breaker_open",
            )

        # 成功率过低告警
        if snap.get("success_rate", 1.0) < 0.5 and snap.get("counter.scrape_total", 0) >= 10:
            await self._alerter.alert(
                title="📉 采集成功率过低",
                message=f"最近成功率 {snap.get('success_rate', 0):.1%}，请检查目标页面是否发生变化。",
                event_key="low_success_rate",
            )
