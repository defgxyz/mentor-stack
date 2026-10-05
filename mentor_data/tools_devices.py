"""
Generic User -> Device -> Service model. A device belongs to one user and
has a type (phone, tablet, laptop, watch, ...). A service is one integration
attached to a device (service_type + provider + whatever identifier that
provider's own API needs) - a device can have several (e.g. "tracking" via
"owntracks" today; "screentime" via some future provider on the same phone
tomorrow). This is the generic layer; the actual integration logic (calling
OwnTracks's API, etc.) lives in that integration's own MCP service, which
resolves a user's external_id through here rather than storing its own copy.
"""
import json
from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_device(args: dict, **kwargs) -> str:
    con = db.connect()
    row = con.execute("SELECT id FROM users WHERE name = ?", (args["user_name"],)).fetchone()
    if row is None:
        con.close()
        return json.dumps({"error": f"no user named '{args['user_name']}' - call create_user first"})
    cur = con.execute(
        "INSERT INTO devices (user_id, name, type, created_at) VALUES (?,?,?,?)",
        (row[0], args["name"], args["type"], _now()),
    )
    con.commit()
    device_id = cur.lastrowid
    con.close()
    return json.dumps({"id": device_id, "user_name": args["user_name"], "name": args["name"], "type": args["type"]})


def list_devices(args: dict, **kwargs) -> str:
    con = db.connect()
    q = "SELECT devices.id, devices.name, devices.type, users.name FROM devices JOIN users ON users.id = devices.user_id"
    params = []
    if args.get("user_name"):
        q += " WHERE users.name = ?"
        params.append(args["user_name"])
    q += " ORDER BY users.name, devices.name"
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "name", "type", "user_name"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def attach_device_service(args: dict, **kwargs) -> str:
    """Attach (or update) one service on a device - service_type is free text
    ("tracking", "screentime", ...), provider names which integration
    implements it ("owntracks", ...), external_id is whatever identifier
    that provider's own API needs to address this device."""
    con = db.connect()
    device = con.execute("SELECT id FROM devices WHERE id = ?", (args["device_id"],)).fetchone()
    if device is None:
        con.close()
        return json.dumps({"error": f"no device with id {args['device_id']}"})
    existing = con.execute(
        "SELECT id FROM device_services WHERE device_id = ? AND service_type = ? AND provider = ?",
        (args["device_id"], args["service_type"], args["provider"]),
    ).fetchone()
    if existing is not None:
        con.execute("UPDATE device_services SET external_id = ? WHERE id = ?", (args.get("external_id"), existing[0]))
        service_id = existing[0]
    else:
        cur = con.execute(
            "INSERT INTO device_services (device_id, service_type, provider, external_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (args["device_id"], args["service_type"], args["provider"], args.get("external_id"), _now()),
        )
        service_id = cur.lastrowid
    con.commit()
    con.close()
    return json.dumps({"id": service_id, "device_id": args["device_id"], "service_type": args["service_type"],
                        "provider": args["provider"], "external_id": args.get("external_id")})


def list_device_services(args: dict, **kwargs) -> str:
    con = db.connect()
    q = (
        "SELECT device_services.id, device_services.service_type, device_services.provider, "
        "device_services.external_id, devices.name, devices.type, users.name "
        "FROM device_services JOIN devices ON devices.id = device_services.device_id "
        "JOIN users ON users.id = devices.user_id WHERE 1=1"
    )
    params = []
    if args.get("user_name"):
        q += " AND users.name = ?"
        params.append(args["user_name"])
    if args.get("device_id"):
        q += " AND devices.id = ?"
        params.append(args["device_id"])
    if args.get("service_type"):
        q += " AND device_services.service_type = ?"
        params.append(args["service_type"])
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "service_type", "provider", "external_id", "device_name", "device_type", "user_name"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def get_service_external_id(user_name: str, service_type: str, provider: str) -> str | None:
    """Internal helper (not an MCP tool) for another service's own MCP wrapper
    to resolve a Mentor user's external_id for one of their devices, without
    needing to know about devices/services beyond this one lookup. Returns
    the first match if a user has more than one device with the same
    service_type+provider attached (e.g. two phones both tracking) - fine
    for the common one-device-per-service case; callers needing to
    distinguish specific devices should use list_device_services directly."""
    con = db.connect()
    row = con.execute(
        "SELECT device_services.external_id FROM device_services "
        "JOIN devices ON devices.id = device_services.device_id "
        "JOIN users ON users.id = devices.user_id "
        "WHERE users.name = ? AND device_services.service_type = ? AND device_services.provider = ? "
        "ORDER BY device_services.id LIMIT 1",
        (user_name, service_type, provider),
    ).fetchone()
    con.close()
    return row[0] if row else None
