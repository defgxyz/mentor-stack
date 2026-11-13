"""
Standalone MCP server exposing Mentor's objective-tracking tools over SSE, for
Hermes to register natively via `hermes mcp add` and call as part of its own
real agent loop - unlike the earlier mentor-bridge approach (a fake OpenAI
"model" wrapping a closed-loop Claude Agent SDK session), this lets Hermes see
and drive the tool calls itself, alongside its own built-in toolsets (web
search, memory, etc.). See ~/.claude/plans/vivid-greeting-perlis.md.

FastMCP builds tool schemas from Python type hints/docstrings, not the raw
JSON-schema dicts in mentor_data/schemas.py - these wrappers exist to give it
something to introspect, then just pack args into a dict for the existing
implementation functions in mentor_data/tools_objectives.py.
"""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from mentor_data import db
from mentor_data import tools_objectives as impl
from mentor_data import tools_health
from mentor_data import tools_media
from mentor_data import tools_routines
from mentor_data import tools_pairing
from mentor_data import tools_capacity
from mentor_data import tools_memory
from mentor_data import tools_relationships
from mentor_data import tools_profile_categories
from mentor_data import tools_devices
from mentor_data import tools_personas
from mentor_data import tools_voice
from mentor_data import tools_topics
from mentor_data import tools_services
from mentor_data import tools_permissions
from mentor_data import tools_notifications

db.init_schema()

mcp = FastMCP("mentor", host="0.0.0.0", port=8000)


