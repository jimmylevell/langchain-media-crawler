"""Entry point for Microsoft Foundry hosted agents (Responses protocol).

`langgraph.json` points the hosting adapter (`langchain_azure_ai.agents.hosting.run`)
at `create_graph`. The adapter serves POST /responses on port 8088 and returns
whatever this graph appends to `messages`.

    START ──► intake ──► scan ──► END
                 └──► (one clarifying question) ──► END

- intake: reads the chat message and sets the run variables (region, period, ...).
- scan:   runs the multi-agent scan (scanner/graph.py) and returns the report.
"""
from __future__ import annotations

from typing import Annotated

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from scanner.config import RunConfig
from scanner.graph import run_scan
from scanner.llm import make_llm

INTAKE_PROMPT = """\
Du nimmst Aufträge für den Medien-Scanner für Akquiseimpulse entgegen und setzt die Laufvariablen.
Standard: Niederlassung Stans, Region «Nidwalden, Obwalden und angrenzende Gemeinden» (Kantone NW, OW), letzte 7 Tage.
Nennt der Benutzer eine andere Niederlassung, Region oder Periode, überschreibt das den Standard; sonst Feld leer bzw. 0 lassen.
`kantone`: Kantonskürzel der genannten Region (z.B. UR, SZ, LU), leer wenn Standard.
`geplant`: true, wenn die Nachricht leer ist, nur einen Start auslöst («Scan starten», «wöchentlicher Lauf») oder kein Mensch mitliest.
`rueckfrage`: nur ausfüllen, wenn Region oder Zeitraum tatsächlich unklar oder widersprüchlich sind – eine einzige kurze Frage. Im Zweifel leer lassen und Standard verwenden."""


class Intake(BaseModel):
    firma: str = Field(description="Firmenname oder ''")
    niederlassung: str = Field(description="Niederlassung oder ''")
    region: str = Field(description="Region oder ''")
    kantone: list[str] = Field(description="Kantonskürzel oder []")
    tage: int = Field(description="Zeitraum in Tagen, 0 = Standard")
    kontaktentwurf: bool = Field(description="true, wenn ein Entwurf für die Kontaktaufnahme verlangt wird")
    geplant: bool
    rueckfrage: str


class State(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]  # the channel the Responses host returns
    intake: Intake


def create_graph():
    """Graph factory loaded by the hosting adapter."""
    llm = make_llm()

    def intake(state: State) -> State:
        result: Intake = llm.with_structured_output(Intake, method="function_calling").invoke(
            [("system", INTAKE_PROMPT), *state["messages"]]
        )
        already_asked = any(isinstance(m, AIMessage) for m in state["messages"])
        if result.rueckfrage and not result.geplant and not already_asked:  # at most one question
            return {"messages": [AIMessage(content=result.rueckfrage)]}
        return {"intake": result.model_copy(update={"rueckfrage": ""})}

    def scan(state: State) -> State:
        i, d = state["intake"], RunConfig()
        overridden = (i.niederlassung or d.niederlassung, i.region or d.region, i.tage or d.tage) != (d.niederlassung, d.region, d.tage)
        cfg = RunConfig(
            firma=i.firma or d.firma,
            niederlassung=i.niederlassung or d.niederlassung,
            region=i.region or d.region,
            kantone=[k.upper() for k in i.kantone] or d.kantone,
            tage=min(max(i.tage, 0), 31) or d.tage,
            kontaktentwurf=i.kontaktentwurf,
            geplant=i.geplant or not overridden,
        )
        result = run_scan(cfg, llm=llm)
        return {"messages": [AIMessage(content=result["markdown"])]}

    g = StateGraph(State)
    g.add_node("intake", intake)
    g.add_node("scan", scan)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", lambda s: "scan" if s.get("intake") else END, ["scan", END])
    g.add_edge("scan", END)
    return g.compile()
