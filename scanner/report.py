"""Deterministic part: the seven-point self-check, the 8-week history and the
Markdown layout. Rules that can be checked in code are checked in code, not by an LLM."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from .collect import Sammlung
from .config import DATA_DIR, RunConfig
from .schemas import QUELLGATTUNGEN, Blocked, Discarded, Report, ReportRow, Task, TaskResult
from .tools import RunLog, norm_url

HISTORY = DATA_DIR / "history.json"
PRIO_ORDER = {"HOCH": 0, "MITTEL": 1, "NIEDRIG": 2}
KEINE_TREFFER = "**«Keine neuen relevanten Ereignisse im gewählten Zeitraum gefunden.»**"


# --- history (events reported in the last 8 weeks) ---------------------------
def load_history(today: date) -> list[dict]:
    if not HISTORY.exists():
        return []
    cutoff = (today - timedelta(weeks=8)).isoformat()
    return [h for h in json.loads(HISTORY.read_text("utf-8")) if h["gemeldet_am"] >= cutoff]


def save_history(history: list[dict], rows: list[ReportRow], today: date) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = history + [
        {"gemeldet_am": today.isoformat(), "organisation": r.organisation, "ereignis": r.ereignis, "url": r.quelle_url}
        for r in rows
    ]
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=1), "utf-8")


def bestand(organisation: str) -> str:
    """Optional data/kunden.txt (one client name per line). Without it: 'ungeprüft'."""
    f = DATA_DIR / "kunden.txt"
    if not f.exists():
        return "ungeprüft"
    kunden = [k.strip().lower() for k in f.read_text("utf-8").splitlines() if k.strip()]
    org = organisation.lower()
    return "Kunde" if any(k in org or org in k for k in kunden) else "Kein Kunde"


# --- self-check --------------------------------------------------------------
def _parse(d: str) -> date | None:
    try:
        return date.fromisoformat(d[:10])
    except ValueError:
        return None


def validate(report: Report, cfg: RunConfig, log: RunLog, history: list[dict]) -> tuple[list[ReportRow], list[Discarded], int]:
    """Returns (rows to publish, rows dropped by a hard rule, number cut off by the max-10 limit)."""
    keep: list[ReportRow] = []
    dropped: list[Discarded] = []
    seen: set[tuple[str, str]] = set()
    old_urls = {norm_url(h["url"]) for h in history}

    for r in report.zeilen:
        pub = _parse(r.publikationsdatum)
        parts = urlsplit(r.quelle_url)
        key = (norm_url(r.quelle_url), r.organisation.lower())
        if not log.was_read(r.quelle_url):
            reason = "Quelle im Lauf nicht nachweislich gelesen"
        elif not (parts.path.strip("/") or parts.query or parts.fragment):
            reason = "nur Startseite statt Artikel-URL"
        elif pub is None:
            reason = "kein Publikationsdatum belegt"
        elif pub > cfg.laufdatum or pub < cfg.nachzuegler_start:
            reason = f"Publikation {r.publikationsdatum} ausserhalb des Zeitfensters"
        elif pub < cfg.fenster_start and r.prio != "HOCH":
            reason = "älter als Zeitfenster und nicht Priorität HOCH"
        elif key in seen:
            reason = "Doublette"
        elif key[0] in old_urls:
            reason = "bereits in einem Lauf der letzten 8 Wochen gemeldet"
        elif r.prio == "NIEDRIG" and not cfg.mit_niedrig:
            reason = "Priorität NIEDRIG"
        elif not r.aktion.strip():
            reason = "keine ausführbare Aktion"
        else:
            seen.add(key)
            keep.append(r)
            continue
        dropped.append(Discarded(was=r.organisation, grund=reason))

    keep.sort(key=lambda r: r.publikationsdatum, reverse=True)
    keep.sort(key=lambda r: PRIO_ORDER[r.prio])
    return keep[: cfg.max_treffer], dropped, max(0, len(keep) - cfg.max_treffer)


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


def render(
    cfg: RunConfig,
    rows: list[ReportRow],
    report: Report,
    results: list[TaskResult],
    dropped: list[Discarded],
    cut: int,
    log: RunLog,
    tasks: list[Task],
    sammlung: Sammlung | None = None,
) -> str:
    h = sum(r.prio == "HOCH" for r in rows)
    m = sum(r.prio == "MITTEL" for r in rows)
    out = [f"`Medien-Scan {cfg.region} · {cfg.zeitraum} · erstellt am {cfg.laufdatum:%d.%m.%Y} · {len(rows)} Treffer ({h} HOCH, {m} MITTEL)`", ""]
    if cfg.geplant:
        out += [f"_Annahme: geplanter Lauf mit Standardwerten (Niederlassung {cfg.niederlassung}, letzte {cfg.tage} Tage)._", ""]

    if not rows:
        out += [KEINE_TREFFER, ""]
    else:
        out += [
            f"| Prio | Organisation / Person / Gemeinde | Ereignis | Datum (Ereignis / Publikation) | Relevanz für {cfg.firma} | Empfohlene Aktion | Bestand | Quelle |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            pub = _parse(r.publikationsdatum)
            datum = f"{_de(r.ereignisdatum)} / {_de(r.publikationsdatum)}"
            if pub and pub < cfg.fenster_start:
                datum += " (Nachzügler)"
            out.append(
                f"| {r.prio} | {_cell(r.organisation)} | {_cell(r.ereignis)} | {datum} | {_cell(r.relevanz)} | "
                f"{_cell(r.aktion)} | {bestand(r.organisation)} | [{_cell(r.quelle_name)}]({r.quelle_url}) |"
            )
        if cut:
            out += ["", f"{cut} weitere Treffer wurden weggelassen."]

        top = report.top
        if not any(top.organisation.lower() in r.organisation.lower() or r.organisation.lower() in top.organisation.lower() for r in rows):
            # the editor's pick was removed by a hard rule: fall back to the first row, facts only
            first = rows[0]
            top = top.model_copy(update=dict(organisation=first.organisation, naechster_schritt=first.aktion, fakten=first.ereignis, einschaetzung=first.relevanz, kontaktentwurf=""))
        out += [
            "",
            "## Top-Empfehlung",
            f"- Organisation / Person: {_cell(top.organisation)}",
            f"- Empfohlener nächster Schritt: {_cell(top.naechster_schritt)}",
            f"- Begründung: **Fakten:** {_cell(top.fakten)} **Einschätzung:** {_cell(top.einschaetzung)}",
        ]
        if cfg.kontaktentwurf and top.kontaktentwurf:
            out.append(f"- Entwurf für Kontaktaufnahme: {_cell(top.kontaktentwurf)}")

    # Taken from the network log, not from what the agents say they did.
    gattungen = [q for q in QUELLGATTUNGEN if any(t.quellgattung == q for t in tasks)]
    geprueft = f"{len(gattungen)} Quellgattungen ({', '.join(gattungen)}); {log.searches} Suchabfragen; " + (f"{sammlung.zusammenfassung()}; " if sammlung and sammlung.listen_gelesen else "") + "Portale: " + (", ".join(sorted(log.hosts)) or "keine")
    blocked: list[Blocked] = [Blocked(quelle=k, grund=v) for k, v in sorted(log.blocked.items())]
    # Agents' own notes are only used for what the log cannot know: an agent that gave up.
    blocked += [b for res in results for b in res.nicht_erreichbar if b.quelle.startswith("Auftrag ")]
    verworfen = dropped + report.verworfen + [d for res in results for d in res.verworfen]
    out += [
        "",
        "## Abdeckungsprotokoll",
        "- **Geprüft:** " + geprueft,
        "- **Nicht erreichbar:** " + ("; ".join(f"{_cell(b.quelle)} ({_cell(b.grund)}) – {_folge(b.grund)}" for b in blocked) or "keine"),
        "- **Bewusst weggelassen:** " + ("; ".join(f"{_cell(d.was)[:90]} ({_cell(d.grund)[:110]})" for d in verworfen[:15]) or "keine")
        + (f"; sowie {len(verworfen) - 15} weitere" if len(verworfen) > 15 else ""),
    ]
    return "\n".join(out) + "\n"
