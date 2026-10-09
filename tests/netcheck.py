"""Which servers of the registered sites accept a connection right now?
Only a TCP handshake per server address, no page request.

    uv run python -m tests.netcheck
"""
import socket
import sys
import time
from collections import defaultdict
from datetime import datetime

from scanner.config import RunConfig
from scanner.sources import quellen_fuer

sys.stdout.reconfigure(encoding="utf-8")
by_ip: dict[str, list[str]] = defaultdict(list)
for q in quellen_fuer(RunConfig().kantone):
    try:
        by_ip[socket.gethostbyname(q.host)].append(q.host.removeprefix("www."))
    except OSError:
        by_ip["DNS-Fehler"].append(q.host)
print(datetime.now().strftime("%H:%M:%S"))
for ip, hosts in sorted(by_ip.items()):
    state = "–"
    if ip[0].isdigit():
        t = time.time()
        try:
            socket.create_connection((ip, 443), timeout=6).close()
            state = f"offen ({time.time() - t:.2f}s)"
        except OSError as e:
            state = f"ZU ({type(e).__name__})"
    print(f"{ip:16} {state:22} {', '.join(hosts)}")
