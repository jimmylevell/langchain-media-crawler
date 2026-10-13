"""Collect first, then judge.

Plain code, no LLM: fetch the list pages and feeds of all registered sources one
after the other and keep only the entries inside the time window. The agents then
get these entries as a ready-made list and open only the ones worth reading.

Three kinds of sources feed the list:
- the registry (scanner/sources.py): authorities, newspapers, magazines, associations
- official APIs (scanner/amtlich.py): Amtsblatt portal, simap
- discovery (scanner/discover.py): outlets that are not in the registry but write about the region
  (needs a search server, SEARXNG_URL)

This replaces agents crawling the sites themselves, which used up the request
limit of the shared municipal hoster and got the run blocked, and it replaces
hit-and-miss web searches for newspaper articles.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable
from urllib.robotparser import RobotFileParser

from . import amtlich, cache, discover
from .config import SEARXNG_URL, RunConfig
from .parsers import PARSER
from .sources import Liste, Quelle, orte_muster, quellen_fuer
from .tools import PAUSE, PAUSE_COLLECTED, RunLog, host_of, norm_url, polite_get, polite_get_raw

VORSCHAU_TAGE = 60  # upcoming sessions and votes are listed this far ahead
LIST_CACHE_HOURS = 3  # a list read less than this long ago is not fetched again
# robots.txt of the municipal sites: every request counts against a tight limit, so it is
# kept for half a year and refreshed for one site per run. Its rules apply once known.
ROBOTS_CACHE_HOURS = 180 * 24
ROBOTS_CHECKS_PER_RUN = 1


@dataclass(frozen=True)
class Eintrag:
    datum: date
    art: str
    quelle: str
    titel: str
    url: str
    quellgattung: str
    zukunft: bool
    anriss: str = ""
    direkt: bool = False  # the API answer already is the evidence; an agent need not open the page

    def zeile(self) -> str:
        teaser = f" — {self.anriss[:160]}" if self.anriss else ""
        art = f"{self.art} (Direktbeleg)" if self.direkt else self.art
        return f"- {self.datum.isoformat()} | {art} | {self.quelle} | {self.titel}{teaser} | {self.url}"


@dataclass
class Sammlung:
    eintraege: list[Eintrag]
    listen_gelesen: int = 0
    websites: int = 0
    eintraege_total: int = 0
    entdeckt: list[discover.Entdeckt] = field(default_factory=list)

    def zusammenfassung(self) -> str:
        return (
            f"vorab gesammelt: {self.listen_gelesen} Listen und Feeds von {self.websites} Websites, "
            f"{self.eintraege_total} Einträge, davon {len(self.eintraege)} im Zeitfenster"
        )


# --- collecting -----------------------------------------------------------------
def _im_fenster(d: date, zukunft: bool, cfg: RunConfig) -> bool:
    if zukunft:
        return cfg.laufdatum <= d <= cfg.laufdatum + timedelta(days=VORSCHAU_TAGE)
    return cfg.nachzuegler_start <= d <= cfg.laufdatum


def _pfade(liste: Liste, cfg: RunConfig) -> list[str]:
    """Expands {woche} to the Mondays of the weeks the time window touches."""
    if "{woche}" not in liste.path:
        return [liste.path]
    monday = cfg.fenster_start - timedelta(days=cfg.fenster_start.weekday())
    weeks = []
    while monday <= cfg.laufdatum:
        weeks.append(liste.path.replace("{woche}", monday.isoformat()))
        monday += timedelta(days=7)
    return weeks


def _robots_schonend(log: RunLog, base: str, budget: list[int]) -> None:
    robots = cache.get(base + "/robots.txt", ROBOTS_CACHE_HOURS)
    if robots is None and budget[0] > 0:
        budget[0] -= 1
        r, _ = polite_get_raw(log, base + "/robots.txt", PAUSE_COLLECTED)
        if r is not None:
            robots = r.text if r.status_code == 200 else ""
            cache.put(base + "/robots.txt", robots)
    rp = None
    if robots and robots.strip():
        rp = RobotFileParser()
        rp.parse(robots.splitlines())
    with log.lock:  # known rules apply; a site not yet checked is read (public list pages only)
        log.robots[base] = rp


class _Filter:
    """Window, region and duplicate rules shared by all kinds of sources."""

    def __init__(self, cfg: RunConfig, log: RunLog, result: Sammlung):
        self.cfg, self.log, self.result = cfg, log, result
        self.orte = orte_muster(cfg.kantone)
        self.seen: set[str] = set()
        self.titles: set[str] = set()  # regional papers share articles: the same headline is listed once

    def nimm(self, d: date | None, titel: str, url: str, anriss: str, *, art: str, quelle: str, quellgattung: str,
             zukunft: bool = False, regional: bool = False, ausschluss: str = "", titel_pruefen: bool = True, direkt: bool = False) -> None:
        if d is None or not _im_fenster(d, zukunft, self.cfg) or norm_url(url) in self.seen:
            return
        if ausschluss and re.search(ausschluss, url):
            return
        if regional and not self.orte.search(f"{titel} {anriss} {url}"):
            return
        if titel_pruefen:
            key = re.sub(r"\W+", "", titel.lower())
            if key in self.titles:
                return
            self.titles.add(key)
        self.seen.add(norm_url(url))
        self.result.eintraege.append(Eintrag(d, art, quelle, titel[:160], url, quellgattung, zukunft, anriss, direkt))
        if direkt:
            self.log.mark_read(url)
        elif anriss:
            self.log.teasers[norm_url(url)] = (titel, d.isoformat(), anriss)


def collect(cfg: RunConfig, log: RunLog, on_event: Callable[[str], None] = lambda _: None) -> Sammlung:
    quellen: list[Quelle] = quellen_fuer(cfg.kantone)
    result = Sammlung(eintraege=[])
    nimm = _Filter(cfg, log, result)
    with log.lock:  # from now on agents may only open collected entries on these hosts
        log.collected_hosts.update(host_of(f"https://{q.host}") for q in quellen)
        log.budget_hosts.update(host_of(f"https://{q.host}") for q in quellen if q.knapp)
    robots_checks = [ROBOTS_CHECKS_PER_RUN]
    for q in quellen:
        base = f"https://{q.host}"
        pause = PAUSE_COLLECTED if q.knapp else PAUSE
        answered = False
        if q.knapp:
            _robots_schonend(log, base, robots_checks)
        for liste in q.listen:
            for path in _pfade(liste, cfg):
                page = cache.get(base + path, LIST_CACHE_HOURS)
                if page is None:
                    r, _ = polite_get(log, base + path, pause)
                    if r is None:  # 404 = this site has no such list; timeouts are already in the log
                        continue
                    page = r.text
                    cache.put(base + path, page)
                else:
                    log.mark_ok(host_of(base))
                answered = True
                rows = PARSER[liste.typ](page, base)
                result.listen_gelesen += 1
                result.eintraege_total += len(rows)
                for d, titel, url, anriss in rows:
                    nimm.nimm(d, titel, url, anriss, art=liste.art, quelle=q.name, quellgattung=liste.quellgattung, zukunft=liste.zukunft,
                              regional=liste.regional, ausschluss=liste.ausschluss, titel_pruefen=liste.typ != "iweb")
        result.websites += answered
        on_event(f"  · {q.name}: {'gelesen' if answered else 'nicht erreichbar'}")

    for source, reader in (("Amtsblatt-Portal", amtlich.amtsblatt), ("simap", amtlich.simap)):
        rows, requests = reader(cfg, log)
        result.listen_gelesen += requests
        result.websites += bool(requests)
        result.eintraege_total += len(rows)
        for r in rows:
            nimm.nimm(r.datum, r.titel, r.url, r.anriss, art=r.art, quelle=r.quelle, quellgattung=r.quellgattung, titel_pruefen=False, direkt=True)
        on_event(f"  · {source}: {len(rows)} Einträge" if requests else f"  · {source}: nicht erreichbar")

    articles, _ = discover.discover(cfg, log, {q.host.removeprefix("www.") for q in quellen})
    for host, (d, titel, url, anriss) in articles:
        result.eintraege_total += 1
        nimm.nimm(d, titel, url, anriss, art="Zeitungsartikel (entdeckt)", quelle=host, quellgattung="Lokale Medien", regional=True)
    counts: dict[str, int] = {}
    for e in result.eintraege:
        if e.art == "Zeitungsartikel (entdeckt)":
            counts[e.quelle] = counts.get(e.quelle, 0) + 1
    result.entdeckt = [discover.Entdeckt(h, n) for h, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    result.websites += len(result.entdeckt)
    on_event("  · Entdeckte Medien: " + (", ".join(o.host for o in result.entdeckt) or ("keine" if SEARXNG_URL else "aus (SEARXNG_URL nicht gesetzt)")))

    with log.lock:
        log.collected_urls.update(u for u in nimm.seen if host_of(u) in log.collected_hosts)
    result.eintraege.sort(key=lambda e: (e.quellgattung, e.quelle, -e.datum.toordinal()))
    return result
