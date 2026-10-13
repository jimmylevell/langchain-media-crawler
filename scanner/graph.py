"""The multi-agent workflow as a LangGraph graph.

    collect ─┐
             ├─► dispatch ──► research (one agent per task, in parallel) ──► pruefen ──► edit ──► check ──► END
    plan ────┘

- collect:  plain Python, no LLM: reads the list pages and feeds of all registered sites, the official
            APIs (Amtsblatt, simap) and newly discovered outlets, and keeps the entries inside the
            time window (scanner/collect.py).
- plan:     Planner LLM splits the rest of the scan into tasks (count is dynamic). Task types that
            found nothing in the last runs are skipped for a while (scanner/store.py).
- dispatch: turns the collected entries into ready-made reading tasks and adds them.
- research: each task is its own tool-using agent with its own context window.
            Only its cleaned, structured result flows on – not its reasoning.
- pruefen:  plain Python: evidence, time window, region, duplicates, score and priority (scanner/report.py, scoring.py).
- edit:     Editor LLM words the surviving findings; it sets no priority.
- check:    plain Python: two lists, history, Markdown.
"""
from __future__ import annotations

import json
import logging
import operator
import time
from typing import Annotated, Callable, TypedDict

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import prompts, telemetry
from .collect import Sammlung, collect
from .config import MAX_PARALLEL_AGENTS, RunConfig
from .report import Kandidat, listen, pruefen, render, zeilen_bauen
from .schemas import QUELLGATTUNGEN, Blocked, Discarded, Plan, Report, Task, TaskResult
from .sources import quellen_fuer
from .store import load_history, record_yield, save_history, skipped_types
from .telemetry import event
from .tools import RunLog, make_tools

BATCH = 30  # collected entries per reading task
NIE_UEBERSPRINGEN = {"SHAB/Handelsregister", "Revisionsstellen-Mutationen"}  # fed by the SHAB API, an empty week is a valid answer


class State(TypedDict, total=False):
    tasks: list[Task]
    sammlung: Sammlung
    jobs: list[dict]  # {"task": Task, "eintraege": [lines], "geplant": bool} – what each research agent gets
    uebersprungen: dict[str, str]
    results: Annotated[list[TaskResult], operator.add]  # parallel agents append here
    lauf: Annotated[list[dict], operator.add]  # {"gattung", "geplant", "funde"} per finished agent
    kandidaten: list[Kandidat]
    verworfen: list[Discarded]
    report: Report
    markdown: str
    treffer: int


