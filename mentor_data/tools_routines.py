import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

from . import db
from . import tools_memory
from . import tools_objectives
from . import tools_topics
from . import wellbeing_templates


def _valid_timezone(tz: str) -> bool:
    if tz not in available_timezones():
        return False
    try:
        ZoneInfo(tz)
        return True
    except Exception:
        return False

# Streak thresholds that count as an earned badge/sticker - computed on the fly
# from routine_completions rather than a separate stateful table, so it's
# always consistent with actual history and never needs backfilling/syncing.
BADGE_THRESHOLDS = [3, 7, 14, 30, 100]

# One consistent emoji per threshold - a real, recognizable "sticker" that
# shows up the exact same way every time someone hits that milestone,
# rather than leaving the agent to invent a random one from imagination
# each time (a genuine collectible feels different from an arbitrary
# celebratory emoji picked fresh per message).
BADGE_EMOJI = {3: "🌟", 7: "✨", 14: "🏆", 30: "👑", 100: "💎"}

HERMES_DASHBOARD_URL = os.environ.get("HERMES_DASHBOARD_URL", "http://hermes:9119")
HERMES_DASHBOARD_USERNAME = os.environ.get("HERMES_DASHBOARD_USERNAME", "")
HERMES_DASHBOARD_PASSWORD = os.environ.get("HERMES_DASHBOARD_PASSWORD", "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# The settings every profile needs for the current memory model (holographic
# fact_store, auto-extraction on, a context-file limit large enough for
# SOUL.md not to silently truncate) - asserted directly onto each NEW
# profile's own config.yaml right after cloning, rather than trusted to
# already be correct on "default" at clone time. "default" doubles as both
# a real (unclaimed) profile and the live clone source for every new one -
# there's no separate, protected template file - so its own config can
# drift for any number of unrelated reasons (a future edit, an experiment
# left in place, a migration pass that touches it without re-verifying
# clone integrity). A user created while it was drifted would otherwise
# silently inherit the wrong state - exactly what happened to a real
# profile (Jennifer, created ~4 hours before the holographic migration
# landed on "default") before this existed. Asserting these three keys
# post-clone makes provisioning correct regardless of "default"'s current
# state, not just correct today.
def _ensure_memory_provider_config(client, profile: str) -> None:
    """Best-effort - never raises. Failing to patch these settings should
    not fail user creation (the profile still exists and mostly works,
    just without the current memory model until someone notices and fixes
    it by hand, same degraded-but-not-broken outcome as before this
    existed)."""
    import yaml
    path = f"/opt/data/profiles/{profile}/config.yaml"
    read_resp = client.get("/api/fs/read-text", params={"path": path})
    if read_resp.status_code != 200:
        return
    try:
        cfg = yaml.safe_load(read_resp.json().get("text", "")) or {}
    except yaml.YAMLError:
        return
    cfg.setdefault("memory", {})["provider"] = "holographic"
    cfg.setdefault("plugins", {}).setdefault("hermes-memory-store", {})["auto_extract"] = "true"
    cfg["context_file_max_chars"] = 100000
    client.post("/api/fs/write-text", json={"path": path, "content": yaml.safe_dump(cfg, sort_keys=False)})


def _provision_hermes_profile(user_name: str) -> "str | None":
    """Best-effort: create a real, isolated Hermes profile for a newly
    created Mentor user via Hermes's own POST /api/profiles (the plain REST
    twin of the profiles.create JSON-RPC method - no websocket/ticket
    handshake needed for a one-shot admin call like this), cloning config/
    SOUL.md/credentials/memory from the "default" profile, then asserting
    the current memory-provider settings directly (see
    _ensure_memory_provider_config - deliberately not just trusted to have
    come along correctly via the clone). "default"'s own memory is
    deliberately kept generic/family-wide only (see the "Evgeniy moved to
    his own profile" plan entry) so cloning it for a brand-new person is
    safe as-is - no separate blank-memory step needed the way it was for
    the first three profiles created before that cleanup. Dashboard chat
    picks up a new profile immediately (resolved per-connection, no
    gateway restart needed) - only Telegram routing (gateway.profile_routes)
    needs one, and only once that user is actually paired. Returns None on
    success, or an error string (never raises - profile provisioning must
    not fail create_user itself, which already succeeded)."""
    if not HERMES_DASHBOARD_PASSWORD:
        return "HERMES_DASHBOARD_PASSWORD not configured - could not create a Hermes profile"
    profile = user_name.strip().lower()
    try:
        import httpx
        with httpx.Client(base_url=HERMES_DASHBOARD_URL, timeout=20) as client:
            login = client.post("/auth/password-login", json={
                "provider": "basic", "username": HERMES_DASHBOARD_USERNAME, "password": HERMES_DASHBOARD_PASSWORD,
            })
            if login.status_code != 200:
                return f"could not authenticate to Hermes ({login.status_code})"
            resp = client.post("/api/profiles", json={"name": profile, "clone_from": "default"})
            if resp.status_code >= 400:
                return f"Hermes profile creation failed ({resp.status_code}): {resp.text[:200]}"
            _ensure_memory_provider_config(client, profile)
        return None
    except Exception as e:
        return f"failed to create Hermes profile: {e}"


def create_user(args: dict, **kwargs) -> str:
    home_tz = args.get("home_timezone")
    if home_tz is not None and not _valid_timezone(home_tz):
        return json.dumps({"error": f"'{home_tz}' is not a real IANA timezone name (e.g. 'Australia/Sydney')"})
    con = db.connect()
    existing = con.execute("SELECT id FROM users WHERE name = ?", (args["name"],)).fetchone()
    if existing is not None:
        con.close()
        return json.dumps({"error": f"a user named '{args['name']}' already exists (id {existing[0]})"})
    cur = con.execute(
        "INSERT INTO users (name, type, created_at, notes, home_timezone, date_of_birth, gender) VALUES (?,?,?,?,?,?,?)",
        (args["name"], args.get("type", "adult"), _now(), args.get("notes"), home_tz,
         args.get("date_of_birth"), args.get("gender")),
    )
    con.commit()
    user_id = cur.lastrowid
    con.close()
    profile_error = _provision_hermes_profile(args["name"])
    tools_memory.sync_profile_to_own_memory(user_id)
    tools_topics.seed_topic_checkins_for_user(user_id)
    result = {"id": user_id, "name": args["name"], "type": args.get("type", "adult"),
              "notes": args.get("notes"), "home_timezone": home_tz,
              "date_of_birth": args.get("date_of_birth"), "gender": args.get("gender")}
    if profile_error:
        result["hermes_profile_error"] = profile_error
    return json.dumps(result)


def update_user(args: dict, **kwargs) -> str:
    """Rename a user, change their type, grant/revoke family-admin status
    (e.g. once you know the real name instead of a placeholder like 'Child'),
    set/update notes - free text for durable context about this person
    (special needs, disability, accessibility considerations, allergies,
    anything that should shape how routines/objectives/tone get calibrated
    for them) - or mark has_own_pairing once they actually get their own
    paired conversation (rather than being managed by proxy through someone
    else's). home_timezone is the person's normal/base IANA timezone (e.g.
    'Australia/Sydney') - set this explicitly; it does NOT auto-update.
    recent_timezone is separate and NOT settable here - it's derived
    automatically from their most recent real location report (see
    owntracks-mcp's get_last_location), since a manually-set "recent"
    value would immediately go stale and lie. date_of_birth (ISO date,
    e.g. '2014-03-21') and gender (free text - not a fixed enum, too
    varied to enumerate) are basic profile facts; pass an empty string to
    clear either one. preferred_checkin_channel ('telegram'/'dashboard',
    or empty to clear/auto-detect) is which channel proactive topic
    check-ins should reach this person on - empty/unset means auto-detect
    (real Telegram pairing preferred, since that reaches a phone;
    dashboard chat otherwise). Changing this does NOT retroactively move
    an already-running topic-checkin cron - that has to be recreated to
    pick up a new preference (see tools_topics.py)."""
    user_id = args["user_id"]
    fields = {}
    if args.get("name") is not None:
        fields["name"] = args["name"]
    if args.get("type") is not None:
        fields["type"] = args["type"]
    if args.get("is_family_admin") is not None:
        fields["is_family_admin"] = 1 if args["is_family_admin"] else 0
    if args.get("notes") is not None:
        fields["notes"] = args["notes"]
    if args.get("has_own_pairing") is not None:
        fields["has_own_pairing"] = 1 if args["has_own_pairing"] else 0
    if args.get("home_timezone") is not None:
        if not _valid_timezone(args["home_timezone"]):
            return json.dumps({"error": f"'{args['home_timezone']}' is not a real IANA timezone name (e.g. 'Australia/Sydney')"})
        fields["home_timezone"] = args["home_timezone"]
    if args.get("date_of_birth") is not None:
        fields["date_of_birth"] = args["date_of_birth"] or None
    if args.get("gender") is not None:
        fields["gender"] = args["gender"] or None
    if args.get("preferred_checkin_channel") is not None:
        channel = args["preferred_checkin_channel"] or None
        if channel is not None and channel not in ("telegram", "dashboard"):
            return json.dumps({"error": f"preferred_checkin_channel must be 'telegram', 'dashboard', or empty "
                                         f"(to auto-detect) - got '{channel}'"})
        fields["preferred_checkin_channel"] = channel
    if not fields:
        return json.dumps({"error": "no updatable fields provided"})

    con = db.connect()
    exists = con.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no user with id {user_id}"})
    if "name" in fields:
        clash = con.execute(
            "SELECT id FROM users WHERE name = ? AND id != ?", (fields["name"], user_id)
        ).fetchone()
        if clash is not None:
            con.close()
            return json.dumps({"error": f"a user named '{fields['name']}' already exists"})

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE users SET {set_clause} WHERE id = ?", (*fields.values(), user_id))
    con.commit()
    con.close()
    _PROFILE_SUMMARY_FIELDS = {"name", "type", "notes", "home_timezone", "date_of_birth", "gender"}
    if _PROFILE_SUMMARY_FIELDS & fields.keys():
        tools_memory.sync_profile_to_own_memory(user_id)
    return json.dumps({"ok": True, "user_id": user_id, "updated": fields})


def list_users(args: dict, **kwargs) -> str:
    con = db.connect()
    rows = con.execute(
        "SELECT id, name, type, is_family_admin, notes, available_hours_per_week, "
        "available_money_per_month, budget_currency, has_own_pairing, home_timezone, "
        "recent_timezone, recent_timezone_at, share_location_with_family, date_of_birth, gender, "
        "persona_key, preferred_checkin_channel, share_network_activity_with_family "
        "FROM users ORDER BY name"
    ).fetchall()
    con.close()
    cols = ["id", "name", "type", "is_family_admin", "notes", "available_hours_per_week",
            "available_money_per_month", "budget_currency", "has_own_pairing", "home_timezone",
            "recent_timezone", "recent_timezone_at", "share_location_with_family", "date_of_birth", "gender",
            "persona_key", "preferred_checkin_channel", "share_network_activity_with_family"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def set_recent_timezone(user_name: str, timezone_name: str) -> None:
    """Best-effort auto-update of a user's recent_timezone from a real
    location report's own tzname (see owntracks-mcp's get_last_location) -
    NOT an MCP tool itself (not something the agent or a human should set by
    hand - see update_user's docstring for why). Silently no-ops on an
    invalid/unknown user or timezone name rather than raising, since this
    always runs as a side effect of a location query that must not fail
    because of it."""
    if not _valid_timezone(timezone_name):
        return
    con = db.connect()
    con.execute(
        "UPDATE users SET recent_timezone = ?, recent_timezone_at = ? WHERE name = ?",
        (timezone_name, _now(), user_name),
    )
    con.commit()
    con.close()


def _get_user_id(con, user_name: str):
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    return row[0] if row else None


def is_family_admin(user_name: str) -> bool:
    """Used by tools_pairing.py to gate pairing approval to family-admin users."""
    con = db.connect()
    row = con.execute("SELECT is_family_admin FROM users WHERE name = ?", (user_name,)).fetchone()
    con.close()
    return bool(row and row[0])


def shares_location_with_family(user_name: str) -> bool:
    """Used by owntracks-mcp to gate location queries: true once this user
    has explicitly opted in (default off) to let any family member (not
    just admins) see their location."""
    con = db.connect()
    row = con.execute("SELECT share_location_with_family FROM users WHERE name = ?", (user_name,)).fetchone()
    con.close()
    return bool(row and row[0])


def set_location_sharing(args: dict, **kwargs) -> str:
    """Opt a user in/out of sharing their location with the rest of the
    family (beyond family admins, who can always see everyone regardless).
    Off by default. Gated: only the person themselves, or a family admin
    (e.g. setting it for a managed child profile), may change it -
    requesting_user_name must be whoever you're actually talking to in THIS
    conversation, never a name they merely claim on someone else's behalf."""
    requesting_user_name = args["requesting_user_name"]
    target_user_name = args["target_user_name"]
    share = bool(args["share"])
    if requesting_user_name != target_user_name and not is_family_admin(requesting_user_name):
        return json.dumps({
            "error": f"'{requesting_user_name}' may not change '{target_user_name}'s location-sharing "
                     f"setting - only {target_user_name} themselves, or a family admin, can."
        })
    con = db.connect()
    if con.execute("SELECT id FROM users WHERE name = ?", (target_user_name,)).fetchone() is None:
        con.close()
        return json.dumps({"error": f"no user named '{target_user_name}'"})
    con.execute("UPDATE users SET share_location_with_family = ? WHERE name = ?", (1 if share else 0, target_user_name))
    con.commit()
    con.close()
    return json.dumps({"user_name": target_user_name, "share_location_with_family": share})


def shares_network_activity_with_family(user_name: str) -> bool:
    """Used by tplink-mcp to gate network-activity queries - same shape as
    shares_location_with_family, but a separate flag/consent entirely."""
    con = db.connect()
    row = con.execute("SELECT share_network_activity_with_family FROM users WHERE name = ?", (user_name,)).fetchone()
    con.close()
    return bool(row and row[0])


def set_network_activity_sharing(args: dict, **kwargs) -> str:
    """Opt a user in/out of sharing their home-network activity (online/
    offline, live up/down speed) with the rest of the family (beyond
    family admins, who can always see everyone regardless). Off by
    default. Gated: only the person themselves, or a family admin, may
    change it - requesting_user_name must be whoever you're actually
    talking to in THIS conversation, never a name they merely claim on
    someone else's behalf."""
    requesting_user_name = args["requesting_user_name"]
    target_user_name = args["target_user_name"]
    share = bool(args["share"])
    if requesting_user_name != target_user_name and not is_family_admin(requesting_user_name):
        return json.dumps({
            "error": f"'{requesting_user_name}' may not change '{target_user_name}'s network-activity-sharing "
                     f"setting - only {target_user_name} themselves, or a family admin, can."
        })
    con = db.connect()
    if con.execute("SELECT id FROM users WHERE name = ?", (target_user_name,)).fetchone() is None:
        con.close()
        return json.dumps({"error": f"no user named '{target_user_name}'"})
    con.execute("UPDATE users SET share_network_activity_with_family = ? WHERE name = ?",
                (1 if share else 0, target_user_name))
    con.commit()
    con.close()
    return json.dumps({"user_name": target_user_name, "share_network_activity_with_family": share})


def create_routine(args: dict, **kwargs) -> str:
    con = db.connect()
    user_id = _get_user_id(con, args["user_name"])
    if user_id is None:
        con.close()
        return json.dumps({
            "error": f"no user named '{args['user_name']}' - call create_user first"
        })
    cur = con.execute(
        "INSERT INTO routines (user_id, name, schedule, created_at) VALUES (?,?,?,?)",
        (user_id, args["name"], args.get("schedule", "daily"), _now()),
    )
    con.commit()
    routine_id = cur.lastrowid
    con.close()
    return json.dumps({"id": routine_id, "user_name": args["user_name"], "name": args["name"]})


def list_routines(args: dict, **kwargs) -> str:
    con = db.connect()
    q = ("SELECT routines.id, routines.name, routines.schedule, routines.active, users.name "
         "FROM routines JOIN users ON users.id = routines.user_id WHERE 1=1")
    params = []
    if args.get("user_name"):
        q += " AND users.name = ?"
        params.append(args["user_name"])
    if args.get("active_only", True):
        q += " AND routines.active = 1"
    q += " ORDER BY users.name, routines.name"
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "name", "schedule", "active", "user_name"]
    return json.dumps([dict(zip(cols, r)) for r in rows])


def complete_routine(args: dict, **kwargs) -> str:
    now = _now()
    con = db.connect()
    exists = con.execute("SELECT id FROM routines WHERE id = ?", (args["routine_id"],)).fetchone()
    if exists is None:
        con.close()
        return json.dumps({"error": f"no routine with id {args['routine_id']}"})
    before = json.loads(_compute_streak(args["routine_id"]))
    con.execute(
        "INSERT INTO routine_completions (routine_id, completed_at, method, note) VALUES (?,?,?,?)",
        (args["routine_id"], now, args.get("method", "told_me"), args.get("note")),
    )
    con.commit()
    con.close()
    streak_info = json.loads(_compute_streak(args["routine_id"]))
    # A "new_badge" is a threshold THIS completion just crossed, not one
    # already earned on a prior day - the real "you just leveled up"
    # moment worth celebrating specially, vs. badges_earned (every
    # threshold reached so far, recomputed fresh every call).
    new_badges = [t for t in streak_info["badges_earned"] if t not in before["badges_earned"]]
    new_badge = max(new_badges) if new_badges else None
    return json.dumps({
        "ok": True, "routine_id": args["routine_id"], "completed_at": now, **streak_info,
        "new_badge": new_badge, "new_badge_emoji": BADGE_EMOJI.get(new_badge) if new_badge else None,
    })


def _compute_streak(routine_id: int) -> str:
    con = db.connect()
    rows = con.execute(
        "SELECT completed_at FROM routine_completions WHERE routine_id = ? ORDER BY completed_at",
        (routine_id,),
    ).fetchall()
    con.close()

    # Distinct calendar dates (UTC) a completion was logged - multiple completions
    # in one day count once for streak purposes.
    days = sorted({datetime.fromisoformat(r[0]).date() for r in rows})

    longest = current = 0
    prev: date | None = None
    for d in days:
        if prev is not None and (d - prev).days == 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        prev = d

    today = datetime.now(timezone.utc).date()
    # Streak is only "current" if the most recent completion was today or yesterday -
    # otherwise it's broken, even though `current` above still holds the last run length.
    if not days or (today - days[-1]).days > 1:
        current = 0

    badges = [t for t in BADGE_THRESHOLDS if longest >= t]
    latest_badge = max(badges) if badges else None
    return json.dumps({
        "current_streak_days": current,
        "longest_streak_days": longest,
        "total_completions": len(rows),
        "badges_earned": badges,
        "latest_badge_emoji": BADGE_EMOJI.get(latest_badge) if latest_badge else None,
    })


def get_routine(args: dict, **kwargs) -> str:
    con = db.connect()
    row = con.execute(
        "SELECT routines.id, routines.name, routines.schedule, routines.active, users.name "
        "FROM routines JOIN users ON users.id = routines.user_id WHERE routines.id = ?",
        (args["routine_id"],),
    ).fetchone()
    con.close()
    if row is None:
        return json.dumps({"error": f"no routine with id {args['routine_id']}"})
    cols = ["id", "name", "schedule", "active", "user_name"]
    result = dict(zip(cols, row))
    result.update(json.loads(_compute_streak(args["routine_id"])))
    return json.dumps(result)


def apply_wellbeing_template(args: dict, **kwargs) -> str:
    """Seed a user with the wellbeing bundle(s) implied by their assigned
    profile categories (profile_template_routines/profile_template_objectives),
    instead of re-enumerating them by hand. If the user isn't assigned to any
    profile category yet, auto-assigns the one matching their type first
    (e.g. type='adult' -> the 'Adult' category) - so this still "just works"
    right after create_user, same as before the category system existed.
    Idempotent - skips anything that already exists by name, safe to call
    again after partial setup or after a new category gets assigned via
    assign_user_profile."""
    user_name = args["user_name"]
    con = db.connect()
    user_row = con.execute("SELECT id, type FROM users WHERE name = ?", (user_name,)).fetchone()
    if user_row is None:
        con.close()
        return json.dumps({"error": f"no user named '{user_name}' - call create_user first"})
    user_id, user_type = user_row

    assigned_profile_ids = [r[0] for r in con.execute(
        "SELECT profile_id FROM user_profiles WHERE user_id = ?", (user_id,)
    ).fetchall()]
    if not assigned_profile_ids:
        display_name = wellbeing_templates.CATEGORY_DISPLAY_NAMES.get(user_type)
        category_row = (
            con.execute("SELECT id FROM profiles WHERE name = ?", (display_name,)).fetchone()
            if display_name else None
        )
        if category_row is not None:
            con.execute(
                "INSERT OR IGNORE INTO user_profiles (user_id, profile_id, assigned_at) VALUES (?,?,?)",
                (user_id, category_row[0], _now()),
            )
            con.commit()
            assigned_profile_ids = [category_row[0]]

    existing_routines = {r[0] for r in con.execute("SELECT name FROM routines WHERE user_id = ?", (user_id,))}
    # Scoped to this user_id - a same-named objective for a DIFFERENT user is not a duplicate.
    existing_titles = {r[0] for r in con.execute("SELECT title FROM objectives WHERE user_id = ?", (user_id,))}

    template_routines, template_objectives = [], []
    for profile_id in assigned_profile_ids:
        template_routines += con.execute(
            "SELECT name, schedule FROM profile_template_routines WHERE profile_id = ?", (profile_id,)
        ).fetchall()
        template_objectives += con.execute(
            "SELECT title, domain, follow_up_interval_days, source_note, essential "
            "FROM profile_template_objectives WHERE profile_id = ?", (profile_id,)
        ).fetchall()
    con.close()

    routines_created, routines_skipped = [], []
    seen_routine_names = set()
    for name, schedule in template_routines:
        if name in existing_routines or name in seen_routine_names:
            routines_skipped.append(name)
            continue
        seen_routine_names.add(name)
        result = json.loads(create_routine({"user_name": user_name, "name": name, "schedule": schedule}))
        routines_created.append(result)

    objectives_created, objectives_skipped = [], []
    seen_titles = set()
    for title, domain, follow_up_interval_days, source_note, essential in template_objectives:
        if title in existing_titles or title in seen_titles:
            objectives_skipped.append(title)
            continue
        seen_titles.add(title)
        result = json.loads(tools_objectives.create_objective({
            "title": title, "domain": domain, "user_name": user_name,
            "follow_up_interval_days": follow_up_interval_days, "source_note": source_note,
            "essential": essential,
        }))
        objectives_created.append(result)

    return json.dumps({
        "user_name": user_name,
        "routines_created": routines_created, "routines_skipped": routines_skipped,
        "objectives_created": objectives_created, "objectives_skipped": objectives_skipped,
    })
