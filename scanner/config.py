"""Run variables (the {{VARIABLES}} of the prompt) and environment settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# On Azure App Service only /home survives restarts, so point DATA_DIR there.
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USER_AGENT = os.getenv("SCANNER_USER_AGENT", "AkquiseScanner/0.1 (regional media scan)")
SEARXNG_URL = os.getenv("SEARXNG_URL", "").rstrip("/")
MAX_PARALLEL_AGENTS = int(os.getenv("MAX_PARALLEL_AGENTS", "4"))


@dataclass
class RunConfig:
    firma: str = "Treuhand Firma"
    niederlassung: str = "Stans"
    region: str = "Nidwalden, Obwalden und angrenzende Gemeinden"
    kantone: list[str] = field(default_factory=lambda: ["NW", "OW"])  # for the SHAB query
    tage: int = 7
    empfaenger: str = ""
    min_jubilaeum: int = 25
    laufdatum: date = field(default_factory=date.today)
    kontaktentwurf: bool = False
    geplant: bool = True  # scheduled run: never ask back, note assumptions instead
    max_treffer: int = 10
    mit_niedrig: bool = False

    def __post_init__(self) -> None:
        if not self.empfaenger:
            self.empfaenger = f"Niederlassungsleitung {self.niederlassung}"

    @property
    def fenster_start(self) -> date:
        return self.laufdatum - timedelta(days=self.tage)

    @property
    def nachzuegler_start(self) -> date:
        return self.fenster_start - timedelta(days=14)

    @property
    def zeitraum(self) -> str:
        return f"{self.fenster_start:%d.%m.%Y}–{self.laufdatum:%d.%m.%Y}"
