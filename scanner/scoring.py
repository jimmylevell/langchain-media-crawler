"""Priority is calculated here, not chosen by a model: the researchers state facts and features
(type, place, size), the weights below decide.

    score = Ereignistyp × Ort × Grösse × Quelle × Aktualität

HOCH from 3.5, MITTEL from 1.8, below that NIEDRIG (not listed unless --mit-niedrig).
Typical results: a succession reported by the local paper scores 5 (HOCH); a small GmbH moving its
seat, seen only in the commercial register, scores about 1.3 (NIEDRIG); an auditor change in the
register scores about 2.5 (MITTEL). Change the numbers when the list feels wrong, nothing else
depends on them.
"""
from __future__ import annotations

import re
from datetime import date

from .config import RunConfig
from .schemas import Finding
from .sources import ort_gewicht, quellentyp

# what happened: how likely it leads to a mandate or a conversation (1 = rarely, 5 = often)
TYP = {
    "nachfolge_uebernahme": 5.0,
    "revisionsstelle": 5.0,
    "fuehrungswechsel": 4.0,
    "investition_expansion": 4.0,
    "jubilaeum_meilenstein": 4.0,
    "stelle_finanzen": 4.0,
    "ausschreibung": 3.0,
    "gruendung_sitzverlegung": 3.0,
    "sanierung_krise": 3.0,
    "politik_behoerde": 3.0,
    "auszeichnung": 2.5,
    "event": 2.0,
    "liquidation_loeschung": 1.0,
    "sonstiges": 1.0,
}
GROESSE = {"gross": 1.3, "mittel": 1.0, "klein": 0.7, "unbekannt": 0.85}
# where we read it: local media see more than the register; the register is the weakest signal
QUELLE = {"medium": 1.0, "behoerde": 0.9, "shab": 0.6}
HOCH_AB = 3.5
MITTEL_AB = 1.8


def lokalitaet(f: Finding, cfg: RunConfig) -> tuple[float | None, str]:
    """(weight, '') for a local finding, (None, reason) for one that must be dropped.
    The place must be a known one of the region, and outside the commercial register it must
    appear in the finding's own text, so a model cannot simply claim it."""
    treffer = ort_gewicht(f.ort, cfg.kantone)
    if treffer is None:
        return None, f"Ort «{f.ort}» liegt ausserhalb der Region"
    gewicht, ort = treffer
    if ort and quellentyp(f.quelle_url) != "shab":  # SHAB is queried per canton; other sources must name the place
        text = f"{f.organisation} {f.ereignis} {f.belegzitat} {f.quelle_name} {f.quelle_url}"
        if not re.search(rf"\b{re.escape(ort)}(?:er|ern|s)?\b", text, re.I):
            return None, f"Ort «{ort}» im Fund nicht belegt"
    return gewicht, ""


def aktualitaet(publikation: date, cfg: RunConfig) -> float:
    age = (cfg.laufdatum - publikation).days
    return 1.0 if age <= 3 else 0.9 if age <= cfg.tage else 0.7


def bewerte(f: Finding, ortgewicht: float, publikation: date, cfg: RunConfig) -> tuple[float, str]:
    """(score, priority)."""
    score = round(TYP[f.ereignistyp] * ortgewicht * GROESSE[f.groesse] * QUELLE[quellentyp(f.quelle_url)] * aktualitaet(publikation, cfg), 2)
    return score, "HOCH" if score >= HOCH_AB else "MITTEL" if score >= MITTEL_AB else "NIEDRIG"
