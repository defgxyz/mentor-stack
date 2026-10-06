"""
MCP server exposing real-time home-network presence/activity from the
family's TP-Link router (Archer AX10 v3), via the open-source
tplinkrouterc6u library (the same one Home Assistant's own TP-Link
integration uses) - it already implements the router's real RSA/AES login
handshake, confirmed live rather than hand-rolled here.

Real finding, confirmed live against this exact router before trusting it:
its own `active` flag is NOT a reliable "is this person actually here right
now" signal - it stayed true even for a clearly-idle guest device with no
real IP. The honest, verified signal is live throughput: `down_speed`/
`up_speed` (the router's own instantaneous downloadSpeed/uploadSpeed,
passed straight through by the library, not computed/estimated) actually
being nonzero means a device is genuinely transferring data RIGHT NOW. Idle-
but-present (phone on the couch, screen off) still reads 0/0 though, so
"transferring now" is a strong positive signal, not a complete home/away
test by itself - said plainly in every result here, not glossed over.

No historical/usage-over-time tracking is built here - the router only
ever gives a live snapshot (current speed, and a `traffic_usage`/
`online_time` counter that resets on reboot, not a real daily/weekly
total) - building real "how much and when" history would mean polling
this periodically ourselves (e.g. via a Service) and logging it, a
separate, not-yet-built piece.

Access: same shape as owntracks-mcp's location gate, but a SEPARATE
consent flag (share_network_activity_with_family) - self always, family
admin always, opt-in for anyone else. Device linking (which MAC belongs to
which Mentor user) is admin-only, via the generic devices/device_services
model (service_type="network", provider="tplink_router") - mirrors
link_owntracks_device exactly.
"""
import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP
from tplinkrouterc6u import TplinkRouterProvider

from mentor_data import db
from mentor_data import tools_devices
from mentor_data import tools_routines
from mentor_data.tools_routines import is_family_admin

db.init_schema()

mcp = FastMCP("tplink", host="0.0.0.0", port=8000)

ROUTER_HOST = os.environ.get("TPLINK_ROUTER_HOST", "192.168.0.1")
ROUTER_USERNAME = os.environ.get("TPLINK_ROUTER_USERNAME", "admin")
ROUTER_PASSWORD = os.environ.get("TPLINK_ROUTER_PASSWORD", "")

SERVICE_TYPE = "network"
PROVIDER = "tplink_router"


def _get_status():
    if not ROUTER_PASSWORD:
        return None, "TPLINK_ROUTER_PASSWORD not configured"
    try:
        router = TplinkRouterProvider.get_client(ROUTER_HOST, ROUTER_PASSWORD, username=ROUTER_USERNAME)
        router.authorize()
        status = router.get_status()
        router.logout()
        return status, None
    except Exception as e:
        return None, f"could not reach/authenticate to the router: {e}"


def _device_dict(d) -> dict:
    transferring_now = bool((d.down_speed or 0) > 0 or (d.up_speed or 0) > 0)
    return {
        "hostname": d.hostname, "macaddr": d.macaddr, "ipaddr": d.ipaddr,
        "connection_type": str(d.type), "down_speed": d.down_speed, "up_speed": d.up_speed,
        "transferring_now": transferring_now,
        "online_time_seconds": d.online_time, "traffic_usage_bytes": d.traffic_usage,
        "router_reported_active": d.active,
    }


def _resolve_external_id(user_name: str) -> Optional[str]:
    return tools_devices.get_service_external_id(user_name, SERVICE_TYPE, PROVIDER)


def _network_access_error(requesting_user_name: str, target_user_name: str) -> Optional[str]:
    """Same shape as owntracks-mcp's _location_access_error, but checks the
    SEPARATE share_network_activity_with_family flag - being fine with
    network-presence visibility doesn't imply being fine with GPS location,
    or vice versa."""
    if requesting_user_name == target_user_name:
        return None
    if is_family_admin(requesting_user_name):
        return None
    if tools_routines.shares_network_activity_with_family(target_user_name):
        return None
    return (
        f"'{target_user_name}' hasn't opted in to share their network activity with the family "
        f"(and you're not a family admin, or them) - they can opt in with set_network_activity_sharing."
    )


