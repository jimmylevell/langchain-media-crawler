"""Call the locally hosted agent (Responses protocol on port 8088).

    uv run python -m langchain_azure_ai.agents.hosting.run --protocol responses   # terminal 1
    uv run python -m tests.call_local_agent "Scan starten"                        # terminal 2
"""
from __future__ import annotations

import sys
import time

import httpx


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    text = sys.argv[1] if len(sys.argv) > 1 else "Scan starten"
    for _ in range(30):  # wait for the server to come up
        try:
            httpx.get("http://localhost:8088/", timeout=2)
            break
        except httpx.HTTPError:
            time.sleep(2)
    start = time.time()
    r = httpx.post("http://localhost:8088/responses", json={"input": text, "stream": False}, timeout=1800)
    data = r.json()
    print(f"HTTP {r.status_code}, Status {data.get('status')}, {time.time() - start:.0f} s\n")
    for item in data.get("output", []):
        for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
            print(part.get("text", ""))
    if r.status_code != 200 or not data.get("output"):
        print(str(data)[:2000])


if __name__ == "__main__":
    main()