def build_graph(cfg: RunConfig, llm: BaseChatModel, log: RunLog, on_event: Callable[[str], None] = lambda _: None):
    history = load_history(cfg.laufdatum)
    gesammelte_websites = [q.host.removeprefix("www.") for q in quellen_fuer(cfg.kantone)]

    def collect_node(state: State) -> State:
        sammlung = collect(cfg, log, on_event)
        event("collect_done", listen=sammlung.listen_gelesen, websites=sammlung.websites, eintraege=sammlung.eintraege_total,
              im_fenster=len(sammlung.eintraege), entdeckt=[o.host for o in sammlung.entdeckt])
        return {"sammlung": sammlung}

    def plan(state: State) -> State:
        result: Plan = llm.with_structured_output(Plan, method="function_calling").with_retry(stop_after_attempt=2).invoke(
            [("system", prompts.planner(cfg, gesammelte_websites)), ("user", "Erstelle den Rechercheplan für diesen Lauf.")]
        )
        skipped = skipped_types(cfg.laufdatum, NIE_UEBERSPRINGEN)
        tasks = [t for t in result.tasks[:12] if t.quellgattung not in skipped]
        event("plan_done", tasks=len(tasks), uebersprungen=list(skipped))
        return {"tasks": tasks, "uebersprungen": skipped}

    def dispatch(state: State) -> State:
        """Joins planner and collector: collected entries become reading tasks."""
        jobs = [{"task": t, "eintraege": [], "geplant": True} for t in state["tasks"]]
        eintraege = state["sammlung"].eintraege
        for gattung in sorted({e.quellgattung for e in eintraege}):
            lines = [e.zeile() for e in eintraege if e.quellgattung == gattung]
            for i in range(0, len(lines), BATCH):
                task = Task(
                    quellgattung=gattung,
                    auftrag=f"Sichte {len(lines[i:i + BATCH])} vorab gesammelte Einträge der Gattung «{gattung}» und lies die relevanten.",
                    suchhinweise=[],
                )
                jobs.append({"task": task, "eintraege": lines[i:i + BATCH], "geplant": False})
        # Guarantee: every mandatory source category is covered, unless it is paused for lack of yield.
        for q in QUELLGATTUNGEN:
            if q not in state["uebersprungen"] and not any(j["task"].quellgattung == q for j in jobs):
                jobs.append({"task": Task(quellgattung=q, auftrag=f"Prüfe die Quellgattung «{q}» für {cfg.region}.", suchhinweise=[]), "eintraege": [], "geplant": True})
        return {"jobs": jobs, "tasks": [j["task"] for j in jobs]}

    def fan_out(state: State) -> list[Send]:
        return [Send("research", job) for job in state["jobs"]]

    def research(payload: dict) -> State:
        task: Task = payload["task"]
        auftrag = f"Quellgattung: {task.quellgattung}\nAuftrag: {task.auftrag}\nStartpunkte: {'; '.join(task.suchhinweise) or '–'}"
        if payload["eintraege"]:
            auftrag += prompts.GESAMMELT + "\n".join(payload["eintraege"])
        # A fresh agent per task: own context window, own tool-call budget.
        researcher = create_agent(
            llm, make_tools(cfg, log), system_prompt=prompts.researcher(cfg), response_format=ToolStrategy(TaskResult)
        )
        start, tool_calls = time.monotonic(), 0
        try:
            out = researcher.invoke({"messages": [("user", auftrag)]}, {"recursion_limit": 60})
            result: TaskResult = out["structured_response"]
            tool_calls = sum(m.type == "tool" for m in out["messages"])
        except Exception as e:  # one failed agent must not sink the whole run
            telemetry.logger.exception("research_failed", extra={"fields": {"gattung": task.quellgattung, "auftrag": task.auftrag[:120]}})
            result = TaskResult(
                findings=[], verworfen=[],
                nicht_erreichbar=[Blocked(quelle=f"Auftrag «{task.quellgattung}»", grund=f"Agent abgebrochen ({type(e).__name__})")],
            )
        event("research_done", gattung=task.quellgattung, geplant=payload["geplant"], eintraege=len(payload["eintraege"]),
              funde=len(result.findings), tool_calls=tool_calls, sekunden=round(time.monotonic() - start))
        return {"results": [result], "lauf": [{"gattung": task.quellgattung, "geplant": payload["geplant"], "funde": len(result.findings)}]}

    def pruefen_node(state: State) -> State:
        findings = [f for r in state["results"] for f in r.findings]
        kandidaten, dropped, cut = pruefen(findings, cfg, log, history)
        event("pruefen_done", funde=len(findings), kandidaten=len(kandidaten), verworfen=len(dropped), abgeschnitten=len(cut),
              scores=[{"org": k.f.organisation, "ort": k.f.ort, "typ": k.f.ereignistyp, "score": k.score, "prio": k.prio} for k in kandidaten])
        return {"kandidaten": kandidaten, "verworfen": dropped + cut}

    def edit(state: State) -> State:
        kandidaten = state["kandidaten"]
        if not kandidaten:
            return {"report": Report(zeilen=[], verworfen=[])}
        liste = [
            {
                "nr": k.nr, "markierung": "TOP" if i == 0 else "", "liste": k.liste, "prio": k.prio, "organisation": k.f.organisation,
                "ort": k.f.ort, "ereignistyp": k.f.ereignistyp, "ereignis": k.f.ereignis, "ereignisdatum": k.f.ereignisdatum,
                "publikationsdatum": k.f.publikationsdatum, "quelle": k.f.quelle_name, "quelle_url": k.f.quelle_url,
                "personen": [p.model_dump() for p in k.f.personen], "belegzitat": k.f.belegzitat,
            }
            for i, k in enumerate(kandidaten)
        ]
        user = (
            "Geprüfte Funde, nach Priorität sortiert (JSON):\n" + json.dumps(liste, ensure_ascii=False, indent=1)
            + "\n\nFrüher gemeldet (letzte 8 Wochen):\n"
            + (json.dumps([{k: h[k] for k in ("organisation", "ereignis", "url")} for h in history], ensure_ascii=False) if history else "nichts")
        )
        # One retry: an answer that does not fit the schema should cost a second call, not the run.
        report: Report = llm.with_structured_output(Report, method="function_calling").with_retry(stop_after_attempt=2).invoke(
            [("system", prompts.editor(cfg)), ("user", user)]
        )
        event("edit_done", kandidaten=len(kandidaten), zeilen=len(report.zeilen), verworfen=[d.model_dump() for d in report.verworfen])
        if not report.zeilen:
            event("edit_empty", logging.WARNING, kandidaten=len(kandidaten))
        return {"report": report}

    def check(state: State) -> State:
        rows, ohne_aktion = zeilen_bauen(state["report"], state["kandidaten"], cfg)
        lokal, shab, weitere = listen(rows, cfg)
        if not cfg.dry_run:
            save_history(
                [{"gemeldet_am": cfg.laufdatum.isoformat(), "organisation": r.organisation, "ereignis": r.ereignis, "url": r.quelle_url} for r in lokal + shab],
                cfg.laufdatum,
            )
            per_type: dict[str, int] = {}
            for entry in state["lauf"]:
                if entry["geplant"]:
                    per_type[entry["gattung"]] = per_type.get(entry["gattung"], 0) + entry["funde"]
            record_yield(per_type, cfg.laufdatum)
        md = render(cfg, lokal, shab, weitere, state["results"], state["verworfen"] + ohne_aktion, state["report"].verworfen, log,
                    state["tasks"], state["sammlung"], state["uebersprungen"])
        return {"markdown": md, "treffer": len(lokal) + len(shab)}

    g = StateGraph(State)
    g.add_node("collect", collect_node)
    g.add_node("plan", plan)
    g.add_node("dispatch", dispatch)
    g.add_node("research", research)
    g.add_node("pruefen", pruefen_node)
    g.add_node("edit", edit)
    g.add_node("check", check)
    g.add_edge(START, "collect")  # collector and planner run side by side
    g.add_edge(START, "plan")
    g.add_edge(["collect", "plan"], "dispatch")
    g.add_conditional_edges("dispatch", fan_out, ["research"])
    g.add_edge("research", "pruefen")
    g.add_edge("pruefen", "edit")
    g.add_edge("edit", "check")
    g.add_edge("check", END)
    return g.compile()


