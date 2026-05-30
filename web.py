#!/usr/bin/env python3
"""
web — chat-style web frontend. Reuses core.process_request() so it behaves
identically to the Telegram bot.

Auth (in order of richness):
  - No auth configured (no WEB_PASSWORD, no admin, no users) → page is open.
  - WEB_PASSWORD set → shared-code login; enter a name for attribution (or blank = Guest).
  - WEB_ADMIN_USER/WEB_ADMIN_PASSWORD set → an admin login (auto-approves, sees /admin).
  - Named users created on the /admin page (admin-provisioned) → per-person login.

The display name is set via APP_NAME (default "Chatarr").
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from core import process_request, perform_add, pop_pending, list_pending

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

WEB_PASSWORD       = os.environ.get("WEB_PASSWORD", "").strip()
PORT               = int(os.environ.get("WEB_PORT", "8080"))
APP_NAME           = os.environ.get("APP_NAME", "Chatarr")
WEB_ADMIN_USER     = os.environ.get("WEB_ADMIN_USER", "admin").strip()
WEB_ADMIN_PASSWORD = os.environ.get("WEB_ADMIN_PASSWORD", "").strip()
WEB_USERS_FILE     = Path("/data/web_users.json")

app = FastAPI(title=APP_NAME)

# sid -> {"name": str, "role": "admin"|"requester"}
_sessions: dict = {}


# ── User store (admin-provisioned, hashed) ──────────────────────────────────────

def _hash_code(code: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(salt), 120_000).hex()

def load_web_users() -> list:
    if WEB_USERS_FILE.exists():
        try:
            return json.loads(WEB_USERS_FILE.read_text())
        except Exception:
            return []
    return []

def save_web_users(users: list):
    WEB_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WEB_USERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(users))
    tmp.replace(WEB_USERS_FILE)

def add_web_user(name: str, code: str, role: str = "requester"):
    role = "admin" if role == "admin" else "requester"
    users = [u for u in load_web_users() if u["name"].lower() != name.lower()]
    salt = secrets.token_hex(16)
    users.append({"name": name, "role": role, "salt": salt, "hash": _hash_code(code, salt)})
    save_web_users(users)

def remove_web_user(name: str):
    save_web_users([u for u in load_web_users() if u["name"].lower() != name.lower()])

def verify_login(name: str, password: str):
    """Return (name, role) on success, else None."""
    name = (name or "").strip()
    if WEB_ADMIN_PASSWORD and name.lower() == WEB_ADMIN_USER.lower() \
            and secrets.compare_digest(password, WEB_ADMIN_PASSWORD):
        return (WEB_ADMIN_USER, "admin")
    for u in load_web_users():
        if u["name"].lower() == name.lower() \
                and secrets.compare_digest(_hash_code(password, u["salt"]), u["hash"]):
            return (u["name"], u["role"])
    # Shared-code fallback: anyone with WEB_PASSWORD logs in as a requester,
    # using whatever name they typed (or "Guest") — gives attribution without
    # a per-person account. Cannot grant admin.
    if WEB_PASSWORD and secrets.compare_digest(password, WEB_PASSWORD):
        return (name or "Guest", "requester")
    return None


# ── Auth helpers ────────────────────────────────────────────────────────────────

def _auth_required() -> bool:
    return bool(WEB_PASSWORD or WEB_ADMIN_PASSWORD or load_web_users())

def _get_sid(request: Request):
    return request.cookies.get("sid")

def _session(request: Request) -> dict:
    return _sessions.get(_get_sid(request) or "", {})

def _is_authed(request: Request) -> bool:
    if not _auth_required():
        return True
    return (_get_sid(request) or "") in _sessions

def _is_admin(request: Request) -> bool:
    return _session(request).get("role") == "admin"


STYLE = """
  :root { --bg:#0f1115; --panel:#171a21; --me:#2b6cb0; --bot:#222730; --text:#e6e8ec; --muted:#8a93a3; --accent:#4fd1c5; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
"""

CHAT_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>__APP_NAME__</title>
<style>
""" + STYLE + """
  #app { display:flex; flex-direction:column; height:100dvh; max-width:760px; margin:0 auto; }
  header { padding:14px 18px; border-bottom:1px solid #262b34; display:flex; align-items:center; gap:10px; }
  header .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); }
  header h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.2px; flex:1; }
  header small { color:var(--muted); font-weight:400; }
  header a { color:var(--accent); font-size:13px; text-decoration:none; }
  #log { flex:1; overflow-y:auto; padding:18px; display:flex; flex-direction:column; gap:12px; }
  .row { display:flex; }
  .row.me { justify-content:flex-end; }
  .bubble { max-width:80%; padding:10px 14px; border-radius:16px; white-space:pre-wrap; word-wrap:break-word; }
  .me .bubble { background:var(--me); border-bottom-right-radius:4px; }
  .bot .bubble { background:var(--bot); border-bottom-left-radius:4px; }
  .typing .bubble { color:var(--muted); font-style:italic; }
  form { display:flex; gap:8px; padding:12px; border-top:1px solid #262b34; }
  input[type=text] { flex:1; padding:12px 14px; border-radius:12px; border:1px solid #2b313b;
    background:var(--panel); color:var(--text); font-size:16px; outline:none; }
  input[type=text]:focus { border-color:var(--accent); }
  button { padding:0 18px; border:none; border-radius:12px; background:var(--accent); color:#06231f;
    font-weight:700; font-size:15px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
</style>
</head>
<body>
<div id="app">
  <header><span class="dot"></span><h1>__APP_NAME__ <small>&nbsp;web</small></h1>__ADMIN_LINK__</header>
  <div id="log">
    <div class="row bot"><div class="bubble">Hey! Ask me about movies and TV shows — e.g. "shows like The Boys", "Jason Statham movies", "add Severance", or "what's downloading?"</div></div>
  </div>
  <form id="f">
    <input type="text" id="msg" placeholder="Type a message…" autocomplete="off" autofocus>
    <button type="submit" id="send">Send</button>
  </form>
</div>
<script>
const log = document.getElementById('log');
const form = document.getElementById('f');
const input = document.getElementById('msg');
const send = document.getElementById('send');
function add(text, who) {
  const row = document.createElement('div');
  row.className = 'row ' + who;
  const b = document.createElement('div');
  b.className = 'bubble'; b.textContent = text;
  row.appendChild(b); log.appendChild(row);
  log.scrollTop = log.scrollHeight; return row;
}
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  add(text, 'me'); input.value = '';
  input.disabled = send.disabled = true;
  const typing = add('typing…', 'bot'); typing.classList.add('typing');
  try {
    const r = await fetch('/chat', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: text}) });
    if (r.status === 401) { window.location = '/login'; return; }
    const data = await r.json();
    typing.remove(); add(data.reply || '(no reply)', 'bot');
  } catch (err) { typing.remove(); add('Network error — please try again.', 'bot'); }
  finally { input.disabled = send.disabled = false; input.focus(); }
});
</script>
</body>
</html>""".replace("__APP_NAME__", APP_NAME)

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP_NAME__ — sign in</title>
<style>
""" + STYLE + """
  body{display:flex;align-items:center;justify-content:center;}
  form{background:var(--panel);padding:28px;border-radius:16px;border:1px solid #262b34;width:300px;}
  h1{font-size:16px;margin:0 0 16px;}
  input{width:100%;padding:12px;border-radius:10px;border:1px solid #2b313b;background:var(--bg);
    color:var(--text);font-size:16px;margin-bottom:12px;}
  button{width:100%;padding:12px;border:none;border-radius:10px;background:var(--accent);color:#06231f;
    font-weight:700;font-size:15px;cursor:pointer;}
  .err{color:#f56565;font-size:13px;margin-bottom:10px;}
  .hint{color:var(--muted);font-size:12px;margin:-4px 0 12px;}
</style></head>
<body>
<form method="post" action="/login">
  <h1>__APP_NAME__</h1>
  __ERR__
  __NAME__
  <input type="password" name="password" placeholder="Access code" autofocus>
  <button type="submit">Enter</button>
</form>
</body></html>""".replace("__APP_NAME__", APP_NAME)


def _login_html(error: str = "") -> str:
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    name_field = ('<input type="text" name="name" placeholder="Your name (optional)" autocomplete="username">'
                  '<div class="hint">Your name shows on requests. Leave blank to use a shared code.</div>')
    return LOGIN_PAGE.replace("__ERR__", err).replace("__NAME__", name_field)


# ── Routes ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    admin_link = '<a href="/admin">admin</a>' if _is_admin(request) else ""
    page = CHAT_PAGE.replace("__ADMIN_LINK__", admin_link)
    resp = HTMLResponse(page)
    if not _get_sid(request) and not _auth_required():
        resp.set_cookie("sid", "web_" + secrets.token_hex(16), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not _auth_required():
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_login_html())


@app.post("/login")
def login(request: Request, password: str = Form(""), name: str = Form("")):
    if not _auth_required():
        return RedirectResponse("/", status_code=302)
    who = verify_login(name, password)
    if not who:
        return HTMLResponse(_login_html("Wrong name or code — try again."), status_code=401)
    sid = _get_sid(request) or ("web_" + secrets.token_hex(16))
    _sessions[sid] = {"name": who[0], "role": who[1]}
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/logout")
def logout(request: Request):
    _sessions.pop(_get_sid(request) or "", None)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("sid")
    return resp


@app.post("/chat")
async def chat(request: Request):
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sid = _get_sid(request) or ("web_" + secrets.token_hex(16))
    sess = _sessions.get(sid, {})
    name = sess.get("name", "A web user")
    is_admin = sess.get("role") == "admin"
    body = await request.json()
    text = (body.get("message") or "").strip()
    if not text:
        return JSONResponse({"reply": "Say something and I'll help."})
    try:
        reply = await asyncio.to_thread(process_request, sid, text, name, is_admin)
    except Exception as e:
        log.error("process_request failed: %s", e)
        reply = "Something went wrong — please try again."
    resp = JSONResponse({"reply": reply})
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


# ── Admin page ──────────────────────────────────────────────────────────────────

def _render_admin() -> str:
    def esc(s):
        return html.escape(str(s))
    pend = list_pending()
    if pend:
        rows = ""
        for e in pend:
            where = "Radarr" if e.get("media_type") == "movie" else "Sonarr"
            rows += (f'<div class="item"><div><b>{esc(e.get("title","?"))}</b> '
                     f'<span class="muted">— {esc(e.get("requester","?"))} → {where}</span></div>'
                     f'<div class="acts">'
                     f'<form method="post" action="/admin/approve"><input type="hidden" name="pid" value="{esc(e.get("id"))}"><button class="ok">Approve</button></form>'
                     f'<form method="post" action="/admin/deny"><input type="hidden" name="pid" value="{esc(e.get("id"))}"><button class="no">Deny</button></form>'
                     f'</div></div>')
        pending_html = rows
    else:
        pending_html = '<p class="muted">No pending requests.</p>'

    users = load_web_users()
    if users:
        urows = ""
        for u in users:
            urows += (f'<div class="item"><div>{esc(u["name"])} <span class="muted">({esc(u["role"])})</span></div>'
                      f'<form method="post" action="/admin/removeuser"><input type="hidden" name="name" value="{esc(u["name"])}"><button class="no">Remove</button></form>'
                      f'</div>')
        users_html = urows
    else:
        users_html = '<p class="muted">No named users yet. Add one below, or share the access code for guest logins.</p>'

    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP_NAME__ — admin</title><style>""" + STYLE + """
  .wrap{max-width:680px;margin:0 auto;padding:18px;}
  h1{font-size:18px;} h2{font-size:15px;margin-top:28px;border-bottom:1px solid #262b34;padding-bottom:6px;}
  a{color:var(--accent);text-decoration:none;}
  .muted{color:var(--muted);}
  .item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid #1d222b;}
  .acts{display:flex;gap:8px;}
  form.add{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}
  input,select{padding:10px;border-radius:10px;border:1px solid #2b313b;background:var(--panel);color:var(--text);font-size:15px;}
  button{padding:8px 14px;border:none;border-radius:10px;background:var(--accent);color:#06231f;font-weight:700;cursor:pointer;}
  button.no{background:#3a3f4a;color:var(--text);} button.ok{background:var(--accent);}
</style></head><body><div class="wrap">
  <h1>__APP_NAME__ admin · <a href="/">← back to chat</a> · <a href="/logout">log out</a></h1>
  <h2>Pending requests</h2>
""" + pending_html + """
  <h2>People</h2>
""" + users_html + """
  <form class="add" method="post" action="/admin/adduser">
    <input type="text" name="name" placeholder="Name" required>
    <input type="text" name="code" placeholder="Their access code" required>
    <select name="role"><option value="requester">requester</option><option value="admin">admin</option></select>
    <button type="submit">Add person</button>
  </form>
  <p class="muted" style="margin-top:18px;font-size:13px;">Requesters' adds wait here for your approval (you also get them on Telegram). Your own requests download straight away.</p>
</div></body></html>""").replace("__APP_NAME__", APP_NAME)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(_render_admin())


@app.post("/admin/adduser")
def admin_adduser(request: Request, name: str = Form(...), code: str = Form(...), role: str = Form("requester")):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    if name.strip() and code:
        add_web_user(name.strip(), code, role)
        log.info("admin added web user '%s' (%s)", name.strip(), role)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/removeuser")
def admin_removeuser(request: Request, name: str = Form(...)):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    remove_web_user(name)
    # Drop any active sessions for that user.
    for sid, s in list(_sessions.items()):
        if s.get("name", "").lower() == name.lower():
            _sessions.pop(sid, None)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/approve")
def admin_approve(request: Request, pid: str = Form(...)):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    entry = pop_pending(pid)
    if entry:
        log.info("admin approved '%s' → %s", entry.get("title"), perform_add(entry)[:80])
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/deny")
def admin_deny(request: Request, pid: str = Form(...)):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    entry = pop_pending(pid)
    if entry:
        log.info("admin denied '%s'", entry.get("title"))
    return RedirectResponse("/admin", status_code=302)


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    log.info("%s web starting on :%d (auth: %s)", APP_NAME, PORT, "on" if _auth_required() else "OFF (open)")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
