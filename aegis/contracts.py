from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SPECIALISTS = ("performance", "change", "risk")
POLICY_VERSION = "release-policy-v1"
PROMPT_VERSION = "release-prompts-v1"
SCENARIOS = {
    "regression": "Latency regression / approve a local canary",
    "healthy": "Healthy release / no action required",
    "injection": "Runbook contains a forged authority instruction",
    "missing": "Missing deployment evidence",
    "stale": "Stale performance evidence",
    "fallback": "Economy provider is unavailable",
    "outage": "All providers are unavailable",
    "forbidden_tool": "Compromised agent requests a forbidden tool",
    "malformed": "Model returns an invalid output contract",
    "revision": "Critic requests one revision",
    "critic_loop": "Critic never accepts the draft",
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Principal(Contract):
    tenant: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    subject: str = Field(min_length=1, max_length=80)
    role: Literal["analyst", "operator", "auditor"]


class StartRequest(Contract):
    goal: str = Field(
        default="Review the mobile editor release and propose a reversible canary.",
        min_length=1,
        max_length=2000,
    )
    scenario: str = "regression"
    max_calls: int = Field(default=20, ge=1, le=40)
    max_units: int = Field(default=32, ge=1, le=80)
    cache: bool = True


class Plan(Contract):
    specialists: list[Literal["performance", "change", "risk"]]
    rationale: str = Field(min_length=1, max_length=500)


class Decision(Contract):
    action: Literal["tool", "finish"]
    tool: str | None = Field(default=None, max_length=80)
    arguments: dict = Field(default_factory=dict)
    summary: str = Field(default="", max_length=1200)
    citations: list[str] = Field(default_factory=list, max_length=6)


class Proposal(Contract):
    recommendation: Literal["simulate_canary", "no_action", "escalate"]
    summary: str = Field(min_length=1, max_length=1800)
    citations: list[str] = Field(max_length=10)
    rollback: str = Field(max_length=500)


class Critique(Contract):
    verdict: Literal["accept", "revise"]
    reason: str = Field(min_length=1, max_length=600)


class ApprovalRequest(Contract):
    approved: bool
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PolicyError(Exception):
    """A safe, stable error code; never carries model/provider payloads."""


class GatewayError(Exception):
    pass


class TransientProviderError(GatewayError):
    pass
