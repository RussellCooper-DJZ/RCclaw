# headless-scrape-reply

> **无 API、界面常变的软件后台自动采集与回复 Skill**
> Author: RussellCooper | License: MIT

---

## 一、解决的核心问题

当目标系统**没有开放 API**、**后台界面频繁变化**时，传统自动化方案（固定 CSS 选择器）极易因 UI 重构而崩溃。本 Skill 通过**三层容错选择器 + 网络拦截降级**，实现对 UI 变化的结构性免疫，并内置**令牌桶限速 + 熔断器 + 人工确认门**，确保合规与稳定。

---

## 二、系统架构

```
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

---

## 三、三层容错选择器（核心创新）

UI 变化是自动化系统最大的工程风险。本 Skill 通过优先级链实现结构性容错：

| 层级 | 策略 | 稳定性 | 示例 |
| :---: | :--- | :---: | :--- |
| **P1** | 语义属性（`data-testid` / `aria-label` / `role`） | ★★★★★ | `testid="send-btn"` |
| **P2** | 文本内容（`:has-text()` / `get_by_text()`） | ★★★★☆ | `text="发送"` |
| **P3** | CSS / XPath | ★★☆☆☆ | `css="button.send"` |
| **P3+** | **网络拦截**（`page.on('response')`） | ★★★★★ | `url_pattern="/api/msg"` |

**P3+ 网络拦截**是对 UI 完全免疫的终极防线：直接从 XHR/Fetch 响应体提取数据，不依赖任何 DOM 结构。

---

## 四、熔断器状态机

```
              失败 ≥ threshold
  CLOSED ──────────────────────▶ OPEN
    ▲                              │
    │ 成功 ≥ success_threshold     │ 等待 recovery_s
    │                              ▼
    └──────────────────────── HALF_OPEN
                 (放行一个探测请求)
```

- **CLOSED**：正常运行，令牌桶限速
- **OPEN**：拒绝所有请求，触发人工告警
- **HALF_OPEN**：放行一个探测请求，成功则恢复，失败则重新 OPEN

---

## 五、合规设计（不可绕过的安全约束）

1. **令牌桶限速**：精确控制 QPS，默认每 2 秒最多 1 次请求，对目标系统友好
2. **人工确认门（HumanGate）**：所有回复操作默认需要人工确认（`auto_reply: false`）
3. **内容安全过滤**：禁止词表过滤，可扩展为向量相似度检测
4. **审计日志（append-only）**：每次操作写入 JSONL 文件，不可修改，完整可追溯
5. **人类行为模拟**：随机化操作间隔（2–5s），逐字符输入（30ms/字），降低被检测风险

---

## 六、快速开始

### 1. 安装依赖

```bash
pip install -r .agents/skills/headless-scrape-reply/requirements.txt
playwright install chromium
```

### 2. 配置

```bash
cp .agents/skills/headless-scrape-reply/config/example.yaml config/my_project.yaml
# 编辑 config/my_project.yaml，填写 target_url 和 task.items
```

### 3. 首次登录（获取 session）

```bash
# 以有头模式运行，手动登录后 session 自动保存
python -m core.engine --config config/my_project.yaml --once
# 修改 config: headless: false，运行后手动登录
```

### 4. 生产运行

```bash
# 持续运行（后台）
python -m core.engine --config config/my_project.yaml

# 单次运行（测试）
python -m core.engine --config config/my_project.yaml --once

# 调试模式
python -m core.engine --config config/my_project.yaml --log-level DEBUG
```

---

## 七、代码结构

```
headless-scrape-reply/
├── SKILL.md              # 本文档
├── requirements.txt      # 最小化依赖（6 个包）
├── pytest.ini
├── core/
│   ├── __init__.py       # 公共接口导出
│   ├── selector.py       # 三层容错选择器引擎 + 网络拦截
│   ├── circuit_breaker.py# 令牌桶 + 熔断器状态机
│   ├── scraper.py        # 无头采集引擎（CDP 模式）
│   ├── replier.py        # 智能回复引擎（LLM + HumanGate + AuditLog）
│   ├── monitor.py        # 实时监控 + 多渠道告警
│   └── engine.py         # 主调度引擎（顶层入口）
├── config/
│   └── example.yaml      # 完整配置模板（带详细注释）
└── tests/
    └── test_core.py      # 23 个单元测试（全部通过）
```

---

## 八、扩展指南

### 适配新的后台系统

只需修改 `config/my_project.yaml` 中的 `task.items` 和选择器配置，**无需修改任何 Python 代码**：

```yaml
task:
  items:
    - name: "order_status"
      testid: "order-status-cell"   # 优先使用 data-testid
      text_contains: "待处理"        # 降级到文本匹配
      css: "td.status"              # 最后尝试 CSS
  network_fallback:
    url_pattern: "/api/orders"
    json_path: "data.orders"
```

### 替换 LLM

修改 `replier.llm_base_url` 和 `replier.llm_model` 即可切换到任意 OpenAI 兼容端点：

```yaml
replier:
  llm_base_url: "https://api.openai.com/v1"
  llm_model: "gpt-4o-mini"
  llm_api_key: "sk-..."
```

### 添加告警渠道

实现 `custom_fn` 回调即可接入任意告警系统：

```python
async def my_alert(title: str, message: str) -> None:
    await send_to_slack(title, message)

cfg.alert.custom_fn = my_alert
```

---

## 九、性能指标

| 指标 | 数值 | 说明 |
| :--- | :---: | :--- |
| 令牌桶 `acquire()` | O(1) | 纯数学计算，无锁 |
| 选择器 `locate()` | O(k) | k = 候选选择器数，通常 ≤ 5 |
| 网络拦截 | O(1) | 事件驱动，零轮询 |
| 熔断器 `guard()` | O(1) | 状态检查 + 令牌桶 |
| 浏览器复用 | 节省 ~1s/轮 | 单 Context 跨多轮复用 |
| 测试覆盖 | 23/23 通过 | 1.48s 完成 |

---

## 十、安全声明

本 Skill 设计遵循以下原则，使用者须自行确保合规：

- 默认 `auto_reply: false`，所有写操作需人工确认
- 内置速率限制，避免对目标系统造成负担
- 所有操作写入不可变审计日志
- 使用者应遵守目标平台的服务条款和 robots.txt
- 不得用于任何非法或侵权用途
