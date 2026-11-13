import json
from datetime import datetime, timedelta, timezone

from . import db
from . import scheduling
from . import tools_memory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_follow_up(domain: str, interval_days: int | None, from_time: datetime,
                     cron_expr: str | None = None) -> tuple[int, str]:
    """cron_expr (a real 5-field cron expression, e.g. "0 9 * * 1" for every
    Monday 9am) takes precedence over the flat day-interval when set - lets a
    follow-up land on a specific day/time instead of "N days from whenever it
    last fired", which drifts across weekdays over time. follow_up_interval_days
    is still returned/stored either way (NOT NULL column, and a sensible
    fallback if schedule_cron is ever cleared later) - it just isn't what
    computes next_follow_up_at when cron_expr is set."""
    days = interval_days or db.DEFAULT_FOLLOW_UP_DAYS.get(domain, 14)
    if cron_expr:
        return days, scheduling.next_from_cron(cron_expr, from_time)
    return days, (from_time + timedelta(days=days)).isoformat()


class _UserNotFound(Exception):
    def __init__(self, name: str):
        self.name = name


_VALID_ESSENTIAL = ("essential", "optional")
_VALID_MONEY_FREQUENCY = ("one_off", "weekly", "monthly", "yearly")
_VALID_VISIBILITY = ("shared", "private")


def _validate_effort_fields(args: dict) -> str | None:
    """Returns an error string if essential/money_cost_frequency/visibility are
    set to an unrecognized value, else None. Existing DBs got these columns via
    a plain ALTER TABLE (no CHECK constraint, unlike a fresh _SCHEMA), so this
    is the only enforcement for anyone who already had a mentor.db before this
    migration ran."""
    essential = args.get("essential")
    if essential is not None and essential not in _VALID_ESSENTIAL:
        return f"essential must be one of {_VALID_ESSENTIAL}, got '{essential}'"
    freq = args.get("money_cost_frequency")
    if freq is not None and freq not in _VALID_MONEY_FREQUENCY:
        return f"money_cost_frequency must be one of {_VALID_MONEY_FREQUENCY}, got '{freq}'"
    visibility = args.get("visibility")
    if visibility is not None and visibility not in _VALID_VISIBILITY:
        return f"visibility must be one of {_VALID_VISIBILITY}, got '{visibility}'"
    return None


def _resolve_user_id(con, user_name) -> int | None:
    """None (unscoped) when user_name is omitted; raises _UserNotFound for
    a name that doesn't match any user, rather than silently creating one -
    unlike routines, an objective is plausible without any user at all, so
    "no user_name given" and "user_name given but wrong" must stay
    distinguishable."""
    if user_name is None:
        return None
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    if row is None:
        raise _UserNotFound(user_name)
    return row[0]


def acknowledge_followup(args: dict, **kwargs) -> str:
    """Reschedule next_follow_up_at forward without logging progress - call this
    after delivering a check-in so an ignored objective still resurfaces after
    another full interval, instead of getting stuck silently due forever (the
    monitor only fires on a change in the due-set, so nothing else would ever
    move it forward again)."""
    now_dt = datetime.now(timezone.utc)
    con = db.connect()
    row = con.execute(
        "SELECT domain, follow_up_interval_days, schedule_cron FROM objectives WHERE id = ?",
        (args["objective_id"],),
    ).fetchone()
    if row is None:
        con.close()
        return json.dumps({"error": f"no objective with id {args['objective_id']}"})
    domain, interval_days, cron_expr = row
    _, next_follow_up_at = _next_follow_up(domain, interval_days, now_dt, cron_expr)
    con.execute(
        "UPDATE objectives SET next_follow_up_at = ? WHERE id = ?",
        (next_follow_up_at, args["objective_id"]),
    )
    con.commit()
    con.close()
    return json.dumps({"ok": True, "objective_id": args["objective_id"], "next_follow_up_at": next_follow_up_at})


