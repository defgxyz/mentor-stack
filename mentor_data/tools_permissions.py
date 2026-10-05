"""Role-based permission grants - deliberately a real, data-driven table
(role_permissions) rather than a hardcoded rule buried in one function, so a
family admin can change policy (e.g. "let teens create their own
subscriptions too") without needing a code change.

"role" for permission purposes is NOT always users.type - see
effective_role(). users.role is a separate, admin-only-settable override:
type stays purely about age/tone calibration (personas, wording), role is
purely about what this person is allowed to do. Most people never get an
explicit role - they just fall back to their type.

is_family_admin is checked separately, always alongside a role_permissions
lookup, never instead of it - an admin can do anything gated here regardless
of what role_permissions says, matching how admin overrides already work
everywhere else in this project (pairing approval, persona-setting for
children, etc.).
"""
import json
from datetime import datetime, timezone

from . import db
from .tools_routines import is_family_admin


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_role(user_name: str) -> str | None:
    """users.role if an admin has explicitly set one, else users.type. None
    if the user doesn't exist."""
    con = db.connect()
    try:
        row = con.execute("SELECT role, type FROM users WHERE name = ?", (user_name,)).fetchone()
        if row is None:
            return None
        role, user_type = row
        return role or user_type
    finally:
        con.close()


def has_permission(user_name: str, permission: str) -> bool:
    """True if user_name is a family admin (always, regardless of
    role_permissions) OR their effective_role has been explicitly granted
    this permission. False (not an error) for a nonexistent user."""
    if is_family_admin(user_name):
        return True
    role = effective_role(user_name)
    if role is None:
        return False
    con = db.connect()
    try:
        row = con.execute(
            "SELECT 1 FROM role_permissions WHERE role = ? AND permission = ?", (role, permission),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def set_user_role(args: dict, **kwargs) -> str:
    """Set (or clear, with an empty string) a user's PERMISSION role,
    overriding their type for has_permission() checks only - never affects
    tone/persona calibration, which stays driven by type. Only a family
    admin may call this, checked against requesting_user_name - never
    self-service, unlike most other profile fields."""
    requesting_user = args["requesting_user_name"]
    if not is_family_admin(requesting_user):
        return json.dumps({
            "error": f"'{requesting_user}' is not a family admin - only a family "
                     "admin can set someone's permission role."
        })
    target_user_name = args["target_user_name"]
    role = args.get("role") or None
    con = db.connect()
    try:
        row = con.execute("SELECT id FROM users WHERE name = ?", (target_user_name,)).fetchone()
        if row is None:
            return json.dumps({"error": f"no user named '{target_user_name}'"})
        con.execute("UPDATE users SET role = ? WHERE id = ?", (role, row[0]))
        con.commit()
        return json.dumps({"ok": True, "user_name": target_user_name, "role": role})
    finally:
        con.close()


def grant_role_permission(args: dict, **kwargs) -> str:
    """Grant a permission to a role - e.g. grant_role_permission(role="teen",
    permission="create_subscription") to let teens start creating their own
    notification-topic subscriptions. Only a family admin may call this."""
    requesting_user = args["requesting_user_name"]
    if not is_family_admin(requesting_user):
        return json.dumps({
            "error": f"'{requesting_user}' is not a family admin - only a family "
                     "admin can change permission policy."
        })
    role, permission = args["role"], args["permission"]
    con = db.connect()
    try:
        con.execute(
            "INSERT OR IGNORE INTO role_permissions (role, permission, created_at) VALUES (?,?,?)",
            (role, permission, _now()),
        )
        con.commit()
        return json.dumps({"ok": True, "role": role, "permission": permission})
    finally:
        con.close()


def revoke_role_permission(args: dict, **kwargs) -> str:
    """Revoke a previously-granted role permission. Only a family admin may
    call this."""
    requesting_user = args["requesting_user_name"]
    if not is_family_admin(requesting_user):
        return json.dumps({
            "error": f"'{requesting_user}' is not a family admin - only a family "
                     "admin can change permission policy."
        })
    role, permission = args["role"], args["permission"]
    con = db.connect()
    try:
        con.execute("DELETE FROM role_permissions WHERE role = ? AND permission = ?", (role, permission))
        con.commit()
        return json.dumps({"ok": True, "role": role, "permission": permission, "revoked": True})
    finally:
        con.close()


def list_role_permissions(args: dict, **kwargs) -> str:
    """List the current permission policy - which roles have which
    permissions granted. Anyone can view this (it's policy, not a secret)."""
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT role, permission FROM role_permissions ORDER BY role, permission",
        ).fetchall()
        return json.dumps([{"role": r, "permission": p} for r, p in rows])
    finally:
        con.close()
