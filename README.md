# Projet 03 - Distributed Tracing avec OpenTelemetry, Tempo et Grafana

**IAI Gabon · ING II Administration Réseaux & Cybersécurité · 2025-2026**  
**Équipe :** ILOUMBOU Roland Junior & ISSAMBOU Jean Samuel ( Lien d'ISSAMBOU Jean Samuel [@Sam-10-Ph](https://github.com/Sam-10-Ph))

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     CLIENT (curl / test_load.py)                │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  MICROSERVICES (Flask + OpenTelemetry SDK)                       │
│                                                                  │
│  service-api :5000 ──► service-orders :5001 ──► service-notifs :5003
│        │                     │                                   │
│        └──────► service-inventory :5002 ◄────────┘              │
└─────────────────────────┬────────────────────────────────────────┘
                          │ OTLP gRPC (:4317)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  OTel Collector (receivers → processors → exporters)            │
└──────┬──────────────────┬──────────────────────────────────────┘
       │ traces OTLP      │ metrics remote_write
       ▼                  ▼
  Grafana Tempo     Prometheus :9090      Loki :3100 ◄── Promtail
     :3200                                (logs Docker)
       │                  │                    │
       └──────────────────┴────────────────────┘
                          │
                    Grafana :3000
              (Explore · Dashboards · Service Graph)
```

## Démarrage rapide

```bash
# 1. Prérequis : Docker + Docker Compose Plugin
curl -fsSL https://get.docker.com | bash
sudo usermod -aG docker $USER && newgrp docker

# 2. Démarrer la stack
chmod +x start.sh && ./start.sh

# 3. Générer du trafic (dans un autre terminal)
pip3 install requests python-json-logger
python3 test_load.py --duration 300 --rate 2

# 4. Ouvrir Grafana
# http://localhost:3000  →  admin / admin123
```

## Accès aux interfaces

| Interface | URL | Identifiants |
|-----------|-----|-------------|
| Grafana | http://localhost:3000 | admin / admin123 |
| Prometheus | http://localhost:9090 | — |
| Tempo API | http://localhost:3200 | — |
| Loki API | http://localhost:3100 | — |
| service-api | http://localhost:5000 | — |
| service-orders | http://localhost:5001 | — |
| service-inventory | http://localhost:5002 | — |
| service-notifications | http://localhost:5003 | — |

## Stack technique

| Composant | Version | Rôle |
|-----------|---------|------|
| OpenTelemetry SDK Python | 1.23.0 | Instrumentation des services |
| OTel Collector Contrib | 0.95.0 | Routage traces/métriques |
| Grafana Tempo | 2.4.1 | Backend traces (remplace Jaeger) |
| Grafana Loki | 2.9.4 | Backend logs |
| Promtail | 2.9.4 | Agent collecte logs Docker |
| Prometheus | 2.50.1 | Backend métriques |
| Grafana | 10.4.0 | Interface unifiée d'observabilité |
| Python/Flask | 3.11 / 3.0 | Microservices |

## Navigation dans Grafana (exercices TP)

### 1. Explorer les traces (Tempo)
```
Explore → Tempo → TraceQL
{ duration > 500ms }                          # Traces lentes
{ name =~ "orders.*" && duration > 200ms }   # Spans orders lents
{ .latency.scenario = "n_plus_one" }          # Scénario N+1
```

### 2. Explorer les logs (Loki)
```
Explore → Loki → LogQL
{service="service-orders"} | json | level="WARNING"
{service=~"service-.*"} | json | trace_id != ""
{service="service-notifications"} | json | message =~ ".*Timeout.*"
```

### 3. Navigation croisée
- **Trace → Log** : Dans Tempo, cliquer sur un span → bouton "Logs for this span"
- **Log → Trace** : Dans Loki, cliquer sur le TraceID surligné → ouvre Tempo
- **Métrique → Trace** : Dans Prometheus, hover sur un point avec exemplar → lien Tempo

## Scénarios de latence (L4)

| # | Scénario | Service | TraceQL de recherche |
|---|----------|---------|---------------------|
| 1 | N+1 Queries | service-orders | `{ .latency.scenario = "n_plus_one" }` |
| 2 | Lock Contention | service-orders | `{ .latency.scenario = "lock_contention" }` |
| 3 | Retry Storm | service-orders | `{ .latency.scenario = "retry_storm" }` |
| 4 | External API Timeout | service-notifications | `{ .notification.status = "queued" }` |
| 5 | Full Table Scan | service-inventory | `{ .db.scan_type = "full_scan" }` |

## Structure du projet

```
tp-tracing/
├── docker-compose.yml              # Stack complète
├── start.sh                        # Script de démarrage
├── test_load.py                    # Générateur de trafic
├── Rapport_L1_Architecture.docx    # Livrable L1
├── otelcol/config.yaml             # OTel Collector
├── tempo/config.yaml               # Grafana Tempo
├── loki/config.yaml                # Grafana Loki
├── promtail/config.yaml            # Promtail (agent logs)
├── prometheus/prometheus.yml       # Prometheus scrape config
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/            # Tempo + Loki + Prometheus auto-provisionnés
│   │   └── dashboards/             # Dossier dashboards auto-chargés
│   └── dashboards/
│       └── red-dashboard.json      # Dashboard RED (Livrable L3)
└── services/
    ├── service-api/                # Gateway (port 5000)
    ├── service-orders/             # Commandes (port 5001)
    ├── service-inventory/          # Stock (port 5002)
    └── service-notifications/      # Notifications (port 5003)
```

## Commandes utiles

```bash
docker compose up -d                    # Démarrer
docker compose down                     # Arrêter
docker compose down -v                  # Arrêter + supprimer volumes
docker compose logs -f service-api      # Logs d'un service
docker compose restart grafana          # Redémarrer Grafana
docker stats                            # Utilisation ressources
```

## Dépannage

**Tempo ne reçoit pas de traces :**
```bash
# Vérifier que le collector tourne
docker compose logs otel-collector | grep -i error
# Tester l'endpoint OTLP
curl http://localhost:4318/v1/traces
```

**Grafana ne voit pas les datasources :**
```bash
# Recharger le provisioning
docker compose restart grafana
# Vérifier les fichiers de config
docker compose exec grafana cat /etc/grafana/provisioning/datasources/datasources.yaml
```

**Service ne démarre pas :**
```bash
docker compose logs service-api
# Rebuilder si code modifié
docker compose build service-api && docker compose up -d service-api
```
