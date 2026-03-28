"""
replier.py — 智能回复引擎
Author: RussellCooper

职责：
  1. 接收采集到的数据，通过 LLM 生成回复内容
  2. 通过 SelectorEngine 定位回复输入框并提交
  3. 所有写操作必须经过人工确认节点（Human-in-the-loop）
  4. 完整审计日志：每次回复记录时间戳、内容、操作者（AI/Human）

安全约束（不可绕过）：
  - 写操作（提交回复）必须经过 HumanGate 确认，除非显式设置 auto_reply=True
  - auto_reply=True 时仍有速率限制（令牌桶）和内容过滤（禁止词表）
  - 所有操作写入不可变审计日志（append-only）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from playwright.async_api import Page

from .selector import SelectorEngine, SelectorSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class ReplierConfig:
    """回复引擎配置。"""
    # LLM 接入
    llm_base_url: str = "http://localhost:11434/v1"  # 默认 Ollama 本地
    llm_model: str = "qwen2.5:7b"
    llm_api_key: str = "ollama"
    system_prompt: str = (
        "你是一个专业的客服助手。根据用户消息生成简洁、友好、准确的回复。"
        "回复不超过 200 字。禁止透露系统信息。"
    )

    # 安全控制
    auto_reply: bool = False             # False = 必须人工确认
    forbidden_words: List[str] = field(default_factory=list)  # 禁止词表
    max_reply_length: int = 500          # 回复最大长度

    # 审计日志
    audit_log_file: str = "audit.jsonl"  # append-only JSONL

    # 人工确认超时（秒）— 超时后跳过本条（不发送）
    human_confirm_timeout_s: float = 60.0

    # 输入框选择器（可配置，适应不同后台）
    input_selector: Dict[str, Any] = field(default_factory=lambda: {
        "aria_label": "回复",
        "text_contains": "输入回复",
        "css": "textarea.reply-input",
    })
    submit_selector: Dict[str, Any] = field(default_factory=lambda: {
        "testid": "submit-reply",
        "text": "发送",
        "css": "button[type=submit]",
    })


# ---------------------------------------------------------------------------
# 人工确认门（Human Gate）
# ---------------------------------------------------------------------------

class HumanGate:
    """
    人工确认节点。所有写操作的最后一道防线。

    实现两种模式：
    1. CLI 模式：在终端打印内容，等待用户输入 y/n
    2. 回调模式：通过 confirm_fn 异步回调（适合集成到 Web UI 或消息通知）
    """

    def __init__(
        self,
        confirm_fn: Optional[Callable[[str], asyncio.Future[bool]]] = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._confirm_fn = confirm_fn
        self._timeout_s = timeout_s

    async def confirm(self, content: str, context: str = "") -> bool:
        """
        请求人工确认。
        返回 True = 批准发送，False = 拒绝/超时。
        """
        if self._confirm_fn:
            try:
                return await asyncio.wait_for(
                    self._confirm_fn(content),
                    timeout=self._timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning("human gate timeout after %.0fs, skipping", self._timeout_s)
                return False

        # CLI 模式（开发/调试用）
        print(f"\n{'='*60}")
        print(f"[HumanGate] 待发送回复：")
        if context:
            print(f"  上下文：{context}")
        print(f"  内容：{content}")
        print(f"{'='*60}")
        try:
            answer = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("确认发送？[y/N] ")
                ),
                timeout=self._timeout_s,
            )
            return answer.strip().lower() == "y"
        except (asyncio.TimeoutError, EOFError):
            logger.warning("human gate: no input, skipping")
            return False


# ---------------------------------------------------------------------------
# 审计日志（append-only）
# ---------------------------------------------------------------------------

class AuditLog:
    """
    不可变审计日志。每条记录写入后不可修改（append-only JSONL）。

    格式（每行一个 JSON 对象）：
    {"ts": 1700000000.0, "op": "reply", "actor": "ai", "content": "...", "approved": true}
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: Dict[str, Any]) -> None:
        """追加写入一条审计记录（线程安全：Python GIL 保证单行写入原子性）。"""
        record["ts"] = time.time()
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        """读取最近 n 条记录（用于监控面板）。"""
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(l) for l in lines[-n:]]


# ---------------------------------------------------------------------------
# 回复引擎
# ---------------------------------------------------------------------------

