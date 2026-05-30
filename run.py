#!/usr/bin/env python3
"""
run — container entrypoint that launches the selected frontend(s).

FRONTENDS env (comma-separated): "web", "telegram", or "web,telegram".
Default: "web".

Each selected frontend runs as a child process. If any child exits, the whole
container exits so the orchestrator's restart policy brings everything back up
together.
"""

import os
import signal
import subprocess
import sys
import time

raw = os.environ.get("FRONTENDS", "web").lower().replace(" ", "")
aliases = {"tg": "telegram", "bot": "telegram"}
selected = {aliases.get(p, p) for p in raw.split(",") if p}

valid = {"web", "telegram"}
unknown = selected - valid
if unknown:
    sys.exit(f"FRONTENDS: unknown value(s) {sorted(unknown)}. Use 'web', 'telegram', or 'web,telegram'.")
if not selected:
    sys.exit("FRONTENDS is empty. Set it to 'web', 'telegram', or 'web,telegram'.")
if "telegram" in selected and not os.environ.get("TELEGRAM_TOKEN"):
    sys.exit("FRONTENDS includes 'telegram' but TELEGRAM_TOKEN is not set.")

plan = []
if "telegram" in selected:
    plan.append(("telegram", [sys.executable, "bot.py"]))
if "web" in selected:
    plan.append(("web", [sys.executable, "web.py"]))

print(f"[run] starting frontend(s): {', '.join(name for name, _ in plan)}", flush=True)
procs = [(name, subprocess.Popen(cmd)) for name, cmd in plan]


def _terminate(*_):
    for _, p in procs:
        if p.poll() is None:
            p.terminate()


signal.signal(signal.SIGTERM, _terminate)
signal.signal(signal.SIGINT, _terminate)

try:
    while True:
        for name, p in procs:
            ret = p.poll()
            if ret is not None:
                print(f"[run] frontend '{name}' exited with code {ret}; shutting the rest down.", flush=True)
                _terminate()
                for _, q in procs:
                    try:
                        q.wait(timeout=10)
                    except Exception:
                        q.kill()
                sys.exit(ret or 1)
        time.sleep(2)
except KeyboardInterrupt:
    _terminate()
    sys.exit(0)
