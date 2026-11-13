"""TTS voice selection: lets a person pick their own Piper voice (see
voices.py for the fixed catalog) from the fixed library, written directly
into their own Hermes profile's config.yaml (tts.piper.voice) via the same
/api/fs/read-text /write-text mechanism used throughout this project - no
new mount, no new infrastructure.

Access model: simpler than personas.py's (no age-based self-choice gate,
since voice choice isn't personality/content-sensitive the way a persona
is) - the person themselves, or any family admin, may set it. Unlike most
config.yaml edits in this project, this does NOT require a gateway restart
to take effect for THAT PERSON's next reply - confirmed live:
_load_piper_voice_for_config (tools/tts_tool_local.py) re-reads
tts_config fresh on every synthesis call via get_config(), it is not
cached at gateway-startup time the way the memory/model providers are.
"""
import json
import os

from . import db
from . import voices
from .tools_routines import is_family_admin

HERMES_DASHBOARD_URL = os.environ.get("HERMES_DASHBOARD_URL", "http://hermes:9119")
HERMES_DASHBOARD_USERNAME = os.environ.get("HERMES_DASHBOARD_USERNAME", "")
HERMES_DASHBOARD_PASSWORD = os.environ.get("HERMES_DASHBOARD_PASSWORD", "")


def _hermes_profile_for_user(user_name: str) -> str:
    return user_name.strip().lower()


def list_available_voices(args: dict, **kwargs) -> str:
    """Every selectable Piper voice, plus this user's current one (null
    meaning "on the default"). Call this before set_tts_voice to get a
    real voice_key - never guess one."""
    con = db.connect()
    row = con.execute("SELECT tts_voice FROM users WHERE name = ?", (args["user_name"],)).fetchone()
    con.close()
    if row is None:
        return json.dumps({"error": f"no user named '{args['user_name']}'"})
    return json.dumps({
        "user_name": args["user_name"], "current_voice_key": row[0],
        "default_voice_key": voices.DEFAULT_VOICE, "options": voices.voice_options(),
    })


def _write_voice_to_config(profile: str, voice_key: str) -> "str | None":
    """None on success, else an error string. Never raises."""
    if not HERMES_DASHBOARD_PASSWORD:
        return "HERMES_DASHBOARD_PASSWORD not configured - could not reach Hermes"
    try:
        import httpx
        import yaml
        with httpx.Client(base_url=HERMES_DASHBOARD_URL, timeout=20) as client:
            login = client.post("/auth/password-login", json={
                "provider": "basic", "username": HERMES_DASHBOARD_USERNAME, "password": HERMES_DASHBOARD_PASSWORD,
            })
            if login.status_code != 200:
                return f"could not authenticate to Hermes ({login.status_code})"
            path = f"/opt/data/profiles/{profile}/config.yaml"
            read_resp = client.get("/api/fs/read-text", params={"path": path})
            if read_resp.status_code != 200:
                return f"could not read config.yaml ({read_resp.status_code})"
            try:
                cfg = yaml.safe_load(read_resp.json().get("text", "")) or {}
            except yaml.YAMLError as e:
                return f"config.yaml did not parse as YAML: {e}"
            tts_cfg = cfg.setdefault("tts", {})
            tts_cfg["provider"] = "piper"
            tts_cfg.setdefault("piper", {})["voice"] = voice_key
            write_resp = client.post("/api/fs/write-text", json={
                "path": path, "content": yaml.safe_dump(cfg, sort_keys=False)})
            if write_resp.status_code != 200:
                return f"could not write config.yaml ({write_resp.status_code})"
        return None
    except Exception as e:
        return f"failed to update config.yaml: {e}"


def set_tts_voice(args: dict, **kwargs) -> str:
    """Set (or reset, with voice_key=null) target_user_name's Piper TTS
    voice - writes directly into THEIR OWN config.yaml, takes effect on
    their very next voice reply (no restart needed - see this module's
    docstring). requesting_user_name must be whoever you're actually
    talking to in THIS conversation, never a claimed identity. Only the
    person themselves, or a family admin, may call this."""
    requesting_user_name = args["requesting_user_name"]
    target_user_name = args["target_user_name"]
    voice_key = args.get("voice_key") or voices.DEFAULT_VOICE

    con = db.connect()
    target_row = con.execute("SELECT id FROM users WHERE name = ?", (target_user_name,)).fetchone()
    if target_row is None:
        con.close()
        return json.dumps({"error": f"no user named '{target_user_name}'"})
    target_id = target_row[0]

    is_admin = is_family_admin(requesting_user_name)
    is_self = requesting_user_name.strip().lower() == target_user_name.strip().lower()
    if not is_admin and not is_self:
        con.close()
        return json.dumps({"error": f"'{requesting_user_name}' cannot set a voice for '{target_user_name}' "
                                     f"- only a family admin, or the person themselves, can"})

    if voice_key not in voices.VOICES:
        con.close()
        return json.dumps({"error": f"unknown voice_key '{voice_key}' - call list_available_voices first"})

    profile = _hermes_profile_for_user(target_user_name)
    error = _write_voice_to_config(profile, voice_key)
    if error:
        con.close()
        return json.dumps({"error": error})

    con.execute("UPDATE users SET tts_voice = ? WHERE id = ?", (voice_key, target_id))
    con.commit()
    con.close()
    return json.dumps({"ok": True, "user_name": target_user_name, "voice_key": voice_key,
                        "voice_label": voices.VOICES[voice_key]["label"]})
