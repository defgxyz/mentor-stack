import json
from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_health_metric(args: dict, **kwargs) -> str:
    now = _now()
    con = db.connect()
    cur = con.execute(
        "INSERT INTO health_metrics (metric_type, value, unit, note, objective_id, logged_at, source) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            args["metric_type"],
            args.get("value"),
            args.get("unit"),
            args.get("note"),
            args.get("objective_id"),
            now,
            args.get("source", "manual"),
        ),
    )
    con.commit()
    metric_id = cur.lastrowid
    con.close()
    return json.dumps({"id": metric_id, "metric_type": args["metric_type"], "logged_at": now})


def list_health_metrics(args: dict, **kwargs) -> str:
    con = db.connect()
    q = "SELECT id, metric_type, value, unit, note, logged_at FROM health_metrics WHERE 1=1"
    params = []
    if args.get("metric_type"):
        q += " AND metric_type = ?"
        params.append(args["metric_type"])
    if args.get("since"):
        q += " AND logged_at >= ?"
        params.append(args["since"])
    q += " ORDER BY logged_at DESC"
    if args.get("limit"):
        q += " LIMIT ?"
        params.append(args["limit"])
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "metric_type", "value", "unit", "note", "logged_at"]
    return json.dumps([dict(zip(cols, r)) for r in rows])
