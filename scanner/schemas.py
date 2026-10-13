"""Structured outputs exchanged between the agents.

Every agent returns one of these models instead of free text, so the next
agent (and the deterministic checks) get clean, parseable data.
All fields are required on purpose: use "" / [] for "unknown".

The researchers extract facts and features (place, type, size); the priority is
calculated from them in scanner/scoring.py, not chosen by a model.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Quellgattung = Literal[
    "SHAB/Handelsregister",
    "Revisionsstellen-Mutationen",
    "Stelleninserate",
    "Lokale Medien",
    "Kanton und Gemeinden",
    "Politische Agenda",
    "Events",
]
QUELLGATTUNGEN: tuple[str, ...] = Quellgattung.__args__  # type: ignore[attr-defined]

Ereignistyp = Literal[
    "nachfolge_uebernahme",  # succession, owner change, M&A, financing round
    "revisionsstelle",  # auditor change or vacancy, opting-out / opting-in
    "fuehrungswechsel",  # CEO, CFO, board or management change
    "investition_expansion",  # new site, large investment, strong growth, big order
    "jubilaeum_meilenstein",  # anniversary, award, record year, handover to next generation
    "stelle_finanzen",  # finance / controlling / accounting vacancy
    "ausschreibung",  # public tender
    "gruendung_sitzverlegung",  # new company, seat moved into the region
    "sanierung_krise",  # restructuring, crisis indicator, bankruptcy of a company with substance
    "politik_behoerde",  # political decision, municipal or cantonal project with a client link
    "event",  # event where target companies or decision makers take part
    "auszeichnung",  # prize, election, appointment as occasion for a congratulation
    "liquidation_loeschung",  # liquidation, deletion
    "sonstiges",
]
Groesse = Literal["gross", "mittel", "klein", "unbekannt"]


# --- Planner -> Researchers -------------------------------------------------
class Task(BaseModel):
    quellgattung: Quellgattung
    auftrag: str = Field(description="Konkreter Rechercheauftrag in 1-3 Sätzen")
    suchhinweise: list[str] = Field(description="Suchbegriffe, Portale oder URLs als Startpunkt")


class Plan(BaseModel):
    tasks: list[Task]


# --- Researchers -> Editor --------------------------------------------------
class Person(BaseModel):
    name: str
    funktion: str = Field(description="Funktion genau wie in der Quelle belegt")


class Finding(BaseModel):
    organisation: str = Field(description="Organisation, Person oder Gemeinde")
    ort: str = Field(description="Gemeinde, auf die sich der Regionsbezug stützt, wie in der Quelle genannt (z.B. 'Stans'); nur der Kanton, wenn keine Gemeinde genannt ist")
    ereignistyp: Ereignistyp = Field(description="Art des Ereignisses; im Zweifel 'sonstiges'")
    groesse: Groesse = Field(description="Grösse des Ereignisses oder der Organisation, nur nach belegten Angaben (Franken, Mitarbeitende, Kapital); sonst 'unbekannt'")
    ereignis: str = Field(description="Was ist passiert - nur belegte Fakten")
    ereignisdatum: str = Field(description="YYYY-MM-DD oder '' wenn nicht belegt")
    publikationsdatum: str = Field(description="YYYY-MM-DD, wie in der Quelle angegeben")
    quelle_name: str = Field(description="Name des Mediums / Portals")
    quelle_url: str = Field(description="Direkte URL der Meldung, die mit fetch_page oder shab_publications gelesen wurde")
    personen: list[Person]
    belegzitat: str = Field(description="Kurzes wörtliches Zitat (max. 20 Wörter) aus der Quelle, das das Ereignis belegt")


class Blocked(BaseModel):
    quelle: str
    grund: str = Field(description="Paywall, 403, robots.txt, Timeout ...")


class Discarded(BaseModel):
    was: str = Field(description="Organisation und Ereignis in wenigen Worten")
    grund: str = Field(description="Ein Halbsatz, max. 12 Wörter")


class TaskResult(BaseModel):
    findings: list[Finding]
    nicht_erreichbar: list[Blocked]
    verworfen: list[Discarded]


# --- Editor -> Report -------------------------------------------------------
class EditorRow(BaseModel):
    """Only the wording. Facts, links, dates and the priority come from the finding, not from the editor."""

    nr: int = Field(description="Nummer des Funds in der Liste")
    ereignis: str = Field(description="max. 40 Wörter, nur belegte Fakten")
    relevanz: str = Field(description="max. 30 Wörter; jede Interpretation mit '(Einschätzung)' markiert")
    aktion: str = Field(description="Ein diese Woche ausführbarer Schritt, beginnt mit einem Verb")
    kontaktentwurf: str = Field(description="max. 70 Wörter, nur beim Fund mit der Markierung TOP und nur wenn angefordert, sonst ''")


class Report(BaseModel):
    zeilen: list[EditorRow]
    verworfen: list[Discarded] = Field(description="Geprüfte, aber bewusst weggelassene Funde (Doubletten, zu schwach)")


class ReportRow(BaseModel):
    """A finished row: the finding's facts plus the editor's wording, built in code."""

    prio: Literal["HOCH", "MITTEL", "NIEDRIG"]
    score: float
    liste: Literal["lokal", "shab"]
    organisation: str
    ort: str
    ereignis: str
    ereignisdatum: str
    publikationsdatum: str
    relevanz: str
    aktion: str
    kontaktentwurf: str
    quelle_name: str
    quelle_url: str
