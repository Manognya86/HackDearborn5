"""Start LIFELOG behind a Cloudflare quick tunnel, so QR codes and shared links open on any phone, on any network.

    python scripts/run_public.py                 # app on port 8000, database from .env
    python scripts/run_public.py --port 8001     # another port (set DATABASE_URL first for the local database)

Needs cloudflared (https://github.com/cloudflare/cloudflared/releases): on PATH, in ../tools/cloudflared.exe, or at
CLOUDFLARED=<path>. A quick tunnel needs no Cloudflare account; its https://….trycloudflare.com address changes every
start, so this script reads it and starts the app with PUBLIC_URL set to it. Ctrl+C stops both. Anyone with the address
can open the app while it runs: stop it after the demo."""
import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_cloudflared() -> str:
    for c in (os.getenv("CLOUDFLARED"), shutil.which("cloudflared"), str(ROOT.parent / "tools" / "cloudflared.exe")):
        if c and Path(c).exists():
            return c
    sys.exit("cloudflared not found: download it from https://github.com/cloudflare/cloudflared/releases "
             "and put it on PATH, in tools/cloudflared.exe next to the repo, or set CLOUDFLARED=<path>.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    tunnel = subprocess.Popen([find_cloudflared(), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{args.port}"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    url, deadline = None, time.time() + 60
    for line in tunnel.stdout:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
        if time.time() > deadline:
            break
    if not url:
        tunnel.terminate()
        sys.exit("The tunnel didn't start (no trycloudflare.com address within 60 s). Check the internet connection.")
    threading.Thread(target=lambda: [None for _ in tunnel.stdout], daemon=True).start()   # keep draining its log

    bar = "=" * 72
    print(f"\n{bar}\n  LIFELOG public address (QR codes and shared links use it):\n\n    {url}\n\n"
          f"  On this computer you can also use http://localhost:{args.port}\n  Ctrl+C stops the app and the tunnel.\n{bar}\n", flush=True)
    env = {**os.environ, "PUBLIC_URL": url}
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "lifelog.app:app", "--app-dir", str(ROOT), "--host", "0.0.0.0",
                            "--port", str(args.port), "--proxy-headers"], env=env, cwd=ROOT)   # the tunnel connects from 127.0.0.1, trusted by default
    try:
        app.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in (app, tunnel):
            if p.poll() is None:
                p.terminate()
        print("stopped the app and the tunnel")


if __name__ == "__main__":
    main()
