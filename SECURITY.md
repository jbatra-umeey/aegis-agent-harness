# Security model

Aegis is a local reference lab using synthetic evidence and synthetic actions. Its policy boundary is deterministic application code. Model instructions and the critic are not authorization mechanisms.

## Trust boundaries and tested controls

| Threat | Implemented boundary | Evidence |
| --- | --- | --- |
| Forged tenant or identity | Server-side bearer-token mapping; tenant fields forbidden in request schemas; tenant checked before checkpoint lookup | API and cross-tenant tests |
| Tool privilege escalation | Exact per-agent tool allowlist; empty arguments only; no shell or arbitrary HTTP tool | Compromised agent requesting `shell_exec` is blocked before observation |
| Prompt injection in evidence | Runbook stays untrusted data; model has no approval capability; tool and final evidence policies are outside the model | Injection fixture + separate compromised-tool test |
| Fabricated citations | Exact specialist evidence IDs, trusted snapshot content and source validation | Forged synthesis citations fail even when the critic accepts |
| Stale evidence | Snapshot age plus elapsed run time must be under one hour; checked again before approved simulation | Stale-evidence trajectory |
| Approval replay / confusion | Tenant + operator role + exact proposal digest + expiry + atomic state claim | Wrong identity, digest, expiry and concurrent-replay tests |
| Budget races | SQLite conditional updates reserve per-attempt units and call count before network access | Parallel fan-out and constrained-budget tests |
| Provider error leakage | Stable error codes; no response bodies or automatic exception recording in traces | Mock provider's private error text never enters run output |
| Secret leakage at ingress | Narrow secret recognizers reject known patterns; emails are redacted; validation errors do not echo request bodies | Ingress and API tests |
| Cached cross-tenant output | Cache keys include server-resolved tenant, context, schema, tool, policy and prompt versions | Warm tenant hit + cold second tenant |
| Hostile dashboard content | `textContent` rendering; no model HTML; restrictive CSP; no external frontend assets | Browser test injects an HTML event handler as model text |

The injection fixture demonstrates routing and boundaries under a known payload. It does **not** measure a real model's general resistance to prompt injection. The independent compromised-tool test is the stronger capability-boundary check.

## Credentials and export

Fixture identities are deliberately public demo credentials. The default server binds to loopback. Proxy mode rejects demo credentials and requires server-configured tokens at least 24 characters long; generate random tokens, because length alone does not establish entropy. Bearer credentials stay in browser memory, not local storage. A page reload returns to the default fixture identity or requires the custom credential again.

Provider credentials are used only by the HTTP adapter. The adapter permits HTTPS, with explicit HTTP exceptions for loopback and a Docker service named `gateway`; it disables redirects and ambient HTTP proxy settings. The endpoint is administrator configuration, never model input. It is not a comprehensive DNS-rebinding or network egress firewall.

Local run state includes sanitized goals and model summaries, plus synthetic tool evidence. Local spans contain explicit metadata and token counts, not prompts or raw exceptions. Optional OTLP export transmits these spans to your configured endpoint. Optional LangSmith export sends root-run summaries with run/trace identifiers, outcome and timing; automatic LangSmith state tracing is explicitly disabled around graph execution. The sample integrations have no enabled export destination by default.

## Known limitations

- A shared process is not a hostile-code sandbox. The demo exposes no arbitrary-code tool.
- SQLite files are not encrypted by the app; anyone who can read or modify the data directory crosses the local trust boundary. Run under a dedicated account with restrictive file permissions.
- The public fixture tokens, lack of OIDC, process-local concurrency/circuit state, and unbounded audit retention make this unsuitable as an internet-facing production service.
- The cache is bounded, but runs/checkpoints/audit events need a production retention policy and encrypted backups.
- The local ledger and graph checkpointer are separate transactional stores. A crash during an approval handoff requires reconciliation; there is no distributed transaction or durable outbox.
- Static bearer authentication has no built-in rotation, revocation service or login flow. Ingress has host/origin/body checks, but no distributed per-tenant rate limiter.
- Regex redaction is narrow and cannot guarantee recognition of every secret or personal datum. Add a data-classification and export policy for real evidence.
- A synthesis summary is model text. Its citations and action preconditions are checked; the semantic truth of every sentence is not proved.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting if enabled for this repository. Otherwise contact the maintainer privately before disclosing reproducible security details. Never put credentials, private prompts or production logs in a public issue.

This design is informed by [OWASP's agentic threat guidance](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/). It is not an assertion of formal compliance or a completed penetration test.
