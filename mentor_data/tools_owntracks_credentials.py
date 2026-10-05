"""
Full OwnTracks credential provisioning: generate a password, bcrypt-hash it,
write it into the Caddyfile's owntracks.defg.xyz basic_auth block (add or
replace), trigger Caddy's narrow internal reload sidecar, and link the
result in Mentor's own devices/device_services model. Automates what was
done by hand across several manual steps earlier - the family admin can now
provision or refresh someone's location credential in one call, from chat
or the dashboard.

CADDYFILE_PATH must be mounted read-write into whichever container imports
this (currently owntracks-mcp and mentor-dashboard).
"""
import json
import os
import re
import secrets
import string

import bcrypt
import httpx

from . import tools_devices

CADDYFILE_PATH = os.environ.get("CADDYFILE_PATH", "/caddy/Caddyfile")
CADDY_RELOAD_URL = os.environ.get("CADDY_RELOAD_URL", "http://caddy:9999/reload")
CADDY_RELOAD_TOKEN = os.environ.get("CADDY_RELOAD_TOKEN", "")

_SITE_BLOCK_RE = re.compile(r"(http://owntracks\.defg\.xyz\s*\{.*?basic_auth\s*\{)(.*?)(\n\t\})", re.DOTALL)
_CRED_LINE_RE = re.compile(r"^\t\t(\S+) (\S+)$", re.MULTILINE)


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=14)).decode()


def _update_caddyfile(username: str, password_hash: str) -> None:
    with open(CADDYFILE_PATH, "r+") as f:
        content = f.read()
        match = _SITE_BLOCK_RE.search(content)
        if not match:
            raise RuntimeError("could not find owntracks.defg.xyz basic_auth block in Caddyfile")
        creds_block = match.group(2)
        lines = [ln for ln in creds_block.split("\n") if ln.strip()]
        lines = [ln for ln in lines if not ln.strip().startswith(f"{username} ")]
        lines.append(f"\t\t{username} {password_hash}")
        new_creds_block = "\n" + "\n".join(lines)
        new_content = content[:match.start(2)] + new_creds_block + content[match.end(2):]
        f.seek(0)
        f.write(new_content)
        f.truncate()


def _trigger_reload() -> None:
    resp = httpx.post(CADDY_RELOAD_URL, headers={"Authorization": f"Bearer {CADDY_RELOAD_TOKEN}"}, timeout=15)
    resp.raise_for_status()


SERVICE_TYPE = "tracking"
PROVIDER = "owntracks"


def provision_credential(args: dict, **kwargs) -> str:
    """args: requesting_user_name, target_user_name (admin-gating is the
    caller's responsibility - this function does the actual work only)."""
    target_user_name = args["target_user_name"]
    username = target_user_name.strip().lower()
    if not re.match(r"^[a-z0-9_-]+$", username):
        return json.dumps({"error": f"'{target_user_name}' doesn't reduce to a safe credential username (got '{username}')"})

    # Resolve/validate the target user BEFORE touching Caddy at all - a
    # nonexistent user must never leave a real, unlinked credential behind.
    existing = json.loads(tools_devices.list_devices({"user_name": target_user_name}))
    if isinstance(existing, dict) and "error" in existing:
        return json.dumps(existing)
    if existing:
        device_id = existing[0]["id"]
    else:
        created = json.loads(tools_devices.create_device({
            "user_name": target_user_name, "name": f"{target_user_name}'s device", "type": "phone",
        }))
        if "error" in created:
            return json.dumps(created)
        device_id = created["id"]

    password = _generate_password()
    password_hash = _hash_password(password)

    try:
        _update_caddyfile(username, password_hash)
        _trigger_reload()
    except Exception as e:
        return json.dumps({"error": f"failed to provision credential: {e}"})

    tools_devices.attach_device_service({
        "device_id": device_id, "service_type": SERVICE_TYPE, "provider": PROVIDER, "external_id": username,
    })

    return json.dumps({
        "user_name": target_user_name,
        "app_username": username,
        "app_password": password,
        "url": "https://owntracks.defg.xyz/pub",
        "note": "Enter these in the OwnTracks app's Connection/Credentials screen. This password is shown "
                "once - if it's lost, provision again to generate a fresh one (the old one stops working).",
    })
