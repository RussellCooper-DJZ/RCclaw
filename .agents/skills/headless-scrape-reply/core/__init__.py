"""
headless-scrape-reply core package
Author: RussellCooper

公共接口导出。外部使用者只需从此包导入，无需了解内部模块结构。
"""

from .circuit_breaker import BreakerConfig, BreakerOpenError, CircuitBreaker, TokenBucket
from .engine import EngineConfig, ScrapeReplyEngine
from .monitor import AlertConfig, Alerter, Metrics, Monitor
from .replier import AuditLog, HumanGate, ReplierConfig, ReplierEngine
from .scraper import ScrapeResult, ScrapeTask, ScraperConfig, ScraperEngine
from .selector import (
    LocateResult,
    NetworkInterceptor,
    NetworkSpec,
    SelectorEngine,
    SelectorSpec,
)

__all__ = [
    # Engine (top-level)
    "ScrapeReplyEngine",
    "EngineConfig",
    # Scraper
    "ScraperEngine",
    "ScraperConfig",
    "ScrapeTask",
    "ScrapeResult",
    # Replier
    "ReplierEngine",
    "ReplierConfig",
    "HumanGate",
    "AuditLog",
    # Selector
    "SelectorEngine",
    "SelectorSpec",
    "LocateResult",
    "NetworkInterceptor",
    "NetworkSpec",
    # Circuit Breaker
    "CircuitBreaker",
    "BreakerConfig",
    "BreakerOpenError",
    "TokenBucket",
    # Monitor
    "Monitor",
    "Metrics",
    "Alerter",
    "AlertConfig",
]
