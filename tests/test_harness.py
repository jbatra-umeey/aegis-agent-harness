import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.contracts import ApprovalRequest, Plan, PolicyError, Principal, StartRequest
from aegis.evals import EXPECTED
from aegis.harness import Harness
from aegis.providers import FixtureProvider, LiteLLMProvider, Reply
from aegis.server import create_app
from aegis.telemetry import LangSmithSummaryExporter

OPERATOR = Principal(tenant="atlas", subject="alice", role="operator")


@pytest.mark.parametrize("scenario,expected", EXPECTED.items())
async def test_adversarial_trajectories(tmp_path, scenario, expected):
    async with Harness.open(tmp_path) as h:
        result = await h.start(OPERATOR, StartRequest(scenario=scenario))
        assert (result["status"], result["error"]) == expected
        assert result["calls"] <= 20 and result["units"] <= 32
        assert result["result"] is None
        if scenario == "critic_loop":
            assert sum(e["kind"] == "proposal" for e in result["events"]) == 2
        if scenario == "forbidden_tool":
            assert not any(
                e["agent"] == "risk" and e["kind"] == "observation" for e in result["events"]
            )


async def test_native_interrupt_survives_restart_and_blocks_replay(tmp_path):
    async with Harness.open(tmp_path) as h:
        pending = await h.start(OPERATOR, StartRequest())
        snapshot = await h.graph.aget_state(h.config(pending["id"]))
        assert snapshot.tasks[0].interrupts
    async with Harness.open(tmp_path) as h:
        approval = ApprovalRequest(approved=True, proposal_digest=pending["proposal_digest"])
        completed = await h.approve(OPERATOR, pending["id"], approval)
        assert completed["status"] == "completed"
        assert completed["result"]["simulation"] is True
        with pytest.raises(PolicyError, match="approval_conflict"):
            await h.approve(OPERATOR, pending["id"], approval)
        with h.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["role", "tenant", "digest", "expired"])
async def test_approval_is_bound_to_identity_proposal_and_time(tmp_path, failure):
    async with Harness.open(tmp_path) as h:
        pending = await h.start(OPERATOR, StartRequest())
        principal = OPERATOR
        proposal_digest = pending["proposal_digest"]
        if failure == "role":
            principal = principal.model_copy(update={"role": "analyst"})
        if failure == "tenant":
            principal = principal.model_copy(update={"tenant": "orion"})
        if failure == "digest":
            proposal_digest = "0" * 64
        if failure == "expired":
            with h.store.db() as db:
                db.execute("UPDATE runs SET approval_expires=0")
        with pytest.raises(PolicyError):
            await h.approve(
                principal,
                pending["id"],
                ApprovalRequest(approved=True, proposal_digest=proposal_digest),
            )
        with h.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


async def test_concurrent_approval_has_one_winner(tmp_path):
    async with Harness.open(tmp_path) as h:
        run = await h.start(OPERATOR, StartRequest())
        request = ApprovalRequest(approved=True, proposal_digest=run["proposal_digest"])
        results = await asyncio.gather(
            *(h.approve(OPERATOR, run["id"], request) for _ in range(4)), return_exceptions=True
        )
        assert sum(isinstance(x, dict) and x["status"] == "completed" for x in results) == 1
        assert sum(isinstance(x, PolicyError) for x in results) == 3


async def test_deny_does_not_execute(tmp_path):
    async with Harness.open(tmp_path) as h:
        run = await h.start(OPERATOR, StartRequest())
        result = await h.approve(
            OPERATOR,
            run["id"],
            ApprovalRequest(approved=False, proposal_digest=run["proposal_digest"]),
        )
        assert result["status"] == "declined" and result["result"] is None


