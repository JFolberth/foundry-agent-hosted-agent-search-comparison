from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.trace import Status, StatusCode

from comparison.clients import open_clients
from comparison.cancellation import while_connected
from comparison.config import ROOT, Settings, load_config
from comparison.contracts import CompareRequest, MAX_HISTORY_CHARS, MAX_HISTORY_MESSAGES, MAX_MESSAGE, MAX_TURNS
from comparison.guard import RequestGuard
from comparison.service import ComparisonService
from comparison.telemetry import configure_telemetry, tracer


def create_app(service=None):
    config = load_config()

    @asynccontextmanager
    async def lifespan(app):
        configure_telemetry("comparison-web")
        async with AsyncExitStack() as stack:
            if app.state.service is None:
                settings = Settings.from_env()
                clients = await open_clients(stack, settings)
                app.state.service = ComparisonService(clients, config)
            app.state.ready = True
            yield
            app.state.ready = False

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.ready = service is not None
    app.add_middleware(RequestGuard)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"error": "Invalid message, history, or continuation."}, status_code=422)

    @app.get("/health")
    @app.get("/readiness")
    @app.get("/health/readiness")
    async def health():
        return JSONResponse(
            {"status": "ready" if app.state.ready else "starting"},
            status_code=200 if app.state.ready else 503,
        )

    @app.get("/api/config")
    async def public_config():
        return {
            "reasoning_effort": config.reasoning_effort,
            "reasoning_fixed": True,
            "search_query_type": config.search_query_type,
            "search_top_k": config.search_top_k,
            "max_output_tokens": config.max_output_tokens,
            "public_anonymous": True,
            "limits": {
                "message_chars": MAX_MESSAGE,
                "history_messages_per_agent": MAX_HISTORY_MESSAGES,
                "history_chars_per_agent": MAX_HISTORY_CHARS,
                "turns_per_agent": MAX_TURNS,
                "continuation_ttl_seconds": 1200,
                "request_timeout_seconds": 90,
            },
            "telemetry": {
                "content_logging": False,
                "tool_calls": "Observed native output only; absent evidence is not zero calls.",
                "query": (
                    "AppDependencies | where tostring(Properties['comparison.id']) == '<comparison_id>' "
                    "| project TimeGenerated, Name, OperationId, Properties"
                ),
            },
        }

    @app.post("/api/compare")
    async def compare(request: CompareRequest, connection: Request):
        if not app.state.ready:
            return JSONResponse({"error": "Service is not ready."}, status_code=503)
        comparison_id = str(uuid4())
        with tracer.start_as_current_span(
            "comparison", record_exception=False, set_status_on_exception=False,
            attributes={"comparison.id": comparison_id},
        ) as span:
            try:
                results = await while_connected(
                    app.state.service.compare(request, comparison_id), connection.is_disconnected,
                )
            except BaseException:
                span.set_status(Status(StatusCode.ERROR))
                raise
            if any(result.get("error") is not None for result in results.values()):
                span.set_status(Status(StatusCode.ERROR))
        return {"comparison_id": comparison_id, "reasoning_effort": config.reasoning_effort, **results}

    static = ROOT / "src/web/static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


app = create_app()
