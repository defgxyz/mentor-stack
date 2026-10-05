import json
from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_media(args: dict, **kwargs) -> str:
    now = _now()
    status = args.get("status", "in_progress")
    con = db.connect()
    cur = con.execute(
        "INSERT INTO media_log (media_type, title, status, rating, notes, started_at, finished_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            args["media_type"],
            args["title"],
            status,
            args.get("rating"),
            args.get("notes"),
            args.get("started_at", now if status == "in_progress" else None),
            args.get("finished_at"),
            now,
        ),
    )
    con.commit()
    media_id = cur.lastrowid
    con.close()
    return json.dumps({"id": media_id, "title": args["title"], "media_type": args["media_type"], "status": status})


_UPDATABLE_MEDIA_FIELDS = ("status", "rating", "notes", "started_at", "finished_at", "title")


def update_media(args: dict, **kwargs) -> str:
    media_id = args["media_id"]
    fields = {k: v for k, v in args.items() if k in _UPDATABLE_MEDIA_FIELDS and v is not None}
    if not fields:
        return json.dumps({"error": "no updatable fields provided"})

    con = db.connect()
    exists = con.execute("SELECT id FROM media_log WHERE id = ?", (media_id,)).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no media entry with id {media_id}"})

    # Marking something completed without an explicit finished_at stamps it now.
    if fields.get("status") == "completed" and "finished_at" not in fields:
        fields["finished_at"] = _now()

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE media_log SET {set_clause} WHERE id = ?", (*fields.values(), media_id))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "media_id": media_id, "updated": fields})


def list_media(args: dict, **kwargs) -> str:
    con = db.connect()
    q = "SELECT id, media_type, title, status, rating, started_at, finished_at FROM media_log WHERE 1=1"
    params = []
    if args.get("media_type"):
        q += " AND media_type = ?"
        params.append(args["media_type"])
    if args.get("status"):
        q += " AND status = ?"
        params.append(args["status"])
    q += " ORDER BY created_at DESC"
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "media_type", "title", "status", "rating", "started_at", "finished_at"]
    return json.dumps([dict(zip(cols, r)) for r in rows])
