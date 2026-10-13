"""Official sources that publish through a public JSON API instead of list pages:
the cantonal Amtsblatt portal (building applications) and simap (public tenders for services).

The API answer is the evidence: such entries need no page fetch by an agent."""
from __future__ import annotations

import re
import time
from datetime import date
from typing import NamedTuple

import httpx

from .config import RunConfig
from .tools import PAUSE, RunLog

AMTSBLATT = "https://amtsblattportal.ch/api/v1/publications"
SIMAP = "https://www.simap.ch/api/publications/v2/project/project-search"
PAGE_SIZE = 100
MAX_PAGES = 3

# building applications that point to a real investment, not to a heat pump or a carport
GROSSES_BAUVORHABEN = re.compile(
    r"Mehrfamilienhaus|Überbauung|Gewerbe|Industrie|Hotel|Büro|Produktion|Werkhalle|Lagerhalle|Areal|Erweiterung|Neubau Wohn|Wohnüberbauung", re.I
)
KLEINES_BAUVORHABEN = re.compile(r"Wärmepumpe|Photovoltaik|Solar|Carport|Parkplatz|Reklame|Zaun|Gartenhaus|Pergola|Balkon|Sitzplatz|Mobilfunk|Antenne|provisorisch", re.I)


class Roh(NamedTuple):
    datum: date
    art: str
    quelle: str
    titel: str
    url: str
    quellgattung: str
    anriss: str


def _json(log: RunLog, host: str, url: str, params: dict) -> dict | None:
    try:
        r = log.client.get(url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.mark_blocked(host, f"API-Fehler ({type(e).__name__})")
        return None
    finally:
        time.sleep(PAUSE)
    log.mark_ok(host)
    return data


def amtsblatt(cfg: RunConfig, log: RunLog) -> tuple[list[Roh], int]:
    """Building applications of large projects. Returns (entries, requests made)."""
    out: list[Roh] = []
    requests = 0
    for kanton in cfg.kantone:
        for page in range(MAX_PAGES):
            data = _json(log, "amtsblattportal.ch", AMTSBLATT, {
                "tenant": "kantone", "cantons": kanton, "publicationStates": "PUBLISHED", "includeContent": "false",
                "publicationDate.start": cfg.nachzuegler_start.isoformat(), "publicationDate.end": cfg.laufdatum.isoformat(),
                "pageRequest.page": page, "pageRequest.size": PAGE_SIZE,
            })
            if data is None:
                break
            requests += 1
            for item in data.get("content", []):
                meta = item["meta"]
                titel = (meta.get("title") or {}).get("de", "")
                if not titel.startswith("Baugesuch") or not GROSSES_BAUVORHABEN.search(titel) or KLEINES_BAUVORHABEN.search(titel):
                    continue
                url = f"https://www.amtsblattportal.ch/#!/search/publications/detail/{meta['id']}"
                out.append(Roh(date.fromisoformat(meta["publicationDate"][:10]), "Baugesuch", f"Amtsblatt {kanton}", titel, url, "Kanton und Gemeinden", ""))
            if (page + 1) * PAGE_SIZE >= data.get("total", 0):
                break
    return out, requests


def simap(cfg: RunConfig, log: RunLog) -> tuple[list[Roh], int]:
    """Public tenders for services (audit, consulting, IT, insurance ...) of authorities in the region."""
    out: list[Roh] = []
    requests = 0
    for kanton in cfg.kantone:
        data = _json(log, "simap.ch", SIMAP, {"orderAddressCantons": kanton, "projectSubTypes": "service"})
        if data is None:
            continue
        requests += 1
        for p in data.get("projects", []):
            d = date.fromisoformat(p["publicationDate"])
            if p.get("pubType") != "tender" or not cfg.nachzuegler_start <= d <= cfg.laufdatum:
                continue
            stelle = (p.get("procOfficeName") or {}).get("de") or ""
            ort = ((p.get("orderAddress") or {}).get("city") or {}).get("de") or ""
            out.append(Roh(d, "Ausschreibung", "simap", (p.get("title") or {}).get("de") or "", f"https://www.simap.ch/project-detail/{p['id']}",
                           "Kanton und Gemeinden", f"{stelle} · {ort}".strip(" ·")))
    return out, requests
