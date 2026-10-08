import asyncio
import time

from pydantic import ValidationError

from .contracts import POLICY_VERSION, PROMPT_VERSION, GatewayError, TransientProviderError
from .security import SCOPES, clean, digest


class Gateway:
    """One budgeted, bounded gateway for all agents. Cache is exact-response, not semantic."""

    def __init__(self, provider, store, telemetry, concurrency=4):
        self.provider, self.store, self.telemetry = provider, store, telemetry
        self.capacity = asyncio.Semaphore(concurrency)
        self.circuits = {}
        # Fixed stripes bound memory and provide same-process singleflight.
        self.locks = [asyncio.Lock() for _ in range(64)]

    def key(self, tenant, role, context, contract, route, scenario):
        return digest(
            {
                "tenant": tenant,
                "policy": POLICY_VERSION,
                "prompt": PROMPT_VERSION,
                "provider": self.provider.namespace,
                "role": role,
                "context": context,
                "schema": contract.model_json_schema(),
                "route": route,
                "scenario": scenario,
                "tool": SCOPES.get(role),
                "temperature": 0,
                "max_tokens": 1600,
            }
        )

    async def call(self, run, role, context, contract):
        primary = "economy" if role in SCOPES else "reasoning"
        for route in (primary, "fallback"):
            key = self.key(run["tenant"], role, context, contract, route, run["scenario"])
            async with self.locks[int(key[:8], 16) % len(self.locks)]:
                if run["cache"]:
                    cached = self.store.cached(key, run["tenant"])
                    if cached is not None:
                        value = contract.model_validate(cached)
                        self.telemetry.cache_hits.add(1, {"agent": role, "route": route})
                        self.store.event(
                            run["id"],
                            "cache_hit",
                            role,
                            {"route": route, "layer": "exact_response"},
                        )
                        return value
                circuit = self.circuits.get(route, (0, 0))
                if circuit[1] > time.monotonic():
                    self.store.event(run["id"], "circuit_open", role, {"route": route})
                    continue
                async with self.capacity:
                    self.store.reserve(run["id"], run["tenant"], 1 if route == "economy" else 2)
                    self.telemetry.requests.add(1, {"agent": role, "route": route})
                    started = time.monotonic()
                    self.store.event(run["id"], "model_attempt", role, {"route": route})
                    try:
                        with self.telemetry.span(
                            "gateway.completion",
                            run["id"],
                            role,
                            **{"gen_ai.operation.name": "chat", "aegis.route": route},
                        ) as span:
                            async with asyncio.timeout(16):
                                reply = await self.provider.complete(
                                    role,
                                    context,
                                    contract.model_json_schema(),
                                    route,
                                    run["scenario"],
                                )
                            try:
                                parsed = contract.model_validate_json(reply.content)
                            except ValidationError as exc:
                                raise GatewayError("model_contract_invalid") from exc
                            # The same sanitized value is used by the graph, cache, and human UI.
                            value = contract.model_validate(clean(parsed.model_dump()))
                            span.set_attribute("gen_ai.usage.input_tokens", reply.input_tokens)
                            span.set_attribute("gen_ai.usage.output_tokens", reply.output_tokens)
                            span.set_attribute("aegis.provider.cached_tokens", reply.cached_tokens)
                            self.store.event(
                                run["id"],
                                "model_usage",
                                role,
                                {
                                    "route": route,
                                    "input_tokens": reply.input_tokens,
                                    "output_tokens": reply.output_tokens,
                                    "provider_cached_tokens": reply.cached_tokens,
                                },
                            )
                        self.circuits[route] = (0, 0)
                        if run["cache"]:
                            self.store.put_cache(key, run["tenant"], value.model_dump())
                        return value
                    except (TransientProviderError, TimeoutError):
                        count = self.circuits.get(route, (0, 0))[0] + 1
                        self.circuits[route] = (count, time.monotonic() + 10 if count >= 2 else 0)
                        self.store.event(run["id"], "fallback", role, {"failed_route": route})
                    finally:
                        self.telemetry.latency.record(
                            time.monotonic() - started, {"agent": role, "route": route}
                        )
        raise GatewayError("all_routes_unavailable")
