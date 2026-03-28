# Headless Scrape & Reply Engine 技术白皮书

> **版本**: 1.0.0  
> **作者**: RussellCooper  
> **许可证**: MIT  
> **所属项目**: RCclaw Framework

本文档详细阐述了 `headless-scrape-reply` 技能的架构设计、核心模块实现、配置指南及安全合规机制。该系统专为**无开放 API 且前端界面频繁变化**的复杂后台系统设计，提供高可用、抗脆弱的自动化数据采集与智能回复能力。

---

## 1. 架构设计与核心理念

传统基于固定 CSS 选择器的 Web 自动化方案在面对现代前端框架（如 React/Vue 动态生成的类名）或频繁的 UI 重构时极易崩溃。本系统引入了**三层容错选择器**与**网络拦截降级**机制，结合**熔断器**与**令牌桶限速**，实现了企业级的稳定性与合规性。

### 1.1 系统拓扑

系统由四个核心引擎组成，通过 `ScrapeReplyEngine` 进行顶层调度：

```text
用户配置 (YAML)
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│                    ScrapeReplyEngine                         │
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │ ScraperEngine│    │ ReplierEngine│    │    Monitor    │  │
│  │              │    │              │    │  (独立协程)    │  │
│  │ ┌──────────┐ │    │ ┌──────────┐ │    │               │  │
│  │ │Selector  │ │    │ │ LLM 生成 │ │    │  Metrics      │  │
│  │ │Engine    │ │    │ │          │ │    │  Alerter      │  │
│  │ │P1→P2→P3  │ │    │ │HumanGate │ │    │  (钉钉/企微)  │  │
│  │ └──────────┘ │    │ │          │ │    │               │  │
│  │ ┌──────────┐ │    │ │AuditLog  │ │    └───────────────┘  │
│  │ │Network   │ │    │ │(append-  │ │                        │
│  │ │Intercept │ │    │ │only)     │ │    ┌───────────────┐  │
│  │ └──────────┘ │    │ └──────────┘ │    │CircuitBreaker │  │
│  └──────────────┘    └──────────────┘    │+ TokenBucket  │  │
│                                          └───────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 核心设计决策

1. **浏览器实例复用**：单 `BrowserContext` 跨多轮采集复用，避免重复启动开销（每次启动耗时约 800ms–2s）。
2. **网络拦截优先**：利用 Playwright 的 `page.on('response')` 事件驱动机制，实现 $O(1)$ 开销的数据提取，无需 DOM 轮询。
3. **状态持久化**：通过 Session Storage 保持登录态，支持 CDP 模式连接已有浏览器。
4. **人类行为模拟**：注入反检测脚本（消除 `navigator.webdriver`），引入随机延迟与鼠标移动轨迹，降低风控拦截概率。

---

## 2. 核心模块解析

### 2.1 三层容错选择器引擎 (`selector.py`)

UI 变化是自动化系统最大的工程风险。`SelectorEngine` 通过优先级链实现结构性容错，按 $P1 \rightarrow P2 \rightarrow P3$ 顺序降级：

| 层级 | 策略 | 稳定性 | 示例 | 适用场景 |
| :---: | :--- | :---: | :--- | :--- |
| **P1** | 语义属性 | ★★★★★ | `testid="send-btn"`, `aria-label="发送"` | UI 重构时最稳定，前端工程化标准 |
| **P2** | 文本内容 | ★★★★☆ | `text="发送"`, `text_contains="回复"` | 标签改变但文案未变 |
| **P3** | CSS / XPath | ★★☆☆☆ | `css="button.send"` | 传统方案，最脆弱，作为最后 DOM 兜底 |
| **P3+** | 网络拦截 | ★★★★★ | `url_pattern="/api/msg"` | UI 完全重构时的终极防线 |

**网络拦截层 (`NetworkInterceptor`)**：
当所有 DOM 选择器失效时，系统自动降级到网络层。通过监听底层 XHR/Fetch 响应，利用极简 JSONPath（如 `data.list.0.content`）直接提取数据，对 UI 变化完全免疫。

### 2.2 熔断器与合规限速 (`circuit_breaker.py`)

为防止雪崩效应并遵守目标平台的访问频率限制，系统实现了结合令牌桶（Token Bucket）的熔断器状态机。

* **令牌桶限速**：精确控制 QPS。`acquire()` 操作为纯数学计算，时间复杂度 $O(1)$，无锁设计保证高并发下的性能。
* **三态熔断器**：
  * **CLOSED**：正常通行，受令牌桶限速。
  * **OPEN**：连续失败达到阈值（默认 5 次）触发，拒绝所有请求，并异步触发人工告警（钉钉/企微）。
  * **HALF_OPEN**：等待恢复时间（默认 30s）后，放行一个探测请求。成功则恢复 CLOSED，失败则退回 OPEN。

### 2.3 智能回复与人工确认门 (`replier.py`)

`ReplierEngine` 负责将采集到的数据转化为回复并提交，其核心在于**安全与审计**。

1. **LLM 生成**：兼容 OpenAI API 规范，支持本地 Ollama（如 `qwen2.5:7b`）或云端大模型。
2. **内容安全过滤**：执行本地禁止词表检查，拦截敏感词汇。
3. **HumanGate（人工确认门）**：
   * 默认 `auto_reply: false`，所有写操作必须经过人工确认。
   * 支持 CLI 交互（`y/N`）或异步回调（集成至 Web UI）。
   * 设有超时机制（默认 60s），超时未确认则自动跳过。
4. **不可变审计日志 (`AuditLog`)**：所有操作（生成、拦截、人工审核、发送）均以 Append-only JSONL 格式落盘，确保完整可追溯。

---

## 3. 配置指南 (`example.yaml`)

系统完全由配置驱动，无需修改 Python 代码即可适配新后台。以下为核心配置项说明：

```yaml
scraper:
  target_url: "https://your-backend.example.com/messages"
  headless: true
  session_file: ".session.json"
  min_interval_s: 2.0
  max_interval_s: 5.0
  breaker:
    failure_threshold: 5
    recovery_s: 30.0
    qps: 0.5

