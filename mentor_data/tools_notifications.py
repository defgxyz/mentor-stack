"""Generic notification fan-out: a topic is "about" a subject (e.g. Jasmine's
location) but its subscribers don't have to be that subject - decoupling who
a check is about from who gets told about it. Deliberately separate from the
check itself (services.script/prompt) - a topic can exist with no service
yet, and a service doesn't need a topic at all; either can be created
independently, then optionally linked via services.output_topic_id (what it
posts to) or services.trigger_topic_id (what triggers it, for chaining).

Delivery is per-subscriber, not a single fan-out send: each subscriber's own
mentor-pulse independently notices a pending post (list_due_topic_posts),
delivers it - framed however SOUL.md says is right for THAT recipient, e.g.
blunt for an admin, a gentle check-in for a child - and acks it
(acknowledge_topic_post_delivery). This is structural, not a style choice:
delivery always runs inside the subscriber's own Hermes profile (the
per-person isolation this whole project is built on), so one event reaching
two people is genuinely two independent pickups, not one message copied
twice.
"""
import json
from datetime import datetime, timezone

from . import db
from .tools_permissions import has_permission
from .tools_routines import is_family_admin


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_user(con, user_name: str):
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    return row[0] if row else None


def create_notification_topic(args: dict, **kwargs) -> str:
    """Create a new notification topic "about" a subject (e.g. Jasmine's
    location, a health concern) - the topic itself has no check logic; link
    a service's output_topic_id to it separately to actually feed it.
    Creating a topic about yourself needs the create_subscription
    permission (see tools_permissions); creating one about someone else
    requires being a family admin."""
    requesting_user = args["requesting_user_name"]
    subject_user_name = args["subject_user_name"]
    if subject_user_name == requesting_user:
        if not has_permission(requesting_user, "create_subscription"):
            return json.dumps({
                "error": f"'{requesting_user}' doesn't have permission to create "
                         "notification topics - ask a family admin."
            })
    elif not is_family_admin(requesting_user):
        return json.dumps({
            "error": f"'{requesting_user}' is not a family admin - only a family admin "
                     f"can create a notification topic about someone else ('{subject_user_name}')."
        })
    con = db.connect()
    try:
        subject_id = _resolve_user(con, subject_user_name)
        if subject_id is None:
            return json.dumps({"error": f"no user named '{subject_user_name}'"})
        now = _now()
        cur = con.execute(
            "INSERT INTO notification_topics (subject_user_id, title, category, created_at) VALUES (?,?,?,?)",
            (subject_id, args["title"], args.get("category") or None, now),
        )
        con.commit()
        return json.dumps({"ok": True, "topic_id": cur.lastrowid, "subject_user_name": subject_user_name,
                            "title": args["title"]})
    finally:
        con.close()


def list_notification_topics(args: dict, **kwargs) -> str:
    """List notification topics, optionally filtered to those about a
    specific subject_user_name."""
    con = db.connect()
    try:
        query = (
            "SELECT notification_topics.id, users.name, notification_topics.title, "
            "notification_topics.category, notification_topics.created_at "
            "FROM notification_topics JOIN users ON users.id = notification_topics.subject_user_id"
        )
        params = []
        if args.get("subject_user_name"):
            query += " WHERE users.name = ?"
            params.append(args["subject_user_name"])
        query += " ORDER BY notification_topics.created_at"
        rows = con.execute(query, params).fetchall()
        return json.dumps([
            {"topic_id": tid, "subject_user_name": name, "title": title, "category": category,
             "created_at": created_at}
            for tid, name, title, category, created_at in rows
        ])
    finally:
        con.close()


def create_topic_subscription(args: dict, **kwargs) -> str:
    """Subscribe someone to a topic, so they get told when something posts
    to it. Subscribing yourself needs the create_subscription permission;
    subscribing someone else (e.g. a parent subscribing a child to their own
    safety topic) requires being a family admin. Safe to call again for an
    already-existing subscription (no-op, not an error)."""
    requesting_user = args["requesting_user_name"]
    subscriber_user_name = args["subscriber_user_name"]
    if subscriber_user_name == requesting_user:
        if not has_permission(requesting_user, "create_subscription"):
            return json.dumps({
                "error": f"'{requesting_user}' doesn't have permission to create "
                         "subscriptions - ask a family admin."
            })
    elif not is_family_admin(requesting_user):
        return json.dumps({
            "error": f"'{requesting_user}' is not a family admin - only a family admin "
                     f"can subscribe someone else ('{subscriber_user_name}') to a topic."
        })
    con = db.connect()
    try:
        topic_id = args["topic_id"]
        topic_row = con.execute("SELECT id FROM notification_topics WHERE id = ?", (topic_id,)).fetchone()
        if topic_row is None:
            return json.dumps({"error": f"no notification topic with id {topic_id}"})
        subscriber_id = _resolve_user(con, subscriber_user_name)
        if subscriber_id is None:
            return json.dumps({"error": f"no user named '{subscriber_user_name}'"})
        con.execute(
            "INSERT OR IGNORE INTO topic_subscriptions (topic_id, subscriber_user_id, created_at) VALUES (?,?,?)",
            (topic_id, subscriber_id, _now()),
        )
        con.commit()
        return json.dumps({"ok": True, "topic_id": topic_id, "subscriber_user_name": subscriber_user_name})
    finally:
        con.close()


