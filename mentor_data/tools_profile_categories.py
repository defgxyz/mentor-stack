"""
Profile categories: a reusable set of goals/routines that applies to a
cohort of people (e.g. the six built-in age-stages - Toddler through Senior,
seeded by db.py - or a custom one like "New Parent"). Distinct from `users`
(real individuals) - a user is assigned to one or more profiles via
user_profiles, and apply_wellbeing_template (tools_routines.py) instantiates
whatever a profile's template rows imply onto a specific user.
"""
import json
from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_profile_id(con, profile_name: str):
    row = con.execute("SELECT id FROM profiles WHERE name = ?", (profile_name,)).fetchone()
    return row[0] if row else None


def _get_user_id(con, user_name: str):
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    return row[0] if row else None


def create_profile_category(args: dict, **kwargs) -> str:
    con = db.connect()
    existing = con.execute("SELECT id FROM profiles WHERE name = ?", (args["name"],)).fetchone()
    if existing is not None:
        con.close()
        return json.dumps({"error": f"a profile category named '{args['name']}' already exists (id {existing[0]})"})
    cur = con.execute(
        "INSERT INTO profiles (name, description, created_at) VALUES (?,?,?)",
        (args["name"], args.get("description"), _now()),
    )
    con.commit()
    profile_id = cur.lastrowid
    con.close()
    return json.dumps({"id": profile_id, "name": args["name"], "description": args.get("description")})


def list_profile_categories(args: dict, **kwargs) -> str:
    con = db.connect()
    rows = con.execute("SELECT id, name, description FROM profiles ORDER BY name").fetchall()
    con.close()
    cols = ["id", "name", "description"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def assign_user_profile(args: dict, **kwargs) -> str:
    """Assign a user to a profile category (e.g. Jasmine -> 'Child'). A user
    can be assigned to several categories at once. Does not itself create any
    objectives/routines - call apply_wellbeing_template afterward to actually
    instantiate what the category implies onto this user."""
    con = db.connect()
    user_id = _get_user_id(con, args["user_name"])
    if user_id is None:
        con.close()
        return json.dumps({"error": f"no user named '{args['user_name']}' - call create_user first"})
    profile_id = _get_profile_id(con, args["profile_name"])
    if profile_id is None:
        con.close()
        return json.dumps({"error": f"no profile category named '{args['profile_name']}' - call create_profile_category first"})
    con.execute(
        "INSERT OR IGNORE INTO user_profiles (user_id, profile_id, assigned_at) VALUES (?,?,?)",
        (user_id, profile_id, _now()),
    )
    con.commit()
    con.close()
    return json.dumps({"ok": True, "user_name": args["user_name"], "profile_name": args["profile_name"]})


def unassign_user_profile(args: dict, **kwargs) -> str:
    con = db.connect()
    user_id = _get_user_id(con, args["user_name"])
    profile_id = _get_profile_id(con, args["profile_name"])
    if user_id is None or profile_id is None:
        con.close()
        return json.dumps({"error": "user or profile category not found"})
    con.execute("DELETE FROM user_profiles WHERE user_id = ? AND profile_id = ?", (user_id, profile_id))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "user_name": args["user_name"], "profile_name": args["profile_name"]})


def list_user_profiles(args: dict, **kwargs) -> str:
    con = db.connect()
    user_id = _get_user_id(con, args["user_name"])
    if user_id is None:
        con.close()
        return json.dumps({"error": f"no user named '{args['user_name']}'"})
    rows = con.execute(
        "SELECT profiles.id, profiles.name, profiles.description FROM user_profiles "
        "JOIN profiles ON profiles.id = user_profiles.profile_id WHERE user_profiles.user_id = ? "
        "ORDER BY profiles.name",
        (user_id,),
    ).fetchall()
    con.close()
    cols = ["id", "name", "description"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def add_profile_template_objective(args: dict, **kwargs) -> str:
    """Add a template objective to a profile category - anyone assigned to
    this category will get this objective the next time apply_wellbeing_template
    runs for them (existing users already instantiated from this category
    won't retroactively get it - call apply_wellbeing_template again for them
    if you want it backfilled)."""
    con = db.connect()
    profile_id = _get_profile_id(con, args["profile_name"])
    if profile_id is None:
        con.close()
        return json.dumps({"error": f"no profile category named '{args['profile_name']}' - call create_profile_category first"})
    cur = con.execute(
        "INSERT INTO profile_template_objectives "
        "(profile_id, title, domain, follow_up_interval_days, source_note, essential) VALUES (?,?,?,?,?,?)",
        (profile_id, args["title"], args["domain"], args.get("follow_up_interval_days"),
         args.get("source_note"), args.get("essential", "optional")),
    )
    con.commit()
    template_id = cur.lastrowid
    con.close()
    return json.dumps({"id": template_id, "profile_name": args["profile_name"], "title": args["title"]})


def add_profile_template_routine(args: dict, **kwargs) -> str:
    """Add a template routine to a profile category - same instantiation
    semantics as add_profile_template_objective."""
    con = db.connect()
    profile_id = _get_profile_id(con, args["profile_name"])
    if profile_id is None:
        con.close()
        return json.dumps({"error": f"no profile category named '{args['profile_name']}' - call create_profile_category first"})
    cur = con.execute(
        "INSERT INTO profile_template_routines (profile_id, name, schedule) VALUES (?,?,?)",
        (profile_id, args["name"], args.get("schedule", "daily")),
    )
    con.commit()
    template_id = cur.lastrowid
    con.close()
    return json.dumps({"id": template_id, "profile_name": args["profile_name"], "name": args["name"]})


def list_profile_templates(args: dict, **kwargs) -> str:
    con = db.connect()
    profile_id = _get_profile_id(con, args["profile_name"])
    if profile_id is None:
        con.close()
        return json.dumps({"error": f"no profile category named '{args['profile_name']}'"})
    routine_rows = con.execute(
        "SELECT id, name, schedule FROM profile_template_routines WHERE profile_id = ? ORDER BY name",
        (profile_id,),
    ).fetchall()
    objective_rows = con.execute(
        "SELECT id, title, domain, follow_up_interval_days, source_note, essential "
        "FROM profile_template_objectives WHERE profile_id = ? ORDER BY title",
        (profile_id,),
    ).fetchall()
    con.close()
    routine_cols = ["id", "name", "schedule"]
    objective_cols = ["id", "title", "domain", "follow_up_interval_days", "source_note", "essential"]
    return json.dumps({
        "profile_name": args["profile_name"],
        "template_routines": [dict(zip(routine_cols, r)) for r in routine_rows],
        "template_objectives": [dict(zip(objective_cols, r)) for r in objective_rows],
    })


def delete_profile_template_objective(args: dict, **kwargs) -> str:
    con = db.connect()
    exists = con.execute(
        "SELECT id FROM profile_template_objectives WHERE id = ?", (args["template_id"],)
    ).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no template objective with id {args['template_id']}"})
    con.execute("DELETE FROM profile_template_objectives WHERE id = ?", (args["template_id"],))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "deleted_id": args["template_id"]})


def delete_profile_template_routine(args: dict, **kwargs) -> str:
    con = db.connect()
    exists = con.execute(
        "SELECT id FROM profile_template_routines WHERE id = ?", (args["template_id"],)
    ).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no template routine with id {args['template_id']}"})
    con.execute("DELETE FROM profile_template_routines WHERE id = ?", (args["template_id"],))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "deleted_id": args["template_id"]})
