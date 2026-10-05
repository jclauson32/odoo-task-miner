{
    'name': 'Purchase Bill Match Columns',
    'version': '18.0.1.0.0',
    'summary': "Show the purchase order's quantity, received quantity and "
               "price next to each vendor bill line",
    'description': """
Adds three read-only columns (PO Qty, Received Qty, PO Price) to the vendor
bill line grid, sourced from the matched purchase order line, so reviewers
no longer have to open the purchase order or the receipt to compare values
against the bill.
""",
    'category': 'Accounting/Accounting',
    'license': 'LGPL-3',
    'depends': ['purchase'],
    'data': [
        'views/account_move_views.xml',
    ],
    'installable': True,
    'auto_install': False,
}