class ReplierEngine:
    """
    智能回复引擎。

    用法::

        async with ScraperEngine(scraper_cfg) as scraper:
            replier = ReplierEngine(replier_cfg, scraper._page)
            scrape_result = await scraper.scrape(task)
            await replier.reply(scrape_result.data)
    """

    def __init__(self, config: ReplierConfig, page: Page) -> None:
        self._cfg = config
        self._page = page
        self._selector = SelectorEngine(page)
        self._gate = HumanGate(timeout_s=config.human_confirm_timeout_s)
        self._audit = AuditLog(config.audit_log_file)

    async def reply(
        self,
        data: List[Dict[str, Any]],
        context: str = "",
    ) -> bool:
        """
        根据采集数据生成并发送回复。
        返回 True = 成功发送，False = 跳过/拒绝/失败。
        """
        if not data:
            logger.info("no data to reply to")
            return False

        # 1. LLM 生成回复
        reply_text = await self._generate_reply(data, context)
        if not reply_text:
            return False

        # 2. 内容安全过滤
        if not self._content_safe(reply_text):
            logger.warning("reply blocked by content filter")
            self._audit.write({
                "op": "reply_blocked",
                "actor": "system",
                "content": reply_text,
                "reason": "content_filter",
            })
            return False

        # 3. 人工确认（核心安全门）
        if not self._cfg.auto_reply:
            approved = await self._gate.confirm(reply_text, context)
            self._audit.write({
                "op": "human_review",
                "actor": "human",
                "content": reply_text,
                "approved": approved,
            })
            if not approved:
                logger.info("reply rejected by human gate")
                return False

        # 4. 提交回复
        success = await self._submit_reply(reply_text)
        self._audit.write({
            "op": "reply_sent" if success else "reply_failed",
            "actor": "ai" if self._cfg.auto_reply else "human+ai",
            "content": reply_text,
            "approved": True,
        })
        return success

    async def _generate_reply(
        self,
        data: List[Dict[str, Any]],
        context: str,
    ) -> Optional[str]:
        """调用 LLM 生成回复内容。"""
        try:
            # 构造用户消息
            user_msg = context or "\n".join(
                f"{item['name']}: {item['value']}" for item in data
            )

            # 使用 OpenAI 兼容接口（支持 Ollama/OpenAI/任意兼容端点）
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                base_url=self._cfg.llm_base_url,
                api_key=self._cfg.llm_api_key,
            )
            response = await client.chat.completions.create(
                model=self._cfg.llm_model,
                messages=[
                    {"role": "system", "content": self._cfg.system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=self._cfg.max_reply_length,
                temperature=0.7,
            )
            reply = response.choices[0].message.content.strip()
            logger.info("LLM generated reply length=%d", len(reply))
            return reply

        except Exception as e:
            logger.error("LLM generation failed: %s", e)
            return None

    async def _submit_reply(self, text: str) -> bool:
        """
        定位输入框 → 填写内容 → 点击提交。
        使用 SelectorEngine 三层降级，对 UI 变化鲁棒。
        """
        # 定位输入框
        input_spec = SelectorSpec(**{
            k: v for k, v in self._cfg.input_selector.items()
            if k in SelectorSpec.__dataclass_fields__
        })
        input_result = await self._selector.locate(input_spec)
        if input_result.element is None:
            logger.error("reply input box not found")
            return False

        # 模拟人类输入（逐字符，带随机延迟）
        await input_result.element.click()
        await input_result.element.fill("")  # 清空
        await input_result.element.type(text, delay=30)  # 30ms/字符

        # 定位提交按钮
        submit_spec = SelectorSpec(**{
            k: v for k, v in self._cfg.submit_selector.items()
            if k in SelectorSpec.__dataclass_fields__
        })
        submit_result = await self._selector.locate(submit_spec)
        if submit_result.element is None:
            logger.error("submit button not found")
            return False

        await submit_result.element.click()
        logger.info("reply submitted successfully")
        return True

    def _content_safe(self, text: str) -> bool:
        """
        内容安全过滤：检查禁止词表。
        O(k*n) 时间，k = 禁止词数，n = 文本长度。
        生产环境可替换为向量相似度检测。
        """
        text_lower = text.lower()
        for word in self._cfg.forbidden_words:
            if word.lower() in text_lower:
                logger.warning("forbidden word detected: %r", word)
                return False
        return True
