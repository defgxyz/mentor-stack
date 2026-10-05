"""
Static capacity analysis: sums the declared time/money cost of a user's
active objectives and compares the totals against that user's optional
budget ceiling. Deliberately does NOT decide what to cut or recommend
anything - that reasoning belongs to the agent (SOUL.md), using these numbers
as ground truth rather than guessing or re-deriving them itself.
"""
import json

from . import db

# Rough month-normalization factors so weekly/yearly costs can be summed with
# monthly ones on one comparable basis. Not exact (weeks-per-month varies),
# good enough for a budgeting conversation, not for accounting.
_MONEY_TO_MONTHLY = {"weekly": 4.345, "monthly": 1.0, "yearly": 1 / 12}


def set_user_budget(args: dict, **kwargs) -> str:
    """Set (or update) how much spare time and money a user realistically
    has available per week/month for discretionary objectives. Optional -
    analyze_objective_load still works without it, just without a ceiling to
    compare against. Ask the person rather than guessing a number."""
    user_name = args["user_name"]
    con = db.connect()
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    if row is None:
        con.close()
        return json.dumps({"error": f"no user named '{user_name}' - call create_user first"})
    user_id = row[0]

    fields = {}
    if args.get("available_hours_per_week") is not None:
        fields["available_hours_per_week"] = args["available_hours_per_week"]
    if args.get("available_money_per_month") is not None:
        fields["available_money_per_month"] = args["available_money_per_month"]
    if args.get("budget_currency") is not None:
        fields["budget_currency"] = args["budget_currency"]
    if not fields:
        con.close()
        return json.dumps({"error": "no budget fields provided"})

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE users SET {set_clause} WHERE id = ?", (*fields.values(), user_id))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "user_name": user_name, "updated": fields})


def analyze_objective_load(args: dict, **kwargs) -> str:
    """Static, numeric-only breakdown of a user's active objectives' declared
    time/money cost - total committed hours/week and $/month, split by
    essential vs optional and by priority, compared against the user's
    budget ceiling if one is set, plus which active objectives are still
    missing cost data. Returns numbers only - YOU do the explaining, tradeoff
    reasoning, and any recommendation about what's realistic to take on,
    using these numbers as ground truth rather than re-deriving them."""
    user_name = args["user_name"]
    con = db.connect()
    user_row = con.execute(
        "SELECT id, available_hours_per_week, available_money_per_month, budget_currency "
        "FROM users WHERE name = ?", (user_name,),
    ).fetchone()
    if user_row is None:
        con.close()
        return json.dumps({"error": f"no user named '{user_name}'"})
    user_id, avail_hours, avail_money, budget_currency = user_row

    rows = con.execute(
        "SELECT id, title, domain, priority, essential, time_cost_hours_per_week, "
        "money_cost_amount, money_cost_currency, money_cost_frequency "
        "FROM objectives WHERE user_id = ? AND status = 'active' "
        "ORDER BY essential ASC, priority DESC",
        (user_id,),
    ).fetchall()
    con.close()

    cols = ["id", "title", "domain", "priority", "essential", "time_cost_hours_per_week",
            "money_cost_amount", "money_cost_currency", "money_cost_frequency"]
    objectives = [dict(zip(cols, r)) for r in rows]

    missing_cost_data = [
        o["title"] for o in objectives
        if o["time_cost_hours_per_week"] is None and o["money_cost_amount"] is None
    ]

    def money_per_month(o):
        if o["money_cost_amount"] is None:
            return 0.0
        freq = o["money_cost_frequency"] or "monthly"
        if freq == "one_off":
            return 0.0  # one-off costs don't recur, excluded from the monthly run-rate
        return o["money_cost_amount"] * _MONEY_TO_MONTHLY.get(freq, 1.0)

    breakdown = {}
    for bucket in ("essential", "optional"):
        bucket_objs = [o for o in objectives if o["essential"] == bucket]
        breakdown[bucket] = {
            "count": len(bucket_objs),
            "time_hours_per_week": round(sum(o["time_cost_hours_per_week"] or 0 for o in bucket_objs), 2),
            "money_per_month": round(sum(money_per_month(o) for o in bucket_objs), 2),
        }

    total_time = round(sum(o["time_cost_hours_per_week"] or 0 for o in objectives), 2)
    total_money = round(sum(money_per_month(o) for o in objectives), 2)
    one_off_money = round(sum(
        o["money_cost_amount"] for o in objectives
        if o["money_cost_amount"] is not None and o["money_cost_frequency"] == "one_off"
    ), 2)

    result = {
        "user_name": user_name,
        "active_objective_count": len(objectives),
        "objectives": objectives,
        "missing_cost_data": missing_cost_data,
        "totals": {
            "time_hours_per_week": total_time,
            "money_per_month": total_money,
            "one_off_money_pending": one_off_money,
        },
        "by_essential": breakdown,
        "budget": {
            "available_hours_per_week": avail_hours,
            "available_money_per_month": avail_money,
            "budget_currency": budget_currency,
        },
    }
    if avail_hours is not None:
        result["time_remaining_hours_per_week"] = round(avail_hours - total_time, 2)
    if avail_money is not None:
        result["money_remaining_per_month"] = round(avail_money - total_money, 2)
    return json.dumps(result)
