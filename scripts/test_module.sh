#!/usr/bin/env bash
# Run a module's tests in the "demo" database, installing or updating the
# module first, and exit with Odoo's exit code.
#
#   ./scripts/test_module.sh <module_name>
#
# Odoo's web server is stopped while the tests run: a server loading the same
# database can collide with the install's writes to the module tables
# ("could not serialize access due to concurrent update"). It is started
# again afterwards, whatever the result.
set -euo pipefail
cd "$(dirname "$0")/.."

MODULE="${1:?usage: $0 <module_name>}"
DB="${DB:-demo}"

if [[ ! -f "addons/$MODULE/__manifest__.py" ]]; then
  echo "No module at addons/$MODULE (missing __manifest__.py)." >&2
  exit 1
fi

docker compose up -d --wait db
docker compose stop odoo
trap 'docker compose start odoo >/dev/null 2>&1 && ./scripts/wait_for_odoo.sh' EXIT

# -i installs it if it is new, -u updates it if it is already installed, so
# its tests run either way.
docker compose run --rm odoo odoo -d "$DB" -i "$MODULE" -u "$MODULE" \
  --test-tags "/$MODULE" --stop-after-init
