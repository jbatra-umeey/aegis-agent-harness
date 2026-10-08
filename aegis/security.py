import hashlib
import hmac
import json
import re

from .contracts import PolicyError, Principal

SCOPES = {"performance": "read_metrics", "change": "read_deployments", "risk": "read_runbook"}
# Narrow recognizers are a demonstration of secret hygiene, not a general DLP engine.
SECRET = re.compile(
    r"(?i)(?:\bsk-[a-z0-9_-]{12,}|\bAKIA[A-Z0-9]{16}\b|(?:api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]{6,})"
)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def clean(value):
    if isinstance(value, str):
        return EMAIL.sub("[email]", SECRET.sub("[secret]", value))
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def guard_goal(goal):
    if SECRET.search(goal):
        raise PolicyError("secret_in_goal")
    return clean(goal)


def authorize_tool(agent, tool, arguments):
    if tool != SCOPES.get(agent) or arguments:
        raise PolicyError("tool_scope_denied")


def authorize(principal: Principal, operation):
    allowed = {
        "read": {"analyst", "operator", "auditor"},
        "start": {"analyst", "operator"},
        "approve": {"operator"},
    }
    if principal.role not in allowed[operation]:
        raise PolicyError("role_denied")


def authenticate(token, identities):
    # Identity and tenant are resolved server-side; client-supplied tenant fields are rejected.
    for key, identity in identities.items():
        if hmac.compare_digest(token.encode(), key.encode()):
            return Principal.model_validate(identity)
    raise PolicyError("unauthenticated")
