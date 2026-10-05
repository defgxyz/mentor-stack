"""
Seed data for the profile-category system (mentor_data/db.py's
profile_template_objectives/profile_template_routines). This is the single
source of truth for what the six built-in age-stage profile categories imply
- read by db.py's one-time migration (for an existing mentor.db) and its
fresh-install seeding path, so a new deployment and a migrated one end up
with identical seeded templates. Content mirrors what was actually set up
for the Child/Adult users by hand first - this is that set, made reusable
and DB-editable per category from here on.
"""

# users.type values, in the same order profile categories get seeded -
# CATEGORY_TYPES[i] is the users.type CHECK-constraint value that maps to
# CATEGORY_DISPLAY_NAMES[i]'s profile category (e.g. "adult" -> "Adult").
CATEGORY_TYPES = ["toddler", "preschool", "child", "teen", "adult", "senior"]
CATEGORY_DISPLAY_NAMES = {
    "toddler": "Toddler", "preschool": "Preschool", "child": "Child",
    "teen": "Teen", "adult": "Adult", "senior": "Senior",
}

COMMON_ROUTINES = [
    {"name": "Wake up on time", "schedule": "morning"},
    {"name": "Brush teeth", "schedule": "daily"},
    {"name": "Take vitamins", "schedule": "daily"},
    {"name": "Go for a walk", "schedule": "4pm or 9am"},
    {"name": "Shower", "schedule": "8pm"},
    {"name": "Read a book", "schedule": "evening wind-down instead of screens, around 8pm"},
    {"name": "Sleep on time", "schedule": "bedtime"},
]

# domain is always "health" for these - applies to every profile type.
COMMON_HEALTH_OBJECTIVES = [
    {"title": "Flu jab", "follow_up_interval_days": 365, "source_note": "annual flu vaccination"},
    {"title": "GP check-up", "follow_up_interval_days": 365, "source_note": "annual general check-up"},
    {"title": "Blood test", "follow_up_interval_days": 365, "source_note": "annual blood test/markers"},
    {"title": "Dentist", "follow_up_interval_days": 182, "source_note": "twice-yearly dental check-up/cleaning"},
]

# Only applied for these profile types - a toddler/child/teen doesn't manage
# their own insurance; an adult/senior profile does.
ADULT_ONLY_OBJECTIVES = [
    {"title": "Review health insurance", "follow_up_interval_days": 365,
     "source_note": "annual insurance coverage/premium review"},
]
ADULT_ONLY_TYPES = {"adult", "senior"}
