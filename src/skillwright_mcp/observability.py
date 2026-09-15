from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from . import __version__
from .config import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from taskiq import AsyncBroker


@dataclass(frozen=True, slots=True)
class Observability:
    tracer_provider: Any | None
    meter_provider: Any | None


_configure_lock = Lock()
_configured: Observability | None = None
_queue_publish_counter: Any | None = None


def _otlp_signal_endpoint(base: str, signal: str) -> str:
    return f"{base.rstrip('/')}/v1/{signal}"


def configure_observability(settings: Settings) -> Observability:
    """Configure process-wide OTLP tracing and metrics once when explicitly enabled."""

    global _configured, _queue_publish_counter
    if not settings.otel_enabled:
        return Observability(tracer_provider=None, meter_provider=None)

    with _configure_lock:
        if _configured is not None:
            return _configured

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": __version__,
            }
        )

        current_tracer_provider = trace.get_tracer_provider()
        if isinstance(current_tracer_provider, TracerProvider):
            tracer_provider: Any = current_tracer_provider
        else:
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=_otlp_signal_endpoint(
                            settings.otel_exporter_otlp_endpoint,
                            "traces",
                        )
                    )
                )
            )
            trace.set_tracer_provider(tracer_provider)

        current_meter_provider = metrics.get_meter_provider()
        if isinstance(current_meter_provider, MeterProvider):
            meter_provider: Any = current_meter_provider
        else:
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=_otlp_signal_endpoint(
                        settings.otel_exporter_otlp_endpoint,
                        "metrics",
                    )
                )
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)

        meter = meter_provider.get_meter("skillwright_mcp", __version__)
        _queue_publish_counter = meter.create_counter(
            "skillwright.queue.publish",
            unit="1",
            description="Run messages published to the execution queue.",
        )
        _configured = Observability(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )
        return _configured


def instrument_fastapi(app: FastAPI, observability: Observability) -> None:
    if observability.tracer_provider is None and observability.meter_provider is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=observability.tracer_provider,
        meter_provider=observability.meter_provider,
    )


def instrument_taskiq_broker(broker: AsyncBroker, observability: Observability) -> None:
    if observability.tracer_provider is None and observability.meter_provider is None:
        return
    from taskiq.instrumentation import TaskiqInstrumentor
    from taskiq.middlewares.opentelemetry_middleware import OpenTelemetryMiddleware

    if any(isinstance(middleware, OpenTelemetryMiddleware) for middleware in broker.middlewares):
        return
    TaskiqInstrumentor().instrument_broker(
        broker,
        tracer_provider=observability.tracer_provider,
        meter_provider=observability.meter_provider,
    )


def record_queue_publish(outcome: Literal["success", "failure", "recovered"]) -> None:
    counter = _queue_publish_counter
    if counter is None:
        return
    counter.add(1, {"outcome": outcome})
