# Mentor (mentor-stack)

Read `ARCHITECTURE.md` in this directory before making changes here — it's the
current-state reference for the whole system (family agent profiles, Telegram
gateway multiplexing, cron/proactive check-ins, tracking domains, dashboard).

For the full history/reasoning behind every decision and bug fix, see
`~/.claude/plans/vivid-greeting-perlis.md`.

This directory (`mentor-stack`) is its own git repo (initialized 2026-10-05) —
source code only. Real family data (`mentor.db`, each profile's holographic
memory, session transcripts) is deliberately NOT in this repo — see
`~/repos/ev-workflow/mentor-data-backup/` (a separate, local-only git repo)
and its own `backup.sh`. Run that script by hand whenever a fresh snapshot is
wanted (manual only, by the user's own choice — no crontab entry exists for
it) — don't add scheduling without asking first, and never add a remote or
push either repo without re-confirming, since the backup repo's history holds
real personal data (health metrics, location sharing, learned facts,
conversation transcripts) that must never leave this machine.

## Standing rules for this project
- No installs on the host — everything runs in Docker containers.
- Every real family member gets their own isolated Hermes profile
  (`/opt/data/profiles/<name>/`) — never share memory/session state across people.
- Telegram: one shared bot/token. Never start a second `hermes gateway start`
  process for a secondary profile — that fights the primary for the poll
  connection (a real incident already happened this way). Use
  `gateway.multiplex_profiles` instead; see `ARCHITECTURE.md` §2.
- Any newly auto-provisioned profile clones `default`'s live Telegram credential
  and a stale `home_channel` — strip `platforms.telegram` from its `config.yaml`
  and comment out `TELEGRAM_BOT_TOKEN`/`TELEGRAM_HOME_CHANNEL*` in its `.env`
  before it's served under multiplex, or it will either fail to start or leak
  deliveries to the wrong person's chat. Not yet automated — check by hand.
- Before editing `config.yaml`/`.env` on any profile, back up the file first
  (`cp x x.bkp-<reason>-<timestamp>`) — this project always has.
- Proactive engagement is ONE cron per person ("mentor-pulse"), not one per
  mechanism — objective-followups, services, topic-checkins, and
  topic-notifications all attach as separate skills to the same
  1-minute-poll job with one combined monitor URL (`/due-pulse?user_name=X`).
  Don't create a new standalone cron for a new proactive mechanism — add it
  as a skill to the existing `mentor-pulse` job and extend `/due-pulse`'s
  combined signal instead. See `ARCHITECTURE.md` §3.
- `mentor-pulse` cron creation is automatic, wired into pairing approval
  (`_ensure_mentor_pulse_cron` in `tools_pairing.py`) — don't create one by
  hand for a newly-paired person, it already happens the moment
  `approve_pairing_code`/`link_conversation` learns their chat_id.
- Permissions are role-based and data-driven (`role_permissions` table,
  `tools_permissions.py`) — never hardcode a "who can do X" rule as a type
  check in code; add/check a permission there instead, so a family admin
  can change policy without a code change. `users.role` (admin-settable
  only) overrides `users.type` for permission checks specifically — `type`
  itself stays purely about age/tone calibration, never repurpose it for
  access control.
- A notification topic's subject and its subscribers can be different
  people (e.g. a location-boundary check "about" a child, delivered to a
  parent) — delivery is always per-subscriber, run inside THAT subscriber's
  own profile, never a single fan-out send. See `ARCHITECTURE.md` §3d.
- Watchlist was generalized into "Service" (2026-10-03) — a service is
  either a script or an agent (or both), triggered by cron OR by new posts
  on a notification topic (`trigger_type`/`trigger_topic_id`) — chain two
  services by pointing one's output_topic_id at another's
  trigger_topic_id. Don't build a new parallel "automation"/"trigger"
  mechanism for a similar idea — extend `services`/`tools_services.py`
  instead, the same way Watchlist itself was extended rather than
  duplicated. See `ARCHITECTURE.md` §3c.
- Service delivery is MANDATORY topic wiring (2026-10-03), not a direct
  reply — every service always has a real `output_topic_id` (auto-created
  + auto-subscribed owner if not given), and the services skill always
  ends `[SILENT]`; `topic-notification` is the one real delivery path for
  every service, for every reader including the owner. See
  `ARCHITECTURE.md` §3c/§3d.
- Skills live in TWO places that must stay in sync: the shared
  `/opt/data/skills/mentor/` directory AND each real profile's OWN copy
  under `/opt/data/profiles/<name>/skills/mentor/` — a cron job resolves
  against the PER-PROFILE copy, not the shared one. Any skill add/rename/
  edit must touch both, for every real profile, or it'll silently show as
  "not found" on that profile's own cron runs. Also clear every affected
  profile's `.skills_prompt_snapshot.json` and restart the gateway
  afterward — it's a cache, not a live scan.
- To manually force-trigger a cron job for real testing, use
  `POST /api/cron/jobs/{id}/trigger` (dashboard REST API, runs inside the
  live gateway process, real delivery) — never
  `hermes --profile <name> cron run <job>` (a fresh ad-hoc CLI process
  with no live Telegram adapter and no local `platforms.telegram` config
  either, by multiplexing design — it will always fail delivery with
  `"platform 'telegram' not configured/enabled"` even though the agent run
  itself succeeds).
- Stacks (the family media library, separate repo at `~/repos/media/index`)
  is wrapped by `stacks-mcp` — Mentor curates it for real (`library:read` +
  `lists:write`, no private namespace restriction; that was tried first
  and deliberately removed once the user clarified Mentor should actually
  curate, not just maintain a sandbox). Never read Stacks'
  `Profile.interests`/library tables directly even though they exist in
  its DB — that's a deliberate boundary its own API draws, unrelated to
  the curation-scope question; use Mentor's own objectives/media_log
  instead. Its web container has no independent network identity (shares
  a VPN sidecar's namespace) — reach it via
  `host.docker.internal:<published port>`, confirmed live at `3002`, not
  a shared docker network. `GET /api/curation/find`'s `kind` param is
  REQUIRED in practice despite its own OpenAPI spec claiming otherwise
  (confirmed live, 400 without it) — trust live behavior over that spec.
  See `ARCHITECTURE.md` §6b.
- Every destructive/cancellation-style dashboard action (archive an
  objective, abandon a service, revoke a permission — anything
  whose effect isn't obviously and immediately reversible with one more
  click) gets a plain `onsubmit="return confirm(...)"` on its `<form>` —
  no JS framework needed, this is a native browser API. Additive actions
  (create, grant, subscribe) and simple reversible toggles (share/private,
  pause/resume) don't need one. Add the same treatment to any new
  destructive action.
