"""
Mentor dashboard: a Family view (every user's shared objectives, correctly
attributed so two people's identically-titled objectives don't look like
duplicates) and per-user views (that user's own objectives, including their
private ones, plus a real embedded chat with Hermes itself). A simple
calendar, category filtering, and manual create/archive round it out.
Reuses mentor_data directly (same pattern as mentor-mcp) - no separate
business logic, just new callers of already-tested functions.

Chat is NOT a parallel agent: it proxies to Hermes's own real /api/ws
JSON-RPC gateway (genuine agent turns, same SOUL.md persona, same Mentor
MCP tools, same subscription credential) via the same password-login ->
ws-ticket -> WS-upgrade handshake Hermes's own dashboard SPA uses to reach
/api/pty. /api/ws drives the exact same tui_gateway.dispatch surface the
TUI uses over stdio, but as structured JSON-RPC (session.create/resume,
prompt.submit, message.start/delta/complete, tool.start/complete,
status.update events) instead of raw ANSI/PTY bytes - real chat bubbles in
the browser, not a terminal emulator, while still the one real Hermes
agent. See ~/.claude/plans/vivid-greeting-perlis.md for the full
investigation (docker-exec, the Relay connector protocol, Hermes's admin
console, and /api/pty+xterm.js were all considered/tried and superseded by
this before landing here).

UI: Tailwind (CDN play script) + DaisyUI (CDN precompiled CSS) for real,
componentized styling instead of hand-rolled CSS - cards, badges, navbar,
chat bubbles, form controls all come from DaisyUI's class vocabulary.
Still a plain server-rendered FastAPI app (no build step, no JS
framework) - matches the project's documented frontend-direction decision
(vivid-greeting-perlis.md) to avoid a full SPA rewrite. Dark/light is a
real toggle (data-theme on <html>, persisted in localStorage, defaulting
to the browser's prefers-color-scheme on first visit), not just a media
query - a corner button flips it.
"""

import asyncio
import calendar as calendar_mod
import contextlib
import itertools
import json
import os
import re
from datetime import date, datetime, timezone
from html import escape
from typing import Optional
from urllib.parse import quote
from zoneinfo import available_timezones

import httpx
import websockets
from fastapi import FastAPI, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.websockets import WebSocketState

from mentor_data import db
from mentor_data import tools_objectives
from mentor_data import tools_routines
from mentor_data import tools_owntracks_credentials
from mentor_data import tools_pairing
from mentor_data import tools_memory
from mentor_data import tools_personas
from mentor_data import tools_voice
from mentor_data import tools_notifications
from mentor_data import tools_permissions
from mentor_data import tools_services
from mentor_data.tools_routines import is_family_admin

app = FastAPI()

DOMAIN_COLORS = {
    "hobby": "#8b5cf6", "health": "#22c55e", "career": "#3b82f6", "finance": "#f59e0b",
    "relationship": "#ec4899", "media": "#06b6d4", "other": "#6b7280",
}
DOMAINS = list(DOMAIN_COLORS.keys())
USER_TYPES = ["toddler", "preschool", "child", "teen", "adult", "senior"]
SERVICE_STATUSES = ["active", "paused", "completed", "abandoned"]

# Real IANA zone names for the home_timezone <datalist> - autocomplete so a
# plain typed guess (a country name like "Malaysia" instead of the real
# "Asia/Kuala_Lumpur") is less likely in the first place; create-user itself
# still tolerates a bad value gracefully either way (see admin_create_user).
_TZ_DATALIST_OPTIONS = "".join(
    f'<option value="{tz}">' for tz in sorted(available_timezones()) if "/" in tz
)

# gender is deliberately free text in the schema (too varied to enumerate,
# same reasoning as health_metrics.metric_type) - this datalist is just a
# few common suggestions for autocomplete convenience, not an enforced set.
_GENDER_DATALIST_OPTIONS = "".join(
    f'<option value="{g}">' for g in ["Female", "Male", "Non-binary", "Prefer not to say"]
)

HERMES_DASHBOARD_URL = "http://hermes:9119"
HERMES_DASHBOARD_USERNAME = os.environ.get("HERMES_DASHBOARD_USERNAME", "ev")
HERMES_DASHBOARD_PASSWORD = os.environ.get("HERMES_DASHBOARD_PASSWORD", "")

# Telegram's own /getMe API confirmed this is the real bot both Hermes's
# gateway (real conversational pairing) and mentor-auth (2FA OTP) already
# use - same TELEGRAM_BOT_TOKEN in both. Hardcoded rather than re-resolved
# live on every page load (no new startup/runtime dependency on Telegram's
# API for a value that only changes if the bot itself is ever recreated -
# update this string by hand if that ever happens).
TELEGRAM_BOT_USERNAME = "Mentor8888Bot"

# Discord's bot-invite flow needs the Application (client) ID, resolved once
# via Discord's own GET /oauth2/applications/@me (same one-time-resolve-and-
# hardcode pattern as TELEGRAM_BOT_USERNAME above) - empty until a real
# DISCORD_BOT_TOKEN exists and the application is created in Discord's
# Developer Portal. Permission bits: View Channels(1024) + Send Messages(2048)
# + Add Reactions(64) + Embed Links(16384) + Attach Files(32768) +
# Read Message History(65536) + Use External Emojis(262144) + Connect(1048576)
# + Speak(2097152) = 3525696 - text parity with the Telegram adapter PLUS
# Hermes's own live voice-channel feature (join/leave/speak/listen - real-time
# STT+TTS in an actual Discord voice call, confirmed in the Discord adapter's
# own source - no equivalent exists on Telegram, which only ever exchanges
# voice as file attachments). Without Connect+Speak the bot can still DM/text
# normally, it just can't join a voice channel if asked to.
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_BOT_PERMISSIONS = 3525696


