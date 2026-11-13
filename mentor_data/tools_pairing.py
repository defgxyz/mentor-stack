"""
Self-service pairing approval for the Family Admin, from inside a normal
Mentor conversation - instead of needing operator/CLI access
(`hermes pairing approve`) every time a new family member wants to join.
Platform-generic (the `platform` arg defaults to "telegram"; "discord" works
identically) since Hermes's own pairing store is keyed the same way for any
platform - see PAIRING_DIR's `{platform}-pending.json`/`{platform}-approved.json`.

This is a careful, minimal reimplementation of the *validation* half of
Hermes's own gateway/pairing.py PairingStore (read directly from the real
source, not guessed): salted-SHA256 code hashing, the exact pending/approved
JSON file shapes and paths. Deliberately narrow - only the approve_code path,
not code generation, rate-limiting, or lockout (those already protect the
*generation* side when a stranger first messages the bot; this tool only ever
consumes an already-issued code).

Known fragility, accepted deliberately (user's own call): if a future Hermes
update changes this file format, this silently stops working until updated to
match. Confirmed empirically that is_approved() re-reads the file on every
call (no in-memory cache to also update), so a file-level approval takes
effect on the very next message - no gateway restart needed.
"""

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import db
from .tools_routines import (
    HERMES_DASHBOARD_URL, HERMES_DASHBOARD_USERNAME, HERMES_DASHBOARD_PASSWORD, is_family_admin,
)

