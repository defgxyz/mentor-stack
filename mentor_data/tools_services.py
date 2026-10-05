"""Service: the generic recurring-job mechanism - generalized from the
original Watchlist ("periodically check something external against my
criteria, tell me only when something new shows up"). A Service runs
either on a schedule (trigger_type='cron', Watchlist's original behaviour)
or in reaction to new posts on a notification topic (trigger_type='topic',
trigger_topic_id) - enabling real chaining (Service A posts to topic X,
Service B is triggered by new posts on topic X, and so on). Either way it
can run a deterministic script, an open-ended agent prompt, or both (script
first via the terminal tool, then the prompt reasons over the result) - in
a sense it's either a script or an agent, same shape either way. See
skills/mentor/services/SKILL.md for the actual run procedure this backs.

Delivery (which channel a person's own service findings actually reach
them on) is NOT decided here - same as tools_topics.py, it's a cron
--deliver target fixed at creation time; changing it later means
recreating the cron job.
"""
import json
from datetime import datetime, timedelta, timezone

from . import db
from . import scheduling

STATUSES = ("active", "paused", "completed", "abandoned")
TRIGGER_TYPES = ("cron", "topic")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_user(con, user_name: str):
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    return row[0] if row else None


def _ensure_output_topic(con, user_id: int, title: str, category) -> int:
    """Every service must always have a real output_topic_id - mandatory
    wiring, not an optional extra: delivery to the owner (and anyone else
    who ever wants to hear about it) always goes through the topic/
    subscription mechanism, never a direct reply from the services skill.
    Auto-creates a new topic "about" the owner (named after the service)
    and auto-subscribes the owner to it. This is structural/system
    scaffolding, not a discretionary self-service action, so it bypasses
    create_notification_topic/create_topic_subscription's own permission
    gate (that gate exists for a PERSON choosing to create a topic, not
    for wiring every service needs regardless of role/permissions)."""
    now = _now()
    cur = con.execute(
        "INSERT INTO notification_topics (subject_user_id, title, category, created_at) VALUES (?,?,?,?)",
        (user_id, f"{title} updates", category, now),
    )
    topic_id = cur.lastrowid
    con.execute(
        "INSERT OR IGNORE INTO topic_subscriptions (topic_id, subscriber_user_id, created_at) VALUES (?,?,?)",
        (topic_id, user_id, now),
    )
    return topic_id


def _row_to_dict(row) -> dict:
    (service_id, user_name, title, category, prompt, sources, script, objective_id, output_topic_id,
     trigger_type, trigger_topic_id, interval_days, cron_expr, status, last_checked_at, next_check_at,
     created_at) = row
    return {
        "service_id": service_id, "user_name": user_name, "title": title,
        "category": category, "prompt": prompt, "sources": sources,
        "script": script, "objective_id": objective_id, "output_topic_id": output_topic_id,
        "trigger_type": trigger_type, "trigger_topic_id": trigger_topic_id,
        "schedule_interval_days": interval_days, "schedule_cron": cron_expr, "status": status,
        "last_checked_at": last_checked_at, "next_check_at": next_check_at,
        "created_at": created_at,
    }


_SELECT = (
    "SELECT services.id, users.name, services.title, services.category, "
    "services.prompt, services.sources, services.script, "
    "services.objective_id, services.output_topic_id, "
    "services.trigger_type, services.trigger_topic_id, "
    "services.schedule_interval_days, services.schedule_cron, "
    "services.status, services.last_checked_at, services.next_check_at, "
    "services.created_at "
    "FROM services JOIN users ON users.id = services.user_id"
)


