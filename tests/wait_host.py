"""Wait until a host answers again: one request every 3 minutes, at most 45 minutes.

    uv run python -m tests.wait_host www.nw.ch
"""
import sys
import time
from datetime import datetime

import httpx

from scanner.config import USER_AGENT

host = sys.argv[1] if len(sys.argv) > 1 else "www.nw.ch"
for _ in range(15):
    try:
        r = httpx.get(f"https://{host}/", headers={"User-Agent": USER_AGENT}, timeout=12)
        print(datetime.now().strftime("%H:%M:%S"), host, "erreichbar", r.status_code, flush=True)
        break
    except httpx.HTTPError as e:
        print(datetime.now().strftime("%H:%M:%S"), host, type(e).__name__, flush=True)
        time.sleep(180)