async def test_cache_reuse_isolation_expiry_and_context(tmp_path):
    async with Harness.open(tmp_path) as h:
        cold = await h.start(OPERATOR, StartRequest())
        warm = await h.start(OPERATOR, StartRequest())
        other = await h.start(OPERATOR.model_copy(update={"tenant": "orion"}), StartRequest())
        assert (cold["calls"], warm["calls"], warm["cache_hits"], other["calls"]) == (9, 0, 9, 9)
        assert warm["provider_cached_tokens"] == 0
        with pytest.raises(PolicyError, match="run_not_found"):
            await h.get(OPERATOR, other["id"])
        with h.store.db() as db:
            db.execute("UPDATE cache SET expires=0")
        expired = await h.start(OPERATOR, StartRequest())
        changed = await h.start(OPERATOR, StartRequest(goal="Assess another release."))
        assert expired["calls"] == 9 and changed["calls"] > 0
        key = h.gateway.key("atlas", "coordinator", {"x": 1}, Plan, "reasoning", "regression")
        assert key != h.gateway.key(
            "atlas", "coordinator", {"x": 2}, Plan, "reasoning", "regression"
        )
        assert key != h.gateway.key(
            "atlas", "coordinator", {"x": 1}, Plan, "fallback", "regression"
        )


async def test_same_key_singleflight_and_parallel_budget(tmp_path):
    async with Harness.open(tmp_path) as h:
        results = await asyncio.gather(*(h.start(OPERATOR, StartRequest()) for _ in range(3)))
        assert sum(r["calls"] for r in results) == 9
        assert all(r["status"] == "awaiting_approval" for r in results)
        limited = await h.start(OPERATOR, StartRequest(max_calls=2, cache=False))
        assert limited["status"] == "blocked" and limited["calls"] == 2
        assert "budget_exhausted" in limited["error"]
        units = await h.start(OPERATOR, StartRequest(max_units=3, cache=False))
        assert units["units"] <= 3 and units["status"] == "blocked"


async def test_tools_really_fan_out_and_spans_have_parents(tmp_path):
    async with Harness.open(tmp_path) as h:
        run = await h.start(OPERATOR, StartRequest())
        spans = [e["payload"] for e in run["events"] if e["kind"] == "span"]
        workers = [s for s in spans if s["name"] == "agent.specialist"]
        assert len(workers) == 3
        assert max(s["start_ns"] for s in workers) < min(s["end_ns"] for s in workers)
        assert all(s["parent_id"] for s in workers)
        assert len({s["trace_id"] for s in spans}) == 1
        assert "aegis.gateway.requests" in json.dumps(h.telemetry.metrics())


async def test_ingress_secrets_rejected_email_redacted_and_no_prompt_spans(tmp_path):
    async with Harness.open(tmp_path) as h:
        with pytest.raises(PolicyError, match="secret_in_goal"):
            await h.start(OPERATOR, StartRequest(goal="use sk-thisisafakecredential123"))
        assert not h.store.list(OPERATOR.tenant)
        run = await h.start(
            OPERATOR, StartRequest(goal="Review for alice@example.test private-phrase-42")
        )
        assert "alice@example.test" not in json.dumps(run)
        spans = [e for e in run["events"] if e["kind"] == "span"]
        assert "private-phrase-42" not in json.dumps(spans)


class ForgedEvidenceProvider(FixtureProvider):
    async def complete(self, role, context, schema, route, scenario):
        reply = await super().complete(role, context, schema, route, scenario)
        if role == "synthesis":
            output = json.loads(reply.content)
            output["citations"] = ["forged:evidence"]
            return Reply(json.dumps(output))
        return reply


async def test_critic_acceptance_cannot_override_evidence_policy(tmp_path):
    async with Harness.open(tmp_path, ForgedEvidenceProvider()) as h:
        run = await h.start(OPERATOR, StartRequest())
        assert run["error"] == "proposal_citations_invalid" and run["status"] == "blocked"


@pytest.mark.parametrize(
    "status,expected_calls,error",
    [
        (401, 1, "provider_rejected_request"),
        (503, 2, "all_routes_unavailable"),
        (429, 2, "all_routes_unavailable"),
    ],
)
async def test_proxy_fallback_only_on_transient_failures(tmp_path, status, expected_calls, error):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"private_provider_error": "do-not-export"})

    provider = LiteLLMProvider(
        "http://localhost:4000", "test-only", transport=httpx.MockTransport(handler)
    )
    async with Harness.open(tmp_path, provider) as h:
        run = await h.start(OPERATOR, StartRequest(cache=False))
        assert run["calls"] == expected_calls == len(calls)
        assert run["error"] == error
        assert "do-not-export" not in json.dumps(run)