def add_service(args: dict, **kwargs) -> str:
    """Add a new standing Service for a person - the generic mechanism
    behind any "periodically do X and tell me only when something new/
    matching shows up" request: a news digest, a property search, a
    nearby-events check, a grocery-deals watch, a location-boundary check,
    or a step in a longer chain of automated checks. `prompt` should be the
    real, specific instructions (e.g. "3+ bedroom houses in Bondi under
    $800k with parking", not just "houses"). `sources` is optional -
    specific sites/domains to check; omit to let the agent use general web
    search. `script` is optional - a real shell command run via the
    terminal tool BEFORE reasoning (deterministic data-gathering, e.g. a
    curl+jq against a specific API, or an internal check like a location
    boundary), with its stdout then digested/reported on against `prompt` -
    omit it to let the agent gather data itself via web search/fetch,
    guided by `prompt` alone. A service can be script-only, prompt-only (an
    open-ended agent check), or both - in a sense it's either a script or
    an agent. `trigger_type` is 'cron' (the default - runs on
    schedule_interval_days/schedule_cron) or 'topic' (runs when a NEW post
    lands on `trigger_topic_id`, enabling chaining: Service A posts to
    topic X, Service B triggers off new posts on topic X). `objective_id`
    is optional - link this service to an existing objective it's
    researching toward (e.g. a property search feeding a "buy a house"
    objective) so get_objective shows it. `output_topic_id` is optional -
    every service ALWAYS delivers its findings via a notification topic
    (mandatory wiring, not a direct reply); if you don't pass one, a new
    topic "about" the owner is auto-created and the owner is automatically
    subscribed to it, so delivery to them keeps working with zero extra
    setup. Only pass an existing topic_id when you deliberately want this
    service to post somewhere else - e.g. a shared topic other people (not
    just the owner) already subscribe to, or to feed a second,
    topic-triggered service for chaining. `schedule_cron` is optional
    (cron-triggered services only) - a
    real 5-field cron expression (e.g. "0 9 * * 1" for every Monday at 9am)
    for a specific recurring day/time, overriding `schedule_interval_days`
    once set. Due immediately on creation (next_check_at = now) for a
    cron-triggered service, then settles into its real cadence after the
    first real check."""
    user_name = args["user_name"]
    title = args["title"]
    prompt = args["prompt"]
    category = args.get("category") or None
    sources = args.get("sources") or None
    script = args.get("script") or None
    objective_id = args.get("objective_id") or None
    output_topic_id = args.get("output_topic_id") or None
    trigger_type = args.get("trigger_type") or "cron"
    if trigger_type not in TRIGGER_TYPES:
        return json.dumps({"error": f"trigger_type must be one of {TRIGGER_TYPES}"})
    trigger_topic_id = args.get("trigger_topic_id") or None
    if trigger_type == "topic" and trigger_topic_id is None:
        return json.dumps({"error": "trigger_type='topic' requires trigger_topic_id"})
    cron_expr = args.get("schedule_cron") or None
    if cron_expr and not scheduling.validate_cron(cron_expr):
        return json.dumps({"error": f"'{cron_expr}' is not a valid 5-field cron expression "
                                     "(e.g. '0 9 * * 1' for every Monday at 9am)"})
    interval_days = int(args.get("schedule_interval_days") or 7)
    con = db.connect()
    try:
        user_id = _resolve_user(con, user_name)
        if user_id is None:
            return json.dumps({"error": f"no user named '{user_name}'"})
        if objective_id is not None:
            obj_row = con.execute("SELECT id FROM objectives WHERE id = ?", (objective_id,)).fetchone()
            if obj_row is None:
                return json.dumps({"error": f"no objective with id {objective_id}"})
        for topic_field, topic_val in (("output_topic_id", output_topic_id), ("trigger_topic_id", trigger_topic_id)):
            if topic_val is not None:
                topic_row = con.execute(
                    "SELECT id FROM notification_topics WHERE id = ?", (topic_val,)).fetchone()
                if topic_row is None:
                    return json.dumps({"error": f"no notification topic with id {topic_val} ({topic_field})"})
        # Mandatory wiring: every service always has a real output_topic_id -
        # auto-create one "about" the owner (and auto-subscribe them) when
        # the caller didn't explicitly point at an existing topic.
        auto_created_topic = output_topic_id is None
        if output_topic_id is None:
            output_topic_id = _ensure_output_topic(con, user_id, title, category)
        now = _now()
        # A topic-triggered service has no real cron due-time - next_check_at
        # is NOT NULL on the table, so it gets a harmless placeholder (now);
        # list_due_services never consults it for a topic-triggered row,
        # only for a cron-triggered one.
        next_check_at = now
        cur = con.execute(
            "INSERT INTO services "
            "(user_id, title, category, prompt, sources, script, objective_id, output_topic_id, "
            "trigger_type, trigger_topic_id, schedule_interval_days, schedule_cron, status, "
            "last_checked_at, next_check_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',NULL,?,?)",
            (user_id, title, category, prompt, sources, script, objective_id, output_topic_id,
             trigger_type, trigger_topic_id, interval_days, cron_expr, next_check_at, now),
        )
        con.commit()
        result = {"ok": True, "service_id": cur.lastrowid, "user_name": user_name,
                  "title": title, "trigger_type": trigger_type, "next_check_at": next_check_at,
                  "output_topic_id": output_topic_id}
        if auto_created_topic:
            result["output_topic_note"] = (
                f"auto-created a new topic for this service and subscribed {user_name} to it - "
                "findings will deliver through their own topic-notification pulse"
            )
        return json.dumps(result)
    finally:
        con.close()


