"""Structured logging (one JSON object per line on stderr) and optional tracing to Application Insights.

Traces: LangChain/LangGraph runs (agents, model calls, tool calls) go through the Azure tracer of
langchain-azure-ai; the scan itself gets one span with the run totals. Message content is not
recorded unless AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=true.
In a Foundry hosted agent the host already configures the exporter, so only the tracer is added."""
from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger("scanner")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def event(name: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, name, extra={"fields": fields})


def setup(hosted: bool = False) -> None:
    """Call once at start. Safe to call again."""
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING") or os.getenv("APPLICATION_INSIGHTS_CONNECTION_STRING")
    if not connection_string:
        return
    try:
        from langchain_azure_ai.callbacks.tracers import enable_auto_tracing

        enable_auto_tracing(connection_string=connection_string, auto_configure_azure_monitor=not hosted)
        event("tracing_enabled", hosted=hosted)
    except Exception as e:  # tracing must never stop a scan
        event("tracing_unavailable", logging.WARNING, error=f"{type(e).__name__}: {e}")


@contextmanager
def span(name: str, **attributes):
    """A span around a unit of work; yields a function to add attributes later. No-op without OpenTelemetry."""
    try:
        from opentelemetry import trace
    except ImportError:
        yield lambda **_: None
        return
    with trace.get_tracer("scanner").start_as_current_span(name, attributes=attributes) as s:
        yield lambda **attrs: [s.set_attribute(k, v) for k, v in attrs.items()]
