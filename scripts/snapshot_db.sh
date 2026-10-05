#!/usr/bin/env bash
# Save the current "demo" database (and its attachments) as "demo_snapshot".
# Run after seeding; restore_db.sh resets to this state before each replay.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${DB:-demo}"
SNAP="${SNAP:-${DB}_snapshot}"

# Odoo is stopped while the database is swapped; if anything below fails,
# start it again rather than leave the demo down.
trap 'docker compose start odoo >/dev/null 2>&1 || true' ERR

# Works straight after a Docker restart, when the database container is down.
docker compose up -d --wait db

docker compose stop odoo
docker compose exec -T db dropdb -U odoo --if-exists --force "$SNAP"
docker compose exec -T db createdb -U odoo -T "$DB" "$SNAP"

# Attachments live in the filestore on disk, not in the database.
docker compose run --rm --no-deps --entrypoint bash odoo -c "
  rm -rf /var/lib/odoo/filestore/$SNAP
  if [ -d /var/lib/odoo/filestore/$DB ]; then
    cp -a /var/lib/odoo/filestore/$DB /var/lib/odoo/filestore/$SNAP
  fi"

docker compose start odoo
./scripts/wait_for_odoo.sh
echo "Snapshot saved: $DB → $SNAP"
