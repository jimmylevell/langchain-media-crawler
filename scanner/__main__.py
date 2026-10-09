"""CLI: uv run python -m scanner [--region ...] [--tage 7] [--kontaktentwurf]"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import DATA_DIR, RunConfig
from .graph import run_scan


def main() -> None:
    d = RunConfig()
    p = argparse.ArgumentParser(description="Medien-Scanner für Akquiseimpulse")
    p.add_argument("--firma", default=d.firma)
    p.add_argument("--niederlassung", default=d.niederlassung)
    p.add_argument("--region", default=d.region)
    p.add_argument("--kantone", default=",".join(d.kantone), help="Kantonskürzel für SHAB, z.B. NW,OW")
    p.add_argument("--tage", type=int, default=d.tage)
    p.add_argument("--laufdatum", type=date.fromisoformat, default=d.laufdatum)
    p.add_argument("--kontaktentwurf", action="store_true")
    p.add_argument("--mit-niedrig", action="store_true")
    a = p.parse_args()
    overridden = any(getattr(a, k) != getattr(d, k) for k in ("niederlassung", "region", "tage"))
    cfg = RunConfig(
        firma=a.firma, niederlassung=a.niederlassung, region=a.region, kantone=a.kantone.upper().split(","),
        tage=a.tage, laufdatum=a.laufdatum, kontaktentwurf=a.kontaktentwurf, mit_niedrig=a.mit_niedrig,
        geplant=not overridden,
    )
    sys.stdout.reconfigure(encoding="utf-8")
    result = run_scan(cfg, on_event=lambda m: print(m, file=sys.stderr, flush=True))
    out = DATA_DIR / "reports" / f"scan-{cfg.laufdatum.isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result["markdown"], "utf-8")
    print(result["markdown"])
    u = result["usage"]
    print(
        f"[{result['searches']} Suchen, {result['pages_read']} Meldungen/Seiten gelesen, "
        f"{u['input']:,} Input- / {u['output']:,} Output-Tokens] → {out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
