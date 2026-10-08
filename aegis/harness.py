import asyncio
import operator
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt
from langsmith import tracing_context

from .contracts import (
    SCENARIOS,
    SPECIALISTS,
    ApprovalRequest,
    Critique,
    Decision,
    GatewayError,
    Plan,
    PolicyError,
    Proposal,
    StartRequest,
)
from .gateway import Gateway
from .providers import FixtureProvider, evidence
from .security import SCOPES, authorize, authorize_tool, digest, guard_goal
from .store import Store
from .telemetry import Telemetry


class State(TypedDict, total=False):
    run: dict
    plan: dict
    findings: Annotated[list[dict], operator.add]
    errors: Annotated[list[str], operator.add]
    proposal: dict
    critique: dict
    revision: int
    outcome: str
    result: dict


class Harness:
    def __init__(self, store, telemetry, provider, saver):
        self.store, self.telemetry, self.provider = store, telemetry, provider
        self.gateway = Gateway(provider, store, telemetry)
        graph = StateGraph(State)
        graph.add_node("coordinator", self.coordinate)
        graph.add_node("specialist", self.specialist)
        graph.add_node("synthesis", self.synthesize)
        graph.add_node("critic", self.critic)
        graph.add_node("policy_gate", self.gate)
        graph.add_node("approval", self.approval)
        graph.add_edge(START, "coordinator")
        graph.add_conditional_edges("coordinator", self.dispatch, ["specialist"])
        graph.add_edge("specialist", "synthesis")
        graph.add_conditional_edges(
            "synthesis",
            lambda s: END if s.get("outcome") == "blocked" else "critic",
            [END, "critic"],
        )
        graph.add_conditional_edges("critic", self.after_critic, ["synthesis", "policy_gate", END])
        graph.add_conditional_edges(
            "policy_gate",
            lambda s: "approval" if s["outcome"] == "awaiting_approval" else END,
            ["approval", END],
        )
        graph.add_edge("approval", END)
        self.graph = graph.compile(checkpointer=saver)

    @classmethod
    @asynccontextmanager
    async def open(cls, directory, provider=None, otlp_endpoint=None, langsmith_client=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        store = Store(directory / "ledger.sqlite")
        telemetry = Telemetry(store, otlp_endpoint, langsmith_client)
        provider = provider or FixtureProvider()
        async with AsyncSqliteSaver.from_conn_string(
            str(directory / "checkpoints.sqlite")
        ) as saver:
            instance = cls(store, telemetry, provider, saver)
            try:
                yield instance
            finally:
                await provider.close()
                telemetry.close()

    @staticmethod
    def config(run_id):
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 24}

    async def coordinate(self, state):
        run = state["run"]
        with self.telemetry.span("agent.coordinate", run["id"], "coordinator"):
            plan = await self.gateway.call(run, "coordinator", {"goal": run["goal"]}, Plan)
            if sorted(plan.specialists) != sorted(SPECIALISTS):
                raise PolicyError("invalid_specialist_plan")
            self.store.event(run["id"], "plan", "coordinator", plan.model_dump())
            return {"plan": plan.model_dump()}

    def dispatch(self, state):
        return [
            Send("specialist", {"run": state["run"], "agent": agent})
            for agent in state["plan"]["specialists"]
        ]

    async def specialist(self, state):
        run, agent = state["run"], state["agent"]
        observations = []
        with self.telemetry.span("agent.specialist", run["id"], agent):
            try:
                for _ in range(3):
                    decision = await self.gateway.call(
                        run,
                        agent,
                        {
                            "goal": run["goal"],
                            "allowed_tool": SCOPES[agent],
                            "observations": observations,
                        },
                        Decision,
                    )
                    self.store.event(run["id"], "decision", agent, decision.model_dump())
                    if decision.action == "tool":
                        authorize_tool(agent, decision.tool, decision.arguments)
                        if observations:
                            raise PolicyError("duplicate_tool_request")
                        with self.telemetry.span(
                            "tool.read", run["id"], agent, **{"aegis.tool": decision.tool}
                        ):
                            item = evidence(agent, run["scenario"])
                            observations.append(item)
                            self.store.event(run["id"], "observation", agent, {"evidence": item})
                    else:
                        if (
                            not observations
                            or not observations[-1]
                            or not decision.summary
                            or set(decision.citations) != {observations[-1]["id"]}
                            or decision.tool is not None
                            or decision.arguments
                        ):
                            raise PolicyError("evidence_missing_or_invalid")
                        return {
                            "findings": [
                                {
                                    "agent": agent,
                                    "summary": decision.summary,
                                    "evidence": observations[-1],
                                }
                            ]
                        }
                raise PolicyError("agent_step_limit")
            except (PolicyError, GatewayError) as exc:
                self.telemetry.blocks.add(1, {"agent": agent, "code": str(exc)})
                self.store.event(run["id"], "blocked", agent, {"code": str(exc)})
                return {"errors": [str(exc)]}

    async def synthesize(self, state):
        if state.get("errors"):
            return {"outcome": "blocked"}
        run = state["run"]
        with self.telemetry.span("agent.synthesis", run["id"], "synthesis"):
            proposal = await self.gateway.call(
                run,
                "synthesis",
                {
                    "goal": run["goal"],
                    "findings": sorted(state["findings"], key=lambda f: f["agent"]),
                    "revision": state.get("revision", 0),
                    "feedback": state.get("critique"),
                },
                Proposal,
            )
            self.store.event(run["id"], "proposal", "synthesis", proposal.model_dump())
            return {"proposal": proposal.model_dump()}

    async def critic(self, state):
        run = state["run"]
        with self.telemetry.span("agent.critic", run["id"], "critic"):
            critique = await self.gateway.call(
                run,
                "critic",
                {
                    "proposal": state["proposal"],
                    "revision": state.get("revision", 0),
                    "findings": sorted(state["findings"], key=lambda f: f["agent"]),
                },
                Critique,
            )
            self.store.event(run["id"], "evaluation", "critic", critique.model_dump())
            if critique.verdict == "revise":
                if state.get("revision", 0) >= 1:
                    return {
                        "critique": critique.model_dump(),
                        "outcome": "blocked",
                        "errors": ["critic_revision_limit"],
                    }
                return {"critique": critique.model_dump(), "revision": 1}
            return {"critique": critique.model_dump()}

    def after_critic(self, state):
        if state.get("outcome") == "blocked":
            return END
        return "synthesis" if state["critique"]["verdict"] == "revise" else "policy_gate"

    def validate_evidence(self, state):
        findings = state["findings"]
        if sorted(f["agent"] for f in findings) != sorted(SPECIALISTS):
            raise PolicyError("incomplete_specialist_review")
        elapsed = max(0, time.time() - state["run"]["started"])
        for finding in findings:
            item = finding["evidence"]
            # Provenance and content are checked against the trusted tool snapshot.
            if item != evidence(finding["agent"], state["run"]["scenario"]):
                raise PolicyError("evidence_provenance_invalid")
            if item["age_seconds"] + elapsed > 3600:
                raise PolicyError("evidence_stale")
        if set(state["proposal"]["citations"]) != {f["evidence"]["id"] for f in findings}:
            raise PolicyError("proposal_citations_invalid")
        by_agent = {f["agent"]: f["evidence"]["data"] for f in findings}
        recommendation = state["proposal"]["recommendation"]
        regressed = by_agent["performance"]["p95_ms"] > by_agent["performance"]["target_ms"]
        if recommendation == "simulate_canary" and (
            not regressed
            or not by_agent["change"]["rollback_available"]
            or by_agent["risk"]["max_canary_percent"] > 5
            or not state["proposal"]["rollback"]
        ):
            raise PolicyError("canary_preconditions_failed")
        if recommendation == "no_action" and regressed:
            raise PolicyError("regression_requires_review")

    async def gate(self, state):
        run = state["run"]
        with self.telemetry.span("policy.evaluate", run["id"], "policy"):
            self.validate_evidence(state)
            outcome = {
                "simulate_canary": "awaiting_approval",
                "no_action": "completed",
                "escalate": "escalated",
            }[state["proposal"]["recommendation"]]
            self.store.event(run["id"], "policy_pass", "policy", {"outcome": outcome})
            return {"outcome": outcome}

    async def approval(self, state):
        # LangGraph replays this node on resume. No side effects occur before interrupt().
        proposal_digest = digest(state["proposal"])
        receipt = interrupt(
            {
                "proposal": state["proposal"],
                "proposal_digest": proposal_digest,
                "action": "local_simulation_only",
            }
        )
        if receipt.get("proposal_digest") != proposal_digest:
            raise PolicyError("approval_digest_mismatch")
        if not receipt.get("approved"):
            return {"outcome": "declined"}
        self.validate_evidence(state)
        run = state["run"]
        with self.telemetry.span("execution.simulate", run["id"], "executor"):
            result = self.store.simulate(run["id"], run["tenant"], proposal_digest)
            self.store.event(run["id"], "execution", "executor", result)
            passed = result["canary_p95_ms"] <= 300 and result["rollback_available"]
            self.store.event(
                run["id"],
                "evaluation",
                "policy",
                {
                    "passed": passed,
                    "next_action": "review_simulation" if passed else "rollback",
                },
            )
            return {"result": result, "outcome": "completed" if passed else "blocked"}

    async def drive(self, run_id, input_value):
        try:
            # Automatic LangSmith tracing can capture state/prompts. Explicitly disable it.
            # A separate opt-in, metadata-only exporter is provided in telemetry.py.
            with tracing_context(enabled=False), self.telemetry.span("harness.run", run_id) as span:
                async with asyncio.timeout(60):
                    result = await self.graph.ainvoke(input_value, self.config(run_id))
                span.set_attribute("aegis.outcome", result.get("outcome", "blocked"))
            if result.get("__interrupt__"):
                self.store.update(
                    run_id, "awaiting_approval", proposal_digest=digest(result["proposal"])
                )
            else:
                self.store.update(
                    run_id,
                    result.get("outcome", "blocked"),
                    error=", ".join(sorted(set(result.get("errors", [])))) or None,
                )
        except (PolicyError, GatewayError) as exc:
            self.telemetry.blocks.add(1, {"agent": "harness", "code": str(exc)})
            self.store.event(run_id, "blocked", "harness", {"code": str(exc)})
            self.store.update(run_id, "blocked", error=str(exc))
        except TimeoutError:
            self.store.update(run_id, "blocked", error="run_deadline_exceeded")
        except Exception:
            self.store.update(run_id, "failed", error="internal_error")
            raise

    async def start(self, principal, request: StartRequest):
        authorize(principal, "start")
        if request.scenario not in SCENARIOS:
            raise PolicyError("unknown_scenario")
        request = request.model_copy(update={"goal": guard_goal(request.goal)})
        run_id = uuid.uuid4().hex
        self.store.create(run_id, principal.tenant, request)
        run = {
            "id": run_id,
            "tenant": principal.tenant,
            "goal": request.goal,
            "scenario": request.scenario,
            "cache": request.cache,
            "started": time.time(),
        }
        await self.drive(run_id, {"run": run, "findings": [], "errors": [], "revision": 0})
        return await self.get(principal, run_id)

    async def approve(self, principal, run_id, request: ApprovalRequest):
        authorize(principal, "approve")
        self.store.get(run_id, principal.tenant)
        # Atomic claim prevents concurrent approvals/replays, including across processes.
        self.store.claim_approval(run_id, principal.tenant, request.proposal_digest)
        self.store.event(
            run_id,
            "approval",
            "operator",
            {
                "subject": principal.subject,
                **request.model_dump(),
            },
        )
        await self.drive(run_id, Command(resume=request.model_dump()))
        return await self.get(principal, run_id)

    async def get(self, principal, run_id):
        authorize(principal, "read")
        row = self.store.get(run_id, principal.tenant)
        checkpoint = await self.graph.aget_state(self.config(run_id))
        state = checkpoint.values or {}
        events = self.store.events(run_id, principal.tenant)
        return {
            **row,
            "proposal": state.get("proposal"),
            "findings": state.get("findings", []),
            "result": state.get("result"),
            "events": events,
            "cache_hits": sum(e["kind"] == "cache_hit" for e in events),
            "provider_cached_tokens": sum(
                e["payload"].get("provider_cached_tokens", 0)
                for e in events
                if e["kind"] == "model_usage"
            ),
        }
