import json
import threading
import uuid
from contextlib import contextmanager

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode


class LangSmithSummaryExporter(SpanExporter):
    """Optional run summaries only; no prompts, goals, findings, credentials or model text."""

    def __init__(self, client, project="aegis-harness"):
        self.client, self.project = client, project

    def export(self, spans):
        try:
            for span in spans:
                if span.name != "harness.run":
                    continue
                run_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{span.context.trace_id}:{span.context.span_id}"
                )
                self.client.create_run(
                    name="aegis.harness",
                    run_type="chain",
                    id=run_id,
                    inputs={},
                    project_name=self.project,
                    extra={
                        "metadata": {
                            "otel_trace_id": f"{span.context.trace_id:032x}",
                            "run_id": span.attributes.get("aegis.run.id"),
                            "outcome": span.attributes.get("aegis.outcome", "error"),
                            "export_mode": "metadata_only",
                        }
                    },
                )
                self.client.update_run(
                    run_id,
                    outputs={
                        "span_status": span.status.status_code.name,
                        "duration_ms": (span.end_time - span.start_time) / 1e6,
                    },
                )
            return SpanExportResult.SUCCESS
        except Exception:
            # Fail open for observability; never leak a remote error payload into the agent trace.
            return SpanExportResult.FAILURE

    def shutdown(self):
        self.client.flush()


class SafeExporter(SpanExporter):
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def export(self, spans):
        with self.lock:
            for span in spans:
                attributes = dict(span.attributes or {})
                run_id = attributes.pop("aegis.run.id", None)
                if run_id:
                    self.store.event(
                        run_id,
                        "span",
                        attributes.get("aegis.agent", "harness"),
                        {
                            "name": span.name,
                            "trace_id": f"{span.context.trace_id:032x}",
                            "span_id": f"{span.context.span_id:016x}",
                            "parent_id": f"{span.parent.span_id:016x}" if span.parent else None,
                            "start_ns": span.start_time,
                            "end_ns": span.end_time,
                            "duration_ms": round((span.end_time - span.start_time) / 1e6, 3),
                            "status": span.status.status_code.name,
                            "attributes": attributes,
                        },
                    )
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class Telemetry:
    def __init__(self, store, endpoint=None, langsmith_client=None):
        resource = Resource.create(
            {"service.name": "aegis-agent-harness", "service.version": "1.0.0"}
        )
        # The inspectable local lab must retain every event regardless of ambient sampling.
        self.provider = TracerProvider(resource=resource, sampler=ALWAYS_ON)
        self.provider.add_span_processor(SimpleSpanProcessor(SafeExporter(store)))
        if langsmith_client:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            self.provider.add_span_processor(
                BatchSpanProcessor(LangSmithSummaryExporter(langsmith_client))
            )
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            self.provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        self.tracer = self.provider.get_tracer("aegis")
        self.reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[self.reader])
        meter = self.meter_provider.get_meter("aegis")
        self.requests = meter.create_counter("aegis.gateway.requests")
        self.cache_hits = meter.create_counter("aegis.gateway.cache_hits")
        self.blocks = meter.create_counter("aegis.policy.blocks")
        self.latency = meter.create_histogram("aegis.gateway.duration", unit="s")

    @contextmanager
    def span(self, name, run_id, agent="harness", **attrs):
        attributes = {"aegis.run.id": run_id, "aegis.agent": agent, **attrs}
        # Exception objects can contain prompts or provider responses. Do not auto-record them.
        with self.tracer.start_as_current_span(
            name, attributes=attributes, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield span
            except Exception:
                span.set_status(Status(StatusCode.ERROR))
                raise

    def metrics(self):
        data = self.reader.get_metrics_data()
        return json.loads(data.to_json()) if data else {}

    def close(self):
        self.provider.shutdown()
        self.meter_provider.shutdown()