@mcp.tool()
def list_network_devices(requesting_user_name: str) -> str:
    """Admin-only: every device the router currently knows about, whether
    or not it's linked to a Mentor user yet - the raw list (hostname, MAC,
    IP, connection type, live down/up speed, transferring_now) to use when
    deciding what to link via link_network_device. Note:
    `router_reported_active` is included for reference but is NOT a
    reliable presence signal (confirmed live - it stayed true for an idle
    device with no real IP); `transferring_now` (down_speed/up_speed > 0)
    is the one actually trustworthy "definitely active right now" signal,
    though a present-but-idle device will still show false for it."""
    if not is_family_admin(requesting_user_name):
        return json.dumps({"error": f"'{requesting_user_name}' is not a family admin - only a family "
                                     "admin can see the raw router device list."})
    status, err = _get_status()
    if err:
        return json.dumps({"error": err})
    return json.dumps([_device_dict(d) for d in status.devices])


@mcp.tool()
def link_network_device(requesting_user_name: str, target_user_name: str, mac_address: str,
                         device_name: Optional[str] = None) -> str:
    """Admin-only: link a MAC address (from list_network_devices) to a
    Mentor user's device, so get_network_activity can resolve it. Creates
    a new device for target_user_name if device_name doesn't already match
    one of theirs."""
    if not is_family_admin(requesting_user_name):
        return json.dumps({"error": f"'{requesting_user_name}' is not a family admin - only a family "
                                     "admin can link a network device."})
    existing = json.loads(tools_devices.list_devices({"user_name": target_user_name}))
    name = device_name or mac_address
    match = next((d for d in existing if d["name"] == name), None)
    if match:
        device_id = match["id"]
    else:
        created = json.loads(tools_devices.create_device(
            {"user_name": target_user_name, "name": name, "type": "network_device"}))
        if "error" in created:
            return json.dumps(created)
        device_id = created["id"]
    return tools_devices.attach_device_service({
        "device_id": device_id, "service_type": SERVICE_TYPE, "provider": PROVIDER,
        "external_id": mac_address.upper(),
    })


@mcp.tool()
def get_network_activity(requesting_user_name: str, target_user_name: str) -> str:
    """Is target_user_name's linked device currently online, and is it
    actually transferring data right now? requesting_user_name must be
    whoever you're actually talking to in THIS conversation, never a name
    they merely claim. Allowed when requesting_user_name == target_user_name,
    the requester is a family admin, or target_user_name has opted in via
    set_network_activity_sharing. Requires link_network_device to have been
    called for them first. Returns the same per-device fields as
    list_network_devices, for whichever of their linked device(s) the
    router currently sees - `transferring_now` is the one genuinely
    trustworthy "definitely active" signal; absence from the router's
    current device list, or transferring_now=false, does NOT reliably mean
    "not home" (could just be idle)."""
    err = _network_access_error(requesting_user_name, target_user_name)
    if err:
        return json.dumps({"error": err})
    mac = _resolve_external_id(target_user_name)
    if not mac:
        return json.dumps({"error": f"no network device linked for '{target_user_name}' yet - "
                                     "ask a family admin to call link_network_device first"})
    status, fetch_err = _get_status()
    if fetch_err:
        return json.dumps({"error": fetch_err})
    for d in status.devices:
        if d.macaddr.upper() == mac.upper():
            return json.dumps({"user_name": target_user_name, **_device_dict(d)})
    return json.dumps({"user_name": target_user_name, "seen": False,
                        "note": "this device is not currently in the router's client list at all"})


if __name__ == "__main__":
    import uvicorn

    # streamable-http, not sse - same reasoning as every other MCP server
    # in this stack: Hermes's MCP client POSTs directly to the given --url
    # rather than following the classic two-endpoint SSE handshake.
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
