"""Network access and the tools the research agents can call.

Nothing here needs an API key. All requests go through `polite_get`, which
serialises requests per hosting network and records in a shared RunLog what
was read and what was blocked. The self-check later uses that log to verify
that a cited URL was really read.
"""
from __future__ import annotations

import functools
import os
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import lxml.html
import trafilatura
from langchain_core.tools import tool

from . import cache
from .config import SEARXNG_URL, USER_AGENT, RunConfig

BLOCK_STATUS = {401, 402, 403, 429, 451}
PAYWALL_MARKERS = ("paywall", "piano-", "abo abschliessen", "jetzt abonnieren", "mit abo weiterlesen", "abopflichtig")
UNREACHABLE = "nicht erreichbar"
PAUSE = 1.5  # seconds between two requests to the same hosting network
PAUSE_COLLECTED = 5.0  # slower on the shared canton/municipality platform, which blocks bursts
ROBOTS_CACHE_HOURS = 7 * 24
PAGE_CACHE_HOURS = 3
# Requests per server and hour on collected hosts. Measured: a server closes at about 30.
SERVER_BUDGET = int(os.getenv("SERVER_BUDGET_PER_HOUR", "20"))
PAYWALL_TEASER = "Paywall: nur Titel und Anriss gelesen"
BUDGET = "Anfragebudget pro Server erreicht (Schutz vor Sperre); ein späterer Lauf liest weiter"


def norm_url(url: str) -> str:
    """Normalise for comparison. The fragment is kept: SHAB detail links live in it."""
    p = urlsplit(url.strip())
    return f"{p.scheme.lower()}://{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}" + (
        f"?{p.query}" if p.query else ""
    ) + (f"#{p.fragment}" if p.fragment else "")


@functools.lru_cache(maxsize=512)
def network_of(netloc: str) -> str:
    """Throttling key: the server address a host resolves to. Many public-sector sites of a
    region share a few servers at one hoster, and each server limits requests per client."""
    try:
        return socket.gethostbyname(netloc.split(":")[0])
    except OSError:
        return netloc


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


@dataclass
class RunLog:
    """Shared by the collector and all agents of one run (thread-safe): the HTTP client
    and what actually happened on the network."""

    read_urls: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)  # portals that answered
    blocked: dict[str, str] = field(default_factory=dict)
    searches: int = 0
    # Circuit breaker: a host that timed out is not asked again; two failing hosts in a row
    # on one server mean that server is dropping us, so it is left alone for the run.
    down_hosts: set[str] = field(default_factory=set)
    net_failures: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Set by the collector: on these hosts agents may only open the collected entries.
    collected_hosts: set[str] = field(default_factory=set)
    collected_urls: set[str] = field(default_factory=set)
    budget_hosts: set[str] = field(default_factory=set)  # collected hosts with a tight request limit
    teasers: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # url -> (title, date, teaser) from feeds
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    robots: dict[str, RobotFileParser | None] = field(default_factory=dict, repr=False)
    host_locks: dict[str, threading.Lock] = field(default_factory=lambda: defaultdict(threading.Lock), repr=False)
    client: httpx.Client = field(
        default_factory=lambda: httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "de-CH,de;q=0.9"}, follow_redirects=True, timeout=25
        ),
        repr=False,
    )

    def mark_read(self, *urls: str) -> None:
        with self.lock:
            self.read_urls.update(norm_url(u) for u in urls)

    def mark_ok(self, host: str) -> None:
        with self.lock:
            self.hosts.add(host)
            if self.blocked.get(host, "").startswith(UNREACHABLE):  # a timeout earlier, fine now
                del self.blocked[host]

    def mark_blocked(self, source: str, reason: str) -> None:
        with self.lock:
            if not (reason.startswith(UNREACHABLE) and source in self.hosts):
                self.blocked.setdefault(source, reason)

    def was_read(self, url: str) -> bool:
        return norm_url(url) in self.read_urls


