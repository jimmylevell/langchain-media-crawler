"""Deterministic part: checks on the findings, scoring, the two lists and the Markdown layout.
Rules that can be checked in code are checked in code, not by an LLM.

    findings ─► pruefen() ─► candidates (numbered, scored) ─► editor words them ─► zeilen_bauen() ─► render()

The report has two lists: local reports (media, authorities, tenders, events, jobs) and
commercial-register signals (SHAB), so the register cannot crowd out the local news."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from .collect import Sammlung
from .config import DATA_DIR, SEARXNG_URL, RunConfig
from .schemas import QUELLGATTUNGEN, Blocked, Discarded, Finding, Report, ReportRow, Task, TaskResult
from .scoring import bewerte, lokalitaet
from .sources import quellentyp
from .tools import RunLog, norm_url

KEINE_TREFFER = "**«Keine neuen relevanten Ereignisse im gewählten Zeitraum gefunden.»**"
KANDIDATEN_LOKAL = 15  # what the editor gets to see per list, a little more than ends up in the report
KANDIDATEN_SHAB = 10
LOG_ZEILEN = 10  # "deliberately left out" entries shown in the coverage log


@dataclass
class Kandidat:
    nr: int
    f: Finding
    score: float
    prio: str
    liste: str  # "lokal" | "shab"


def _parse(d: str) -> date | None:
    try:
        return date.fromisoformat(d[:10])
    except ValueError:
        return None


def _org_key(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


# --- checks and scoring before the editor -------------------------------------
def pruefen(findings: list[Finding], cfg: RunConfig, log: RunLog, history: list[dict]) -> tuple[list[Kandidat], list[Discarded], list[Discarded]]:
    """Returns (candidates for the editor, findings dropped by a rule, findings cut by the list limits)."""
    kept: list[tuple[Finding, float, str]] = []
    dropped: list[Discarded] = []
    seen: set[tuple[str, str]] = set()
    old_urls = {norm_url(h["url"]) for h in history}

    for f in findings:
        pub = _parse(f.publikationsdatum)
        parts = urlsplit(f.quelle_url)
        key = (norm_url(f.quelle_url), _org_key(f.organisation))
        ortgewicht, ort_grund = lokalitaet(f, cfg)
        score, prio = bewerte(f, ortgewicht or 0.0, pub, cfg) if pub and ortgewicht else (0.0, "NIEDRIG")
        if not log.was_read(f.quelle_url):
            reason = "Quelle im Lauf nicht nachweislich gelesen"
        elif not (parts.path.strip("/") or parts.query or parts.fragment):
            reason = "nur Startseite statt Artikel-URL"
        elif pub is None:
            reason = "kein Publikationsdatum belegt"
        elif pub > cfg.laufdatum or pub < cfg.nachzuegler_start:
            reason = f"Publikation {f.publikationsdatum} ausserhalb des Zeitfensters"
        elif ortgewicht is None:
            reason = ort_grund
        elif key in seen:
            reason = "Doublette"
        elif key[0] in old_urls:
            reason = "bereits in einem Lauf der letzten 8 Wochen gemeldet"
        elif prio == "NIEDRIG" and not cfg.mit_niedrig:
            reason = f"Priorität NIEDRIG (Score {score})"
        elif pub < cfg.fenster_start and prio != "HOCH":
            reason = "älter als Zeitfenster und nicht Priorität HOCH"
        else:
            seen.add(key)
            kept.append((f, score, prio))
            continue
        dropped.append(Discarded(was=f"{f.organisation} ({f.ort})", grund=reason))

    # the same event reported twice (media and register): keep the stronger one
    best: dict[tuple[str, str], tuple[Finding, float, str]] = {}
    for item in kept:
        k = (_org_key(item[0].organisation), item[0].ereignistyp)
        if k not in best or item[1] > best[k][1]:
            best[k] = item
    dropped += [Discarded(was=f"{f.organisation} ({f.ort})", grund="Doublette mit stärkerem Fund") for f, _, _ in kept if best[(_org_key(f.organisation), f.ereignistyp)][0] is not f]

    ranked = sorted(best.values(), key=lambda x: x[0].publikationsdatum, reverse=True)
    ranked.sort(key=lambda x: -x[1])  # stable: newest first among equal scores
    lokal = [x for x in ranked if quellentyp(x[0].quelle_url) != "shab"]
    shab = [x for x in ranked if quellentyp(x[0].quelle_url) == "shab"]
    cut = [Discarded(was=f"{f.organisation} ({f.ort})", grund="ausserhalb der Kandidatenliste") for f, _, _ in lokal[KANDIDATEN_LOKAL:] + shab[KANDIDATEN_SHAB:]]
    candidates = [
        Kandidat(i, f, score, prio, "lokal" if quellentyp(f.quelle_url) != "shab" else "shab")
        for i, (f, score, prio) in enumerate(lokal[:KANDIDATEN_LOKAL] + shab[:KANDIDATEN_SHAB], start=1)
    ]
    return candidates, dropped, cut


def zeilen_bauen(report: Report, candidates: list[Kandidat], cfg: RunConfig) -> tuple[list[ReportRow], list[Discarded]]:
    """Joins the editor's wording to the facts of its finding. An unknown number is ignored,
    so the editor cannot add a row that no research agent found."""
    by_nr = {k.nr: k for k in candidates}
    rows: list[ReportRow] = []
    dropped: list[Discarded] = []
    for z in report.zeilen:
        k = by_nr.pop(z.nr, None)
        if k is None:
            continue
        if not z.aktion.strip():
            dropped.append(Discarded(was=k.f.organisation, grund="keine ausführbare Aktion"))
            continue
        f = k.f
        rows.append(ReportRow(
            prio=k.prio, score=k.score, liste=k.liste, organisation=f.organisation, ort=f.ort, ereignis=z.ereignis,
            ereignisdatum=f.ereignisdatum, publikationsdatum=f.publikationsdatum, relevanz=z.relevanz, aktion=z.aktion,
            kontaktentwurf=z.kontaktentwurf if cfg.kontaktentwurf else "", quelle_name=f.quelle_name, quelle_url=f.quelle_url,
        ))
    return rows, dropped


def listen(rows: list[ReportRow], cfg: RunConfig) -> tuple[list[ReportRow], list[ReportRow], list[ReportRow]]:
    """(local list, register list, rows cut by the limits), each sorted by score."""
    lokal = sorted((r for r in rows if r.liste == "lokal"), key=lambda r: -r.score)
    shab = sorted((r for r in rows if r.liste == "shab"), key=lambda r: -r.score)
    return lokal[: cfg.max_treffer], shab[: cfg.max_shab], lokal[cfg.max_treffer:] + shab[cfg.max_shab:]


# --- clients -----------------------------------------------------------------
RECHTSFORM = re.compile(r"\b(ag|gmbh|sa|sarl|kg|genossenschaft|stiftung|verein)\b")


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", RECHTSFORM.sub("", name.lower().replace("à", "a"))).split())


def kunden() -> list[str]:
    """Optional data/kunden.txt, one client name per line."""
    f = DATA_DIR / "kunden.txt"
    return [_norm(k) for k in f.read_text("utf-8").splitlines() if k.strip()] if f.exists() else []


def bestand(organisation: str, liste: list[str]) -> str:
    """'ungeprüft' without a client list. A client matches when its name equals the organisation's, or
    (from 5 characters on) appears in it as whole words, so 'Muster' finds 'Muster Bau AG' but not 'Mustermann'."""
    if not liste:
        return "ungeprüft"
    org = _norm(organisation)
    treffer = any(k == org or (len(k) >= 5 and re.search(rf"\b{re.escape(k)}\b", org)) for k in liste if k)
    return "Kunde" if treffer else "Kein Kunde"


# --- rendering ---------------------------------------------------------------
def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", s).replace("|", "/").replace("ß", "ss").strip(' "”')


def _de(d: str) -> str:
    p = _parse(d)
    return f"{p:%d.%m.%Y}" if p else "–"


def _folge(grund: str) -> str:
    if grund.startswith("Anfragebudget"):
        return "Liste gelesen, einzelne Einträge nicht geöffnet"
    if grund.startswith("Paywall: nur"):
        return "Aussagen auf Titel und Anriss beschränkt"
    return "Meldungen dieser Quelle fehlen"


def _tabelle(rows: list[ReportRow], cfg: RunConfig) -> list[str]:
    clients = kunden()
    out = [
        f"| Prio | Organisation / Person | Ort | Ereignis | Datum (Ereignis / Publikation) | Relevanz für {cfg.firma} | Empfohlene Aktion | Bestand | Quelle |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        pub = _parse(r.publikationsdatum)
        datum = f"{_de(r.ereignisdatum)} / {_de(r.publikationsdatum)}"
        if pub and pub < cfg.fenster_start:
            datum += " (Nachzügler)"
        out.append(
            f"| {r.prio} | {_cell(r.organisation)} | {_cell(r.ort)} | {_cell(r.ereignis)} | {datum} | {_cell(r.relevanz)} | "
            f"{_cell(r.aktion)} | {bestand(r.organisation, clients)} | [{_cell(r.quelle_name)}]({r.quelle_url}) |"
        )
    return out


def render(
    cfg: RunConfig,
    lokal: list[ReportRow],
    shab: list[ReportRow],
    weitere: list[ReportRow],
    results: list[TaskResult],
    dropped: list[Discarded],
    editor_verworfen: list[Discarded],
    log: RunLog,
    tasks: list[Task],
    sammlung: Sammlung | None = None,
    uebersprungen: dict[str, str] | None = None,
) -> str:
    rows = lokal + shab
    h = sum(r.prio == "HOCH" for r in rows)
    m = sum(r.prio == "MITTEL" for r in rows)
    out = [f"`Medien-Scan {cfg.region} · {cfg.zeitraum} · erstellt am {cfg.laufdatum:%d.%m.%Y} · {len(rows)} Treffer ({h} HOCH, {m} MITTEL)`", ""]
    if cfg.geplant:
        out += [f"_Annahme: geplanter Lauf mit Standardwerten (Niederlassung {cfg.niederlassung}, letzte {cfg.tage} Tage)._", ""]
    if cfg.dry_run:
        out += ["_Testlauf: Historie und Statistik wurden nicht gespeichert._", ""]

    if not rows:
        out += [KEINE_TREFFER, ""]
    else:
        out += ["## Lokale Meldungen", ""]
        out += _tabelle(lokal, cfg) if lokal else ["Keine lokalen Meldungen mit ausreichender Priorität."]
        out += ["", "## Handelsregister-Signale (SHAB)", ""]
        out += _tabelle(shab, cfg) if shab else ["Keine Handelsregister-Signale mit ausreichender Priorität."]
        if weitere:
            out += ["", "Weitere Treffer, nicht aufgeführt: " + "; ".join(f"{_cell(r.organisation)} ({_cell(r.ort)}, [Quelle]({r.quelle_url}))" for r in weitere)]

        top = rows[0]  # the best local row, or the best register row if there is no local one
        out += [
            "",
            "## Top-Empfehlung",
            f"- Organisation / Person: {_cell(top.organisation)} ({_cell(top.ort)})",
            f"- Empfohlener nächster Schritt: {_cell(top.aktion)}",
            f"- Begründung: **Fakten:** {_cell(top.ereignis)} **Einschätzung:** {_cell(top.relevanz)}",
        ]
        if cfg.kontaktentwurf and top.kontaktentwurf:
            out.append(f"- Entwurf für Kontaktaufnahme: {_cell(top.kontaktentwurf)}")

    # Taken from the network log, not from what the agents say they did.
    gattungen = [q for q in QUELLGATTUNGEN if any(t.quellgattung == q for t in tasks)]
    geprueft = f"{len(gattungen)} Quellgattungen ({', '.join(gattungen)}); {log.searches} Suchabfragen; " + (f"{sammlung.zusammenfassung()}; " if sammlung and sammlung.listen_gelesen else "") + "Portale: " + (", ".join(sorted(log.hosts)) or "keine")
    blocked: list[Blocked] = [Blocked(quelle=k, grund=v) for k, v in sorted(log.blocked.items())]
    # Agents' own notes are only used for what the log cannot know: an agent that gave up.
    blocked += [b for res in results for b in res.nicht_erreichbar if b.quelle.startswith("Auftrag ")]
    verworfen = dropped + editor_verworfen + [d for res in results for d in res.verworfen]
    out += [
        "",
        "## Abdeckungsprotokoll",
        "- **Geprüft:** " + geprueft,
        "- **Nicht erreichbar:** " + ("; ".join(f"{_cell(b.quelle)} ({_cell(b.grund)}) – {_folge(b.grund)}" for b in blocked) or "keine"),
    ]
    if sammlung and (sammlung.entdeckt or SEARXNG_URL):
        out.append(
            "- **Neu entdeckte Medien** (nicht in `sources.py`, ggf. aufnehmen): "
            + ("; ".join(f"{o.host} ({o.treffer} Artikel im Zeitfenster)" for o in sammlung.entdeckt) or "keine")
        )
    elif sammlung:
        out.append("- **Neu entdeckte Medien:** Entdeckung aus (SEARXNG_URL nicht gesetzt)")
    if uebersprungen:
        out.append("- **Übersprungene Rechercheaufträge:** " + "; ".join(f"{g} ({grund})" for g, grund in uebersprungen.items()))
    out.append(
        "- **Bewusst weggelassen:** " + ("; ".join(f"{_cell(d.was)[:90]} ({_cell(d.grund)[:110]})" for d in verworfen[:LOG_ZEILEN]) or "keine")
        + (f"; sowie {len(verworfen) - LOG_ZEILEN} weitere" if len(verworfen) > LOG_ZEILEN else "")
    )
    return "\n".join(out) + "\n"