replier:
  llm_base_url: "http://localhost:11434/v1"
  llm_model: "qwen2.5:7b"
  auto_reply: false
  forbidden_words: ["退款", "投诉"]
  audit_log_file: "logs/audit.jsonl"
  input_selector:
    aria_label: "回复内容"
    css: "textarea.reply-input"
  submit_selector:
    testid: "submit-reply"

task:
  items:
    - name: "latest_message"
      aria_label: "最新消息"
      css: ".message-content:last-child"
  network_fallback:
    url_pattern: "/api/messages"
    json_path: "data.list"
```

---

## 4. 快速开始与使用示例

### 4.1 环境准备

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器内核
playwright install chromium
```

### 4.2 首次运行（获取登录态）

首次运行建议关闭无头模式，手动完成登录，系统会自动保存 Session：

```bash
# 修改 config.yaml 中 headless: false
python -m core.engine --config config/my_project.yaml --once
```

### 4.3 生产环境运行

```bash
# 持续后台运行
python -m core.engine --config config/my_project.yaml

# 开启 DEBUG 日志
python -m core.engine --config config/my_project.yaml --log-level DEBUG
```

### 4.4 代码集成示例

若需将引擎集成到现有异步框架中：

```python
import asyncio
from core.engine import ScrapeReplyEngine, EngineConfig

async def main():
    # 加载配置
    cfg = EngineConfig.from_yaml("config/my_project.yaml")
    
    # 使用异步上下文管理器管理生命周期
    async with ScrapeReplyEngine(cfg) as engine:
        # 执行单次采集与回复
        result = await engine.run_once()
        print(f"采集状态: {result.source}, 降级层级: {result.tier_used}")
        
        # 或持续运行
        # await engine.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 5. 性能与测试指标

* **时间复杂度**：
  * 令牌桶 `acquire()`: $O(1)$
  * 选择器 `locate()`: $O(k)$，其中 $k$ 为候选选择器数量（通常 $\le 5$）
  * 网络拦截: $O(1)$ 事件驱动
* **测试覆盖率**：核心模块单元测试 23/23 全部通过，覆盖边界条件与状态机转换。
* **资源消耗**：单浏览器实例复用，内存占用稳定在 150MB-300MB 之间。

---

## 6. 安全声明

本系统设计遵循严格的合规原则：
1. 默认开启人工确认（Human-in-the-loop）。
2. 内置速率限制，避免对目标系统造成拒绝服务攻击。
3. 强制审计日志记录。
4. **使用者须自行确保遵守目标平台的服务条款（Terms of Service）及相关法律法规。**
