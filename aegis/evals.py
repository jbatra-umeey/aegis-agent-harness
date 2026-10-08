"""Deterministic trajectory evaluations, not a claim about live LLM answer quality."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .contracts import SCENARIOS, ApprovalRequest, Principal, StartRequest
from .harness import Harness

EXPECTED = {
    "regression": ("awaiting_approval", None),
    "healthy": ("completed", None),
    "injection": ("awaiting_approval", None),
    "missing": ("blocked", "evidence_missing_or_invalid"),
    "stale": ("blocked", "evidence_stale"),
    "fallback": ("awaiting_approval", None),
    "outage": ("blocked", "all_routes_unavailable"),
    "forbidden_tool": ("blocked", "tool_scope_denied"),
    "malformed": ("blocked", "model_contract_invalid"),
    "revision": ("awaiting_approval", None),
    "critic_loop": ("blocked", "critic_revision_limit"),
}


async def evaluate(output=None):
    principal = Principal(tenant="eval", subject="evaluator", role="operator")
    results = []
    with tempfile.TemporaryDirectory() as root:
        for scenario in SCENARIOS:
            async with Harness.open(Path(root) / scenario) as h:
                run = await h.start(principal, StartRequest(scenario=scenario))
                expected, error = EXPECTED[scenario]
                passed = run["status"] == expected and run["error"] == error
                passed = (
                    passed and run["calls"] <= run["max_calls"] and run["units"] <= run["max_units"]
                )
                if scenario == "injection":
                    passed = passed and all(
                        e["agent"] in {"performance", "change", "risk"}
                        for e in run["events"]
                        if e["kind"] == "observation"
                    )
                if scenario == "fallback":
                    passed = passed and any(e["kind"] == "fallback" for e in run["events"])
                results.append(
                    {
                        "case": scenario,
                        "passed": passed,
                        "status": run["status"],
                        "outbound_attempts": run["calls"],
                        "policy_units": run["units"],
                        "error": run["error"],
                    }
                )
        async with Harness.open(Path(root) / "cache") as h:
            cold = await h.start(principal, StartRequest())
            warm = await h.start(principal, StartRequest())
            other = await h.start(principal.model_copy(update={"tenant": "other"}), StartRequest())
            results.append(
                {
                    "case": "exact_cache",
                    "passed": cold["calls"] == 9 and warm["calls"] == 0,
                    "cold_attempts": cold["calls"],
                    "warm_attempts": warm["calls"],
                    "cache_hits": warm["cache_hits"],
                }
            )
            results.append({"case": "tenant_cache_isolation", "passed": other["calls"] == 9})
            approved = await h.approve(
                principal,
                cold["id"],
                ApprovalRequest(approved=True, proposal_digest=cold["proposal_digest"]),
            )
            results.append(
                {
                    "case": "approved_simulation",
                    "passed": approved["status"] == "completed"
                    and approved["result"]["simulation"] is True,
                }
            )
            declined = await h.approve(
                principal,
                warm["id"],
                ApprovalRequest(approved=False, proposal_digest=warm["proposal_digest"]),
            )
            results.append(
                {
                    "case": "declined_simulation",
                    "passed": declined["status"] == "declined" and declined["result"] is None,
                }
            )
            limited = await h.start(principal, StartRequest(max_calls=2, cache=False))
            results.append(
                {
                    "case": "parallel_budget",
                    "passed": limited["status"] == "blocked"
                    and limited["calls"] == 2
                    and "budget_exhausted" in limited["error"],
                }
            )
        path = Path(root) / "restart"
        async with Harness.open(path) as h:
            pending = await h.start(principal, StartRequest())
        async with Harness.open(path) as h:
            resumed = await h.approve(
                principal,
                pending["id"],
                ApprovalRequest(approved=True, proposal_digest=pending["proposal_digest"]),
            )
            results.append({"case": "durable_restart", "passed": resumed["status"] == "completed"})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "deterministic_fixture",
        "scope": "Control-flow, safety boundaries, fault handling and cache behavior; no live provider quality or latency claims.",
        "passed": sum(x["passed"] for x in results),
        "total": len(results),
        "cases": results,
    }
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")
    return report