def list_services(args: dict, **kwargs) -> str:
    """List Services. Filter by user_name, status, and/or objective_id (only
    services linked to that objective); omit all to see everyone's (mainly
    for admin/debugging)."""
    con = db.connect()
    try:
        query = _SELECT
        params = []
        conditions = []
        if args.get("user_name"):
            conditions.append("users.name = ?")
            params.append(args["user_name"])
        if args.get("status"):
            if args["status"] not in STATUSES:
                return json.dumps({"error": f"status must be one of {STATUSES}"})
            conditions.append("services.status = ?")
            params.append(args["status"])
        if args.get("objective_id") is not None:
            conditions.append("services.objective_id = ?")
            params.append(args["objective_id"])
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY services.created_at"
        rows = con.execute(query, params).fetchall()
        return json.dumps([_row_to_dict(r) for r in rows])
    finally:
        con.close()


def update_service(args: dict, **kwargs) -> str:
    """Update a Service's title/prompt/sources/script/objective_id/
    output_topic_id/trigger_type/trigger_topic_id/cadence (schedule_interval_days
    or schedule_cron)/status. Only provided fields change. Passing
    output_topic_id="" doesn't remove delivery entirely (a service always
    has one) - it re-auto-creates a fresh default topic instead, same as a
    brand-new service without one. Use status='paused' to temporarily stop
    checking without losing it, 'completed' when the thing being watched
    for was found (e.g. the house was bought), 'abandoned' if it's no
    longer wanted - there is no hard delete, matching
    every other soft-deletable record in this system."""
    service_id = args["service_id"]
    con = db.connect()
    try:
        row = con.execute(
            "SELECT user_id, title, category FROM services WHERE id = ?", (service_id,)).fetchone()
        if row is None:
            return json.dumps({"error": f"no service with id {service_id}"})
        owner_user_id, current_title, current_category = row
        fields = {}
        if args.get("title") is not None:
            fields["title"] = args["title"]
        if args.get("prompt") is not None:
            fields["prompt"] = args["prompt"]
        if "sources" in args:
            fields["sources"] = args["sources"] or None
        if "script" in args:
            fields["script"] = args["script"] or None
        if "objective_id" in args:
            new_obj_id = args["objective_id"] or None
            if new_obj_id is not None:
                obj_row = con.execute("SELECT id FROM objectives WHERE id = ?", (new_obj_id,)).fetchone()
                if obj_row is None:
                    return json.dumps({"error": f"no objective with id {new_obj_id}"})
            fields["objective_id"] = new_obj_id
        if "output_topic_id" in args:
            new_topic_id = args["output_topic_id"] or None
            if new_topic_id is not None:
                topic_row = con.execute(
                    "SELECT id FROM notification_topics WHERE id = ?", (new_topic_id,)).fetchone()
                if topic_row is None:
                    return json.dumps({"error": f"no notification topic with id {new_topic_id}"})
            else:
                # Mandatory wiring: a service always has an output_topic_id -
                # clearing it just re-auto-creates a fresh default one
                # (same as add_service's own default) rather than leaving
                # delivery with nowhere to go.
                new_topic_id = _ensure_output_topic(con, owner_user_id, current_title, current_category)
            fields["output_topic_id"] = new_topic_id
        if args.get("trigger_type") is not None:
            if args["trigger_type"] not in TRIGGER_TYPES:
                return json.dumps({"error": f"trigger_type must be one of {TRIGGER_TYPES}"})
            fields["trigger_type"] = args["trigger_type"]
        if "trigger_topic_id" in args:
            new_trigger_topic_id = args["trigger_topic_id"] or None
            if new_trigger_topic_id is not None:
                topic_row = con.execute(
                    "SELECT id FROM notification_topics WHERE id = ?", (new_trigger_topic_id,)).fetchone()
                if topic_row is None:
                    return json.dumps({"error": f"no notification topic with id {new_trigger_topic_id}"})
            fields["trigger_topic_id"] = new_trigger_topic_id
        if args.get("schedule_interval_days") is not None:
            fields["schedule_interval_days"] = int(args["schedule_interval_days"])
        if "schedule_cron" in args:
            new_cron = args["schedule_cron"] or None
            if new_cron is not None and not scheduling.validate_cron(new_cron):
                return json.dumps({"error": f"'{new_cron}' is not a valid 5-field cron expression "
                                             "(e.g. '0 9 * * 1' for every Monday at 9am)"})
            fields["schedule_cron"] = new_cron
        if args.get("status") is not None:
            if args["status"] not in STATUSES:
                return json.dumps({"error": f"status must be one of {STATUSES}"})
            fields["status"] = args["status"]
        if not fields:
            return json.dumps({"error": "no fields to update"})
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        con.execute(f"UPDATE services SET {set_clause} WHERE id = ?",
                    (*fields.values(), service_id))
        con.commit()
        return json.dumps({"ok": True, "service_id": service_id, "updated": list(fields.keys())})
    finally:
        con.close()


