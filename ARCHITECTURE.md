# Mentor — Architecture Reference

Mentor is a self-hosted family assistant built on Nous Research's **Hermes Agent**,
running entirely in Docker containers on this machine (`ev-workflow/mentor-stack/`).
Nothing is installed on the host — every piece of logic, storage, and the agent
itself runs in a container; host directories are bind-mounted in for persistent data.

This document describes the **current, live architecture** — what exists and how it
actually works today. For the history of *why* things are shaped this way, real bugs
hit and fixed, and every design decision's reasoning, see the full build log:
`~/.claude/plans/vivid-greeting-perlis.md` (chronological, one entry per feature/fix).

---

## 1. Containers

| Service | Role |
|---|---|
| `hermes` | The actual agent runtime (Nous Research's Hermes Agent, official image). Owns Telegram, cron, sessions, memory, personas. One process serves every family member (see §2). |
| `mentor-mcp` | MCP server exposing Mentor's own domain logic (objectives, routines, health, media, personas, topic-checkins, relationships, capacity, memory-sharing, pairing) as tools Hermes calls. Talks to `mentor.db` (SQLite) directly. |
| `mentor-dashboard` | Read/write web UI (FastAPI, server-rendered HTML + htmx, DaisyUI). Family + per-person views, calendar, embedded real Hermes chat (via Hermes's own `/api/ws` JSON-RPC), admin console. |
| `caddy` | Reverse proxy + TLS termination (behind Cloudflare Tunnel). Basic auth + Telegram-delivered 2FA (`mentor-auth`) gate `mentor.defg.xyz` and `hermes.defg.xyz`. |
| `mentor-auth` | Small stdlib service implementing the Telegram 2FA second factor for the dashboard. |
| `owntracks-recorder` + `owntracks-mcp` | Self-hosted continuous location tracking (OwnTracks app → Recorder → MCP query layer). Family-admin-gated by default; self always allowed; opt-in sharing for everyone else. |
| `open-wearables` (7-container stack) + `open-wearables-mcp` | Self-hosted wearables aggregator (Apple Health/Samsung Health/Garmin/Whoop/Oura/etc.). Infrastructure is live; no real device connected yet. |

`mentor_data/` is a shared Python package (not a container) mounted into both
`mentor-mcp` and `mentor-dashboard` — the single source of truth for all business
logic (`tools_*.py` files) and the `mentor.db` schema (`db.py`).

---

## 2. Family agents: one Hermes *profile* per real person

Every real family member has their own, fully isolated Hermes **profile** — a
separate directory (`/opt/data/profiles/<name>/`) with its own `SOUL.md`, `.env`,
`config.yaml`, memory store (`memory_store.db`, holographic/fact_store provider),
and session history. Nothing is shared between profiles at the data layer.

Current real profiles: `default` (unclaimed/neutral — never used by a real person),
`evgeniy`, `jasmine`, `bigaunt`, `fataunt`, `jennifer`, `benjamin`, `family` (a
special non-personal profile for the shared family dashboard chat — never assumes a
specific speaker, never performs person-scoped actions).

New profiles are auto-provisioned whenever a person is created (`create_user` →
`_provision_hermes_profile` in `tools_routines.py`), cloned from `default` via
Hermes's own `POST /api/profiles`, with a config-correctness assertion
(`_ensure_memory_provider_config`) so a future drift in `default`'s config can't
silently propagate into new profiles.

**Personas**: each profile's `SOUL.md` has a `<!-- PERSONA:START/END -->` region
that can be spliced with one of 9 fixed personas (`mentor_data/personas.py`) —
3 each for child/adult/senior — via `set_persona`. Family admin manages a child's;
teen/adult/senior choose their own. Defaults to a generic age-calibration block.

### Gateway multiplexing (how one process serves all profiles)

**Critical, non-obvious point**: cron and Telegram both have real single-consumer
constraints — cron jobs only tick from inside the gateway process scoped to their
own profile home, and Telegram's `getUpdates` long-poll allows only one consumer
per bot token. Naively starting a second `hermes gateway start` for a profile (the
"standalone" path, `--force`) starts a genuinely separate process that **fights the
primary for the Telegram connection** — this caused a real incident (seized the
shared token, orphaned a live dashboard session) before the correct mechanism was
found.

**The correct, sanctioned mechanism**: `gateway.multiplex_profiles: true` in the
`default` profile's `config.yaml` (currently set). This turns the *one already-
running* `default` gateway process into a real multiplexer:
- Keeps the single Telegram connection — no second poller, ever.
- Its cron ticker loops over **every served profile's own cron store** each cycle
  (confirmed in Hermes source — this is the actual fix for "cron never fires for
  anyone but the launch profile").
- Every operation (message handling, cron execution, memory/session access) stays
  scoped to the correct profile's own home directory via a context-manager Hermes
  swaps into per-turn (`_profile_runtime_scope`) — **multiplexing shares the
  process and the platform connection, never memory or conversation context.**
- `served_profiles` (visible in `/opt/data/gateway_state.json`) currently:
  `default, benjamin, bigaunt, evgeniy, family, fataunt, jasmine, jennifer`.

**Required companion fix, easy to re-break**: every profile is cloned from
`default` at creation time, which means each one *also* clones `default`'s live
Telegram credential (`TELEGRAM_BOT_TOKEN` in `.env`) and a stale `home_channel`
(always defaulted to whoever was `default`'s home channel at clone time — in
practice, Evgeniy's own chat_id, on every single profile). Under multiplexing this
causes two real problems if left in place:
1. **Startup refusal**: the multiplexer detects the same credential configured on
   two profiles and refuses to start the duplicate adapter (`"Profile 'default'
   and 'X' both configure telegram with the same credential"`) — logged as an
   ERROR but does not crash the gateway; it just means that profile silently isn't
   served.
2. **Cross-family delivery leak** (a real incident this caused): any cron job with
   no explicit delivery target falls back to a profile's own `home_channel`, and
   since every profile's stale clone pointed at Evgeniy's chat_id, *any* fallback
   delivery for *any* family member lands in Evgeniy's chat instead of theirs.

**Fix applied**: for every non-`default` profile, `platforms.telegram` was removed
from `config.yaml`, and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_HOME_CHANNEL` /
`TELEGRAM_HOME_CHANNEL_THREAD_ID` were commented out of `.env` (backups kept
alongside as `.bkp-pre-multiplex-<timestamp>`). With no credential of its own, a
profile is correctly treated as "served by the primary's shared adapter" instead
of trying (and failing) to claim a duplicate. **Any newly auto-provisioned profile
will need the same cleanup** — this is not yet automated in
`_provision_hermes_profile`; a real follow-up if more family members are added.

### Inbound message routing (who a Telegram message belongs to)

One shared bot (`@Mentor8888Bot`, one token). `gateway.profile_routes` in
`default`'s `config.yaml` maps a real Telegram `chat_id` to a profile name — e.g.
`{name: telegram-Evgeniy, platform: telegram, chat_id: '5356768720', profile:
evgeniy}`. This is what makes "Evgeniy's Telegram messages land in his own
profile, with his own memory/history" work, all through the one multiplexed
process. **Only Evgeniy has a real chat_id routed today** — nobody else has
completed Telegram pairing yet (see §7, open items).

### Telegram noise suppression

Hermes's own default display tier for Telegram is already quiet
(`tool_progress: off`), but a global `display:` block in `config.yaml` (meant for
the interactive TUI) was overriding it gateway-wide, causing "AI attempting to use
mcp__ tools" / long-running-task chatter to leak into real Telegram chats. Fixed
with an explicit `display.platforms.telegram` override (`tool_progress: off`,
`interim_assistant_messages: false`, `long_running_notifications: false`,
`busy_ack_detail: false`) — scoped to Telegram only, TUI/dashboard verbosity
unaffected. Also set `platforms.telegram.gateway_restart_notification: false` to
stop "♻️ Gateway shutting down/restarted" pings reaching real chats on every
restart (there were many, during this same debugging work).

---

## 3. Proactive engagement (cron)

**One combined per-person cron ("mentor-pulse"), three independent mechanisms
attached to it as skills.** This is the current, correct architecture — not
three separate cron jobs. All three mechanisms are the exact same shape (a
cheap DB query for "is anything due," gating whether the agent runs at all,
each skill re-checking its own due-condition once invoked), so there was never
a real structural reason to run them as separate jobs — that only happened
because they were built incrementally, at different points in the same
session. `topic-checkin` originally had its own separate 60-minute cron; it
was folded into the shared pulse on 2026-09-27 for exactly this reason.

### a) Per-objective follow-ups
Every objective has `next_follow_up_at` (auto-scheduled on creation, advanced by
`log_progress` or `acknowledge_followup`). `list_due_followups(user_name=...)` /
the shared pulse's monitor surfaces only that person's own objectives that are
actually due. **This mechanism used to run cross-family, unscoped, via an old
`coaching-checkin` cron job on `default`** — that job (`2a48e6864d6f`) was
deleted 2026-09-26 after it was confirmed to be leaking other family members'
due objectives into Evgeniy's chat (see the incident note in §2 above — same
root cause, the stale shared home_channel). Its real, per-person, correctly-
scoped replacement is the `objective-followup` skill (`skills/mentor/
objective-followup/SKILL.md`) attached to `mentor-pulse` (see below) —
verified live: a re-scoping test confirmed a due objective planted for a
DIFFERENT user was correctly excluded from Evgeniy's own due-list, re-confirming
the exact leak class that caused the original incident does not reappear here.

### b) Topic check-ins (the real "initiative" system)
Generalizes proactive engagement to the *topic* level, not just already-tracked
objectives — so a life domain nobody's ever discussed still eventually gets
raised. `topic_checkins` table: one row per `(user, domain)` across the 7 domains
(`hobby/health/career/finance/relationship/media/other`), each with its own
`next_checkin_at` and `checkin_interval_days` (default 7, weekly). Seeding
staggers the first pass (domain 0 due now, domain 1 in +1 day, …) so topics roll
out gradually rather than all at once.

- `list_due_topic_checkins` / `acknowledge_topic_checkin` (`mentor_data/tools_topics.py`)
- HTTP monitor endpoint: `mentor-mcp:8000/due-topic-checkins?user_name=<Name>`
- Skill: `skills/mentor/topic-checkin/SKILL.md` — pulls the due domain, checks
  existing objectives + relevant history, has a real conversation, creates/updates
  objectives as warranted, and **unconditionally** calls `acknowledge_topic_checkin`
  at the end regardless of engagement (non-negotiable — otherwise the topic never
  becomes due again).
- Now runs as the `topic-checkin` skill attached to `mentor-pulse` (folded in
  2026-09-27, was previously its own separate 60-minute cron `d4ffbedd4f4e`,
  since removed). Same due-check logic, just polled every 1 minute like
  everything else on the shared pulse instead of its own slower interval —
  polling more often costs nothing extra (it's a plain DB query either way).
- **Currently only Evgeniy has this running at all** — blocked on delivery
  channel for everyone else (see §7).

### c) Services (generic recurring job — generalized Watchlist)
Generalizes the same "check something periodically, only speak up when it
changed" pattern to arbitrary **external** research, not just Mentor's own
data — a news digest from named sites, a property search, nearby family/kids
events, grocery deals, a location-boundary check, or anything similarly
shaped — and further generalizes *what triggers a run* from "only a cron
schedule" to "a cron schedule OR new posts on a notification topic," enabling
real chaining: Service A posts to topic X, Service B is triggered by new
posts on topic X, and so on. One reusable mechanism rather than a bespoke
skill per idea. In a sense a service is either a script or an agent (or
both). (History: "research watches" → "Watchlist" (2026-09-26) →
generalized to "Service" (2026-10-03), the same table renamed/extended in
place each time rather than built as a parallel mechanism, preserving all
real live data through each rename.)

- `services` table: one row per service (`user_id`, `title`, free-text
  `category`, `prompt` — the real specific instructions, e.g. "3+ bedroom
  houses in Bondi under $800k with parking" — optional `sources` domain
  list, optional `script`, optional `objective_id`, a real (never NULL in
  practice) `output_topic_id` — see §3d, mandatory topic delivery since
  2026-10-03 —, `trigger_type` ('cron'/'topic', default 'cron'),
  optional `trigger_topic_id` (required when `trigger_type='topic'`),
  `schedule_interval_days`, optional `schedule_cron`, `status`
  active/paused/completed/abandoned, `next_check_at`). `service_results`
  (renamed from `service_matches` 2026-10-03 — "matches" read as a verb
  phrase, "results" is unambiguous as a noun) logs each genuinely new
  finding (with an optional `dedupe_key`, e.g. a listing URL) so a run
  never re-reports the same thing twice.
  `service_topic_consumption` tracks which topic-triggered services have
  already processed which incoming `topic_posts` row, so a topic-triggered
  service doesn't re-fire on the same post forever (mirrors
  `topic_post_deliveries`'s per-consumer-ack shape, but for a service
  deciding whether to re-run rather than a person being delivered a
  message).
- **`objective_id`** (optional): links a service to the objective it's
  researching toward (e.g. several property-search services feeding one
  "buy a house" objective) — many services can point at one objective.
  `get_objective` (`tools_objectives.py`) now returns a `services` array of
  any linked services (id/title/status/cadence/last+next check) so looking
  at the objective shows its active research alongside milestones/progress.
- **`script`** (optional): a real shell command, not a natural-language
  instruction — when set, the services skill runs it via the agent's
  terminal tool FIRST, deterministically, and reasons/digests/dedupes over
  its literal stdout against `prompt`, instead of doing its own web search.
  "Script, then prompt back to Hermes" — deliberately a single fixed
  data-gathering step, not a multi-stage pipeline (nothing built so far
  needs more than one, and the agent's own tool-calling loop can still do
  extra lookups on top if a run genuinely needs it — a rigid multi-step
  schema would just be a more constrained reimplementation of that same
  capability). Leave it unset for the common case (prompt alone, agent
  searches the web itself). **Verified live, automatically, under the
  original Watchlist name**: a test item with a script producing two
  fixed, unguessable identifiers was picked up by the real 1-minute pulse,
  and both match rows logged afterward contained those exact strings —
  proof the agent genuinely executed the script and reasoned over its real
  output, not a fabricated plausible-sounding answer. Cleaned up afterward;
  the underlying script-then-reason mechanism is unchanged by the Service
  generalization, only renamed/extended.
- **`trigger_type`/`trigger_topic_id`** (new in the Service generalization,
  not yet exercised with real data — every pre-existing row defaulted to
  `trigger_type='cron'` by the rename migration): a service can instead run
  whenever a NEW post lands on `trigger_topic_id`, rather than on a cron
  schedule — `list_due_services` returns `due_reason: "topic"` plus
  `pending_topic_post_ids` for these, and the services skill calls
  `consume_topic_post` for each one processed (instead of
  `acknowledge_service_check`, which is cron-only). This is how services
  chain into pipelines: one service's `output_topic_id` can be a second
  service's `trigger_topic_id`.
- Tools (`mentor_data/tools_services.py`): `add_service`, `list_services`,
  `update_service` (conversational — adding/managing a service),
  `list_due_services`, `list_service_results`, `log_service_result`, `acknowledge_service_check`,
  `consume_topic_post` (skill-only — the actual scheduled/triggered run).
- Monitor endpoint: `mentor-mcp:8000/due-services?user_name=<Name>`.
- Skill: `skills/mentor/services/SKILL.md` (renamed from `watchlist/`,
  2026-10-03) — reads due service(s) (cron- or topic-triggered), checks
  prior matches first (dedup), researches via Hermes's real web search/
  fetch (or the script's output), logs + reports only genuinely new
  matches, and **produces no reply at all** when nothing new is found
  (relies on the same empty-content delivery suppression already observed
  for `coaching-checkin`) — this is what keeps it from becoming a noisy
  repeat digest. Unconditionally acknowledges/consumes at the end either
  way.
- **Initiative is real, not just available on request**: `SOUL.md` (all
  profiles) instructs proactively offering to add a service when someone
  expresses a standing interest worth monitoring in passing — verified live
  under the original Watchlist name (a test conversation mentioning
  manually re-checking property listings produced an unprompted,
  correctly-scoped `add_watchlist_item` call with real criteria/sources,
  plus an honest caveat that it won't actually fire until a recurring cron
  exists for that person's channel); the same SOUL.md instruction now
  points at `add_service`.
- Delivery has the same constraint as every mechanism on the shared pulse
  (§7): a cron-triggered service is stored and ready the moment it's added,
  but nothing checks it automatically until that person has a
  `mentor-pulse` cron (a topic-triggered one is also gated on that, plus on
  something actually posting to its trigger topic).

### d) Notification topics (who's told, decoupled from who it's about)
Originally built for cases where the subject and the recipient differ —
"tell a parent if their child is outside an agreed location boundary,"
where the check is about the child but the right recipient is someone
else. Since 2026-10-03 this is no longer an edge-case layer: it's the
**one real delivery mechanism for every Service**, including the owner's
own normal case (see §3c's `output_topic_id` note) — a topic can still
exist with no service feeding it, but every service now always has one
feeding it. A topic can also be a service's *trigger*, not just its
output (chaining).

- **`role_permissions`** (`mentor_data/tools_permissions.py`): real,
  data-driven access control — presence of a `(role, permission)` row
  means granted (absent = denied, same default-deny shape as
  `fact_visibility`). Seeded default: `adult`/`senior` →
  `create_subscription`; nothing else granted. A family admin can change
  this later (`grant_role_permission`/`revoke_role_permission`) without a
  code change — e.g. deciding teens should get it too. `role` here isn't
  always `users.type`: `effective_role()` uses `users.role` (nullable,
  **admin-settable only**, via `set_user_role` — never self-service) when
  set, falling back to `type` otherwise. `type` stays purely about
  age/tone calibration (personas); `role` is purely about what someone's
  allowed to do — the two are deliberately independent.
- **`notification_topics`** (subject_user_id, title, category) +
  **`topic_subscriptions`** (topic_id, subscriber_user_id) — a topic is
  "about" a subject but its subscribers don't have to be that subject.
  Creating/subscribing to a topic about **yourself** needs the
  `create_subscription` permission; about **someone else** requires being
  a family admin (checked directly, not assumed).
- **`services.output_topic_id`** — **mandatory wiring, not optional**
  (changed 2026-10-03; was additive/optional at first). Every service
  always has a real `output_topic_id`: `add_service` auto-creates a new
  topic "about" the owner and auto-subscribes them to it the moment the
  service is created, unless the caller explicitly points at an existing
  one (e.g. to feed a shared topic, or a second service's
  `trigger_topic_id` for chaining). The services skill no longer composes
  its own owner-facing reply at all — it posts each genuinely new finding
  to the topic via `post_to_topic` and always ends its own turn with
  `[SILENT]`; `topic-notification` (same pulse, same turn) is now the ONE
  real delivery path for every service, for every reader including the
  owner — e.g. a location-boundary check "about" a child still posts to
  the same topic a parent subscribes to, exactly as before, but the
  owner's OWN delivery now goes through the identical mechanism instead of
  a separate direct-reply shortcut. `topic-notification`'s own guidance was
  strengthened accordingly: since most of its runs are now "the owner
  reading about their own service," not a third party, it's told
  explicitly not to flatten that case into something more generic than a
  direct reply would have been.
- **`topic_posts`** (one row per real post) + **`topic_post_deliveries`**
  (per-subscriber ack) — delivery is genuinely per-subscriber, not one
  fan-out send: each subscriber's *own* `mentor-pulse` independently
  notices a pending post (`list_due_topic_posts`), delivers it, and acks it
  (`acknowledge_topic_post_delivery`) — structural, not a style choice,
  since delivery always runs inside that subscriber's own Hermes profile
  (the per-person isolation this whole project is built on).
- **Tone is a `SOUL.md` concern, not a schema field** (deliberately no
  `delivery_style` column): the `topic-notification` skill knows who it's
  about (`subject_user_name`) and who's actually reading it, and is
  instructed to calibrate — direct/factual for an admin, a gentle
  conversational check-in for a child hearing about themselves — using the
  same age/sensitivity judgment already applied to personas/routines, not a
  new mechanism.
- **Verified live**: full permission enforcement tested directly — a
  child-type test user correctly refused to create a topic or subscription
  about themselves (no permission, not admin); an admin test user
  correctly created a topic about the child AND subscribed both itself and
  the child; the child was correctly refused when trying to self-subscribe
  separately; `set_user_role` correctly refused for a non-admin and
  succeeded for the admin, and immediately changed the permission-check
  result for that user. Then the full pipeline end-to-end, automatically,
  through the real `mentor-pulse`: a real topic/subscription/post for
  Evgeniy was picked up by the 1-minute pulse on its own, and a real
  `topic_post_deliveries` row was written — confirming the skill correctly
  found the due post and acknowledged it (no message actually sent, since
  the test content was deliberately labeled as a test — same sensible
  judgment observed on other mechanism tests with obviously-synthetic
  content). All test data cleaned up afterward.
- **Mandatory topic wiring (2026-10-03), verified live with real data and
  real delivery**: all 7 of Evgeniy's real services were backfilled with
  an auto-created topic + auto-subscription (`_ensure_output_topic` in
  `tools_services.py`, dry-run tested on a scratch copy first, DB backed
  up before the live backfill). Caught two real bugs along the way, both
  fixed: manually triggering a job via `hermes --profile <name> cron run
  <job>` (a fresh ad-hoc CLI process) can never deliver via Telegram —
  that profile's own `config.yaml` deliberately has no `platforms.telegram`
  block (multiplexing), and the ad-hoc process has no live adapter either
  — `last_delivery_error: "platform 'telegram' not configured/enabled"`
  every time; the correct way to force a real test run is
  `POST /api/cron/jobs/{id}/trigger` (dashboard REST API), which executes
  inside the already-running gateway process and shares its live adapter,
  confirmed via `delivery_outcome: "delivered"`, `source: "builtin"`. Also:
  skills are NOT purely read from the shared `/opt/data/skills/mentor/`
  directory — every real profile additionally has its OWN local copy
  under `/opt/data/profiles/<name>/skills/mentor/`, which is what a cron
  job actually resolves against; editing only the shared copy during the
  Watchlist→Service rename left every profile's own copy silently stale
  (`watchlist` still registered, `services` not found at all) until this
  was caught and both the per-profile skill directories and the stale
  `.skills_prompt_snapshot.json` caches were fixed/cleared, followed by a
  gateway restart. After both fixes: forced a real due service, triggered
  it via the REST API, and confirmed in one agent turn — a new
  `service_results` row, a new `topic_posts` row on the auto-created
  topic, a real `topic_post_deliveries` ack, and `delivery_outcome:
  "delivered"` — the full pipeline, no direct reply anywhere in it.

### `mentor-pulse` — the actual cron job (every mechanism above runs through this)
One **generic, cheap, frequent pulse** per person (job `de0630fb4d34` on
`evgeniy`, `every 1m`, `--deliver telegram:<chat_id>`) with **all four
skills attached** (`objective-followup`, `services`, `topic-checkin`,
`topic-notification`) and **one combined monitor URL**
(`http://mentor-mcp:8000/due-pulse?user_name=<Name>` — a single plain-text
signal like `F:69|S:5,6|T:hobby|N:2` combining due-followups, due-services
(cron-due or topic-due), due-topic-checkins, and due
notification-topic-posts). The cron itself never decides anything; it just
polls that one URL every minute (four static DB queries, effectively free)
and only invokes the real agent when the combined output changes. When it
does run, each attached skill independently re-checks its OWN
due-condition (`list_due_followups`/`list_due_services`/
`list_due_topic_checkins`/`list_due_topic_posts`) and handles whatever's
due for it, doing nothing if nothing is — the monitor only ever decides
*whether* to run the agent, never *what* to do. This is the general shape
the old (deleted) cross-family `coaching-checkin` cron used, corrected to
be per-person and multi-mechanism (`hermes cron edit <job_id> --add-skill
<name> --monitor-url .../due-pulse?user_name=<Name>` is how an existing
single-purpose pulse job upgrades to this combined shape — used three
times so far: adding `objective-followup` to what was originally a
watchlist-only job, folding in `topic-checkin` and retiring its separate
cron, then adding `topic-notification`; the job's own `skills` list was
later updated in place from `watchlist` to `services` when that mechanism
was generalized, 2026-10-03, no new cron needed). Each
`services`/`objectives`/`topic_checkins` row carries its OWN
`next_check_at`/`next_follow_up_at`/`next_checkin_at` — "hit that time" is
the actual due-check per item, entirely independent of the cron's own
1-minute polling cadence (a topic-triggered service instead keys off
unconsumed `topic_posts`, not a time at all). `_ensure_mentor_pulse_cron`
(§7) auto-creates this whole job — all four skills, combined monitor — the
moment a person's real chat_id becomes known, so a newly-provisioned pulse
already includes `topic-notification` from day one.

**Verified live end-to-end, automatically** (not manually triggered),
across every mechanism: (1) a watchlist item (pre-Service-generalization)
with deliberately unmatchable criteria — picked up ~90s later, found
nothing, produced no reply (`delivery_outcome: 'suppressed'`), rescheduled
forward 7 days; (2) a backdated-due objective — picked up within a minute,
correctly rescheduled `next_follow_up_at` forward 14 days
(`acknowledge_followup` fired), and a planted due objective for a
DIFFERENT user was correctly excluded from Evgeniy's own scoped due-list;
(3) after folding in `topic-checkin`, a run with all three (then-current)
skills attached completed cleanly and correctly stayed silent when nothing
was genuinely due; (4) after adding `topic-notification`, a real topic
post was picked up automatically and correctly acknowledged
per-subscriber. All test data cleaned up afterward — these were mechanism
tests, not real objectives/watches/topics. (Live re-verified immediately
after the Service generalization/rename, 2026-10-03: `due-pulse` for
Evgeniy returned a correctly-shaped signal against the renamed `services`
table with zero data loss across all 7 real rows, and a real `hermes -z`
call correctly listed all 7 by name, trigger_type, and cadence.)

### e) Real cron scheduling per item (not just "every N days")
Every per-item due-time (`objectives.next_follow_up_at`,
`services.next_check_at`, `topic_checkins.next_checkin_at`) can optionally
use a real 5-field cron expression (`schedule_cron` column on each table)
instead of a flat day-interval — "every Monday at 9am" (`"0 9 * * 1"`),
"every Tuesday and Friday at 9am" (`"0 9 * * 2,5"`), etc. (Cron-triggered
services only — a topic-triggered service's due-time is governed entirely
by unconsumed posts on its trigger topic, not `schedule_cron`/
`next_check_at`, though the NOT-NULL `next_check_at` column still holds a
harmless creation-time placeholder for those rows.) This is about the
per-item due-time computation done in our own code, not `mentor-pulse`'s
own outer 1-minute poll (which stays a fixed, generic, cheap tick
regardless — richer scheduling only applies to *when a specific item
becomes due*, not how often the pulse checks).

- `mentor_data/scheduling.py` — a tiny shared wrapper around **croniter**
  (a small, standard library; not hand-rolled recurrence math):
  `validate_cron(expr)` and `next_from_cron(expr, from_time)`. Both
  `mentor-mcp` and `mentor-dashboard` have `croniter` in their Dockerfiles.
- `schedule_cron` takes precedence over the plain interval
  (`follow_up_interval_days`/`schedule_interval_days`/`checkin_interval_days`)
  once set, on all three tables; the interval field is still stored either
  way (NOT NULL columns, and a sensible fallback if `schedule_cron` is ever
  cleared). Set via `create_objective`/`update_objective`,
  `add_service`/`update_service`, or the new `set_topic_checkin_schedule` —
  an invalid cron expression is rejected with a clear error before ever
  being stored; pass `""` to clear it back to the plain interval.
- **Verified**: an invalid expression correctly rejected on create; a real
  `"0 9 * * 1"` objective correctly computed its first occurrence as the
  next Monday 9am; simulating a realistic acknowledge (now genuinely past
  the scheduled fire time) correctly advanced to the *following* Monday, 7
  days later, not the same day again; a multi-day expression (`"0 9 * *
  2,5"`) from a Monday correctly picked the very next Tuesday. Same
  confirmed for `services` (then `watchlist_items`) and `topic_checkins`.
  All test rows cleaned up afterward, and Evgeniy's real `health`
  topic-checkin schedule (touched only for this test) restored to its
  normal weekly interval.

### Delivery mechanics worth knowing
- A cron job's `--deliver <platform>:<chat_id>` is fixed at creation time — not
  re-evaluated if a person's channel preference changes later; recreate the job.
- Delivery has two lanes: **live-adapter** (if a connected adapter is available and
  the job's origin session is resumable) and **standalone** (a raw one-shot API
  call, e.g. `python-telegram-bot`'s `Bot.send_message` — does *not* need or use
  the poll connection, works from any process with a valid token). This is what
  makes multiplexed cron delivery safe.
- `users.preferred_checkin_channel` (nullable: `telegram`/`dashboard`/NULL=auto)
  exists as a schema field but is **not yet wired into actual cron-creation
  logic** — still manual today.

---

## 4. Tracking domains (all via `mentor-mcp` tools, backed by `mentor.db`)

- **Objectives** (`tools_objectives.py`): goals with domain, status, priority,
  target date, follow-up interval, optional parent (sub-objectives), effort/cost
  fields (`time_cost_hours_per_week`, `money_cost_*`, `essential`), visibility
  (shared/private). `analyze_objective_load` (`tools_capacity.py`) does pure
  static time/money-budget math against a profile's optional
  `available_hours_per_week`/`available_money_per_month` — the *agent* does the
  judgment/conversation, code only computes numbers.
- **Routines** (`tools_routines.py`): recurring habits, streaks, badges
  (`BADGE_EMOJI`: 🌟✨🏆👑💎 at 3/7/14/30/100 days), self-reported completion only
  (no automated compliance detection). Gamification skill:
  `skills/mentor/gif-search-giphy/` (Tenor is dead — Google shut down third-party
  access June 2026; Giphy free tier used instead, with explicit dashboard-markdown
  vs. Telegram-download-then-attach delivery paths).
- **Health metrics** (`tools_health.py`): free-text `metric_type`, optional
  objective link.
- **Media log** (`tools_media.py`): book/show/movie/game/podcast/article, fixed
  status enum.
- **Relationships** (`tools_relationships.py`): directed graph edges between
  users (`parent`/`child`/etc., free text), used for cross-profile awareness and
  onboarding suggestions.
- **Memory / fact_store**: Hermes's own `holographic` provider is the *only* real
  memory now (per-profile `memory_store.db`) — `add_user_memory`/legacy
  `USER.md`/`MEMORY.md` are retired/inactive. `set_fact_visibility` marks a fact
  `shared`, which event-syncs it (one-directional, on share/unshare only) into
  `family`'s own memory via `family_synced_facts`, visible through
  `list_family_shared_context`. Profile-field changes (`type`, DOB, gender,
  notes, timezone) also auto-sync into the person's own fact_store, then family,
  via `sync_profile_to_own_memory`.

---

## 5. Dashboard (`mentor-dashboard`)

Public at `mentor.defg.xyz` (Caddy basic auth + Telegram-delivered 2FA). Family
view (calendar, filtered objective cards, admin console) and per-person views
(their own objectives incl. private, routines, editable profile/persona/SOUL.md,
embedded **real** Hermes chat — a JSON-RPC client against Hermes's own `/api/ws`,
not a second agent).

**`/admin` is a landing page of subscreens**, not one long scrolling page
(restructured 2026-09-28, once it grew past ~5 sections) — each area gets
its own route, with a shared `_admin_context()` helper (the "I am" admin
picker, target-user dropdowns) and `_admin_subscreen()` shell (back-link +
heading) so they stay visually consistent:
- `/admin/users` — add a family member.
- `/admin/telegram` — Telegram onboarding + pending-pairing approval.
- `/admin/location` — OwnTracks credential provisioning.
- `/admin/services` — read/oversight table of every Service across every
  family member (owner, category, trigger type, status, cadence, linked
  objective/output-topic/trigger-topic by name via a direct join) with
  inline status-change buttons (active/paused/completed/abandoned) **and,
  for a cron-triggered service, an inline cadence-edit form** (interval-days
  input + `schedule_cron` text input, same validation as the MCP tool — an
  invalid expression is rejected with a clear error, a blank cron field
  clears back to the plain interval; a topic-triggered service shows "not
  cron-scheduled" instead, since its due-time has no interval/cron at all).
  This edit needs no Hermes API at all: it's a direct write to `mentor.db`
  via the same `tools_services.update_service` the chat path uses, picked
  up automatically by `mentor-pulse`'s next 1-minute tick (which always
  reads the table fresh — nothing to invalidate or sync). Deliberately no
  create form — creation stays conversational ("just ask Mentor"), this is
  visibility + control for an admin who wants to see/adjust what exists
  without querying the DB directly. Verified (under the original Watchlist
  name): a real cron value set through the actual form, an invalid one
  correctly rejected, and clearing back to a plain interval — all confirmed
  in the DB, test item cleaned up after; re-verified rendering correctly
  against the renamed `services` table immediately after the 2026-10-03
  generalization, with all 7 real rows showing their correct trigger type
  (cron) and no watchlist terminology left in the rendered page.
- `/admin/notifications` — **notification topics** (create a topic, view
  existing topics + their subscribers, add a subscription) and
  **permissions** (view/grant/revoke `role_permissions` policy) — the admin
  UI for §3d, calling `tools_notifications.py`/`tools_permissions.py`
  directly (no new mentor-mcp round trip needed).

Every POST route redirects back to its own subscreen (`/admin/services?
notice=...`, etc.), not the old generic `/admin?notice=...`. **Verified
end-to-end via real HTTP POSTs** after the restructure: all six pages
(`/admin` + 5 subscreens) return 200 with no template-escaping artifacts;
topic creation, subscription, permission grant/revoke, a non-admin's
attempt correctly refused, and a real watchlist status-change all confirmed
through the actual rendered forms, redirecting to the correct subscreen —
not just the underlying tools. (One real bug caught by this testing, not
shipped: an early version of the refactor accidentally deleted the
`@app.get("/admin")` route registration itself while restructuring the
function, leaving `/admin` 404ing while every subscreen worked fine — found
immediately by testing all six paths together rather than assuming the
refactor was clean, fixed, re-verified.)

No dashboard UI yet for editing an existing objective (create + archive
only), or for creating a service / notification topic's underlying check
from the dashboard itself (topics can be created here now; the service
that would feed one is still chat-only).

---

## 6. Location & wearables

- **OwnTracks** (`owntracks-recorder` + `owntracks-mcp`): real continuous location
  history, phone → Caddy (`owntracks.defg.xyz`, device-only Basic Auth, read API
  never exposed externally) → Recorder → MCP. Access: self always, family admin
  always, opt-in (`share_location_with_family`) for everyone else.
- **Open Wearables**: full 7-container stack running, MCP wired, zero real
  devices connected (needs the user to link one via the frontend or a mobile SDK
  for Apple/Samsung Health specifically — not completable without the physical
  device).

---

## 6b. Media library (Stacks) — added 2026-10-05

`stacks-mcp` is a thin wrapper around a SEPARATE self-hosted app's
("Stacks," own repo at `~/repos/media/index`) own documented **curation
API** (`docs/CURATION.md` there, `GET /api/curation/openapi.json`) — lets
Mentor suggest real, already-available content when it learns about
someone's interests, check whether a find is already indexed, and add a
genuinely enriching one when it isn't (Stacks is explicitly meant to be a
curated *index*, not something that already has everything).

- **Network path, confirmed live**: Stacks' own `stacks_web` container runs
  with `network_mode: container:<vpn-sidecar>` (its outbound traffic routes
  through a VPN container in this specific deployment) — it has no
  independent container-name network identity for `stacks-mcp` to join by
  name. The stable way in is the VPN sidecar's own host-published port:
  confirmed live, `http://host.docker.internal:3002` → Stacks' real `3001`
  (NOT the `3001` the repo's own generic docs assume as the default — this
  deployment publishes it differently). `stacks-mcp` gained `extra_hosts:
  host.docker.internal:host-gateway`, same mechanism Stacks' own
  `docker-compose.yml` already uses for its own outbound needs.
- **Mentor curates for real — this was a deliberate design correction,
  not the original plan.** The first version force-prefixed every write
  into a private `"mentor-"` namespace (`lists:write:mentor-*`), reasoning
  that curation should stay a separate human/agent role (Stacks' own
  `skills/stacks-curator/SKILL.md`) and Mentor should only ever suggest.
  The user corrected this directly: Mentor is *meant* to curate real
  content, not just maintain its own sandboxed suggestion lists. The
  `_mentor_slug` guardrail was removed; `create_or_update_stacks_list`/
  `add_to_stacks_list` now operate on any list, the same as a human
  curator would (a write through either side "claims" that list — Stacks
  stops reloading it from its own JSON files). Read tools
  (`stacks_status`/`search_stacks_library`/`list_stacks_lists`/
  `get_stacks_list`) need `library:read`; the write tools need
  `lists:write`. The live token (deliberately, by the administrator's own
  choice) also carries `channels:write`/`links:write`/`jobs:run`, unused
  by any tool here — proposing a channel, linking items, or running a
  maintenance job stay genuinely out of scope (no tool exists for them),
  not because the credential couldn't, but because nothing here calls them.
- **Deliberately does NOT read Stacks' own `Profile.interests`/library
  (favorite/watchlist/watched) data**, even though it exists in Stacks'
  own database (confirmed via its `prisma/schema.prisma`) — the curation
  API's own docs state plainly: "There is no scope for profiles… an agent
  curates the library; it never decides who sees what." Mentor uses its
  OWN already-tracked interest signals instead (objectives, media_log,
  routines) and cross-references them against what Stacks actually has —
  two systems, each read/written through its own sanctioned boundary, not
  one bypassing the other's. This part of the design was NOT changed by
  the curation correction above — it's a privacy boundary, not a scoping
  choice about who's "allowed" to curate.
- **A find that isn't in Stacks yet isn't a dead end**: if it's genuinely
  worth keeping, curate it in for real (dry-run first on
  `add_to_stacks_list`, the API's own recommended workflow, default
  `dry_run=True`); if it's a one-off not worth indexing, just tell the
  person where it's actually available — ordinary conversation/web
  search, no Stacks call needed for that case.
- **Real bug found and fixed via live testing, not assumed from the
  docs**: `GET /api/curation/find`'s own OpenAPI spec marks `kind` as
  optional, but the live API rejects a call without it (400 — "kind must
  be title, book, audiobook, playlist, channel or video"). Confirmed by
  calling it both ways against the real deployment. Fixed by making
  `search_stacks_library`'s own `kind` parameter required (not just
  documented as a recommendation) — the tool now matches live behaviour,
  not the spec's claim.
- **Token live and verified end-to-end (2026-10-05)**: `STACKS_API_TOKEN`
  set in `.env`, `stacks-mcp` restarted and re-registered (6 tools).
  `stacks_status` confirmed the real granted scopes;
  `search_stacks_library("Cosmos", kind="title")` returned real matches
  (`tv:58474` "Cosmos" 2014, `tv:1430` "Cosmos: A Personal Voyage" 1980,
  `movie:9305` "Microcosmos" 1996); `list_stacks_lists` returned real
  curated collections (e.g. "Early Years: gentle first screens," ages
  5-7). A live `hermes -z` conversational test (as Evgeniy, asking about
  rock climbing/mountaineering) correctly searched for real, found
  nothing, said so honestly rather than fabricating a result, and
  proactively offered to curate something in instead — confirming the
  whole conversational path, not just the raw tool calls.
- SOUL.md (all profiles) gained a new "Media library (Stacks)" section
  covering: proactive suggestion when a real interest is already known
  (never improvised), the profile-data access boundary above, the
  curate-or-point-elsewhere choice for something not yet indexed (updated
  to drop the now-removed "own namespace" framing), and applying the same
  age/tone judgement to a suggestion that Stacks' own viewer-side gating
  would eventually apply anyway.

---

## 7. Known open items (as of 2026-09-27)

- **Stacks integration live and verified** (2026-10-05) — see §6b. Token
  set, real search/list/curate calls confirmed end-to-end, including a
  live conversational test. Not yet used with any real family interest
  data (no list has actually been curated through it for a real person
  yet) — only mechanism-level verification so far.
- **`mentor-pulse` cron creation is now automatic** (2026-09-27) —
  `_ensure_mentor_pulse_cron` (`mentor_data/tools_pairing.py`) runs
  automatically inside both `approve_pairing_code` and `link_conversation`,
  the moment a person's real chat_id first becomes known (the same point
  `_ensure_profile_route` already hooks). It's idempotent (checks for an
  existing `mentor-pulse` job by name first) and uses two plain REST calls
  against Hermes's own dashboard API — `POST /api/cron/jobs` to create
  (schedule `1m`, all four skills, `--deliver <platform>:<chat_id>`), then
  `PUT /api/cron/jobs/{id}` with `{"updates": {"monitor_url": ...}}` to
  attach the combined monitor (the create model has no `monitor_url` field
  at all; the update path's untyped `updates` dict reaches the same
  underlying field the CLI's `--monitor-url` does — confirmed by reading
  Hermes's own source, not guessed). No shell-out, no Hermes fork needed.
  **Verified live**: idempotency confirmed against Evgeniy's real existing
  job (correctly skipped, no duplicate); real creation confirmed against a
  throwaway test user + Hermes profile (job created with the correct
  schedule/skills/deliver AND monitor_url all correctly attached via the
  two-step call) — cleaned up afterward.
- **Jasmine, Benjamin, bigaunt, fataunt, Jennifer still have no
  `mentor-pulse` cron** — the automation above is real and tested, but
  it only *fires* at pairing-approval time, and none of them have a
  confirmed Telegram pairing yet (`has_own_pairing=false`), so there's been
  no real chat_id to trigger it with. The moment any of them completes
  pairing, this should now happen automatically with no manual step.
- **`preferred_checkin_channel`** exists as a DB field but isn't used by any
  cron-creation logic yet — channel selection is still manual.
- **Newly auto-provisioned profiles will re-introduce the stale Telegram
  credential problem** (§2) unless `_provision_hermes_profile` is updated to also
  strip the cloned `platforms.telegram` block / `.env` token — not yet automated.
- **`bot-chat` delivery** (for someone with no Telegram pairing) was tested and
  found unreliable — not a working fallback today.
- **Services has no cron for anyone except Evgeniy** — same blocker as
  topic-checkins (§7 above): everyone else needs a confirmed Telegram
  pairing (a real chat_id) before a cron can be created for them.
- **Topic-triggered services are built but not yet used with real data** —
  every one of Evgeniy's 7 real services defaulted to `trigger_type='cron'`
  on the rename migration (pre-existing rows); no chained
  service→topic→service pipeline has been created yet, so the
  `pending_topic_post_ids`/`consume_topic_post` path is verified by design
  and by the DB-level dry-run migration, not by a real live chain firing.
- No screentime/browsing-history tracking, no automated wearable data (device not
  connected), no finance/spending tracking (needs a real data source decision).

For full reasoning behind every decision above, and the complete history of bugs
found/fixed along the way, see `~/.claude/plans/vivid-greeting-perlis.md`.
