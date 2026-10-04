#!/usr/bin/env bash
# Reset "demo" to "demo_snapshot". Use as the replay pre-hook:
#   odoo-miner run recording.json --pre-hook ./scripts/restore_db.sh --cookie …
# Your browser session survives this: sessions are stored in the Odoo volume,
# not in the database.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${DB:-demo}"
SNAP="${SNAP:-${DB}_snapshot}"

if ! docker compose exec -T db psql -U odoo -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$SNAP'" | grep -q 1; then
  echo "No snapshot '$SNAP'. Run ./scripts/snapshot_db.sh first." >&2
  exit 1
fi

# Odoo holds connections and caches; stop it so the drop works and nothing stale survives.
docker compose stop odoo
docker compose exec -T db dropdb -U odoo --if-exists --force "$DB"
docker compose exec -T db createdb -U odoo -T "$SNAP" "$DB"

docker compose run --rm --no-deps --entrypoint bash odoo -c "
  rm -rf /var/lib/odoo/filestore/$DB
  if [ -d /var/lib/odoo/filestore/$SNAP ]; then
    cp -a /var/lib/odoo/filestore/$SNAP /var/lib/odoo/filestore/$DB
  fi"

docker compose start odoo
./scripts/wait_for_odoo.sh
echo "Restored $DB from $SNAP"
