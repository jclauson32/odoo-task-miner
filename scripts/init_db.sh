#!/usr/bin/env bash
# Create the "demo" database with Purchase, Inventory and Invoicing installed.
# Login afterwards: admin / admin at http://localhost:8069
#
# Safe to run after `docker compose up`: Odoo creates an empty "demo" database
# on first start (because of --database=demo), and this script replaces it.
set -euo pipefail
cd "$(dirname "$0")/.."
 
DB="${DB:-demo}"
MODULES="purchase,stock,account"
 
psql_db() {
  docker compose exec -T db psql -U odoo -d "$1" -tAc "$2"
}
 
docker compose up -d --wait db
# Stop the Odoo server so it isn't using the database while we set it up.
docker compose stop odoo
 
if psql_db postgres "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1; then
  account_state="$(psql_db "$DB" "SELECT state FROM ir_module_module WHERE name='account'" 2>/dev/null || true)"
  if [[ "$account_state" == "installed" ]]; then
    echo "Database '$DB' is already set up. To start over:"
    echo "  docker compose stop odoo && docker compose exec db dropdb -U odoo --force $DB && $0"
    docker compose start odoo
    exit 0
  fi
  echo "Replacing the empty '$DB' database Odoo created on first start…"
  docker compose exec -T db dropdb -U odoo --force "$DB"
fi
 
echo "Creating '$DB' and installing $MODULES (takes a few minutes)…"
docker compose run --rm odoo odoo -d "$DB" -i "$MODULES" \
  --without-demo=all --stop-after-init
 
docker compose up -d odoo
./scripts/wait_for_odoo.sh
echo "Ready: http://localhost:8069  (admin / admin)"
echo "Next: python3 scripts/seed.py"
