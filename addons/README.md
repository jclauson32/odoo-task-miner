Custom Odoo modules go here. This folder is mounted into the Odoo container at
`/mnt/extra-addons`, so a module placed here can be installed from Apps after
restarting Odoo (`docker compose restart odoo`), or from the command line:

```bash
docker compose run --rm odoo odoo -d demo -i <module_name> --stop-after-init
```

Keep customizations here as separate modules rather than editing Odoo's code.