def _allowed(log: RunLog, url: str, pause: float) -> bool:
    """robots.txt check. The file is cached on disk and its request is paced like any
    other, so a new host never causes two requests in quick succession."""
    p = urlsplit(url)
    base = f"{p.scheme}://{p.netloc}"
    with log.lock:
        known = base in log.robots
    if not known:
        rp: RobotFileParser | None = None
        text = cache.get(base + "/robots.txt", ROBOTS_CACHE_HOURS)
        if text is None:
            try:
                r = log.client.get(base + "/robots.txt", timeout=10)
                text = r.text if r.status_code == 200 else ""
                cache.put(base + "/robots.txt", text)
            except httpx.HTTPError:
                text = ""
            time.sleep(pause)
        if text.strip():
            rp = RobotFileParser()
            rp.parse(text.splitlines())
        with log.lock:
            log.robots.setdefault(base, rp)
    rp = log.robots[base]
    return rp is None or rp.can_fetch(USER_AGENT, url)


def _over_budget(log: RunLog, url: str, server: str) -> bool:
    """Counts the request against the server's hourly budget; True if there is none left."""
    if host_of(url) not in log.budget_hosts:
        return False
    if cache.requests_in_window(server) >= SERVER_BUDGET:
        log.mark_blocked(host_of(url), BUDGET)
        return True
    cache.record_request(server)
    return False


def polite_get_raw(log: RunLog, url: str, pause: float) -> tuple[httpx.Response | None, str]:
    """One paced request without robots check or status handling (used for robots.txt itself)."""
    net = network_of(urlsplit(url).netloc)
    if host_of(url) in log.down_hosts or log.net_failures[net] >= 2:
        return None, "nicht erreichbar"
    with log.host_locks[net]:
        if host_of(url) in log.budget_hosts:  # a robots.txt refresh is optional: skip silently when the budget is used up
            if cache.requests_in_window(net) >= SERVER_BUDGET:
                return None, BUDGET
            cache.record_request(net)
        try:
            r = log.client.get(url, timeout=12)
        except httpx.HTTPError as e:
            r = None
            err = type(e).__name__
        time.sleep(pause)
    return (r, "") if r is not None else (None, err)


def polite_get(log: RunLog, url: str, pause: float = PAUSE) -> tuple[httpx.Response | None, str]:
    """One request, at most one at a time per hosting network, followed by a pause.
    Returns (response, "") or (None, message for the caller)."""
    if not url.startswith(("http://", "https://")):
        return None, "FEHLER: keine gültige http(s)-URL."
    host = host_of(url)
    net = network_of(urlsplit(url).netloc)
    if host in log.down_hosts or log.net_failures[net] >= 2:
        log.mark_blocked(host, f"{UNREACHABLE} (Hoster antwortet in diesem Lauf nicht)")
        return None, f"FEHLER: {host} ist in diesem Lauf nicht erreichbar. Nicht erneut versuchen, andere Quelle nutzen."
    with log.host_locks[net]:
        if not _allowed(log, url, pause):
            log.mark_blocked(host, "robots.txt-Sperre")
            return None, f"GESPERRT (robots.txt): {url} – nicht verwenden, Zweitquelle suchen."
        if _over_budget(log, url, net):
            return None, f"BUDGET: {host} wird in dieser Stunde nicht weiter abgefragt (Schutz vor Sperre). Mit den bisher gelesenen Einträgen arbeiten."
        r, err = None, ""
        for _ in range(2):
            try:
                r = log.client.get(url)
                break
            except httpx.HTTPError as e:
                err = type(e).__name__
                time.sleep(2)
        time.sleep(pause)
    if r is None:
        with log.lock:
            log.down_hosts.add(host)
            log.net_failures[net] += 1
        log.mark_blocked(host, f"{UNREACHABLE} ({err})")
        return None, f"FEHLER ({err}): {url} nicht erreichbar."
    with log.lock:
        log.net_failures[net] = 0
    if r.status_code in BLOCK_STATUS:
        log.mark_blocked(host, f"HTTP {r.status_code}")
        return None, f"GESPERRT (HTTP {r.status_code}): {url} – Zweitquelle suchen."
    if r.status_code >= 400:
        return None, f"FEHLER (HTTP {r.status_code}): {url}"
    log.mark_ok(host)
    return r, ""


