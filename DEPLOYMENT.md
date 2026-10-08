# Deployment and integrations

## Local lab

Python 3.12 is the verified runtime. Install with the committed constraints and start one worker:

```bash
python -m pip install -c requirements.lock '.[dev]'
python -m aegis serve --host 127.0.0.1 --port 8080
```

The UI is packaged with the Python wheel. Data defaults to `.data`; override it with `AEGIS_DATA_DIR`. Preserve both SQLite databases for approval resume. `/healthz` reports service health; `/docs` redirects to the generated OpenAPI JSON schema. The control room and API reference have no external asset dependency.

Demo bearer tokens are `demo-operator-atlas`, `demo-auditor-atlas`, and `demo-operator-orion`. These are fixture-only convenience identities. Do not expose fixture mode publicly.

```bash
curl http://127.0.0.1:8080/api/runs \
  -H 'Authorization: Bearer demo-operator-atlas' \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"regression","max_calls":20,"max_units":32,"cache":true}'
```

To approve, POST `approved` and the returned `proposal_digest` to `/api/runs/{id}/approval` with an operator token. A read or approval from another tenant returns 404. Roles are `analyst` (start/read), `operator` (start/read/approve), and `auditor` (read).

## Container recipe

```bash
docker compose up --build
```

The image runs as UID 10001, installs runtime dependencies in a separate build stage, binds the published port to loopback, drops Linux capabilities, sets a read-only root filesystem and writes only to its data volume and temporary directory. The recipe was not executed in the authoring environment. Pin the base image by a reviewed digest and run an image/dependency scan before deployment. The supplied Compose file runs only the fixture harness.

## LiteLLM proxy adapter

Provision a LiteLLM deployment using its [official gateway guide](https://docs.litellm.ai/docs/proxy/docker_quick_start). The included `litellm.example.yaml` shows the required aliases without committing provider credentials or assuming a particular model ID. Configure models that accept JSON-object responses, a zero temperature and a `max_tokens` output limit, or adapt the provider adapter to your model's contract and rerun its tests.

| Alias | Workload |
| --- | --- |
| `aegis-economy` | Three specialist decision loops |
| `aegis-reasoning` | Coordinator, synthesis and critic |
| `aegis-fallback` | A separately configured fallback deployment |

Use a restricted application virtual key, with permitted models and database-backed rate/spend budgets in the gateway. Aegis's policy units are conservative work limits, not a financial spending cap. The example without a database does not supply virtual-key budget enforcement.

Configure the app environment:

```text
AEGIS_MODE=proxy
AEGIS_IDENTITIES_JSON={"<random-credential-at-least-24-characters>":{"tenant":"atlas","subject":"operator","role":"operator"}}
LITELLM_BASE_URL=https://your-approved-gateway.example/v1
LITELLM_API_KEY=<restricted-application-virtual-key>
```

These values must come from your shell, runtime or secret manager; `.env.example` is a reference, not an automatically loaded file. Do not put actual credentials in a command you intend to share, logs, commits or screenshots. The API credential map and gateway credential are separate.

Proxy mode uses real models for all agent roles. Its tools and execution remain synthetic. Fault scenarios such as forced 503s or malformed responses are controlled by the fixture provider; selecting those names in proxy mode does not inject a fault into the real service. Missing/stale/injected tool evidence variants still apply.

Disable proxy-side retries to retain one retry owner. Verify your LiteLLM/provider settings do not retry internally; otherwise the application attempt budget cannot count every upstream attempt. There is no automatic live call on startup.

## Two cache layers

The exact response cache is active by default and is independently selectable in each run request. It is authorization-sensitive and tenant scoped. Bump prompt/policy versions when changing behavior. Cache entries last 300 seconds and are capped at 1,000.

Set `AEGIS_PROVIDER_PREFIX_CACHE=1` only for a gateway/provider combination that supports the explicit ephemeral system-content cache marker in the [LiteLLM caching documentation](https://docs.litellm.ai/docs/completion/prompt_caching). The default is off. Some providers cache prefixes automatically, have minimum token counts, or impose storage/write charges. The app reports cache-read tokens only from the provider response; it never infers them from a faster response.

## OpenTelemetry

Local spans are always sampled so every demonstration is inspectable. They share parent/child trace IDs and are stored in the run timeline. A resumed run has a new trace correlated by the same run ID. Metrics use low-cardinality role/route labels and stay in the in-memory SDK reader; the demo does not expose a public cross-tenant `/metrics` endpoint.

Set `AEGIS_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces` to enable the OTLP HTTP span exporter. `otel-collector.example.yaml` is a small reference receiver with memory limiting and batching. Run it on a trusted network; add authenticated export to your chosen backend. Production Prometheus metrics export and dashboards are a documented extension, not bundled services.

## LangSmith summaries

```text
AEGIS_LANGSMITH_SUMMARIES=1
LANGSMITH_API_KEY=<configured-secret>
```

The SDK sends one metadata-only summary for each root invocation to project `aegis-harness`, including local run ID, OpenTelemetry trace ID, outcome and duration. A resume is a second root invocation. It does not mirror the full trace tree, model prompts, goals, findings or tool observations. Automatic graph/state tracing is explicitly disabled. The standard LangSmith SDK endpoint settings can be used for a supported workspace. Remote delivery is opt-in and was not exercised in the committed baseline.

## Production rollout

The [architecture document](ARCHITECTURE.md) separates implemented local controls from the proposed OIDC, Postgres, Redis, policy-service, isolated-worker and telemetry-backend deployment. Real tool execution requires a separate adapter, scoped credentials, approval receipts, idempotency and failure reconciliation. Do not replace the simulation with a shell call and assume the same safety properties hold.
