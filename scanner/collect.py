"""Collect first, then judge.

Plain code, no LLM: fetch the list pages and feeds of all registered sources one
after the other and keep only the entries inside the time window. The agents then
get these entries as a ready-made list and open only the ones worth reading.

This replaces agents crawling the sites themselves, which used up the request
limit of the shared municipal hoster and got the run blocked, and it replaces
hit-and-miss web searches for newspaper articles.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.robotparser import RobotFileParser

from . import cache
from .config import RunConfig
from .sources import Liste, Quelle, orte_fuer, quellen_fuer
from .tools import PAUSE, PAUSE_COLLECTED, RunLog, host_of, norm_url, polite_get, polite_get_raw

VORSCHAU_TAGE = 60  # upcoming sessions and votes are listed this far ahead
LIST_CACHE_HOURS = 3  # a list read less than this long ago is not fetched again
# robots.txt of the municipal sites: every request counts against a tight limit, so it is
# kept for half a year and refreshed for one site per run. Its rules apply once known.
ROBOTS_CACHE_HOURS = 180 * 24
ROBOTS_CHECKS_PER_RUN = 1
DATE_FIELDS = ("_datum", "datum-sort", "_geschaeftsdatum", "datumVon-sort", "datumVon", "datum")
ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
CH = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

Zeile = tuple[date | None, str, str, str]  # date, title, absolute url, teaser


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

    def zeile(self) -> str:
        teaser = f" — {self.anriss[:160]}" if self.anriss else ""
        return f"- {self.datum.isoformat()} | {self.art} | {self.quelle} | {self.titel}{teaser} | {self.url}"


@dataclass
class Sammlung:
    eintraege: list[Eintrag]
    listen_gelesen: int = 0
    websites: int = 0
    eintraege_total: int = 0

    def zusammenfassung(self) -> str:
        return (
            f"vorab gesammelt: {self.listen_gelesen} Listen und Feeds von {self.websites} Websites, "
            f"{self.eintraege_total} Einträge, davon {len(self.eintraege)} im Zeitfenster"
        )


# --- parsers ------------------------------------------------------------------
def _date(value: str) -> date | None:
    if m := ISO.search(value):
        y, mo, d = map(int, m.groups())
    elif m := CH.search(value):
        d, mo, y = map(int, m.groups())
    else:
        try:  # RSS: "Tue, 08 Sep 2026 07:53:50 +0000"
            return parsedate_to_datetime(value.strip()).date()
        except (TypeError, ValueError):
            return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _text(fragment: str) -> str:
    fragment = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", fragment, flags=re.S)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", htmllib.unescape(fragment))).split())


def _tag(block: str, *names: str) -> str:
    for name in names:
        if m := re.search(rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>", block, re.S):
            return m.group(1).strip()
    return ""


def parse_iweb(page_html: str, base: str) -> list[Zeile]:
    """The list the municipal web platform embeds in the page as JSON."""
    m = re.search(r'data-entities="([^"]+)"', page_html)
    if not m:
        return []
    try:
        rows = json.loads(htmllib.unescape(m.group(1))).get("data", [])
    except (ValueError, AttributeError):
        return []
    out: list[Zeile] = []
    for row in rows:
        cell = str(row.get("name") or row.get("title") or "")
        link = re.search(r'href="([^"]+)"', cell)
        titel = _text(cell)
        if link and titel:
            href = link.group(1)
            d = next((x for x in (_date(str(row.get(k) or "")) for k in DATE_FIELDS) if x), None)
            out.append((d, titel, href if href.startswith("http") else base + href, ""))
    return out


def parse_rss(xml: str, base: str) -> list[Zeile]:
    """RSS 2.0 and Atom."""
    out: list[Zeile] = []
    for item in re.findall(r"<(?:item|entry)[\s>].*?</(?:item|entry)>", xml, re.S):
        link = _text(_tag(item, "link")) or (re.search(r'<link[^>]+href="([^"]+)"', item) or [None, ""])[1]
        titel = _text(_tag(item, "title"))
        if link and titel:
            d = _date(_tag(item, "pubDate", "published", "updated", "dc:date"))
            out.append((d, titel, link, _text(_tag(item, "description", "summary"))[:400]))
    return out


def parse_sitemap(xml: str, base: str) -> list[Zeile]:
    """XML sitemaps that carry a date per URL. The title comes from the news or video
    extension if present, else from the URL."""
    out: list[Zeile] = []
    for block in re.findall(r"<url>.*?</url>", xml, re.S):
        loc = _text(_tag(block, "loc"))
        if not loc:
            continue
        titel = _text(_tag(block, "news:title", "video:title"))
        if not titel:
            slug = re.sub(r"-(?:art|ld)?[-.]?\d+$", "", loc.rstrip("/").rsplit("/", 1)[-1])
            titel = slug.replace("-", " ").strip().capitalize()
        d = _date(_tag(block, "news:publication_date", "lastmod"))
        out.append((d, titel, loc, ""))
    return out


PARSER = {"iweb": parse_iweb, "rss": parse_rss, "sitemap": parse_sitemap}


# --- collecting -----------------------------------------------------------------
def _im_fenster(d: date, liste: Liste, cfg: RunConfig) -> bool:
    if liste.zukunft:
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


def collect(cfg: RunConfig, log: RunLog, on_event: Callable[[str], None] = lambda _: None) -> Sammlung:
    quellen: list[Quelle] = quellen_fuer(cfg.kantone)
    # whole words only ("Kerns" must not match "Kernstück"); short canton codes case-sensitive
    namen = [o for o in orte_fuer(cfg.kantone) if len(o) > 2]
    codes = [o for o in orte_fuer(cfg.kantone) if len(o) <= 2]
    orte = re.compile(
        r"\b(?i:" + "|".join(re.escape(o) for o in namen) + r")(?i:er|ern|s)?\b" + (r"|\b(?:" + "|".join(codes) + r")\b" if codes else "")
        if namen else r"$^"
    )
    result = Sammlung(eintraege=[])
    with log.lock:  # from now on agents may only open collected entries on these hosts
        log.collected_hosts.update(host_of(f"https://{q.host}") for q in quellen)
        log.budget_hosts.update(host_of(f"https://{q.host}") for q in quellen if q.knapp)
    seen: set[str] = set()
    titles: set[str] = set()  # regional papers share articles: the same headline is listed once
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
                    if d is None or not _im_fenster(d, liste, cfg) or norm_url(url) in seen:
                        continue
                    if liste.ausschluss and re.search(liste.ausschluss, url):
                        continue
                    if liste.regional and not orte.search(f"{titel} {anriss} {url}"):
                        continue
                    if liste.typ != "iweb":
                        key = re.sub(r"\W+", "", titel.lower())
                        if key in titles:
                            continue
                        titles.add(key)
                    seen.add(norm_url(url))
                    result.eintraege.append(
                        Eintrag(d, liste.art, q.name, titel[:160], url, liste.quellgattung, liste.zukunft, anriss)
                    )
                    if anriss:
                        log.teasers[norm_url(url)] = (titel, d.isoformat(), anriss)
        result.websites += answered
        on_event(f"  · {q.name}: {'gelesen' if answered else 'nicht erreichbar'}")
    with log.lock:
        log.collected_urls.update(seen)
    result.eintraege.sort(key=lambda e: (e.quellgattung, e.quelle, -e.datum.toordinal()))
    return result