def _discord_invite_url() -> Optional[str]:
    if not DISCORD_CLIENT_ID:
        return None
    return (f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
            f"&permissions={DISCORD_BOT_PERMISSIONS}&scope=bot")

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script>
  // Blocking, runs before first paint - avoids a flash of the wrong theme.
  // No stored choice yet -> follow the OS/browser preference once, then
  // remember whatever the user picks via the corner toggle from then on.
  (function() {{
    var stored = localStorage.getItem("mentor-theme");
    var theme = stored || ((window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  }})();
</script>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://cdn.jsdelivr.net/npm/daisyui@4/dist/full.min.css" rel="stylesheet" type="text/css">
<script src="https://cdn.jsdelivr.net/npm/htmx.org@1.9.12/dist/htmx.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .cal-dot {{ display: block; font-size: 9.5px; border-radius: 4px; padding: 1px 4px; margin-top: 2px;
              color: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .cal-dot.busy {{ background: hsl(var(--bc) / 0.35) !important; font-style: italic; }}
  /* Rendered markdown inside a chat bubble - no explicit colors (inherits
     the bubble's own DaisyUI text color, so it looks right in both user
     and mentor bubbles, light and dark theme, without a Tailwind Typography
     plugin's dark-mode class assumptions clashing with our data-theme
     toggle). */
  .chat-bubble p {{ margin: 0.3em 0; }}
  .chat-bubble p:first-child {{ margin-top: 0; }}
  .chat-bubble p:last-child {{ margin-bottom: 0; }}
  .chat-bubble strong {{ font-weight: 700; }}
  .chat-bubble em {{ font-style: italic; }}
  .chat-bubble ul {{ list-style: disc; padding-left: 1.3em; margin: 0.3em 0; }}
  .chat-bubble ol {{ list-style: decimal; padding-left: 1.3em; margin: 0.3em 0; }}
  .chat-bubble li {{ margin: 0.1em 0; }}
  .chat-bubble h1, .chat-bubble h2, .chat-bubble h3 {{ font-weight: 700; margin: 0.5em 0 0.25em; }}
  .chat-bubble h1 {{ font-size: 1.15em; }}
  .chat-bubble h2 {{ font-size: 1.08em; }}
  .chat-bubble h3 {{ font-size: 1.02em; }}
  .chat-bubble code {{ background: hsl(var(--bc) / 0.12); padding: 0.1em 0.35em; border-radius: 0.25em; font-size: 0.9em; }}
  .chat-bubble pre {{ background: hsl(var(--bc) / 0.1); padding: 0.5em 0.6em; border-radius: 0.375em;
                       overflow-x: auto; margin: 0.3em 0; }}
  .chat-bubble pre code {{ background: none; padding: 0; }}
  .chat-bubble blockquote {{ border-left: 3px solid hsl(var(--bc) / 0.25); padding-left: 0.6em; margin: 0.3em 0; opacity: 0.85; }}
  .chat-bubble a {{ text-decoration: underline; }}
</style>
</head>
<body class="min-h-screen bg-base-200 text-base-content">
  <button type="button" onclick="
      var d=document.documentElement;
      var next=d.getAttribute('data-theme')==='dark'?'light':'dark';
      d.setAttribute('data-theme', next);
      localStorage.setItem('mentor-theme', next);
      document.getElementById('theme-icon').textContent = next==='dark' ? '☀️' : '🌙';
    " class="btn btn-circle btn-sm fixed top-3 right-3 z-50 shadow-md" title="Toggle dark/light">
    <span id="theme-icon">🌙</span>
  </button>
  <script>
    document.getElementById("theme-icon").textContent =
      document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
  </script>
  <div class="max-w-3xl mx-auto px-4 py-8 pb-24">
    {body}
  </div>
</body></html>"""


def _badge(text: str, color: str) -> str:
    return f'<span class="badge badge-sm text-white border-0" style="background:{color}">{escape(text)}</span>'


def _fmt(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).strftime("%-d %b %Y")
    except ValueError:
        return ts


def _domain_color(domain: str) -> str:
    return DOMAIN_COLORS.get(domain, "#6b7280")


def _iso_to_ddmmyyyy(iso_date: Optional[str]) -> str:
    """date_of_birth is stored (and used everywhere else - profile-summary
    facts, list_users, etc.) as ISO (yyyy-mm-dd) - deliberately unchanged,
    since that's the correct sortable/unambiguous internal form. Only the
    UI's display/input format is dd/mm/yyyy (matching this family's real
    locale - Australia/Sydney, Australia/Canberra - explicitly, rather
    than a native <input type="date">, which silently renders per the
    VISITING BROWSER's own OS locale and can't be pinned to one specific
    format from plain HTML)."""
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


def _ddmmyyyy_to_iso(text: str) -> "str | None":
    """None on empty input, the original string back (unchanged) if it
    doesn't match dd/mm/yyyy - update_user's own _valid timezone-style
    validation isn't reused here since this is a pure format conversion,
    not the dashboard's job to silently guess a malformed date into
    something else."""
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return text


def _list_users() -> list[dict]:
    return json.loads(tools_routines.list_users({}))


def _nav_html(active: Optional[str]) -> str:
    def _link(href: str, label: str, key: Optional[str]) -> str:
        cls = "btn btn-sm btn-primary" if active == key else "btn btn-sm btn-ghost"
        return f'<a href="{href}" class="{cls}">{escape(label)}</a>'

    links = [_link("/", "Family", "family")]
    for u in _list_users():
        links.append(_link(f"/user/{escape(u['name'])}", u["name"], u["name"]))
    links.append(_link("/new", "+ New", None))
    right = _link("/admin", "Admin", "admin") if _is_admin_visible() else ""
    return (
        '<div class="navbar bg-base-100 rounded-box shadow-sm mb-4 px-2 py-2 flex flex-wrap gap-1">'
        f'<div class="flex flex-wrap gap-1 flex-1">{"".join(links)}</div>'
        f'<div>{right}</div>'
        "</div>"
    )


def _query(domain: Optional[str], show_archived: bool, month: Optional[str]) -> str:
    """Build a query string carrying forward every filter that currently
    applies - shared by pills and calendar nav so switching one control
    (a domain pill, an archived toggle, a calendar month) never silently
    resets the others, whether via a full reload or an htmx partial swap."""
    parts = []
    if domain:
        parts.append(f"domain={domain}")
    if show_archived:
        parts.append("show_archived=1")
    if month:
        parts.append(f"month={month}")
    return ("?" + "&".join(parts)) if parts else ""


def _hx_attrs(href: str, partial_url: Optional[str], base_path: str) -> str:
    """htmx attributes for a link that should partially swap #browse-section
    instead of a full page reload, when partial_url is given (empty string
    otherwise - the plain href stays as a real fallback either way)."""
    if not partial_url:
        return ""
    hx_href = partial_url + href[len(base_path):]
    return f' hx-get="{hx_href}" hx-target="#browse-section" hx-swap="outerHTML" hx-push-url="{href}"'


def _pills_html(base_path: str, current_domain: Optional[str], show_archived: bool,
                 month: Optional[str] = None, partial_url: Optional[str] = None) -> str:
    """partial_url (e.g. "/partials/family/browse"), when given, makes every
    pill an htmx partial swap of #browse-section (calendar + objectives
    list together - they share the same domain/show_archived-filtered data,
    so they must update in lockstep) instead of a full page reload."""
    def _link(label, domain, color):
        active = domain == current_domain
        href = base_path + _query(domain, show_archived, month)
        cls = "btn btn-xs text-white border-0" if active else "btn btn-xs btn-outline"
        style = f' style="background:{color};border-color:{color}"' if active and color else ""
        return f'<a href="{href}" class="{cls}"{style}{_hx_attrs(href, partial_url, base_path)}>{escape(label)}</a>'

    pills = [_link("All", None, "#2563eb")]
    for d in DOMAINS:
        pills.append(_link(d, d, DOMAIN_COLORS[d]))
    archived_href = base_path + _query(current_domain, not show_archived, month)
    archived_label = "Hide archived" if show_archived else "Show archived"
    pills.append(
        f'<a href="{archived_href}" class="btn btn-xs btn-outline"'
        f'{_hx_attrs(archived_href, partial_url, base_path)}>{escape(archived_label)}</a>'
    )
    return '<div class="flex flex-wrap gap-1.5 mb-4">' + "".join(pills) + "</div>"


def _is_admin_visible() -> bool:
    """Whether the Admin nav link/page should be reachable at all - true as
    soon as at least one family admin exists. Anyone who reaches the
    dashboard can open the page (same shared-login trust boundary as the
    rest of the dashboard), but the "I am" selector only ever lists real
    admins, and the backend still re-checks is_family_admin on submit."""
    return any(u.get("is_family_admin") for u in _list_users())


def _objective_rows(user_name: Optional[str], domain: Optional[str], show_archived: bool) -> list[tuple]:
    con = db.connect()
    q = ("SELECT objectives.id, objectives.title, objectives.domain, objectives.status, objectives.priority, "
         "objectives.next_follow_up_at, objectives.target_date, objectives.visibility, users.name "
         "FROM objectives LEFT JOIN users ON users.id = objectives.user_id "
         "WHERE objectives.parent_objective_id IS NULL")
    params: list = []
    if not show_archived:
        q += " AND objectives.status != 'abandoned'"
    if user_name:
        q += " AND users.name = ?"
        params.append(user_name)
    if domain:
        q += " AND objectives.domain = ?"
        params.append(domain)
    q += " ORDER BY objectives.status = 'active' DESC, objectives.priority DESC, objectives.created_at DESC"
    rows = con.execute(q, params).fetchall()
    con.close()
    return rows


def _card_html(row: tuple, show_owner: bool) -> str:
    oid, title, domain, status, priority, next_fu, target_date, visibility, owner = row
    dim = "" if status == "active" else "opacity-60"
    owner_badge = _badge(owner, "#444") if (show_owner and owner) else ""
    private_badge = _badge("private", "#9333ea") if visibility == "private" else ""
    archive_form = (
        f'<form method="post" action="/objective/{oid}/archive" class="inline" '
        f'onsubmit="return confirm(&quot;Archive this objective? It will be hidden from the default view (not deleted - reversible from Show archived).&quot;);">'
        f'<button class="btn btn-xs btn-outline btn-error ml-1" type="submit">Archive</button></form>'
        if status != "abandoned" else ""
    )
    return f"""
    <div class="card bg-base-100 shadow-sm border border-base-300 mb-3 {dim}">
      <div class="card-body p-4">
        <h2 class="card-title text-base font-semibold"><a href="/objective/{oid}" class="link link-hover">{escape(title)}</a></h2>
        <div class="flex flex-wrap items-center gap-1">
          {_badge(domain, _domain_color(domain))}{_badge(status, "#111" if status == "active" else "#999")}{owner_badge}{private_badge}{archive_form}
        </div>
        <div class="text-xs opacity-60 mt-1">Priority {priority}/5 &middot; Next follow-up: {_fmt(next_fu)}</div>
      </div>
    </div>"""


def _objectives_list_html(rows_for_cards: list[tuple], show_owner: bool) -> str:
    cards = "".join(_card_html(r, show_owner=show_owner) for r in rows_for_cards)
    return cards if cards else '<p class="opacity-60">No objectives tracked yet.</p>'


def _calendar_html(rows: list[tuple], year: int, month: int, base_path: str, redact_private: bool,
                    domain: Optional[str] = None, show_archived: bool = False,
                    partial_url: Optional[str] = None) -> str:
    """rows: objective rows as from _objective_rows. redact_private=True (Family
    view) shows a private objective as an anonymous "Busy" block with no title
    or owner; False (a user's own view) shows everything fully - nothing to
    hide from someone about their own objectives. domain/show_archived are
    carried forward on the month-nav links (not used to filter this
    calendar's own data - that already happened in the rows passed in) so
    switching months never silently drops the current domain/archived
    filter, matching _pills_html's own month-preserving behavior."""
    by_date: dict[str, list[tuple]] = {}
    for oid, title, domain_, status, priority, next_fu, target_date, visibility, owner in rows:
        if status == "abandoned":
            continue
        anchor = target_date or next_fu
        if not anchor:
            continue
        day = anchor[:10]
        by_date.setdefault(day, []).append((title, domain_, visibility, owner))

    cal = calendar_mod.Calendar(firstweekday=0)
    weeks = cal.monthdatescalendar(year, month)
    dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    month_name = calendar_mod.month_name[month]

    cells = "".join(f'<div class="text-[11px] opacity-50 text-center pb-1">{d}</div>' for d in dow)
    for week in weeks:
        for d in week:
            other = " opacity-40" if d.month != month else ""
            iso = d.isoformat()
            dots = ""
            for title, domain_, visibility, owner in by_date.get(iso, [])[:4]:
                if redact_private and visibility == "private":
                    dots += '<span class="cal-dot busy">Busy</span>'
                else:
                    label = escape(title) if not owner else escape(f"{title} ({owner})")
                    dots += f'<span class="cal-dot" style="background:{_domain_color(domain_)}" title="{label}">{escape(title)}</span>'
            cells += (
                f'<div class="border border-base-300 rounded-md min-h-[56px] p-1 text-[11px]{other}">'
                f'<div class="opacity-50 text-[11px]">{d.day}</div>{dots}</div>'
            )

    prev_href = base_path + _query(domain, show_archived, f"{prev_year:04d}-{prev_month:02d}")
    next_href = base_path + _query(domain, show_archived, f"{next_year:04d}-{next_month:02d}")

    return f"""
    <div class="mb-6">
      <div class="flex justify-between items-center mb-2 text-sm">
        <a href="{prev_href}" class="btn btn-xs btn-ghost"{_hx_attrs(prev_href, partial_url, base_path)}>&larr;</a>
        <strong>{month_name} {year}</strong>
        <a href="{next_href}" class="btn btn-xs btn-ghost"{_hx_attrs(next_href, partial_url, base_path)}>&rarr;</a>
      </div>
      <div class="grid grid-cols-7 gap-1">{cells}</div>
    </div>"""


def _browse_section_html(calendar_rows: list[tuple], card_rows: list[tuple], year: int, month: int,
                          base_path: str, domain: Optional[str], show_archived: bool, redact_private: bool,
                          show_owner: bool, partial_url: Optional[str] = None) -> str:
    """Calendar + filter pills + card list, wrapped in one element
    (#browse-section) an htmx partial swap can replace on a filter click or
    a calendar month change - no full page reload, chat/nav above it
    untouched. Calendar and card list share the same domain/show_archived-
    filtered rows, so they always update together, never independently
    (drifting out of sync would be worse than not having partial updates at
    all). Shared by the full-page render (family_view/user_view) and the
    /partials/... fragment routes below - one place decides what this
    section looks like."""
    return (
        '<div id="browse-section">'
        + _calendar_html(calendar_rows, year, month, base_path, redact_private, domain, show_archived, partial_url)
        + _pills_html(base_path, domain, show_archived, f"{year:04d}-{month:02d}", partial_url)
        + _objectives_list_html(card_rows, show_owner)
        + "</div>"
    )


def _parse_month(month_param: Optional[str]) -> tuple[int, int]:
    if month_param:
        try:
            y, m = month_param.split("-")
            return int(y), int(m)
        except (ValueError, AttributeError):
            pass
    today = date.today()
    return today.year, today.month


@app.get("/", response_class=HTMLResponse)
def family_view(domain: Optional[str] = None, show_archived: Optional[int] = 0, month: Optional[str] = None):
    show_arch = bool(show_archived)
    all_rows = _objective_rows(None, domain, show_arch)
    shared_rows = [r for r in all_rows if r[7] == "shared"]
    year, mo = _parse_month(month)

    body = f"""
      <h1 class="text-2xl font-bold mb-1">Mentor</h1>
      <div class="text-sm opacity-60 mb-4">Family view &mdash; shared objectives across everyone. Private ones show only as "Busy" on the calendar.</div>
      {_nav_html("family")}
      {_browse_section_html(all_rows, shared_rows, year, mo, "/", domain, show_arch,
                             redact_private=True, show_owner=True, partial_url="/partials/family/browse")}

      <div class="text-xs uppercase tracking-wide opacity-50 mt-7 mb-2">Family (not any one person's conversation)</div>
      {_chat_and_memory_html("family")}
    """
    return _PAGE.format(title="Mentor", body=body)


@app.get("/partials/family/browse", response_class=HTMLResponse)
def family_browse_partial(domain: Optional[str] = None, show_archived: Optional[int] = 0, month: Optional[str] = None):
    """htmx target for family_view's #browse-section - same query, same
    data, just returns the fragment instead of a full page (see
    _browse_section_html's own docstring for why calendar+pills+cards move
    together as one swap)."""
    show_arch = bool(show_archived)
    all_rows = _objective_rows(None, domain, show_arch)
    shared_rows = [r for r in all_rows if r[7] == "shared"]
    year, mo = _parse_month(month)
    return _browse_section_html(all_rows, shared_rows, year, mo, "/", domain, show_arch,
                                 redact_private=True, show_owner=True, partial_url="/partials/family/browse")


@app.get("/user/{user_name}", response_class=HTMLResponse)
def user_view(user_name: str, domain: Optional[str] = None, show_archived: Optional[int] = 0,
              month: Optional[str] = None, notice: Optional[str] = None):
    all_users = _list_users()
    user_row = next((u for u in all_users if u["name"] == user_name), None)
    if user_row is None:
        return _PAGE.format(title="Not found", body='<p><a href="/" class="link">&larr; Family</a></p><p>No such user.</p>')

    show_arch = bool(show_archived)
    rows = _objective_rows(user_name, domain, show_arch)
    year, mo = _parse_month(month)
    base_path = f"/user/{user_name}"

    notice_html = (
        f'<div class="alert alert-warning text-sm py-2 px-3 mb-4"><span>{escape(notice)}</span></div>'
        if notice else ""
    )
    shares_location = bool(user_row.get("share_location_with_family"))
    location_sharing_html = f"""
      <div class="form-control mb-4">
        <form method="post" action="/user/{escape(user_name)}/location-sharing">
          <label class="label cursor-pointer justify-start gap-3 py-1">
            <input type="checkbox" name="share" value="1" onchange="this.form.submit()"
                   {"checked" if shares_location else ""} class="toggle toggle-primary toggle-sm">
            <span class="label-text text-sm">Share {escape(user_name)}'s location with the rest of the family
              <span class="opacity-50">(off by default - family admins can always see it)</span></span>
          </label>
        </form>
      </div>
    """
    partial_url = f"/partials/user/{user_name}/browse"
    type_options_edit = "".join(
        f'<option value="{t}"{" selected" if t == user_row.get("type") else ""}>{t}</option>' for t in USER_TYPES
    )
    edit_profile_html = f"""
      <details class="collapse collapse-arrow bg-base-100 border border-base-300 mb-4">
        <summary class="collapse-title text-sm font-medium">Edit profile</summary>
        <div class="collapse-content">
          <form method="post" action="/user/{escape(user_name)}/edit-profile" class="space-y-2 pt-1">
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Type</span></label>
              <select name="type" class="select select-bordered select-sm w-full max-w-xs">{type_options_edit}</select>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Date of birth (dd/mm/yyyy)</span></label>
              <input name="date_of_birth" type="text" placeholder="dd/mm/yyyy" pattern="\\d{{2}}/\\d{{2}}/\\d{{4}}"
                     value="{escape(_iso_to_ddmmyyyy(user_row.get("date_of_birth")))}"
                     class="input input-bordered input-sm w-full max-w-xs">
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Gender</span></label>
              <input name="gender" list="gender-options" value="{escape(user_row.get("gender") or "")}"
                     class="input input-bordered input-sm w-full max-w-xs">
              <datalist id="gender-options">{_GENDER_DATALIST_OPTIONS}</datalist>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Home timezone</span></label>
              <input name="home_timezone" list="tz-options" value="{escape(user_row.get("home_timezone") or "")}"
                     class="input input-bordered input-sm w-full max-w-xs">
              <datalist id="tz-options">{_TZ_DATALIST_OPTIONS}</datalist>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Notes</span></label>
              <textarea name="notes" rows="2" class="textarea textarea-bordered textarea-sm w-full">{escape(user_row.get("notes") or "")}</textarea>
            </div>
            <button type="submit" class="btn btn-xs btn-outline mt-1">Save</button>
          </form>
        </div>
      </details>
    """
    persona_section_html = _persona_section_html(user_name, user_row.get("type"))
    voice_section_html = _voice_section_html(user_name)
    body = f"""
      <h1 class="text-2xl font-bold mb-1">{escape(user_name)}</h1>
      <div class="text-sm opacity-60 mb-2">Their objectives, including private ones (only visible here) &mdash; and a real chat with Hermes.</div>
      {notice_html}
      {location_sharing_html}
      {edit_profile_html}
      {persona_section_html}
      {voice_section_html}
      {_nav_html(user_name)}

      <div class="text-xs uppercase tracking-wide opacity-50 mb-2">Objectives</div>
      {_browse_section_html(rows, rows, year, mo, base_path, domain, show_arch,
                             redact_private=False, show_owner=False, partial_url=partial_url)}

      {_chat_and_memory_html(user_name)}
    """
    return _PAGE.format(title=f"Mentor — {user_name}", body=body)


@app.get("/partials/user/{user_name}/browse", response_class=HTMLResponse)
def user_browse_partial(user_name: str, domain: Optional[str] = None, show_archived: Optional[int] = 0,
                         month: Optional[str] = None):
    """htmx target for user_view's #browse-section - same query, same data,
    just returns the fragment instead of a full page."""
    show_arch = bool(show_archived)
    rows = _objective_rows(user_name, domain, show_arch)
    year, mo = _parse_month(month)
    base_path = f"/user/{user_name}"
    return _browse_section_html(rows, rows, year, mo, base_path, domain, show_arch,
                                 redact_private=False, show_owner=False,
                                 partial_url=f"/partials/user/{user_name}/browse")


@app.get("/partials/user/{user_name}/facts", response_class=HTMLResponse)
def user_facts_partial(user_name: str):
    """htmx target for #facts-section - this profile's real fact_store
    memory, read-only (see _read_facts)."""
    return _facts_list_html(user_name)


@app.get("/partials/user/{user_name}/soul", response_class=HTMLResponse)
def user_soul_partial(user_name: str):
    """htmx target for user_view's #soul-section, loaded async (hx-trigger
    load) after the rest of the page - reading SOUL.md over HTTP is a real
    round-trip (see _hermes_fs_read), so this keeps it off the critical
    path of the main page render instead of blocking on it."""
    return _soul_section_html(user_name)


@app.post("/user/{user_name}/soul")
def save_user_soul(user_name: str, content: str = Form("")):
    # Form("") not Form(...): an empty textarea submits an empty value, and
    # FastAPI/python-multipart treats a truly-empty form field as absent
    # rather than present-but-empty for a required field - confirmed live
    # for the old memory-file editor this replaced, same gotcha applies
    # here.
    path = f"{_hermes_profile_dir(user_name)}/SOUL.md"
    error = _hermes_fs_write(path, content)
    notice = f"Failed to save SOUL.md: {error}" if error else "Saved SOUL.md."
    return RedirectResponse(url=f"/user/{user_name}?notice={quote(notice)}", status_code=303)


@app.post("/user/{user_name}/fact/{fact_id}/visibility")
def set_fact_entry_visibility(user_name: str, fact_id: int, shared: Optional[str] = Form(None)):
    tools_memory.set_fact_visibility({
        "user_name": user_name, "fact_id": fact_id, "visibility": "shared" if shared else "private",
    })
    return RedirectResponse(url=f"/user/{user_name}", status_code=303)


@app.post("/user/{user_name}/edit-profile")
def edit_profile(user_name: str, type: str = Form(...), date_of_birth: str = Form(""),
                  gender: str = Form(""), home_timezone: str = Form(""), notes: str = Form("")):
    con = db.connect()
    row = con.execute("SELECT id FROM users WHERE name = ?", (user_name,)).fetchone()
    con.close()
    if row is None:
        return RedirectResponse(url="/", status_code=303)
    dob_text = date_of_birth.strip()
    if dob_text and not re.fullmatch(r"\d{2}/\d{2}/\d{4}", dob_text):
        notice = f"'{dob_text}' isn't a valid date - use dd/mm/yyyy (e.g. 21/03/2014)."
        return RedirectResponse(url=f"/user/{user_name}?notice={quote(notice)}", status_code=303)
    args = {"user_id": row[0], "type": type, "date_of_birth": _ddmmyyyy_to_iso(dob_text) or "",
            "gender": gender.strip(), "notes": notes}
    if home_timezone.strip():
        args["home_timezone"] = home_timezone.strip()
    result = json.loads(tools_routines.update_user(args))
    notice = result["error"] if "error" in result else "Profile updated."
    return RedirectResponse(url=f"/user/{user_name}?notice={quote(notice)}", status_code=303)


_PERSONA_SELF_CHOICE_TYPES = {"teen", "adult", "senior"}


def _persona_section_html(user_name: str, user_type: Optional[str]) -> str:
    """Collapsed by default, same as Edit profile. teen/adult/senior use
    the same self-service trust boundary as location-sharing
    (requesting_user_name == target_user_name == this page's own
    user_name, regardless of who's actually at the keyboard - the
    dashboard has never had real per-viewer login). toddler/preschool/
    child instead need a real family-admin identity, since set_persona
    itself refuses self-choice for those types - same "I am" dropdown
    pattern already used for OwnTracks credential provisioning."""
    info = json.loads(tools_personas.list_available_personas({"user_name": user_name}))
    if "error" in info:
        return ""
    current = info.get("current_persona_key")
    option_html = "".join(
        f'<option value="{escape(o["persona_key"] or "")}"'
        f'{" selected" if o["persona_key"] == current else ""}>{escape(o["name"])}</option>'
        for o in info["options"]
    )
    self_choice = user_type in _PERSONA_SELF_CHOICE_TYPES
    if self_choice:
        admin_field_html = ""
    else:
        admins = [u for u in _list_users() if u.get("is_family_admin")]
        admin_option_html = "".join(f'<option value="{escape(a["name"])}">{escape(a["name"])}</option>' for a in admins)
        admin_field_html = f"""
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Set as (family admin)</span></label>
              <select name="acting_admin" class="select select-bordered select-sm w-full max-w-xs" required>
                <option value="" disabled selected>Choose an admin</option>
                {admin_option_html}
              </select>
            </div>
        """
    return f"""
      <details class="collapse collapse-arrow bg-base-100 border border-base-300 mb-4">
        <summary class="collapse-title text-sm font-medium">Personality</summary>
        <div class="collapse-content">
          <form method="post" action="/user/{escape(user_name)}/persona" class="space-y-2 pt-1">
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Persona</span></label>
              <select name="persona_key" class="select select-bordered select-sm w-full max-w-xs">{option_html}</select>
            </div>
            {admin_field_html}
            <button type="submit" class="btn btn-xs btn-outline mt-1">Save</button>
          </form>
        </div>
      </details>
    """


@app.post("/user/{user_name}/persona")
def set_persona_route(user_name: str, persona_key: str = Form(""), acting_admin: str = Form("")):
    row = next((u for u in _list_users() if u["name"] == user_name), None)
    if row is None:
        return RedirectResponse(url="/", status_code=303)
    self_choice = row.get("type") in _PERSONA_SELF_CHOICE_TYPES
    requesting_user_name = user_name if self_choice else acting_admin
    if not requesting_user_name:
        return RedirectResponse(
            url=f"/user/{user_name}?notice={quote('Choose which family admin is setting this.')}", status_code=303,
        )
    result = json.loads(tools_personas.set_persona({
        "requesting_user_name": requesting_user_name, "target_user_name": user_name,
        "persona_key": persona_key or None,
    }))
    notice = result["error"] if "error" in result else f"Personality set to {result['persona_name']}."
    return RedirectResponse(url=f"/user/{user_name}?notice={quote(notice)}", status_code=303)


def _voice_section_html(user_name: str) -> str:
    """Collapsed by default, same as Personality/Edit profile. Always
    self-service (requesting_user_name == target_user_name) - unlike
    Personality, set_tts_voice has no age gate, so there's no admin-
    selection fallback needed here."""
    info = json.loads(tools_voice.list_available_voices({"user_name": user_name}))
    if "error" in info:
        return ""
    current = info.get("current_voice_key")
    option_html = "".join(
        f'<option value="{escape(o["voice_key"])}"{" selected" if o["voice_key"] == current else ""}>'
        f'{escape(o["label"])} ({escape(o["gender"])}, {escape(o["locale"])}){" - default" if o["is_default"] else ""}'
        f'</option>'
        for o in info["options"]
    )
    return f"""
      <details class="collapse collapse-arrow bg-base-100 border border-base-300 mb-4">
        <summary class="collapse-title text-sm font-medium">Voice</summary>
        <div class="collapse-content">
          <form method="post" action="/user/{escape(user_name)}/voice" class="space-y-2 pt-1">
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Voice reply voice</span></label>
              <select name="voice_key" class="select select-bordered select-sm w-full max-w-xs">{option_html}</select>
            </div>
            <button type="submit" class="btn btn-xs btn-outline mt-1">Save</button>
          </form>
        </div>
      </details>
    """


@app.post("/user/{user_name}/voice")
def set_tts_voice_route(user_name: str, voice_key: str = Form("")):
    result = json.loads(tools_voice.set_tts_voice({
        "requesting_user_name": user_name, "target_user_name": user_name,
        "voice_key": voice_key or None,
    }))
    notice = result["error"] if "error" in result else f"Voice set to {result['voice_label']}."
    return RedirectResponse(url=f"/user/{user_name}?notice={quote(notice)}", status_code=303)


@app.post("/user/{user_name}/location-sharing")
def set_location_sharing(user_name: str, share: Optional[str] = Form(None)):
    """Self-service toggle on a person's own page - requesting_user_name ==
    target_user_name == this page's own user_name, which set_location_sharing
    always allows regardless of who's actually at the keyboard (same shared-
    login trust boundary already accepted for private objectives and
    credential provisioning elsewhere in this dashboard - not a new gap)."""
    tools_routines.set_location_sharing({
        "requesting_user_name": user_name, "target_user_name": user_name, "share": bool(share),
    })
    return RedirectResponse(url=f"/user/{user_name}", status_code=303)


@app.get("/objective/{objective_id}", response_class=HTMLResponse)
def objective_detail(objective_id: int):
    con = db.connect()
    obj = con.execute(
        "SELECT objectives.id, objectives.title, objectives.domain, objectives.description, objectives.status, "
        "objectives.priority, objectives.target_date, objectives.created_at, objectives.last_progress_at, "
        "objectives.source_note, objectives.next_follow_up_at, objectives.parent_objective_id, "
        "objectives.visibility, users.name FROM objectives "
        "LEFT JOIN users ON users.id = objectives.user_id WHERE objectives.id = ?", (objective_id,),
    ).fetchone()
    if obj is None:
        con.close()
        return _PAGE.format(title="Not found", body='<p><a href="/" class="link">&larr; back</a></p><p>No such objective.</p>')

    (oid, title, domain, description, status, priority, target_date, created_at,
     last_progress_at, source_note, next_fu, parent_id, visibility, owner) = obj

    parent_html = ""
    if parent_id is not None:
        p = con.execute("SELECT id, title FROM objectives WHERE id = ?", (parent_id,)).fetchone()
        if p:
            parent_html = f'<div class="text-xs opacity-60 mb-2">Sub-objective of <a href="/objective/{p[0]}" class="link">{escape(p[1])}</a></div>'

    milestones = con.execute(
        "SELECT title, status FROM milestones WHERE objective_id = ? ORDER BY order_index", (objective_id,)
    ).fetchall()
    milestone_html = "".join(
        f'<div class="text-sm py-0.5{" line-through opacity-50" if s == "done" else ""}">{"☑" if s == "done" else "☐"} {escape(t)}</div>'
        for t, s in milestones
    )

    progress = con.execute(
        "SELECT note, metric_value, metric_unit, sentiment, logged_at FROM progress_entries "
        "WHERE objective_id = ? ORDER BY logged_at DESC LIMIT 20", (objective_id,)
    ).fetchall()
    progress_html = "".join(
        f'<div class="border-l-2 border-base-300 pl-3 my-2 text-sm"><strong>{_fmt(logged_at)}</strong>'
        + (f" &mdash; {mv}{escape(mu or '')}" if mv is not None else "")
        + (f" ({escape(sent)})" if sent else "")
        + (f"<br>{escape(note)}" if note else "")
        + "</div>"
        for note, mv, mu, sent, logged_at in progress
    )

    children = con.execute(
        "SELECT id, title, domain, status FROM objectives WHERE parent_objective_id = ? "
        "ORDER BY priority DESC, created_at DESC", (objective_id,)
    ).fetchall()
    children_html = "".join(
        f'<div class="card bg-base-100 border border-base-300 p-3 mb-2">'
        f'<a href="/objective/{cid}" class="link link-hover">{escape(ct)}</a> '
        f'{_badge(cd, _domain_color(cd))}{_badge(cs, "#111" if cs == "active" else "#999")}</div>'
        for cid, ct, cd, cs in children
    )

    con.close()

    owner_badge = _badge(owner, "#444") if owner else ""
    private_badge = _badge("private", "#9333ea") if visibility == "private" else ""
    archive_form = (
        f'<form method="post" action="/objective/{oid}/archive" class="inline" '
        f'onsubmit="return confirm(&quot;Archive this objective? It will be hidden from the default view (not deleted - reversible from Show archived).&quot;);">'
        f'<button class="btn btn-xs btn-outline btn-error ml-1" type="submit">Archive</button></form>'
        if status != "abandoned" else ""
    )

    body = f"""
      <a href="/" class="link text-sm inline-block mb-4">&larr; all objectives</a>
      <h1 class="text-2xl font-bold mb-2">{escape(title)}</h1>
      <div class="flex flex-wrap items-center gap-1 mb-2">
        {_badge(domain, _domain_color(domain))}{_badge(status, "#111" if status == "active" else "#999")}{owner_badge}{private_badge}{archive_form}
      </div>
      {parent_html}
      <div class="text-xs opacity-60 mb-3">Priority {priority}/5 &middot; Created {_fmt(created_at)} &middot;
        Target: {_fmt(target_date)} &middot; Next follow-up: {_fmt(next_fu)}</div>
      {f'<p class="mb-2">{escape(description)}</p>' if description else ''}
      {f'<p class="opacity-60 italic mb-2">&ldquo;{escape(source_note)}&rdquo;</p>' if source_note else ''}

      {'<div class="text-xs uppercase tracking-wide opacity-50 mt-5 mb-2">Milestones</div>' + milestone_html if milestones else ''}
      {'<div class="text-xs uppercase tracking-wide opacity-50 mt-5 mb-2">Sub-objectives</div><div class="pl-1">' + children_html + '</div>' if children else ''}
      <div class="text-xs uppercase tracking-wide opacity-50 mt-5 mb-2">Progress</div>
      {progress_html if progress else '<p class="opacity-60">No progress logged yet.</p>'}
    """
    return _PAGE.format(title=title, body=body)


def _admin_context(users: list, admins: list) -> tuple:
    """Shared derived values every admin subscreen needs - the "I am" admin
    picker (hidden field when there's only one real choice, a dropdown
    otherwise - see admin_page's own note on why), plus the two "pick a
    person" dropdowns used across several forms."""
    if len(admins) == 1:
        requesting_field = f'<input type="hidden" name="requesting_user_name" value="{escape(admins[0]["name"])}">'
        requesting_label = f'<div class="text-sm opacity-60">Acting as <strong>{escape(admins[0]["name"])}</strong> (the only family admin)</div>'
    else:
        admin_options = "".join(f'<option value="{escape(u["name"])}">{escape(u["name"])}</option>' for u in admins)
        requesting_field = f'<select name="requesting_user_name" class="select select-bordered select-sm w-full max-w-xs">{admin_options}</select>'
        requesting_label = '<label class="label py-1"><span class="label-text text-sm">I am</span></label>'
    target_options = "".join(f'<option value="{escape(u["name"])}">{escape(u["name"])}</option>' for u in users)
    onboarding_options = "".join(
        f'<option value="{escape(u["name"])}">{escape(u["name"])}{" (already paired)" if u.get("has_own_pairing") else ""}</option>'
        for u in users
    )
    return requesting_label, requesting_field, target_options, onboarding_options


def _admin_subscreen(title: str, body: str) -> str:
    """Common shell for an admin subscreen: back-link + heading, consistent
    across all of them."""
    shell = (
        '<a href="/admin" class="link text-sm inline-block mb-4">&larr; back to Admin</a>\n'
        f'<h1 class="text-2xl font-bold mb-1">{escape(title)}</h1>\n' + body
    )
    return _PAGE.format(title=f"Admin — {title}", body=shell)


def _add_user_card_html(new_user_error: Optional[str]) -> str:
    type_options = "".join(f'<option value="{t}"{" selected" if t == "adult" else ""}>{t}</option>' for t in USER_TYPES)
    new_user_error_html = (
        f'<div class="alert alert-error text-sm py-2 px-3 mt-2"><span>{escape(new_user_error)}</span></div>'
        if new_user_error else ""
    )
    return f"""
      <div class="card bg-base-100 shadow-sm border-2 border-primary mt-4">
        <div class="card-body p-4">
          <h2 class="card-title text-base">Add family member</h2>
          <div class="text-sm opacity-60">Create a new person Mentor knows about - either someone who'll have their own
          paired conversation, or a known related person (e.g. a young child) tracked by proxy for now.</div>
          {new_user_error_html}
          <form method="post" action="/admin/create-user" class="mt-2 space-y-2">
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Name</span></label>
              <input name="name" required class="input input-bordered input-sm w-full max-w-xs">
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Type</span></label>
              <select name="type" class="select select-bordered select-sm w-full max-w-xs">{type_options}</select>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Home timezone (optional - pick from the list, e.g. Australia/Sydney)</span></label>
              <input name="home_timezone" list="tz-options" placeholder="Start typing a city or region..."
                     class="input input-bordered input-sm w-full max-w-xs">
              <datalist id="tz-options">{_TZ_DATALIST_OPTIONS}</datalist>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Date of birth (dd/mm/yyyy, optional)</span></label>
              <input name="date_of_birth" type="text" placeholder="dd/mm/yyyy" pattern="\\d{{2}}/\\d{{2}}/\\d{{4}}"
                     class="input input-bordered input-sm w-full max-w-xs">
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Gender (optional)</span></label>
              <input name="gender" list="gender-options" class="input input-bordered input-sm w-full max-w-xs">
              <datalist id="gender-options">{_GENDER_DATALIST_OPTIONS}</datalist>
            </div>
            <label class="label cursor-pointer justify-start gap-2 py-1">
              <input type="checkbox" name="is_family_admin" value="1" class="checkbox checkbox-sm">
              <span class="label-text text-sm">Family admin (can approve pairings, provision location credentials, see everyone's data)</span>
            </label>
            <button type="submit" class="btn btn-primary btn-sm mt-2">Create</button>
          </form>
        </div>
      </div>
    """


@app.get("/admin", response_class=HTMLResponse)
def admin_page(new_user_error: Optional[str] = None, notice: Optional[str] = None):
    users = _list_users()
    admins = [u for u in users if u.get("is_family_admin")]
    notice_html = (
        f'<div class="alert alert-success text-sm py-2 px-3 mt-2"><span>{escape(notice)}</span></div>'
        if notice else ""
    )
    if not admins:
        body = f"""
          <h1 class="text-2xl font-bold mb-1">Admin</h1>
          {_nav_html("admin")}
          {_add_user_card_html(new_user_error)}
          <p class="opacity-60 mt-4">No family-admin user exists yet - mark one via update_user
          (is_family_admin=true) from chat, or check "Family admin" above when adding one here.</p>
        """
        return _PAGE.format(title="Admin", body=body)

    nav_cards = """
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
        <a href="/admin/users" class="card bg-base-100 shadow-sm border-2 border-primary hover:shadow-md transition">
          <div class="card-body p-4"><h2 class="card-title text-base">Family members</h2>
          <div class="text-sm opacity-60">Add a new person Mentor knows about.</div></div>
        </a>
        <a href="/admin/telegram" class="card bg-base-100 shadow-sm border-2 border-info hover:shadow-md transition">
          <div class="card-body p-4"><h2 class="card-title text-base">Telegram onboarding</h2>
          <div class="text-sm opacity-60">Get someone paired, approve pending codes.</div></div>
        </a>
        <a href="/admin/discord" class="card bg-base-100 shadow-sm border-2 border-info hover:shadow-md transition">
          <div class="card-body p-4"><h2 class="card-title text-base">Discord onboarding</h2>
          <div class="text-sm opacity-60">Get someone paired, approve pending codes.</div></div>
        </a>
        <a href="/admin/location" class="card bg-base-100 shadow-sm border-2 border-secondary hover:shadow-md transition">
          <div class="card-body p-4"><h2 class="card-title text-base">Location credentials</h2>
          <div class="text-sm opacity-60">Provision or refresh an OwnTracks device credential.</div></div>
        </a>
        <a href="/admin/services" class="card bg-base-100 shadow-sm border-2 border-warning hover:shadow-md transition">
          <div class="card-body p-4"><h2 class="card-title text-base">Services</h2>
          <div class="text-sm opacity-60">See and manage everyone's standing recurring checks.</div></div>
        </a>
        <a href="/admin/notifications" class="card bg-base-100 shadow-sm border-2 border-accent hover:shadow-md transition sm:col-span-2">
          <div class="card-body p-4"><h2 class="card-title text-base">Notifications &amp; permissions</h2>
          <div class="text-sm opacity-60">Topics, subscriptions, and who's allowed to create them.</div></div>
        </a>
      </div>
    """
    body = f"""
      <h1 class="text-2xl font-bold mb-1">Admin</h1>
      {_nav_html("admin")}
      {notice_html}
      {nav_cards}
    """
    return _PAGE.format(title="Admin", body=body)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(new_user_error: Optional[str] = None):
    return _admin_subscreen("Family members", _add_user_card_html(new_user_error))


@app.get("/admin/telegram", response_class=HTMLResponse)
def admin_telegram_page(notice: Optional[str] = None, new_user_error: Optional[str] = None):
    users = _list_users()
    admins = [u for u in users if u.get("is_family_admin")]
    requesting_label, requesting_field, target_options, onboarding_options = _admin_context(users, admins)
    notice_html = (
        f'<div class="alert alert-success text-sm py-2 px-3 mb-2"><span>{escape(notice)}</span></div>' if notice else ""
    )
    error_html = (
        f'<div class="alert alert-error text-sm py-2 px-3 mb-2"><span>{escape(new_user_error)}</span></div>'
        if new_user_error else ""
    )
    pending = json.loads(tools_pairing.list_pending_pairings({"platform": "telegram"}))
    if pending:
        pending_html = '<ul class="text-sm mt-2 space-y-1">' + "".join(
            f'<li>&bull; {escape(p.get("user_name") or "(unknown)")} &mdash; requested '
            f'{datetime.fromtimestamp(p["requested_at"], tz=timezone.utc).strftime("%-d %b %H:%M UTC") if p.get("requested_at") else "recently"}</li>'
            for p in pending
        ) + "</ul>"
    else:
        pending_html = '<p class="text-sm opacity-50 mt-2">No pending requests right now - ask them to message the bot first.</p>'
    body = f"""
      {notice_html}{error_html}
      <div class="card bg-base-100 shadow-sm border-2 border-info">
        <div class="card-body p-4">
          <div class="text-sm opacity-60">Generate the link and steps to get a family member talking to Mentor on Telegram for the first time.</div>
          <form method="get" action="/admin/telegram-onboarding" class="mt-2 space-y-2">
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">For</span></label>
              <select name="user_name" class="select select-bordered select-sm w-full max-w-xs">{onboarding_options}</select>
            </div>
            <button type="submit" class="btn btn-info btn-sm mt-2">Generate</button>
          </form>

          <h3 class="font-semibold text-sm mt-5">Pending Telegram pairings</h3>
          <div class="text-xs opacity-60">Someone who's already messaged the bot but isn't approved yet. The
          code itself is never shown here (it's DM'd directly to them) - ask them for it, then approve below.</div>
          {pending_html}
          <form method="post" action="/admin/approve-pairing" class="mt-3 space-y-2">
            <div class="form-control">
              {requesting_label}
              {requesting_field}
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Code (ask them what the bot DM'd)</span></label>
              <input name="code" required class="input input-bordered input-sm w-full max-w-xs uppercase">
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">For</span></label>
              <select name="mentor_user_name" class="select select-bordered select-sm w-full max-w-xs">{target_options}</select>
            </div>
            <button type="submit" class="btn btn-info btn-sm mt-2">Approve</button>
          </form>
        </div>
      </div>
    """
    return _admin_subscreen("Telegram onboarding", body)


@app.get("/admin/discord", response_class=HTMLResponse)
def admin_discord_page(notice: Optional[str] = None, new_user_error: Optional[str] = None):
    users = _list_users()
    admins = [u for u in users if u.get("is_family_admin")]
    requesting_label, requesting_field, target_options, onboarding_options = _admin_context(users, admins)
    notice_html = (
        f'<div class="alert alert-success text-sm py-2 px-3 mb-2"><span>{escape(notice)}</span></div>' if notice else ""
    )
    error_html = (
        f'<div class="alert alert-error text-sm py-2 px-3 mb-2"><span>{escape(new_user_error)}</span></div>'
        if new_user_error else ""
    )
    invite_url = _discord_invite_url()
    if invite_url:
        invite_html = f"""
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">1. Invite the bot to your server (one-time)</div>
            <a href="{invite_url}" target="_blank" class="btn btn-info btn-sm">Add Mentor to a Discord server</a>
            <div class="text-xs opacity-50 mt-1">Only needs doing once - every family member below then talks to that
            same bot inside that server, or DMs it directly once they share a server with it.</div>
          </div>
        """
    else:
        invite_html = """
          <div class="alert alert-warning text-sm py-2 px-3">
            <span>Discord isn't set up yet - needs a real bot created at
            <code>discord.com/developers/applications</code> (Message Content + Server Members
            intents enabled), its token saved as <code>DISCORD_BOT_TOKEN</code> and application id
            as <code>DISCORD_CLIENT_ID</code>, and <code>platforms.discord.enabled: true</code> in
            Hermes's config.yaml, then a gateway restart.</span>
          </div>
        """
    pending = json.loads(tools_pairing.list_pending_pairings({"platform": "discord"}))
    if pending:
        pending_html = '<ul class="text-sm mt-2 space-y-1">' + "".join(
            f'<li>&bull; {escape(p.get("user_name") or "(unknown)")} &mdash; requested '
            f'{datetime.fromtimestamp(p["requested_at"], tz=timezone.utc).strftime("%-d %b %H:%M UTC") if p.get("requested_at") else "recently"}</li>'
            for p in pending
        ) + "</ul>"
    else:
        pending_html = '<p class="text-sm opacity-50 mt-2">No pending requests right now - ask them to message the bot first.</p>'
    body = f"""
      {notice_html}{error_html}
      <div class="card bg-base-100 shadow-sm border-2 border-info">
        <div class="card-body p-4 space-y-3">
          {invite_html}
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">2. Get someone paired</div>
            <div class="text-sm opacity-60">Generate the steps to get a family member talking to Mentor on Discord
            for the first time.</div>
            <form method="get" action="/admin/discord-onboarding" class="mt-2 space-y-2">
              <div class="form-control">
                <label class="label py-1"><span class="label-text text-sm">For</span></label>
                <select name="user_name" class="select select-bordered select-sm w-full max-w-xs">{onboarding_options}</select>
              </div>
              <button type="submit" class="btn btn-info btn-sm mt-2">Generate</button>
            </form>
          </div>

          <h3 class="font-semibold text-sm mt-5">Pending Discord pairings</h3>
          <div class="text-xs opacity-60">Someone who's already messaged the bot but isn't approved yet. The
          code itself is never shown here (it's DM'd directly to them) - ask them for it, then approve below.</div>
          {pending_html}
          <form method="post" action="/admin/approve-pairing" class="mt-3 space-y-2">
            <input type="hidden" name="platform" value="discord">
            <div class="form-control">
              {requesting_label}
              {requesting_field}
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Code (ask them what the bot DM'd)</span></label>
              <input name="code" required class="input input-bordered input-sm w-full max-w-xs uppercase">
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">For</span></label>
              <select name="mentor_user_name" class="select select-bordered select-sm w-full max-w-xs">{target_options}</select>
            </div>
            <button type="submit" class="btn btn-info btn-sm mt-2">Approve</button>
          </form>
        </div>
      </div>
    """
    return _admin_subscreen("Discord onboarding", body)


@app.get("/admin/location", response_class=HTMLResponse)
def admin_location_page():
    users = _list_users()
    admins = [u for u in users if u.get("is_family_admin")]
    requesting_label, requesting_field, target_options, _ = _admin_context(users, admins)
    body = f"""
      <div class="card bg-base-100 shadow-sm border-2 border-secondary">
        <div class="card-body p-4">
          <div class="text-sm opacity-60">Provision or refresh a family member's OwnTracks device credential.</div>
          <form method="post" action="/admin/provision-owntracks" class="mt-2 space-y-2">
            <div class="form-control">
              {requesting_label}
              {requesting_field}
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">For</span></label>
              <select name="target_user_name" class="select select-bordered select-sm w-full max-w-xs">{target_options}</select>
            </div>
            <button type="submit" class="btn btn-secondary btn-sm mt-2">Provision / Refresh</button>
          </form>
        </div>
      </div>
    """
    return _admin_subscreen("Location credentials", body)


@app.get("/admin/services", response_class=HTMLResponse)
def admin_services_page(notice: Optional[str] = None, new_user_error: Optional[str] = None):
    notice_html = (
        f'<div class="alert alert-success text-sm py-2 px-3 mb-2"><span>{escape(notice)}</span></div>' if notice else ""
    )
    error_html = (
        f'<div class="alert alert-error text-sm py-2 px-3 mb-2"><span>{escape(new_user_error)}</span></div>'
        if new_user_error else ""
    )
    return _admin_subscreen("Services", notice_html + error_html + _services_admin_html())


@app.get("/admin/notifications", response_class=HTMLResponse)
def admin_notifications_page(notice: Optional[str] = None, new_user_error: Optional[str] = None):
    users = _list_users()
    admins = [u for u in users if u.get("is_family_admin")]
    requesting_label, requesting_field, target_options, _ = _admin_context(users, admins)
    notice_html = (
        f'<div class="alert alert-success text-sm py-2 px-3 mb-2"><span>{escape(notice)}</span></div>' if notice else ""
    )
    error_html = (
        f'<div class="alert alert-error text-sm py-2 px-3 mb-2"><span>{escape(new_user_error)}</span></div>'
        if new_user_error else ""
    )
    return _admin_subscreen(
        "Notifications & permissions",
        notice_html + error_html
        + _notifications_admin_html(admins, requesting_label, requesting_field, target_options),
    )


def _services_admin_html() -> str:
    """Read + basic status management for every Service, across every
    person - creation itself stays conversational (the whole point of the
    feature is "just ask Mentor"), but there was no way to see what exists
    without querying the DB directly, which only this project's own operator
    could do. Direct SQL (not tools_services.list_services + N lookups) to
    resolve owner/objective/topic names in one query rather than one round
    trip per row."""
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT services.id, users.name, services.title, services.category, "
            "services.status, services.script, services.trigger_type, services.next_check_at, "
            "services.schedule_interval_days, services.schedule_cron, "
            "objectives.title, out_topics.title, trig_topics.title "
            "FROM services "
            "JOIN users ON users.id = services.user_id "
            "LEFT JOIN objectives ON objectives.id = services.objective_id "
            "LEFT JOIN notification_topics AS out_topics ON out_topics.id = services.output_topic_id "
            "LEFT JOIN notification_topics AS trig_topics ON trig_topics.id = services.trigger_topic_id "
            "ORDER BY services.created_at DESC"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        table = '<p class="text-sm opacity-50 mt-2">No Services exist yet - these are created conversationally (ask Mentor to keep an eye on something).</p>'
    else:
        status_badge = {
            "active": "badge-success", "paused": "badge-warning",
            "completed": "badge-info", "abandoned": "badge-ghost",
        }
        trs = []
        for (service_id, owner, title, category, status, script, trigger_type, next_check_at, interval_days,
             cron_expr, obj_title, out_topic_title, trig_topic_title) in rows:
            links = []
            if obj_title:
                links.append(f'objective: {escape(obj_title)}')
            if out_topic_title:
                links.append(f'posts to: {escape(out_topic_title)}')
            if trig_topic_title:
                links.append(f'triggered by: {escape(trig_topic_title)}')
            links_html = ", ".join(links) or '<span class="opacity-40">none</span>'
            actions = "".join(
                f'<form method="post" action="/admin/update-service-status" class="inline"'
                + (' onsubmit="return confirm(&quot;Abandon this service? It stops running (not deleted - reversible by setting it active again).&quot;);"'
                   if s == "abandoned" else "")
                + f'><input type="hidden" name="service_id" value="{service_id}">'
                f'<input type="hidden" name="status" value="{s}">'
                f'<button type="submit" class="btn btn-ghost btn-xs">{s}</button></form>'
                for s in SERVICE_STATUSES if s != status
            )
            if trigger_type == "topic":
                schedule_cell = '<span class="text-xs opacity-40">triggered by topic, not cron</span>'
            else:
                schedule_cell = f"""
                  <form method="post" action="/admin/update-service-schedule" class="join">
                    <input type="hidden" name="service_id" value="{service_id}">
                    <input type="number" name="schedule_interval_days" value="{interval_days}" min="1"
                           title="Interval in days (used when no cron expression is set)"
                           class="input input-bordered input-xs join-item w-14">
                    <input type="text" name="schedule_cron" value="{escape(cron_expr or '')}"
                           placeholder="cron e.g. 0 9 * * 1" title="Optional 5-field cron expression - overrides the day interval when set"
                           class="input input-bordered input-xs join-item w-32">
                    <button type="submit" class="btn btn-xs join-item">Save</button>
                  </form>
                  <div class="text-xs opacity-40 mt-1">next: {escape((next_check_at or '')[:16].replace('T', ' '))}</div>
                """
            trs.append(
                f'<tr><td class="text-sm">{escape(title)}'
                f'{" <span class=\"opacity-40\">(script)</span>" if script else ""}</td>'
                f'<td class="text-sm">{escape(owner)}</td>'
                f'<td class="text-sm">{escape(category or "")}</td>'
                f'<td><span class="badge badge-sm badge-outline">{trigger_type}</span></td>'
                f'<td><span class="badge badge-sm {status_badge.get(status, "")}">{status}</span></td>'
                f'<td>{schedule_cell}</td>'
                f'<td class="text-sm">{links_html}</td>'
                f'<td class="text-xs">{actions}</td></tr>'
            )
        table = (
            '<div class="overflow-x-auto"><table class="table table-sm mt-2">'
            '<thead><tr><th>Title</th><th>Owner</th><th>Category</th><th>Trigger</th><th>Status</th>'
            '<th>Cadence</th><th>Linked to</th><th>Change status</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>'
        )
    return f"""
      <div class="card bg-base-100 shadow-sm border-2 border-warning mt-4">
        <div class="card-body p-4">
          <h2 class="card-title text-base">Services</h2>
          <div class="text-sm opacity-60">Standing recurring jobs (property/news/deals/anything else) across every
          family member - a script, an open-ended agent check, or both, triggered either on a cron schedule or by
          new posts on a notification topic (enabling chaining: one service's output topic feeds another's
          trigger). Created conversationally - this is a read/oversight view plus basic status and cadence
          control, not a create form. Cadence (cron-triggered only): set an interval in days, or a 5-field cron
          expression for a specific day/time (e.g. <code>0 9 * * 1</code> = every Monday 9am, <code>0 9 * * 1,5</code>
          = Monday+Friday 9am) - the cron field takes precedence once set; leave it blank to use the plain interval.</div>
          {table}
        </div>
      </div>
    """


def _notifications_admin_html(admins: list, requesting_label: str, requesting_field: str,
                               target_options: str) -> str:
    """Notification topics/subscriptions + role-permission policy - admin-only
    UI for the mechanism built in tools_notifications.py/tools_permissions.py.
    Creating/subscribing to your own topic only needs the create_subscription
    permission, but this page is already admin-gated end to end (same as
    every other card here), so every action here goes through as the acting
    admin - the real self-service path is conversational (SOUL.md), not this
    page."""
    topics = json.loads(tools_notifications.list_notification_topics({}))
    if topics:
        rows = []
        for t in topics:
            subs = json.loads(tools_notifications.list_topic_subscriptions({"topic_id": t["topic_id"]}))
            sub_names = ", ".join(escape(s["subscriber_user_name"]) for s in subs) or "<span class=\"opacity-40\">none yet</span>"
            rows.append(
                f'<tr><td class="text-sm">{escape(t["title"])}</td>'
                f'<td class="text-sm">{escape(t["subject_user_name"])}</td>'
                f'<td class="text-sm">{escape(t.get("category") or "")}</td>'
                f'<td class="text-sm">{sub_names}</td></tr>'
            )
        topics_table = (
            '<table class="table table-sm mt-2"><thead><tr><th>Topic</th><th>About</th>'
            f'<th>Category</th><th>Subscribers</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
        )
        topic_options = "".join(
            f'<option value="{t["topic_id"]}">{escape(t["title"])} (about {escape(t["subject_user_name"])})</option>'
            for t in topics
        )
    else:
        topics_table = '<p class="text-sm opacity-50 mt-2">No notification topics yet.</p>'
        topic_options = ""

    perms = json.loads(tools_permissions.list_role_permissions({}))
    if perms:
        perm_rows = "".join(
            f'<tr><td class="text-sm">{escape(p["role"])}</td><td class="text-sm">{escape(p["permission"])}</td>'
            f'<td><form method="post" action="/admin/revoke-role-permission" class="inline" '
            f'onsubmit="return confirm(&quot;Revoke {escape(p["permission"])} from {escape(p["role"])}? '
            f'They will no longer be able to do this until it\'s granted again.&quot;);">'
            f'<input type="hidden" name="requesting_user_name" value="{escape(admins[0]["name"])}">'
            f'<input type="hidden" name="role" value="{escape(p["role"])}">'
            f'<input type="hidden" name="permission" value="{escape(p["permission"])}">'
            f'<button type="submit" class="btn btn-ghost btn-xs text-error">Revoke</button></form></td></tr>'
        for p in perms)
        perms_table = (
            '<table class="table table-sm mt-2"><thead><tr><th>Role</th><th>Permission</th><th></th></tr></thead>'
            f'<tbody>{perm_rows}</tbody></table>'
        )
    else:
        perms_table = '<p class="text-sm opacity-50 mt-2">No permissions granted yet.</p>'

    type_options_html = "".join(f'<option value="{t}">{t}</option>' for t in USER_TYPES)
    subscribe_section = (
        f"""
          <h3 class="font-semibold text-sm mt-5">Add a subscription</h3>
          <form method="post" action="/admin/create-topic-subscription" class="mt-2 space-y-2">
            <div class="form-control">{requesting_label}{requesting_field}</div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Topic</span></label>
              <select name="topic_id" class="select select-bordered select-sm w-full max-w-xs">{topic_options}</select>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Subscriber</span></label>
              <select name="subscriber_user_name" class="select select-bordered select-sm w-full max-w-xs">{target_options}</select>
            </div>
            <button type="submit" class="btn btn-accent btn-sm mt-2">Subscribe</button>
          </form>
        """
        if topics else
        '<p class="text-sm opacity-50 mt-4">Create a topic first before adding subscriptions.</p>'
    )

    return f"""
      <div class="card bg-base-100 shadow-sm border-2 border-accent mt-4">
        <div class="card-body p-4">
          <h2 class="card-title text-base">Notification topics</h2>
          <div class="text-sm opacity-60">Who's told about what - decoupled from who it's about. E.g. a location-boundary
          watch "about" a child, posted to a topic a parent subscribes to. Link a service's output_topic_id to one of
          these from chat to actually feed it (or its trigger_topic_id, to chain off it).</div>
          {topics_table}

          <h3 class="font-semibold text-sm mt-5">Create a topic</h3>
          <form method="post" action="/admin/create-notification-topic" class="mt-2 space-y-2">
            <div class="form-control">{requesting_label}{requesting_field}</div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">About (subject)</span></label>
              <select name="subject_user_name" class="select select-bordered select-sm w-full max-w-xs">{target_options}</select>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Title</span></label>
              <input name="title" required class="input input-bordered input-sm w-full max-w-xs">
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Category (optional)</span></label>
              <input name="category" class="input input-bordered input-sm w-full max-w-xs">
            </div>
            <button type="submit" class="btn btn-accent btn-sm mt-2">Create</button>
          </form>

          {subscribe_section}

          <div class="divider"></div>

          <h2 class="card-title text-base">Permissions</h2>
          <div class="text-sm opacity-60">Who's allowed to create their own subscriptions/topics - a real, changeable
          policy, not a fixed rule. Anyone acting on behalf of someone else still needs to be a family admin regardless
          of this table.</div>
          {perms_table}

          <h3 class="font-semibold text-sm mt-5">Grant a permission</h3>
          <form method="post" action="/admin/grant-role-permission" class="mt-2 space-y-2">
            <div class="form-control">{requesting_label}{requesting_field}</div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Role</span></label>
              <select name="role" class="select select-bordered select-sm w-full max-w-xs">{type_options_html}</select>
            </div>
            <div class="form-control">
              <label class="label py-1"><span class="label-text text-sm">Permission</span></label>
              <input name="permission" value="create_subscription" class="input input-bordered input-sm w-full max-w-xs">
            </div>
            <button type="submit" class="btn btn-accent btn-sm mt-2">Grant</button>
          </form>
        </div>
      </div>
    """


@app.get("/admin/telegram-onboarding", response_class=HTMLResponse)
def admin_telegram_onboarding(user_name: str):
    bot_link = f"https://t.me/{TELEGRAM_BOT_USERNAME}"
    body = f"""
      <a href="/admin/telegram" class="link text-sm inline-block mb-4">&larr; back to Telegram onboarding</a>
      <h1 class="text-2xl font-bold mb-2">Telegram onboarding for {escape(user_name)}</h1>
      <div class="card bg-base-100 border border-base-300">
        <div class="card-body p-4 space-y-3">
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">1. Open the bot</div>
            <a href="{bot_link}" target="_blank" class="btn btn-info btn-sm">Open @{TELEGRAM_BOT_USERNAME} on Telegram</a>
            <div class="text-xs opacity-50 mt-1">Or search <code>@{TELEGRAM_BOT_USERNAME}</code> directly in Telegram if the link doesn't open.</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">2. Say hello</div>
            <div class="text-sm">Have {escape(user_name)} press <strong>Start</strong> (or just send any message) - the
            bot will DM back a one-time pairing code.</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">3. Get the code</div>
            <div class="text-sm">{escape(user_name)} tells a family admin that code directly - never share it
            anywhere else, it's how the bot proves it's really them.</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">4. Approve it</div>
            <div class="text-sm">Back on the <a href="/admin/telegram" class="link">Telegram onboarding page</a>, under
            "Pending Telegram pairings" - enter the code, pick {escape(user_name)} as who it's for, and approve.</div>
          </div>
        </div>
      </div>
    """
    return _PAGE.format(title=f"Telegram onboarding — {user_name}", body=body)


@app.get("/admin/discord-onboarding", response_class=HTMLResponse)
def admin_discord_onboarding(user_name: str):
    invite_url = _discord_invite_url()
    invite_reminder = (
        f'<div class="text-sm">Not in a server with {escape(user_name)} yet? '
        f'<a href="{invite_url}" target="_blank" class="link">Invite the bot</a> to one you share first.</div>'
        if invite_url else
        '<div class="text-sm opacity-60">Discord bot setup isn\'t finished yet - see the warning on the '
        '<a href="/admin/discord" class="link">Discord onboarding page</a>.</div>'
    )
    body = f"""
      <a href="/admin/discord" class="link text-sm inline-block mb-4">&larr; back to Discord onboarding</a>
      <h1 class="text-2xl font-bold mb-2">Discord onboarding for {escape(user_name)}</h1>
      <div class="card bg-base-100 border border-base-300">
        <div class="card-body p-4 space-y-3">
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">1. Share a server with the bot</div>
            {invite_reminder}
          </div>
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">2. Say hello</div>
            <div class="text-sm">Have {escape(user_name)} DM the bot directly, or @mention it in a channel of
            that server - the bot will DM back a one-time pairing code.</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">3. Get the code</div>
            <div class="text-sm">{escape(user_name)} tells a family admin that code directly - never share it
            anywhere else, it's how the bot proves it's really them.</div>
          </div>
          <div>
            <div class="text-xs uppercase opacity-50 mb-1">4. Approve it</div>
            <div class="text-sm">Back on the <a href="/admin/discord" class="link">Discord onboarding page</a>, under
            "Pending Discord pairings" - enter the code, pick {escape(user_name)} as who it's for, and approve.</div>
          </div>
        </div>
      </div>
    """
    return _PAGE.format(title=f"Discord onboarding — {user_name}", body=body)


@app.post("/admin/approve-pairing", response_class=HTMLResponse)
def admin_approve_pairing(requesting_user_name: str = Form(...), code: str = Form(...),
                           mentor_user_name: str = Form(...), platform: str = Form("telegram")):
    result = json.loads(tools_pairing.approve_pairing_code({
        "requesting_user_name": requesting_user_name, "code": code,
        "mentor_user_name": mentor_user_name, "platform": platform,
    }))
    admin_path = f"/admin/{platform}"
    if "error" in result:
        return RedirectResponse(url=f"{admin_path}?new_user_error={quote(result['error'])}", status_code=303)
    notices = [f"{mentor_user_name} is now paired on {platform.capitalize()}."]
    for key in ("conversation_link_error", "profile_route_error", "profile_route_note"):
        if result.get(key):
            notices.append(result[key])
    return RedirectResponse(url=f"{admin_path}?notice={quote(' / '.join(notices))}", status_code=303)


@app.post("/admin/update-service-status", response_class=HTMLResponse)
def admin_update_service_status(service_id: int = Form(...), status: str = Form(...)):
    result = json.loads(tools_services.update_service({"service_id": service_id, "status": status}))
    if "error" in result:
        return RedirectResponse(url=f"/admin/services?new_user_error={quote(result['error'])}", status_code=303)
    return RedirectResponse(url=f"/admin/services?notice={quote(f'Service {service_id} set to {status}.')}", status_code=303)


@app.post("/admin/update-service-schedule", response_class=HTMLResponse)
def admin_update_service_schedule(service_id: int = Form(...), schedule_interval_days: int = Form(...),
                                   schedule_cron: str = Form("")):
    """schedule_cron always passed (possibly empty) - update_service's own
    "" -> clear semantics handle both "set a cron" and "clear it back to
    the plain interval" from the same form field."""
    result = json.loads(tools_services.update_service({
        "service_id": service_id, "schedule_interval_days": schedule_interval_days,
        "schedule_cron": schedule_cron,
    }))
    if "error" in result:
        return RedirectResponse(url=f"/admin/services?new_user_error={quote(result['error'])}", status_code=303)
    return RedirectResponse(
        url=f"/admin/services?notice={quote(f'Service {service_id} schedule updated.')}", status_code=303)


@app.post("/admin/create-notification-topic", response_class=HTMLResponse)
def admin_create_notification_topic(requesting_user_name: str = Form(...), subject_user_name: str = Form(...),
                                     title: str = Form(...), category: str = Form("")):
    result = json.loads(tools_notifications.create_notification_topic({
        "requesting_user_name": requesting_user_name, "subject_user_name": subject_user_name,
        "title": title.strip(), "category": category.strip() or None,
    }))
    if "error" in result:
        return RedirectResponse(url=f"/admin/notifications?new_user_error={quote(result['error'])}", status_code=303)
    return RedirectResponse(
        url=f"/admin/notifications?notice={quote(f'Topic {title!r} created (about {subject_user_name}).')}", status_code=303)


@app.post("/admin/create-topic-subscription", response_class=HTMLResponse)
def admin_create_topic_subscription(requesting_user_name: str = Form(...), topic_id: int = Form(...),
                                     subscriber_user_name: str = Form(...)):
    result = json.loads(tools_notifications.create_topic_subscription({
        "requesting_user_name": requesting_user_name, "topic_id": topic_id,
        "subscriber_user_name": subscriber_user_name,
    }))
    if "error" in result:
        return RedirectResponse(url=f"/admin/notifications?new_user_error={quote(result['error'])}", status_code=303)
    return RedirectResponse(
        url=f"/admin/notifications?notice={quote(f'{subscriber_user_name} subscribed.')}", status_code=303)


@app.post("/admin/grant-role-permission", response_class=HTMLResponse)
def admin_grant_role_permission(requesting_user_name: str = Form(...), role: str = Form(...),
                                 permission: str = Form(...)):
    result = json.loads(tools_permissions.grant_role_permission({
        "requesting_user_name": requesting_user_name, "role": role, "permission": permission.strip(),
    }))
    if "error" in result:
        return RedirectResponse(url=f"/admin/notifications?new_user_error={quote(result['error'])}", status_code=303)
    return RedirectResponse(
        url=f"/admin/notifications?notice={quote(f'Granted {permission} to {role}.')}", status_code=303)


@app.post("/admin/revoke-role-permission", response_class=HTMLResponse)
def admin_revoke_role_permission(requesting_user_name: str = Form(...), role: str = Form(...),
                                  permission: str = Form(...)):
    result = json.loads(tools_permissions.revoke_role_permission({
        "requesting_user_name": requesting_user_name, "role": role, "permission": permission,
    }))
    if "error" in result:
        return RedirectResponse(url=f"/admin/notifications?new_user_error={quote(result['error'])}", status_code=303)
    return RedirectResponse(
        url=f"/admin/notifications?notice={quote(f'Revoked {permission} from {role}.')}", status_code=303)


@app.post("/admin/create-user", response_class=HTMLResponse)
def admin_create_user(name: str = Form(...), type: str = Form("adult"), home_timezone: str = Form(""),
                       date_of_birth: str = Form(""), gender: str = Form(""),
                       is_family_admin_flag: str = Form("", alias="is_family_admin")):
    tz = home_timezone.strip()
    dob_text = date_of_birth.strip()
    dob_warning = ""
    args = {"name": name.strip(), "type": type}
    if tz:
        args["home_timezone"] = tz
    if dob_text:
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", dob_text):
            args["date_of_birth"] = _ddmmyyyy_to_iso(dob_text)
        else:
            # Same "never block creation over an optional field" pattern as
            # the timezone warning just below - a typo'd date shouldn't
            # lose the whole submission.
            dob_warning = f'"{dob_text}" isn\'t a valid dd/mm/yyyy date, so it wasn\'t saved. Set it later from their page.'
    if gender.strip():
        args["gender"] = gender.strip()
    result = json.loads(tools_routines.create_user(args))
    tz_warning = ""
    if "error" in result and tz and "IANA timezone" in result["error"]:
        # A bad timezone string (e.g. "Malaysia" instead of "Asia/Kuala_Lumpur")
        # must never block creating the person - retry without it and just
        # flag it, rather than silently losing everything else they typed.
        result = json.loads(tools_routines.create_user({k: v for k, v in args.items() if k != "home_timezone"}))
        if "error" not in result:
            tz_warning = (
                f'Created - but "{tz}" isn\'t a real IANA timezone name (e.g. "Asia/Kuala_Lumpur"), '
                f"so it wasn't saved. Set it later from their page or chat."
            )
    if "error" in result:
        return RedirectResponse(url=f"/admin/users?new_user_error={result['error']}", status_code=303)
    if is_family_admin_flag:
        tools_routines.update_user({"user_id": result["id"], "is_family_admin": True})
    notices = [n for n in (tz_warning, dob_warning, result.get("hermes_profile_error")) if n]
    redirect = f"/user/{result['name']}"
    if notices:
        redirect += f"?notice={quote(' / '.join(notices))}"
    return RedirectResponse(url=redirect, status_code=303)


@app.get("/new", response_class=HTMLResponse)
def new_form(error: Optional[str] = None):
    users = _list_users()
    user_options = "".join(f'<option value="{escape(u["name"])}">{escape(u["name"])}</option>' for u in users)
    domain_options = "".join(f'<option value="{d}">{d}</option>' for d in DOMAINS)
    err_html = f'<div class="alert alert-error text-sm py-2 px-3 mb-3"><span>{escape(error)}</span></div>' if error else ""
    body = f"""
      <a href="/" class="link text-sm inline-block mb-4">&larr; all objectives</a>
      <h1 class="text-2xl font-bold mb-4">New objective</h1>
      {err_html}
      <form method="post" action="/new" class="space-y-3 max-w-sm">
        <div class="form-control">
          <label class="label py-1"><span class="label-text">Title</span></label>
          <input name="title" required class="input input-bordered input-sm w-full">
        </div>
        <div class="form-control">
          <label class="label py-1"><span class="label-text">Domain</span></label>
          <select name="domain" class="select select-bordered select-sm w-full">{domain_options}</select>
        </div>
        <div class="form-control">
          <label class="label py-1"><span class="label-text">For</span></label>
          <select name="user_name" class="select select-bordered select-sm w-full">
            <option value="">(unscoped / family-wide)</option>{user_options}
          </select>
        </div>
        <div class="form-control">
          <label class="label py-1"><span class="label-text">Target date (optional)</span></label>
          <input name="target_date" type="date" class="input input-bordered input-sm w-full">
        </div>
        <div class="form-control">
          <label class="label py-1"><span class="label-text">Priority (1-5)</span></label>
          <input name="priority" type="number" min="1" max="5" value="3" class="input input-bordered input-sm w-full">
        </div>
        <div class="form-control">
          <label class="label py-1"><span class="label-text">Visibility</span></label>
          <select name="visibility" class="select select-bordered select-sm w-full">
            <option value="shared">Shared (visible to the whole family)</option>
            <option value="private">Private (only visible to whoever it's for)</option>
          </select>
        </div>
        <button type="submit" class="btn btn-primary btn-sm mt-2">Create objective</button>
      </form>
    """
    return _PAGE.format(title="New objective", body=body)


@app.post("/new")
def new_submit(title: str = Form(...), domain: str = Form(...), user_name: str = Form(""),
               target_date: str = Form(""), priority: int = Form(3), visibility: str = Form("shared")):
    args = {"title": title, "domain": domain, "priority": priority, "visibility": visibility}
    if user_name:
        args["user_name"] = user_name
    if target_date:
        args["target_date"] = target_date
    result = json.loads(tools_objectives.create_objective(args))
    if "error" in result:
        return RedirectResponse(url=f"/new?error={result['error']}", status_code=303)
    redirect = f"/user/{user_name}" if user_name else "/"
    return RedirectResponse(url=redirect, status_code=303)


@app.post("/objective/{objective_id}/archive")
def archive_objective(objective_id: int):
    tools_objectives.update_objective({"objective_id": objective_id, "status": "abandoned"})
    con = db.connect()
    row = con.execute(
        "SELECT users.name FROM objectives LEFT JOIN users ON users.id = objectives.user_id "
        "WHERE objectives.id = ?", (objective_id,),
    ).fetchone()
    con.close()
    redirect = f"/user/{row[0]}" if row and row[0] else "/"
    return RedirectResponse(url=redirect, status_code=303)


@app.post("/admin/provision-owntracks", response_class=HTMLResponse)
def admin_provision_owntracks(requesting_user_name: str = Form(...), target_user_name: str = Form(...)):
    """Same access rule as the chat MCP tool: requesting_user_name must be a
    family admin. Tied to the page context that submitted the form (see
    _admin_section_html), not a verified per-viewer session - same accepted
    dashboard-auth limitation as private objectives."""
    if not is_family_admin(requesting_user_name):
        return _PAGE.format(title="Not authorized", body=(
            f'<a href="/user/{escape(requesting_user_name)}" class="link text-sm inline-block mb-4">&larr; back</a>'
            f'<p>Only a family admin can provision a location credential.</p>'
        ))
    result = json.loads(tools_owntracks_credentials.provision_credential({"target_user_name": target_user_name}))
    if "error" in result:
        body = (
            f'<a href="/user/{escape(requesting_user_name)}" class="link text-sm inline-block mb-4">&larr; back</a>'
            f'<h1 class="text-2xl font-bold mb-2">Provisioning failed</h1><p class="opacity-60">{escape(result["error"])}</p>'
        )
    else:
        body = f"""
          <a href="/user/{escape(requesting_user_name)}" class="link text-sm inline-block mb-4">&larr; back</a>
          <h1 class="text-2xl font-bold mb-2">Credential for {escape(result['user_name'])}</h1>
          <p class="opacity-60 text-sm mb-3">Shown once - enter these into the OwnTracks app's Connection/Credentials
          screen now. Reloading or leaving this page won't show the password again (provision again
          to get a fresh one if it's lost).</p>
          <div class="card bg-base-100 border border-base-300">
            <div class="card-body p-4 space-y-2">
              <div><div class="text-xs uppercase opacity-50">URL</div><code class="text-sm">{escape(result['url'])}</code></div>
              <div><div class="text-xs uppercase opacity-50">Username</div><code class="text-sm">{escape(result['app_username'])}</code></div>
              <div><div class="text-xs uppercase opacity-50">Password</div><code class="text-sm">{escape(result['app_password'])}</code></div>
            </div>
          </div>
        """
    return _PAGE.format(title="Provision credential", body=body)


def _hermes_profile_dir(user_name: str) -> str:
    """Absolute path (inside the Hermes container) to this Mentor user's own
    isolated profile's root directory - every user (and "family") has their
    own real Hermes profile now (see _hermes_profile_for_user / the "Real
    per-user memory isolation" plan entry), so this is uniform, no special
    cases."""
    return f"/opt/data/profiles/{_hermes_profile_for_user(user_name)}"


# Category values the holographic memory plugin's own fact_store tool
# actually writes (confirmed directly against a live profile's real data,
# not guessed): 'user_pref' vs {'general','project','tool'}. This mirrors
# exactly the USER.md-vs-MEMORY.md split Hermes's own built-in memory used
# before every profile moved to holographic - 'user_pref' is "who this
# person is" (the old USER.md role), everything else is "notes/context"
# (the old MEMORY.md role) - so the same two-way label still applies here,
# just driven by fact_store's category field instead of two separate files.
_USER_FACT_CATEGORY = "user_pref"


def _real_user_exists(user_name: str) -> bool:
    """"family" is a Hermes profile (has its own fact_store) but not a row
    in `users` - the shared/private toggle only makes sense for a real
    person (set_fact_visibility resolves user_name against `users`), so the
    facts panel needs to know which case it's rendering."""
    con = db.connect()
    row = con.execute("SELECT 1 FROM users WHERE name = ?", (user_name,)).fetchone()
    con.close()
    return row is not None


def _facts_list_html(user_name: str) -> str:
    """Display of this profile's real, active memory (fact_store/
    holographic, read via the shared tools_memory.read_holographic_facts -
    same mount/query mentor-mcp uses for list_family_shared_context, one
    implementation not two). Highlights each fact as 'User' (fact_store's
    'user_pref' category - who this person is) vs 'Memory' (everything
    else - notes/context), the same two-way distinction Hermes's own
    USER.md/MEMORY.md files used before this project moved every profile to
    the holographic provider. For a real user (not the "family" pseudo-
    profile), each entry also gets a Shared/Private toggle backed by the
    fact_visibility table - the same mechanism set_fact_visibility uses,
    so a toggle here and an agent's own tool call stay perfectly in sync
    (one piece of state, two ways to change it). Content itself stays
    read-only here: editing it would mean writing directly into the
    HRR-indexed SQLite store, bypassing fact_store's own add_fact() (entity
    linking, vector computation)."""
    facts, err = tools_memory.read_holographic_facts(user_name)
    if err:
        return f'<div id="facts-section"><p class="text-sm text-error">{escape(err)}</p></div>'
    if not facts:
        return '<div id="facts-section"><p class="text-sm opacity-50">No memory saved yet.</p></div>'
    can_toggle = _real_user_exists(user_name)
    shared_ids = set()
    if can_toggle:
        con = db.connect()
        shared_ids = {
            r[0] for r in con.execute(
                "SELECT fact_visibility.fact_id FROM fact_visibility "
                "JOIN users ON users.id = fact_visibility.user_id "
                "WHERE users.name = ? AND fact_visibility.visibility = 'shared'",
                (user_name,),
            ).fetchall()
        }
        con.close()
    rows = []
    for f in facts:
        is_user = f["category"] == _USER_FACT_CATEGORY
        badge_class = "badge-primary" if is_user else "badge-ghost"
        badge_text = "User" if is_user else "Memory"
        sub_cat = (
            f' <span class="badge badge-ghost badge-xs">{escape(f["category"])}</span>'
            if not is_user else ""
        )
        toggle_html = ""
        if can_toggle:
            shared = f["fact_id"] in shared_ids
            toggle_html = f"""
              <form method="post" action="/user/{escape(user_name)}/fact/{f['fact_id']}/visibility">
                <label class="label cursor-pointer gap-1 py-0">
                  <span class="label-text text-xs">Shared</span>
                  <input type="checkbox" name="shared" value="1" onchange="this.form.submit()"
                         {"checked" if shared else ""} class="toggle toggle-xs toggle-primary">
                </label>
              </form>
            """
        rows.append(f"""
          <div class="flex items-start justify-between gap-3 py-2 border-b border-base-300 last:border-0">
            <span class="badge {badge_class} badge-sm shrink-0 mt-0.5">{badge_text}</span>
            <div class="flex-1 text-sm">
              {escape(f["content"])}{sub_cat}
              <div class="text-xs opacity-40 mt-0.5">{_fmt(f["created_at"])}</div>
            </div>
            <div class="shrink-0">{toggle_html}</div>
          </div>
        """)
    return '<div id="facts-section">' + "".join(rows) + "</div>"


def _hermes_fs_read(path: str) -> tuple[str, Optional[str]]:
    """(text, error) - reads a text file from inside Hermes's own container
    via its real dashboard API (GET /api/fs/read-text - the same generic
    file-read/write endpoints Hermes's own in-app spot editor uses), using
    the same password-login session-cookie auth as everywhere else in this
    file. No new filesystem mount needed for this. A missing file (a
    profile that's never had anything written to USER.md/MEMORY.md yet)
    reads as "" , not an error - that's the normal state for a brand-new
    profile, not a problem to report."""
    try:
        with httpx.Client(base_url=HERMES_DASHBOARD_URL, timeout=10) as client:
            login = client.post("/auth/password-login", json={
                "provider": "basic", "username": HERMES_DASHBOARD_USERNAME, "password": HERMES_DASHBOARD_PASSWORD,
            })
            if login.status_code != 200:
                return "", "could not authenticate to Hermes"
            resp = client.get("/api/fs/read-text", params={"path": path})
            if resp.status_code == 404:
                return "", None
            if resp.status_code != 200:
                return "", f"failed to read ({resp.status_code})"
            return resp.json().get("text", ""), None
    except Exception as e:
        return "", f"failed to read: {e}"


def _hermes_fs_write(path: str, content: str) -> Optional[str]:
    """None on success, else an error string. See _hermes_fs_read."""
    try:
        with httpx.Client(base_url=HERMES_DASHBOARD_URL, timeout=10) as client:
            login = client.post("/auth/password-login", json={
                "provider": "basic", "username": HERMES_DASHBOARD_USERNAME, "password": HERMES_DASHBOARD_PASSWORD,
            })
            if login.status_code != 200:
                return "could not authenticate to Hermes"
            resp = client.post("/api/fs/write-text", json={"path": path, "content": content})
            if resp.status_code != 200:
                return f"failed to write ({resp.status_code}): {resp.text[:200]}"
            return None
    except Exception as e:
        return f"failed to write: {e}"


def _soul_section_html(user_name: str) -> str:
    """SOUL.md is this profile's real, active persona/system-prompt file -
    injected into every turn (unlike USER.md/MEMORY.md, which the earlier
    version of this section edited but which went dead the moment every
    profile moved to the holographic memory provider - see the plan's
    "Migrated every profile's private memory..." entry. Editing a file
    nothing reads was a difference-free edit; this replaces it with the one
    Hermes file per profile that's still genuinely live."""
    path = f"{_hermes_profile_dir(user_name)}/SOUL.md"
    text, err = _hermes_fs_read(path)
    err_html = f'<div class="text-xs text-error mt-1">{escape(err)}</div>' if err else ""
    return f"""
      <div id="soul-section">
        <form method="post" action="/user/{escape(user_name)}/soul">
          <textarea name="content" rows="16"
                    class="textarea textarea-bordered w-full text-xs font-mono">{escape(text)}</textarea>
          <button type="submit" class="btn btn-xs btn-outline mt-1">Save</button>
        </form>
        {err_html}
      </div>
    """


def _chat_and_memory_html(target: str) -> str:
    """Chat panel + lazy-loaded Memory section, shared verbatim by
    user_view (target = a real Mentor user_name) and family_view (target =
    "family", the dedicated shared-context profile - see chat_proxy's own
    identity-priming special-case for what makes that one different from a
    real person's chat)."""
    return f"""
      <div id="chat-wrap" class="mt-7">
        <div class="text-xs uppercase tracking-wide opacity-50 mb-2">Chat with Mentor</div>
        <div id="chat-log" class="bg-base-100 border border-base-300 rounded-box p-3 h-[420px] overflow-y-auto flex flex-col gap-1"></div>
        <div class="flex gap-2 mt-2">
          <input id="chat-input" type="text" placeholder="Message Mentor..." autocomplete="off" class="input input-bordered input-sm flex-1">
          <button id="chat-send" class="btn btn-primary btn-sm">Send</button>
        </div>
      </div>

      <div class="mt-7">
        <div class="text-xs uppercase tracking-wide opacity-50 mb-2">
          Memory <span class="opacity-50 normal-case">(fact_store &mdash; <span class="badge badge-primary badge-xs align-middle">User</span> = who they are, <span class="badge badge-ghost badge-xs align-middle">Memory</span> = notes/context; Shared toggle controls visibility to the Family chat, private by default)</span>
        </div>
        <div id="facts-section" hx-get="/partials/user/{escape(target)}/facts" hx-trigger="load" hx-swap="outerHTML">
          <p class="opacity-50 text-sm">Loading&hellip;</p>
        </div>
      </div>

      <div class="mt-7">
        <div class="text-xs uppercase tracking-wide opacity-50 mb-2">SOUL.md (persona)</div>
        <div id="soul-section" hx-get="/partials/user/{escape(target)}/soul" hx-trigger="load" hx-swap="outerHTML">
          <p class="opacity-50 text-sm">Loading&hellip;</p>
        </div>
      </div>

      <script>
        (function() {{
          var log = document.getElementById("chat-log");
          var input = document.getElementById("chat-input");
          var sendBtn = document.getElementById("chat-send");
          var pendingRow = null;   // chat row currently being streamed into
          var pendingText = "";

          function scrollDown() {{ log.scrollTop = log.scrollHeight; }}

          marked.setOptions({{ gfm: true, breaks: true }});
          function renderBubbleText(el, text) {{
            // Real markdown rendering (bold/lists/headers/links/code), not
            // raw **asterisks** shown literally - Mentor's replies are
            // written assuming markdown rendering, same as Telegram/every
            // other client it talks through. marked's GFM mode also
            // autolinks bare URLs, so a plain https://... still becomes
            // clickable without needing our own regex pass. DOMPurify
            // sanitizes the result before it hits innerHTML - this is
            // trusted backend content, not untrusted user input, but
            // sanitizing real HTML output before an innerHTML assignment
            // is cheap insurance, not optional.
            var html = marked.parse(text || "");
            el.innerHTML = DOMPurify.sanitize(html, {{ ADD_ATTR: ["target", "rel"] }});
            el.querySelectorAll("a").forEach(function(a) {{
              a.setAttribute("target", "_blank");
              a.setAttribute("rel", "noopener noreferrer");
            }});
          }}

          function addBubble(role, text) {{
            var row = document.createElement("div");
            row.className = "chat " + (role === "user" ? "chat-end" : "chat-start");
            var b = document.createElement("div");
            b.className = "chat-bubble" + (role === "user" ? " chat-bubble-primary" : "");
            renderBubbleText(b, text);
            row.appendChild(b);
            log.appendChild(row);
            scrollDown();
            return row;
          }}

          function addLine(kind, text) {{
            var row = document.createElement("div");
            row.className = "text-center text-xs opacity-50 py-1" + (kind === "tool" ? " italic" : "");
            row.textContent = text;
            log.appendChild(row);
            scrollDown();
            return row;
          }}

          var proto = location.protocol === "https:" ? "wss:" : "ws:";
          var ws = new WebSocket(proto + "//" + location.host + "/ws/chat/{escape(target)}");

          ws.onopen = function() {{ addLine("status", "Connected."); }};
          ws.onclose = function() {{ addLine("status", "Disconnected."); sendBtn.disabled = true; }};
          ws.onerror = function() {{ addLine("status", "Connection error."); }};

          ws.onmessage = function(ev) {{
            var msg;
            try {{ msg = JSON.parse(ev.data); }} catch (e) {{ return; }}
            if (msg.type === "history") {{
              (msg.messages || []).forEach(function(m) {{ addBubble(m.role, m.text); }});
            }} else if (msg.type === "message_start") {{
              pendingText = "";
              pendingRow = addBubble("assistant", "...");
            }} else if (msg.type === "message_delta") {{
              pendingText += msg.text || "";
              if (pendingRow) {{ renderBubbleText(pendingRow.querySelector(".chat-bubble"), pendingText); scrollDown(); }}
            }} else if (msg.type === "message_complete") {{
              var finalText = msg.text || pendingText;
              if (pendingRow) {{
                renderBubbleText(pendingRow.querySelector(".chat-bubble"), finalText);
              }} else if (finalText) {{
                addBubble("assistant", finalText);
              }}
              pendingRow = null;
              scrollDown();
            }} else if (msg.type === "tool_start") {{
              addLine("tool", "using " + (msg.name || "a tool") + "...");
            }} else if (msg.type === "status") {{
              addLine("status", msg.text || "");
            }} else if (msg.type === "error") {{
              addLine("status", "Error: " + (msg.text || "unknown"));
            }}
          }};

          function send() {{
            var text = input.value.trim();
            if (!text || ws.readyState !== WebSocket.OPEN) return;
            addBubble("user", text);
            ws.send(JSON.stringify({{ text: text }}));
            input.value = "";
          }}
          sendBtn.onclick = send;
          input.addEventListener("keydown", function(ev) {{
            if (ev.key === "Enter") send();
          }});
        }})();
      </script>
    """


async def _mint_ws_ticket() -> Optional[str]:
    """Real password-login -> ws-ticket handshake against Hermes's own
    dashboard, using the credentials in HERMES_DASHBOARD_USERNAME/PASSWORD.
    Returns None if Hermes rejects the login or the ticket mint fails."""
    async with httpx.AsyncClient(base_url=HERMES_DASHBOARD_URL, timeout=10) as client:
        login = await client.post("/auth/password-login", json={
            "provider": "basic", "username": HERMES_DASHBOARD_USERNAME, "password": HERMES_DASHBOARD_PASSWORD,
        })
        if login.status_code != 200:
            return None
        cookies = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        ticket_resp = await client.post("/api/auth/ws-ticket", headers={"Cookie": cookies})
        if ticket_resp.status_code != 200:
            return None
        return ticket_resp.json().get("ticket")


def _filter_priming_ack(messages: list[dict]) -> list[dict]:
    """Drop the identity-priming turn from replayed history. prompt.submit's
    display_kind="hidden" only hides the USER row we sent (confirmed:
    session_history.py filters a message when ITS OWN display_kind is
    hidden) - the agent's resulting assistant acknowledgement carries no
    such flag and would otherwise resurface as a stray "Understood, I've
    got X set..." bubble on every reconnect. Real conversation always
    alternates user->assistant once filtered to just those two roles, so an
    assistant message with no immediately-preceding kept user message is
    structurally exactly the priming ack (and only that) - drop it on that
    basis rather than string-matching its text."""
    kept: list[dict] = []
    last_role = None
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant") or not (m.get("text") or "").strip():
            continue
        if role == "assistant" and last_role != "user":
            continue
        kept.append({"role": role, "text": m["text"]})
        last_role = role
    return kept


# Every family member - Evgeniy included - gets their own real, dedicated
# Hermes profile (isolated memory/SOUL.md/sessions, not just a separate
# conversation thread). Evgeniy originally stayed on the launch/default
# profile as a special case (his real Telegram pairing lived there first),
# but that asymmetry directly caused a real bug (see below) and meant the
# "default" profile was implicitly "his" - now "default" is a genuinely
# neutral, unclaimed profile nothing routes to on purpose. See the "real
# per-user isolation" retrospective in vivid-greeting-perlis.md.
#
# Provisioned once via profiles.create (clone_from="default", clone_all=True
# for Evgeniy specifically to carry over his real memory/config - note
# clone_all still does NOT copy sessions/, a deliberate Hermes exclusion,
# so his dashboard chat history did not carry over, only memory/persona
# did); new users get this automatically now via create_user's own
# _provision_hermes_profile call.
#
# Historical bug, fixed: this used to special-case Evgeniy to the literal
# string "current", which is NOT a magic "launch profile" keyword for the
# JSON-RPC `profile` param on session.list/resume/create (unlike the
# /api/pty query string, which has its own separate resolution) - Hermes
# looked for a real profile literally named "current" (none existed) and
# every call failed. Fixed by giving him a real profile like everyone else
# instead of trying to special-case "no profile" again.
def _hermes_profile_for_user(user_name: str) -> str:
    return user_name.strip().lower()


@app.websocket("/ws/chat/{user_name}")
async def chat_proxy(ws: WebSocket, user_name: str):
    """Proxies the browser to Hermes's real /api/ws JSON-RPC gateway - NOT a
    parallel agent, and NOT raw PTY/ANSI bytes either: /api/ws drives the
    exact same tui_gateway.dispatch surface the TUI uses over stdio, just as
    structured JSON-RPC (session.create/resume, prompt.submit, and
    message.start/delta/complete + tool.*/status.update events), so the
    browser can render real chat bubbles instead of an embedded terminal.
    Each Mentor user_name gets its own persistent Hermes session AND its own
    isolated Hermes profile (see _hermes_profile_for_user) - looked up by a
    stable title, "mentor-dashboard-{user_name}", within that profile's own
    session store - distinct from (but the same real agent as) their
    Telegram conversation, and survives a page reload with full history via
    session.resume."""
    await ws.accept()

    ticket = await _mint_ws_ticket()
    if not ticket:
        await ws.send_json({"type": "error", "text": "Chat unavailable: could not authenticate to Hermes."})
        await ws.close(code=1011)
        return

    profile = _hermes_profile_for_user(user_name)
    hermes_ws_host = HERMES_DASHBOARD_URL.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{hermes_ws_host}/api/ws?ticket={ticket}&profile={profile}"
    title = f"mentor-dashboard-{user_name}"

    try:
        async with websockets.connect(uri) as upstream:
            _ids = itertools.count(1)
            pending: dict[int, asyncio.Future] = {}
            state = {"priming_inflight": False}

            async def rpc(method_name: str, params: dict) -> dict:
                rid = next(_ids)
                fut = asyncio.get_running_loop().create_future()
                pending[rid] = fut
                await upstream.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method_name, "params": params}))
                return await fut

            async def hermes_reader():
                async for raw in upstream:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    mid = msg.get("id")
                    if mid is not None and mid in pending:
                        pending.pop(mid).set_result(msg)
                        continue
                    if msg.get("method") != "event":
                        continue
                    params = msg.get("params") or {}
                    etype = params.get("type")
                    payload = params.get("payload") or {}
                    if state["priming_inflight"]:
                        # Identity priming's own reply is not real conversation -
                        # swallow its stream so it never paints as a bubble.
                        if etype == "message.complete":
                            state["priming_inflight"] = False
                        continue
                    if etype == "message.start":
                        await ws.send_json({"type": "message_start"})
                    elif etype == "message.delta":
                        await ws.send_json({"type": "message_delta", "text": payload.get("text", "")})
                    elif etype == "message.complete":
                        await ws.send_json({"type": "message_complete", "text": payload.get("text", "")})
                    elif etype == "tool.start":
                        await ws.send_json({"type": "tool_start", "name": payload.get("name") or payload.get("tool") or ""})
                    elif etype == "status.update":
                        await ws.send_json({"type": "status", "text": payload.get("text", "")})
                    elif etype == "error":
                        await ws.send_json({"type": "error", "text": payload.get("message", "error")})

            reader_task = asyncio.create_task(hermes_reader())

            try:
                # Find-or-create this user's persistent Hermes session, scoped to
                # their own profile (mirrors the old PTY `attach` token, but via the
                # real session store this time - a reload replays real history
                # instead of just reconnecting a live PTY screen buffer). profile
                # must be passed on every one of these - each profile has its own
                # separate session store, so title collisions across different
                # people are now structurally impossible (not just avoided by
                # convention).
                found = (await rpc("session.list", {"title": title, "profile": profile})).get("result") or {}
                sessions = found.get("sessions") or []
                if sessions:
                    target_id = sessions[0].get("resolved_id") or sessions[0]["id"]
                    result = (await rpc("session.resume", {"session_id": target_id, "profile": profile})).get("result") or {}
                else:
                    result = (await rpc("session.create", {"title": title, "profile": profile})).get("result") or {}
                sid = result.get("session_id")
                if not sid:
                    await ws.send_json({"type": "error", "text": "Chat unavailable: Hermes session setup failed."})
                    await ws.close(code=1011)
                    return

                await ws.send_json({"type": "history", "messages": _filter_priming_ack(result.get("messages", []))})

                # Hermes has zero structural signal for who's on the other end of
                # a dashboard chat session - same "who am I talking to" mechanism
                # already used for Telegram (see SOUL.md), just told explicitly
                # here instead of left to guess. display_kind="hidden" keeps this
                # turn (and the agent's brief ack) out of the visible transcript
                # AND out of history replay (session_history.py filters it the
                # same way on both sides) - a page reload/reconnect re-asserts it
                # every time, which is a real mitigating factor against a claimed
                # identity switch surviving a refresh (see SOUL.md's "Who you're
                # talking to" section for the rest of this defense).
                if user_name == "family":
                    priming_text = (
                        "This is the shared FAMILY conversation on the Mentor dashboard, not any "
                        "specific person's - do not assume you're speaking with any one named family "
                        "member here, even if the wording sounds like one. Only record genuinely "
                        "family-wide, non-personal facts in this profile's own memory here - never a "
                        "specific person's private disclosure. Never perform a user_name-scoped action "
                        "(objectives, routines, per-user memory, relationships, location, pairing) on "
                        "anyone's behalf from this conversation - that needs their own identified chat. "
                        "This message needs no reply - just continue normally when the first real "
                        "message comes in."
                    )
                else:
                    priming_text = (
                        f"You are now speaking with {user_name} via the Mentor web dashboard "
                        f"(not Telegram). Use this identity for any user_name-scoped tool calls "
                        f"in this conversation. This message needs no reply - just continue "
                        f"normally when they send their first real message."
                    )
                state["priming_inflight"] = True
                await upstream.send(json.dumps({
                    "jsonrpc": "2.0", "method": "prompt.submit", "params": {
                        "session_id": sid, "profile": profile, "display_kind": "hidden",
                        "text": priming_text,
                    },
                }))

                async def browser_to_hermes():
                    try:
                        while True:
                            raw = await ws.receive_text()
                            try:
                                text = (json.loads(raw) or {}).get("text", "")
                            except (TypeError, ValueError):
                                text = raw
                            if not isinstance(text, str) or not text.strip():
                                continue
                            await upstream.send(json.dumps({
                                "jsonrpc": "2.0", "method": "prompt.submit",
                                "params": {"session_id": sid, "profile": profile, "text": text},
                            }))
                    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                        pass

                await browser_to_hermes()
            finally:
                reader_task.cancel()
    except (OSError, websockets.exceptions.WebSocketException):
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "text": "Chat unavailable: could not reach Hermes."})
            await ws.close(code=1011)
    except Exception:
        pass
    finally:
        if ws.client_state == WebSocketState.CONNECTED:
            await ws.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}
