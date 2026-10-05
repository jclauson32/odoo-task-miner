# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    po_line_product_qty = fields.Float(
        related='purchase_line_id.product_qty',
        string='PO Qty',
        digits='Product Unit of Measure',
        help="Quantity ordered on the linked purchase order line, shown "
             "read-only so you can compare it with this bill line without "
             "opening the purchase order.",
    )
    po_line_qty_received = fields.Float(
        related='purchase_line_id.qty_received',
        string='Received Qty',
        digits='Product Unit of Measure',
        help="Total quantity received so far for the linked purchase order "
             "line, across all receipts/backorders - not necessarily the "
             "quantity on a single receipt.",
    )
    po_line_price_unit = fields.Float(
        related='purchase_line_id.price_unit',
        string='PO Price',
        digits='Product Price',
        help="Unit price on the linked purchase order line, shown read-only "
             "so you can compare it with this bill line without opening "
             "the purchase order.",
    )
