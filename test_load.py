#!/usr/bin/env python3
"""
Script de test de charge - TP Distributed Tracing
===================================================
Génère du trafic sur les 4 microservices pour produire
des traces visibles dans Grafana Tempo.

Couvre les 5 scénarios de latence demandés dans le TP :
  1. N+1 queries (service-orders)
  2. Lock contention DB (service-orders)
  3. Retry storm (service-orders -> service-notifications)
  4. External API timeout (service-notifications)
  5. Full table scan sans index (service-inventory)

Usage :
    python test_load.py [--url http://localhost:5000] [--duration 120] [--rate 2]
"""

import time
import random
import argparse
import requests
import threading
from datetime import datetime

# Couleurs terminal
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def log(msg, color=RESET):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{RESET}")


def create_order(base_url: str, order_num: int):
    """Envoie une requête de création de commande."""
    items_count = random.randint(1, 5)
    payload = {
        "order_id": f"ord-test-{order_num:04d}",
        "user_id": f"user-{random.randint(1, 100)}",
        "items": [
            {
                "product_id": f"P{random.randint(1, 50):03d}",
                "qty": random.randint(1, 3)
            }
            for _ in range(items_count)
        ]
    }
    
    try:
        start = time.time()
        r = requests.post(f"{base_url}/api/orders", json=payload, timeout=15)
        duration = time.time() - start
        
        if r.status_code == 201:
            data = r.json()
            log(f"✅ Commande {payload['order_id']} créée en {duration:.2f}s "
                f"(scénario: {data.get('order', {}).get('scenario', '?')})", GREEN)
        elif r.status_code == 409:
            log(f"⚠️  Commande {payload['order_id']}: stock insuffisant ({duration:.2f}s)", YELLOW)
        else:
            log(f"❌ Commande {payload['order_id']}: HTTP {r.status_code} ({duration:.2f}s)", RED)
            
        return r.status_code, duration
        
    except requests.exceptions.Timeout:
        log(f"⏱️  Timeout pour commande {payload['order_id']}", RED)
        return 504, 15.0
    except Exception as e:
        log(f"💥 Erreur: {e}", RED)
        return 500, 0


def get_order(base_url: str, order_num: int):
    """Récupère une commande existante."""
    order_id = f"ord-test-{random.randint(1, max(1, order_num - 1)):04d}"
    try:
        r = requests.get(f"{base_url}/api/orders/{order_id}", timeout=5)
        return r.status_code
    except Exception:
        return 500


def list_products(base_url: str):
    """Liste les produits (charge sur service-inventory)."""
    try:
        r = requests.get(f"{base_url}/api/products", timeout=5)
        if r.status_code == 200:
            count = r.json().get("total", 0)
            log(f"📦 Produits listés: {count} au total", BLUE)
        return r.status_code
    except Exception:
        return 500


def check_health(base_url: str):
    """Vérifie la santé de tous les services."""
    services = {
        "service-api": f"{base_url}/health",
        "service-orders": base_url.replace("5000", "5001") + "/health",
        "service-inventory": base_url.replace("5000", "5002") + "/health",
        "service-notifications": base_url.replace("5000", "5003") + "/health",
    }
    
    print(f"\n{BOLD}=== Vérification de santé des services ==={RESET}")
    all_healthy = True
    
    for name, url in services.items():
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                log(f"✅ {name}: OK", GREEN)
            else:
                log(f"❌ {name}: HTTP {r.status_code}", RED)
                all_healthy = False
        except Exception as e:
            log(f"❌ {name}: Inaccessible ({e})", RED)
            all_healthy = False
    
    return all_healthy


