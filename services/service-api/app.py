"""
Service API Gateway
===================
Point d'entrée de l'architecture microservices.
Instrumentation complète OpenTelemetry avec propagation W3C TraceContext.

Architecture : service-api -> service-orders -> service-inventory
                           -> service-notifications
"""

import os
import time
import random
import logging
import requests
from flask import Flask, jsonify, request, g

# ─── OpenTelemetry : imports ───────────────────────────────────────────────────
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.trace import SpanKind, Status, StatusCode

# ─── Logging structuré JSON avec TraceID ──────────────────────────────────────
from pythonjsonlogger import jsonlogger


def setup_logging(service_name: str) -> logging.Logger:
    """Configure le logging JSON avec injection automatique du TraceID."""
    logger = logging.getLogger(service_name)
    handler = logging.StreamHandler()

    class TraceIdFilter(logging.Filter):
        """Injecte trace_id et span_id dans chaque log record."""
        def filter(self, record):
            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx.is_valid:
                record.trace_id = format(ctx.trace_id, '032x')
                record.span_id = format(ctx.span_id, '016x')
            else:
                record.trace_id = "0" * 32
                record.span_id = "0" * 16
            record.service = service_name
            return True

    formatter = jsonlogger.JsonFormatter(
        fmt='%(asctime)s %(levelname)s %(name)s %(message)s %(trace_id)s %(span_id)s %(service)s',
        datefmt='%Y-%m-%dT%H:%M:%S'
    )
    handler.setFormatter(formatter)
    handler.addFilter(TraceIdFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def setup_opentelemetry(service_name: str, service_version: str = "1.0.0"):
    """
    Initialise l'instrumentation OpenTelemetry complète.
    - TracerProvider avec export OTLP vers l'OTel Collector
    - MeterProvider pour les métriques
    - Propagation W3C TraceContext (standard inter-services)
    """
    # Ressource identifiant le service
    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
        "deployment.environment": os.getenv("DEPLOYMENT_ENV", "production"),
        "host.name": os.uname().nodename,
    })

    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    # ── Traces ────────────────────────────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    otlp_trace_exporter = OTLPSpanExporter(
        endpoint=otel_endpoint,
        insecure=True
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(otlp_trace_exporter)
    )
    trace.set_tracer_provider(tracer_provider)

    # ── Métriques ─────────────────────────────────────────────────────────────
    otlp_metric_exporter = OTLPMetricExporter(
        endpoint=otel_endpoint,
        insecure=True
    )
    metric_reader = PeriodicExportingMetricReader(
        otlp_metric_exporter,
        export_interval_millis=15000  # Export toutes les 15s
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # ── Propagation W3C TraceContext (standard CNCF) ──────────────────────────
    set_global_textmap(CompositePropagator([
        TraceContextTextMapPropagator(),   # W3C traceparent header
        W3CBaggagePropagator(),            # W3C baggage header
    ]))

    return trace.get_tracer(service_name), metrics.get_meter(service_name)


# ─── Initialisation ───────────────────────────────────────────────────────────
SERVICE_NAME_ENV = os.getenv("SERVICE_NAME", "service-api")
SERVICE_ORDERS_URL = os.getenv("SERVICE_ORDERS_URL", "http://service-orders:5001")
SERVICE_INVENTORY_URL = os.getenv("SERVICE_INVENTORY_URL", "http://service-inventory:5002")
PORT = int(os.getenv("PORT", 5000))

logger = setup_logging(SERVICE_NAME_ENV)
tracer, meter = setup_opentelemetry(SERVICE_NAME_ENV)

# ─── Métriques personnalisées ──────────────────────────────────────────────────
request_counter = meter.create_counter(
    "http_requests_total",
    description="Nombre total de requêtes HTTP reçues",
    unit="1"
)
request_duration = meter.create_histogram(
    "http_request_duration_seconds",
    description="Durée des requêtes HTTP en secondes",
    unit="s"
)
error_counter = meter.create_counter(
    "http_errors_total",
    description="Nombre total d'erreurs HTTP",
    unit="1"
)

# ─── Application Flask ────────────────────────────────────────────────────────
app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)
RequestsInstrumentor().instrument()

# Métriques Prometheus exposées sur /metrics
from prometheus_flask_exporter import PrometheusMetrics
prom_metrics = PrometheusMetrics(app)
prom_metrics.info('service_info', 'Service API Gateway', version='1.0.0')


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    """Endpoint de santé pour Docker healthcheck."""
    return jsonify({"status": "healthy", "service": SERVICE_NAME_ENV}), 200


