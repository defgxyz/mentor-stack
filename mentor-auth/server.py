"""
Telegram-based second factor, gating the Mentor dashboard behind Caddy's
basic_auth (factor 1: username/password) + this service (factor 2: a 6-digit
code delivered to the paired Telegram chat), checked via Caddy's forward_auth.

Session and OTP-pending tokens are opaque random ids mapped to state in a
tiny local JSON file (survives restarts) - not JWTs, no crypto libraries
needed beyond stdlib `secrets`/`hmac`. Single-user, low-volume, so this
stays intentionally simple rather than pulling in a real session store.
"""

import hmac
import json
import os
import secrets
import time
import urllib.request
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DATA_PATH = Path("/app/data/state.json")

SESSION_TTL = 30 * 24 * 3600  # 30 days
OTP_TTL = 5 * 60  # 5 minutes

app = FastAPI()


def _load_state() -> dict:
    if not DATA_PATH.exists():
        return {"sessions": {}, "pending": {}}
    try:
        return json.loads(DATA_PATH.read_text())
    except Exception:
        return {"sessions": {}, "pending": {}}


def _save_state(state: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(state))


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    state = _load_state()
    entry = state["sessions"].get(token)
    if not entry:
        return False
    return entry["expires_at"] > time.time()


@app.get("/check")
def check(request: Request):
    token = request.cookies.get("mentor_session")
    if _valid_session(token):
        return Response(status_code=200)
    # 302, not 401 - Caddy's forward_auth relays this straight to the
    # browser, which follows it to the OTP page (excluded from forward_auth).
    return RedirectResponse(url="/login-otp", status_code=302)


_FORM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Verify</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 380px; margin: 80px auto;
          padding: 0 20px; color: #1a1a1a; background: #fafafa; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #17181c; color: #e8e8ea; }} input {{ background:#21232b; color:#eee; border-color:#444 !important; }} }}
  h1 {{ font-size: 18px; }}
  input {{ font-size: 22px; letter-spacing: 4px; text-align: center; width: 100%;
           padding: 10px; margin: 16px 0; border-radius: 8px; border: 1px solid #ccc; box-sizing: border-box; }}
  button {{ width: 100%; padding: 10px; border-radius: 8px; border: none; background: #2563eb;
            color: #fff; font-size: 15px; cursor: pointer; }}
  p.err {{ color: #dc2626; font-size: 13px; }}
  p.muted {{ color: #888; font-size: 13px; }}
</style></head><body>
  <h1>Enter the code sent to Telegram</h1>
  {error}
  <form method="post" action="/verify-otp">
    <input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" autofocus>
    <button type="submit">Verify</button>
  </form>
  <p class="muted">Code expires in 5 minutes. Reload this page to send a new one.</p>
</body></html>"""


@app.get("/login-otp", response_class=HTMLResponse)
def login_otp(request: Request, response: Response):
    pending_token = request.cookies.get("mentor_pending")
    state = _load_state()
    entry = state["pending"].get(pending_token) if pending_token else None

    if not entry or entry["expires_at"] <= time.time():
        code = f"{secrets.randbelow(1_000_000):06d}"
        pending_token = _new_token()
        state["pending"][pending_token] = {"code": code, "expires_at": time.time() + OTP_TTL}
        _save_state(state)
        _send_telegram(f"Mentor dashboard login code: {code}")

    resp = HTMLResponse(_FORM_PAGE.format(error=""))
    # domain=.defg.xyz (not host-only) so one login covers mentor.defg.xyz and
    # hermes.defg.xyz both, rather than needing 2FA twice for the same person.
    resp.set_cookie("mentor_pending", pending_token, max_age=OTP_TTL, httponly=True,
                     samesite="lax", domain=".defg.xyz")
    return resp


@app.post("/verify-otp")
async def verify_otp(request: Request):
    form = await request.form()
    code = (form.get("code") or "").strip()
    pending_token = request.cookies.get("mentor_pending")

    state = _load_state()
    entry = state["pending"].get(pending_token) if pending_token else None

    if not entry or entry["expires_at"] <= time.time() or not hmac.compare_digest(entry["code"], code):
        return HTMLResponse(_FORM_PAGE.format(error='<p class="err">Wrong or expired code.</p>'), status_code=401)

    state["pending"].pop(pending_token, None)
    session_token = _new_token()
    state["sessions"][session_token] = {"expires_at": time.time() + SESSION_TTL}
    _save_state(state)

    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie("mentor_session", session_token, max_age=SESSION_TTL, httponly=True,
                     samesite="lax", domain=".defg.xyz")
    resp.delete_cookie("mentor_pending", domain=".defg.xyz")
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}
