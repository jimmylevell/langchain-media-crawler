"""Collector tests.

    uv run python -m tests.test_collect          # offline parser checks + live collection
    uv run python -m tests.test_collect offline  # parser and gatekeeping only, no network
"""
from __future__ import annotations

import html
import json
import sys
import time
from datetime import date

from scanner.collect import collect, parse_iweb, parse_rss, parse_sitemap
from scanner.config import RunConfig
from scanner.tools import RunLog, make_tools, norm_url


def page(rows: list[dict]) -> str:
    return f'<div data-entities="{html.escape(json.dumps({"data": rows}))}"></div>'


def test_parser() -> None:
    rows = [
        {"name": '<a href="/_rte/information/1">Gemeindeversammlung &amp; Budget</a>', "_datum": "2026-10-06 08:00:00"},
        {"title": '<a href="/_rte/geschaeft/2">Teilrevision Steuergesetz</a>', "_geschaeftsdatum": "29.09.2026"},
        {"name": '<a href="/_rte/sitzung/3">Landratssitzung</a>', "_datum": "25.11.2026, 8.30 Uhr 25.11.2026, 8.30 Uhr "},
        {"name": "ohne Link", "_datum": "2026-10-01"},
        {"name": '<a href="https://example.org/x">Extern</a>', "datum": ""},
    ]
    got = parse_iweb(page(rows), "https://www.example.ch")
    assert got == [
        (date(2026, 10, 6), "Gemeindeversammlung & Budget", "https://www.example.ch/_rte/information/1", ""),
        (date(2026, 9, 29), "Teilrevision Steuergesetz", "https://www.example.ch/_rte/geschaeft/2", ""),
        (date(2026, 11, 25), "Landratssitzung", "https://www.example.ch/_rte/sitzung/3", ""),
        (None, "Extern", "https://example.org/x", ""),
    ], got
    assert parse_iweb("<html>nichts</html>", "https://x.ch") == []
    assert parse_iweb('<div data-entities="kaputt"></div>', "https://x.ch") == []

    rss = """<rss><channel>
      <item><title><![CDATA[Neuer CFO bei der Muster AG]]></title><link>https://zeitung.example/a-ld.1</link>
        <pubDate>Tue, 06 Oct 2026 07:53:50 +0000</pubDate><description><![CDATA[<p>Die Muster AG in Stans &amp; Umgebung.</p>]]></description></item>
      <item><title>Ohne Datum</title><link>https://zeitung.example/b</link></item>
      <entry><title>Atom</title><link href="https://zeitung.example/c"/><updated>2026-10-07T15:32:41.234Z</updated><summary>kurz</summary></entry>
    </channel></rss>"""
    assert parse_rss(rss, "") == [
        (date(2026, 10, 6), "Neuer CFO bei der Muster AG", "https://zeitung.example/a-ld.1", "Die Muster AG in Stans & Umgebung."),
        (None, "Ohne Datum", "https://zeitung.example/b", ""),
        (date(2026, 10, 7), "Atom", "https://zeitung.example/c", "kurz"),
    ], parse_rss(rss, "")

    sitemap = """<urlset>
      <url><loc>https://bote.example/nachrichten/firma-zieht-nach-stans-art-1725900</loc><news:news>
        <news:publication_date>2026-10-08T10:31:04+02:00</news:publication_date><news:title><![CDATA[Firma zieht nach Stans]]></news:title></news:news></url>
      <url><loc>https://u24.example/news/gewerbeausstellung-sarnen-eroeffnet/</loc><lastmod>2026-10-03T09:59:40.000Z</lastmod></url>
    </urlset>"""
    assert parse_sitemap(sitemap, "") == [
        (date(2026, 10, 8), "Firma zieht nach Stans", "https://bote.example/nachrichten/firma-zieht-nach-stans-art-1725900", ""),
        (date(2026, 10, 3), "Gewerbeausstellung sarnen eroeffnet", "https://u24.example/news/gewerbeausstellung-sarnen-eroeffnet/", ""),
    ], parse_sitemap(sitemap, "")
    print("Parser (Gemeindeplattform, RSS, Sitemap): OK")


def test_gatekeeping() -> None:
    """On a collected host, agents may open collected entries only – no request leaves otherwise."""
    log = RunLog()
    log.collected_hosts.add("example.invalid")
    log.budget_hosts.add("example.invalid")
    log.collected_urls.add(norm_url("https://www.example.invalid/_rte/information/1"))
    _, fetch_page, list_links, _ = make_tools(RunConfig(), log)
    assert fetch_page.invoke({"url": "https://www.example.invalid/irgendwas"}).startswith("NICHT NÖTIG")
    assert list_links.invoke({"url": "https://www.example.invalid/aktuellesinformationen"}).startswith("NICHT NÖTIG")
    assert not log.blocked and not log.hosts, "gatekeeping must not touch the network"
    # the collected entry itself is let through (and fails here only because the host does not exist)
    assert "nicht erreichbar" in fetch_page.invoke({"url": "https://www.example.invalid/_rte/information/1"})
    print("Zugriffsregel: OK")


def test_live() -> None:
    cfg, log = RunConfig(), RunLog()
    start = time.time()
    s = collect(cfg, log, on_event=print)
    secs = time.time() - start
    print(f"\n{s.zusammenfassung()} – {secs:.0f} s")
    print("nicht erreichbar:", log.blocked or "nichts")
    by = {}
    for e in s.eintraege:
        by[(e.quellgattung, e.art)] = by.get((e.quellgattung, e.art), 0) + 1
    print("im Zeitfenster nach Art:", by)
    per_source = {}
    for e in s.eintraege:
        per_source[e.quelle] = per_source.get(e.quelle, 0) + 1
    print("im Zeitfenster nach Quelle:", per_source)
    for e in [e for e in s.eintraege if e.quellgattung in ("Lokale Medien", "Events")][:14]:
        print(e.zeile()[:230])
    assert s.websites >= 8 and len(s.eintraege) >= 10, "collector found too little"
    assert any(e.quellgattung == "Lokale Medien" for e in s.eintraege), "no media entries collected"
    assert all(cfg.nachzuegler_start <= e.datum for e in s.eintraege)
    assert all(norm_url(e.url) in log.collected_urls for e in s.eintraege)
    print("Live-Sammlung: OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    test_parser()
    test_gatekeeping()
    if sys.argv[1:] != ["offline"]:
        test_live()
