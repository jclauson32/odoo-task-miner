from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAccountMoveApplyPOTerms(TransactionCase):
    """Acceptance tests for account.move.action_apply_po_terms().

    Deliberately avoids purchase_stock: the test product is a service with
    purchase_method='receive' (services otherwise default to 'purchase'), so
    'received' quantity is the manual qty_received field instead of stock
    moves.
    """

    @classmethod
    def setUpClass(cls):
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
        """A line with a purchase_line_id is rewritten to the PO's price and
        to qty_to_invoice + its own previous quantity, only when either
        differs."""
        po = self._create_po(product_qty=10, price_unit=10.0, qty_received=10.0)
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=8, price_unit=12.0)
        bill_line = bill.invoice_line_ids
        # qty_invoiced counts the draft line's own (wrong) quantity, so
        # qty_to_invoice undershoots by exactly that amount.
        self.assertAlmostEqual(po_line.qty_to_invoice, 2.0, places=2)

        bill.action_apply_po_terms()

        self.assertAlmostEqual(bill_line.price_unit, 10.0, places=2)
        self.assertAlmostEqual(bill_line.quantity, 10.0, places=2)
        # Confirm stays a separate, manual step.
        self.assertEqual(bill.state, 'draft')

    def test_uom_conversion_quantity_across_dozen_and_unit(self):
        """The quantity is converted with _compute_quantity when the bill
        line's UoM (units) differs from the PO line's (dozens): converting
        the bill's own quantity into the PO line's unit before adding it
        back to qty_to_invoice, and only then converting the total back,
        avoids compounding two separate UoM roundings."""
        po = self._create_po(product_qty=2, price_unit=10.0, qty_received=2.0, uom=self.uom_dozen)
        po_line = po.order_line
        bill = self._create_bill(po_line, quantity=20, price_unit=1.0, uom=self.uom_unit)
        bill_line = bill.invoice_line_ids

        bill.action_apply_po_terms()

        # 2 dozen (24 units) received, fully reconstructed despite the
        # dozen<->unit conversion not being a round decimal.
        self.assertAlmostEqual(bill_line.quantity, 24.0, places=2)
        # 10.0 / dozen converted to per-unit.
        self.assertAlmostEqual(bill_line.price_unit, 10.0 / 12.0, places=4)

    def test_uom_conversion_of_price_and_quantity(self):
        """When the bill line's UoM differs from the PO line's, the quantity
        is converted with _compute_quantity and the price with
        _compute_price."""
        # PO in kg, bill in grams: a clean 1000:1 ratio avoids rounding noise
        # and isolates the conversion itself from UoM rounding precision.
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
        """Exactly one message_post lists every change/removal with old and
        new values, with HTML-unsafe values escaped; nothing is posted if
        nothing changed."""
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
        """Calling it on a state != 'draft' move raises UserError and leaves
        the move's lines and state unchanged."""
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
        """Calling it again right after, with no new deviations, writes
        nothing and posts no further message."""
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
        """A bill line whose purchase_line_id is referenced by another line
        on the same bill is left untouched, not recalculated. Since that is
        the only finding here (nothing else to write or remove), it raises
        a UserError naming the lines instead of silently doing nothing or
        posting the same "needs review" note on every click."""
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

        # Left untouched: neither recalculated nor removed nor noted about.
        self.assertEqual(len(bill.invoice_line_ids), 2)
        for line in lines:
            self.assertEqual(line.price_unit, 12.0)
            self.assertEqual(line.quantity, 4.0)
        self.assertEqual(bill.message_ids, before_messages)

    def test_refuses_on_refund_move(self):
        """A vendor refund (in_refund) is refused like any non-in_invoice
        move, and is never confirmed."""
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
        """A PO line in a different currency than the bill raises UserError
        instead of silently misapplying the price."""
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
