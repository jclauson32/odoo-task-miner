{
    'name': "Apply PO Terms on Vendor Bills",
    'version': '18.0.1.0.0',
    'summary': "Apply the purchase order's price, quantity, and line matching to a draft vendor bill",
    'description': """
Adds an "Apply PO Terms" button on draft vendor bills that records the bill
at the purchase order's terms, per payables policy:

* every product line linked to a purchase order line is rewritten to that
  line's unit price and to the quantity received/accepted less anything
  already billed elsewhere (UoM-converted if the bill line's unit differs
  from the PO line's);
* every product line with no purchase order line is removed (to be raised
  with the vendor);
* a single chatter message lists every change and removal, with old and new
  values, so the deviation is always explained.

Lines whose purchase order line is shared by more than one bill line are
left untouched and flagged for manual review rather than guessed at.
Confirming the bill remains a separate, manual step.
""",
    'category': 'Accounting/Accounting',
    'depends': ['account', 'purchase'],
    'data': [
        'views/account_move_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
}