def run_scan(cfg: RunConfig, llm: BaseChatModel | None = None, on_event: Callable[[str], None] = lambda _: None) -> dict:
    """Runs one scan. Returns {'markdown', 'treffer', 'usage', 'blocked'}."""
    if llm is None:
        from .llm import make_llm

        llm = make_llm()
    log = RunLog()
    usage = UsageMetadataCallbackHandler()  # token tracking across all agents
    graph = build_graph(cfg, llm, log, on_event)
    final: dict = {}
    done = 0
    event("scan_start", region=cfg.region, kantone=cfg.kantone, tage=cfg.tage, dry_run=cfg.dry_run)
    with telemetry.span("scan", region=cfg.region, tage=cfg.tage, dry_run=cfg.dry_run) as set_attrs:
        for update in graph.stream({}, {"callbacks": [usage], "max_concurrency": MAX_PARALLEL_AGENTS}, stream_mode="updates"):
            for node, value in update.items():
                final.update(value or {})
                if node == "collect":
                    on_event(f"Sammlung: {value['sammlung'].zusammenfassung()}")
                elif node == "dispatch":
                    on_event(f"Plan: {len(value['jobs'])} Rechercheaufträge")
                    for j in value["jobs"]:
                        on_event(f"  · [{j['task'].quellgattung}] {j['task'].auftrag[:110]}")
                elif node == "research":
                    done += 1
                    on_event(f"Recherche {done} fertig: {len(value['results'][0].findings)} Funde")
                elif node == "pruefen":
                    on_event(f"Prüfung: {len(value['kandidaten'])} Kandidaten, {len(value['verworfen'])} aussortiert")
                elif node == "edit":
                    on_event(f"Redaktion: {len(value['report'].zeilen)} Zeilen formuliert")
                elif node == "check":
                    on_event(f"Selbstprüfung: {value['treffer']} Treffer im Bericht")
        tokens = {
            "input": sum(u.get("input_tokens", 0) for u in usage.usage_metadata.values()),
            "output": sum(u.get("output_tokens", 0) for u in usage.usage_metadata.values()),
        }
        set_attrs(treffer=final["treffer"], pages_read=len(log.read_urls), searches=log.searches, input_tokens=tokens["input"], output_tokens=tokens["output"])
    event("scan_done", treffer=final["treffer"], pages_read=len(log.read_urls), blocked=dict(log.blocked), **{f"tokens_{k}": v for k, v in tokens.items()})
    return {
        "markdown": final["markdown"], "treffer": final["treffer"], "usage": tokens,
        "blocked": dict(log.blocked), "pages_read": len(log.read_urls), "searches": log.searches,
    }
