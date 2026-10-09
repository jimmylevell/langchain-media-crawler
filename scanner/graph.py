"""The multi-agent workflow as a LangGraph graph.

    collect ─┐
             ├─► dispatch ──► research (one agent per task, in parallel) ──► edit ──► check ──► END
    plan ────┘

- collect:  plain Python, no LLM: reads the list pages of all registered canton and
            municipality sites slowly, one after the other, and keeps the entries
            inside the time window (scanner/collect.py).
- plan:     Planner LLM splits the rest of the scan into tasks (count is dynamic).
- dispatch: turns the collected entries into ready-made reading tasks and adds them.
- research: each task is its own tool-using agent with its own context window.
            Only its cleaned, structured result flows on – not its reasoning.
- edit:     Editor LLM sees all results, dedupes, prioritises, writes the rows.
- check:    plain Python: self-check, history, Markdown.
"""
from __future__ import annotations

import json
import operator
from typing import Annotated, Callable, TypedDict

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import prompts
from .collect import Sammlung, collect
from .config import MAX_PARALLEL_AGENTS, RunConfig
from .report import load_history, render, save_history, validate
from .schemas import QUELLGATTUNGEN, Blocked, Plan, Report, Task, TaskResult
from .sources import quellen_fuer
from .tools import RunLog, make_tools

BATCH = 30  # collected entries per reading task


class State(TypedDict, total=False):
    tasks: list[Task]
    sammlung: Sammlung
    jobs: list[dict]  # {"task": Task, "eintraege": [lines]} – what each research agent gets
    results: Annotated[list[TaskResult], operator.add]  # parallel agents append here
    report: Report
    markdown: str
    treffer: int


def build_graph(cfg: RunConfig, llm: BaseChatModel, log: RunLog, on_event: Callable[[str], None] = lambda _: None):
    history = load_history(cfg.laufdatum)
    gesammelte_websites = [q.host.removeprefix("www.") for q in quellen_fuer(cfg.kantone)]

    def collect_node(state: State) -> State:
        return {"sammlung": collect(cfg, log, on_event)}

    def plan(state: State) -> State:
        result: Plan = llm.with_structured_output(Plan, method="function_calling").with_retry(stop_after_attempt=2).invoke(
            [("system", prompts.planner(cfg, gesammelte_websites)), ("user", "Erstelle den Rechercheplan für diesen Lauf.")]
        )
        return {"tasks": result.tasks[:12]}

    def dispatch(state: State) -> State:
        """Joins planner and collector: collected entries become reading tasks."""
        jobs = [{"task": t, "eintraege": []} for t in state["tasks"]]
        eintraege = state["sammlung"].eintraege
        for gattung in sorted({e.quellgattung for e in eintraege}):
            lines = [e.zeile() for e in eintraege if e.quellgattung == gattung]
            for i in range(0, len(lines), BATCH):
                task = Task(
                    quellgattung=gattung,
                    auftrag=f"Sichte {len(lines[i:i + BATCH])} vorab gesammelte Einträge der Gattung «{gattung}» und lies die relevanten.",
                    suchhinweise=[],
                )
                jobs.append({"task": task, "eintraege": lines[i:i + BATCH]})
        # Guarantee: every mandatory source category is covered, whatever the planner did.
        for q in QUELLGATTUNGEN:
            if not any(j["task"].quellgattung == q for j in jobs):
                jobs.append({"task": Task(quellgattung=q, auftrag=f"Prüfe die Quellgattung «{q}» für {cfg.region}.", suchhinweise=[]), "eintraege": []})
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
        try:
            out = researcher.invoke({"messages": [("user", auftrag)]}, {"recursion_limit": 60})
            result: TaskResult = out["structured_response"]
        except Exception as e:  # one failed agent must not sink the whole run
            result = TaskResult(
                findings=[], verworfen=[],
                nicht_erreichbar=[Blocked(quelle=f"Auftrag «{task.quellgattung}»", grund=f"Agent abgebrochen ({type(e).__name__})")],
            )
        return {"results": [result]}

    def edit(state: State) -> State:
        findings = [f.model_dump() for r in state["results"] for f in r.findings]
        if not findings:
            return {"report": Report(zeilen=[], verworfen=[], top=dict(organisation="", naechster_schritt="", fakten="", einschaetzung="", kontaktentwurf=""))}
        user = (
            "Funde der Recherche-Agenten (JSON):\n" + json.dumps(findings, ensure_ascii=False, indent=1)
            + "\n\nFrüher gemeldet (letzte 8 Wochen):\n"
            + (json.dumps([{k: h[k] for k in ("organisation", "ereignis", "url")} for h in history], ensure_ascii=False) if history else "nichts")
        )
        # One retry: an answer that does not fit the schema should cost a second call, not the run.
        report: Report = llm.with_structured_output(Report, method="function_calling").with_retry(stop_after_attempt=2).invoke(
            [("system", prompts.editor(cfg)), ("user", user)]
        )
        return {"report": report}

    def check(state: State) -> State:
        rows, dropped, cut = validate(state["report"], cfg, log, history)
        save_history(history, rows, cfg.laufdatum)
        md = render(cfg, rows, state["report"], state["results"], dropped, cut, log, state["tasks"], state["sammlung"])
        return {"markdown": md, "treffer": len(rows)}

    g = StateGraph(State)
    g.add_node("collect", collect_node)
    g.add_node("plan", plan)
    g.add_node("dispatch", dispatch)
    g.add_node("research", research)
    g.add_node("edit", edit)
    g.add_node("check", check)
    g.add_edge(START, "collect")  # collector and planner run side by side
    g.add_edge(START, "plan")
    g.add_edge(["collect", "plan"], "dispatch")
    g.add_conditional_edges("dispatch", fan_out, ["research"])
    g.add_edge("research", "edit")
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
            elif node == "edit":
                on_event(f"Redaktion: {len(value['report'].zeilen)} Zeilen vorgeschlagen")
            elif node == "check":
                on_event(f"Selbstprüfung: {value['treffer']} Treffer im Bericht")
    tokens = {
        "input": sum(u.get("input_tokens", 0) for u in usage.usage_metadata.values()),
        "output": sum(u.get("output_tokens", 0) for u in usage.usage_metadata.values()),
    }
    return {
        "markdown": final["markdown"], "treffer": final["treffer"], "usage": tokens,
        "blocked": dict(log.blocked), "pages_read": len(log.read_urls), "searches": log.searches,
    }
