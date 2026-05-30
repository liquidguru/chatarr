#!/usr/bin/env python3
"""
web — chat-style web frontend. Reuses core.process_request() so it behaves
identically to the Telegram bot.

Auth: if WEB_PASSWORD is set in the environment, visitors must enter it once
(stored against their session). If it's unset, the page is open — only do that
behind a trusted tunnel / on the LAN.

The display name is set via the APP_NAME env var (default "Chatarr").
"""

import asyncio
import logging
import os
import secrets

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from core import process_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "").strip()
PORT = int(os.environ.get("WEB_PORT", "8080"))
APP_NAME = os.environ.get("APP_NAME", "Chatarr")

app = FastAPI(title=APP_NAME)

# Session ids that have cleared the password gate (in-memory; cleared on restart).
_authed: set = set()


def _get_sid(request: Request) -> str | None:
    return request.cookies.get("sid")


def _is_authed(sid: str | None) -> bool:
    if not WEB_PASSWORD:
        return True
    return sid is not None and sid in _authed


CHAT_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>__APP_NAME__</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --me:#2b6cb0; --bot:#222730; --text:#e6e8ec; --muted:#8a93a3; --accent:#4fd1c5; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  #app { display:flex; flex-direction:column; height:100dvh; max-width:760px; margin:0 auto; }
  header { padding:14px 18px; border-bottom:1px solid #262b34; display:flex; align-items:center; gap:10px; }
  header .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); }
  header h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.2px; }
  header small { color:var(--muted); font-weight:400; }
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
  .hint { color:var(--muted); padding:0 18px 8px; font-size:13px; }
</style>
</head>
<body>
<div id="app">
  <header><span class="dot"></span><h1>__APP_NAME__ <small>&nbsp;web</small></h1></header>
  <div id="log">
    <div class="row bot"><div class="bubble">Hey! Ask me about movies and TV shows — e.g. "shows like The Boys", "Jason Statham movies", "add Severance", or "what's downloading?"</div></div>
  </div>
  <div class="hint" id="hint"></div>
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
  b.className = 'bubble';
  b.textContent = text;
  row.appendChild(b);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  add(text, 'me');
  input.value = '';
  input.disabled = send.disabled = true;
  const typing = add('typing…', 'bot');
  typing.classList.add('typing');
  try {
    const r = await fetch('/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: text})
    });
    if (r.status === 401) { window.location = '/login'; return; }
    const data = await r.json();
    typing.remove();
    add(data.reply || '(no reply)', 'bot');
  } catch (err) {
    typing.remove();
    add('Network error — please try again.', 'bot');
  } finally {
    input.disabled = send.disabled = false;
    input.focus();
  }
});
</script>
</body>
</html>"""

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP_NAME__ — sign in</title>
<style>
  html,body{margin:0;height:100%;background:#0f1115;color:#e6e8ec;
    font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    display:flex;align-items:center;justify-content:center;}
  form{background:#171a21;padding:28px;border-radius:16px;border:1px solid #262b34;width:300px;}
  h1{font-size:16px;margin:0 0 16px;}
  input{width:100%;padding:12px;border-radius:10px;border:1px solid #2b313b;background:#0f1115;
    color:#e6e8ec;font-size:16px;margin-bottom:12px;}
  button{width:100%;padding:12px;border:none;border-radius:10px;background:#4fd1c5;color:#06231f;
    font-weight:700;font-size:15px;cursor:pointer;}
  .err{color:#f56565;font-size:13px;margin-bottom:10px;}
</style></head>
<body>
<form method="post" action="/login">
  <h1>__APP_NAME__</h1>
  __ERR__
  <input type="password" name="password" placeholder="Access code" autofocus>
  <button type="submit">Enter</button>
</form>
</body></html>"""

# Bake the configured app name into the templates once at startup.
CHAT_PAGE = CHAT_PAGE.replace("__APP_NAME__", APP_NAME)
LOGIN_PAGE = LOGIN_PAGE.replace("__APP_NAME__", APP_NAME)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    sid = _get_sid(request)
    if not _is_authed(sid):
        return RedirectResponse("/login", status_code=302)
    resp = HTMLResponse(CHAT_PAGE)
    if not sid:
        resp.set_cookie("sid", "web_" + secrets.token_hex(16), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not WEB_PASSWORD:
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(LOGIN_PAGE.replace("__ERR__", ""))


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if not WEB_PASSWORD:
        return RedirectResponse("/", status_code=302)
    if not secrets.compare_digest(password, WEB_PASSWORD):
        page = LOGIN_PAGE.replace("__ERR__", '<div class="err">Wrong code — try again.</div>')
        return HTMLResponse(page, status_code=401)
    sid = _get_sid(request) or ("web_" + secrets.token_hex(16))
    _authed.add(sid)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/chat")
async def chat(request: Request):
    sid = _get_sid(request)
    if not _is_authed(sid):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not sid:
        sid = "web_" + secrets.token_hex(16)
    body = await request.json()
    text = (body.get("message") or "").strip()
    if not text:
        return JSONResponse({"reply": "Say something and I'll help."})
    try:
        reply = await asyncio.to_thread(process_request, sid, text, "A web user")
    except Exception as e:
        log.error("process_request failed: %s", e)
        reply = "Something went wrong — please try again."
    resp = JSONResponse({"reply": reply})
    # Ensure the session cookie persists even if the browser arrived without one.
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    log.info("%s web starting on :%d (password gate: %s)", APP_NAME, PORT, "on" if WEB_PASSWORD else "off")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
