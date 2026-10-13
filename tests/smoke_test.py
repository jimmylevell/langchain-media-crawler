"""Plumbing test without Azure: a scripted stand-in model drives the real graph and
the real tools (live SHAB + web). Proves wiring, not answer quality.

    uv run python -m tests.smoke_test
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()  # keep the real history untouched

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

from pathlib import Path

from scanner import cache
from scanner.config import RunConfig
from scanner.graph import run_scan
from scanner.tools import RunLog, make_tools


class ScriptedModel(BaseChatModel):
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        names = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"tool_names": names})

    def _call(self, name: str, args: dict) -> ChatResult:
        msg = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if "Plan" in self.tool_names:
            return self._call("Plan", {"tasks": [{"quellgattung": "SHAB/Handelsregister", "auftrag": "Neueintragungen prüfen", "suchhinweise": []}]})
        if "Report" in self.tool_names:
            kandidaten = json.loads(messages[-1].content.split("(JSON):\n")[1].split("\n\nFrüher gemeldet")[0])
            zeilen = [
                {"nr": k["nr"], "ereignis": k["ereignis"], "relevanz": "Revisionsstelle betroffen, Mandat möglich (Einschätzung)",
                 "aktion": "Rufen Sie die Geschäftsleitung an.", "kontaktentwurf": ""}
                for k in kandidaten
            ]
            # one row with an invented number: joining the wording to the findings must drop it
            zeilen.append({**zeilen[0], "nr": 999, "ereignis": "Erfundene AG"})
            return self._call("Report", {"zeilen": zeilen, "verworfen": []})
        # research agent: one SHAB call, then report the first publication
        tool_out = [m for m in messages if isinstance(m, ToolMessage)]
        if not tool_out:
            return self._call("shab_publications", {"stichwort": "", "seite": 0})
        first = next((l for l in tool_out[0].content.splitlines() if l.startswith("- ")), None)
        findings = []
        if first:
            datum, titel, text, url = [p.strip() for p in first[2:].split(" | ")]
            ort = re.sub(r"\s*\(\w\w\)$", "", titel.rsplit(",", 1)[-1].strip())  # "Mutation X AG, Stansstad" -> "Stansstad"
            findings = [{"organisation": re.sub(r"^\S+ ", "", titel), "ort": ort, "ereignistyp": "revisionsstelle", "groesse": "mittel",
                         "ereignis": titel, "ereignisdatum": "", "publikationsdatum": datum,
                         "quelle_name": "SHAB", "quelle_url": url, "personen": [], "belegzitat": text[:80]}]
        return self._call("TaskResult", {"findings": findings, "nicht_erreichbar": [], "verworfen": []})


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cache.CACHE_DIR = Path("data/cache")  # reuse collected lists instead of fetching them again for a test
    cfg = RunConfig(dry_run=True)

    print("== Tools (live) ==")
    log = RunLog()
    fetch_page, list_links, shab = make_tools(cfg, log)  # no web_search without SEARXNG_URL
    for name, out in [
        ("shab_publications", shab.invoke({"stichwort": "Revisionsstelle"})),
        ("list_links", list_links.invoke({"url": "https://www.gewerbe-nw.ch/"})),
        ("fetch_page", fetch_page.invoke({"url": "https://www.gewerbe-nw.ch/"})),
    ]:
        print(f"-- {name}: {len(out)} Zeichen\n{out[:500]}\n")
    print("blocked:", log.blocked)

    print("== Graph mit Stand-in-Modell ==")
    result = run_scan(cfg, llm=ScriptedModel(), on_event=print)
    print(result["markdown"])
    assert "Erfundene AG" not in result["markdown"].split("## Abdeckungsprotokoll")[0], "invented row passed the join with the findings"
    assert "## Handelsregister-Signale (SHAB)" in result["markdown"] and "Testlauf" in result["markdown"]
    assert result["treffer"] >= 1
    assert not (Path(os.environ["DATA_DIR"]) / "history.json").exists(), "a dry run must not write the history"
    print("OK")


if __name__ == "__main__":
    main()
