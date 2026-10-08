"""The Apply PO Terms action on vendor bills."""
from markupsafe import Markup

from odoo import _, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class AccountMove(models.Model):
    """Vendor bills that can be set to their purchase order's terms."""

    _inherit = 'account.move'

    def action_apply_po_terms(self):
        """Record each product line at its purchase order line's terms.

        A line linked to a PO line gets its price and the quantity received
        less what was billed elsewhere; a line with no PO line is removed; a PO
        line shared by two bill lines is left for a person. One chatter note
        lists every change, old to new. The bill is never confirmed.
        """
        for move in self:
            move._apply_po_terms()
        return True

    def _apply_po_terms(self):
        """Apply the PO terms to one draft vendor bill."""
        self.ensure_one()
        if self.state != 'draft' or self.move_type != 'in_invoice':
            raise UserError(_("Apply PO Terms can only be used on draft vendor bills."))

        price_precision = self.env['decimal.precision'].precision_get('Product Price')
        qty_precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')

        product_lines = self.invoice_line_ids.filtered(lambda l: l.display_type == 'product')

        po_line_use_count = {}
        for line in product_lines:
            if line.purchase_line_id:
                po_line_use_count[line.purchase_line_id.id] = po_line_use_count.get(line.purchase_line_id.id, 0) + 1

        to_remove = self.env['account.move.line']
        to_update = []  # list of (line, {field: (old, new)})
        to_review = self.env['account.move.line']

        for line in product_lines:
            po_line = line.purchase_line_id
            if not po_line:
                to_remove |= line
                continue

            if po_line_use_count[po_line.id] > 1:
                to_review |= line
                continue

            if po_line.currency_id != self.currency_id:
                raise UserError(_(
                    "Cannot apply PO terms on %(product)s: its purchase order line is in "
                    "%(po_currency)s but this bill is in %(move_currency)s. Correct this "
                    "manually.",
                    product=line.product_id.display_name,
                    po_currency=po_line.currency_id.name,
                    move_currency=self.currency_id.name,
                ))

            target_price = po_line.product_uom._compute_price(po_line.price_unit, line.product_uom_id)
            # Work in the PO line's unit, which qty_to_invoice is rounded in, and
            # convert back once; converting each part separately drifts.
            billed_here = line.product_uom_id._compute_quantity(line.quantity, po_line.product_uom)
            target_qty = po_line.product_uom._compute_quantity(
                po_line.qty_to_invoice + billed_here, line.product_uom_id)

            changes = {}
            if float_compare(line.price_unit, target_price, precision_digits=price_precision) != 0:
                changes['price_unit'] = (line.price_unit, target_price)
            if float_compare(line.quantity, target_qty, precision_digits=qty_precision) != 0:
                changes['quantity'] = (line.quantity, target_qty)
            if changes:
                to_update.append((line, changes))

        if not to_update and not to_remove:
            if to_review:
                # Only shared PO lines: refuse rather than post the same note again.
                review_products = ", ".join(sorted(set(to_review.mapped('product_id.display_name'))))
                raise UserError(_(
                    "Cannot apply PO terms: %(products)s share a purchase order line with "
                    "another line on this bill. Resolve this manually, then try again.",
                    products=review_products,
                ))
            # Already at PO terms: nothing to write or post.
            return

        for line, changes in to_update:
            vals = {field: new for field, (_old, new) in changes.items()}
            line.write(vals)

        removed_lines_info = [
            (line.product_id.display_name, line.quantity, line.price_unit) for line in to_remove
        ]
        if to_remove:
            to_remove.unlink()

        self.message_post(body=self._po_terms_message_body(to_update, removed_lines_info, to_review))

    def _po_terms_message_body(self, to_update, removed_lines_info, to_review):
        """Build the chatter summary as safe HTML, escaping every interpolated value."""
        bullets = Markup("")

        for line, changes in to_update:
            product = line.product_id.display_name
            if 'price_unit' in changes and 'quantity' in changes:
                old_price, new_price = changes['price_unit']
                old_qty, new_qty = changes['quantity']
                tmpl = _(
                    "%(product)s: price %(old_price)s \u2192 %(new_price)s, "
                    "quantity %(old_qty)s \u2192 %(new_qty)s"
                )
                detail = Markup(tmpl) % {
                    'product': product,
                    'old_price': "%.2f" % old_price,
                    'new_price': "%.2f" % new_price,
                    'old_qty': "%.2f" % old_qty,
                    'new_qty': "%.2f" % new_qty,
                }
            elif 'price_unit' in changes:
                old_price, new_price = changes['price_unit']
                tmpl = _("%(product)s: price %(old)s \u2192 %(new)s")
                detail = Markup(tmpl) % {
                    'product': product,
                    'old': "%.2f" % old_price,
                    'new': "%.2f" % new_price,
                }
            else:
                old_qty, new_qty = changes['quantity']
                tmpl = _("%(product)s: quantity %(old)s \u2192 %(new)s")
                detail = Markup(tmpl) % {
                    'product': product,
                    'old': "%.2f" % old_qty,
                    'new': "%.2f" % new_qty,
                }
            bullets += Markup("<li>%s</li>") % detail

        for product_name, qty, price in removed_lines_info:
            tmpl = _(
                "%(product)s: removed, no purchase order line (was quantity %(qty)s, "
                "price %(price)s) \u2013 raise this with the vendor"
            )
            detail = Markup(tmpl) % {
                'product': product_name,
                'qty': "%.2f" % qty,
                'price': "%.2f" % price,
            }
            bullets += Markup("<li>%s</li>") % detail

        for line in to_review:
            tmpl = _(
                "%(product)s: purchase order line is shared with another line on this bill, "
                "left unchanged \u2013 needs manual review"
            )
            detail = Markup(tmpl) % {'product': line.product_id.display_name}
            bullets += Markup("<li>%s</li>") % detail

        return Markup("<p>%s</p><ul>%s</ul>") % (_("Applied PO terms:"), bullets)
