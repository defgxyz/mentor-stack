"""
MCP server wrapping Stacks (the self-hosted family media library/platform)'s
own documented "curation API" (docs/CURATION.md in the Stacks repo,
GET /api/curation/openapi.json) - lets Mentor suggest real content based on
a person's interests, check whether it's already indexed, and add a good
find to the library when it isn't (Stacks is meant to be a curated INDEX,
not something that already has everything - enriching it is the point).

Read tools (stacks_status/search_stacks_library/list_stacks_lists/
get_stacks_list) need `library:read`. The write tools
(create_or_update_stacks_list/add_to_stacks_list) need `lists:write` - by
deliberate choice, Mentor acts as a REAL curator here (create/maintain any
list, not just a private sandbox), since that's what was actually asked
for: Mentor should be able to genuinely enrich the library, not just
describe things it found elsewhere. The same stays true of the separate
human-curator workflow (Stacks' own skills/stacks-curator/SKILL.md) - both
can touch the same lists; a write through either one "claims" that list
per Stacks' own rules (reloading from its JSON files then skips it). A
suggestion that isn't worth adding (a one-off, not genuinely enriching)
should instead just point the person to where it's actually available
elsewhere - that's normal conversation/web-search judgement, not a Stacks
API call.

Deliberately does NOT read Stacks' own Profile.interests/library (favorite/
watchlist/watched) data at all, even though that data exists in its
database - the Curation API's own docs state this plainly: "There is no
scope for profiles... an agent curates the library; it never decides who
sees what." Reaching into Stacks' database directly to bypass that boundary
would undermine a deliberate access boundary Stacks' own design enforces.
Mentor uses ITS OWN already-tracked interest signals instead (objectives,
media_log, routines) and cross-references them against what Stacks actually
has - two systems, each read/written through its own sanctioned boundary.

Auth: a bearer token (STACKS_API_TOKEN) with `library:read` + `lists:write`
(this deployment's token also carries channels:write/links:write/jobs:run,
unused by this server - no tool here exercises them), created by the
Stacks administrator via Admin -> Agent access -> New token (never
self-provisioned here - same explicit rule Stacks' own stacks-curator
skill states for itself).
"""
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stacks", host="0.0.0.0", port=8000)

# host.docker.internal, not a docker-network container name: Stacks' own
# web container runs with network_mode: container:<vpn-sidecar> (its real
# deployment routes outbound traffic through a VPN container), so it has no
# independent network identity to join by container name - only the VPN
# container's host-published port is a stable, deployment-agnostic way in.
STACKS_API_URL = os.environ.get("STACKS_API_URL", "http://host.docker.internal:3002")
STACKS_API_TOKEN = os.environ.get("STACKS_API_TOKEN", "")

SECTIONS = ("watch", "read", "channels")
FIND_KINDS = ("title", "book", "audiobook", "playlist", "channel", "video")


def _client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {STACKS_API_TOKEN}"} if STACKS_API_TOKEN else {}
    return httpx.Client(base_url=STACKS_API_URL, headers=headers, timeout=20)


def _call(method: str, path: str, **kwargs) -> dict:
    if not STACKS_API_TOKEN:
        return {"error": "STACKS_API_TOKEN not configured - ask the Stacks administrator for a "
                          "library:read token (Admin -> Agent access -> New token)"}
    try:
        with _client() as client:
            r = client.request(method, path, **kwargs)
    except httpx.RequestError as e:
        return {"error": f"could not reach Stacks at {STACKS_API_URL}: {e}"}
    if r.status_code == 401:
        return {"error": "Stacks rejected the token (missing, invalid, revoked or expired) - "
                          "ask the administrator for a fresh one"}
    if r.status_code == 403:
        return {"error": "token lacks the library:read scope this needs"}
    if r.status_code >= 400:
        try:
            return {"error": r.json().get("error", r.text[:300])}
        except Exception:
            return {"error": r.text[:300]}
    try:
        return r.json()
    except Exception:
        return {"error": "Stacks returned a non-JSON response"}


@mcp.tool()
def stacks_status() -> dict:
    """Check the Stacks library connection: who this token is, its scopes,
    the library's sections and relation types. Call this first if anything
    else from this server errors unexpectedly - most issues are a missing/
    expired token or a scope gap, both shown clearly here."""
    return _call("GET", "/api/curation")


