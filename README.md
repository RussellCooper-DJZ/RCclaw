# RCclaw

> **Enterprise-grade AI Agent Framework with Multi-channel Gateway.**
> Built by [RussellCooper](https://github.com/RussellCooper-DJZ) · Based on [openclaw](https://github.com/openclaw/openclaw) (MIT)

RCclaw is a hardened, performance-optimized fork of openclaw, evolved into a full-fledged enterprise AI agent framework. It connects any LLM to 20+ messaging platforms while providing advanced autonomous capabilities including Human-in-the-Loop (HITL) workflows, multi-modal vision, stealth automation, and optimized RAG pipelines.

## 🌟 Core Capabilities (New in RCclaw)

RCclaw introduces four enterprise-grade AI capabilities in the `python/core/` module:

### 1. LangGraph Agent Engine (`langgraph_engine.py`)
- **Architecture:** `StateGraph`-based execution with Plan → Execute → Synthesize nodes.
- **HITL (Human-in-the-Loop):** Asynchronous interrupt/resume mechanism via `interrupt_before`.
- **Integration:** Dify Workflow API compatible.

### 2. Multi-modal Vision Engine (`vision_engine.py`)
- **Dual-layer Detection:** pHash perceptual hashing for fast filtering (Hamming distance threshold 10) + VLM (MiniCPM-V) for semantic diffing.
- **OCR Integration:** PaddleOCR for text region extraction before semantic comparison.
- **Use Case:** Reliable UI change detection that distinguishes between "layout refactoring" and "data updates".

### 3. Stealth Automation Engine (`automation_engine.py`)
- **Anti-bot Bypass:** Playwright with `navigator.webdriver = undefined` injection.
- **Human Simulation:** Randomized delays, mouse trajectory simulation, and persistent `browser_context`.
- **Resilience:** Multi-level fallback selectors (CSS → XPath → Text Match → OCR).

### 4. Enhanced RAG Pipeline (`rag_engine_enhanced.py`)
- **4-Layer Optimization:** 
  1. Semantic Chunking (sentence boundary based)
  2. HyDE (Hypothetical Document Embeddings)
  3. Hybrid Search (Vector 0.7 + BM25 0.3)
  4. MMR (Maximal Marginal Relevance) Reranking + Context Compression.

## ⚡ Performance & Security (vs openclaw)

| Area | openclaw | RCclaw |
|---|---|---|
| Queue operations | O(n) `shift()`/`splice()` | O(1) ring-buffer via `copyWithin` |
| Secret normalization | O(n) char loop | Single-pass regex, ~3-5× faster |
| Concurrency scheduler | Dynamic array growth | Pre-allocated worker-pool pattern |
| Hot-path tokenizers | `/\s/.test(ch)` per char | `charCodeAt(0) < 33` branchless |
| ReDoS protection | Basic | Full CWE-1333 threat model + docs |
| Timing-attack defence | `timingSafeEqual` | SHA-256 normalisation + CWE-208 docs |
| WebSocket CSRF | Origin check | 3-path acceptance matrix + DNS-rebinding note |
| Resource exhaustion | `Promise.race()` timeout | `AbortController` (cancels TCP socket) |

## 🚀 Quick Start

```bash
# Clone the repository
git clone https://github.com/RussellCooper-DJZ/RCclaw.git
cd RCclaw

# Install Node.js dependencies (Gateway)
pnpm install

# Install Python dependencies (AI Core)
pip install -r python/requirements.txt

# Run tests
pytest python/tests/
```

## 🧠 Skills

RCclaw ships with a curated set of AI agent skills in `.agents/skills/`:

| Skill | Description |
|---|---|
| `headless-scrape-reply` | **(New)** Headless scraping and automated replying with circuit breakers and multi-level selectors. |
| `parallels-discord-roundtrip` | macOS Parallels smoke harness with Discord E2E roundtrip |
| `ai-org-digital-workforce` | Enterprise Human-AI collaboration governance framework |
| `bangkok-ecommerce-llm` | Multi-modal cross-border e-commerce localization engine |
| `ecommerce-multiagent` | Full-cycle e-commerce multi-agent automation system |
| `github-pages-site-enhancer` | Diagnose, repair and enhance pre-built GitHub Pages sites |
| `industrial-vision-patent` | Industrial anti-glare vision algorithm + patent workflow |
| `portfolio-site-promo` | Personal brand & multi-platform marketing automation |

## 📄 License

MIT — Copyright (c) 2026 RussellCooper (RCclaw)

Based on openclaw — Copyright (c) 2025 Peter Steinberger (MIT)
See [LICENSE](./LICENSE) for full terms.
