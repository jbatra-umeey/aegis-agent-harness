# Evaluation card

The committed baseline is [eval-results.json](eval-results.json): **17/17 deterministic trajectory evaluations passed**. The backend suite contains **34 passing tests**. These measure application boundaries and control flow; they are not accuracy or safety rates for an LLM population.

## Dataset and oracles

All inputs are synthetic, versioned in `aegis/providers.py`, and contain no employer data. The use case is a mobile editor release whose p95 latency is 420 ms against a 300 ms target. The healthy variant is 240 ms. The local canary returns a fixed synthetic 280 ms result. These numbers are fixture parameters, not measurements from a real product.

| Scenario | Required outcome |
| --- | --- |
| Regression | Complete three specialist findings; wait for approval |
| Healthy | Complete without asking to execute |
| Injected runbook instruction | Retain the normal evidence/approval boundary |
| Missing deployment evidence | Stop before synthesis releases an action |
| Stale performance evidence | Deterministic policy blocks the proposal |
| Economy route failure | Bounded fallback reaches a usable review |
| All routes unavailable | Stop after bounded provider attempts |
| Forbidden tool | Reject the request before tool execution |
| Malformed model output | Reject strict schema violation without fallback |
| One critic revision | Revise once, then reach approval |
| Critic loop | Stop after the second critique requests revision |
| Exact response cache | Cold 9 attempts; warm 0 attempts / 9 cache hits |
| Tenant cache isolation | Identical other-tenant request remains cold |
| Approved simulation | One local action; completed outcome |
| Declined simulation | No action; declined outcome |
| Parallel budget | No overshoot even with concurrent specialists |
| Durable restart | Close and reopen the harness, then resume the native interrupt |

## Additional verification

`tests/test_harness.py` exercises the identity/approval boundaries, concurrency, cache expiry, context/route separation, actual overlapping specialist spans, trace parentage, secret handling, critic bypass resistance, proxy authentication failure behavior, 429/503 failover, provider payload and cached-token normalization, circuit breaking, and the LangSmith summary contract.

`tests/browser-smoke.cjs` checks the complete control-room flow at desktop and 390px mobile widths, including safe rendering of hostile model text. It starts an isolated fixture server. Credentials are not persisted; the reload check explicitly selects the same tenant before loading its history.

```bash
python -m pytest -q
python -m aegis eval --output artifacts/eval-report.json
npm run test:browser
```

Run Ruff separately as documented in the README. The GitHub Actions job executes these gates and uploads the new evaluation report and browser screenshots.

## What was and was not measured

| Area | Status |
| --- | --- |
| Native LangGraph branching/checkpoints/interrupts | Executed locally |
| SQLite cache, budgets and approval transactions | Executed locally |
| OpenTelemetry traces and metrics SDK | Executed locally |
| LiteLLM-compatible HTTP payloads and fault responses | Mock transport verified |
| LangSmith summary export calls and omission of prompts | Mock client verified |
| Remote OTLP collector / LangSmith delivery | Not exercised |
| Live model quality, cost, prefix-cache hit rates | Not exercised |
| Docker recipe and collector/proxy example deployment | Configuration supplied; not executed in the authoring environment |
| Production scale, availability, penetration resistance | Not measured |

## Moving to live model evaluations

Keep the deterministic suite as a release gate. Add a separately approved dataset of real tasks, human-labeled citation correctness, action suitability, abstention and rollback decisions. Run each selected model multiple times; record model and prompt versions, latency, token usage, dollar cost from the gateway, and provider-reported prefix-cache reads. Separate model quality from harness failures.

Review adversarial cases where tool content claims authority, cites forged IDs, asks to exfiltrate data, or causes an excessive revision loop. Combine deterministic assertions with human review. An LLM judge can assist analysis, but must not replace permission checks or become the only release gate. Upload only classified, redacted examples to a remote evaluation platform.