@mcp.tool()
def create_objective(
    title: str,
    domain: str,
    description: Optional[str] = None,
    target_date: Optional[str] = None,
    priority: Optional[int] = None,
    source_note: Optional[str] = None,
    follow_up_interval_days: Optional[int] = None,
    schedule_cron: Optional[str] = None,
    parent_objective_id: Optional[int] = None,
    user_name: Optional[str] = None,
    time_cost_hours_per_week: Optional[float] = None,
    money_cost_amount: Optional[float] = None,
    money_cost_currency: Optional[str] = None,
    money_cost_frequency: Optional[str] = None,
    essential: Optional[str] = None,
    visibility: Optional[str] = None,
) -> str:
    """Create a new trackable objective when the user mentions a new goal, hobby,
    project, or intention they want to pursue over time - including one that
    already exists in their life (not just brand-new intentions), if you learn
    about it in conversation and it isn't tracked yet: offer to track it rather
    than letting it pass as just prose. domain must be one of: hobby, health,
    career, finance, relationship, media, other. ALWAYS call list_objectives
    first to check it doesn't already exist before creating a duplicate. If
    this is a real chunk of a bigger existing objective that deserves its own
    independent tracking and follow-up cadence (not just a checklist step -
    use add_milestone for those), set parent_objective_id to link it as a
    sub-objective. Set user_name to whoever this objective is for (the user
    you identified this conversation as) - omit only for a genuinely
    family-wide/unscoped objective, which should be rare. Effort fields (all
    optional, ask rather than guess if unknown): time_cost_hours_per_week
    (realistic ongoing time commitment), money_cost_amount +
    money_cost_currency + money_cost_frequency (one_off, weekly, monthly, or
    yearly), and essential ("essential" - a real obligation/need, vs
    "optional" - discretionary, default). These feed analyze_objective_load,
    which compares total committed time/money across a user's active
    objectives against their available budget - useful whenever the person is
    weighing whether they can realistically take on something new. visibility
    must be "shared" (default - visible to the whole family in the dashboard)
    or "private" (hidden from the Family view and other users' views, shown
    only to its own owner - and as an opaque "Busy" block on the calendar to
    everyone else). Ask before marking something private rather than
    assuming; most objectives should stay shared. schedule_cron is optional -
    a real 5-field cron expression (e.g. "0 9 * * 1" for every Monday at
    9am, "0 9 * * 1,2" for every Monday and Tuesday at 9am) for a specific
    recurring check-in day/time, overriding follow_up_interval_days once set
    - use this when the person names a specific day/time rather than just
    "every N days"."""
    args = {"title": title, "domain": domain, "description": description, "target_date": target_date,
            "source_note": source_note, "follow_up_interval_days": follow_up_interval_days,
            "schedule_cron": schedule_cron,
            "parent_objective_id": parent_objective_id, "user_name": user_name,
            "time_cost_hours_per_week": time_cost_hours_per_week, "money_cost_amount": money_cost_amount,
            "money_cost_currency": money_cost_currency, "money_cost_frequency": money_cost_frequency,
            "essential": essential, "visibility": visibility}
    if priority is not None:
        args["priority"] = priority
    return impl.create_objective({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def add_milestone(objective_id: int, title: str, order_index: Optional[int] = None,
                   target_date: Optional[str] = None) -> str:
    """Add a milestone (a checklist step, not an independently-tracked sub-objective)
    to an existing objective."""
    args = {"objective_id": objective_id, "title": title, "order_index": order_index, "target_date": target_date}
    return impl.add_milestone({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def update_objective(
    objective_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[int] = None,
    target_date: Optional[str] = None,
    follow_up_interval_days: Optional[int] = None,
    schedule_cron: Optional[str] = None,
    next_follow_up_at: Optional[str] = None,
    user_name: Optional[str] = None,
    time_cost_hours_per_week: Optional[float] = None,
    money_cost_amount: Optional[float] = None,
    money_cost_currency: Optional[str] = None,
    money_cost_frequency: Optional[str] = None,
    essential: Optional[str] = None,
    visibility: Optional[str] = None,
) -> str:
    """Update any subset of an objective's fields directly - only the fields you
    pass are changed. Use this to: change status (active, paused, completed,
    abandoned - e.g. when the user says they're pausing or dropping a goal, or
    finished it), change priority, correct a title/description, reassign
    user_name (e.g. an objective created before users existed, or logged
    under the wrong person), reschedule directly by setting next_follow_up_at
    (ISO datetime), follow_up_interval_days (in days from now the next
    check-in cadence should be), or schedule_cron (a real 5-field cron
    expression, e.g. "0 9 * * 1" for every Monday at 9am - takes precedence
    over follow_up_interval_days once set; pass "" to clear it and go back to
    the plain interval), set/correct effort fields
    (time_cost_hours_per_week, money_cost_amount/currency/frequency,
    essential) once you learn them, or change visibility ("shared"/"private" -
    e.g. the user asks to make something private, or share a private one with
    the family). Prefer log_progress instead of this when the user reports a
    real progress update - that also reschedules and keeps a history entry,
    this tool does not."""
    args = {"objective_id": objective_id, "title": title, "description": description, "domain": domain,
             "status": status, "priority": priority, "target_date": target_date,
             "follow_up_interval_days": follow_up_interval_days, "schedule_cron": schedule_cron,
             "next_follow_up_at": next_follow_up_at,
             "user_name": user_name, "time_cost_hours_per_week": time_cost_hours_per_week,
             "money_cost_amount": money_cost_amount, "money_cost_currency": money_cost_currency,
             "money_cost_frequency": money_cost_frequency, "essential": essential, "visibility": visibility}
    return impl.update_objective({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def update_milestone(milestone_id: int, title: Optional[str] = None,
                      status: Optional[str] = None, target_date: Optional[str] = None) -> str:
    """Update a milestone - most commonly to mark it done (status="done") or
    skipped (status="skipped") when the user reports finishing a step, or to
    rename/reschedule it. status must be one of: pending, done, skipped."""
    args = {"milestone_id": milestone_id, "title": title, "status": status, "target_date": target_date}
    return impl.update_milestone({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def log_progress(objective_id: int, note: Optional[str] = None, milestone_id: Optional[int] = None,
                  metric_value: Optional[float] = None, metric_unit: Optional[str] = None,
                  sentiment: Optional[str] = None) -> str:
    """Record a progress update against an existing objective (or one of its
    milestones). sentiment, if given, must be one of: on_track, struggling,
    excited, stalled. This automatically reschedules the objective's next
    follow-up date."""
    args = {"objective_id": objective_id, "note": note, "milestone_id": milestone_id,
            "metric_value": metric_value, "metric_unit": metric_unit, "sentiment": sentiment}
    return impl.log_progress({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_objectives(status: Optional[str] = None, domain: Optional[str] = None,
                     top_level_only: Optional[bool] = None, parent_objective_id: Optional[int] = None,
                     user_name: Optional[str] = None, visibility: Optional[str] = None) -> str:
    """List tracked objectives, optionally filtered by status (active, paused,
    completed, abandoned), domain, hierarchy position, user_name (whoever it
    belongs to), or visibility ("shared"/"private"). Call this before
    creating a new objective to avoid duplicates, and whenever the user asks
    about their goals. Use top_level_only=true for a clean top-level view.
    Use parent_objective_id to list only the sub-objectives of one objective.
    Objectives created before users existed may have no user_name -
    filtering by user_name will not show those."""
    args = {"status": status, "domain": domain, "top_level_only": top_level_only,
            "parent_objective_id": parent_objective_id, "user_name": user_name, "visibility": visibility}
    return impl.list_objectives({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def get_objective(objective_id: int) -> str:
    """Get full detail on one objective: milestones, progress history, its
    parent (if it's a sub-objective), its own direct sub-objectives (if any,
    one level - call again on a child's id to go deeper), any Services
    linked to it (standing research actively working toward this
    objective, e.g. property searches feeding a "buy a house" goal), and
    tagged_facts - fact_store facts tagged `obj:<this id>` by convention,
    broader context beyond what was explicitly logged via log_progress.
    Use progress + milestones + tagged_facts together when asked to recap
    or tell the story of how an objective went - narrate from what's
    actually here, chronologically, never invented."""
    return impl.get_objective({"objective_id": objective_id})


@mcp.tool()
def list_due_followups(user_name: Optional[str] = None) -> str:
    """List active objectives whose scheduled follow-up date has arrived. Call
    this at the start of a proactive check-in to know exactly which objectives
    to mention - don't recompute staleness yourself from list_objectives. Pass
    your own user_name in a per-person pulse run so you only ever see your own
    due objectives, never anyone else's."""
    return impl.list_due_followups({"user_name": user_name} if user_name else {})


@mcp.tool()
def acknowledge_followup(objective_id: int) -> str:
    """Call this for each objective right after delivering a proactive
    check-in about it, whether or not the user has replied yet. Reschedules
    its next follow-up date forward so it resurfaces later instead of staying
    silently stuck. Do NOT call this for an objective the user just gave a
    real progress update on in the same turn - use log_progress instead,
    which already reschedules it."""
    return impl.acknowledge_followup({"objective_id": objective_id})


@mcp.tool()
def list_due_topic_checkins(user_name: str) -> str:
    """Which life topics (domains) are due for a proactive check-in for
    this person right now - normally 0 or 1. Different from
    list_due_followups: that's per-OBJECTIVE (an existing goal overdue for
    a status update); this is per-TOPIC (a whole area of life - health,
    career, finance, hobby, relationship, media, other - due for Mentor to
    initiate a conversation about, even one with nothing tracked there
    yet). Call this at the start of a topic-checkin run to know which
    domain to actually talk about - don't guess."""
    return tools_topics.list_due_topic_checkins({"user_name": user_name})


@mcp.tool()
def acknowledge_topic_checkin(user_name: str, domain: str) -> str:
    """Call this once, at the END of a topic-checkin conversation,
    regardless of how it went (deep engagement, a one-word reply, or no
    reply at all) - reschedules this topic forward (weekly by default) so
    it comes up again later instead of never resurfacing. Do this even if
    nothing changed and the conversation concluded "all good, nothing to
    update" - that's a completely valid outcome, not a reason to skip
    acknowledging."""
    return tools_topics.acknowledge_topic_checkin({"user_name": user_name, "domain": domain})


@mcp.tool()
def set_topic_checkin_schedule(user_name: str, domain: str, schedule_cron: Optional[str] = None,
                                schedule_interval_days: Optional[int] = None) -> str:
    """Set a specific recurring day/time for this person's topic check-in on
    one domain (e.g. schedule_cron="0 9 * * 1" for every Monday at 9am),
    overriding the default weekly cadence for just this one (user, domain)
    pair - use when the person names a specific day/time. Pass
    schedule_cron="" to clear it and fall back to schedule_interval_days."""
    args = {"user_name": user_name, "domain": domain, "schedule_interval_days": schedule_interval_days}
    if schedule_cron is not None:
        args["schedule_cron"] = schedule_cron
    return tools_topics.set_topic_checkin_schedule({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def add_service(user_name: str, title: str, prompt: str,
                 category: Optional[str] = None, sources: Optional[str] = None,
                 script: Optional[str] = None, objective_id: Optional[int] = None,
                 output_topic_id: Optional[int] = None, trigger_type: str = "cron",
                 trigger_topic_id: Optional[int] = None, schedule_interval_days: int = 7,
                 schedule_cron: Optional[str] = None) -> str:
    """Add a standing Service for someone - the generic mechanism behind
    "check property listings weekly," "summarise news from these sites,"
    "find family-friendly events nearby this weekend," "watch for grocery
    deals," "check if someone's outside a location boundary," or any other
    "periodically do X and only tell me when something new/matching shows
    up" request. In a sense a service is either a script or an agent (or
    both): `prompt` must be the real, specific instructions (e.g. "3+
    bedroom houses in Bondi under $800k with parking," not just "houses") -
    this is what the services skill digests/reports against on each run.
    `sources` is optional specific sites/domains to check when the agent
    does its own web search. `script` is optional - a real shell command to
    run via the terminal tool BEFORE reasoning (deterministic data-
    gathering, e.g. a curl+jq against a specific API, or an internal check
    against this person's own location/health data), with its stdout then
    digested/reported on against `prompt`; omit it to let the agent gather
    data itself via web search/fetch, guided by `prompt`/`sources` alone.
    `trigger_type` is "cron" (the default - runs on
    schedule_interval_days/schedule_cron) or "topic" (runs whenever a NEW
    post lands on `trigger_topic_id` - use this to chain services: one
    service's output_topic_id feeds another service's trigger_topic_id).
    `objective_id` is optional - link this service to an existing objective
    it's researching toward (e.g. a property search feeding a "buy a house"
    objective), so get_objective shows it there. `output_topic_id` is
    optional - post genuinely new findings to a notification topic (see
    create_notification_topic) so its subscribers hear about it too, not
    just this service's own owner - e.g. a location-boundary check "about"
    a child, posted to a topic a parent subscribes to. `schedule_cron` is
    optional (cron-triggered services only) - a real 5-field cron
    expression (e.g. "0 9 * * 1" for every Monday at 9am, "0 9 * * 1,2" for
    every Monday and Tuesday at 9am) for a specific recurring day/time,
    overriding schedule_interval_days once set - use this when the person
    names a specific day/time rather than just "every N days". Proactively
    offer this when someone expresses a standing interest worth monitoring,
    not only when explicitly asked for a recurring check."""
    return tools_services.add_service({
        "user_name": user_name, "title": title, "prompt": prompt,
        "category": category, "sources": sources, "script": script,
        "objective_id": objective_id, "output_topic_id": output_topic_id,
        "trigger_type": trigger_type, "trigger_topic_id": trigger_topic_id,
        "schedule_interval_days": schedule_interval_days, "schedule_cron": schedule_cron,
    })


@mcp.tool()
def list_services(user_name: Optional[str] = None, status: Optional[str] = None,
                   objective_id: Optional[int] = None) -> str:
    """List someone's Services (or everyone's, for admin/debugging, if
    user_name is omitted). Filter by status (active/paused/completed/abandoned)
    and/or objective_id (only services linked to that objective) if wanted."""
    return tools_services.list_services({
        k: v for k, v in {"user_name": user_name, "status": status, "objective_id": objective_id}.items()
        if v is not None
    })


@mcp.tool()
def update_service(service_id: int, title: Optional[str] = None, prompt: Optional[str] = None,
                    sources: Optional[str] = None, script: Optional[str] = None,
                    objective_id: Optional[int] = None, output_topic_id: Optional[int] = None,
                    trigger_type: Optional[str] = None, trigger_topic_id: Optional[int] = None,
                    schedule_interval_days: Optional[int] = None,
                    schedule_cron: Optional[str] = None,
                    status: Optional[str] = None) -> str:
    """Update a Service's title/prompt/sources/script/objective_id/
    output_topic_id/trigger_type/trigger_topic_id/cadence, or its status -
    'paused' to temporarily stop checking, 'completed' once the thing being
    watched for was found (e.g. the house was bought), 'abandoned' if it's
    no longer wanted. schedule_cron is a real 5-field cron expression (e.g.
    "0 9 * * 1" for every Monday at 9am) - takes precedence over
    schedule_interval_days once set; pass "" to clear it and go back to the
    plain interval. There is no hard delete; only provided fields change."""
    return tools_services.update_service({
        k: v for k, v in {
            "service_id": service_id, "title": title, "prompt": prompt, "sources": sources,
            "script": script, "objective_id": objective_id, "output_topic_id": output_topic_id,
            "trigger_type": trigger_type, "trigger_topic_id": trigger_topic_id,
            "schedule_interval_days": schedule_interval_days, "schedule_cron": schedule_cron,
            "status": status,
        }.items() if v is not None or k == "service_id"
    })


@mcp.tool()
def list_due_services(user_name: str) -> str:
    """Which of this person's Services are due to run right now - either a
    cron-triggered one whose schedule has passed, or a topic-triggered one
    with new, unconsumed posts on its trigger topic (see
    pending_topic_post_ids in the result). Call this at the start of a
    services run to know what to actually do - don't guess."""
    return tools_services.list_due_services({"user_name": user_name})


@mcp.tool()
def list_service_results(service_id: int, limit: int = 20) -> str:
    """Results already reported for a Service, most recent first - check
    this BEFORE reporting something new, so the same result doesn't get
    re-sent every cycle."""
    return tools_services.list_service_results({"service_id": service_id, "limit": limit})


@mcp.tool()
def log_service_result(service_id: int, summary: str, dedupe_key: Optional[str] = None) -> str:
    """Record a genuinely new finding for a Service BEFORE telling the
    person about it, so future checks know not to repeat it. dedupe_key
    should be a stable identifier for the found item (a URL/listing id/etc.)
    when one exists - omit it only when nothing stable is available."""
    return tools_services.log_service_result({
        "service_id": service_id, "summary": summary, "dedupe_key": dedupe_key,
    })


@mcp.tool()
def acknowledge_service_check(service_id: int) -> str:
    """Call this once, at the END of checking a CRON-triggered Service,
    regardless of whether anything new was found - reschedules it forward
    so it comes up again later instead of never resurfacing. This is NOT
    optional. Not used for a topic-triggered service - call
    consume_topic_post for each pending post instead."""
    return tools_services.acknowledge_service_check({"service_id": service_id})


@mcp.tool()
def consume_topic_post(service_id: int, topic_post_id: int) -> str:
    """Call this once per id in list_due_services' own
    pending_topic_post_ids, for a TOPIC-triggered service, regardless of
    whether it produced a new match - marks that post as processed by this
    service so it doesn't make the service due again every pulse tick
    forever. This is NOT optional."""
    return tools_services.consume_topic_post({"service_id": service_id, "topic_post_id": topic_post_id})


@mcp.tool()
def set_user_role(requesting_user_name: str, target_user_name: str, role: Optional[str] = None) -> str:
    """Set (or clear, with an empty string) a user's PERMISSION role -
    overrides their type for has_permission()/role_permissions checks only,
    never affects tone/persona calibration (which stays driven by type).
    Only a family admin may call this - never self-service."""
    return tools_permissions.set_user_role({
        "requesting_user_name": requesting_user_name, "target_user_name": target_user_name, "role": role,
    })


@mcp.tool()
def grant_role_permission(requesting_user_name: str, role: str, permission: str) -> str:
    """Grant a permission to a role - e.g. role='teen', permission=
    'create_subscription' to let teens start creating their own
    notification-topic subscriptions (currently only adult/senior have this
    by default). Only a family admin may call this."""
    return tools_permissions.grant_role_permission({
        "requesting_user_name": requesting_user_name, "role": role, "permission": permission,
    })


@mcp.tool()
def revoke_role_permission(requesting_user_name: str, role: str, permission: str) -> str:
    """Revoke a previously-granted role permission. Only a family admin may
    call this."""
    return tools_permissions.revoke_role_permission({
        "requesting_user_name": requesting_user_name, "role": role, "permission": permission,
    })


@mcp.tool()
def list_role_permissions() -> str:
    """List the current permission policy - which roles have which
    permissions granted. This is policy, not a secret - anyone can view it."""
    return tools_permissions.list_role_permissions({})


@mcp.tool()
def create_notification_topic(requesting_user_name: str, subject_user_name: str, title: str,
                               category: Optional[str] = None) -> str:
    """Create a notification topic "about" a subject (e.g. Jasmine's
    location, a health concern) - the topic itself has no check logic; link
    a Service's output_topic_id to it separately to actually feed it (see
    add_service). Creating a topic about yourself needs the
    create_subscription permission; creating one about someone else
    requires being a family admin."""
    return tools_notifications.create_notification_topic({
        "requesting_user_name": requesting_user_name, "subject_user_name": subject_user_name,
        "title": title, "category": category,
    })


@mcp.tool()
def list_notification_topics(subject_user_name: Optional[str] = None) -> str:
    """List notification topics, optionally filtered to those about a
    specific subject_user_name."""
    return tools_notifications.list_notification_topics({
        k: v for k, v in {"subject_user_name": subject_user_name}.items() if v is not None
    })


@mcp.tool()
def create_topic_subscription(requesting_user_name: str, topic_id: int, subscriber_user_name: str) -> str:
    """Subscribe someone to a topic, so they get told (via their own
    topic-notification pulse) when something posts to it. Subscribing
    yourself needs the create_subscription permission; subscribing someone
    else (e.g. a parent subscribing themselves AND their child to the same
    safety topic) requires being a family admin. Safe to call again for an
    already-existing subscription."""
    return tools_notifications.create_topic_subscription({
        "requesting_user_name": requesting_user_name, "topic_id": topic_id,
        "subscriber_user_name": subscriber_user_name,
    })


@mcp.tool()
def list_topic_subscriptions(topic_id: Optional[int] = None, subscriber_user_name: Optional[str] = None) -> str:
    """List subscriptions, optionally filtered by topic_id and/or
    subscriber_user_name."""
    return tools_notifications.list_topic_subscriptions({
        k: v for k, v in {"topic_id": topic_id, "subscriber_user_name": subscriber_user_name}.items()
        if v is not None
    })


@mcp.tool()
def post_to_topic(topic_id: int, content: str) -> str:
    """Post a real finding to a topic - called by whatever check triggered
    (e.g. the services skill, for a service with an output_topic_id set),
    not conversationally. Every current subscriber picks this up
    independently via their own pulse."""
    return tools_notifications.post_to_topic({"topic_id": topic_id, "content": content})


@mcp.tool()
def list_due_topic_posts(user_name: str) -> str:
    """Posts on topics this person subscribes to, not yet delivered to THEM
    specifically. Call this at the start of a topic-notification run - don't
    guess what's pending."""
    return tools_notifications.list_due_topic_posts({"user_name": user_name})


@mcp.tool()
def acknowledge_topic_post_delivery(topic_post_id: int, user_name: str) -> str:
    """Call this once, for each post list_due_topic_posts returned, right
    after delivering it to this specific person - regardless of how it was
    framed or whether they responded. This is NOT optional - without it, the
    same post resurfaces every pulse tick forever."""
    return tools_notifications.acknowledge_topic_post_delivery({
        "topic_post_id": topic_post_id, "user_name": user_name,
    })


@mcp.tool()
def log_health_metric(metric_type: str, value: Optional[float] = None, unit: Optional[str] = None,
                       note: Optional[str] = None, objective_id: Optional[int] = None) -> str:
    """Log a wellbeing/health data point - e.g. sleep_hours, weight_kg, mood
    (1-10), exercise_minutes, steps, water_liters, or any other metric_type the
    user mentions. metric_type is free text, not a fixed list - use whatever
    name the user's own words imply, consistently across calls for the same
    thing. Optionally link to an objective_id if this metric relates to one
    (e.g. weight logs under a 'lose weight' objective)."""
    args = {"metric_type": metric_type, "value": value, "unit": unit, "note": note, "objective_id": objective_id}
    return tools_health.log_health_metric({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_health_metrics(metric_type: Optional[str] = None, since: Optional[str] = None,
                         limit: Optional[int] = None) -> str:
    """List logged health metrics, optionally filtered by metric_type (e.g.
    'sleep_hours') and/or a since date (ISO). Use this before answering
    questions like 'how has my sleep been' rather than guessing."""
    args = {"metric_type": metric_type, "since": since, "limit": limit}
    return tools_health.list_health_metrics({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def add_media(media_type: str, title: str, status: Optional[str] = None,
              rating: Optional[int] = None, notes: Optional[str] = None) -> str:
    """Add something to the media/hobbies log: media_type must be one of book,
    show, movie, game, podcast, article, other. status must be one of want_to,
    in_progress, completed, dropped (defaults to in_progress). Call
    list_media first to avoid duplicating an entry already tracked."""
    args = {"media_type": media_type, "title": title, "status": status, "rating": rating, "notes": notes}
    return tools_media.add_media({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def update_media(media_id: int, status: Optional[str] = None, rating: Optional[int] = None,
                  notes: Optional[str] = None, finished_at: Optional[str] = None) -> str:
    """Update a media log entry - most commonly to mark it completed (status=
    "completed", auto-stamps finished_at if you don't pass one) or dropped, or
    to add a rating (1-5) once finished."""
    args = {"media_id": media_id, "status": status, "rating": rating, "notes": notes, "finished_at": finished_at}
    return tools_media.update_media({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_media(media_type: Optional[str] = None, status: Optional[str] = None) -> str:
    """List media/hobby log entries, optionally filtered by media_type or
    status. Use status="in_progress" to answer 'what am I currently
    reading/watching' type questions."""
    args = {"media_type": media_type, "status": status}
    return tools_media.list_media({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def create_user(name: str, type: Optional[str] = None, notes: Optional[str] = None,
                 home_timezone: Optional[str] = None, date_of_birth: Optional[str] = None,
                 gender: Optional[str] = None) -> str:
    """Create a real individual - either a family member actively using
    Mentor (their own paired conversation) or a known related person who
    doesn't have their own pairing yet (e.g. a young child managed by proxy
    through a parent's account - has_own_pairing defaults to false, set it
    true later via update_user once they're actually paired). type must be
    one of: toddler, preschool, child, teen, adult, senior (defaults to
    adult) - pick whichever fits their actual age/developmental stage, it
    calibrates Mentor's tone with them, and determines which profile category
    apply_wellbeing_template assigns them to by default. notes is free text
    for durable context that should shape how you treat this person - special
    needs, disability, accessibility considerations, allergies, or anything
    else relevant - ask about this naturally rather than assuming none
    exists, and record it here rather than letting it live only in
    conversation history. home_timezone is their normal/base IANA timezone
    (e.g. "Australia/Sydney") if known - separate from recent_timezone,
    which is derived automatically from their phone's real location reports,
    never set by hand. Call this the first time you learn who you're talking
    to in a conversation if list_users doesn't already show them, or when
    you learn about a new related person (e.g. someone's child) worth
    tracking even before they use Mentor themselves - then call
    add_user_relationship to link them. This also creates a real, isolated
    Hermes profile for them automatically (own memory, never shared with
    anyone else's conversation) - if that step fails for some reason the
    result carries hermes_profile_error; the Mentor user still gets created
    either way, just mention the error rather than silently ignoring it.
    date_of_birth (ISO date, e.g. "2014-03-21") and gender (free text - not
    a fixed set, too varied to enumerate) are optional basic profile facts -
    ask rather than assume, same as notes."""
    args = {"name": name, "type": type, "notes": notes, "home_timezone": home_timezone,
            "date_of_birth": date_of_birth, "gender": gender}
    return tools_routines.create_user({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_users() -> str:
    """List all known individuals, including notes (special needs/
    accessibility/etc), budget info, has_own_pairing (true if they use
    Mentor via their own paired conversation; false if they're a known
    person managed by proxy through someone else's, e.g. a young child - a
    candidate for suggesting onboarding once old enough), and their
    home_timezone/recent_timezone (recent_timezone + recent_timezone_at come
    from their phone's last real location report, when known - see SOUL.md's
    "Timezones" section for when to use which), and date_of_birth/gender when
    known. Call this at the start of a
    new conversation if you don't already know who you're talking to - if
    their name isn't here, ask, then call create_user. Also call
    list_user_relationships for someone to see who they're related to, and
    list_user_profiles to see which profile categories they're assigned to."""
    return tools_routines.list_users({})


@mcp.tool()
def update_user(user_id: int, name: Optional[str] = None, type: Optional[str] = None,
                 notes: Optional[str] = None, has_own_pairing: Optional[bool] = None,
                 home_timezone: Optional[str] = None, date_of_birth: Optional[str] = None,
                 gender: Optional[str] = None, preferred_checkin_channel: Optional[str] = None) -> str:
    """Rename a user, change their type (toddler, preschool, child, teen,
    adult, senior), set/update notes (special needs, disability,
    accessibility considerations, or other durable context), set their
    home_timezone (IANA name, e.g. "Australia/Sydney" - their normal/base
    timezone; separate from recent_timezone, which is derived automatically
    from real location reports, never set here), date_of_birth (ISO date)
    or gender (free text), or mark has_own_pairing=true once they actually
    get their own paired conversation (as opposed to being a known person
    managed by proxy so far) - e.g. once you learn a real name instead of a
    placeholder like 'Child', realize "child" should really be "teen",
    learn something worth remembering going forward, or a family admin
    confirms someone just got paired. preferred_checkin_channel
    ('telegram' or 'dashboard') is which channel proactive topic check-ins
    should reach them on - set it if they express a real preference (e.g.
    "message me on Telegram, not the dashboard"); leave unset to keep
    auto-detecting. NOTE: an already-created topic-checkin cron doesn't
    move automatically when this changes - flag to a family admin that
    the cron needs recreating if it matters right away."""
    args = {"user_id": user_id, "name": name, "type": type, "notes": notes,
            "has_own_pairing": has_own_pairing, "home_timezone": home_timezone,
            "date_of_birth": date_of_birth, "gender": gender,
            "preferred_checkin_channel": preferred_checkin_channel}
    return tools_routines.update_user({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def create_routine(user_name: str, name: str, schedule: Optional[str] = None) -> str:
    """Create a recurring routine/habit for a user (e.g. "brush teeth", "go
    for a walk", "bedtime") - distinct from objectives, which are one-time
    goals. schedule defaults to "daily". The user must already exist (call
    create_user first if needed)."""
    args = {"user_name": user_name, "name": name, "schedule": schedule}
    return tools_routines.create_routine({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_routines(user_name: Optional[str] = None, active_only: Optional[bool] = None) -> str:
    """List routines, optionally filtered to one user. active_only defaults
    to true."""
    args = {"user_name": user_name, "active_only": active_only}
    return tools_routines.list_routines({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def complete_routine(routine_id: int, note: Optional[str] = None) -> str:
    """Mark a routine done for today (e.g. the user says "I brushed my teeth"
    or replies "done" to a nudge). Returns the updated streak and badges.
    `new_badge_emoji` is set ONLY when this exact completion just crossed a
    new threshold (3/7/14/30/100 days) - always include that literal emoji
    in your celebration when it's set, don't invent your own - it's the
    same one every time someone hits that milestone, which is what makes
    it feel like a real collectible sticker rather than a random emoji.
    `latest_badge_emoji` is the highest badge already earned (set even on
    an ordinary, non-milestone completion) if you want to reference their
    current sticker level in passing. Celebrate a new badge warmly,
    especially for a child - that's the point of tracking it."""
    args = {"routine_id": routine_id, "note": note}
    return tools_routines.complete_routine({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def apply_wellbeing_template(user_name: str) -> str:
    """Seed a user with the standard wellbeing bundle in one call: common
    daily routines (wake up, brush teeth, vitamins, walk, shower, read,
    sleep) plus periodic health objectives (flu jab, GP check-up, blood
    test, dentist - annual insurance review too, for adult/senior profile
    categories). Instantiates whatever the user's assigned profile
    categories imply (list_user_profiles to see which) - auto-assigns the
    category matching their type first if they have none yet. Idempotent -
    safe to call again, skips anything that already exists by name. Use
    this instead of creating each routine/objective one at a time for a new
    user."""
    return tools_routines.apply_wellbeing_template({"user_name": user_name})


@mcp.tool()
def get_routine(routine_id: int) -> str:
    """Get one routine's current streak, longest streak, total completions,
    and badges earned (streak milestones: 3/7/14/30/100 days)."""
    return tools_routines.get_routine({"routine_id": routine_id})


@mcp.tool()
def create_profile_category(name: str, description: Optional[str] = None) -> str:
    """Create a new profile category (cohort) - a reusable set of goals/
    routines that applies to whoever gets assigned to it, beyond the six
    built-in age-stages (Toddler, Preschool, Child, Teen, Adult, Senior,
    already seeded). E.g. "New Parent" or "Diabetic Care". After creating
    it, use add_profile_template_objective/add_profile_template_routine to
    define what it implies, then assign_user_profile to put someone in it."""
    args = {"name": name, "description": description}
    return tools_profile_categories.create_profile_category({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_profile_categories() -> str:
    """List all profile categories (the six built-in age-stages plus any
    custom ones created)."""
    return tools_profile_categories.list_profile_categories({})


@mcp.tool()
def assign_user_profile(user_name: str, profile_name: str) -> str:
    """Assign a user to a profile category (e.g. Jasmine -> "Child"). A user
    can be assigned to several categories at once (e.g. an "Adult" also
    assigned to a custom "New Parent" category). Doesn't itself create any
    objectives/routines - call apply_wellbeing_template afterward to
    instantiate what the category implies onto this user."""
    return tools_profile_categories.assign_user_profile({"user_name": user_name, "profile_name": profile_name})


@mcp.tool()
def unassign_user_profile(user_name: str, profile_name: str) -> str:
    """Remove a user's assignment to a profile category. Does not delete any
    objectives/routines already instantiated from it - only stops it from
    being considered next time apply_wellbeing_template runs for them."""
    return tools_profile_categories.unassign_user_profile({"user_name": user_name, "profile_name": profile_name})


@mcp.tool()
def list_user_profiles(user_name: str) -> str:
    """List which profile categories a user is currently assigned to."""
    return tools_profile_categories.list_user_profiles({"user_name": user_name})


@mcp.tool()
def add_profile_template_objective(profile_name: str, title: str, domain: str,
                                    follow_up_interval_days: Optional[int] = None,
                                    source_note: Optional[str] = None,
                                    essential: Optional[str] = None) -> str:
    """Add a template objective to a profile category - anyone assigned to
    this category will get this objective the next time
    apply_wellbeing_template runs for them (existing users already
    instantiated from this category won't retroactively get it - call
    apply_wellbeing_template again for them if you want it backfilled).
    domain must be one of: hobby, health, career, finance, relationship,
    media, other. essential must be "essential" or "optional" (default)."""
    args = {"profile_name": profile_name, "title": title, "domain": domain,
            "follow_up_interval_days": follow_up_interval_days, "source_note": source_note,
            "essential": essential}
    return tools_profile_categories.add_profile_template_objective({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def add_profile_template_routine(profile_name: str, name: str, schedule: Optional[str] = None) -> str:
    """Add a template routine to a profile category - same instantiation
    semantics as add_profile_template_objective. schedule defaults to
    "daily"."""
    args = {"profile_name": profile_name, "name": name, "schedule": schedule}
    return tools_profile_categories.add_profile_template_routine({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_profile_templates(profile_name: str) -> str:
    """List everything a profile category currently implies (its template
    routines and template objectives) - what a user assigned to it would get
    from apply_wellbeing_template."""
    return tools_profile_categories.list_profile_templates({"profile_name": profile_name})


@mcp.tool()
def delete_profile_template_objective(template_id: int) -> str:
    """Remove a template objective from a profile category - doesn't affect
    objectives already instantiated for users, only future applications."""
    return tools_profile_categories.delete_profile_template_objective({"template_id": template_id})


@mcp.tool()
def delete_profile_template_routine(template_id: int) -> str:
    """Remove a template routine from a profile category - doesn't affect
    routines already instantiated for users, only future applications."""
    return tools_profile_categories.delete_profile_template_routine({"template_id": template_id})


@mcp.tool()
def approve_pairing_code(requesting_user_name: str, code: str, mentor_user_name: Optional[str] = None,
                          platform: Optional[str] = None) -> str:
    """Approve a pending pairing request (platform defaults to "telegram";
    pass "discord" for a Discord pairing - same mechanism, different
    platform) so a new family member can start using Mentor themselves. Only
    a family-admin user may do this - requesting_user_name must be whoever
    you're actually talking to in THIS conversation (the one you identified
    at the start of the chat), not a name they merely claim on someone
    else's behalf. The code is the one the platform's bot DM'd to the new
    person when they first messaged it - they'll need to tell the family
    admin that code directly. Pass
    mentor_user_name (from list_users - call create_user first if they're
    new) to record which Mentor user this pairing belongs to - this writes a
    real, permanent link between their conversation and their user record,
    so you don't have to re-ask who you're talking to in future sessions
    with them. It also routes their chat to their own isolated Hermes
    profile (real separate memory, not just a separate thread) instead of
    sharing the shared/launch profile's memory with everyone else on this
    platform - the result's profile_route_note tells you a gateway restart
    is needed before that takes effect (not automatic), so mention that to
    the family admin rather than implying it's instant."""
    args = {"requesting_user_name": requesting_user_name, "code": code,
            "mentor_user_name": mentor_user_name, "platform": platform}
    return tools_pairing.approve_pairing_code({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_pending_pairings(platform: Optional[str] = None) -> str:
    """List pending pairing requests (platform defaults to "telegram"; pass
    "discord" for Discord) waiting for approval (names and
    request times only - never the code itself, matching Hermes's own
    design). Useful for a family admin asking "is anyone waiting to join"."""
    args = {"platform": platform}
    return tools_pairing.list_pending_pairings({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def link_conversation(user_name: str, chat_id: str, source: Optional[str] = None) -> str:
    """Manually link a conversation (a platform + chat_id) to a Mentor user -
    for retroactive/manual cases approve_pairing_code doesn't cover, e.g.
    fixing a mis-linked conversation. Most new pairings should go through
    approve_pairing_code's mentor_user_name instead, which does this
    automatically at approval time. source defaults to "telegram". Also
    routes that chat to the user's own isolated Hermes profile, same as
    approve_pairing_code - see its docstring for the gateway-restart caveat."""
    args = {"user_name": user_name, "chat_id": chat_id, "source": source}
    return tools_pairing.link_conversation({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def set_user_budget(user_name: str, available_hours_per_week: Optional[float] = None,
                     available_money_per_month: Optional[float] = None,
                     budget_currency: Optional[str] = None) -> str:
    """Set (or update) how much spare time and money a user realistically
    has available per week/month for discretionary objectives, so
    analyze_objective_load has a ceiling to compare against. Ask the person
    directly rather than guessing a number - and don't assume everyone wants
    to share this; only set it if they volunteer it or you ask and they
    answer."""
    args = {"user_name": user_name, "available_hours_per_week": available_hours_per_week,
            "available_money_per_month": available_money_per_month, "budget_currency": budget_currency}
    return tools_capacity.set_user_budget({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def analyze_objective_load(user_name: str) -> str:
    """Static, numeric breakdown of a user's active objectives: total
    committed time (hours/week) and money (per month, normalized from
    whatever frequency each objective uses), split by essential vs optional
    and listed with priority, compared against the user's budget ceiling
    if set_user_budget has been called for them, plus which active
    objectives are still missing cost data (ask about those before relying on
    the totals). This tool only computes numbers - it does NOT decide what's
    realistic or what to cut. Use the numbers it returns to have that
    conversation yourself: which objectives are essential vs optional, which
    are already over budget, and what the honest tradeoffs are if the person
    is considering taking on something new. Call this whenever someone is
    weighing a new commitment against their existing ones, or asks directly
    whether they have room for something."""
    return tools_capacity.analyze_objective_load({"user_name": user_name})


@mcp.tool()
def set_fact_visibility(user_name: str, fact_id: int, visibility: str) -> str:
    """Mark one of this person's already-saved fact_store facts as
    "shared" (visible to guardians/family admins via
    list_family_shared_context) or "private" (default - not shared). This
    does NOT create or edit fact content - fact_store owns that entirely
    (add/update/remove). fact_id comes from a prior fact_store call's own
    response (add/search/probe/list all return each fact's fact_id) - call
    this right after saving a fact via fact_store when it's genuinely
    meant to be visible family-wide, or later once you learn it should be.
    Every fact starts private by default (no visibility marker at all) -
    only call this to make something shared, not as a routine step after
    every fact_store call. Use real judgment the same as any other privacy
    call in this system: err toward private for anything that reads as a
    personal disclosure rather than a plain fact or preference."""
    return tools_memory.set_fact_visibility({"user_name": user_name, "fact_id": fact_id, "visibility": visibility})


@mcp.tool()
def list_family_shared_context() -> str:
    """Everyone's basic profile facts (name, type, date_of_birth, gender,
    notes, home_timezone) plus any of their real fact_store facts
    explicitly marked shared via set_fact_visibility - the real, narrow
    set of cross-person information that's safe to bring into a
    family-wide conversation. Never includes a private fact, an objective/
    routine someone hasn't marked shared, or anything from Hermes's own
    per-profile USER.md/MEMORY.md files - those stay exactly as isolated
    as everywhere else in this project; a fact_store entry with no
    visibility marker is private by default, not shared. This is the tool
    to use in the Family dashboard chat when discussing family-wide
    concerns, planning, or opportunities across people - not for looking
    up one specific person (use that person's own conversation and
    fact_store there instead)."""
    return tools_memory.list_family_shared_context({})


@mcp.tool()
def list_available_personas(user_name: str) -> str:
    """Every named persona option valid for this person's actual type
    (child/adult/senior each have several distinct options; other types
    only have the "default" age-based calibration) - call this before
    set_persona to get real persona_key values, never guess one. Also
    returns their current_persona_key (null means the default
    calibration, not "no persona system available")."""
    return tools_personas.list_available_personas({"user_name": user_name})


@mcp.tool()
def set_persona(requesting_user_name: str, target_user_name: str, persona_key: Optional[str] = None) -> str:
    """Assign target_user_name a specific named persona (a real character/
    tone, not just a label - see list_available_personas for their real
    options) - takes effect immediately, spliced directly into their own
    SOUL.md. Pass persona_key=null to reset to the generic age-based
    calibration instead of a specific persona. requesting_user_name must
    be whoever you're actually talking to in THIS conversation, never a
    claimed identity (same discipline as approve_pairing_code/location).
    A family admin can set anyone's persona; the person themselves can
    only set their OWN if their type is teen/adult/senior - never for
    toddler/preschool/child, where a guardian decides. Don't call this
    speculatively - only when someone (or their guardian) has actually
    expressed a preference for how Mentor should talk to them."""
    return tools_personas.set_persona({
        "requesting_user_name": requesting_user_name, "target_user_name": target_user_name,
        "persona_key": persona_key,
    })


@mcp.tool()
def list_available_voices(user_name: str) -> str:
    """Every selectable Piper TTS voice (male/female, US/UK English) - call
    this before set_tts_voice to get a real voice_key, never guess one.
    Also returns their current_voice_key (null means "on the default
    voice", not "no voice configured") and default_voice_key."""
    return tools_voice.list_available_voices({"user_name": user_name})


@mcp.tool()
def set_tts_voice(requesting_user_name: str, target_user_name: str, voice_key: Optional[str] = None) -> str:
    """Set target_user_name's Piper TTS voice for voice replies (see
    list_available_voices for real options) - takes effect on their very
    next voice reply, no restart needed. Pass voice_key=null to reset to
    the default voice. requesting_user_name must be whoever you're
    actually talking to in THIS conversation, never a claimed identity.
    Only the person themselves, or a family admin, may call this - no age
    restriction (unlike set_persona), since picking a voice isn't
    personality/content-sensitive."""
    return tools_voice.set_tts_voice({
        "requesting_user_name": requesting_user_name, "target_user_name": target_user_name,
        "voice_key": voice_key,
    })


@mcp.tool()
def add_user_relationship(from_user_name: str, to_user_name: str, relationship_type: str,
                           reciprocal_type: Optional[str] = None) -> str:
    """Record a static, directed relationship edge between two existing
    users (both must already exist - call create_user first for anyone
    missing). relationship_type is free text (e.g. father, mother, daughter,
    son, parent, child, sibling, spouse, guardian, friend) - whatever
    accurately describes from_user_name's relation TO to_user_name. Pass
    reciprocal_type to also record the reverse edge in the same call (e.g.
    relationship_type="parent", reciprocal_type="child") - nothing infers
    the reverse label automatically, so provide it explicitly when known
    rather than leaving the relationship one-directional."""
    args = {"from_user_name": from_user_name, "to_user_name": to_user_name,
            "relationship_type": relationship_type, "reciprocal_type": reciprocal_type}
    return tools_relationships.add_user_relationship({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_user_relationships(user_name: str) -> str:
    """List a user's relationships: outgoing edges (this user's relation
    to others, e.g. "parent" of Jasmine) and incoming edges (others'
    relation to this user, e.g. Jasmine's "child" edge back, if recorded).
    Useful for understanding family structure before suggesting things like
    onboarding a related person, or reasoning about whose wellbeing overlaps
    with whose."""
    return tools_relationships.list_user_relationships({"user_name": user_name})


@mcp.tool()
def delete_user_relationship(relationship_id: int) -> str:
    """Remove one relationship edge - e.g. it was recorded wrong or is no
    longer accurate."""
    return tools_relationships.delete_user_relationship({"relationship_id": relationship_id})


@mcp.tool()
def create_device(user_name: str, name: str, type: str) -> str:
    """Register a physical device belonging to a user (e.g. "Jasmine's phone",
    type "phone") - type is free text (phone, tablet, laptop, watch, ...).
    A device is what an external service (like location tracking) gets
    attached to via attach_device_service - call this first if the user
    doesn't have a device registered yet."""
    args = {"user_name": user_name, "name": name, "type": type}
    return tools_devices.create_device(args)


@mcp.tool()
def list_devices(user_name: Optional[str] = None) -> str:
    """List registered devices, optionally filtered to one user."""
    args = {"user_name": user_name}
    return tools_devices.list_devices({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def attach_device_service(device_id: int, service_type: str, provider: str,
                           external_id: Optional[str] = None) -> str:
    """Attach (or update) one integration on a device - service_type is free
    text describing what kind of thing this is (e.g. "tracking",
    "screentime"), provider names which specific integration implements it
    (e.g. "owntracks"), and external_id is whatever identifier that
    provider's own API needs to address this device. Usually you'll use a
    dedicated tool for a specific integration (e.g. link_owntracks_device)
    rather than this directly - this is the generic building block those are
    built on."""
    args = {"device_id": device_id, "service_type": service_type, "provider": provider, "external_id": external_id}
    return tools_devices.attach_device_service({k: v for k, v in args.items() if v is not None})


@mcp.tool()
def list_device_services(user_name: Optional[str] = None, device_id: Optional[int] = None,
                          service_type: Optional[str] = None) -> str:
    """List integrations attached to devices, optionally filtered by user,
    a specific device, or service_type."""
    args = {"user_name": user_name, "device_id": device_id, "service_type": service_type}
    return tools_devices.list_device_services({k: v for k, v in args.items() if v is not None})


async def due_followups(request):
    # Plain-text, stable, no-LLM signal for hermes cron's --monitor-url mode -
    # unchanged bytes (same due-set) suppresses the agent run; a change (an
    # objective newly due, or resolved) triggers the coaching-checkin skill.
    # Mounted onto the same app as the MCP/SSE routes rather than a separate
    # service, since this container already has the DB access it needs.
    from starlette.responses import PlainTextResponse
    raw = impl.list_due_followups({})
    ids = sorted(o["id"] for o in json.loads(raw))
    return PlainTextResponse(",".join(str(i) for i in ids))


async def due_topic_checkins(request):
    # Same monitor-gate pattern as due_followups, but per-user (?user_name=X)
    # since each real person's topic-checkin cron lives on THEIR OWN Hermes
    # profile and must only ever see THEIR OWN due topics, never anyone
    # else's.
    from starlette.responses import PlainTextResponse
    user_name = request.query_params.get("user_name", "")
    raw = tools_topics.list_due_topic_checkins({"user_name": user_name})
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and "error" in parsed:
        return PlainTextResponse("")
    domains = sorted(d["domain"] for d in parsed)
    return PlainTextResponse(",".join(domains))


async def due_services(request):
    # Same monitor-gate pattern as due_topic_checkins - per-user (?user_name=X),
    # since each real person's services cron lives on THEIR OWN Hermes
    # profile and must only ever see THEIR OWN due services (cron-due or
    # topic-due, list_due_services already combines both).
    from starlette.responses import PlainTextResponse
    user_name = request.query_params.get("user_name", "")
    raw = tools_services.list_due_services({"user_name": user_name})
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and "error" in parsed:
        return PlainTextResponse("")
    ids = sorted(w["service_id"] for w in parsed)
    return PlainTextResponse(",".join(str(i) for i in ids))


async def due_pulse(request):
    # The general per-person pulse monitor: combines due-followups,
    # due-services (cron-due or topic-due), due-topic-checkins (life
    # domains), AND due notification-topic-posts (things this person
    # subscribes to - see tools_notifications.py; unrelated to
    # topic-checkins despite the similar name) into ONE stable signal,
    # per-user (?user_name=X). Pure static DB queries - no LLM involved in
    # the tick itself. When the agent DOES run (because this output
    # changed), it has `objective-followup`/`services`/`topic-checkin`/
    # `topic-notification` skills attached and each independently
    # re-checks its own due-condition and handles whatever's actually due
    # for it - this endpoint only decides WHETHER to run the agent at all,
    # never WHAT to do.
    from starlette.responses import PlainTextResponse
    user_name = request.query_params.get("user_name", "")
    followups = json.loads(impl.list_due_followups({"user_name": user_name}))
    services_due = json.loads(tools_services.list_due_services({"user_name": user_name}))
    checkins = json.loads(tools_topics.list_due_topic_checkins({"user_name": user_name}))
    posts = json.loads(tools_notifications.list_due_topic_posts({"user_name": user_name}))
    if isinstance(services_due, dict) and "error" in services_due:
        services_due = []
    if isinstance(checkins, dict) and "error" in checkins:
        checkins = []
    if isinstance(posts, dict) and "error" in posts:
        posts = []
    f_ids = sorted(o["id"] for o in followups)
    s_ids = sorted(s["service_id"] for s in services_due)
    c_domains = sorted(d["domain"] for d in checkins)
    p_ids = sorted(p["topic_post_id"] for p in posts)
    signal = (
        f"F:{','.join(str(i) for i in f_ids)}"
        f"|S:{','.join(str(i) for i in s_ids)}"
        f"|T:{','.join(c_domains)}"
        f"|N:{','.join(str(i) for i in p_ids)}"
    )
    return PlainTextResponse(signal)


if __name__ == "__main__":
    import uvicorn
    from starlette.routing import Route

    # streamable-http, not sse: Hermes's MCP client POSTs directly to the given
    # --url (single endpoint, newer MCP transport revision) rather than following
    # the classic two-endpoint SSE handshake (GET /sse -> session-specific POST
    # /messages/ URL) - confirmed live (POST /sse got 405 against the sse_app()).
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/due-followups", due_followups))
    app.router.routes.append(Route("/due-topic-checkins", due_topic_checkins))
    app.router.routes.append(Route("/due-services", due_services))
    app.router.routes.append(Route("/due-pulse", due_pulse))
    uvicorn.run(app, host="0.0.0.0", port=8000)
