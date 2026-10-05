"""
Persona assignment: splices a fixed-library persona (mentor_data/personas.py)
into a specific person's own SOUL.md, replacing the delimited
<!-- PERSONA:START/END --> region every profile's SOUL.md carries (seeded
with personas.DEFAULT_PERSONA_BLOCK by default - see the plan file for the
propagation pass that added it). Not a runtime lookup: since every Mentor
user already has their own physically separate Hermes profile/SOUL.md,
"this person's assigned persona" is just "whatever's currently written
into their one file" - no new mechanism needed beyond the same
/api/fs/read-text /write-text calls used throughout this project.

Access model (confirmed with the user before building): a family admin
can set anyone's persona; the person themselves can also set their own IF
their type is teen/adult/senior (self-choice) - NOT for toddler/
preschool/child, where only a family admin may change it.
"""
import json
import os
import re

from . import db
from . import personas
from . import tools_routines

HERMES_DASHBOARD_URL = os.environ.get("HERMES_DASHBOARD_URL", "http://hermes:9119")
HERMES_DASHBOARD_USERNAME = os.environ.get("HERMES_DASHBOARD_USERNAME", "")
HERMES_DASHBOARD_PASSWORD = os.environ.get("HERMES_DASHBOARD_PASSWORD", "")

_SELF_CHOICE_TYPES = {"teen", "adult", "senior"}

_PERSONA_RE = re.compile(
    r"<!-- PERSONA:START -->.*?<!-- PERSONA:END -->",
    re.DOTALL,
)


def _hermes_profile_for_user(user_name: str) -> str:
    return user_name.strip().lower()


def list_available_personas(args: dict, **kwargs) -> str:
    """Every persona option valid for this user's type, plus the always-
    available "default" (persona_key null) meaning "use the generic
    age-based calibration". Call this before set_persona to get real
    persona_key values - never guess one."""
    con = db.connect()
    row = con.execute("SELECT type, persona_key FROM users WHERE name = ?", (args["user_name"],)).fetchone()
    con.close()
    if row is None:
        return json.dumps({"error": f"no user named '{args['user_name']}'"})
    user_type, current = row
    return json.dumps({
        "user_name": args["user_name"], "type": user_type, "current_persona_key": current,
        "options": personas.personas_for_type(user_type),
    })


def _splice_persona(profile: str, heading_suffix: str, body: str) -> "str | None":
    """None on success, else an error string. Never raises."""
    if not HERMES_DASHBOARD_PASSWORD:
        return "HERMES_DASHBOARD_PASSWORD not configured - could not reach Hermes"
    try:
        import httpx
        with httpx.Client(base_url=HERMES_DASHBOARD_URL, timeout=20) as client:
            login = client.post("/auth/password-login", json={
                "provider": "basic", "username": HERMES_DASHBOARD_USERNAME, "password": HERMES_DASHBOARD_PASSWORD,
            })
            if login.status_code != 200:
                return f"could not authenticate to Hermes ({login.status_code})"
            path = f"/opt/data/profiles/{profile}/SOUL.md"
            read_resp = client.get("/api/fs/read-text", params={"path": path})
            if read_resp.status_code != 200:
                return f"could not read SOUL.md ({read_resp.status_code})"
            soul_text = read_resp.json().get("text", "")
            replacement = (
                f"<!-- PERSONA:START -->\n"
                f"## Your personality for this conversation{heading_suffix}\n\n"
                f"{body}\n"
                f"<!-- PERSONA:END -->"
            )
            if not _PERSONA_RE.search(soul_text):
                return "SOUL.md has no <!-- PERSONA:START/END --> region to replace - it may predate the persona feature"
            new_text = _PERSONA_RE.sub(replacement, soul_text, count=1)
            write_resp = client.post("/api/fs/write-text", json={"path": path, "content": new_text})
            if write_resp.status_code != 200:
                return f"could not write SOUL.md ({write_resp.status_code})"
        return None
    except Exception as e:
        return f"failed to update SOUL.md: {e}"


def set_persona(args: dict, **kwargs) -> str:
    """Assign (or reset, with persona_key=null) a fixed-library persona
    for target_user_name - immediately splices it into THEIR OWN SOUL.md
    (see personas.py for the library, tools_personas.py's own module
    docstring for the access model). requesting_user_name must be
    whoever you're actually talking to in THIS conversation, never a
    claimed identity. A family admin can set anyone's; the person
    themselves can set their own only if their type is teen/adult/senior
    - never for toddler/preschool/child, where a guardian decides."""
    requesting_user_name = args["requesting_user_name"]
    target_user_name = args["target_user_name"]
    persona_key = args.get("persona_key")

    con = db.connect()
    target_row = con.execute(
        "SELECT id, type FROM users WHERE name = ?", (target_user_name,)
    ).fetchone()
    if target_row is None:
        con.close()
        return json.dumps({"error": f"no user named '{target_user_name}'"})
    target_id, target_type = target_row

    is_admin = tools_routines.is_family_admin(requesting_user_name)
    is_self = requesting_user_name.strip().lower() == target_user_name.strip().lower()
    if not is_admin:
        if not is_self:
            con.close()
            return json.dumps({"error": f"'{requesting_user_name}' cannot set a persona for '{target_user_name}' "
                                         f"- only a family admin, or the person themselves (if old enough), can"})
        if target_type not in _SELF_CHOICE_TYPES:
            con.close()
            return json.dumps({"error": f"'{target_user_name}' is type '{target_type}' - only a family admin can "
                                         f"set a persona for toddler/preschool/child, not the person themselves"})

    if persona_key is not None:
        if persona_key not in personas.PERSONA_TYPE_SCOPE:
            con.close()
            return json.dumps({"error": f"unknown persona_key '{persona_key}' - call list_available_personas first"})
        if personas.PERSONA_TYPE_SCOPE[persona_key] != target_type:
            con.close()
            return json.dumps({"error": f"persona '{persona_key}' is for type "
                                         f"'{personas.PERSONA_TYPE_SCOPE[persona_key]}', but "
                                         f"'{target_user_name}' is type '{target_type}'"})

    heading_suffix, body = personas.persona_block(persona_key)
    profile = _hermes_profile_for_user(target_user_name)
    error = _splice_persona(profile, heading_suffix, body)
    if error:
        con.close()
        return json.dumps({"error": error})

    con.execute("UPDATE users SET persona_key = ? WHERE id = ?", (persona_key, target_id))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "user_name": target_user_name, "persona_key": persona_key,
                        "persona_name": personas.PERSONA_NAMES.get(persona_key, "Default (age-based calibration)")})