def create_objective(args: dict, **kwargs) -> str:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    cron_expr = args.get("schedule_cron") or None
    if cron_expr and not scheduling.validate_cron(cron_expr):
        return json.dumps({"error": f"'{cron_expr}' is not a valid 5-field cron expression "
                                     "(e.g. '0 9 * * 1' for every Monday at 9am)"})
    interval_days, next_follow_up_at = _next_follow_up(
        args["domain"], args.get("follow_up_interval_days"), now_dt, cron_expr)
    parent_id = args.get("parent_objective_id")
    err = _validate_effort_fields(args)
    if err is not None:
        return json.dumps({"error": err})
    con = db.connect()
    if parent_id is not None:
        parent = con.execute("SELECT id FROM objectives WHERE id = ?", (parent_id,)).fetchone()
        if parent is None:
            con.close()
            return json.dumps({"error": f"no objective with id {parent_id} to attach as parent"})
    try:
        user_id = _resolve_user_id(con, args.get("user_name"))
    except _UserNotFound as e:
        con.close()
        return json.dumps({"error": f"no user named '{e.name}' - call create_user first, or omit user_name for an unscoped objective"})
    cur = con.execute(
        "INSERT INTO objectives (title, domain, description, target_date, priority, created_at, source_note, "
        "follow_up_interval_days, schedule_cron, next_follow_up_at, parent_objective_id, user_id, "
        "time_cost_hours_per_week, money_cost_amount, money_cost_currency, money_cost_frequency, essential, "
        "visibility) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            args["title"],
            args["domain"],
            args.get("description"),
            args.get("target_date"),
            args.get("priority", 3),
            now,
            args.get("source_note"),
            interval_days,
            cron_expr,
            next_follow_up_at,
            parent_id,
            user_id,
            args.get("time_cost_hours_per_week"),
            args.get("money_cost_amount"),
            args.get("money_cost_currency"),
            args.get("money_cost_frequency"),
            args.get("essential", "optional"),
            args.get("visibility", "shared"),
        ),
    )
    con.commit()
    obj_id = cur.lastrowid
    con.close()
    return json.dumps({
        "id": obj_id, "title": args["title"], "domain": args["domain"], "status": "active",
        "next_follow_up_at": next_follow_up_at, "parent_objective_id": parent_id,
        "user_name": args.get("user_name"),
        "time_cost_hours_per_week": args.get("time_cost_hours_per_week"),
        "money_cost_amount": args.get("money_cost_amount"),
        "money_cost_currency": args.get("money_cost_currency"),
        "money_cost_frequency": args.get("money_cost_frequency"),
        "essential": args.get("essential", "optional"),
        "visibility": args.get("visibility", "shared"),
    })


_UPDATABLE_OBJECTIVE_FIELDS = (
    "title", "description", "domain", "status", "priority", "target_date",
    "follow_up_interval_days", "schedule_cron", "next_follow_up_at",
    "time_cost_hours_per_week", "money_cost_amount", "money_cost_currency",
    "money_cost_frequency", "essential", "visibility",
)


def update_objective(args: dict, **kwargs) -> str:
    """Update any subset of an objective's fields directly - including status
    (pause/complete/abandon/reactivate), priority, title/description corrections,
    reassigning user_name (e.g. an objective created before users
    existed, or filed under the wrong person), rescheduling next_follow_up_at
    or follow_up_interval_days explicitly without needing a progress update as
    a side effect (unlike log_progress, which reschedules but requires a
    progress entry), and setting/correcting effort fields (time_cost_hours_per_week,
    money_cost_amount/currency/frequency, essential) once you know them."""
    obj_id = args["objective_id"]
    err = _validate_effort_fields(args)
    if err is not None:
        return json.dumps({"error": err})
    if args.get("schedule_cron") and not scheduling.validate_cron(args["schedule_cron"]):
        return json.dumps({"error": f"'{args['schedule_cron']}' is not a valid 5-field cron expression "
                                     "(e.g. '0 9 * * 1' for every Monday at 9am)"})
    fields = {k: v for k, v in args.items() if k in _UPDATABLE_OBJECTIVE_FIELDS and v is not None}
    if "schedule_cron" in args and args["schedule_cron"] == "":
        fields["schedule_cron"] = None  # explicit clear, back to the flat day-interval

    con = db.connect()
    if "user_name" in args and args["user_name"] is not None:
        try:
            fields["user_id"] = _resolve_user_id(con, args["user_name"])
        except _UserNotFound as e:
            con.close()
            return json.dumps({"error": f"no user named '{e.name}'"})

    if not fields:
        con.close()
        return json.dumps({"error": "no updatable fields provided"})

    exists = con.execute("SELECT id FROM objectives WHERE id = ?", (obj_id,)).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no objective with id {obj_id}"})

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE objectives SET {set_clause} WHERE id = ?", (*fields.values(), obj_id))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "objective_id": obj_id, "updated": fields})


def update_milestone(args: dict, **kwargs) -> str:
    """Update a milestone's status (e.g. mark it done/skipped), title, or
    target_date. There was previously no way to mark a milestone complete at
    all after add_milestone created it - this closes that gap."""
    milestone_id = args["milestone_id"]
    fields = {}
    if args.get("title") is not None:
        fields["title"] = args["title"]
    if args.get("target_date") is not None:
        fields["target_date"] = args["target_date"]
    status = args.get("status")
    if status is not None:
        fields["status"] = status

    if not fields:
        return json.dumps({"error": "no updatable fields provided"})

    con = db.connect()
    exists = con.execute("SELECT id FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no milestone with id {milestone_id}"})

    if status == "done":
        fields["completed_at"] = _now()

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE milestones SET {set_clause} WHERE id = ?", (*fields.values(), milestone_id))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "milestone_id": milestone_id, "updated": fields})


