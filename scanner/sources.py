"""Source registry: verified websites per canton that the collector reads.

Only sources that were checked by hand belong here (last check: 8 October 2026).
Three list formats are understood:

- "iweb":    the municipal web platform used by the cantons and most municipalities,
             which embeds its lists as JSON in the page
- "rss":     RSS / Atom feeds of newspapers, magazines and associations
- "sitemap": XML sitemaps with a date per article (news sitemaps, weekly sitemaps)

To add a canton or a publication: add it here. Cantons without an entry skip the
collector and are researched by the agents alone.
"""
from __future__ import annotations

from dataclasses import dataclass


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
    _medium("Luzerner Zeitung, Wirtschaft", "www.luzernerzeitung.ch", _artikel("/wirtschaft.rss", regional=True)),
    _medium("Bote der Urschweiz", "www.bote.ch", _artikel("/googlenews.sitemap.xml", "sitemap", regional=True, ausschluss=KEIN_SPORT)),
    _medium("Tele 1", "www.tele1.ch", Liste("/__week/{woche}/sitemap.xml", "TV-Beitrag", "Lokale Medien", typ="sitemap", regional=True)),
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

# Names that make an entry of a supra-regional source "regional".
ORTE: dict[str, tuple[str, ...]] = {
    "NW": ("Nidwalden", "Nidwaldner", "Unterwalden", "NW", "Stans", "Stansstad", "Hergiswil", "Buochs", "Beckenried",
           "Ennetbürgen", "Ennetbuergen", "Ennetmoos", "Emmetten", "Dallenwil", "Wolfenschiessen", "Bürgenstock"),
    "OW": ("Obwalden", "Obwaldner", "Unterwalden", "OW", "Sarnen", "Kerns", "Sachseln", "Alpnach", "Giswil",
           "Lungern", "Engelberg", "Melchsee-Frutt"),
}


def quellen_fuer(kantone: list[str]) -> list[Quelle]:
    seen: dict[str, Quelle] = {}
    for k in kantone:
        for q in REGISTRY.get(k.upper(), []):
            seen.setdefault(q.host, q)
    return list(seen.values())


def orte_fuer(kantone: list[str]) -> list[str]:
    return [o for k in kantone for o in ORTE.get(k.upper(), ())]
