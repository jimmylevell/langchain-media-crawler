"""Offline tests of the rules in code: scoring, locality, register noise, the two lists, client match,
SSRF guard, state files and the pause of unproductive task types. No network, no model.

    uv run python -m tests.test_logic
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from typing import get_args

os.environ["DATA_DIR"] = tempfile.mkdtemp()  # keep the real history untouched

from scanner import store
from scanner.config import RunConfig
from scanner.netsafe import BlockedTarget, ensure_public
from scanner.report import bestand, kunden, listen, pruefen, zeilen_bauen, _norm
from scanner.schemas import EditorRow, Ereignistyp, Finding, Report
from scanner.scoring import GROESSE, QUELLE, TYP, bewerte, lokalitaet
from scanner.sources import ort_gewicht, quellentyp
from scanner.tools import RunLog, kapital, polite_get, shab_rauschen

CFG = RunConfig(laufdatum=date(2026, 10, 13))
MEDIUM = "https://www.nidwaldnerzeitung.ch/zentralschweiz/nidwalden/firma-x-ld.1"
SHAB = "https://www.shab.ch/#!/search/publications/detail/abc"


def finding(**kw) -> Finding:
    base = dict(
        organisation="Muster AG", ort="Stans", ereignistyp="nachfolge_uebernahme", groesse="mittel", ereignis="Die Muster AG in Stans wird verkauft.",
        ereignisdatum="", publikationsdatum="2026-10-12", quelle_name="Nidwaldner Zeitung", quelle_url=MEDIUM, personen=[],
        belegzitat="Muster AG Stans", )
    return Finding(**{**base, **kw})


def read_log(*urls: str) -> RunLog:
    log = RunLog()
    log.mark_read(*urls)
    return log


def test_tables() -> None:
    assert set(TYP) == set(get_args(Ereignistyp)), "every event type needs a weight"
    assert set(GROESSE) == {"gross", "mittel", "klein", "unbekannt"} and set(QUELLE) == {"medium", "behoerde", "shab"}
    print("Gewichte vollständig: OK")


def test_locality() -> None:
    assert ort_gewicht("Stans", ["NW", "OW"]) == (1.0, "Stans")
    assert ort_gewicht("Hergiswil (NW)", ["NW", "OW"]) == (1.0, "Hergiswil")
    assert ort_gewicht("Sarnen", ["NW", "OW"]) == (0.9, "Sarnen")
    assert ort_gewicht("Luzern", ["NW", "OW"]) == (0.6, "Luzern")
    assert ort_gewicht("Zürich", ["NW", "OW"]) is None
    assert ort_gewicht("Stanserhorn-Bahn", ["NW", "OW"]) is None, "whole words only"
    assert ort_gewicht("Zürich", ["UR"]) == (1.0, ""), "a canton without registry data cannot be checked"
    # the place must be in the finding's own text unless it comes from the register
    assert lokalitaet(finding(), CFG)[0] == 1.0
    assert lokalitaet(finding(ort="Sarnen", organisation="Muster AG", ereignis="Verkauf", belegzitat="Muster AG"), CFG)[0] is None
    assert lokalitaet(finding(ort="Sarnen", organisation="Muster AG", ereignis="Verkauf", belegzitat="x", quelle_url=SHAB), CFG)[0] == 0.9
    assert lokalitaet(finding(ort="Zürich"), CFG)[0] is None
    assert quellentyp(SHAB) == "shab" and quellentyp(MEDIUM) == "medium" and quellentyp("https://www.nw.ch/_rte/information/1") == "behoerde"
    assert quellentyp("https://www.simap.ch/project-detail/1") == "behoerde" and quellentyp("https://neuer-anzeiger.ch/a") == "medium"
    print("Ortsprüfung: OK")


def test_scoring() -> None:
    nachfolge_medium = bewerte(finding(), 1.0, date(2026, 10, 12), CFG)
    assert nachfolge_medium == (5.0, "HOCH"), nachfolge_medium
    sitz_shab = bewerte(finding(ereignistyp="gruendung_sitzverlegung", groesse="klein", quelle_url=SHAB), 1.0, date(2026, 10, 12), CFG)
    assert sitz_shab[1] == "NIEDRIG", sitz_shab
    revision_shab = bewerte(finding(ereignistyp="revisionsstelle", groesse="unbekannt", quelle_url=SHAB), 1.0, date(2026, 10, 12), CFG)
    assert revision_shab[1] == "MITTEL", revision_shab
    assert bewerte(finding(), 1.0, date(2026, 10, 12), CFG)[0] > bewerte(finding(), 1.0, date(2026, 10, 1), CFG)[0], "older is weaker"
    assert bewerte(finding(), 1.0, date(2026, 10, 12), CFG)[0] > bewerte(finding(), 0.6, date(2026, 10, 12), CFG)[0], "farther is weaker"
    print("Bewertung: OK")


def test_register_noise() -> None:
    assert shab_rauschen("Mutation Bepa AG in Liquidation, Stans", "Auflösung und Liquidation. Stammkapital CHF 20'000")
    assert not shab_rauschen("Mutation Gross AG in Liquidation, Stans", "Aktienkapital CHF 2'000'000.00. Liquidation beschlossen")
    assert shab_rauschen("Konkurs Muster GmbH", "Das Konkursverfahren wurde mangels Aktiven eingestellt")
    assert shab_rauschen("Löschung Muster AG, Hergiswil", "Die Gesellschaft ist erloschen")
    assert not shab_rauschen("Löschung Muster AG, Hergiswil", "Infolge Fusion: Aktiven und Passiven gingen an die Evoleen AG")
    assert not shab_rauschen("Mutation Convinia AG, Sarnen", "Verzicht auf eingeschränkte Revision (Opting-out)")
    assert kapital("Aktienkapital: CHF 100'000.00, Sitz Stans") == 100000 and kapital("kein Betrag") is None
    print("SHAB-Rauschen: OK")


def test_pipeline() -> None:
    """Checks, scoring, two lists, editor wording joined to the facts, top recommendation."""
    medium, shab_url = MEDIUM, SHAB
    findings = [
        finding(),
        finding(organisation="Fremd AG", ort="Zürich", quelle_url="https://www.nidwaldnerzeitung.ch/z/fremd"),  # outside the region
        finding(organisation="Ungelesen AG", quelle_url="https://www.nidwaldnerzeitung.ch/z/ungelesen"),  # never read
        finding(organisation="Alt AG", publikationsdatum="2026-08-01", quelle_url="https://www.nidwaldnerzeitung.ch/z/alt"),  # outside the window
        finding(organisation="Doppel AG", quelle_url="https://www.nidwaldnerzeitung.ch/z/doppel"),
        finding(organisation="Doppel AG", quelle_url="https://www.bote.ch/z/doppel", belegzitat="Doppel AG Stans"),
        finding(organisation="Klein GmbH", ereignistyp="gruendung_sitzverlegung", groesse="klein", ort="Emmetten", ereignis="Sitz nach Emmetten", belegzitat="Emmetten",
                quelle_name="SHAB", quelle_url=shab_url),  # register, too weak
        finding(organisation="Revisions AG", ereignistyp="revisionsstelle", groesse="mittel", ort="Sarnen", quelle_name="SHAB", quelle_url=SHAB + "2"),
    ]
    log = read_log(MEDIUM, "https://www.nidwaldnerzeitung.ch/z/fremd", "https://www.nidwaldnerzeitung.ch/z/alt", "https://www.nidwaldnerzeitung.ch/z/doppel",
                   "https://www.bote.ch/z/doppel", shab_url, SHAB + "2")
    candidates, dropped, cut = pruefen(findings, CFG, log, history=[])
    reasons = {d.was.split(" (")[0]: d.grund for d in dropped}
    assert reasons["Fremd AG"].startswith("Ort"), reasons
    assert reasons["Ungelesen AG"].startswith("Quelle im Lauf nicht"), reasons
    assert "Zeitfenster" in reasons["Alt AG"], reasons
    assert "Doublette" in reasons["Doppel AG"], reasons
    assert reasons["Klein GmbH"].startswith("Priorität NIEDRIG"), reasons
    assert [k.f.organisation for k in candidates] == ["Muster AG", "Doppel AG", "Revisions AG"], [k.f.organisation for k in candidates]
    assert [k.liste for k in candidates] == ["lokal", "lokal", "shab"] and [k.nr for k in candidates] == [1, 2, 3]
    # history: the same article is not reported twice
    again, dropped2, _ = pruefen(findings[:1], CFG, log, history=[{"url": MEDIUM}])
    assert not again and "8 Wochen" in dropped2[0].grund

    report = Report(
        zeilen=[
            EditorRow(nr=1, ereignis="Verkauf der Muster AG", relevanz="(Einschätzung) Nachfolge", aktion="Rufen Sie die Inhaberin an.", kontaktentwurf="Guten Tag"),
            EditorRow(nr=3, ereignis="Wechsel der Revisionsstelle", relevanz="(Einschätzung) Mandat", aktion="Laden Sie den VR-Präsidenten ein.", kontaktentwurf=""),
            EditorRow(nr=2, ereignis="Aktion fehlt", relevanz="x", aktion="  ", kontaktentwurf=""),
            EditorRow(nr=99, ereignis="erfunden", relevanz="x", aktion="Anrufen", kontaktentwurf=""),
        ],
        verworfen=[],
    )
    rows, ohne_aktion = zeilen_bauen(report, candidates, CFG)
    assert [r.organisation for r in rows] == ["Muster AG", "Revisions AG"], "invented number and empty action are dropped"
    assert ohne_aktion[0].grund == "keine ausführbare Aktion"
    assert rows[0].quelle_url == MEDIUM and rows[0].ort == "Stans" and rows[0].kontaktentwurf == "", "no draft unless requested"
    lokal, shab, weitere = listen(rows, CFG)
    assert [r.organisation for r in lokal] == ["Muster AG"] and [r.organisation for r in shab] == ["Revisions AG"] and not weitere
    many = [rows[1].model_copy(update={"organisation": f"R{i}", "score": 2.5 - i / 10}) for i in range(5)]
    assert len(listen(many, CFG)[1]) == CFG.max_shab and len(listen(many, CFG)[2]) == 2, "at most three register rows"
    print("Prüfung, Listen, Redaktion: OK")


def test_clients() -> None:
    assert bestand("Muster AG", []) == "ungeprüft"
    liste = [_norm(x) for x in ["Muster AG", "Titlis Bergbahnen AG", "AB"]]
    assert bestand("Muster", liste) == "Kunde" and bestand("Titlis Bergbahnen", liste) == "Kunde"
    assert bestand("Mustermann GmbH", liste) == "Kein Kunde", "no substring hits"
    assert bestand("Rabatt AG", liste) == "Kein Kunde", "a short client name must not match inside another word"
    assert bestand("Muster Bau AG", liste) == "Kunde"
    assert kunden() == []
    print("Kundenabgleich: OK")


def test_ssrf() -> None:
    for url in ("http://127.0.0.1/", "http://localhost/admin", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "http://[::1]/",
                "http://192.168.1.1:80/", "http://[::ffff:127.0.0.1]/", "ftp://example.com/", "https://8.8.8.8:8443/", "file:///etc/passwd"):
        try:
            ensure_public(url)
        except BlockedTarget:
            continue
        raise AssertionError(f"{url} must be blocked")
    ensure_public("https://8.8.8.8/")  # public address passes
    r, msg = polite_get(RunLog(), "http://169.254.169.254/latest/meta-data/")
    assert r is None and msg.startswith("GESPERRT (interne Adresse)"), msg
    print("SSRF-Schutz: OK")


def test_state() -> None:
    today = date(2026, 10, 13)
    store.save_history([{"gemeldet_am": "2026-10-01", "organisation": "A", "ereignis": "e", "url": "u"}], today)
    store.save_history([{"gemeldet_am": "2026-10-13", "organisation": "B", "ereignis": "e", "url": "v"}], today)
    assert [h["organisation"] for h in store.load_history(today)] == ["A", "B"]
    assert store.load_history(today + timedelta(weeks=9)) == [h for h in store.load_history(today) if h["gemeldet_am"] >= (today + timedelta(weeks=1)).isoformat()]

    # a source type is paused after three empty runs and tried again after four weeks
    for _ in range(3):
        store.record_yield({"Stelleninserate": 0, "Events": 2}, today)
    skipped = store.skipped_types(today + timedelta(days=1), never=set())
    assert list(skipped) == ["Stelleninserate"], skipped
    assert store.skipped_types(today + timedelta(days=1), never={"Stelleninserate"}) == {}
    assert store.skipped_types(today + timedelta(days=store.RETRY_AFTER_DAYS), never=set()) == {}, "retried after four weeks"
    store.record_yield({"Stelleninserate": 1}, today)
    assert store.skipped_types(today + timedelta(days=1), never=set()) == {}, "a find resets the count"

    # two writers at once must not lose entries
    import threading
    def writer(n: int) -> None:
        for i in range(10):
            store.save_history([{"gemeldet_am": "2026-10-13", "organisation": f"W{n}-{i}", "ereignis": "e", "url": f"{n}-{i}"}], today)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(store.load_history(today)) == 2 + 40, len(store.load_history(today))
    print("Zustand (Historie, Sperre, Pause): OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    test_tables()
    test_locality()
    test_scoring()
    test_register_noise()
    test_pipeline()
    test_clients()
    test_ssrf()
    test_state()
