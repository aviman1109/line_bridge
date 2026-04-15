"""OpenTelemetry setup — mirrors pattern used by other Caddie MCPs."""
from __future__ import annotations
import logging
import os

log = logging.getLogger("line_bridge.otel")


def setup_otel() -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        service_name = os.environ.get("OTEL_SERVICE_NAME", "line-bridge")
        resource_attrs = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
        attrs = {"service.name": service_name}
        for kv in resource_attrs.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                attrs[k.strip()] = v.strip()
        provider = TracerProvider(resource=Resource.create(attrs))
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
        ))
        trace.set_tracer_provider(provider)
        log.info("OTEL tracing enabled → %s", endpoint)
    except Exception as e:
        log.warning("OTEL setup failed (non-fatal): %s", e)
