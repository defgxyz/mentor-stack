import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import wellbeing_templates

DB_PATH = Path(__file__).parent / "mentor.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    domain TEXT NOT NULL CHECK (domain IN ('hobby','health','career','finance','relationship','media','other')),
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed','abandoned')),
    priority INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    target_date TEXT,
    created_at TEXT NOT NULL,
    last_progress_at TEXT,
    source_note TEXT,
    follow_up_interval_days INTEGER NOT NULL DEFAULT 7,
    schedule_cron TEXT,
    next_follow_up_at TEXT,
    parent_objective_id INTEGER REFERENCES objectives(id),
    user_id INTEGER REFERENCES users(id),
    time_cost_hours_per_week REAL,
    money_cost_amount REAL,
    money_cost_currency TEXT,
    money_cost_frequency TEXT CHECK (money_cost_frequency IN ('one_off','weekly','monthly','yearly') OR money_cost_frequency IS NULL),
    essential TEXT NOT NULL DEFAULT 'optional' CHECK (essential IN ('essential','optional')),
    visibility TEXT NOT NULL DEFAULT 'shared' CHECK (visibility IN ('shared','private'))
);

CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id INTEGER NOT NULL REFERENCES objectives(id),
    title TEXT NOT NULL,
    order_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','skipped')),
    target_date TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS progress_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id INTEGER NOT NULL REFERENCES objectives(id),
    milestone_id INTEGER REFERENCES milestones(id),
    note TEXT,
    metric_value REAL,
    metric_unit TEXT,
    logged_at TEXT NOT NULL,
    sentiment TEXT CHECK (sentiment IN ('on_track','struggling','excited','stalled', NULL))
);

CREATE TABLE IF NOT EXISTS health_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_type TEXT NOT NULL,
    value REAL,
    unit TEXT,
    note TEXT,
    objective_id INTEGER REFERENCES objectives(id),
    logged_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS media_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL CHECK (media_type IN ('book','show','movie','game','podcast','article','other')),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('want_to','in_progress','completed','dropped')),
    rating INTEGER CHECK (rating BETWEEN 1 AND 5),
    notes TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

-- A real individual (Evgeniy, Jasmine) - was formerly called `profiles`.
-- `type` still drives SOUL.md's tone calibration directly (fast/simple
-- attribute); richer category membership lives in `user_profiles` below.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'adult' CHECK (type IN ('toddler','preschool','child','teen','adult','senior')),
    is_family_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    available_hours_per_week REAL,
    available_money_per_month REAL,
    budget_currency TEXT,
    notes TEXT,
    has_own_pairing INTEGER NOT NULL DEFAULT 0,
    home_timezone TEXT,
    recent_timezone TEXT,
    recent_timezone_at TEXT,
    share_location_with_family INTEGER NOT NULL DEFAULT 0,
    date_of_birth TEXT,
    gender TEXT,
    role TEXT,
    share_network_activity_with_family INTEGER NOT NULL DEFAULT 0
);

-- A physical device belonging to a user (phone, tablet, laptop, watch...).
-- Generic on purpose: type is free text like health_metrics.metric_type,
-- since the real variety here is unbounded.
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- One integration instance attached to a device (e.g. service_type=
-- "tracking", provider="owntracks", external_id="jasmine" - the identity
-- that provider's own API needs). A device can have several services
-- (e.g. "tracking" AND, later, "screentime" on the same phone). Generic so
-- a new integration is a new row, not a new column on `users`/`devices`.
CREATE TABLE IF NOT EXISTS device_services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    service_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_id TEXT,
    created_at TEXT NOT NULL
);

-- A category/cohort (e.g. "Adult", "Child", or a custom one like "New
-- Parent") - a reusable set of goals/routines that applies to whoever is
-- assigned to it via user_profiles. Seeded with the six age-stages by
-- _seed_profile_categories(); extensible with custom categories afterward.
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL
);

-- Many-to-many: a user can belong to several profile categories at once.
CREATE TABLE IF NOT EXISTS user_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    assigned_at TEXT NOT NULL
);

-- What a profile category implies - DB-backed replacement for the old
-- hardcoded wellbeing_templates.py bundle, editable per category without a
-- code change. apply_wellbeing_template() instantiates these onto a user.
CREATE TABLE IF NOT EXISTS profile_template_routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    name TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT 'daily'
);

