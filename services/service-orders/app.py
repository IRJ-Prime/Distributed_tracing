"""
Service Orders
==============
Gestion des commandes. Appelle service-inventory pour réservation
et service-notifications pour confirmation.
Simule des goulots d'étranglement (N+1 queries, lock contention).
"""

import os
import time
import random
import logging
import requests
from flask import Flask, jsonify, request

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
from pythonjsonlogger import jsonlogger


# ─── Setup ───────────────────────────────────────────────────────────────────
SERVICE_NAME_ENV = os.getenv("SERVICE_NAME", "service-orders")
SERVICE_INVENTORY_URL = os.getenv("SERVICE_INVENTORY_URL", "http://service-inventory:5002")
SERVICE_NOTIFICATIONS_URL = os.getenv("SERVICE_NOTIFICATIONS_URL", "http://service-notifications:5003")
PORT = int(os.getenv("PORT", 5001))

# Logging JSON avec TraceID
logger = logging.getLogger(SERVICE_NAME_ENV)
handler = logging.StreamHandler()

class TraceIdFilter(logging.Filter):
    def filter(self, record):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            record.trace_id = format(ctx.trace_id, '032x')
            record.span_id = format(ctx.span_id, '016x')
        else:
            record.trace_id = "0" * 32
            record.span_id = "0" * 16
        record.service = SERVICE_NAME_ENV
        return True

handler.setFormatter(jsonlogger.JsonFormatter(
    '%(asctime)s %(levelname)s %(name)s %(message)s %(trace_id)s %(span_id)s %(service)s'
))
handler.addFilter(TraceIdFilter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# OpenTelemetry
resource = Resource.create({
    SERVICE_NAME: SERVICE_NAME_ENV,
    SERVICE_VERSION: "1.0.0",
    "deployment.environment": "production",
})
otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint, insecure=True))
)
trace.set_tracer_provider(tracer_provider)

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=otel_endpoint, insecure=True),
    export_interval_millis=15000
)
metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

set_global_textmap(CompositePropagator([
    TraceContextTextMapPropagator(),
    W3CBaggagePropagator(),
]))

tracer = trace.get_tracer(SERVICE_NAME_ENV)
meter = metrics.get_meter(SERVICE_NAME_ENV)

# Métriques
order_counter = meter.create_counter("orders_created_total", description="Commandes créées")
order_duration = meter.create_histogram("order_processing_seconds", description="Durée traitement commande")
db_query_duration = meter.create_histogram("db_query_seconds", description="Durée des requêtes DB simulées")

# Base de données en mémoire (simulation)
ORDERS_DB = {}

# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)
RequestsInstrumentor().instrument()

from prometheus_flask_exporter import PrometheusMetrics
PrometheusMetrics(app).info('service_info', 'Service Orders', version='1.0.0')


@app.route('/health')
def health():
    return jsonify({"status": "healthy", "service": SERVICE_NAME_ENV}), 200


