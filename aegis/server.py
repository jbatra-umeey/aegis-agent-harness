import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .contracts import SCENARIOS, ApprovalRequest, PolicyError, StartRequest
from .harness import Harness
from .providers import LiteLLMProvider
from .security import authenticate

DEMO_IDENTITIES = {
    "demo-operator-atlas": {"tenant": "atlas", "subject": "demo-operator", "role": "operator"},
    "demo-auditor-atlas": {"tenant": "atlas", "subject": "demo-auditor", "role": "auditor"},
    "demo-operator-orion": {"tenant": "orion", "subject": "demo-operator", "role": "operator"},
}


def create_app(directory=None, provider=None, identities=None):
    mode = os.getenv("AEGIS_MODE", "fixture")
    if mode not in {"fixture", "proxy"}:
        raise ValueError("AEGIS_MODE must be fixture or proxy")
    configured = identities or json.loads(os.getenv("AEGIS_IDENTITIES_JSON", "{}"))
    if mode == "proxy" and (
        not configured or any(k.startswith("demo-") or len(k) < 24 for k in configured)
    ):
        raise ValueError("Proxy mode requires strong, server-configured bearer credentials")
    demo = not configured and mode == "fixture"
    identities = configured or DEMO_IDENTITIES
    capacity = asyncio.Semaphore(8)

    @asynccontextmanager
    async def lifespan(app):
        selected = provider
        if mode == "proxy" and selected is None:
            selected = LiteLLMProvider(
                os.environ["LITELLM_BASE_URL"],
                os.environ["LITELLM_API_KEY"],
                prefix_cache=os.getenv("AEGIS_PROVIDER_PREFIX_CACHE") == "1",
            )
        ls = None
        if os.getenv("AEGIS_LANGSMITH_SUMMARIES") == "1":
            from langsmith import Client

            ls = Client(
                api_key=os.environ["LANGSMITH_API_KEY"],
                hide_inputs=True,
                hide_outputs=True,
                auto_batch_tracing=True,
            )
        async with Harness.open(
            directory or os.getenv("AEGIS_DATA_DIR", ".data"),
            selected,
            os.getenv("AEGIS_OTLP_TRACES_ENDPOINT"),
            ls,
        ) as harness:
            app.state.harness = harness
            yield

    app = FastAPI(
        title="Aegis Agent Harness",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "localhost",
            "127.0.0.1",
            "[::1]",
            "testserver",
            *[x.strip() for x in os.getenv("AEGIS_ALLOWED_HOSTS", "").split(",") if x.strip()],
        ],
    )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "origin_denied"}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({"error": "body_too_large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(PolicyError)
    async def policy_error(request, exc):
        code = str(exc)
        status = (
            401
            if code == "unauthenticated"
            else 404
            if code == "run_not_found"
            else 409
            if code == "approval_conflict_or_expired"
            else 403
        )
        return JSONResponse({"error": code}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default detail can echo rejected secret-bearing inputs.
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    async def principal(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise PolicyError("unauthenticated")
        return authenticate(authorization[7:], identities)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "mode": mode}

    @app.get("/docs", include_in_schema=False)
    async def api_reference():
        return RedirectResponse("/openapi.json")

    @app.get("/api/config")
    async def config():
        return {
            "mode": mode,
            "scenarios": SCENARIOS,
            "demo_identities": DEMO_IDENTITIES if demo else {},
            "version": "1.0.0",
        }

    @app.get("/api/runs")
    async def list_runs(identity=Depends(principal)):
        return app.state.harness.store.list(identity.tenant)

    @app.post("/api/runs")
    async def start(body: StartRequest, identity=Depends(principal)):
        async with capacity:
            return await app.state.harness.start(identity, body)

    @app.get("/api/runs/{run_id}")
    async def read(run_id: str, identity=Depends(principal)):
        return await app.state.harness.get(identity, run_id)

    @app.post("/api/runs/{run_id}/approval")
    async def approve(run_id: str, body: ApprovalRequest, identity=Depends(principal)):
        async with capacity:
            return await app.state.harness.approve(identity, run_id, body)

    web = Path(__file__).parent / "web"

    @app.get("/")
    async def index():
        return FileResponse(web / "index.html")

    @app.get("/app.js")
    async def javascript():
        return FileResponse(web / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    async def stylesheet():
        return FileResponse(web / "style.css", media_type="text/css")

    return app