async def test_proxy_payload_usage_and_prefix_cache_contract(tmp_path):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-only"
        assert request.headers.get("traceparent")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "specialists": ["performance", "change", "risk"],
                                    "rationale": "Inspect release.",
                                }
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 2100,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 1800},
                },
            },
        )

    provider = LiteLLMProvider(
        "http://localhost:4000",
        "test-only",
        prefix_cache=True,
        transport=httpx.MockTransport(handler),
    )
    async with Harness.open(tmp_path, provider) as h:
        request = StartRequest()
        h.store.create("contract-test", "atlas", request)
        run = {"id": "contract-test", "tenant": "atlas", "scenario": "regression", "cache": False}
        await h.gateway.call(run, "coordinator", {"goal": "Review."}, Plan)
        assert captured[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert captured[0]["model"] == "aegis-reasoning"
        usage = [e for e in h.store.events("contract-test", "atlas") if e["kind"] == "model_usage"][
            0
        ]["payload"]
        assert usage["provider_cached_tokens"] == 1800


async def test_circuit_breaker_skips_open_routes(tmp_path):
    async with Harness.open(tmp_path) as h:
        first = await h.start(OPERATOR, StartRequest(scenario="outage", cache=False))
        second = await h.start(OPERATOR, StartRequest(scenario="outage", cache=False))
        third = await h.start(OPERATOR, StartRequest(scenario="outage", cache=False))
        assert (first["calls"], second["calls"], third["calls"]) == (2, 2, 0)
        assert any(e["kind"] == "circuit_open" for e in third["events"])


def test_api_auth_tenant_controls_body_and_secret_errors(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/runs").status_code == 401
        headers = {"Authorization": "Bearer demo-operator-atlas"}
        assert (
            client.post("/api/runs", headers=headers, json={"tenant": "orion"}).status_code == 422
        )
        invalid = client.post(
            "/api/runs", headers=headers, json={"goal": "sk-fakecredential123456"}
        )
        assert invalid.status_code == 403 and "sk-fakecredential" not in invalid.text
        assert (
            client.post(
                "/api/runs", headers={**headers, "Origin": "https://evil.example"}, json={}
            ).status_code
            == 403
        )
        assert client.post("/api/runs", headers=headers, content="x" * 17000).status_code == 413
        pending = client.post("/api/runs", headers=headers, json={}).json()
        assert pending["status"] == "awaiting_approval"
        assert (
            client.get(
                "/api/runs/" + pending["id"],
                headers={"Authorization": "Bearer demo-operator-orion"},
            ).status_code
            == 404
        )
        request = {"approved": True, "proposal_digest": pending["proposal_digest"]}
        assert (
            client.post(
                f"/api/runs/{pending['id']}/approval",
                headers={"Authorization": "Bearer demo-auditor-atlas"},
                json=request,
            ).status_code
            == 403
        )
        done = client.post(f"/api/runs/{pending['id']}/approval", headers=headers, json=request)
        assert done.json()["status"] == "completed"
        assert "frame-ancestors 'none'" in done.headers["content-security-policy"]


def test_proxy_mode_rejects_demo_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AEGIS_MODE", "proxy")
    monkeypatch.delenv("AEGIS_IDENTITIES_JSON", raising=False)
    with pytest.raises(ValueError, match="server-configured"):
        create_app(tmp_path)


@pytest.mark.parametrize(
    "url", ["http://evil.example", "https://user:pass@example.com", "https://example.com?token=x"]
)
def test_gateway_rejects_unsafe_destinations(url):
    with pytest.raises(ValueError):
        LiteLLMProvider(url, "not-a-real-key")


async def test_langsmith_summary_excludes_prompts(tmp_path):
    client = Mock()
    async with Harness.open(tmp_path) as h:
        h.telemetry.provider.add_span_processor(
            __import__(
                "opentelemetry.sdk.trace.export", fromlist=["SimpleSpanProcessor"]
            ).SimpleSpanProcessor(LangSmithSummaryExporter(client))
        )
        await h.start(OPERATOR, StartRequest(goal="private-goal-never-export"))
        assert client.create_run.call_count == 1
        args = client.create_run.call_args.kwargs
        assert args["inputs"] == {} and "private-goal" not in str(client.mock_calls)
        assert args["extra"]["metadata"]["export_mode"] == "metadata_only"
