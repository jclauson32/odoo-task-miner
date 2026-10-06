"""Tests for the purchase order columns on vendor bill lines."""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPurchaseBillMatchColumns(TransactionCase):
    """The columns mirror the purchase order line and change nothing."""

    @classmethod
    def setUpClass(cls):
        """A confirmed PO for 100 units, 80 received, and a bill created from it."""
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({'name': 'Test Vendor'})
        cls.product = cls.env['product.product'].create({
            'name': 'Test Product',
            # A service keeps qty_received manual, so no stock receipt is needed.
            'type': 'service',
            'purchase_method': 'purchase',  # bill on ordered quantities
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

        # A partial receipt: 80 of the 100 units ordered.
        cls.po_line.qty_received_manual = 80.0

        invoice_action = cls.po.action_create_invoice()
        cls.bill = cls.env['account.move'].browse(invoice_action['res_id'])
        cls.bill.invoice_date = '2024-01-02'
        cls.bill_line = cls.bill.invoice_line_ids.filtered(lambda l: l.purchase_line_id)

    def test_po_columns_reflect_purchase_order_line(self):
        """The columns show the matched PO line's quantity, received quantity and price."""
        self.assertTrue(self.bill_line, "The bill line should be linked to the purchase order line")
        self.assertEqual(self.bill_line.po_line_product_qty, 100.0)
        self.assertEqual(self.bill_line.po_line_qty_received, 80.0)
        self.assertEqual(self.bill_line.po_line_price_unit, 10.0)

    def test_po_columns_are_read_only_and_do_not_affect_purchase_order(self):
        """The columns never change the PO line, and posting the bill still works."""
        # The vendor bills a different price; the PO price column keeps the PO's.
        self.bill_line.price_unit = 12.0
        self.assertEqual(self.bill_line.po_line_price_unit, 10.0,
                          "The PO's own price must stay visible/unchanged even if the bill line price differs")

        self.bill.action_post()
        self.assertEqual(self.bill.state, 'posted')
        # Posting the bill leaves the purchase order line untouched.
        self.assertEqual(self.po_line.product_qty, 100.0)
        self.assertEqual(self.po_line.qty_received, 80.0)
        self.assertEqual(self.po_line.price_unit, 10.0)

    def test_columns_blank_when_not_matched_to_a_purchase_order(self):
        """A line with no PO line shows empty columns instead of failing."""
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
