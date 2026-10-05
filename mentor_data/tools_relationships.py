"""
Static, directed edges between user nodes (e.g. Evgeniy --parent--> Jasmine).
Nothing here infers a reverse edge automatically - "parent" does not imply
"child" in code, since the correct reverse label depends on facts (gender,
relationship type) this system has no business guessing. Pass reciprocal_type
to add_user_relationship to record both directions explicitly in one call
when both are known.
"""
import json
from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_user(con, name: str):
    row = con.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def add_user_relationship(args: dict, **kwargs) -> str:
    from_name = args["from_user_name"]
    to_name = args["to_user_name"]
    relationship_type = args["relationship_type"]
    reciprocal_type = args.get("reciprocal_type")

    con = db.connect()
    from_id = _resolve_user(con, from_name)
    if from_id is None:
        con.close()
        return json.dumps({"error": f"no user named '{from_name}' - call create_user first"})
    to_id = _resolve_user(con, to_name)
    if to_id is None:
        con.close()
        return json.dumps({"error": f"no user named '{to_name}' - call create_user first"})

    now = _now()
    created = []
    con.execute(
        "INSERT OR IGNORE INTO user_relationships (from_user_id, to_user_id, relationship_type, created_at) "
        "VALUES (?,?,?,?)",
        (from_id, to_id, relationship_type, now),
    )
    created.append({"from_user_name": from_name, "to_user_name": to_name, "relationship_type": relationship_type})
    if reciprocal_type:
        con.execute(
            "INSERT OR IGNORE INTO user_relationships (from_user_id, to_user_id, relationship_type, created_at) "
            "VALUES (?,?,?,?)",
            (to_id, from_id, reciprocal_type, now),
        )
        created.append({"from_user_name": to_name, "to_user_name": from_name, "relationship_type": reciprocal_type})
    con.commit()
    con.close()
    return json.dumps({"ok": True, "created": created})


def list_user_relationships(args: dict, **kwargs) -> str:
    """Both directions: edges where this user is the source ('outgoing' -
    e.g. Evgeniy's 'parent' edge to Jasmine) and edges where it's the target
    ('incoming' - e.g. Jasmine's 'child' edge back to Evgeniy, if that
    reverse edge was also recorded)."""
    user_name = args["user_name"]
    con = db.connect()
    user_id = _resolve_user(con, user_name)
    if user_id is None:
        con.close()
        return json.dumps({"error": f"no user named '{user_name}'"})

    outgoing_rows = con.execute(
        "SELECT user_relationships.id, users.name, user_relationships.relationship_type "
        "FROM user_relationships JOIN users ON users.id = user_relationships.to_user_id "
        "WHERE user_relationships.from_user_id = ? ORDER BY users.name",
        (user_id,),
    ).fetchall()
    incoming_rows = con.execute(
        "SELECT user_relationships.id, users.name, user_relationships.relationship_type "
        "FROM user_relationships JOIN users ON users.id = user_relationships.from_user_id "
        "WHERE user_relationships.to_user_id = ? ORDER BY users.name",
        (user_id,),
    ).fetchall()
    con.close()

    return json.dumps({
        "user_name": user_name,
        "outgoing": [{"id": r[0], "to_user_name": r[1], "relationship_type": r[2]} for r in outgoing_rows],
        "incoming": [{"id": r[0], "from_user_name": r[1], "relationship_type": r[2]} for r in incoming_rows],
    })


def delete_user_relationship(args: dict, **kwargs) -> str:
    con = db.connect()
    exists = con.execute("SELECT id FROM user_relationships WHERE id = ?", (args["relationship_id"],)).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no relationship with id {args['relationship_id']}"})
    con.execute("DELETE FROM user_relationships WHERE id = ?", (args["relationship_id"],))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "deleted_id": args["relationship_id"]})