def make_tools(cfg: RunConfig, log: RunLog, budget: int = 18) -> list:
    """One tool set per research agent: `budget` caps that agent's tool calls."""
    client = log.client
    calls = 0

    def over_budget() -> str | None:
        nonlocal calls
        calls += 1
        if calls > budget:
            return "BUDGET ERSCHÖPFT: keine weiteren Tool-Aufrufe. Liefere jetzt dein Ergebnis mit den bisher belegten Funden."
        return None

    def get(url: str) -> tuple[httpx.Response | None, str]:
        host = host_of(url)
        if host in log.collected_hosts:
            if norm_url(url) not in log.collected_urls:
                return None, (
                    f"NICHT NÖTIG: {host} wurde vorab vollständig gesammelt. Dort dürfen nur die Einträge aus der Liste "
                    "«Gesammelte Einträge» geöffnet werden; alles andere liegt ausserhalb des Zeitfensters."
                )
            return polite_get(log, url, PAUSE_COLLECTED)
        return polite_get(log, url)

    @tool
    def web_search(query: str, zeitraum: Literal["d", "w", "m", "alle"] = "w", typ: Literal["web", "news"] = "web") -> str:
        """Websuche. Liefert Titel, URL, ggf. Datum und Snippet. zeitraum: d=Tag, w=Woche, m=Monat.
        typ='news' durchsucht Nachrichten (mit Datum). Snippets sind kein Beleg: Artikel danach mit fetch_page lesen."""
        if msg := over_budget():
            return msg
        with log.lock:
            log.searches += 1
        try:
            if SEARXNG_URL:
                params = {"q": query, "format": "json", "language": "de-CH"}
                if zeitraum != "alle":
                    params["time_range"] = {"d": "day", "w": "week", "m": "month"}[zeitraum]
                if typ == "news":
                    params["categories"] = "news"
                items = client.get(f"{SEARXNG_URL}/search", params=params).json().get("results", [])[:8]
                rows = [(i.get("title"), i.get("url"), i.get("publishedDate") or "", i.get("content") or "") for i in items]
            else:
                from ddgs import DDGS

                limit = None if zeitraum == "alle" else zeitraum
                if typ == "news":
                    items = DDGS().news(query, region="ch-de", timelimit=limit, max_results=8)
                    rows = [(i.get("title"), i.get("url"), i.get("date") or "", i.get("body") or "") for i in items]
                else:
                    items = DDGS().text(query, region="ch-de", timelimit=limit, max_results=8)
                    rows = [(i.get("title"), i.get("href"), "", i.get("body") or "") for i in items]
        except Exception as e:  # search backends fail in many ways; the agent should just try another query
            if "no results" in str(e).lower():
                return "Keine Treffer."
            log.mark_blocked("Websuche", f"einzelne Suchanfragen fehlgeschlagen ({type(e).__name__})")
            return f"FEHLER Suche: {e}"
        if not rows:
            return "Keine Treffer."
        return "\n".join(f"- {t} | {u} | {d} | {s[:220]}" for t, u, d, s in rows)

    @tool
    def fetch_page(url: str) -> str:
        """Lädt eine Seite und gibt Titel, Publikationsdatum (aus Metadaten, falls vorhanden) und Haupttext zurück.
        Nur URLs, die hiermit erfolgreich gelesen wurden, dürfen als Quelle zitiert werden.
        Nicht für shab.ch-Links: SHAB-Meldungen liefert shab_publications bereits im Volltext."""
        if host_of(url) == "shab.ch":
            return "Nicht nötig: shab_publications liefert den vollständigen Meldungstext. Die dort ausgegebene URL und das Datum sind zitierfähig."
        if msg := over_budget():
            return msg
        # Collected entries are kept for a few hours: a second run must not spend the
        # server's request budget on pages that were read a moment ago.
        collected = norm_url(url) in log.collected_urls
        if collected and (cached := cache.get("page:" + norm_url(url), PAGE_CACHE_HOURS)):
            log.mark_read(url)
            log.mark_ok(host_of(url))
            return cached
        r, err = get(url)
        if r is None:
            return err
        if "html" not in r.headers.get("content-type", "html"):
            return f"FEHLER: {url} ist kein HTML ({r.headers.get('content-type')})."
        doc = trafilatura.bare_extraction(r.text, url=str(r.url), with_metadata=True)
        text = (doc.text if doc else "") or ""
        if len(text) < 400 and any(m in r.text.lower() for m in PAYWALL_MARKERS):
            if teaser := log.teasers.get(norm_url(url)):
                # The publisher's own feed gives title, date and teaser to everyone; that much is
                # public and may be cited, the article behind the paywall may not.
                log.mark_blocked(host_of(url), PAYWALL_TEASER)
                log.mark_read(url)
                return (
                    f"URL: {url}\nTITEL: {teaser[0]}\nPUBLIKATIONSDATUM (Feed): {teaser[1]}\n"
                    "HINWEIS: Der Volltext liegt hinter einer Paywall. Öffentlich belegt sind nur Titel und Anriss; "
                    f"verwende ausschliesslich diese Angaben und suche bei Bedarf eine Zweitquelle.\n\nANRISS: {teaser[2]}"
                )
            log.mark_blocked(host_of(url), "Paywall")
            return f"GESPERRT (Paywall vermutet): {url} – Zweitquelle suchen."
        if not text:
            return f"Kein Artikeltext erkannt auf {url}. Für Übersichtsseiten list_links verwenden."
        log.mark_read(url, str(r.url))
        result = (
            f"URL: {url}\nTITEL: {doc.title or ''}\nPUBLIKATIONSDATUM (Metadaten): {doc.date or 'unbekannt'}\n\n"
            f"{text[:6000]}"
        )
        if collected:
            cache.put("page:" + norm_url(url), result)
        return result

    @tool
    def list_links(url: str) -> str:
        """Listet die Links einer Übersichtsseite (z.B. Medienmitteilungen, News, Stellen, Agenda) als 'Text | URL'.
        Danach einzelne Meldungen mit fetch_page lesen."""
        if msg := over_budget():
            return msg
        r, err = get(url)
        if r is None:
            return err
        try:
            tree = lxml.html.fromstring(r.text)
        except Exception:
            return f"FEHLER: {url} nicht lesbar."
        seen, out = set(), []
        for a in tree.xpath("//a[@href]"):
            text = " ".join(a.text_content().split())
            href = urljoin(str(r.url), a.get("href")).split("#")[0]
            if len(text) < 15 or not href.startswith("http") or href in seen:
                continue
            seen.add(href)
            out.append(f"- {text[:140]} | {href}")
        return "\n".join(out[:80]) or "Keine Links gefunden."

    @tool
    def shab_publications(stichwort: str = "", seite: int = 0) -> str:
        """Handelsregister-Publikationen im Schweizerischen Handelsamtsblatt (SHAB) für die Kantone der Region
        im Zeitfenster inkl. Nachzügler-Frist, im Volltext. stichwort = EIN Suchwort ohne Operatoren (z.B. 'Revisionsstelle',
        'Sitzverlegung', 'Fusion', 'Liquidation', 'Opting'); leer = alle. Je Seite 40 Einträge.
        Jede Zeile: Publikationsdatum | Titel | Meldungstext | URL. Datum und URL sind direkt zitierfähig."""
        if msg := over_budget():
            return msg
        params: list[tuple[str, str]] = [
            ("allowRubricSelection", "true"),
            ("includeContent", "true"),
            ("publicationStates", "PUBLISHED"),
            ("rubrics", "HR"),
            ("publicationDate.start", cfg.nachzuegler_start.isoformat()),
            ("publicationDate.end", cfg.laufdatum.isoformat()),
            ("pageRequest.page", str(seite)),
            ("pageRequest.size", "40"),
        ] + [("cantons", k) for k in cfg.kantone]
        if stichwort:
            params.append(("keyword", stichwort))
        try:
            r = client.get("https://www.shab.ch/api/v1/publications", params=params, headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.mark_blocked("shab.ch", f"API-Fehler ({type(e).__name__})")
            return f"FEHLER SHAB: {e}"
        log.mark_ok("shab.ch")
        lines = []
        for item in data.get("content", []):
            meta, content = item["meta"], item.get("content") or {}
            url = f"https://www.shab.ch/#!/search/publications/detail/{meta['id']}"
            log.mark_read(url)
            lines.append(
                f"- {meta['publicationDate'][:10]} | {meta['title'].get('de', '')} | "
                f"{(content.get('publicationText') or '')[:900]} | {url}"
            )
        total = data.get("total", len(lines))
        head = f"{total} Publikationen ({', '.join(cfg.kantone)}, {cfg.nachzuegler_start} bis {cfg.laufdatum}), Seite {seite}:"
        return head + "\n" + "\n".join(lines) if lines else "Keine Publikationen."

    return [web_search, fetch_page, list_links, shab_publications]