def list_due_services(args: dict, **kwargs) -> str:
    """Services due to run right now, for either reason: a cron-triggered
    service whose next_check_at has passed, OR a topic-triggered service
    with at least one new, not-yet-consumed post on its trigger_topic_id.
    With user_name: just that person's due services (the monitor/cron path -
    normally 0 or a handful). Without it: everyone's, for inspection. Each
    topic-triggered result includes `pending_topic_post_ids` - the specific
    new post ids to actually process (call consume_topic_post for each one
    you've handled)."""
    con = db.connect()
    try:
        now = _now()
        params = [now]
        query = (
            _SELECT + " WHERE services.status = 'active' AND services.trigger_type = 'cron' "
            "AND services.next_check_at <= ?"
        )
        if args.get("user_name"):
            user_id = _resolve_user(con, args["user_name"])
            if user_id is None:
                return json.dumps({"error": f"no user named '{args['user_name']}'"})
            query += " AND services.user_id = ?"
            params.append(user_id)
        query += " ORDER BY services.next_check_at"
        rows = con.execute(query, params).fetchall()
        due = [_row_to_dict(r) for r in rows]
        for d in due:
            d["due_reason"] = "cron"

        topic_query = _SELECT + " WHERE services.status = 'active' AND services.trigger_type = 'topic'"
        topic_params = []
        if args.get("user_name"):
            topic_query += " AND services.user_id = ?"
            topic_params.append(user_id)
        topic_rows = con.execute(topic_query, topic_params).fetchall()
        for r in topic_rows:
            svc = _row_to_dict(r)
            pending = con.execute(
                "SELECT id FROM topic_posts WHERE topic_id = ? AND id NOT IN ("
                "  SELECT topic_post_id FROM service_topic_consumption WHERE service_id = ?"
                ") ORDER BY created_at",
                (svc["trigger_topic_id"], svc["service_id"]),
            ).fetchall()
            if pending:
                svc["due_reason"] = "topic"
                svc["pending_topic_post_ids"] = [p[0] for p in pending]
                due.append(svc)
        return json.dumps(due)
    finally:
        con.close()


