"""Structured outputs exchanged between the agents.

Every agent returns one of these models instead of free text, so the next
agent (and the deterministic checks) get clean, parseable data.
All fields are required on purpose: use "" / [] for "unknown".
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
class ReportRow(BaseModel):
    prio: Literal["HOCH", "MITTEL", "NIEDRIG"]
    organisation: str
    ereignis: str = Field(description="max. 40 Wörter, nur belegte Fakten")
    ereignisdatum: str = Field(description="YYYY-MM-DD oder ''")
    publikationsdatum: str = Field(description="YYYY-MM-DD")
    relevanz: str = Field(description="max. 30 Wörter; jede Interpretation mit '(Einschätzung)' markiert")
    aktion: str = Field(description="Ein diese Woche ausführbarer Schritt, beginnt mit einem Verb")
    quelle_name: str
    quelle_url: str

    @field_validator("prio", mode="before")
    @classmethod
    def _prio_deutsch(cls, v: str) -> str:
        """Models occasionally answer in English; that must not sink a finished run."""
        return {"HIGH": "HOCH", "MEDIUM": "MITTEL", "MID": "MITTEL", "LOW": "NIEDRIG"}.get(str(v).strip().upper(), str(v).strip().upper())


class TopEmpfehlung(BaseModel):
    organisation: str
    naechster_schritt: str
    fakten: str
    einschaetzung: str
    kontaktentwurf: str = Field(description="max. 70 Wörter, nur wenn angefordert, sonst ''")


class Report(BaseModel):
    zeilen: list[ReportRow]
    top: TopEmpfehlung
    verworfen: list[Discarded] = Field(description="Geprüfte, aber bewusst weggelassene Ereignisse")