@app.route('/orders', methods=['POST'])
def create_order():
    """
    Crée une commande avec simulation de goulots :
    - Scénario 1 : N+1 queries (vérification item par item)
    - Scénario 2 : Lock contention (attente verrou DB)
    - Scénario 3 : Cascade de retries
    """
    start = time.time()
    
    with tracer.start_as_current_span(
        "orders.create",
        kind=SpanKind.SERVER,
        attributes={"db.system": "postgresql", "db.operation": "INSERT"}
    ) as span:
        data = request.get_json() or {}
        order_id = data.get("order_id", f"ord-{random.randint(1000,9999)}")
        items = data.get("items", [{"product_id": "P001", "qty": 1}])
        
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.items_count", len(items))
        span.set_attribute("customer.id", data.get("user_id", "unknown"))
        
        logger.info(f"Traitement commande {order_id} avec {len(items)} articles")

        # ── Simulation : requête DB d'insertion ───────────────────────────────
        with tracer.start_as_current_span(
            "orders.db.insert",
            attributes={"db.statement": "INSERT INTO orders VALUES (?)", "db.system": "postgresql"}
        ) as db_span:
            db_latency = simulate_db_query("insert_order")
            db_span.set_attribute("db.duration_ms", db_latency * 1000)
            db_query_duration.record(db_latency, {"operation": "insert", "table": "orders"})

        # ── Simulation goulot N+1 : vérif article par article ─────────────────
        scenario = random.choice(["normal", "n_plus_one", "lock_contention", "normal", "retry_storm"])
        span.set_attribute("latency.scenario", scenario)

        if scenario == "n_plus_one":
            # GOULOT 1 : N appels DB au lieu d'un seul batch
            span.add_event("n_plus_one_detected", {"items_count": len(items)})
            logger.warning(f"Goulot N+1 détecté pour commande {order_id}")
            with tracer.start_as_current_span("orders.db.n_plus_one_queries") as n1_span:
                n1_span.set_attribute("antipattern", "n_plus_one")
                n1_span.set_attribute("queries_count", len(items))
                for i, item in enumerate(items):
                    with tracer.start_as_current_span(f"orders.db.check_item_{i}") as item_span:
                        item_span.set_attribute("product.id", item.get("product_id", f"P{i}"))
                        time.sleep(random.uniform(0.05, 0.15))  # Chaque query = 50-150ms

        elif scenario == "lock_contention":
            # GOULOT 2 : Attente de verrou DB
            wait = random.uniform(0.3, 0.8)
            span.add_event("lock_wait_started", {"expected_wait_ms": wait * 1000})
            logger.warning(f"Attente verrou DB : {wait:.2f}s pour commande {order_id}")
            with tracer.start_as_current_span("orders.db.lock_wait") as lock_span:
                lock_span.set_attribute("lock.type", "row_exclusive")
                lock_span.set_attribute("lock.wait_ms", wait * 1000)
                time.sleep(wait)
            span.add_event("lock_acquired")

        elif scenario == "retry_storm":
            # GOULOT 3 : Retries en cascade sur service-notifications
            span.add_event("retry_storm_started", {"max_retries": 3})
            logger.warning(f"Retry storm détecté pour notifications de commande {order_id}")
            for attempt in range(3):
                with tracer.start_as_current_span(f"orders.notify.retry_{attempt}") as retry_span:
                    retry_span.set_attribute("retry.attempt", attempt)
                    time.sleep(0.1 * (2 ** attempt))  # Backoff exponentiel
                    if attempt < 2:
                        retry_span.set_status(Status(StatusCode.ERROR, "Timeout"))

        # ── Réservation inventaire ─────────────────────────────────────────────
        with tracer.start_as_current_span("orders.reserve_inventory") as inv_span:
            inv_span.set_attribute("downstream.service", "service-inventory")
            try:
                inv_response = requests.post(
                    f"{SERVICE_INVENTORY_URL}/inventory/reserve",
                    json={"order_id": order_id, "items": items},
                    timeout=5
                )
                inv_span.set_attribute("downstream.status", inv_response.status_code)
            except Exception as e:
                inv_span.record_exception(e)
                inv_span.set_status(Status(StatusCode.ERROR, str(e)))

        # ── Notification ───────────────────────────────────────────────────────
        with tracer.start_as_current_span("orders.send_notification") as notif_span:
            notif_span.set_attribute("downstream.service", "service-notifications")
            try:
                requests.post(
                    f"{SERVICE_NOTIFICATIONS_URL}/notify",
                    json={"order_id": order_id, "event": "order_created", "user_id": data.get("user_id")},
                    timeout=3
                )
            except Exception as e:
                notif_span.record_exception(e)
                logger.warning(f"Notification échouée pour {order_id}: {e}")

        # Sauvegarde en mémoire
        duration = time.time() - start
        ORDERS_DB[order_id] = {
            "order_id": order_id,
            "status": "confirmed",
            "items": items,
            "scenario": scenario,
            "processing_time_ms": round(duration * 1000, 2)
        }
        
        order_counter.add(1, {"scenario": scenario})
        order_duration.record(duration, {"scenario": scenario})
        
        span.set_attribute("processing.duration_ms", duration * 1000)
        span.set_status(Status(StatusCode.OK))
        
        logger.info(f"Commande {order_id} traitée en {duration:.3f}s (scénario: {scenario})")
        
        return jsonify(ORDERS_DB[order_id]), 201


@app.route('/orders/<order_id>', methods=['GET'])
def get_order(order_id):
    """Récupère une commande par ID."""
    with tracer.start_as_current_span(
        "orders.get",
        kind=SpanKind.SERVER,
        attributes={"order.id": order_id, "db.operation": "SELECT"}
    ) as span:
        # Simulation latence DB lecture
        with tracer.start_as_current_span("orders.db.select") as db_span:
            db_latency = simulate_db_query("select_order")
            db_span.set_attribute("db.duration_ms", db_latency * 1000)
        
        order = ORDERS_DB.get(order_id)
        if not order:
            span.set_status(Status(StatusCode.ERROR, "Not found"))
            return jsonify({"error": f"Commande {order_id} non trouvée"}), 404
        
        return jsonify(order), 200


def simulate_db_query(operation: str) -> float:
    """Simule une latence de requête base de données."""
    base_latency = {
        "insert_order": random.uniform(0.01, 0.05),
        "select_order": random.uniform(0.005, 0.02),
        "update_stock": random.uniform(0.008, 0.03),
    }.get(operation, 0.01)
    
    # Spike occasionnel (connection pool saturé)
    if random.random() < 0.05:
        base_latency *= 10
    
    time.sleep(base_latency)
    return base_latency


if __name__ == '__main__':
    logger.info(f"Démarrage {SERVICE_NAME_ENV} sur port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
