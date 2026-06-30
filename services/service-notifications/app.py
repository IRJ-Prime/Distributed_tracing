"""
Service Notifications
=====================
Envoi de notifications (email, SMS, push).
Simule des goulots : external API timeout, serialization lente.
"""

import os
import time
import random
import logging
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
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.trace import SpanKind, Status, StatusCode
from pythonjsonlogger import jsonlogger

# ─── Setup ────────────────────────────────────────────────────────────────────
SERVICE_NAME_ENV = os.getenv("SERVICE_NAME", "service-notifications")
PORT = int(os.getenv("PORT", 5003))

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

notif_counter = meter.create_counter("notifications_sent_total", description="Notifications envoyées")
notif_duration = meter.create_histogram("notification_send_seconds", description="Durée envoi notification")
notif_errors = meter.create_counter("notification_errors_total", description="Erreurs de notification")

# Historique en mémoire
NOTIFICATIONS_LOG = []

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

from prometheus_flask_exporter import PrometheusMetrics
PrometheusMetrics(app).info('service_info', 'Service Notifications', version='1.0.0')


@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "service": SERVICE_NAME_ENV,
        "notifications_sent": len(NOTIFICATIONS_LOG)
    }), 200


@app.route('/notify', methods=['POST'])
def send_notification():
    """
    Envoie une notification.
    Simule :
    - Goulot 4 : Timeout API externe (SMTP/SMS gateway)
    - Goulot 5 : Sérialisation lente du payload (template rendering)
    """
    start = time.time()
    
    with tracer.start_as_current_span(
        "notifications.send",
        kind=SpanKind.SERVER,
        attributes={"messaging.system": "email"}
    ) as span:
        data = request.get_json() or {}
        order_id = data.get("order_id", "unknown")
        event = data.get("event", "order_created")
        user_id = data.get("user_id", "unknown")
        
        span.set_attribute("notification.event", event)
        span.set_attribute("notification.order_id", order_id)
        span.set_attribute("notification.user_id", user_id)
        
        logger.info(f"Envoi notification '{event}' pour commande {order_id} à utilisateur {user_id}")

        # ── Étape 1 : Rendu du template email ────────────────────────────────
        with tracer.start_as_current_span("notifications.template_render") as tmpl_span:
            tmpl_span.set_attribute("template.name", f"order_{event}.html")
            # Simulation : rendu template avec Jinja2 (lent si beaucoup de données)
            render_time = random.uniform(0.01, 0.05)
            if len(str(data)) > 200:  # Payload large = rendu lent
                render_time *= 3
                tmpl_span.set_attribute("template.slow_render", True)
                tmpl_span.add_event("slow_template_rendering", {"payload_size": len(str(data))})
            time.sleep(render_time)
            tmpl_span.set_attribute("template.render_ms", render_time * 1000)

        # ── Étape 2 : Envoi via API externe ──────────────────────────────────
        channel = random.choice(["email", "email", "sms", "push"])
        span.set_attribute("notification.channel", channel)
        
        with tracer.start_as_current_span(
            f"notifications.send_{channel}",
            attributes={
                "messaging.destination": f"{channel}-gateway",
                "messaging.system": channel,
                "peer.service": f"external-{channel}-api"
            }
        ) as send_span:
            # GOULOT 4 : Timeout API externe (20% de chance)
            external_timeout = random.random() < 0.2
            
            if external_timeout:
                timeout_duration = random.uniform(2.0, 4.0)
                send_span.set_attribute("external_api.timeout", True)
                send_span.set_attribute("external_api.timeout_ms", timeout_duration * 1000)
                send_span.add_event("external_api_slow", {
                    "provider": f"{channel}-provider",
                    "timeout_ms": timeout_duration * 1000
                })
                logger.error(f"Timeout API {channel} pour notification {order_id}: {timeout_duration:.1f}s")
                # Simulation du timeout (réduit pour le lab)
                time.sleep(min(timeout_duration, 1.5))
                
                send_span.set_status(Status(StatusCode.ERROR, f"External {channel} API timeout"))
                notif_errors.add(1, {"channel": channel, "error": "timeout"})
                
                # Fallback : file d'attente
                with tracer.start_as_current_span("notifications.queue_fallback") as queue_span:
                    queue_span.set_attribute("queue.name", "notification-retry-queue")
                    queue_span.set_attribute("queue.retry_at", "T+5min")
                    time.sleep(0.01)
                    logger.info(f"Notification {order_id} mise en file d'attente pour retry")
                
                status = "queued"
                send_span.set_status(Status(StatusCode.OK, "Queued for retry"))
            else:
                # Envoi normal
                api_latency = random.uniform(0.05, 0.2)
                time.sleep(api_latency)
                send_span.set_attribute("external_api.latency_ms", api_latency * 1000)
                send_span.set_attribute("external_api.message_id", f"msg-{random.randint(100000, 999999)}")
                send_span.set_status(Status(StatusCode.OK))
                status = "sent"

        duration = time.time() - start
        
        notif_counter.add(1, {"channel": channel, "event": event, "status": status})
        notif_duration.record(duration, {"channel": channel})
        
        result = {
            "order_id": order_id,
            "event": event,
            "channel": channel,
            "status": status,
            "duration_ms": round(duration * 1000, 2)
        }
        NOTIFICATIONS_LOG.append(result)
        
        span.set_attribute("notification.status", status)
        span.set_attribute("notification.duration_ms", duration * 1000)
        span.set_status(Status(StatusCode.OK))
        
        logger.info(f"Notification '{event}' pour {order_id}: {status} via {channel} en {duration:.3f}s")
        
        return jsonify(result), 200


@app.route('/notify/history', methods=['GET'])
def notification_history():
    """Retourne l'historique des notifications."""
    with tracer.start_as_current_span("notifications.history"):
        return jsonify({
            "total": len(NOTIFICATIONS_LOG),
            "notifications": NOTIFICATIONS_LOG[-50:]  # Dernières 50
        }), 200


if __name__ == '__main__':
    logger.info(f"Démarrage {SERVICE_NAME_ENV} sur port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
