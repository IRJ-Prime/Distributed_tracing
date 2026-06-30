"""
Service Inventory
=================
Gestion du stock des produits.
Simule des goulots : full table scan, index manquant, slow aggregation.
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

# ─── Setup ────────────────────────────────────────────────────────────────────
SERVICE_NAME_ENV = os.getenv("SERVICE_NAME", "service-inventory")
PORT = int(os.getenv("PORT", 5002))

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

otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
resource = Resource.create({SERVICE_NAME: SERVICE_NAME_ENV, SERVICE_VERSION: "1.0.0"})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint, insecure=True)))
trace.set_tracer_provider(tracer_provider)

meter_provider = MeterProvider(resource=resource, metric_readers=[
    PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=otel_endpoint, insecure=True), export_interval_millis=15000)
])
metrics.set_meter_provider(meter_provider)
set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

tracer = trace.get_tracer(SERVICE_NAME_ENV)
meter = metrics.get_meter(SERVICE_NAME_ENV)

stock_check_duration = meter.create_histogram("inventory_check_seconds", description="Durée vérification stock")
reservation_counter = meter.create_counter("inventory_reservations_total", description="Réservations de stock")
low_stock_gauge = meter.create_up_down_counter("inventory_low_stock_items", description="Articles en stock faible")

# Catalogue produits (simulation)
PRODUCTS = {
    f"P{i:03d}": {
        "name": f"Produit {i}",
        "stock": random.randint(0, 100),
        "price": round(random.uniform(5, 500), 2),
        "category": random.choice(["electronics", "clothing", "food", "books"])
    }
    for i in range(1, 51)
}

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)
RequestsInstrumentor().instrument()

from prometheus_flask_exporter import PrometheusMetrics
PrometheusMetrics(app).info('service_info', 'Service Inventory', version='1.0.0')


@app.route('/health')
def health():
    return jsonify({"status": "healthy", "service": SERVICE_NAME_ENV, "products": len(PRODUCTS)}), 200


@app.route('/inventory/check', methods=['POST'])
def check_inventory():
    """
    Vérifie la disponibilité des articles.
    Simule goulots : full table scan sans index.
    """
    start = time.time()
    
    with tracer.start_as_current_span(
        "inventory.check",
        kind=SpanKind.SERVER,
        attributes={"db.operation": "SELECT", "db.system": "postgresql"}
    ) as span:
        data = request.get_json() or {}
        items = data.get("items", [{"product_id": "P001", "qty": 1}])
        
        span.set_attribute("items.count", len(items))
        
        # ── Simulation goulot : Full Table Scan ───────────────────────────────
        scan_type = random.choice(["indexed", "indexed", "full_scan", "indexed"])
        span.set_attribute("db.scan_type", scan_type)
        
        if scan_type == "full_scan":
            # Sans index : scan de toute la table
            with tracer.start_as_current_span("inventory.db.full_table_scan") as scan_span:
                scan_span.set_attribute("db.statement", "SELECT * FROM products WHERE product_id = ?")
                scan_span.set_attribute("antipattern", "missing_index")
                scan_span.set_attribute("rows_scanned", len(PRODUCTS))
                # Simulation : linéaire en fonction du nombre de produits
                time.sleep(len(PRODUCTS) * 0.002)
                logger.warning("Full table scan détecté - index manquant sur product_id")
                scan_span.set_status(Status(StatusCode.OK))
                span.add_event("slow_query_detected", {
                    "query": "SELECT * FROM products WHERE product_id = ?",
                    "rows_scanned": len(PRODUCTS),
                    "recommendation": "Ajouter INDEX(product_id)"
                })
        else:
            # Avec index : rapide
            with tracer.start_as_current_span("inventory.db.index_scan") as scan_span:
                scan_span.set_attribute("db.statement", "SELECT * FROM products USE INDEX(pk) WHERE product_id = ?")
                time.sleep(random.uniform(0.002, 0.01))

        # Vérification du stock
        results = []
        available = True
        for item in items:
            pid = item.get("product_id", "P001")
            qty = item.get("qty", 1)
            product = PRODUCTS.get(pid, {"stock": 50, "name": pid})
            
            item_available = product["stock"] >= qty
            if not item_available:
                available = False
                span.add_event("insufficient_stock", {"product_id": pid, "requested": qty, "available": product["stock"]})
                logger.warning(f"Stock insuffisant pour {pid}: demandé={qty}, disponible={product['stock']}")
            
            results.append({
                "product_id": pid,
                "requested": qty,
                "available": product["stock"],
                "ok": item_available
            })

        duration = time.time() - start
        stock_check_duration.record(duration, {"scan_type": scan_type})
        span.set_attribute("check.duration_ms", duration * 1000)
        span.set_attribute("check.all_available", available)
        
        if not available:
            span.set_status(Status(StatusCode.ERROR, "Stock insuffisant"))
            return jsonify({"available": False, "items": results}), 409
        
        return jsonify({"available": True, "items": results}), 200


@app.route('/inventory/reserve', methods=['POST'])
def reserve_inventory():
    """Réserve des articles pour une commande."""
    with tracer.start_as_current_span(
        "inventory.reserve",
        kind=SpanKind.SERVER,
        attributes={"db.operation": "UPDATE"}
    ) as span:
        data = request.get_json() or {}
        order_id = data.get("order_id", "unknown")
        items = data.get("items", [])
        
        span.set_attribute("order.id", order_id)
        
        # Simulation UPDATE avec transaction
        with tracer.start_as_current_span("inventory.db.update_stock") as update_span:
            update_span.set_attribute("db.statement", "UPDATE products SET stock = stock - ? WHERE product_id = ?")
            
            # Latence aléatoire : contention de transaction
            if random.random() < 0.2:
                wait = random.uniform(0.1, 0.4)
                update_span.set_attribute("transaction.contention_wait_ms", wait * 1000)
                update_span.add_event("transaction_contention", {"wait_ms": wait * 1000})
                time.sleep(wait)
            else:
                time.sleep(random.uniform(0.01, 0.03))

        reserved = []
        for item in items:
            pid = item.get("product_id", "P001")
            qty = item.get("qty", 1)
            if pid in PRODUCTS:
                PRODUCTS[pid]["stock"] = max(0, PRODUCTS[pid]["stock"] - qty)
                if PRODUCTS[pid]["stock"] < 5:
                    low_stock_gauge.add(1, {"product_id": pid})
                    logger.warning(f"Stock faible pour {pid}: {PRODUCTS[pid]['stock']} restants")
            reserved.append({"product_id": pid, "reserved": qty})

        reservation_counter.add(1, {"order_id": order_id})
        span.set_status(Status(StatusCode.OK))
        
        logger.info(f"Réservation effectuée pour commande {order_id}: {len(reserved)} articles")
        return jsonify({"order_id": order_id, "reserved": reserved, "status": "reserved"}), 200


@app.route('/inventory/products', methods=['GET'])
def list_products():
    """Liste tous les produits avec leur stock."""
    with tracer.start_as_current_span("inventory.list_products") as span:
        # Simulation : aggregation lente
        with tracer.start_as_current_span("inventory.db.aggregate") as agg_span:
            agg_span.set_attribute("db.statement", "SELECT category, COUNT(*), SUM(stock) FROM products GROUP BY category")
            time.sleep(random.uniform(0.02, 0.08))  # Aggregation coûteuse
        
        products_list = [
            {**{"product_id": pid}, **info}
            for pid, info in list(PRODUCTS.items())[:20]  # Pagination
        ]
        span.set_attribute("products.returned", len(products_list))
        return jsonify({"products": products_list, "total": len(PRODUCTS)}), 200


if __name__ == '__main__':
    logger.info(f"Démarrage {SERVICE_NAME_ENV} sur port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