@mcp.tool()
def search_stacks_library(query: str, kind: str) -> dict:
    """Search for something ALREADY in the Stacks library by name - the real
    way to check "do we actually have this" before suggesting it, since a
    plausible-sounding title can easily not be in the library at all.
    `query` is 2-100 characters of the name to find. `kind` is REQUIRED -
    one of: title (films/TV), book, audiobook, playlist, channel, video -
    the live API rejects a missing kind (400), despite its own OpenAPI
    spec claiming it's optional; "title" is the right default for a normal
    movie/show interest. Call it more than once with different kinds if
    you're not sure which applies. Returns each match's `ref` (for lists)
    and `node` (for cross-referencing)."""
    if kind not in FIND_KINDS:
        return {"error": f"kind must be one of {FIND_KINDS}"}
    return _call("GET", "/api/curation/find", params={"q": query, "kind": kind})


@mcp.tool()
def list_stacks_lists(section: Optional[str] = None) -> dict:
    """List Stacks' own curated collections (e.g. a "Science" or
    award-winners list) - the real, already-organized starting point for a
    themed suggestion, rather than guessing at individual titles. `section`
    optionally narrows to one of: watch, read, channels (omit for all).
    Each entry includes item counts - call get_stacks_list for one you want
    to actually suggest from."""
    if section is not None and section not in SECTIONS:
        return {"error": f"section must be one of {SECTIONS}"}
    params = {"section": section} if section else {}
    return _call("GET", "/api/curation/lists", params=params)


@mcp.tool()
def get_stacks_list(section: str, slug: str) -> dict:
    """Get one Stacks list's full detail - every item it actually contains,
    with its own ref. `section` is one of: watch, read, channels. `slug`
    comes from list_stacks_lists' own results. Use this before suggesting
    from a themed collection, so what you describe matches what's really
    on it."""
    if section not in SECTIONS:
        return {"error": f"section must be one of {SECTIONS}"}
    return _call("GET", f"/api/curation/lists/{section}/{slug}")


@mcp.tool()
def create_or_update_stacks_list(section: str, slug: str, name: str, description: str,
                                  purpose: str, audience: str, min_age: int, max_age: int) -> dict:
    """Create (or update) a list in Stacks - e.g. a running "things Jasmine
    might like" list you build up over time as you learn her interests, or
    a themed collection worth curating properly. This is a real curator
    action, the same as Stacks' own human-curator workflow: whichever side
    (Mentor or a person) last writes to a list "claims" it (it stops
    reloading from Stacks' own JSON files - see docs/CURATION.md's
    "release" call if that's ever not wanted). `section` is one of: watch
    (films/TV), read (books), channels (YouTube playlists). `name`/
    `description` are what a person sees; `purpose` is a short label (e.g.
    "Suggested for Jasmine"); `audience` is a short label (e.g. "Jasmine");
    `min_age`/`max_age` bound who it's shown to - set them honestly for
    what's actually on the list, don't just default to 0/99."""
    if section not in SECTIONS:
        return {"error": f"section must be one of {SECTIONS}"}
    body = {
        "name": name, "description": description, "purpose": purpose,
        "audience": audience, "minAge": min_age, "maxAge": max_age,
    }
    return _call("PUT", f"/api/curation/lists/{section}/{slug}", json=body)


@mcp.tool()
def add_to_stacks_list(section: str, slug: str, item: dict, rationale: str,
                        dry_run: bool = True) -> dict:
    """Add a genuinely enriching find to a Stacks list (create it first
    with create_or_update_stacks_list if it doesn't exist yet). Only for
    something worth actually indexing - not a one-off that's better just
    described in conversation with a pointer to where it's available
    elsewhere. `rationale` (one sentence, why this fits) is required and
    gets merged into `item`. `item`'s other required fields depend on
    `section`:
    - watch: {"title": ..., "year"?: int, "type": "movie"|"tv"|"either",
      "tmdbId"?: int} - give tmdbId+type to pin an exact, unambiguous match.
    - read: {"isbn": ...} or {"olWork": "OL...W"} or {"title": ..., "author": ...}.
    - channels: {"playlist": "PL..."} - its channel must already be in the
      library (propose the channel through the human curator first if not).
    Defaults to `dry_run=True` (checks and reports what would happen,
    changes nothing) - the API's own recommended workflow; pass
    `dry_run=False` once you've confirmed the dry-run result looks right."""
    if section not in SECTIONS:
        return {"error": f"section must be one of {SECTIONS}"}
    body = {**item, "rationale": rationale}
    params = {"dryRun": "1"} if dry_run else {}
    return _call("POST", f"/api/curation/lists/{section}/{slug}/items", json=body, params=params)


if __name__ == "__main__":
    import uvicorn

    # streamable-http, not sse - same reasoning as mentor-mcp/owntracks-mcp/
    # open-wearables-mcp: Hermes's MCP client POSTs directly to the given
    # --url rather than following the classic two-endpoint SSE handshake.
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
