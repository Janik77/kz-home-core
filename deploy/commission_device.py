"""Explicit host-side ACL preparation for one permanently bound physical relay.

No database, broker connection, passwords, or application settings are accessed.
Password-file updates remain interactive mosquitto_passwd operator commands.
"""

import argparse
import ipaddress
import os
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASE_ACL = ROOT / "deploy/mosquitto/acl"
LOCAL_ACL = ROOT / "deploy/local/mqtt/acl"
MARKER = "# Operator-managed physical relay (commission_device.py)"


def identity(house_id: str, device_id: str) -> str:
    for value in (house_id, device_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise ValueError("Use the canonical topic-safe Core IDs")
    return "kzdevice-" + device_id


def validate_lan_ip(value: str) -> None:
    address = ipaddress.IPv4Address(value)
    if not any(
        address in ipaddress.IPv4Network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("An explicit RFC1918 LAN IPv4 address is required")


def render_acl(base: str, house_id: str, device_id: str) -> str:
    username = identity(house_id, device_id)
    current_user = None
    users = set()
    for line in base.splitlines():
        directive = line.strip()
        if not directive or directive.startswith("#"):
            continue
        if directive.startswith("user "):
            current_user = directive[5:]
            if not current_user or current_user in users:
                raise ValueError("Duplicate/empty base ACL user")
            users.add(current_user)
        elif current_user is None or not directive.startswith(
            ("topic read ", "topic write ")
        ):
            # Global/pattern permissions could silently apply to every device.
            raise ValueError("Only explicit per-user base ACL directives are supported")
    if MARKER in base or any(
        line.strip() == "user " + username for line in base.splitlines()
    ):
        raise ValueError("Base ACL conflicts with the physical identity")
    topic = f"kzhome/v1/{house_id}/{device_id}/"
    return (
        base.rstrip()
        + "\n\n"
        + MARKER
        + "\nuser "
        + username
        + "\n"
        + "topic read "
        + topic
        + "set\n"
        + "".join(
            "topic write " + topic + kind + "\n" for kind in ("ack", "state", "status")
        )
    )


def prepare_acl(
    base: str, path: Path, house_id: str, device_id: str, action: str
) -> None:
    """Refuse conflicting ownership/manual edits; reruns do not rewrite the file."""
    if action not in ("grant", "check", "revoke"):
        raise ValueError("Unknown ACL action")
    granted = render_acl(base, house_id, device_id)
    baseline = base.rstrip() + "\n"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("ACL symlinks are not supported")
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current is not None and current not in (baseline, granted):
        raise ValueError("Local ACL conflicts; review it without overwriting")
    desired = granted if action in ("grant", "check") else baseline
    if action == "check":
        if current != desired:
            raise ValueError("Local ACL does not match the intended binding")
        return
    if current == desired:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".acl-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(desired)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("grant", "check", "revoke"))
    parser.add_argument("--house-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--lan-ip", help="validate the optional LAN bind address")
    args = parser.parse_args()
    try:
        if args.lan_ip is not None:
            validate_lan_ip(args.lan_ip)
        prepare_acl(
            BASE_ACL.read_text(encoding="utf-8"),
            LOCAL_ACL,
            args.house_id,
            args.device_id,
            args.action,
        )
    except Exception:
        print(
            "FAIL: invalid IDs/LAN address, conflicting ACL or file access; no broker update applied"
        )
        raise SystemExit(1) from None
    print(
        "ACL prepared/checked; password update and broker recreation remain explicit operator steps"
    )


if __name__ == "__main__":
    main()
