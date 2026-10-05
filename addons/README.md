Custom Odoo modules go here. This folder is mounted into the Odoo container at
`/mnt/extra-addons`. Install or update a module, and run its tests, with:

```bash
./scripts/install_module.sh <module_name>
./scripts/test_module.sh <module_name>
```

Both stop Odoo's web server while they work and start it again afterwards; a
running server loading the same database can collide with an install.

Keep customizations here as separate modules rather than editing Odoo's code.
