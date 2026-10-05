"""
Unified memory model: Hermes's own holographic memory plugin (fact_store,
per-profile SQLite - see memory_store.db) is THE real memory now, not a
separate Mentor-owned store. This module owns exactly one thing on top of
it: a thin (user, fact_id) -> visibility marker (fact_visibility table),
keyed by the real fact_id fact_store's own tool calls already return -
never a second copy of the content itself.

Superseded the old `user_memories` table entirely (dropped in db.py's
migrations) - that table existed to hold BOTH the content and its
visibility, which meant two separate things the agent could write facts
into (a real, observed source of confusion and a duplicate-write bug -
see the SOUL.md holographic-migration entries). Now there is exactly one
place content lives (fact_store) and exactly one place visibility lives
(this table) - set_fact_visibility is the only write path, called with a
fact_id the agent already has from a prior fact_store call's response.

No row for a given fact means private - the safe default, since fact_store
now captures everything (not just things deliberately meant to share, the
way the old user_memories table's entries always were) - so an absent
visibility marker must not accidentally read as "safe to share."

Marking a fact shared also syncs a name-prefixed copy directly into the
"family" Hermes profile's own fact_store (event-driven - happens inside
set_fact_visibility itself, not a periodic job), so the family agent can
recall it natively rather than only via list_family_shared_context.
family_synced_facts (keyed by the source fact's stable users.id, not its
name) tracks the mapping so un-sharing removes the right copy. See
set_fact_visibility's own docstring for the real trade-off this makes
(no HRR vector on synced rows - fact_store's own add_fact() isn't
reachable from this container).
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import db

_VALID_VISIBILITY = {"private", "shared"}

# Where each profile's real holographic memory store lives, AS MOUNTED into
# whichever container is calling this (mentor-mcp and mentor-dashboard both
# mount ~/.hermes/profiles read-only at this same path - see docker-compose.yml).
# Not the path Hermes's own /api/fs REST calls use (that's the path inside
# the Hermes container itself, /opt/data/profiles/...) - this is a filesystem
# read from a completely different container, which is why the mount exists.
_HERMES_PROFILES_MOUNT = "/hermes_home/profiles"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hermes_profile_for_user(user_name: str) -> str:
    return user_name.strip().lower()


def _facts_db_path(user_name: str) -> str:
    return f"{_HERMES_PROFILES_MOUNT}/{_hermes_profile_for_user(user_name)}/memory_store.db"


def read_holographic_facts(user_name: str) -> tuple[list[dict], Optional[str]]:
    """(facts, error). A profile with no facts saved yet has no
    memory_store.db file at all (created lazily on first write) - that's
    the normal state for a brand-new profile, not an error. Shared by
    mentor-mcp (set_fact_visibility's existence check, list_family_shared_context)
    and mentor-dashboard (the Memory panel) - one implementation, not two
    that could drift."""
    path = _facts_db_path(user_name)
    if not os.path.exists(path):
        return [], None
    try:
        # immutable=1 only when this directory is actually mounted
        # read-only from here (every profile except "family" - see
        # docker-compose.yml). Real bug hit and fixed: immutable=1 tells
        # SQLite this file will NEVER change for the connection's
        # lifetime and skips re-checking the WAL for later writes - fine
        # (necessary, even - mode=ro alone still fails to open at all on a
        # read-only bind mount) for profiles this container never writes
        # to, but WRONG for "family", which set_fact_visibility's sync
        # writes to from this same process - confirmed live: immutable=1
        # served a stale fact list right after a real delete had already
        # committed and was visible from a fresh connection elsewhere.
        # os.access is a generic "can I actually write here" check, not a
        # hardcoded "family is special" - stays correct if another
        # profile ever gets write access for some other reason later.
        writable = os.access(os.path.dirname(path), os.W_OK)
        uri = f"file:{path}?mode=ro" if writable else f"file:{path}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT fact_id, content, category, trust_score, created_at "
                "FROM facts ORDER BY created_at DESC"
            ).fetchall()
        finally:
            con.close()
        return [
            {"fact_id": r[0], "content": r[1], "category": r[2] or "general",
             "trust_score": r[3], "created_at": r[4]}
            for r in rows
        ], None
    except sqlite3.Error as e:
        return [], f"could not read memory store: {e}"


def _resolve_user_id(con, user_name: str) -> Optional[int]:
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    return row[0] if row else None


# The one profile shared facts sync into - not a real Mentor user (no
# users row), so it's a constant rather than something resolved via
# _resolve_user_id.
_FAMILY_PROFILE = "family"

# holographic's own schema, verified live against a real, already-in-use
# profile's memory_store.db (facts/entities/fact_entities/facts_fts/
# memory_banks + the three FTS-sync triggers) - needed here because a
# freshly auto-provisioned profile (create_user -> _provision_hermes_profile)
# does NOT come with a working memory_store.db: cloning "default" excludes
# it the same way it excludes sessions/, so the file either doesn't exist
# yet or (worse, discovered live) SQLite auto-creates an empty 0-byte stub
# the moment ANY connection - including a failed one - touches the path,
# leaving a real file with zero tables that silently breaks every write
# after it, not just the first. Normally the real plugin creates this
# lazily on first genuine use; since this container writes directly
# instead (see _write_fact's own docstring for why), it has to be able to
# bootstrap the same schema itself rather than assume it already exists.
_HOLOGRAPHIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);
CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);
CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;
"""


def _ensure_holographic_schema(con: sqlite3.Connection) -> None:
    """Idempotent (IF NOT EXISTS everywhere) - safe to call before every
    write, cheap no-op on a profile that already has the real schema from
    genuine fact_store use."""
    con.executescript(_HOLOGRAPHIC_SCHEMA)


def _write_fact(profile_name: str, content: str, category: str) -> int:
    """INSERT one row directly into the given profile's own facts table,
    returning the new fact_id. Deliberately NOT going through fact_store's
    own add_fact() (that class lives inside the Hermes image's own Python
    package, not reachable from this container) - a real, disclosed
    trade-off: the trigger-based FTS index (facts_ai, confirmed present on
    every profile's store) keeps content/tags searchable via fact_store's
    plain search/list/probe actions same as any other fact, but
    hrr_vector stays NULL for a written-this-way row, so it won't
    participate in HRR-based semantic `related`/`reason` similarity
    scoring the way a natively-added fact would - acceptable for what
    these syncs need (making a fact recallable at all), not a full
    reimplementation of the plugin's own write path. Callable against ANY
    profile this container has write access to (currently: every real
    profile plus family - see docker-compose.yml), not just family -
    generalized once the profile-field sync needed to write into a
    person's OWN store first, not just family's."""
    path = _facts_db_path(profile_name)
    con = sqlite3.connect(path, timeout=10)
    try:
        _ensure_holographic_schema(con)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cur = con.execute(
            "INSERT INTO facts (content, category, tags, trust_score, created_at, updated_at) "
            "VALUES (?, ?, '', 0.5, ?, ?)",
            (content, category, now, now),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _delete_fact(profile_name: str, fact_id: int) -> None:
    path = _facts_db_path(profile_name)
    if not os.path.exists(path):
        return
    con = sqlite3.connect(path, timeout=10)
    try:
        con.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
        con.commit()
    finally:
        con.close()


def _update_fact(profile_name: str, fact_id: int, content: str) -> None:
    """Content replacement in place (same fact_id) - the facts_au trigger
    keeps the FTS index correct on UPDATE the same way facts_ai/facts_ad do
    for INSERT/DELETE (all three confirmed present on every profile's
    schema)."""
    path = _facts_db_path(profile_name)
    con = sqlite3.connect(path, timeout=10)
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        con.execute("UPDATE facts SET content = ?, updated_at = ? WHERE fact_id = ?",
                    (content, now, fact_id))
        con.commit()
    finally:
        con.close()


def _sync_fact_to_family(source_user_id: int, source_user_name: str, source_fact_id: int,
                          content: str, category: str) -> None:
    """Creates the synced copy on first share; on every later call for the
    same (source_user_id, source_fact_id) - i.e. an explicit re-call of
    set_fact_visibility(shared) for something already shared - refreshes
    the family-side copy's content instead of no-op'ing. Real bug found
    via sync_profile_to_own_memory (which calls set_fact_visibility(shared)
    again on every profile edit): a plain no-op here meant family's copy
    of a person's profile-summary fact went stale forever after the FIRST
    edit, since the ledger row already existed. An explicit re-call of
    "make this shared" is naturally read as "and reflect what it says
    now," not just "leave whatever's already there alone" - fixed for
    every synced fact, not special-cased for profile summaries. A fact
    that's ALREADY shared and never gets set_fact_visibility called again
    still won't auto-refresh (this only runs when that tool is actually
    called) - true one-directional freshness would need watching the
    source for edits, out of scope here. Prefixes with the person's
    CURRENT name (e.g. "Evgeniy: ...") so family's own memory - which
    otherwise has no concept of whose fact this originally was - stays
    attributable; keyed internally by users.id (stable) rather than the
    name (which can change via update_user), per family_synced_facts' own
    docstring in db.py."""
    con = db.connect()
    try:
        existing = con.execute(
            "SELECT id, family_fact_id FROM family_synced_facts WHERE source_user_id = ? AND source_fact_id = ?",
            (source_user_id, source_fact_id),
        ).fetchone()
        if existing:
            _, family_fact_id = existing
            _update_fact(_FAMILY_PROFILE, family_fact_id, f"{source_user_name}: {content}")
            return
        family_fact_id = _write_fact(_FAMILY_PROFILE, f"{source_user_name}: {content}", category)
        con.execute(
            "INSERT INTO family_synced_facts (source_user_id, source_fact_id, family_fact_id, synced_at) "
            "VALUES (?,?,?,?)",
            (source_user_id, source_fact_id, family_fact_id, _now()),
        )
        con.commit()
    finally:
        con.close()


def _unsync_fact_from_family(source_user_id: int, source_fact_id: int) -> None:
    """Idempotent: no-ops if this fact was never synced (e.g. it was
    already private)."""
    con = db.connect()
    try:
        row = con.execute(
            "SELECT id, family_fact_id FROM family_synced_facts WHERE source_user_id = ? AND source_fact_id = ?",
            (source_user_id, source_fact_id),
        ).fetchone()
        if not row:
            return
        ledger_id, family_fact_id = row
        _delete_fact(_FAMILY_PROFILE, family_fact_id)
        con.execute("DELETE FROM family_synced_facts WHERE id = ?", (ledger_id,))
        con.commit()
    finally:
        con.close()


def set_fact_visibility(args: dict, **kwargs) -> str:
    """Mark one already-saved fact_store fact as shared (visible to the
    Family dashboard chat via list_family_shared_context, AND synced - see
    below - into the family profile's own fact_store) or private (default
    - not shared). fact_id comes from a prior fact_store call's own
    response (add/search/probe/list all return it) - this tool never
    creates or edits fact content itself, only its visibility.

    Marking something shared immediately copies it (prefixed with this
    person's name) into the family profile's own memory, so the family
    agent can recall it natively via its own fact_store, not just via
    list_family_shared_context - marking it private again immediately
    removes that copy. This sync is one-directional and content-frozen at
    share time: editing or removing the ORIGINAL fact via fact_store does
    not update or remove an already-synced copy - re-share (or ask a
    family admin to) if the content changes and the shared copy should
    catch up."""
    user_name = args["user_name"]
    fact_id = args["fact_id"]
    visibility = args.get("visibility", "private")
    if visibility not in _VALID_VISIBILITY:
        return json.dumps({"error": f"visibility must be 'private' or 'shared', got '{visibility}'"})
    con = db.connect()
    user_id = _resolve_user_id(con, user_name)
    if user_id is None:
        con.close()
        return json.dumps({"error": f"no user named '{user_name}'"})
    con.close()

    facts, err = read_holographic_facts(user_name)
    if err:
        return json.dumps({"error": err})
    matched = next((f for f in facts if f["fact_id"] == fact_id), None)
    if matched is None:
        return json.dumps({"error": f"no fact with fact_id {fact_id} found for '{user_name}' - "
                                     f"check fact_store's own list/search result for the real id"})

    con = db.connect()
    now = _now()
    con.execute(
        "INSERT INTO fact_visibility (user_id, fact_id, visibility, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, fact_id) DO UPDATE SET visibility = excluded.visibility, updated_at = excluded.updated_at",
        (user_id, fact_id, visibility, now),
    )
    con.commit()
    con.close()

    sync_error = None
    try:
        if visibility == "shared":
            _sync_fact_to_family(user_id, user_name, fact_id, matched["content"], matched["category"])
        else:
            _unsync_fact_from_family(user_id, fact_id)
    except sqlite3.Error as e:
        # The visibility marker itself is already committed (list_family_shared_context
        # is correct either way, since it reads live) - a sync-step failure degrades to
        # "not yet natively recallable via family's own fact_store" rather than losing
        # the actual visibility decision.
        sync_error = str(e)

    result = {"ok": True, "user_name": user_name, "fact_id": fact_id, "visibility": visibility}
    if sync_error:
        result["family_sync_error"] = sync_error
    return json.dumps(result)


def list_family_shared_context(args: dict, **kwargs) -> str:
    """Everyone's basic profile facts (name, type, date_of_birth, gender,
    notes, home_timezone - the same fields already visible on each
    person's own dashboard page, nothing from private storage) plus any of
    their real fact_store facts explicitly marked visibility='shared' via
    set_fact_visibility - the real, narrow set of cross-person information
    that's safe to bring into a family-wide conversation. Never includes a
    private fact, an objective/routine that wasn't itself marked shared,
    or anything not explicitly marked - a fact_store entry with no
    visibility marker at all is private by default. Intended for the
    Family dashboard chat's own planning/discussion use, not for looking
    up one specific person (use fact_store directly, scoped to that
    person's own conversation, for that)."""
    con = db.connect()
    users = con.execute(
        "SELECT id, name, type, date_of_birth, gender, notes, home_timezone FROM users ORDER BY name"
    ).fetchall()
    out = []
    for user_id, name, type_, dob, gender, notes, home_tz in users:
        shared_ids = {
            r[0] for r in con.execute(
                "SELECT fact_id FROM fact_visibility WHERE user_id = ? AND visibility = 'shared'",
                (user_id,),
            ).fetchall()
        }
        shared_facts = []
        if shared_ids:
            facts, _err = read_holographic_facts(name)
            shared_facts = [
                {"content": f["content"], "category": f["category"], "created_at": f["created_at"]}
                for f in facts if f["fact_id"] in shared_ids
            ]
        out.append({
            "user_name": name, "type": type_, "date_of_birth": dob, "gender": gender,
            "notes": notes, "home_timezone": home_tz,
            "shared_memories": shared_facts,
        })
    con.close()
    return json.dumps(out)


def _profile_summary_content(name: str, type_: str, dob: Optional[str], gender: Optional[str],
                              home_tz: Optional[str], notes: Optional[str]) -> str:
    return (f"type={type_}; date_of_birth={dob or '?'}; gender={gender or '?'}; "
            f"home_timezone={home_tz or '?'}; notes={notes or '(none)'}")


def sync_profile_to_own_memory(user_id: int) -> None:
    """Best-effort, never raises (called from create_user/update_user,
    which must succeed regardless of this). Two-hop by design, not a
    separate direct-to-family path: upserts ONE combined profile-summary
    fact per person into THAT PERSON'S OWN fact_store first (tracked via
    user_profile_facts, one row per user, content replaced in place on
    each call - not five separate per-field facts), then calls
    set_fact_visibility on it with visibility="shared" - the exact same
    mechanism a person's own agent uses for any other fact, which is what
    actually propagates it into family's memory (_sync_fact_to_family).
    One real sync pipeline into family, not two - a person's own
    dashboard "Memory" panel also shows this fact with its Shared toggle
    already on, same as anything else they've shared, rather than family
    silently holding a copy with no equivalent anywhere the person can
    see it. These fields are already always included in
    list_family_shared_context with no per-field visibility gate (see
    that function's own docstring) - this doesn't make a new privacy
    decision, it just gives fact_store (both the person's own and
    family's) a native copy of data that was already unconditionally
    family-visible."""
    try:
        con = db.connect()
        try:
            row = con.execute(
                "SELECT name, type, date_of_birth, gender, home_timezone, notes FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return
            name, type_, dob, gender, home_tz, notes = row
            content = _profile_summary_content(name, type_, dob, gender, home_tz, notes)
            existing = con.execute(
                "SELECT own_fact_id FROM user_profile_facts WHERE source_user_id = ?",
                (user_id,),
            ).fetchone()
            now = _now()
            if existing:
                own_fact_id = existing[0]
                _update_fact(name, own_fact_id, content)
                con.execute(
                    "UPDATE user_profile_facts SET updated_at = ? WHERE source_user_id = ?",
                    (now, user_id),
                )
            else:
                own_fact_id = _write_fact(name, content, "user_pref")
                con.execute(
                    "INSERT INTO user_profile_facts (source_user_id, own_fact_id, updated_at) VALUES (?,?,?)",
                    (user_id, own_fact_id, now),
                )
            con.commit()
        finally:
            con.close()
        set_fact_visibility({"user_name": name, "fact_id": own_fact_id, "visibility": "shared"})
    except sqlite3.Error:
        pass
