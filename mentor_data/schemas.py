CREATE_OBJECTIVE = {
    "name": "create_objective",
    "description": (
        "Create a new trackable objective when the user mentions a new goal, hobby, "
        "project, or intention they want to pursue over time (e.g. 'I picked up "
        "bouldering', 'I want to save for a house deposit', 'I'm trying to read more'). "
        "ALWAYS call list_objectives first to check it doesn't already exist before "
        "creating a duplicate. If this is a real chunk of a bigger existing objective "
        "that deserves its own independent tracking and follow-up cadence (not just a "
        "checklist step - use add_milestone for those), set parent_objective_id to "
        "link it as a sub-objective."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short human title, e.g. 'Learn bouldering'"},
            "domain": {
                "type": "string",
                "enum": ["hobby", "health", "career", "finance", "relationship", "media", "other"],
            },
            "description": {"type": "string"},
            "target_date": {"type": "string", "description": "ISO date, optional"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5},
            "source_note": {"type": "string", "description": "The user's own words that prompted this"},
            "follow_up_interval_days": {
                "type": "integer",
                "description": (
                    "How often to proactively check in on this, in days. Only set this if the "
                    "user asks for a specific cadence (e.g. 'check in on this weekly') - "
                    "otherwise omit it and a sensible domain-based default is used."
                ),
            },
            "parent_objective_id": {
                "type": "integer",
                "description": "Optional: id of the parent objective this is a sub-objective of.",
            },
        },
        "required": ["title", "domain"],
    },
}

ADD_MILESTONE = {
    "name": "add_milestone",
    "description": "Add a milestone (a concrete step toward completion) to an existing objective.",
    "parameters": {
        "type": "object",
        "properties": {
            "objective_id": {"type": "integer"},
            "title": {"type": "string"},
            "order_index": {"type": "integer"},
            "target_date": {"type": "string", "description": "ISO date, optional"},
        },
        "required": ["objective_id", "title"],
    },
}

LOG_PROGRESS = {
    "name": "log_progress",
    "description": "Record a progress update against an existing objective (or one of its milestones).",
    "parameters": {
        "type": "object",
        "properties": {
            "objective_id": {"type": "integer"},
            "milestone_id": {"type": "integer"},
            "note": {"type": "string"},
            "metric_value": {"type": "number"},
            "metric_unit": {"type": "string"},
            "sentiment": {
                "type": "string",
                "enum": ["on_track", "struggling", "excited", "stalled"],
            },
        },
        "required": ["objective_id"],
    },
}

LIST_OBJECTIVES = {
    "name": "list_objectives",
    "description": (
        "List tracked objectives, optionally filtered by status, domain, or hierarchy "
        "position. Call this before creating a new objective to avoid duplicates, and "
        "whenever the user asks about their goals. Use top_level_only=true for a clean "
        "top-level view when the user just says 'what are my goals' (sub-objectives "
        "will show up nested under their parent via get_objective instead of cluttering "
        "a flat list). Use parent_objective_id to list only the sub-objectives of one "
        "specific objective."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "paused", "completed", "abandoned"],
            },
            "domain": {
                "type": "string",
                "enum": ["hobby", "health", "career", "finance", "relationship", "media", "other"],
            },
            "top_level_only": {
                "type": "boolean",
                "description": "Only return objectives with no parent (excludes sub-objectives).",
            },
            "parent_objective_id": {
                "type": "integer",
                "description": "Only return direct sub-objectives of this objective id.",
            },
        },
    },
}

GET_OBJECTIVE = {
    "name": "get_objective",
    "description": (
        "Get full detail on one objective, including its milestones, progress history, "
        "its parent (if it's a sub-objective of something), and its own direct "
        "sub-objectives (if any). To go deeper than one level of children, call "
        "get_objective again on a specific child's id."
    ),
    "parameters": {
        "type": "object",
        "properties": {"objective_id": {"type": "integer"}},
        "required": ["objective_id"],
    },
}

LIST_DUE_FOLLOWUPS = {
    "name": "list_due_followups",
    "description": (
        "List active objectives whose scheduled follow-up date has arrived. Call this "
        "at the start of a proactive check-in to know exactly which objectives to "
        "mention - don't just recompute staleness yourself from list_objectives."
    ),
    "parameters": {"type": "object", "properties": {}},
}

ACKNOWLEDGE_FOLLOWUP = {
    "name": "acknowledge_followup",
    "description": (
        "Call this for each objective right after you've delivered a proactive "
        "check-in message about it, whether or not the user has replied yet. "
        "Reschedules its next follow-up date forward so it resurfaces again "
        "later instead of staying silently stuck. Do NOT call this for an "
        "objective the user just gave a real progress update on in the same "
        "turn - use log_progress instead, which already reschedules it."
    ),
    "parameters": {
        "type": "object",
        "properties": {"objective_id": {"type": "integer"}},
        "required": ["objective_id"],
    },
}
