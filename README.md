# RCclaw

> **Multi-channel AI gateway with extensible messaging integrations.**
> Built by [RussellCooper](https://github.com/RussellCooper-DJZ) · Based on [openclaw](https://github.com/openclaw/openclaw) (MIT)

RCclaw is a hardened, performance-optimized fork of openclaw — a personal AI assistant gateway that connects any LLM to 20+ messaging platforms (Telegram, Discord, Slack, WhatsApp, iMessage, and more).

## What's different from openclaw

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
| Code documentation | Minimal | Big-O annotations + threat models on all modules |

## Quick start

```bash
# Install
npm install -g rcclaw   # or: pnpm add -g rcclaw

# Run
rcclaw
```

## Skills

RCclaw ships with a curated set of AI agent skills in `.agents/skills/`:

| Skill | Description |
|---|---|
| `parallels-discord-roundtrip` | macOS Parallels smoke harness with Discord E2E roundtrip |
| `ai-org-digital-workforce` | Enterprise Human-AI collaboration governance framework |
| `bangkok-ecommerce-llm` | Multi-modal cross-border e-commerce localization engine |
| `ecommerce-multiagent` | Full-cycle e-commerce multi-agent automation system |
| `github-pages-site-enhancer` | Diagnose, repair and enhance pre-built GitHub Pages sites |
| `industrial-vision-patent` | Industrial anti-glare vision algorithm + patent workflow |
| `portfolio-site-promo` | Personal brand & multi-platform marketing automation |

## License

MIT — Copyright (c) 2026 RussellCooper (RCclaw)

Based on openclaw — Copyright (c) 2025 Peter Steinberger (MIT)
See [LICENSE](./LICENSE) for full terms.
