# c:\Users\tyriq\Documents\Github\lead_ignite_backend_3.0\backend\app\core\telemetry\client.py
import logging
import os
from contextlib import contextmanager
from typing import Any

import grpc
from circuitbreaker import circuit
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import (
    LoggerProvider,
    LoggingHandler,
)
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)


class TelemetryClient:
    def __init__(self, service_name: str, service_version: str = "1.0.0", auto_init: bool = True, 
                 instance_id: str = None, environment: str = None):
        """Production-ready telemetry client with metrics and proper shutdown."""
        self.service_name = service_name
        self.service_version = service_version
        self.instance_id = instance_id or f"{service_name}-{os.getpid()}"
        self.environment = environment or os.getenv("ENVIRONMENT", "development")
        
        # Always initialize the basic resource and providers
        self._initialize_base_providers()
        
        # Only auto-initialize exporters if requested (allows external configuration)
        if auto_init:
            self._initialize_tracing()
            self._initialize_metrics()
            self._initialize_logging()

    def _initialize_base_providers(self):
        """Initialize base trace and metric providers without exporters"""
        resource_attributes = {
            "service.name": self.service_name,
            "service.version": self.service_version,
            "service.instance.id": self.instance_id,
            "environment": self.environment,
        }
        
        resource = Resource.create(resource_attributes)
        
        # Initialize TracerProvider if not already set
        if not hasattr(trace.get_tracer_provider(), 'add_span_processor'):
            trace.set_tracer_provider(TracerProvider(resource=resource))
        
        # Initialize MeterProvider if not already set  
        try:
            current_provider = metrics.get_meter_provider()
            # Check if it's the default NoOpMeterProvider
            if type(current_provider).__name__ == 'NoOpMeterProvider':
                metrics.set_meter_provider(MeterProvider(resource=resource))
        except Exception as e:
            logger.warning(f"MeterProvider already initialized or error setting: {e}")

    @circuit(
        failure_threshold=3,
        recovery_timeout=30,
        expected_exception=(grpc.RpcError, ConnectionError, RuntimeError),
        fallback_function=lambda e: logger.warning(f"Telemetry circuit open: {str(e)}"),
    )
    def _initialize_tracing(self):
        """Initialize tracing with production-ready configuration."""
        otlp_exporter = OTLPSpanExporter(
            endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
            insecure=True,
            timeout=2  # Fast timeout for non-blocking operation
        )

        # Use optimized batch processor settings
        span_processor = BatchSpanProcessor(
            otlp_exporter,
            max_queue_size=2048,  # Larger queue to reduce blocking
            export_timeout_millis=1000,  # 1s export timeout
            schedule_delay_millis=500,  # 500ms delay between exports
            max_export_batch_size=512  # Larger batches
        )
        trace.get_tracer_provider().add_span_processor(span_processor)

    @circuit(
        failure_threshold=3,
        recovery_timeout=30,
        expected_exception=(grpc.RpcError, ConnectionError, RuntimeError),
    )
    def _initialize_metrics(self):
        """Initialize metrics collection."""
        metric_exporter = OTLPMetricExporter(
            endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://localhost:4317"
            ),
            # ! Respect insecure flag for consistency and security
            insecure=True,
        )
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        
        # Update the existing meter provider with the new reader
        try:
            current_provider = metrics.get_meter_provider()
            if hasattr(current_provider, '_metric_readers'):
                current_provider._metric_readers.append(metric_reader)
            elif type(current_provider).__name__ == 'NoOpMeterProvider':
                # Only set if it's the default NoOp provider
                metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader]))
            else:
                logger.warning("MeterProvider already exists, skipping metrics setup")
        except Exception as e:
            logger.warning(f"Failed to setup metrics exporter: {e}")

    @circuit(
        failure_threshold=3,
        recovery_timeout=30,
        expected_exception=(grpc.RpcError, ConnectionError, RuntimeError),
    )
    def _initialize_logging(self):
        """Initialize logging with OTLP exporter for Loki"""
        resource = Resource.create(
            {
                "service.name": self.service_name,
                "service.version": self.service_version,
                "environment": os.getenv("ENVIRONMENT", "development"),
            }
        )
        logger_provider = LoggerProvider(resource=resource)
        log_exporter = OTLPLogExporter(
            endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://localhost:4317"
            ),
            insecure=True,
        )
        log_processor = BatchLogRecordProcessor(log_exporter)
        logger_provider.add_log_record_processor(log_processor)
        set_logger_provider(logger_provider)
        # Pipe Python logs into OpenTelemetry
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        logging.getLogger().addHandler(handler)

    def instrument_fastapi(self, app):
        """Instrument FastAPI application for automatic tracing."""
        FastAPIInstrumentor.instrument_app(app)

    @contextmanager
    def start_span(self, name: str, attributes: dict[str, Any] | None):
        """Context manager for creating spans with proper error handling."""
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(name) as span:
            try:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, str(v))
                yield span
            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                logger.error(f"Error in span {name}: {str(e)}")
                raise

    def get_tracer(self):
        """Get the configured tracer instance."""
        return trace.get_tracer(__name__)

    def shutdown(self):
        """Properly shutdown telemetry providers."""
        trace.get_tracer_provider().shutdown()
        metrics.get_meter_provider().shutdown()

    def span_pulsar_operation(self, operation: str, attributes: dict[str, Any] | None):
        """
        Context manager for tracing a Pulsar client operation.
        Usage: with telemetry_client.span_pulsar_operation('send_message'):
        """
        return self.start_span(f"pulsar.{operation}", attributes)

    def span_cache_operation(self, operation: str, attributes: dict[str, Any] | None):
        """
        Context manager for tracing a VALKEY cache operation.
        Usage: with telemetry_client.span_cache_operation('get'):
        """
        return self.start_span(f"cache.{operation}", attributes)

    def span_celery_operation(self, operation: str, attributes: dict[str, Any] | None):
        """
        Context manager for tracing Celery operations.
        Usage: with telemetry_client.span_celery_operation('execute', {'task_name': name}):
        """
        return self.start_span(f"celery.{operation}", attributes)

    def configure_exporters(self, span_exporter, metric_exporter=None):
        """
        Configure custom exporters for traces and metrics.
        This allows external configuration for Grafana Cloud or other providers.

        Args:
            span_exporter: OpenTelemetry span exporter instance
            metric_exporter: Optional OpenTelemetry metric exporter instance
        """
        # Configure trace exporter with optimized batch settings for better performance
        span_processor = BatchSpanProcessor(
            span_exporter,
            max_queue_size=2048,  # Larger queue to reduce blocking
            export_timeout_millis=1000,  # 1s export timeout
            schedule_delay_millis=500,  # 500ms delay between exports
            max_export_batch_size=512  # Larger batches
        )
        trace.get_tracer_provider().add_span_processor(span_processor)

        # Configure metric exporter if provided
        if metric_exporter:
            metric_reader = PeriodicExportingMetricReader(metric_exporter)
            try:
                current_provider = metrics.get_meter_provider()
                if type(current_provider).__name__ == 'NoOpMeterProvider':
                    metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader]))
                else:
                    logger.warning("MeterProvider already exists, skipping metric exporter setup")
            except Exception as e:
                logger.warning(f"Failed to configure metric exporter: {e}")

        logger.info("Configured custom exporters for telemetry with optimized batching")
