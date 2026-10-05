#!/usr/bin/env python3
"""Seed the demo database with purchasing scenarios where PO, receipt and bill disagree.

Each scenario creates a confirmed purchase order, receives some or all of the
goods, and leaves a *draft* vendor bill for a buyer to investigate in the demo:

  clean             everything matches (the control case)
  price_variance    supplier bills above the PO price
  partial_receipt   600 of 1,000 received, billed for 1,000 (rest on backorder)
  rejected_goods    20 of 200 rejected at inspection, billed for 200
  extra_charges     bill adds an expedite freight line that isn't on the PO

Uses Odoo's JSON-RPC API and only the standard library. Safe to re-run:
scenarios that already exist are skipped.

    python3 scripts/seed.py [--url http://localhost:8069] [--db demo] [--user admin] [--password admin]
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import os
import sys
import urllib.error
import urllib.request

VENDORS = {
    "polymer": "Precision Polymer Components",
    "medflex": "MedFlex Tubing Co.",
    "apex": "Apex Guidewire Supply",
}

PRODUCTS = {
    # key: (name, internal reference, cost)
    "shaft": ("Catheter Shaft Tubing 5Fr", "CST-5FR", 2.00),
    "valve": ("Hemostasis Valve Assembly", "HVA-100", 4.10),
    "luer": ("Luer Lock Connector", "LLC-200", 0.85),
    "coating": ("Guidewire Coating Kit", "GCK-035", 12.50),
    "liner": ("PTFE Liner 0.014in", "PTFE-014", 3.20),
}

# Each scenario: vendor, product, ordered qty, PO price, received qty,
# keep backorder?, billed qty, billed price, extra bill lines, note on the bill.
SCENARIOS = [
    dict(key="clean", vendor="polymer", product="shaft", qty=1000, price=2.00,
         received=1000, backorder=False, bill_qty=1000, bill_price=2.00,
         extra=[], ref="PPC-INV-1001", note=""),
    dict(key="price_variance", vendor="medflex", product="valve", qty=500, price=4.10,
         received=500, backorder=False, bill_qty=500, bill_price=4.35,
         extra=[], ref="MF-88213", note="Supplier price increase effective this month."),
    dict(key="partial_receipt", vendor="polymer", product="luer", qty=1000, price=0.85,
         received=600, backorder=True, bill_qty=1000, bill_price=0.85,
         extra=[], ref="PPC-INV-1017", note=""),
    dict(key="rejected_goods", vendor="apex", product="coating", qty=200, price=12.50,
         received=180, backorder=False, bill_qty=200, bill_price=12.50,
         extra=[], ref="AGS-5520", note="20 units failed incoming inspection (coating thickness out of spec)."),
    dict(key="extra_charges", vendor="medflex", product="liner", qty=300, price=3.20,
         received=300, backorder=False, bill_qty=300, bill_price=3.20,
         extra=[("Expedite freight", 145.00)], ref="MF-88240", note=""),
]

ORIGIN_PREFIX = "odoo-miner seed: "


class Odoo:
    """Minimal JSON-RPC client (handles None return values, unlike XML-RPC)."""

    def __init__(self, url: str, db: str, user: str, password: str):
        self.url = url.rstrip("/") + "/jsonrpc"
        self.db, self.password = db, password
        self._ids = itertools.count(1)
        self.uid = self._call("common", "login", db, user, password)
        if not self.uid:
            sys.exit(f"Login failed for {user} on database {db}.")

    def _call(self, service: str, method: str, *args):
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": next(self._ids),
            "params": {"service": service, "method": method, "args": args},
        }).encode()
        req = urllib.request.Request(self.url, payload, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.load(resp)
        except urllib.error.URLError as exc:
            sys.exit(f"Cannot reach Odoo at {self.url}: {exc.reason}")
        if body.get("error"):
            err = body["error"]
            raise RuntimeError(err.get("data", {}).get("message") or err.get("message"))
        return body["result"]

    def __call__(self, model: str, method: str, *args, **kwargs):
        return self._call("object", "execute_kw", self.db, self.uid, self.password,
                          model, method, list(args), kwargs)


def get_or_create(odoo: Odoo, model: str, domain: list, values: dict) -> int:
    found = odoo(model, "search", domain, limit=1)
    return found[0] if found else odoo(model, "create", [values])[0]


def receive(odoo: Odoo, po_id: int, qty: float, keep_backorder: bool) -> None:
    picking_ids = odoo("purchase.order", "read", [po_id], ["picking_ids"])[0]["picking_ids"]
    if len(picking_ids) != 1:
        raise RuntimeError(f"Expected one receipt for PO {po_id}, found {len(picking_ids)}.")
    picking_id = picking_ids[0]

    moves = odoo("stock.move", "search_read", [("picking_id", "=", picking_id)], fields=["id"])
    odoo("stock.move", "write", [m["id"] for m in moves], {"quantity": qty, "picked": True})

    context = {"skip_backorder": True}
    if not keep_backorder:
        context["picking_ids_not_to_backorder"] = [picking_id]
    odoo("stock.picking", "button_validate", [picking_id], context=context)


def seed(odoo: Odoo) -> list[dict]:
    vendor_ids = {
        key: get_or_create(odoo, "res.partner", [("name", "=", name)],
                           {"name": name, "is_company": True, "supplier_rank": 1})
        for key, name in VENDORS.items()
    }
    product_ids = {
        key: get_or_create(odoo, "product.product", [("default_code", "=", code)], {
            "name": name, "default_code": code, "standard_price": cost,
            "type": "consu", "is_storable": True,     # Odoo 18: tracked inventory
            "purchase_method": "receive",             # bill against received quantities
        })
        for key, (name, code, cost) in PRODUCTS.items()
    }
    freight_id = get_or_create(odoo, "product.product", [("default_code", "=", "FRT-EXP")], {
        "name": "Expedite freight", "default_code": "FRT-EXP", "type": "service",
        "purchase_method": "purchase",
    })

    today = dt.date.today().isoformat()
    results = []

    for s in SCENARIOS:
        origin = ORIGIN_PREFIX + s["key"]
        existing = odoo("purchase.order", "search_read", [("origin", "=", origin)], fields=["name"], limit=1)
        if existing:
            print(f"  skip {s['key']:16} already seeded as {existing[0]['name']}")
            continue

        po_id = odoo("purchase.order", "create", [{
            "partner_id": vendor_ids[s["vendor"]],
            "origin": origin,
            "order_line": [(0, 0, {
                "product_id": product_ids[s["product"]],
                "product_qty": s["qty"],
                "price_unit": s["price"],
            })],
        }])[0]
        odoo("purchase.order", "button_confirm", [po_id])
        receive(odoo, po_id, s["received"], s["backorder"])

        po_line_id = odoo("purchase.order.line", "search", [("order_id", "=", po_id)], limit=1)[0]
        lines = [(0, 0, {
            "product_id": product_ids[s["product"]],
            "quantity": s["bill_qty"],
            "price_unit": s["bill_price"],
            "purchase_line_id": po_line_id,
        })]
        for label, amount in s["extra"]:
            lines.append((0, 0, {"product_id": freight_id, "name": label, "quantity": 1, "price_unit": amount}))

        bill_id = odoo("account.move", "create", [{
            "move_type": "in_invoice",
            "partner_id": vendor_ids[s["vendor"]],
            "invoice_date": today,
            "ref": s["ref"],
            "narration": s["note"] or False,
            "invoice_line_ids": lines,
        }])[0]

        po = odoo("purchase.order", "read", [po_id], ["name"])[0]
        results.append({"scenario": s["key"], "po": po["name"], "po_id": po_id, "bill_id": bill_id})
        print(f"  made {s['key']:16} {po['name']} → draft bill id {bill_id}")

    return results


def verify(odoo: Odoo) -> None:
    """Print ordered / received / billed per scenario so the mismatches are visible."""
    print("\nScenario          PO        ordered  received  billed   PO price  bill price")
    for s in SCENARIOS:
        po = odoo("purchase.order", "search_read", [("origin", "=", ORIGIN_PREFIX + s["key"])],
                  fields=["name", "order_line"], limit=1)
        if not po:
            continue
        line = odoo("purchase.order.line", "read", po[0]["order_line"][:1],
                    ["product_qty", "qty_received", "qty_invoiced", "price_unit"])[0]
        bill_lines = odoo("account.move.line", "search_read",
                          [("purchase_line_id", "=", po[0]["order_line"][0]),
                           ("move_id.move_type", "=", "in_invoice")],
                          fields=["quantity", "price_unit"], limit=1)
        billed = bill_lines[0] if bill_lines else {"quantity": 0, "price_unit": 0}
        print(f"{s['key']:17} {po[0]['name']:9} {line['product_qty']:7.0f}  {line['qty_received']:8.0f}  "
              f"{billed['quantity']:6.0f}   {line['price_unit']:8.2f}  {billed['price_unit']:10.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("ODOO_URL", "http://localhost:8069"))
    parser.add_argument("--db", default=os.environ.get("ODOO_DB", "demo"))
    parser.add_argument("--user", default=os.environ.get("ODOO_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("ODOO_PASSWORD", "admin"))
    args = parser.parse_args()

    odoo = Odoo(args.url, args.db, args.user, args.password)
    print(f"Seeding {args.db} at {args.url}")
    seed(odoo)
    verify(odoo)
    print("\nNext: ./scripts/snapshot_db.sh")


if __name__ == "__main__":
    main()
