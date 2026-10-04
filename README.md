# odoo-miner

Turns a Chrome DevTools Recorder session of someone working in Odoo into
structured data the analysis agents can reason about: every step, the element
it touched, the screen and record it happened on, and the exact backend calls
it triggered.

```
recording.json ──ingest──▶ clicks.json ─┐
       │                                ├─merge──▶ session.json ──▶ segmenter_agent → tracer_agent → …
       └───────replay────▶ network.json ┘
```

## Setup

### 1. Odoo 18 in Docker

`docker-compose.yml` runs Odoo 18 Community and PostgreSQL 16. The CLI and the
replay run on your machine and talk to Odoo at `http://localhost:8069`.

```bash
./scripts/init_db.sh          # creates "demo" with Purchase, Inventory, Invoicing (admin / admin)
python scripts/seed.py        # adds the purchasing scenarios below
./scripts/snapshot_db.sh      # saves "demo_snapshot"; replays reset to this
```

The seed creates five confirmed purchase orders, receives the goods, and
leaves a draft vendor bill on each for a buyer to work through:

| Scenario | Ordered | Received | Billed | PO price | Bill price |
|---|---|---|---|---|---|
| clean | 1000 | 1000 | 1000 | 2.00 | 2.00 |
| price_variance | 500 | 500 | 500 | 4.10 | 4.35 |
| partial_receipt | 1000 | 600 (rest on backorder) | 1000 | 0.85 | 0.85 |
| rejected_goods | 200 | 180 (20 failed inspection) | 200 | 12.50 | 12.50 |
| extra_charges | 300 | 300 | 300 + $145 expedite freight | 3.20 | 3.20 |

Re-running the seed skips scenarios that already exist.

`./scripts/restore_db.sh` resets `demo` to the snapshot: it stops Odoo,
recreates the database from the snapshot, copies the attachments folder back
and starts Odoo again. Pass it as `--pre-hook` so every replay starts from the
same data. Custom modules go in `addons/`, which is mounted into the container.

For the agents that read Odoo's code, clone the matching source next to the
project: `git clone --depth 1 -b 18.0 https://github.com/odoo/odoo`.

### 2. The CLI and replay

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# Replay needs Node 18+ and downloads Chrome on first install
cd replay && npm install && cd ..
```

## Usage

1. In Chrome, open DevTools → **Recorder**, record the workflow in Odoo, and
   export it as **JSON**.
2. Run the pipeline:

```bash
odoo-miner run recording.json -d out/ \
  --cookie "session_id=<your Odoo session cookie>" \
  --pre-hook "./scripts/restore_db.sh"
odoo-miner show out/session.json
```

Or run the stages separately:

```bash
odoo-miner ingest recording.json -o clicks.json
odoo-miner replay recording.json -o network.json --cookie "session_id=…"
odoo-miner merge clicks.json network.json -o session.json
```

`--skip-replay` on `run` (or `merge` without a network file) gives you clicks
only, with no backend calls.

## What each file contains

| File | Model | Contents |
|---|---|---|
| `clicks.json` | `ClickLog` | Each step's type, target (aria label, visible text, `name` attribute, CSS), typed value, the page URL it happened on, and Odoo context parsed from that URL (model, record id, action, view type). |
| `network.json` | `NetworkLog` | Every Odoo RPC fired during replay (`call_kw`, `call_button`, `action/load`, `/json/2`), with model, method, args, status and the recording step that was running. |
| `session.json` | `Session` | Clicks with their backend calls attached, each call tagged `read` / `write` / `compute` / `action_load` / `unknown`, plus `has_write` per step. |

Schemas live in `src/odoo_miner/models.py`; every stage reads and writes these,
so a stage can be inspected or re-run on its own.

## Things to know

- **Replay repeats every write.** If the recording posts a bill, so does the
  replay. Restore the database before each run with `--pre-hook` (for Odoo in
  Docker, a `pg_restore` or Odoo's database duplicate/restore). The replay
  aborts if the hook fails.
- **Login.** Replay starts a fresh browser. Pass your session cookie with
  `--cookie`, or include the login steps in the recording.
- **No human timing.** The Recorder format stores no timestamps, and replay
  timestamps reflect the replay, not the person. Difficulty is scored from
  structure, not time.
- **Late requests.** After each step the replay waits for the network to go
  idle (`--settle`, default 500 ms) so that onchange/autosave calls land on the
  step that caused them. Attribution is usually right but not guaranteed.
- **Call kinds are hints.** The read/write classification uses known Odoo
  method names and `action_`/`button_` prefixes. Custom methods come out as
  `unknown` for `tracer_agent` to resolve against the backend code.
- **URL parsing.** Both the legacy `/web#model=…&id=…` style and the Odoo 18
  `/odoo/…` path style are handled. Odoo 18 paths encode the breadcrumb trail
  (`/odoo/action-245/1042/action-388/77`); the parser follows Odoo's own router
  rules and reports the last screen, with the full trail kept in `path_slugs`.

## Tests

```bash
pytest
```

The fixture in `tests/fixtures/` is a hand-written vendor-bill workflow
(open bill → check PO → check receipt → fix price → confirm). Replace it with
real recordings as you capture them.