# The three proactive-engagement skills every real person's own pulse should
# run - see ARCHITECTURE.md §3. Extend this list (and mentor-mcp's
# /due-pulse endpoint) if a new proactive mechanism is added later, rather
# than creating a new standalone cron for it.
MENTOR_PULSE_SKILLS = ["objective-followup", "services", "topic-checkin", "topic-notification"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _link_conversation(con, source: str, chat_id: str, user_id: int) -> None:
    con.execute(
        "INSERT OR IGNORE INTO conversations (source, chat_id, user_id, linked_at) VALUES (?,?,?,?)",
        (source, chat_id, user_id, _now()),
    )
    con.commit()

PAIRING_DIR = Path("/hermes_home/platforms/pairing")
CODE_TTL_SECONDS = 3600

# Same real-memory-isolation mechanism as mentor-dashboard's own chat
# (_hermes_profile_for_user) - every Mentor user, Evgeniy included, gets
# their own isolated Hermes profile now; "default" is a neutral, unclaimed
# profile nothing routes to on purpose (see the "Real per-user memory
# isolation" / "Evgeniy moved to his own profile" plan entries - the
# earlier special-case for Evgeniy caused a real bug and was removed).
HERMES_CONFIG_PATH = Path("/hermes_home/config.yaml")


def _ensure_profile_route(platform: str, chat_id: str, mentor_user_name: str) -> Optional[str]:
    """Add (or update) a gateway.profile_routes entry so this platform/chat_id
    routes to mentor_user_name's own isolated Hermes profile, instead of
    silently sharing the launch profile's memory with whoever else talks to
    the shared bot - same reasoning as the dashboard chat's per-user
    profile routing. Returns None on success (route added/updated - still
    needs a gateway restart to take effect, config.yaml is only read at
    startup), or an error string if the config file couldn't be read/
    written (never raises - a routing-config problem must not fail the
    pairing approval itself, which already succeeded)."""
    profile = mentor_user_name.strip().lower()
    try:
        import yaml
        with open(HERMES_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        gateway = cfg.setdefault("gateway", {})
        routes = gateway.setdefault("profile_routes", [])
        route_name = f"{platform}-{mentor_user_name}"
        chat_id = str(chat_id)
        routes[:] = [r for r in routes if not (isinstance(r, dict) and r.get("name") == route_name)]
        routes.append({
            "name": route_name, "platform": platform, "chat_id": chat_id, "profile": profile,
        })
        with open(HERMES_CONFIG_PATH, "w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        return None
    except Exception as e:
        return f"pairing approved and conversation linked, but failed to write the profile route: {e}"


def _ensure_mentor_pulse_cron(platform: str, chat_id: str, mentor_user_name: str) -> Optional[str]:
    """Auto-create this person's mentor-pulse cron (objective-followup +
    services + topic-checkin, one combined monitor-gated pulse - see
    ARCHITECTURE.md §3) the moment their real delivery chat_id becomes
    known. This is the correct point to do it: a cron needs a real chat_id
    to deliver to, which doesn't exist before pairing, so it can't be done
    at plain profile-creation time the way topic_checkins seeding is.

    Idempotent - skips if a mentor-pulse job already exists for this
    profile (checked by name, not assumed absent).

    Two REST calls, not a shell-out: POST /api/cron/jobs (create - its
    request model has no monitor_url field at all) then PUT /api/cron/jobs/
    {id} with {"updates": {"monitor_url": ...}} (the update path's raw dict
    reaches the same underlying field the CLI's --monitor-url does - traced
    directly in Hermes's own source, not guessed). Never raises - a
    cron-provisioning problem must not fail the pairing approval that
    already succeeded."""
    if not HERMES_DASHBOARD_PASSWORD:
        return "HERMES_DASHBOARD_PASSWORD not configured - could not create the mentor-pulse cron"
    profile = mentor_user_name.strip().lower()
    try:
        import httpx
        with httpx.Client(base_url=HERMES_DASHBOARD_URL, timeout=20) as client:
            login = client.post("/auth/password-login", json={
                "provider": "basic", "username": HERMES_DASHBOARD_USERNAME, "password": HERMES_DASHBOARD_PASSWORD,
            })
            if login.status_code != 200:
                return f"could not authenticate to Hermes ({login.status_code})"

            existing = client.get("/api/cron/jobs", params={"profile": profile})
            if existing.status_code == 200:
                jobs = existing.json()
                if isinstance(jobs, list) and any(
                    isinstance(j, dict) and j.get("name") == "mentor-pulse" for j in jobs
                ):
                    return None  # already exists - nothing to do

            create = client.post("/api/cron/jobs", params={"profile": profile}, json={
                "schedule": "1m", "name": "mentor-pulse",
                "deliver": f"{platform}:{chat_id}",
                "skills": MENTOR_PULSE_SKILLS,
            })
            if create.status_code >= 400:
                return f"mentor-pulse cron creation failed ({create.status_code}): {create.text[:200]}"
            job = create.json()
            job_id = job.get("id") or job.get("job_id") if isinstance(job, dict) else None
            if not job_id:
                return f"mentor-pulse cron created but no job id in the response: {job}"

            monitor_url = f"http://mentor-mcp:8000/due-pulse?user_name={mentor_user_name}"
            update = client.put(f"/api/cron/jobs/{job_id}", params={"profile": profile}, json={
                "updates": {"monitor_url": monitor_url},
            })
            if update.status_code >= 400:
                return (f"mentor-pulse cron created (job {job_id}) but failed to attach its "
                        f"monitor_url ({update.status_code}): {update.text[:200]}")
        return None
    except Exception as e:
        return f"failed to create mentor-pulse cron: {e}"


def _hash_code(code: str, salt: bytes) -> str:
    return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def approve_pairing_code(args: dict, **kwargs) -> str:
    """Approve a pending pairing code (platform defaults to "telegram"; pass
    "discord" etc - the mechanism is platform-generic, keyed only by the
    {platform}-pending.json/{platform}-approved.json file pair) so a new
    family member can start using Mentor. Only callable by a family-admin
    user - checked against the requesting_user_name you were told earlier in
    this conversation, not inferred. mentor_user_name is which Mentor user
    (from list_users - create_user first if they're new) this pairing
    belongs to; when given and that user exists, a real conversations row
    gets written linking the platform chat to them, so identity doesn't have
    to be re-inferred conversationally every future session, and their
    has_own_pairing flips to true automatically."""
    requesting_user = args["requesting_user_name"]
    if not is_family_admin(requesting_user):
        return json.dumps({
            "error": f"'{requesting_user}' is not a family admin - only a family "
                     "admin user can approve new pairings."
        })

    platform = args.get("platform", "telegram")
    code = args["code"].upper().strip()

    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    approved_path = PAIRING_DIR / f"{platform}-approved.json"

    pending = _load_json(pending_path)
    now = time.time()
    matched_key = None
    matched_entry = None
    for entry_id, entry in pending.items():
        created_at = entry.get("created_at")
        if created_at is None or (now - created_at) > CODE_TTL_SECONDS:
            continue
        salt_hex = entry.get("salt")
        entry_hash = entry.get("hash")
        if not salt_hex or not entry_hash:
            continue
        try:
            salt = bytes.fromhex(salt_hex)
        except ValueError:
            continue
        if secrets.compare_digest(_hash_code(code, salt), entry_hash):
            matched_key, matched_entry = entry_id, entry
            break

    if matched_entry is None:
        return json.dumps({"error": "that code is invalid or expired - ask them to message the bot again for a new one"})

    del pending[matched_key]
    _save_json(pending_path, pending)

    approved = _load_json(approved_path)
    chat_id = matched_entry["user_id"]
    display_name = matched_entry.get("user_name", "")
    approved[chat_id] = {"user_name": display_name, "approved_at": now}
    _save_json(approved_path, approved)

    result = {
        "ok": True, "platform": platform, "chat_id": chat_id,
        "display_name": display_name, "approved_by": requesting_user,
    }

    mentor_user_name = args.get("mentor_user_name")
    if mentor_user_name:
        con = db.connect()
        row = con.execute("SELECT id FROM users WHERE name = ?", (mentor_user_name,)).fetchone()
        if row is None:
            con.close()
            result["conversation_link_error"] = (
                f"no Mentor user named '{mentor_user_name}' - approved the pairing anyway, "
                "but call link_conversation once that user exists to record the link"
            )
        else:
            _link_conversation(con, platform, chat_id, row[0])
            # A real approved Telegram pairing IS "has their own paired
            # conversation" by definition - flip it here rather than
            # leaving it to a separate, easy-to-forget update_user call.
            con.execute("UPDATE users SET has_own_pairing = 1 WHERE id = ?", (row[0],))
            con.commit()
            con.close()
            result["linked_mentor_user"] = mentor_user_name
            route_error = _ensure_profile_route(platform, chat_id, mentor_user_name)
            if route_error:
                result["profile_route_error"] = route_error
            else:
                result["profile_route_note"] = (
                    f"{mentor_user_name} will use their own isolated Hermes profile on this "
                    "platform once the gateway restarts to pick up the new route - not "
                    "immediate, same as any other config.yaml change."
                )
            pulse_error = _ensure_mentor_pulse_cron(platform, chat_id, mentor_user_name)
            if pulse_error:
                result["mentor_pulse_error"] = pulse_error
            else:
                result["mentor_pulse_note"] = (
                    f"{mentor_user_name}'s proactive check-ins (objectives, services, topic "
                    "check-ins) are now live on this channel - no separate setup needed."
                )

    return json.dumps(result)


def list_pending_pairings(args: dict, **kwargs) -> str:
    """List pending (unapproved) pairing requests waiting for a family admin
    to approve, so you can tell them what's outstanding."""
    platform = args.get("platform", "telegram")
    pending = _load_json(PAIRING_DIR / f"{platform}-pending.json")
    now = time.time()
    out = []
    for entry_id, entry in pending.items():
        created_at = entry.get("created_at")
        if created_at is None or (now - created_at) > CODE_TTL_SECONDS:
            continue
        out.append({
            "user_name": entry.get("user_name", ""),
            "requested_at": created_at,
        })
    return json.dumps(out)


def link_conversation(args: dict, **kwargs) -> str:
    """Manually link a conversation source (platform + chat_id) to a Mentor
    user - for retroactive/manual cases approve_pairing_code doesn't cover,
    e.g. bootstrapping the very first admin's own pre-existing pairing, or
    fixing a mis-linked conversation. Most new pairings should go through
    approve_pairing_code's mentor_user_name instead, which does this
    automatically at the moment of approval."""
    con = db.connect()
    row = con.execute("SELECT id FROM users WHERE name = ?", (args["user_name"],)).fetchone()
    if row is None:
        con.close()
        return json.dumps({"error": f"no user named '{args['user_name']}' - call create_user first"})
    source = args.get("source", "telegram")
    _link_conversation(con, source, str(args["chat_id"]), row[0])
    con.close()
    result = {"ok": True, "user_name": args["user_name"], "source": source, "chat_id": str(args["chat_id"])}
    route_error = _ensure_profile_route(source, str(args["chat_id"]), args["user_name"])
    if route_error:
        result["profile_route_error"] = route_error
    else:
        result["profile_route_note"] = (
            f"{args['user_name']} will use their own isolated Hermes profile on this platform "
            "once the gateway restarts to pick up the new route - not immediate."
        )
    pulse_error = _ensure_mentor_pulse_cron(source, str(args["chat_id"]), args["user_name"])
    if pulse_error:
        result["mentor_pulse_error"] = pulse_error
    else:
        result["mentor_pulse_note"] = (
            f"{args['user_name']}'s proactive check-ins (objectives, services, topic "
            "check-ins) are now live on this channel - no separate setup needed."
        )
    return json.dumps(result)
