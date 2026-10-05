"""
Generic proactive-initiative scheduling: one topic_checkins row per
(user, domain), independent of whether any objective exists for that
domain yet. Deliberately separate from objectives.next_follow_up_at,
which is per-OBJECTIVE ("is this specific goal overdue"); this is
per-TOPIC ("should Mentor initiate a conversation about this whole area
of life at all"), so a domain nobody has ever mentioned still eventually
gets raised, not just domains that already have something tracked.

seed_topic_checkins_for_user staggers each domain's first next_checkin_at
rather than firing all 7 at once - topics get introduced one at a time
(roughly every couple of days) rather than one overwhelming
"let's discuss your entire life" conversation on day one. Once a topic
has had its first real check-in, it settles into a regular
checkin_interval_days cadence (7 days / weekly by default).

Delivery (which channel a person's proactive check-in actually reaches
them on) is NOT decided here - see docker-compose/hermes cron setup and
users.preferred_checkin_channel. Hermes's own `--deliver` target is fixed
at cron-creation time, not re-evaluated per run, so changing a person's
channel preference after their cron already exists requires recreating
that one cron job, not just updating the DB row.
"""
import json
from datetime import datetime, timedelta, timezone

from . import db
from . import scheduling

DOMAINS = ["hobby", "health", "career", "finance", "relationship", "media", "other"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_user(con, user_name: str):
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    return row[0] if row else None


def seed_topic_checkins_for_user(user_id: int) -> None:
    """Idempotent (INSERT OR IGNORE keyed on the real unique index) - safe
    to call on an already-seeded user, only fills in domains that don't
    have a row yet (e.g. a genuinely new domain added to the enum later).
    Called from create_user; also safe to call once as a backfill for
    users that existed before this feature."""
    con = db.connect()
    try:
        now_dt = datetime.now(timezone.utc)
        for i, domain in enumerate(DOMAINS):
            # Staggered: domain 0 due immediately, domain 1 in a day, etc. -
            # first pass through all 7 takes about a week, then each
            # settles into its own regular interval from there.
            next_at = (now_dt + timedelta(days=i)).isoformat()
            con.execute(
                "INSERT OR IGNORE INTO topic_checkins "
                "(user_id, domain, last_checkin_at, next_checkin_at, checkin_interval_days, created_at) "
                "VALUES (?,?,NULL,?,7,?)",
                (user_id, domain, next_at, _now()),
            )
        con.commit()
    finally:
        con.close()


def list_due_topic_checkins(args: dict, **kwargs) -> str:
    """Topics due for a proactive check-in right now. With user_name: just
    that person's due topics (normally 0 or 1 - the monitor/cron path).
    Without it: every due (user, domain) pair across everyone, for
    inspection/debugging."""
    con = db.connect()
    now = _now()
    if args.get("user_name"):
        user_id = _resolve_user(con, args["user_name"])
        if user_id is None:
            con.close()
            return json.dumps({"error": f"no user named '{args['user_name']}'"})
        rows = con.execute(
            "SELECT domain, next_checkin_at FROM topic_checkins "
            "WHERE user_id = ? AND next_checkin_at <= ? ORDER BY next_checkin_at",
            (user_id, now),
        ).fetchall()
        con.close()
        return json.dumps([{"domain": d, "due_since": t} for d, t in rows])
    rows = con.execute(
        "SELECT users.name, topic_checkins.domain, topic_checkins.next_checkin_at "
        "FROM topic_checkins JOIN users ON users.id = topic_checkins.user_id "
        "WHERE topic_checkins.next_checkin_at <= ? ORDER BY topic_checkins.next_checkin_at",
        (now,),
    ).fetchall()
    con.close()
    return json.dumps([{"user_name": n, "domain": d, "due_since": t} for n, d, t in rows])


def acknowledge_topic_checkin(args: dict, **kwargs) -> str:
    """Reschedule this topic forward - by its own schedule_cron (a specific
    day/time, e.g. every Monday 9am) if set, else checkin_interval_days
    (default 7/weekly) - MUST be called at the end of a topic-checkin
    conversation regardless of how it went (person engaged deeply, gave a
    one-word reply, or didn't respond at all). Without this call, a topic
    that fired once would never become due again, the same non-negotiable
    pattern as objectives' own acknowledge_followup."""
    user_name = args["user_name"]
    domain = args["domain"]
    if domain not in DOMAINS:
        return json.dumps({"error": f"unknown domain '{domain}' - must be one of {DOMAINS}"})
    con = db.connect()
    user_id = _resolve_user(con, user_name)
    if user_id is None:
        con.close()
        return json.dumps({"error": f"no user named '{user_name}'"})
    row = con.execute(
        "SELECT checkin_interval_days, schedule_cron FROM topic_checkins WHERE user_id = ? AND domain = ?",
        (user_id, domain),
    ).fetchone()
    if row is None:
        con.close()
        return json.dumps({"error": f"no topic_checkins row for '{user_name}'/'{domain}' - "
                                     f"call seed_topic_checkins_for_user first"})
    interval_days, cron_expr = row
    now_dt = datetime.now(timezone.utc)
    if cron_expr:
        next_at = scheduling.next_from_cron(cron_expr, now_dt)
    else:
        next_at = (now_dt + timedelta(days=interval_days)).isoformat()
    con.execute(
        "UPDATE topic_checkins SET last_checkin_at = ?, next_checkin_at = ? WHERE user_id = ? AND domain = ?",
        (now_dt.isoformat(), next_at, user_id, domain),
    )
    con.commit()
    con.close()
    return json.dumps({"ok": True, "user_name": user_name, "domain": domain, "next_checkin_at": next_at})


def set_topic_checkin_schedule(args: dict, **kwargs) -> str:
    """Set a specific recurring day/time for a person's topic check-in
    (e.g. schedule_cron="0 9 * * 1" for every Monday at 9am), or a plain
    day-interval (schedule_interval_days) - overrides the default weekly
    cadence for just this one (user, domain) pair. Pass schedule_cron="" to
    clear it and fall back to the plain interval."""
    user_name = args["user_name"]
    domain = args["domain"]
    if domain not in DOMAINS:
        return json.dumps({"error": f"unknown domain '{domain}' - must be one of {DOMAINS}"})
    con = db.connect()
    try:
        user_id = _resolve_user(con, user_name)
        if user_id is None:
            return json.dumps({"error": f"no user named '{user_name}'"})
        row = con.execute(
            "SELECT id FROM topic_checkins WHERE user_id = ? AND domain = ?", (user_id, domain),
        ).fetchone()
        if row is None:
            return json.dumps({"error": f"no topic_checkins row for '{user_name}'/'{domain}' - "
                                         f"call seed_topic_checkins_for_user first"})
        fields = {}
        if "schedule_cron" in args:
            cron_expr = args["schedule_cron"] or None
            if cron_expr is not None and not scheduling.validate_cron(cron_expr):
                return json.dumps({"error": f"'{cron_expr}' is not a valid 5-field cron expression "
                                             "(e.g. '0 9 * * 1' for every Monday at 9am)"})
            fields["schedule_cron"] = cron_expr
        if args.get("schedule_interval_days") is not None:
            fields["checkin_interval_days"] = int(args["schedule_interval_days"])
        if not fields:
            return json.dumps({"error": "no fields to update"})
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        con.execute(f"UPDATE topic_checkins SET {set_clause} WHERE user_id = ? AND domain = ?",
                    (*fields.values(), user_id, domain))
        con.commit()
        return json.dumps({"ok": True, "user_name": user_name, "domain": domain, "updated": list(fields.keys())})
    finally:
        con.close()