CREATE TABLE IF NOT EXISTS profile_template_objectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    follow_up_interval_days INTEGER,
    source_note TEXT,
    essential TEXT NOT NULL DEFAULT 'optional'
);

-- Real, stored Conversation -> User link, so identity doesn't have to be
-- re-inferred conversationally every session. source is free text (telegram,
-- discord, whatsapp, web, etc) so new sources need no schema change.
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    linked_at TEXT NOT NULL
);

-- Static edges between user nodes (e.g. Evgeniy --parent--> Jasmine).
-- Directed and free-text on purpose - relationship_type is too varied to
-- enumerate (father/mother/daughter/son/sibling/spouse/guardian/friend/...),
-- same reasoning as health_metrics.metric_type. Record both directions
-- explicitly (two rows) when both are known - nothing infers "child" from
-- "parent" automatically.
CREATE TABLE IF NOT EXISTS user_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id),
    to_user_id INTEGER NOT NULL REFERENCES users(id),
    relationship_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT 'daily',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routine_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id INTEGER NOT NULL REFERENCES routines(id),
    completed_at TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'told_me',
    note TEXT
);

-- Ownership metadata layered on top of Hermes's own holographic memory
-- (fact_store) - NOT a memory store itself. fact_store is the one real
-- memory now (per-profile SQLite, holographic plugin); this table only
-- ever holds a (user, fact_id) -> visibility marker, keyed by the real
-- fact_id fact_store's own tool calls already return. No row for a given
-- fact means private (the safe default - fact_store captures everything,
-- not just things meant to share, unlike the old user_memories table this
-- replaced, which existed only for deliberately-shared entries and so
-- could safely default the other way). Superseded the old `user_memories`
-- table entirely (dropped - see _MIGRATIONS) once this shipped; that
-- table's own real content was already fully duplicated in fact_store, so
-- nothing needed migrating.
CREATE TABLE IF NOT EXISTS fact_visibility (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    fact_id INTEGER NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','shared')),
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_visibility_unique ON fact_visibility(user_id, fact_id);

-- Ledger for the shared-fact sync into the "family" Hermes profile's own
-- fact_store (see tools_memory.py's set_fact_visibility) - tracks which
-- source fact (keyed by the real, stable users.id, not a name that could
-- change) produced which fact_id inside family's own memory_store.db, so
-- an un-share can find and remove the right synced copy, and a re-share
-- doesn't create a duplicate. user_id here is deliberately NOT a foreign
-- key with ON DELETE CASCADE - a deleted user's already-synced facts stay
-- listed here so they can still be cleaned up rather than orphaned.
CREATE TABLE IF NOT EXISTS family_synced_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id INTEGER NOT NULL REFERENCES users(id),
    source_fact_id INTEGER NOT NULL,
    family_fact_id INTEGER NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_family_synced_facts_source
    ON family_synced_facts(source_user_id, source_fact_id);

-- For the "basic profile fields" (type/date_of_birth/gender/home_timezone/
-- notes) list_family_shared_context already always includes for everyone -
-- those aren't fact_store facts on the source side, so create_user/
-- update_user upsert ONE combined profile-summary fact per person
-- directly into THAT PERSON'S OWN fact_store first (see tools_memory.py's
-- sync_profile_to_own_memory), tracked here one-row-per-user so later
-- calls replace its content in place instead of duplicating it. That
-- fact is then marked shared via the normal set_fact_visibility path,
-- same as any other fact - propagation into family's memory reuses
-- family_synced_facts above, not a second family-side ledger.
CREATE TABLE IF NOT EXISTS user_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id INTEGER NOT NULL REFERENCES users(id),
    own_fact_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profile_facts_user
    ON user_profile_facts(source_user_id);

-- Generic proactive-initiative scheduling: one row per (user, domain) -
-- same domain enum as objectives.domain - tracking when that TOPIC
-- (health/career/finance/hobby/relationship/media/other) was last
-- proactively raised, independent of whether any objective exists for it
-- yet. This is deliberately a SEPARATE mechanism from objectives.
-- next_follow_up_at (that's per-objective, "is this specific goal
-- overdue for a check-in"; this is per-topic, "should Mentor initiate a
-- conversation about this whole area of life at all, even with nothing
-- tracked there yet"). See tools_topics.py.
CREATE TABLE IF NOT EXISTS topic_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    domain TEXT NOT NULL CHECK (domain IN ('hobby','health','career','finance','relationship','media','other')),
    last_checkin_at TEXT,
    next_checkin_at TEXT NOT NULL,
    checkin_interval_days INTEGER NOT NULL DEFAULT 7,
    schedule_cron TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_checkins_user_domain
    ON topic_checkins(user_id, domain);
CREATE INDEX IF NOT EXISTS idx_topic_checkins_next ON topic_checkins(next_checkin_at);

-- Generic recurring job ("Service") - generalized from the original
-- Watchlist mechanism ("periodically check something external against my
-- criteria, tell me only when something new shows up"): runs either on a
-- schedule (trigger_type='cron', the original behaviour) or in reaction to
-- new posts on a notification topic (trigger_type='topic', trigger_topic_id)
-- - either a deterministic script, an open-ended agent prompt, or both
-- (script runs first via the terminal tool, then prompt reasons over the
-- result - the same "script, then reason" shape Watchlist always used).
-- category is free text (like health_metrics.metric_type) since new kinds
-- of service keep showing up - prompt carries the real instructions
-- regardless of category. Can optionally post its own output to a topic
-- (output_topic_id) for chaining: Service A -> topic X -> Service B
-- (trigger_topic_id = X) -> topic Y -> a person. See tools_services.py.
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    category TEXT,
    prompt TEXT NOT NULL,
    sources TEXT,
    script TEXT,
    objective_id INTEGER REFERENCES objectives(id),
    output_topic_id INTEGER REFERENCES notification_topics(id),
    trigger_type TEXT NOT NULL DEFAULT 'cron' CHECK (trigger_type IN ('cron','topic')),
    trigger_topic_id INTEGER REFERENCES notification_topics(id),
    schedule_interval_days INTEGER NOT NULL DEFAULT 7,
    schedule_cron TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed','abandoned')),
    last_checked_at TEXT,
    next_check_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_services_user ON services(user_id);
CREATE INDEX IF NOT EXISTS idx_services_next ON services(next_check_at);

-- One row per genuinely new finding a service has already reported - lets a
-- run ask "have I told them this one already" instead of re-reporting the
-- same result every cycle. dedupe_key is a stable identifier for the found
-- item (a URL/listing id/etc.) when one exists; NULL is allowed for
-- findings with no natural stable id (falls back to eyeballing the summary
-- text). Named "results" rather than "matches" - reads as a noun ("the
-- results a service produced"), not a verb phrase.
CREATE TABLE IF NOT EXISTS service_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL REFERENCES services(id),
    summary TEXT NOT NULL,
    dedupe_key TEXT,
    found_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_service_results_service ON service_results(service_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_service_results_dedupe
    ON service_results(service_id, dedupe_key) WHERE dedupe_key IS NOT NULL;

-- Tracks which topic-triggered services have already processed which
-- incoming topic post - mirrors topic_post_deliveries' per-consumer-ack
-- shape, but for a SERVICE deciding whether to re-run, not a person being
-- delivered a message. Only relevant when services.trigger_type='topic';
-- unused for a plain cron-triggered service.
CREATE TABLE IF NOT EXISTS service_topic_consumption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL REFERENCES services(id),
    topic_post_id INTEGER NOT NULL REFERENCES topic_posts(id),
    consumed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_service_topic_consumption_unique
    ON service_topic_consumption(service_id, topic_post_id);

-- Role-based permission grants. "role" is the effective role for a user
-- (users.role if an admin has explicitly set one, else users.type -
-- see tools_permissions.effective_role) - NOT itself a users column value
-- validated here, since new permissions/roles may be introduced later
-- without a schema change. Presence of a row = granted (same "absent row =
-- denied by default" pattern as fact_visibility) - simpler than an
-- allowed/denied boolean since there's no real use for an explicit deny yet.
CREATE TABLE IF NOT EXISTS role_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    permission TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_role_permissions_unique ON role_permissions(role, permission);

-- Generic notification fan-out: a topic is "about" a subject (e.g. Jasmine's
-- location) but can have subscribers who are NOT that subject (e.g. a
-- parent) - decoupling who a check is about from who gets told about it.
-- Deliberately separate from services itself (script/prompt = the check;
-- topic = who cares and how many of them) so either can be created
-- independently - a service doesn't need a topic, and a topic can be fed
-- by several services (or consumed by one, via trigger_topic_id, to chain).
CREATE TABLE IF NOT EXISTS notification_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    category TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_topics_subject ON notification_topics(subject_user_id);

CREATE TABLE IF NOT EXISTS topic_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES notification_topics(id),
    subscriber_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_subscriptions_unique
    ON topic_subscriptions(topic_id, subscriber_user_id);

-- One row per real post to a topic (e.g. a linked service found
-- something notable). Content is a plain-language description of what
-- happened - deliberately NOT pre-styled for any one recipient; each
-- subscriber's own delivery (see topic_post_deliveries) is where SOUL.md's
-- tone/sensitivity guidance actually gets applied, per who's reading it.
CREATE TABLE IF NOT EXISTS topic_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES notification_topics(id),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topic_posts_topic ON topic_posts(topic_id);

-- Per-subscriber delivery tracking - each subscriber's own mentor-pulse
-- independently notices/delivers/acks a post, since delivery always runs
-- inside THAT subscriber's own Hermes profile (per-person isolation), never
-- a single fan-out send from somewhere else.
CREATE TABLE IF NOT EXISTS topic_post_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_post_id INTEGER NOT NULL REFERENCES topic_posts(id),
    subscriber_user_id INTEGER NOT NULL REFERENCES users(id),
    delivered_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_post_deliveries_unique
    ON topic_post_deliveries(topic_post_id, subscriber_user_id);

CREATE INDEX IF NOT EXISTS idx_objectives_status ON objectives(status);
CREATE INDEX IF NOT EXISTS idx_progress_objective ON progress_entries(objective_id);
CREATE INDEX IF NOT EXISTS idx_health_metrics_type ON health_metrics(metric_type);
CREATE INDEX IF NOT EXISTS idx_media_log_status ON media_log(status);
CREATE INDEX IF NOT EXISTS idx_routines_user ON routines(user_id);
CREATE INDEX IF NOT EXISTS idx_routine_completions_routine ON routine_completions(routine_id);
CREATE INDEX IF NOT EXISTS idx_user_relationships_from ON user_relationships(from_user_id);
CREATE INDEX IF NOT EXISTS idx_user_relationships_to ON user_relationships(to_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_relationships_unique
    ON user_relationships(from_user_id, to_user_id, relationship_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profiles_unique ON user_profiles(user_id, profile_id);
CREATE INDEX IF NOT EXISTS idx_profile_template_routines_profile ON profile_template_routines(profile_id);
CREATE INDEX IF NOT EXISTS idx_profile_template_objectives_profile ON profile_template_objectives(profile_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_source_chat ON conversations(source, chat_id);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_device_services_device ON device_services(device_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_services_unique
    ON device_services(device_id, service_type, provider);
"""

# Indexes on columns added by _MIGRATIONS below - must run AFTER migrations, not
# as part of _SCHEMA, or CREATE INDEX fails on an existing db that predates the
# column (CREATE TABLE IF NOT EXISTS is a no-op there, but the index isn't).
_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_objectives_next_follow_up ON objectives(next_follow_up_at);
CREATE INDEX IF NOT EXISTS idx_objectives_parent ON objectives(parent_objective_id);
CREATE INDEX IF NOT EXISTS idx_objectives_user ON objectives(user_id);
CREATE INDEX IF NOT EXISTS idx_services_objective ON services(objective_id);
CREATE INDEX IF NOT EXISTS idx_services_output_topic ON services(output_topic_id);
CREATE INDEX IF NOT EXISTS idx_services_trigger_topic ON services(trigger_topic_id);
"""

# Default cadence per domain when an objective doesn't set its own - matches the
# staleness thresholds SKILL.md used to compute ad hoc from last_progress_at; now
# baked into next_follow_up_at directly instead of being recomputed each check.
DEFAULT_FOLLOW_UP_DAYS = {
    "hobby": 7,
    "health": 7,
    "career": 14,
    "finance": 14,
    "relationship": 14,
    "media": 14,
    "other": 14,
}

# Columns added after the original CREATE TABLE shipped - ALTER TABLE ADD COLUMN
# for anyone with an existing mentor.db (SQLite has no "ADD COLUMN IF NOT EXISTS").
# Targets `users` (not `profiles`) throughout - by the time these run, the
# one-time _migrate_to_user_profile_model() below has already renamed the old
# `profiles` table (individuals) to `users`, so these entries silently no-op
# via "duplicate column" on any DB that already has them (which is every DB
# that's been through this session's migrations, live or fresh).
_MIGRATIONS = [
    "ALTER TABLE objectives ADD COLUMN follow_up_interval_days INTEGER NOT NULL DEFAULT 7",
    "ALTER TABLE objectives ADD COLUMN next_follow_up_at TEXT",
    "ALTER TABLE objectives ADD COLUMN parent_objective_id INTEGER REFERENCES objectives(id)",
    "ALTER TABLE users ADD COLUMN is_family_admin INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE objectives ADD COLUMN time_cost_hours_per_week REAL",
    "ALTER TABLE objectives ADD COLUMN money_cost_amount REAL",
    "ALTER TABLE objectives ADD COLUMN money_cost_currency TEXT",
    "ALTER TABLE objectives ADD COLUMN money_cost_frequency TEXT",
    "ALTER TABLE objectives ADD COLUMN essential TEXT NOT NULL DEFAULT 'optional'",
    "ALTER TABLE users ADD COLUMN available_hours_per_week REAL",
    "ALTER TABLE users ADD COLUMN available_money_per_month REAL",
    "ALTER TABLE users ADD COLUMN budget_currency TEXT",
    "ALTER TABLE users ADD COLUMN notes TEXT",
    "ALTER TABLE users ADD COLUMN has_own_pairing INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE objectives ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'",
    "ALTER TABLE users ADD COLUMN home_timezone TEXT",
    "ALTER TABLE users ADD COLUMN recent_timezone TEXT",
    "ALTER TABLE users ADD COLUMN recent_timezone_at TEXT",
    "ALTER TABLE users ADD COLUMN share_location_with_family INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN date_of_birth TEXT",
    "ALTER TABLE users ADD COLUMN gender TEXT",
    # user_memories retired in favor of fact_visibility (see _SCHEMA's
    # comment) - its own real content was already fully duplicated in
    # fact_store by the earlier holographic migration, so this is a clean
    # drop, not a data-losing one. DROP TABLE IF EXISTS is itself
    # idempotent (no-op on a fresh install or an already-migrated DB), so
    # this stays a permanent line here rather than a one-time script -
    # consistent with how the owntracks_user_id column drop was handled.
    "DROP TABLE IF EXISTS user_memories",
    # family_synced_profiles was a same-session redesign (direct-to-family
    # sync for profile fields), replaced before it ever synced anything
    # real by the two-hop design (user_profile_facts + reusing
    # set_fact_visibility) - confirmed empty (0 rows) before dropping, so
    # this is a clean removal of dead schema, not a data-losing one.
    "DROP TABLE IF EXISTS family_synced_profiles",
    # Which fixed-library persona (see mentor_data/personas.py) is spliced
    # into this person's own SOUL.md right now - NULL means "use the
    # generic age-based DEFAULT_PERSONA_BLOCK", the normal starting state
    # (nothing auto-assigns one). Free text, not a CHECK-constrained enum,
    # since personas.py's own PERSONA_TYPE_SCOPE is already the real
    # validation (checked in tools_personas.set_persona before ever
    # writing here) - a CHECK constraint here would just be a second,
    # driftable copy of that same list.
    "ALTER TABLE users ADD COLUMN persona_key TEXT",
    # NULL means auto-detect at cron-creation time (real Telegram pairing ->
    # telegram, preferring mobile reach; otherwise their dashboard session).
    # Explicit values ('telegram'/'dashboard') override auto-detection -
    # a genuine per-person preference, not something Mentor decides for
    # them. NOTE: changing this after a person's topic-checkin cron already
    # exists does NOT retroactively move it - Hermes's cron --deliver
    # target is fixed at creation time, so the cron must be re-created to
    # pick up a changed preference (see tools_topics.py's own docstring).
    "ALTER TABLE users ADD COLUMN preferred_checkin_channel TEXT",
    # research_watches/research_watch_matches renamed to watchlist_items/
    # watchlist_matches (same feature, renamed before any real cron/data
    # existed for it - confirmed empty before dropping, so this is a clean
    # removal of the old names, not a data-losing one).
    "DROP TABLE IF EXISTS research_watch_matches",
    "DROP TABLE IF EXISTS research_watches",
    # Admin-settable override of a user's effective role for PERMISSION
    # purposes (tools_permissions.effective_role) - separate from `type`,
    # which stays purely about age/tone calibration (personas, wording).
    # NULL (the default - nobody has one yet) means permission checks fall
    # back to `type` directly. Only ever settable by a family admin
    # (tools_permissions.set_user_role) - never self-service, unlike most
    # other profile fields.
    "ALTER TABLE users ADD COLUMN role TEXT",
    # Optional real cron expression ("0 9 * * 1" = every Monday 9am)
    # overriding the flat day-interval for this item's own due-time -
    # see mentor_data/scheduling.py. NULL (the default) keeps the existing
    # "every N days from whenever it last fired" behaviour.
    "ALTER TABLE objectives ADD COLUMN schedule_cron TEXT",
    "ALTER TABLE topic_checkins ADD COLUMN schedule_cron TEXT",
    # NOTE: the old "ALTER TABLE watchlist_items ADD COLUMN
    # script/objective_id/topic_id/schedule_cron" entries that used to live
    # here were folded into _migrate_watchlist_to_services() below once
    # watchlist_items itself was renamed/extended into `services` - by the
    # time _MIGRATIONS runs (after _SCHEMA), the table no longer exists
    # under the old name, so those lines would always silently no-op anyway
    # (caught by this loop's own OperationalError handling either way).
    # Same shape and purpose as share_location_with_family (self always,
    # family admin always, opt-in for anyone else) but a SEPARATE flag,
    # deliberately - being fine with "my phone shows up as online/offline
    # and transferring data or not" is a different consent than being fine
    # with precise GPS location, and either should be settable without the
    # other changing.
    "ALTER TABLE users ADD COLUMN share_network_activity_with_family INTEGER NOT NULL DEFAULT 0",
    # NULL = "use voices.DEFAULT_VOICE" (same NULL-means-default convention as
    # persona_key above) - an explicit value means this person chose it via
    # set_tts_voice, see tools_voice.py.
    "ALTER TABLE users ADD COLUMN tts_voice TEXT",
]

# Evgeniy's own Telegram pairing predates the `conversations` table entirely
# (it's Hermes's home_channel, set up outside approve_pairing_code) - seeded
# once, directly, from config.yaml's telegram.home_channel.user_id.
_KNOWN_HOME_CHANNEL = {"source": "telegram", "chat_id": "5356768720", "user_name": "Evgeniy"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_profile_categories(con: sqlite3.Connection, now: str) -> None:
    """Idempotent: seeds the six built-in age-stage profile categories (and
    their template routines/objectives) from wellbeing_templates.py, skipping
    any category name that already exists. Safe to call on every init_schema()
    - used both by the one-time live-DB migration and by a fresh install."""
    existing = {r[0] for r in con.execute("SELECT name FROM profiles")}
    for user_type in wellbeing_templates.CATEGORY_TYPES:
        display_name = wellbeing_templates.CATEGORY_DISPLAY_NAMES[user_type]
        if display_name in existing:
            continue
        cur = con.execute(
            "INSERT INTO profiles (name, description, created_at) VALUES (?,?,?)",
            (display_name, f"Default wellbeing bundle for the '{user_type}' age-stage.", now),
        )
        profile_id = cur.lastrowid
        for r in wellbeing_templates.COMMON_ROUTINES:
            con.execute(
                "INSERT INTO profile_template_routines (profile_id, name, schedule) VALUES (?,?,?)",
                (profile_id, r["name"], r["schedule"]),
            )
        objs = list(wellbeing_templates.COMMON_HEALTH_OBJECTIVES)
        if user_type in wellbeing_templates.ADULT_ONLY_TYPES:
            objs = objs + wellbeing_templates.ADULT_ONLY_OBJECTIVES
        for o in objs:
            con.execute(
                "INSERT INTO profile_template_objectives "
                "(profile_id, title, domain, follow_up_interval_days, source_note, essential) VALUES (?,?,?,?,?,?)",
                (profile_id, o["title"], "health", o["follow_up_interval_days"], o["source_note"], "optional"),
            )


def _migrate_to_user_profile_model(con: sqlite3.Connection) -> None:
    """One-time structural migration for a pre-existing mentor.db: the old
    `profiles` table meant an individual; from here on `users` does, and
    `profiles` means a category. No-ops on a fresh install (nothing to
    migrate - _SCHEMA creates the final shape directly) or an already-
    migrated DB (checked via sqlite_master, not a flag column, so this is
    safe to call on every init_schema()).

    Ordering is load-bearing: every rename (table AND column) happens BEFORE
    _SCHEMA's executescript runs. _SCHEMA's CREATE TABLE IF NOT EXISTS
    statements only skip a table that already exists under its FINAL name -
    if `profile_memories`/`profile_relationships` etc. hadn't already been
    renamed to `user_memories`/`user_relationships` by this point, that
    executescript would silently create empty new-named tables alongside the
    old data instead of migrating it in place.
    """
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "users" in tables:
        return  # already migrated
    if "profiles" not in tables:
        return  # fresh install - _SCHEMA alone creates the final shape

    con.execute("ALTER TABLE profiles RENAME TO users")
    con.execute("ALTER TABLE objectives RENAME COLUMN profile_id TO user_id")
    con.execute("ALTER TABLE routines RENAME COLUMN profile_id TO user_id")
    con.execute("ALTER TABLE profile_memories RENAME TO user_memories")
    con.execute("ALTER TABLE user_memories RENAME COLUMN profile_id TO user_id")
    con.execute("ALTER TABLE profile_relationships RENAME TO user_relationships")
    con.execute("ALTER TABLE user_relationships RENAME COLUMN from_profile_id TO from_user_id")
    con.execute("ALTER TABLE user_relationships RENAME COLUMN to_profile_id TO to_user_id")

    # Now safe: every existing table has its final name/shape, so this only
    # creates the genuinely new tables (profiles-as-categories, user_profiles,
    # profile_template_*, conversations).
    con.executescript(_SCHEMA)

    now = _now()
    _seed_profile_categories(con, now)

    category_ids = {r[0]: r[1] for r in con.execute("SELECT name, id FROM profiles")}
    for user_id, user_type in con.execute("SELECT id, type FROM users").fetchall():
        display_name = wellbeing_templates.CATEGORY_DISPLAY_NAMES.get(user_type)
        if display_name and display_name in category_ids:
            con.execute(
                "INSERT OR IGNORE INTO user_profiles (user_id, profile_id, assigned_at) VALUES (?,?,?)",
                (user_id, category_ids[display_name], now),
            )

    home = _KNOWN_HOME_CHANNEL
    owner = con.execute("SELECT id FROM users WHERE name = ?", (home["user_name"],)).fetchone()
    if owner is not None:
        con.execute(
            "INSERT OR IGNORE INTO conversations (source, chat_id, user_id, linked_at) VALUES (?,?,?,?)",
            (home["source"], home["chat_id"], owner[0], now),
        )

    con.commit()


def _migrate_watchlist_to_services(con: sqlite3.Connection) -> None:
    """One-time: generalizes the old watchlist-only mechanism into a
    "Service" (a recurring job triggered by cron OR by new posts on a
    notification topic, running a script and/or an agent prompt, optionally
    posting its own output to a topic) - watchlist was always this shape
    minus the topic-trigger half, so this renames/extends the existing
    tables in place rather than building Service as a second, parallel
    mechanism. No-ops on a fresh install (nothing to migrate - _SCHEMA
    creates `services` directly) or an already-migrated DB (checked via
    sqlite_master). Must run BEFORE _SCHEMA's executescript, same ordering
    rule as _migrate_to_user_profile_model - otherwise _SCHEMA's
    "CREATE TABLE IF NOT EXISTS services" would create an empty table
    alongside the still-watchlist_items-named real data instead of renaming
    it in place."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "services" in tables:
        return  # already migrated
    if "watchlist_items" not in tables:
        return  # fresh install - _SCHEMA alone creates the final shape

    con.execute("ALTER TABLE watchlist_items RENAME TO services")
    con.execute("ALTER TABLE services RENAME COLUMN criteria TO prompt")
    con.execute("ALTER TABLE services RENAME COLUMN topic_id TO output_topic_id")
    con.execute(
        "ALTER TABLE services ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'cron' "
        "CHECK (trigger_type IN ('cron','topic'))"
    )
    con.execute("ALTER TABLE services ADD COLUMN trigger_topic_id INTEGER REFERENCES notification_topics(id)")
    con.execute("ALTER TABLE watchlist_matches RENAME TO service_matches")
    con.execute("ALTER TABLE service_matches RENAME COLUMN item_id TO service_id")
    con.commit()


def _migrate_service_matches_to_results(con: sqlite3.Connection) -> None:
    """One-time: service_matches renamed to service_results (clearer name -
    "matches" read as a verb phrase; "results" is unambiguous as a noun for
    what a service run produces). Same rename-in-place pattern as every
    other table rename in this project - must run BEFORE _SCHEMA's
    executescript, same ordering rule as _migrate_watchlist_to_services,
    otherwise "CREATE TABLE IF NOT EXISTS service_results" would create an
    empty table alongside the still-service_matches-named real data instead
    of renaming it in place. Drops the old-named indexes explicitly (a
    plain table RENAME updates what an index points at but not the index's
    own name, and _SCHEMA's indexes below use the new name, so without this
    the old-named ones would linger as harmless but redundant duplicates)."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "service_results" in tables:
        return  # already migrated
    if "service_matches" not in tables:
        return  # fresh install - _SCHEMA alone creates the final shape

    con.execute("ALTER TABLE service_matches RENAME TO service_results")
    con.execute("DROP INDEX IF EXISTS idx_service_matches_service")
    con.execute("DROP INDEX IF EXISTS idx_service_matches_dedupe")
    con.commit()


def _migrate_owntracks_column_to_devices(con: sqlite3.Connection) -> None:
    """One-time: the OwnTracks link used to live as a single `users.owntracks_user_id`
    column; it's now a generic devices/device_services pair (user -> device ->
    service). No-ops once the column is gone (already migrated, or a fresh
    install that never had it). Must run AFTER _SCHEMA's executescript -
    unlike the users/profiles rename, these are net-new tables with no name
    collision, so creating them first is simplest."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    if "owntracks_user_id" not in cols:
        return

    now = _now()
    for user_id, name, external_id in con.execute(
        "SELECT id, name, owntracks_user_id FROM users WHERE owntracks_user_id IS NOT NULL"
    ).fetchall():
        cur = con.execute(
            "INSERT INTO devices (user_id, name, type, created_at) VALUES (?,?,?,?)",
            (user_id, f"{name}'s device", "phone", now),
        )
        device_id = cur.lastrowid
        con.execute(
            "INSERT INTO device_services (device_id, service_type, provider, external_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (device_id, "tracking", "owntracks", external_id, now),
        )
    con.execute("ALTER TABLE users DROP COLUMN owntracks_user_id")
    con.commit()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_schema() -> None:
    con = connect()
    _migrate_to_user_profile_model(con)
    _migrate_watchlist_to_services(con)
    _migrate_service_matches_to_results(con)
    con.executescript(_SCHEMA)
    _migrate_owntracks_column_to_devices(con)
    _seed_profile_categories(con, _now())
    for stmt in _MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    con.executescript(_POST_MIGRATION_INDEXES)
    _seed_default_role_permissions(con, _now())
    con.commit()
    con.close()


# Starting policy for the new role/permission system - a family admin can
# change this later via grant_role_permission/revoke_role_permission
# (tools_permissions.py), this is just where it starts. "adult"/"senior" can
# create their own notification-topic subscriptions; every other type
# (toddler/preschool/child/teen) has no permissions granted here at all -
# absent row = denied, matching fact_visibility's own default-deny shape.
# is_family_admin is checked separately (tools_permissions.has_permission)
# and always passes regardless of this table - admins aren't a "role" row.
_DEFAULT_ROLE_PERMISSIONS = [
    ("adult", "create_subscription"),
    ("senior", "create_subscription"),
]


def _seed_default_role_permissions(con: sqlite3.Connection, now: str) -> None:
    """Idempotent - INSERT OR IGNORE keyed on the real unique index, so a
    permission an admin deliberately revoked later never gets silently
    re-granted by a future init_schema() call."""
    for role, permission in _DEFAULT_ROLE_PERMISSIONS:
        con.execute(
            "INSERT OR IGNORE INTO role_permissions (role, permission, created_at) VALUES (?,?,?)",
            (role, permission, now),
        )
