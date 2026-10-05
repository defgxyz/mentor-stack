"""
Maps a Mentor user to their OwnTracks device identity (the "user" string
their phone's OwnTracks app is configured with). Location data itself lives
entirely in the self-hosted OwnTracks Recorder (owntracks-recorder), not
mirrored into mentor.db - same pattern as Open Wearables. owntracks-mcp
reads this mapping to translate a Mentor user_name into the OwnTracks
identity it needs to query the Recorder's API.
"""
import json

from . import db


def set_owntracks_user_id(args: dict, **kwargs) -> str:
    con = db.connect()
    row = con.execute("SELECT id FROM users WHERE name = ?", (args["user_name"],)).fetchone()
    if row is None:
        con.close()
        return json.dumps({"error": f"no user named '{args['user_name']}' - call create_user first"})
    con.execute("UPDATE users SET owntracks_user_id = ? WHERE id = ?", (args["owntracks_user_id"], row[0]))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "user_name": args["user_name"], "owntracks_user_id": args["owntracks_user_id"]})


def get_owntracks_user_id(user_name: str) -> str | None:
    con = db.connect()
    row = con.execute("SELECT owntracks_user_id FROM users WHERE name = ?", (user_name,)).fetchone()
    con.close()
    return row[0] if row else None