def run_scenario(base_url: str, scenario_name: str, count: int = 5):
    """Exécute un scénario de test spécifique."""
    log(f"\n{BOLD}▶ Scénario : {scenario_name}{RESET}", YELLOW)
    
    for i in range(count):
        create_order(base_url, i + 1000)
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description="Générateur de trafic pour le TP Tracing")
    parser.add_argument("--url", default="http://localhost:5000", help="URL du service-api")
    parser.add_argument("--duration", type=int, default=120, help="Durée du test en secondes (défaut: 120)")
    parser.add_argument("--rate", type=float, default=1.0, help="Requêtes par seconde (défaut: 1)")
    parser.add_argument("--health-only", action="store_true", help="Vérifier uniquement la santé")
    args = parser.parse_args()

    print(f"""
{BOLD}╔══════════════════════════════════════════════════════════════╗
║     TP Distributed Tracing - Générateur de trafic            ║
║     IAI Gabon ING2 2025-2026 - IRJ & IJS               ║
╚══════════════════════════════════════════════════════════════╝{RESET}

URL cible : {args.url}
Durée     : {args.duration}s
Débit     : {args.rate} req/s

Accès Grafana : http://localhost:3000 (admin / admin123)
  - Dashboard RED : TP Distributed Tracing > RED Dashboard
  - Traces Explore : Menu Explore > Sélectionner Tempo
  - Logs   Explore : Menu Explore > Sélectionner Loki
""")

    # Vérification santé
    if not check_health(args.url):
        print(f"\n{RED}⚠️  Certains services ne répondent pas. "
              f"Vérifiez que docker-compose est lancé :{RESET}")
        print("  docker compose up -d")
        print("  docker compose logs -f")
        if args.health_only:
            return
        print("\nContinuation quand même dans 5s...")
        time.sleep(5)

    if args.health_only:
        return

    print(f"\n{BOLD}=== Démarrage du test ({args.duration}s) ==={RESET}\n")
    print("5 scénarios de latence vont être injectés automatiquement.")
    print("Surveillez Grafana Explore avec la datasource Tempo !\n")

    # Compteurs
    stats = {"total": 0, "success": 0, "errors": 0, "timeouts": 0}
    start_time = time.time()
    order_num = 1
    interval = 1.0 / args.rate

    try:
        while time.time() - start_time < args.duration:
            elapsed = time.time() - start_time
            
            # Mix de requêtes : 60% création, 30% lecture, 10% listing
            r = random.random()
            
            if r < 0.60:
                status, duration = create_order(args.url, order_num)
                order_num += 1
            elif r < 0.90:
                status = get_order(args.url, order_num)
            else:
                status = list_products(args.url)
                duration = 0

            stats["total"] += 1
            if status in (200, 201):
                stats["success"] += 1
            elif status == 504:
                stats["timeouts"] += 1
            else:
                stats["errors"] += 1

            # Résumé toutes les 30 requêtes
            if stats["total"] % 30 == 0:
                elapsed_min = elapsed / 60
                log(f"\n📊 Résumé après {stats['total']} requêtes ({elapsed_min:.1f}min) : "
                    f"✅ {stats['success']} succès | "
                    f"❌ {stats['errors']} erreurs | "
                    f"⏱ {stats['timeouts']} timeouts\n", BOLD)

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Test interrompu par l'utilisateur{RESET}")

    # Résumé final
    total_duration = time.time() - start_time
    print(f"""
{BOLD}╔══════════════════════════════════════════════════════════════╗
║                    RÉSUMÉ FINAL                              ║
╚══════════════════════════════════════════════════════════════╝{RESET}

Durée totale   : {total_duration:.0f}s
Requêtes total : {stats['total']}
Succès         : {GREEN}{stats['success']}{RESET}
Erreurs        : {RED}{stats['errors']}{RESET}
Timeouts       : {YELLOW}{stats['timeouts']}{RESET}
Débit effectif : {stats['total']/total_duration:.2f} req/s

{BOLD}Grafana Explore :{RESET}
  1. Ouvrir http://localhost:3000
  2. Menu gauche → Explore
  3. Sélectionner datasource : Tempo
  4. Rechercher les traces avec latence > 500ms :
     {{ duration > 500ms }}
  5. Cliquer sur une trace → voir les spans → cliquer TraceID dans les logs
""")


if __name__ == "__main__":
    main()
