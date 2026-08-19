# Low-resource runtime profile

RCclaw is a large multi-channel gateway. A constrained deployment should not start every channel, browser integration, vector store or model feature by default. This document defines a **software-only** starting profile using capabilities that already exist in the repository; it does not claim a universal RAM or latency figure.

## 1. Start with the smallest gateway surface

The repository already provides a development gateway command that disables channel initialization:

```bash
pnpm gateway:dev
# equivalent environment boundary used by the script:
# OPENCLAW_SKIP_CHANNELS=1 CLAWDBOT_SKIP_CHANNELS=1
```

Use this profile first when validating a local model endpoint, a single integration or basic gateway behavior. Enable one channel or extension at a time only after the core path is measured. This reduces resident listeners, token refresh work and plugin initialization compared with a broad multi-channel deployment.

## 2. Preserve measurable budgets

The package exposes the following checks. They are guardrails, not a substitute for deployment-specific measurement.

| Command | Purpose | Resource claim it can support |
|---|---|---|
| `node scripts/check-lite-profile.mjs` | Verifies that the low-resource commands and budget checks remain declared in `package.json`. | Documentation/configuration integrity only. |
| `pnpm test:startup:memory` | Runs the repository's CLI startup-memory check. | Startup-memory regression signal for the tested environment. |
| `pnpm test:perf:budget` | Runs existing performance-budget checks. | Regression signal for the configured benchmark. |
| `pnpm test:perf:hotspots` | Runs existing hotspot checks. | Evidence about checked code paths, not whole-system latency. |
| `pnpm check:loc` | Enforces the configured TypeScript file-size limit. | A maintainability guardrail against monolithic source growth. |

Run the zero-dependency profile check before dependency installation:

```bash
node scripts/check-lite-profile.mjs
```

Then record the exact revision, enabled channels, model provider, context window, hardware, process RSS and request latency for any deployment claim. Do not compare model systems only by their parameter count or assume that a newer machine makes an unbounded configuration acceptable.

## 3. Capability budget

| Layer | Lite default | Escalation trigger |
|---|---|---|
| Channels | None during core validation. | A single required user-facing channel has passed its own smoke test. |
| Models | One already available provider or endpoint. | Task quality and measured context/token budget demonstrate a need for a second route. |
| Browser / voice / vision | Disabled unless the user request requires it. | A capability-specific test and explicit operational approval exist. |
| Retrieval | Disabled for short-lived tasks. | Evaluation shows retrieval improves the target task enough to justify index and embedding costs. |
| Diagnostics | Budget checks, targeted logs and explicit measurements. | More telemetry only when it changes an engineering decision. |

## 4. Maintainer rule

A feature belongs in the default profile only when it has a measurable benefit for the target workload and an independently disableable path. More integrations are not automatically more capable; a reliable minimal path is the baseline from which complexity must earn its place.
