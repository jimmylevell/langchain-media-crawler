"""Source registry: verified websites per canton that the collector reads.

Only sources that were checked by hand belong here (last check: 13 October 2026).
Four list formats are understood (scanner/parsers.py):

- "iweb":    the municipal web platform used by the cantons and most municipalities,
             which embeds its lists as JSON in the page
- "rss":     RSS / Atom feeds of newspapers, magazines and associations
- "sitemap": XML sitemaps with a date per article (news sitemaps, weekly sitemaps)
- "ihz":     the event list of the Industrie- und Handelskammer Zentralschweiz

To add a canton or a publication: add it here. Cantons without an entry skip the
collector and are researched by the agents alone. Outlets that are not listed here
are found by scanner/discover.py; a regular one belongs in this file.
The APIs of the Amtsblatt portal and simap are read by scanner/amtlich.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Liste:
    path: str  # may contain {woche} = Monday of a week as YYYY-MM-DD
    art: str  # shown to the agents
    quellgattung: str
    typ: str = "iweb"
    zukunft: bool = False  # True: dates are upcoming events, not publication dates
    regional: bool = False  # True: supra-regional source, keep only entries naming the region
    ausschluss: str = ""  # regex on the URL: sections that never matter (sport, ...)


# --- public sector (municipal web platform) ----------------------------------
MELDUNGEN = Liste("/aktuellesinformationen", "Meldung", "Kanton und Gemeinden")
GESCHAEFTE = Liste("/politbusiness", "Politisches Geschäft", "Politische Agenda")
VERNEHMLASSUNGEN = Liste("/vernehmlassungen", "Vernehmlassung", "Politische Agenda")
SITZUNGEN = Liste("/sitzung", "Sitzungstermin", "Politische Agenda", zukunft=True)
ABSTIMMUNGEN = Liste("/abstimmungen", "Abstimmungstermin", "Politische Agenda", zukunft=True)

KEIN_SPORT = r"/(sport|leben|kultur|meinung|ratgeber)/"


@dataclass(frozen=True)
class Quelle:
    name: str
    host: str
    listen: tuple[Liste, ...] = (MELDUNGEN,)
    knapp: bool = True  # shared municipal servers with a tight request limit; False for media sites


def _gemeinden(*pairs: tuple[str, str]) -> list[Quelle]:
    return [Quelle(f"Gemeinde {name}", host) for name, host in pairs]


def _medium(name: str, host: str, *listen: Liste) -> Quelle:
    return Quelle(name, host, listen, knapp=False)


def _artikel(path: str, typ: str = "rss", **kw) -> Liste:
    return Liste(path, "Zeitungsartikel", "Lokale Medien", typ=typ, **kw)


# Newspapers, magazines and broadcasters covering all of Central Switzerland: read for every
# canton of the region, filtered to entries that name the canton or one of its municipalities.
ZENTRALSCHWEIZ: list[Quelle] = [
    _medium("ROI – Zentralschweizer Wirtschaftsmagazin", "roi-online.ch", _artikel("/rss_feed/", regional=True)),
    _medium("Luzerner Zeitung, Wirtschaft und Zentralschweiz", "www.luzernerzeitung.ch",
            _artikel("/wirtschaft.rss", regional=True), _artikel("/zentralschweiz.rss", regional=True, ausschluss=KEIN_SPORT)),
    _medium("zentralplus", "www.zentralplus.ch", _artikel("/feed/", regional=True)),  # the site answers 403, the feed is open
    _medium("Bote der Urschweiz", "www.bote.ch", _artikel("/googlenews.sitemap.xml", "sitemap", regional=True, ausschluss=KEIN_SPORT)),
    _medium("Tele 1", "www.tele1.ch", Liste("/__week/{woche}/sitemap.xml", "TV-Beitrag", "Lokale Medien", typ="sitemap", regional=True)),
    _medium("IHZ – Industrie- und Handelskammer Zentralschweiz", "www.ihz.ch",
            Liste("/netzwerk-anlaesse/anlaesse/", "Verbandsanlass", "Events", typ="ihz", zukunft=True)),
]
UNTERWALDEN: list[Quelle] = [  # publications for both Nidwalden and Obwalden
    _medium("Unterwaldner", "unterwaldner.ch", _artikel("/feed/")),
    _medium("unterwalden24", "www.unterwalden24.ch", _artikel("/sitemap-teile/sitemap/beitraege-1.xml", "sitemap")),
]

REGISTRY: dict[str, list[Quelle]] = {
    "NW": [
        Quelle("Kanton Nidwalden", "www.nw.ch", (MELDUNGEN, GESCHAEFTE, VERNEHMLASSUNGEN, SITZUNGEN, ABSTIMMUNGEN)),
        *_gemeinden(
            ("Beckenried", "www.beckenried.ch"), ("Buochs", "www.buochs.ch"), ("Dallenwil", "www.dallenwil.ch"),
            ("Emmetten", "www.emmetten.ch"), ("Ennetbürgen", "www.ennetbuergen.ch"), ("Ennetmoos", "www.ennetmoos.ch"),
            ("Oberdorf", "www.oberdorf-nw.ch"), ("Stans", "www.stans.ch"), ("Wolfenschiessen", "www.wolfenschiessen.ch"),
        ),
        # Hergiswil and Stansstad use a different list layout and are left to the agents.
        _medium("Nidwaldner Zeitung", "www.nidwaldnerzeitung.ch", _artikel("/zentralschweiz/nidwalden.rss", ausschluss=KEIN_SPORT)),
        _medium("Nidwaldner Gewerbeverband", "gewerbe-nw.ch", Liste("/feed/", "Verbandsmeldung", "Events", typ="rss")),
        *UNTERWALDEN,
        *ZENTRALSCHWEIZ,
    ],
    "OW": [
        Quelle("Kanton Obwalden", "www.ow.ch", (MELDUNGEN, GESCHAEFTE, SITZUNGEN, ABSTIMMUNGEN)),
        *_gemeinden(
            ("Alpnach", "www.alpnach.ch"), ("Engelberg", "www.gde-engelberg.ch"), ("Giswil", "www.giswil.ch"),
            ("Kerns", "www.kerns.ch"), ("Lungern", "www.lungern.ch"), ("Sachseln", "www.sachseln.ch"),
            ("Sarnen", "www.sarnen.ch"),
        ),
        _medium("Obwaldner Zeitung", "www.obwaldnerzeitung.ch", _artikel("/zentralschweiz/obwalden.rss", ausschluss=KEIN_SPORT)),
        *UNTERWALDEN,
        *ZENTRALSCHWEIZ,
    ],
}

# --- places: used to filter supra-regional sources and to check the place a model reports ---
GEMEINDEN: dict[str, tuple[str, ...]] = {
    "NW": ("Stans", "Stansstad", "Hergiswil", "Buochs", "Beckenried", "Ennetbürgen", "Ennetbuergen", "Ennetmoos",
           "Emmetten", "Dallenwil", "Oberdorf", "Wolfenschiessen", "Bürgenstock"),
    "OW": ("Sarnen", "Kerns", "Sachseln", "Alpnach", "Giswil", "Lungern", "Engelberg", "Melchsee-Frutt"),
}
KANTONSWORTE: dict[str, tuple[str, ...]] = {
    "NW": ("Nidwalden", "Nidwaldner", "Unterwalden", "NW"),
    "OW": ("Obwalden", "Obwaldner", "Unterwalden", "OW"),
}
# neighbouring places that still count, with a lower weight (scanner/scoring.py)
ANGRENZEND = ("Luzern", "Horw", "Kriens", "Meggen", "Weggis", "Vitznau", "Entlebuch", "Flühli", "Seelisberg", "Isenthal",
              "Attinghausen", "Altdorf", "Küssnacht", "Gersau", "Schwyz", "Brunnen")
ORTE: dict[str, tuple[str, ...]] = {k: KANTONSWORTE[k] + GEMEINDEN[k] for k in GEMEINDEN}

# hosts that are authorities although they are not in the registry (and *.admin.ch)
BEHOERDEN_HOSTS = ("amtsblattportal.ch", "simap.ch")


def quellen_fuer(kantone: list[str]) -> list[Quelle]:
    seen: dict[str, Quelle] = {}
    for k in kantone:
        for q in REGISTRY.get(k.upper(), []):
            seen.setdefault(q.host, q)
    return list(seen.values())


def orte_fuer(kantone: list[str]) -> list[str]:
    return [o for k in kantone for o in ORTE.get(k.upper(), ())]


def orte_muster(kantone: list[str]) -> re.Pattern:
    """Whole words only ("Kerns" must not match "Kernstück"); short canton codes case-sensitive."""
    namen = [o for o in orte_fuer(kantone) if len(o) > 2]
    codes = [o for o in orte_fuer(kantone) if len(o) <= 2]
    if not namen:
        return re.compile(r"$^")
    return re.compile(
        r"\b(?i:" + "|".join(re.escape(o) for o in namen) + r")(?i:er|ern|s)?\b" + (r"|\b(?:" + "|".join(codes) + r")\b" if codes else "")
    )


def _wort(text: str, names: tuple[str, ...]) -> str:
    """The first of `names` that occurs in `text` as a whole word, else ''."""
    for n in names:
        if re.search(rf"\b{re.escape(n)}(?:er|s)?\b", text, 0 if len(n) <= 2 else re.I):
            return n
    return ""


def ort_gewicht(ort: str, kantone: list[str]) -> tuple[float, str] | None:
    """Locality of a place name: (weight, the known place it matched), or None if it is outside the region.
    1.0 = first canton of the run (the branch's own), 0.9 = other canton of the region, 0.6 = neighbouring place.
    Cantons without registry data cannot be checked and always pass."""
    bekannt = [k.upper() for k in kantone if k.upper() in GEMEINDEN]
    if not bekannt:
        return 1.0, ""
    for i, k in enumerate(bekannt):
        if treffer := _wort(ort, GEMEINDEN[k] + KANTONSWORTE[k]):
            return (1.0 if i == 0 else 0.9), treffer
    if treffer := _wort(ort, ANGRENZEND):
        return 0.6, treffer
    return None


def quellentyp(url: str) -> str:
    """'shab', 'behoerde' or 'medium' (everything else, including outlets found by discovery)."""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    if host == "shab.ch":
        return "shab"
    if host in BEHOERDEN_HOSTS or host.endswith(".admin.ch"):
        return "behoerde"
    return "behoerde" if any(q.knapp and q.host.removeprefix("www.") == host for qs in REGISTRY.values() for q in qs) else "medium"
