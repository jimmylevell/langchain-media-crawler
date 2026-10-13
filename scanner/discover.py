"""Outlets that are not in the registry.

A search server (SearXNG, set SEARXNG_URL) is asked for recent news about the region. Articles of
outlets we do not know are collected like those of a registered outlet, and the outlets are listed in
the report, so a regular one can be added to sources.py. Without SEARXNG_URL discovery is off.

Public search APIs were tried and dropped: Google News forbids automated clients in its robots.txt,
GDELT has next to no Swiss regional papers, and Bing grounding ranks poorly for local news."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .config import SEARXNG_URL, RunConfig
from .parsers import Zeile, parse_date
from .sources import KANTONSWORTE
from .tools import RunLog

QUERIES = ("{} Unternehmen", "{} Wirtschaft Investition", "{} Nachfolge Jubiläum Eröffnung")  # per canton
PAUSE = 1.0


@dataclass
class Entdeckt:
    host: str
    treffer: int  # articles of this outlet in the search results


def discover(cfg: RunConfig, log: RunLog, bekannt: set[str]) -> tuple[list[tuple[str, Zeile]], list[Entdeckt]]:
    """Returns (articles of unknown outlets as (host, row), the outlets). `bekannt`: hosts that are already collected."""
    if not SEARXNG_URL:
        return [], []
    articles: dict[str, tuple[str, Zeile]] = {}
    counts: dict[str, int] = {}
    for kanton in cfg.kantone:
        name = next(iter(KANTONSWORTE.get(kanton.upper(), ())), "")
        for query in (q.format(name) for q in QUERIES if name):
            params = {"q": query, "format": "json", "language": "de-CH", "categories": "news", "time_range": "week" if cfg.tage <= 7 else "month"}
            try:  # the search server is configured by the operator and may be local, so it bypasses the public-address check
                results = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=25).json().get("results", [])
            except (httpx.HTTPError, ValueError) as e:
                log.mark_blocked("Websuche", f"Entdeckung fehlgeschlagen ({type(e).__name__})")
                continue
            finally:
                time.sleep(PAUSE)
            with log.lock:
                log.searches += 1
            for r in results:
                url, host = r.get("url") or "", (urlsplit(r.get("url") or "").hostname or "").removeprefix("www.")
                if host and host not in bekannt and url not in articles:
                    counts[host] = counts.get(host, 0) + 1
                    articles[url] = (host, (parse_date(r.get("publishedDate") or ""), r.get("title") or "", url, r.get("content") or ""))
    return list(articles.values()), [Entdeckt(h, n) for h, n in sorted(counts.items(), key=lambda kv: -kv[1])]