def add_milestone(args: dict, **kwargs) -> str:
    now = _now()
    con = db.connect()
    cur = con.execute(
        "INSERT INTO milestones (objective_id, title, order_index, target_date, created_at) "
        "VALUES (?,?,?,?,?)",
        (
            args["objective_id"],
            args["title"],
            args.get("order_index", 0),
            args.get("target_date"),
            now,
        ),
    )
    con.commit()
    milestone_id = cur.lastrowid
    con.close()
    return json.dumps({"id": milestone_id, "objective_id": args["objective_id"], "title": args["title"]})


def log_progress(args: dict, **kwargs) -> str:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    con = db.connect()
    con.execute(
        "INSERT INTO progress_entries (objective_id, milestone_id, note, metric_value, metric_unit, logged_at, sentiment) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            args["objective_id"],
            args.get("milestone_id"),
            args.get("note"),
            args.get("metric_value"),
            args.get("metric_unit"),
            now,
            args.get("sentiment"),
        ),
    )
    # Progress counts as a real check-in - push the next follow-up back out from now,
    # using the objective's own domain/interval rather than re-deriving one here.
    row = con.execute(
        "SELECT domain, follow_up_interval_days, schedule_cron FROM objectives WHERE id = ?",
        (args["objective_id"],),
    ).fetchone()
    next_follow_up_at = None
    if row is not None:
        domain, interval_days, cron_expr = row
        _, next_follow_up_at = _next_follow_up(domain, interval_days, now_dt, cron_expr)
        con.execute(
            "UPDATE objectives SET last_progress_at = ?, next_follow_up_at = ? WHERE id = ?",
            (now, next_follow_up_at, args["objective_id"]),
        )
    con.commit()
    con.close()
    return json.dumps({
        "ok": True, "objective_id": args["objective_id"], "logged_at": now,
        "next_follow_up_at": next_follow_up_at,
    })


