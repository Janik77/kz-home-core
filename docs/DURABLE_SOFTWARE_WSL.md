# Persistent software profile

The current supported pins, fresh setup, start/stop, backup/restore and narrowly
supported upgrade/rollback are documented together in
[SOFTWARE_UPGRADE.md](SOFTWARE_UPGRADE.md).

Ordinary restore is exact-version only. The dedicated upgrade supports only the
specified Core #42 backup; it restores and migrates a new isolated profile. It
never changes the original profile's path/device/inode, volume or identity.
Older drafts without identity and other source combinations are refused.

No physical relay, firmware C, user WSL or production validation is claimed.
