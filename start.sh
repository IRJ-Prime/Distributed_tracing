#!/bin/bash
# =============================================================================
# TP Distributed Tracing - Script de démarrage
# IAI Gabon ING2 2025-2026 - IRJ & IJS
# =============================================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

print_banner() {
cat << 'EOF'
╔══════════════════════════════════════════════════════════════════╗
║    Projet 03 - Distributed Tracing avec OpenTelemetry            ║
║    Grafana Tempo + Loki + Prometheus                             ║
║    IAI Gabon ING2 2025-2026 - IRJ & IJS                          ║
╚══════════════════════════════════════════════════════════════════╝
EOF
}

print_banner

echo ""
echo -e "${BOLD}Vérification des prérequis...${NC}"

# Docker
if ! command -v docker &>/dev/null; then
    echo -e "${RED}❌ Docker non installé. Installer avec :${NC}"
    echo "   curl -fsSL https://get.docker.com | bash"
    echo "   sudo usermod -aG docker $USER && newgrp docker"
    exit 1
fi
echo -e "${GREEN}✅ Docker : $(docker --version | cut -d' ' -f3 | tr -d ',')${NC}"

# Docker Compose
if ! docker compose version &>/dev/null 2>&1; then
    echo -e "${RED}❌ Docker Compose plugin non installé. Installer avec :${NC}"
    echo "   sudo apt-get install docker-compose-plugin"
    exit 1
fi
echo -e "${GREEN}✅ Docker Compose : $(docker compose version --short)${NC}"

# Ressources
TOTAL_MEM=$(free -m | awk 'NR==2{print $2}')
if [ "$TOTAL_MEM" -lt 3000 ]; then
    echo -e "${YELLOW}⚠️  RAM disponible : ${TOTAL_MEM}MB (recommandé : 4GB+)${NC}"
    echo -e "${YELLOW}   La stack peut être lente. Continuer ? (Ctrl+C pour annuler)${NC}"
    sleep 3
else
    echo -e "${GREEN}✅ RAM disponible : ${TOTAL_MEM}MB${NC}"
fi

# Espace disque
FREE_DISK=$(df -BG . | awk 'NR==2{print $4}' | tr -d 'G')
if [ "$FREE_DISK" -lt 5 ]; then
    echo -e "${RED}❌ Espace disque insuffisant : ${FREE_DISK}GB (minimum 5GB)${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Espace disque : ${FREE_DISK}GB disponible${NC}"

echo ""
echo -e "${BOLD}Démarrage de la stack...${NC}"

# Pull des images d'abord
echo -e "${BLUE}📥 Téléchargement des images Docker...${NC}"
docker compose pull --quiet 2>/dev/null || true

# Build des microservices
echo -e "${BLUE}🔨 Build des microservices Python/Flask...${NC}"
docker compose build --parallel

# Démarrage
echo -e "${BLUE}🚀 Démarrage de tous les services...${NC}"
docker compose up -d

echo ""
echo -e "${BOLD}Attente de la disponibilité des services...${NC}"

wait_for_service() {
    local name=$1
    local url=$2
    local max_wait=${3:-60}
    local count=0
    
    printf "  %-30s " "$name"
    while ! curl -sf "$url" > /dev/null 2>&1; do
        sleep 2
        count=$((count + 2))
        if [ $count -ge $max_wait ]; then
            echo -e "${RED}TIMEOUT${NC}"
            return 1
        fi
        printf "."
    done
    echo -e " ${GREEN}✅ OK${NC}"
}

wait_for_service "Tempo"                "http://localhost:3200/ready"         90
wait_for_service "Loki"                 "http://localhost:3100/ready"         60
wait_for_service "Prometheus"           "http://localhost:9090/-/ready"       60
wait_for_service "Grafana"              "http://localhost:3000/api/health"    90
wait_for_service "service-api"          "http://localhost:5000/health"        60
wait_for_service "service-orders"       "http://localhost:5001/health"        60
wait_for_service "service-inventory"    "http://localhost:5002/health"        60
wait_for_service "service-notifications" "http://localhost:5003/health"       60

echo ""
cat << EOF
${BOLD}╔══════════════════════════════════════════════════════════════════╗
║                  STACK DÉMARRÉE AVEC SUCCÈS ✅                  ║
╠══════════════════════════════════════════════════════════════════╣
║  INTERFACES WEB                                                  ║
║  ─────────────────────────────────────────────────────────────   ║
║  Grafana (principal)  →  http://localhost:3000                   ║
║                           Login: admin / admin123                ║
║  Prometheus           →  http://localhost:9090                   ║
║  Tempo API            →  http://localhost:3200                   ║
║  Loki API             →  http://localhost:3100                   ║
║                                                                  ║
║  MICROSERVICES                                                   ║
║  ─────────────────────────────────────────────────────────────   ║
║  service-api          →  http://localhost:5000                   ║
║  service-orders       →  http://localhost:5001                   ║
║  service-inventory    →  http://localhost:5002                   ║
║  service-notifications →  http://localhost:5003                  ║
╠══════════════════════════════════════════════════════════════════╣
║  GÉNÉRATION DE TRAFIC (dans un autre terminal)                   ║
║  ─────────────────────────────────────────────────────────────   ║
║  pip3 install requests python-json-logger                        ║
║  python3 test_load.py --duration 300 --rate 2                    ║
╠══════════════════════════════════════════════════════════════════╣
║  NAVIGATION DANS GRAFANA                                         ║
║  ─────────────────────────────────────────────────────────────   ║
║  1. Dashboards → TP Distributed Tracing → RED Dashboard          ║
║  2. Explore → Tempo → Rechercher traces avec durée > 500ms       ║
║     TraceQL: { duration > 500ms }                                ║
║  3. Explore → Loki → Filtrer par service et trace_id             ║
║  4. Cliquer sur TraceID dans les logs → Ouvre Tempo              ║
╚══════════════════════════════════════════════════════════════════╝
EOF

echo ""
echo -e "${BOLD}Logs en temps réel :${NC} docker compose logs -f"
echo -e "${BOLD}Arrêter la stack  :${NC} docker compose down"
echo -e "${BOLD}Supprimer tout    :${NC} docker compose down -v"
echo ""
