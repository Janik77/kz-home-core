# Integrated favorites software stack

The supported new combination is Core #43
`5b87f4b71a7a2a99d4dd3d288c7f1ec76ab781f2`, migration
`0011_device_favorites`, and unmodified Web #25
`d6533042b950ded2f8b90dbec1af5a05fd078ce0`.
The source Core tooling's intermediate pin 27b20c7 is replaced with the actual final
reviewed Core source; runtime files are unchanged. Web #25 independent review is complete.

Use [SOFTWARE_UPGRADE.md](SOFTWARE_UPGRADE.md) as the single setup/run/stop guide.
It separates a NEW profile from the narrowly supported #42 -> #43 upgrade and
rollback. Do not use earlier launch instructions or manually alter identity fields.

For disposable smoke after setup:
```bash
.venv-new/bin/python -B core-new/tools/run_software_stack.py --web-root web-new --smoke-test
```

This smoke mode deletes its own temporary data. Persistent profiles retain data on
Ctrl-C. CI verifies the exact integration head (recorded in the draft PR), Core
regressions, actual PostgreSQL/MQTT TLS scenes, persistent restart/backup/restore,
supported upgrade/rollback, and pinned Web unit/demo/contract/live tests. Main and
source branches remain untouched. No merge/deploy or physical qualification.
