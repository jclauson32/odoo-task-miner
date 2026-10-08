"""Tests for the Apply PO Terms action."""
from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAccountMoveApplyPOTerms(TransactionCase):
    """Acceptance tests for action_apply_po_terms.

    The products are services billed on received quantities, so qty_received
    can be set directly and the tests do not need purchase_stock.
    """

    @classmethod
    def setUpClass(cls):
        """Create the vendor, units of measure and products the tests share."""
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({'name': 'Apply PO Terms Vendor'})
        cls.uom_unit = cls.env.ref('uom.product_uom_unit')
        cls.uom_dozen = cls.env.ref('uom.product_uom_dozen')
        cls.uom_kg = cls.env.ref('uom.product_uom_kgm')
        cls.uom_gram = cls.env.ref('uom.product_uom_gram')
        cls.product = cls.env['product.product'].create({
            'name': 'Consulting Hours',
            'type': 'service',
            'purchase_method': 'receive',
            'uom_id': cls.uom_unit.id,
            'uom_po_id': cls.uom_unit.id,
        })
        cls.freight_product = cls.env['product.product'].create({
            'name': 'Expedite Freight',
            'type': 'service',
            'purchase_method': 'receive',
        })
        cls.product_weight = cls.env['product.product'].create({
            'name': 'Bulk Material',
            'type': 'service',
            'purchase_method': 'receive',
            'uom_id': cls.uom_kg.id,
            'uom_po_id': cls.uom_kg.id,
        })

    def _create_po(self, product_qty, price_unit, qty_received, uom=None, currency=None, product=None):
        """A confirmed one-line purchase order with `qty_received` recorded."""
        product = product or self.product
        uom = uom or self.uom_unit
        po = self.env['purchase.order'].create({
            'partner_id': self.partner.id,
            'currency_id': (currency or self.env.company.currency_id).id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'name': product.name,
                'product_qty': product_qty,
                'product_uom': uom.id,
                'price_unit': price_unit,
                'date_planned': fields.Datetime.now(),
            })],
        })
        po.button_confirm()
        po.order_line.qty_received = qty_received
        return po

    def _create_bill(self, po_line, quantity, price_unit, uom=None, extra_lines=None, product=None):
        """A draft vendor bill with one line linked to `po_line`, plus `extra_lines`."""
        product = product or po_line.product_id
        uom = uom or po_line.product_uom
        line_vals = [(0, 0, {
            'product_id': product.id,
            'quantity': quantity,
            'price_unit': price_unit,
            'product_uom_id': uom.id,
            'purchase_line_id': po_line.id,
            'name': product.name,
        })]
        if extra_lines:
            line_vals += extra_lines
        return self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': line_vals,
        })

    def test_price_and_quantity_rewritten_to_po_terms(self):
        """A linked line gets the PO's price and the received quantity, and stays draft."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=8, price_unit=12.0)
        bill_line = bill.invoice_line_ids
        # Odoo counts this draft line as invoiced, so qty_to_invoice is 10 - 8.
        self.assertAlmostEqual(po_line.qty_to_invoice, 2.0, places=2)

        bill.action_apply_po_terms()

        self.assertAlmostEqual(bill_line.price_unit, 10.0, places=2)
        self.assertAlmostEqual(bill_line.quantity, 10.0, places=2)
        # Confirm stays a separate, manual step.
        self.assertEqual(bill.state, 'draft')

    def test_uom_conversion_quantity_across_dozen_and_unit(self):
        """2 dozen received and billed in units comes out at exactly 24 units."""
        po = self._create_po(product_qty=2, price_unit=10.0, qty_received=2.0, uom=self.uom_dozen)
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=20, price_unit=1.0, uom=self.uom_unit)
        bill_line = bill.invoice_line_ids

        bill.action_apply_po_terms()

        # 20 units round to 1.67 dozen; the result must still be 24 units.
        self.assertAlmostEqual(bill_line.quantity, 24.0, places=2)
        # 10.0 / dozen converted to per-unit.
        self.assertAlmostEqual(bill_line.price_unit, 10.0 / 12.0, places=4)

    def test_uom_conversion_of_price_and_quantity(self):
        """Quantity and price are converted between kilograms and grams."""
        po = self._create_po(
            product_qty=2, price_unit=10.0, qty_received=2.0,
            uom=self.uom_kg, product=self.product_weight,
        )
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=1500, price_unit=0.02, uom=self.uom_gram)
        bill_line = bill.invoice_line_ids

        bill.action_apply_po_terms()

        # 2 kg received, 1.5 kg (1500 g) already billed -> 0.5 kg (500 g) left to invoice.
        self.assertAlmostEqual(bill_line.quantity, 2000.0, places=2)
        # 10.0 / kg converted to per-gram.
        self.assertAlmostEqual(bill_line.price_unit, 0.01, places=4)

    def test_removes_lines_without_purchase_line(self):
        """Every product line with no purchase_line_id is unlinked."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        bill = self._create_bill(
            po_line, quantity=10, price_unit=10.0,
            extra_lines=[(0, 0, {
                'product_id': self.freight_product.id,
                'quantity': 1,
                'price_unit': 145.0,
                'name': 'Expedite freight',
            })],
        )
        self.assertEqual(len(bill.invoice_line_ids), 2)

        bill.action_apply_po_terms()

        self.assertEqual(len(bill.invoice_line_ids), 1)
        self.assertNotIn(self.freight_product, bill.invoice_line_ids.product_id)

    def test_single_chatter_message_with_old_and_new_values_escaped(self):
        """One note lists every change, old to new and escaped; a second call posts none."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        unsafe_product = self.env['product.product'].create({
            'name': '<b>Freight</b> & Co',
            'type': 'service',
        })
        bill = self._create_bill(
            po_line, quantity=8, price_unit=12.0,
            extra_lines=[(0, 0, {
                'product_id': unsafe_product.id,
                'quantity': 1,
                'price_unit': 145.0,
                'name': 'Unsafe freight',
            })],
        )
        before_messages = bill.message_ids

        bill.action_apply_po_terms()

        new_messages = bill.message_ids - before_messages
        self.assertEqual(len(new_messages), 1)
        body = new_messages.body
        self.assertIn('10.00', body)
        self.assertIn('145.00', body)
        # The product name must be escaped, not injected as raw HTML.
        self.assertNotIn('<b>Freight</b>', body)
        self.assertIn('&lt;b&gt;Freight&lt;/b&gt;', body)

        # Second call: nothing left to change or remove, so nothing more is posted.
        before_messages = bill.message_ids
        bill.action_apply_po_terms()
        self.assertEqual(bill.message_ids, before_messages)

    def test_refuses_on_non_draft_move(self):
        """A posted bill is refused and left as it was."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=8, price_unit=12.0)
        bill.sudo().write({'state': 'posted'})

        with self.assertRaises(UserError):
            bill.action_apply_po_terms()

        self.assertEqual(bill.state, 'posted')
        self.assertEqual(bill.invoice_line_ids.price_unit, 12.0)
        self.assertEqual(bill.invoice_line_ids.quantity, 8.0)

    def test_idempotent_second_call_is_a_noop(self):
        """A second call with nothing left to change writes and posts nothing."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=8, price_unit=12.0)

        bill.action_apply_po_terms()
        messages_after_first_call = bill.message_ids
        write_date_after_first_call = bill.invoice_line_ids.write_date

        bill.action_apply_po_terms()

        self.assertEqual(bill.message_ids, messages_after_first_call)
        self.assertEqual(bill.invoice_line_ids.write_date, write_date_after_first_call)

    def test_shared_purchase_line_raises_instead_of_reapplying(self):
        """Two lines sharing a PO line are left alone, and the user is told why."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        bill = self._create_bill(
            po_line, quantity=4, price_unit=12.0,
            extra_lines=[(0, 0, {
                'product_id': self.product.id,
                'quantity': 4,
                'price_unit': 12.0,
                'product_uom_id': self.uom_unit.id,
                'purchase_line_id': po_line.id,
                'name': self.product.name,
            })],
        )
        lines = bill.invoice_line_ids
        self.assertEqual(len(lines), 2)
        before_messages = bill.message_ids

        with self.assertRaises(UserError):
            bill.action_apply_po_terms()

        # Left as they were, with no note posted.
        self.assertEqual(len(bill.invoice_line_ids), 2)
        for line in lines:
            self.assertEqual(line.price_unit, 12.0)
            self.assertEqual(line.quantity, 4.0)
        self.assertEqual(bill.message_ids, before_messages)

    def test_refuses_on_refund_move(self):
        """A vendor refund is refused and stays draft."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        refund = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 8,
                'price_unit': 12.0,
                'product_uom_id': self.uom_unit.id,
                'purchase_line_id': po_line.id,
                'name': self.product.name,
            })],
        })

        with self.assertRaises(UserError):
            refund.action_apply_po_terms()

        self.assertEqual(refund.state, 'draft')
        self.assertEqual(refund.invoice_line_ids.price_unit, 12.0)
        self.assertEqual(refund.invoice_line_ids.quantity, 8.0)

    def test_currency_mismatch_raises_user_error(self):
        """A PO in another currency is refused instead of having its price copied."""
        other_currency = self.env['res.currency'].with_context(active_test=False).search(
            [('id', '!=', self.env.company.currency_id.id)], limit=1)
        other_currency.sudo().write({'active': True})

        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0, currency=other_currency)
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=8, price_unit=12.0)
        self.assertEqual(bill.currency_id, self.env.company.currency_id)
        self.assertNotEqual(po_line.currency_id, bill.currency_id)

        with self.assertRaises(UserError):
            bill.action_apply_po_terms()

        self.assertEqual(bill.invoice_line_ids.price_unit, 12.0)
        self.assertEqual(bill.invoice_line_ids.quantity, 8.0)