@app.route('/api/orders', methods=['POST'])
def create_order():
    """
    Crée une commande : appelle service-orders puis service-inventory.
    Simule différents scénarios de latence pour le TP.
    """
    start = time.time()
    
    with tracer.start_as_current_span(
        "api.create_order",
        kind=SpanKind.SERVER,
        attributes={
            "http.method": request.method,
            "http.url": request.url,
            "user.id": request.json.get("user_id", "anonymous") if request.json else "anonymous",
        }
    ) as span:
        try:
            data = request.get_json() or {}
            order_id = data.get("order_id", f"ord-{random.randint(1000, 9999)}")
            
            # Attribut custom : identifiant de commande
            span.set_attribute("order.id", order_id)
            span.set_attribute("order.items_count", len(data.get("items", [])))
            
            logger.info(f"Création commande {order_id}", extra={"order_id": order_id})

            # ── Étape 1 : Vérification inventaire ─────────────────────────────
            with tracer.start_as_current_span("api.check_inventory") as inv_span:
                inv_span.set_attribute("downstream.service", "service-inventory")
                inv_response = requests.post(
                    f"{SERVICE_INVENTORY_URL}/inventory/check",
                    json={"items": data.get("items", [])},
                    timeout=5
                )
                inv_span.set_attribute("downstream.status_code", inv_response.status_code)
                
                if inv_response.status_code != 200:
                    inv_span.set_status(Status(StatusCode.ERROR, "Inventaire insuffisant"))
                    raise Exception("Inventaire insuffisant")

            # ── Étape 2 : Création de la commande ─────────────────────────────
            with tracer.start_as_current_span("api.forward_to_orders") as ord_span:
                ord_span.set_attribute("downstream.service", "service-orders")
                ord_response = requests.post(
                    f"{SERVICE_ORDERS_URL}/orders",
                    json={**data, "order_id": order_id},
                    timeout=10
                )
                ord_span.set_attribute("downstream.status_code", ord_response.status_code)

            duration = time.time() - start
            request_counter.add(1, {"service": SERVICE_NAME_ENV, "endpoint": "/api/orders", "status": "success"})
            request_duration.record(duration, {"service": SERVICE_NAME_ENV, "endpoint": "/api/orders"})
            
            span.set_attribute("response.duration_ms", duration * 1000)
            span.set_status(Status(StatusCode.OK))
            
            logger.info(f"Commande {order_id} créée en {duration:.3f}s")
            
            return jsonify({
                "order_id": order_id,
                "status": "created",
                "duration_ms": round(duration * 1000, 2),
                "inventory": inv_response.json(),
                "order": ord_response.json()
            }), 201

        except requests.exceptions.Timeout as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, "Timeout downstream"))
            error_counter.add(1, {"service": SERVICE_NAME_ENV, "error_type": "timeout"})
            logger.error(f"Timeout lors de la création de commande: {e}")
            return jsonify({"error": "Service timeout", "detail": str(e)}), 504

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            error_counter.add(1, {"service": SERVICE_NAME_ENV, "error_type": "internal"})
            logger.error(f"Erreur création commande: {e}")
            return jsonify({"error": str(e)}), 500


@app.route('/api/orders/<order_id>', methods=['GET'])
def get_order(order_id):
    """Récupère le détail d'une commande."""
    with tracer.start_as_current_span(
        "api.get_order",
        kind=SpanKind.SERVER,
        attributes={"order.id": order_id}
    ) as span:
        try:
            # Simulation de latence aléatoire (scénario TP)
            latency_scenario = random.choice(["normal", "normal", "slow", "very_slow", "normal"])
            if latency_scenario == "slow":
                span.add_event("latency_detected", {"scenario": "slow", "extra_ms": 200})
                time.sleep(0.2)
            elif latency_scenario == "very_slow":
                span.add_event("latency_detected", {"scenario": "very_slow", "extra_ms": 800})
                time.sleep(0.8)

            response = requests.get(
                f"{SERVICE_ORDERS_URL}/orders/{order_id}",
                timeout=5
            )
            span.set_attribute("response.status_code", response.status_code)
            
            return jsonify(response.json()), response.status_code

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            return jsonify({"error": str(e)}), 500


@app.route('/api/products', methods=['GET'])
def list_products():
    """Liste les produits disponibles via service-inventory."""
    with tracer.start_as_current_span("api.list_products", kind=SpanKind.SERVER) as span:
        response = requests.get(f"{SERVICE_INVENTORY_URL}/inventory/products", timeout=5)
        span.set_attribute("products.count", len(response.json().get("products", [])))
        return jsonify(response.json()), response.status_code


if __name__ == '__main__':
    logger.info(f"Démarrage {SERVICE_NAME_ENV} sur port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
