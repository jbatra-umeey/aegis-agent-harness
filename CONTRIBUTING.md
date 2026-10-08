# Contributing

Use Python 3.12, install the pinned constraints from the README, and run the existing verification gates before opening a change.

Keep the harness as the sole owner of routing, budgets and fallback. Every model output needs a strict Pydantic contract. New tools need explicit capabilities, fixed schemas, provenance, independent authorization and a threat-focused test. Untrusted observations must never modify the server identity, approval policy or available tools.

Add fixture scenarios in `providers.py`, an expected trajectory in `evals.py`, and tests for the concrete boundary the change introduces. Keep fixtures synthetic. A new provider integration needs mock HTTP contract tests and a clearly separate opt-in live evaluation; do not put provider keys in CI.

When modifying prompts or policy, bump `PROMPT_VERSION` or `POLICY_VERSION` in `contracts.py` so cache entries cannot silently cross the change. Preserve tenant and schema information in the cache namespace. Do not cache action approvals.

Update `requirements.lock` from a clean Python 3.12 environment after intentional dependency upgrades. It is a version constraints snapshot, not a cryptographic hash lock. Verify the fresh install and review upstream changes. Commit `package-lock.json` for the optional browser checks and keep Actions SHA pins current.

Document behavior, reasons, validation and remaining limits in the pull request. Public issues should contain minimal synthetic reproductions, never real credentials or customer data.
