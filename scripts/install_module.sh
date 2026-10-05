#!/usr/bin/env bash
# Install (or update) a module from addons/ into the "demo" database, then
# restart Odoo so the web client loads it.
#
#   ./scripts/install_module.sh <module_name>
#
# Pair it with restore_db.sh to get a clean database with the change applied:
#   ./scripts/restore_db.sh && ./scripts/install_module.sh <module_name>
set -euo pipefail
cd "$(dirname "$0")/.."

MODULE="${1:?usage: $0 <module_name>}"
DB="${DB:-demo}"

if [[ ! -f "addons/$MODULE/__manifest__.py" ]]; then
  echo "No module at addons/$MODULE (missing __manifest__.py)." >&2
  exit 1
fi

# If anything below fails, start Odoo again rather than leave it down.
trap 'docker compose start odoo >/dev/null 2>&1 || true' ERR

docker compose up -d --wait db
# Odoo's web server is stopped during the install: a server loading the same
# database can collide with the install's writes to the module tables.
docker compose stop odoo
# -i installs it if it is new, -u updates it if it is already installed.
docker compose run --rm odoo odoo -d "$DB" -i "$MODULE" -u "$MODULE" --stop-after-init
docker compose start odoo
./scripts/wait_for_odoo.sh
echo "Installed $MODULE in $DB"
