# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPurchaseBillMatchColumns(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({'name': 'Test Vendor'})
        cls.product = cls.env['product.product'].create({
            'name': 'Test Product',
            # 'service' (rather than 'consu') keeps the received quantity on
            # 'manual' even when purchase_stock is installed, so the test
            # does not need to create an actual stock receipt.
            'type': 'service',
            'purchase_method': 'purchase',  # invoice based on ordered qty, no receipt needed
            'standard_price': 10.0,
        })

        cls.po = cls.env['purchase.order'].create({
            'partner_id': cls.partner.id,
            'order_line': [(0, 0, {
                'product_id': cls.product.id,
                'name': cls.product.name,
                'product_qty': 100.0,
                'product_uom': cls.product.uom_po_id.id,
                'price_unit': 10.0,
                'date_planned': '2024-01-01',
            })],
        })
        cls.po.button_confirm()
        cls.po_line = cls.po.order_line[0]

        # Simulate a partial receipt: only 80 out of the 100 ordered units
        # have actually come in so far.
        cls.po_line.qty_received_manual = 80.0

        invoice_action = cls.po.action_create_invoice()
        cls.bill = cls.env['account.move'].browse(invoice_action['res_id'])
        cls.bill.invoice_date = '2024-01-02'
        cls.bill_line = cls.bill.invoice_line_ids.filtered(lambda l: l.purchase_line_id)

    def test_po_columns_reflect_purchase_order_line(self):
        """The related PO qty/received qty/price columns on the bill line
        must mirror the matched purchase order line, without requiring any
        navigation to the purchase order or its receipt."""
        self.assertTrue(self.bill_line, "The bill line should be linked to the purchase order line")
        self.assertEqual(self.bill_line.po_line_product_qty, 100.0)
        self.assertEqual(self.bill_line.po_line_qty_received, 80.0)
        self.assertEqual(self.bill_line.po_line_price_unit, 10.0)

    def test_po_columns_are_read_only_and_do_not_affect_purchase_order(self):
        """Writing to the related columns must not be possible in a way that
        changes the purchase order line, and action_post must keep working
        exactly as it does without the module."""
        # Simulate the vendor billing a different price than the PO: the bill
        # line's own price_unit is independent of the related PO columns.
        self.bill_line.price_unit = 12.0
        self.assertEqual(self.bill_line.po_line_price_unit, 10.0,
                          "The PO's own price must stay visible/unchanged even if the bill line price differs")

        self.bill.action_post()
        self.assertEqual(self.bill.state, 'posted')
        # The purchase order line itself must be untouched by posting the bill.
        self.assertEqual(self.po_line.product_qty, 100.0)
        self.assertEqual(self.po_line.qty_received, 80.0)
        self.assertEqual(self.po_line.price_unit, 10.0)

    def test_columns_blank_when_not_matched_to_a_purchase_order(self):
        """A bill line with no linked purchase order line should simply show
        falsy values, never raise."""
        other_bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner.id,
            'invoice_date': '2024-01-02',
            'invoice_line_ids': [(0, 0, {
                'name': 'Unrelated line',
                'quantity': 1,
                'price_unit': 5.0,
            })],
        })
        line = other_bill.invoice_line_ids[0]
        self.assertFalse(line.purchase_line_id)
        self.assertEqual(line.po_line_product_qty, 0.0)
        self.assertEqual(line.po_line_qty_received, 0.0)
        self.assertEqual(line.po_line_price_unit, 0.0)