def list_service_results(args: dict, **kwargs) -> str:
    """Results already reported for a Service, most recent first - check
    this BEFORE reporting something new, so the same result isn't re-sent
    every cycle."""
    service_id = args["service_id"]
    limit = int(args.get("limit") or 20)
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT id, summary, dedupe_key, found_at FROM service_results "
            "WHERE service_id = ? ORDER BY found_at DESC LIMIT ?",
            (service_id, limit),
        ).fetchall()
        return json.dumps([
            {"result_id": rid, "summary": summary, "dedupe_key": dedupe_key, "found_at": found_at}
            for rid, summary, dedupe_key, found_at in rows
        ])
    finally:
        con.close()


def log_service_result(args: dict, **kwargs) -> str:
    """Record a genuinely new finding for a Service - call this for each new
    result BEFORE telling the person about it, so future checks know not to
    repeat it. If dedupe_key was already logged for this service, this is a
    safe no-op (not an error) rather than a duplicate row."""
    service_id = args["service_id"]
    summary = args["summary"]
    dedupe_key = args.get("dedupe_key") or None
    con = db.connect()
    try:
        row = con.execute("SELECT id FROM services WHERE id = ?", (service_id,)).fetchone()
        if row is None:
            return json.dumps({"error": f"no service with id {service_id}"})
        cur = con.execute(
            "INSERT OR IGNORE INTO service_results (service_id, summary, dedupe_key, found_at) "
            "VALUES (?,?,?,?)",
            (service_id, summary, dedupe_key, _now()),
        )
        con.commit()
        return json.dumps({"ok": True, "service_id": service_id,
                            "already_known": cur.rowcount == 0 and dedupe_key is not None})
    finally:
        con.close()


def acknowledge_service_check(args: dict, **kwargs) -> str:
    """Reschedule a CRON-triggered service forward - by its own
    schedule_cron (a specific day/time) if set, else its
    schedule_interval_days - MUST be called at the end of every cron-due
    check run regardless of whether anything new was found. Without this
    call the service never becomes due again. Not used for a
    topic-triggered service - call consume_topic_post for each pending post
    instead."""
    service_id = args["service_id"]
    con = db.connect()
    try:
        row = con.execute(
            "SELECT schedule_interval_days, schedule_cron FROM services WHERE id = ?", (service_id,),
        ).fetchone()
        if row is None:
            return json.dumps({"error": f"no service with id {service_id}"})
        interval_days, cron_expr = row
        now_dt = datetime.now(timezone.utc)
        if cron_expr:
            next_at = scheduling.next_from_cron(cron_expr, now_dt)
        else:
            next_at = (now_dt + timedelta(days=interval_days)).isoformat()
        con.execute(
            "UPDATE services SET last_checked_at = ?, next_check_at = ? WHERE id = ?",
            (now_dt.isoformat(), next_at, service_id),
        )
        con.commit()
        return json.dumps({"ok": True, "service_id": service_id, "next_check_at": next_at})
    finally:
        con.close()


def consume_topic_post(args: dict, **kwargs) -> str:
    """Mark a topic post as processed by a TOPIC-triggered service - MUST be
    called for every id in list_due_services' own pending_topic_post_ids,
    regardless of whether it produced a new match. Without this, the same
    post would make this service due again every pulse tick forever. Safe
    to call again for an already-consumed post (no-op, not an error)."""
    service_id = args["service_id"]
    topic_post_id = args["topic_post_id"]
    con = db.connect()
    try:
        row = con.execute("SELECT id FROM services WHERE id = ?", (service_id,)).fetchone()
        if row is None:
            return json.dumps({"error": f"no service with id {service_id}"})
        post_row = con.execute("SELECT id FROM topic_posts WHERE id = ?", (topic_post_id,)).fetchone()
        if post_row is None:
            return json.dumps({"error": f"no topic post with id {topic_post_id}"})
        con.execute(
            "INSERT OR IGNORE INTO service_topic_consumption (service_id, topic_post_id, consumed_at) "
            "VALUES (?,?,?)",
            (service_id, topic_post_id, _now()),
        )
        con.commit()
        return json.dumps({"ok": True, "service_id": service_id, "topic_post_id": topic_post_id})
    finally:
        con.close()