def list_objectives(args: dict, **kwargs) -> str:
    con = db.connect()
    q = ("SELECT objectives.id, objectives.title, objectives.domain, objectives.status, objectives.priority, "
         "objectives.target_date, objectives.last_progress_at, objectives.next_follow_up_at, "
         "objectives.parent_objective_id, users.name, objectives.time_cost_hours_per_week, "
         "objectives.money_cost_amount, objectives.money_cost_currency, objectives.money_cost_frequency, "
         "objectives.essential, objectives.visibility FROM objectives "
         "LEFT JOIN users ON users.id = objectives.user_id WHERE 1=1")
    params = []
    if args.get("status"):
        q += " AND objectives.status = ?"
        params.append(args["status"])
    if args.get("domain"):
        q += " AND objectives.domain = ?"
        params.append(args["domain"])
    if args.get("top_level_only"):
        q += " AND objectives.parent_objective_id IS NULL"
    if "parent_objective_id" in args and args["parent_objective_id"] is not None:
        q += " AND objectives.parent_objective_id = ?"
        params.append(args["parent_objective_id"])
    if args.get("user_name"):
        q += " AND users.name = ?"
        params.append(args["user_name"])
    if args.get("visibility"):
        q += " AND objectives.visibility = ?"
        params.append(args["visibility"])
    q += " ORDER BY objectives.priority DESC, objectives.created_at DESC"
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "title", "domain", "status", "priority", "target_date", "last_progress_at",
            "next_follow_up_at", "parent_objective_id", "user_name", "time_cost_hours_per_week",
            "money_cost_amount", "money_cost_currency", "money_cost_frequency", "essential", "visibility"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def list_due_followups(args: dict, **kwargs) -> str:
    """Objectives whose next_follow_up_at has passed - the cheap external checker
    verifies this same condition via raw SQL before ever triggering an agent run;
    this tool is what the agent itself calls once invoked, to get the specifics.
    Optional user_name scopes to just that person's own objectives (the real
    per-person cron path - each person's pulse must only ever see THEIR OWN due
    followups, never anyone else's); omitted, it returns everyone's, for
    inspection/debugging only."""
    con = db.connect()
    now = _now()
    query = (
        "SELECT objectives.id, objectives.title, objectives.domain, objectives.status, objectives.priority, "
        "objectives.target_date, objectives.last_progress_at, objectives.next_follow_up_at, users.name "
        "FROM objectives LEFT JOIN users ON users.id = objectives.user_id "
        "WHERE objectives.status = 'active' AND objectives.next_follow_up_at IS NOT NULL "
        "AND objectives.next_follow_up_at <= ?"
    )
    params = [now]
    if args.get("user_name"):
        query += " AND users.name = ?"
        params.append(args["user_name"])
    query += " ORDER BY objectives.next_follow_up_at ASC"
    rows = con.execute(query, params).fetchall()
    con.close()
    cols = ["id", "title", "domain", "status", "priority", "target_date", "last_progress_at",
            "next_follow_up_at", "user_name"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def get_objective(args: dict, **kwargs) -> str:
    con = db.connect()
    obj_row = con.execute(
        "SELECT objectives.id, objectives.title, objectives.domain, objectives.description, objectives.status, "
        "objectives.priority, objectives.target_date, objectives.created_at, objectives.last_progress_at, "
        "objectives.source_note, objectives.follow_up_interval_days, objectives.next_follow_up_at, "
        "objectives.parent_objective_id, users.name, objectives.time_cost_hours_per_week, "
        "objectives.money_cost_amount, objectives.money_cost_currency, objectives.money_cost_frequency, "
        "objectives.essential, objectives.visibility FROM objectives "
        "LEFT JOIN users ON users.id = objectives.user_id WHERE objectives.id = ?",
        (args["objective_id"],),
    ).fetchone()
    if obj_row is None:
        con.close()
        return json.dumps({"error": f"no objective with id {args['objective_id']}"})

    obj_cols = ["id", "title", "domain", "description", "status", "priority", "target_date", "created_at",
                "last_progress_at", "source_note", "follow_up_interval_days", "next_follow_up_at",
                "parent_objective_id", "user_name", "time_cost_hours_per_week", "money_cost_amount",
                "money_cost_currency", "money_cost_frequency", "essential", "visibility"]
    objective = dict(zip(obj_cols, obj_row))

    if objective["parent_objective_id"] is not None:
        parent_row = con.execute(
            "SELECT id, title, domain, status FROM objectives WHERE id = ?",
            (objective["parent_objective_id"],),
        ).fetchone()
        if parent_row is not None:
            objective["parent"] = dict(zip(["id", "title", "domain", "status"], parent_row))

    child_rows = con.execute(
        "SELECT id, title, domain, status, priority, next_follow_up_at FROM objectives "
        "WHERE parent_objective_id = ? ORDER BY priority DESC, created_at DESC",
        (args["objective_id"],),
    ).fetchall()
    child_cols = ["id", "title", "domain", "status", "priority", "next_follow_up_at"]
    objective["children"] = [dict(zip(child_cols, r)) for r in child_rows]

    milestone_rows = con.execute(
        "SELECT id, title, order_index, status, target_date, completed_at FROM milestones "
        "WHERE objective_id = ? ORDER BY order_index",
        (args["objective_id"],),
    ).fetchall()
    milestone_cols = ["id", "title", "order_index", "status", "target_date", "completed_at"]
    objective["milestones"] = [dict(zip(milestone_cols, r)) for r in milestone_rows]

    progress_rows = con.execute(
        "SELECT id, milestone_id, note, metric_value, metric_unit, logged_at, sentiment FROM progress_entries "
        "WHERE objective_id = ? ORDER BY logged_at DESC",
        (args["objective_id"],),
    ).fetchall()
    progress_cols = ["id", "milestone_id", "note", "metric_value", "metric_unit", "logged_at", "sentiment"]
    objective["progress"] = [dict(zip(progress_cols, r)) for r in progress_rows]

    service_rows = con.execute(
        "SELECT id, title, status, schedule_interval_days, last_checked_at, next_check_at "
        "FROM services WHERE objective_id = ? ORDER BY created_at",
        (args["objective_id"],),
    ).fetchall()
    service_cols = ["service_id", "title", "status", "schedule_interval_days", "last_checked_at", "next_check_at"]
    objective["services"] = [dict(zip(service_cols, r)) for r in service_rows]

    con.close()

    # fact_store facts tagged `obj:<id>` by convention (see SOUL.md's
    # Objectives section) - broader conversational context the agent judged
    # worth linking to this objective, distinct from progress_entries (which
    # only holds what was explicitly logged via log_progress). Both feed
    # "tell me the story of this objective"; neither replaces the other.
    # Best-effort: a missing/unreadable memory store must not break get_objective.
    objective["tagged_facts"] = []
    if objective.get("user_name"):
        tag_marker = f"obj:{args['objective_id']}"
        facts, _facts_error = tools_memory.read_holographic_facts(objective["user_name"])
        objective["tagged_facts"] = [
            {"fact_id": f["fact_id"], "content": f["content"], "created_at": f["created_at"]}
            for f in facts
            if tag_marker in [t.strip() for t in f.get("tags", "").split(",")]
        ]

    return json.dumps(objective)