def list_topic_subscriptions(args: dict, **kwargs) -> str:
    """List subscriptions, optionally filtered by topic_id and/or
    subscriber_user_name."""
    con = db.connect()
    try:
        query = (
            "SELECT topic_subscriptions.id, topic_subscriptions.topic_id, users.name, "
            "topic_subscriptions.created_at "
            "FROM topic_subscriptions JOIN users ON users.id = topic_subscriptions.subscriber_user_id"
        )
        conditions, params = [], []
        if args.get("topic_id") is not None:
            conditions.append("topic_subscriptions.topic_id = ?")
            params.append(args["topic_id"])
        if args.get("subscriber_user_name"):
            conditions.append("users.name = ?")
            params.append(args["subscriber_user_name"])
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        rows = con.execute(query, params).fetchall()
        return json.dumps([
            {"subscription_id": sid, "topic_id": tid, "subscriber_user_name": name, "created_at": created_at}
            for sid, tid, name, created_at in rows
        ])
    finally:
        con.close()


def post_to_topic(args: dict, **kwargs) -> str:
    """Post a real finding to a topic - called by whatever check triggered
    (e.g. the services skill, for a service with an output_topic_id set),
    not conversationally. Every current subscriber will pick this up
    independently via their own list_due_topic_posts."""
    topic_id = args["topic_id"]
    content = args["content"]
    con = db.connect()
    try:
        topic_row = con.execute("SELECT id FROM notification_topics WHERE id = ?", (topic_id,)).fetchone()
        if topic_row is None:
            return json.dumps({"error": f"no notification topic with id {topic_id}"})
        cur = con.execute(
            "INSERT INTO topic_posts (topic_id, content, created_at) VALUES (?,?,?)",
            (topic_id, content, _now()),
        )
        con.commit()
        return json.dumps({"ok": True, "topic_post_id": cur.lastrowid, "topic_id": topic_id})
    finally:
        con.close()


def list_due_topic_posts(args: dict, **kwargs) -> str:
    """Posts on topics this person subscribes to, that haven't been
    delivered to THEM yet specifically - the per-subscriber due-check for
    the topic-notification skill. Call this at the start of that skill's
    run; call acknowledge_topic_post_delivery for each one after delivering
    it, regardless of how it's framed."""
    user_name = args["user_name"]
    con = db.connect()
    try:
        subscriber_id = _resolve_user(con, user_name)
        if subscriber_id is None:
            return json.dumps({"error": f"no user named '{user_name}'"})
        rows = con.execute(
            "SELECT topic_posts.id, notification_topics.title, notification_topics.id, "
            "subject_users.name, topic_posts.content, topic_posts.created_at "
            "FROM topic_posts "
            "JOIN topic_subscriptions ON topic_subscriptions.topic_id = topic_posts.topic_id "
            "JOIN notification_topics ON notification_topics.id = topic_posts.topic_id "
            "JOIN users AS subject_users ON subject_users.id = notification_topics.subject_user_id "
            "WHERE topic_subscriptions.subscriber_user_id = ? "
            "AND topic_posts.id NOT IN ("
            "  SELECT topic_post_id FROM topic_post_deliveries WHERE subscriber_user_id = ?"
            ") ORDER BY topic_posts.created_at",
            (subscriber_id, subscriber_id),
        ).fetchall()
        return json.dumps([
            {"topic_post_id": pid, "topic_title": ttitle, "topic_id": tid, "subject_user_name": subj,
             "content": content, "created_at": created_at}
            for pid, ttitle, tid, subj, content, created_at in rows
        ])
    finally:
        con.close()


def acknowledge_topic_post_delivery(args: dict, **kwargs) -> str:
    """Mark a topic post as delivered to this specific subscriber - MUST be
    called for every post list_due_topic_posts returned, regardless of how
    it was framed or whether the person responded. Without this, the same
    post would resurface every pulse tick forever."""
    topic_post_id = args["topic_post_id"]
    user_name = args["user_name"]
    con = db.connect()
    try:
        subscriber_id = _resolve_user(con, user_name)
        if subscriber_id is None:
            return json.dumps({"error": f"no user named '{user_name}'"})
        post_row = con.execute("SELECT id FROM topic_posts WHERE id = ?", (topic_post_id,)).fetchone()
        if post_row is None:
            return json.dumps({"error": f"no topic post with id {topic_post_id}"})
        con.execute(
            "INSERT OR IGNORE INTO topic_post_deliveries (topic_post_id, subscriber_user_id, delivered_at) "
            "VALUES (?,?,?)",
            (topic_post_id, subscriber_id, _now()),
        )
        con.commit()
        return json.dumps({"ok": True, "topic_post_id": topic_post_id, "user_name": user_name})
    finally:
        con.close()
