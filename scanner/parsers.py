"""Parsers for the list formats the collector reads. Each returns (date | None, title, absolute url, teaser)."""
from __future__ import annotations

import html as htmllib
import json
import re
from datetime import date
from email.utils import parsedate_to_datetime

import lxml.html
from lxml import etree

Zeile = tuple[date | None, str, str, str]

ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
CH = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
DATE_FIELDS = ("_datum", "datum-sort", "_geschaeftsdatum", "datumVon-sort", "datumVon", "datum")
MONATE = ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "dezember"]
# no entity expansion, no network: feeds come from third parties
XML = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, encoding="utf-8")


def parse_date(value: str) -> date | None:
    if m := ISO.search(value):
        y, mo, d = map(int, m.groups())
    elif m := CH.search(value):
        d, mo, y = map(int, m.groups())
    else:
        try:  # RSS: "Tue, 08 Sep 2026 07:53:50 +0000"
            return parsedate_to_datetime(value.strip()).date()
        except (TypeError, ValueError):
            return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def clean(fragment: str) -> str:
    """Markup and entities out, whitespace collapsed."""
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", htmllib.unescape(fragment or ""))).split())


def xml_root(text: str):
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        return etree.fromstring(body.encode("utf-8"), XML)
    except etree.XMLSyntaxError:  # empty or not XML at all
        return None


def local_name(el) -> str:
    """Tag name without namespace; also tolerates undeclared prefixes ("news:title") in sloppy feeds."""
    return el.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1] if isinstance(el.tag, str) else ""


def child_text(item, *names: str) -> str:
    """Text of the first descendant with one of these local names (namespaces ignored); names are tried in order."""
    for name in names:
        for el in item.iter():
            if el is not item and local_name(el) == name and (el.text or "").strip():
                return el.text.strip()
    return ""


def _link(item) -> str:
    if text := child_text(item, "link"):
        return text
    for el in item.iter():  # Atom: <link href="..."/>
        if local_name(el) == "link" and el.get("href") and el.get("rel", "alternate") == "alternate":
            return el.get("href")
    return ""


def parse_iweb(page_html: str, base: str) -> list[Zeile]:
    """The list the municipal web platform embeds in the page as JSON."""
    m = re.search(r'data-entities="([^"]+)"', page_html)
    if not m:
        return []
    try:
        rows = json.loads(htmllib.unescape(m.group(1))).get("data", [])
    except (ValueError, AttributeError):
        return []
    out: list[Zeile] = []
    for row in rows:
        cell = str(row.get("name") or row.get("title") or "")
        link = re.search(r'href="([^"]+)"', cell)
        titel = clean(cell)
        if link and titel:
            href = link.group(1)
            d = next((x for x in (parse_date(str(row.get(k) or "")) for k in DATE_FIELDS) if x), None)
            out.append((d, titel, href if href.startswith("http") else base + href, ""))
    return out


def parse_rss(xml: str, base: str = "") -> list[Zeile]:
    """RSS 2.0 and Atom."""
    root = xml_root(xml)
    if root is None:
        return []
    out: list[Zeile] = []
    for item in root.iter():
        if local_name(item) not in ("item", "entry"):
            continue
        link, titel = _link(item), clean(child_text(item, "title"))
        if link and titel:
            d = parse_date(child_text(item, "pubDate", "published", "updated", "date"))
            out.append((d, titel, link, clean(child_text(item, "description", "summary"))[:400]))
    return out


def parse_sitemap(xml: str, base: str = "") -> list[Zeile]:
    """XML sitemaps that carry a date per URL. The title comes from the news or video
    extension if present, else from the URL."""
    root = xml_root(xml)
    if root is None:
        return []
    out: list[Zeile] = []
    for block in root.iter():
        if local_name(block) != "url":
            continue
        loc = child_text(block, "loc")
        if not loc:
            continue
        titel = clean(child_text(block, "title"))
        if not titel:
            slug = re.sub(r"-(?:art|ld)?[-.]?\d+$", "", loc.rstrip("/").rsplit("/", 1)[-1])
            titel = slug.replace("-", " ").strip().capitalize()
        out.append((parse_date(child_text(block, "publication_date", "lastmod")), titel, loc, ""))
    return out


def parse_ihz(page_html: str, base: str) -> list[Zeile]:
    """Event list of the Industrie- und Handelskammer Zentralschweiz: month groups ("Oktober 2026")
    holding events that carry only the day of the month."""
    try:
        tree = lxml.html.fromstring(page_html)
    except (ValueError, etree.ParserError):
        return []
    out: list[Zeile] = []
    for group in tree.xpath('//li[contains(@class, "c-event__group")]'):
        head = re.match(r"\s*(\w+)\s+(\d{4})", group.text_content())
        month = MONATE.index(head.group(1).lower()) + 1 if head and head.group(1).lower() in MONATE else 0
        for item in group.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " c-event-item ")]'):
            parts = [" ".join(p.split()) for p in item.itertext() if p.strip() and p.strip() != "Link zum Event"]
            href = item.xpath('.//a[contains(@href, "/detail/")]/@href')
            day = re.match(r"(\d{1,2})\.?$", parts[0]) if parts else None
            rest = parts[2:]  # after day and weekday: category, more categories (", Netzwerk"), title, subtitle, time, place
            i = 1
            while i < len(rest) and rest[i].startswith(","):
                i += 1
            if not (month and day and href and i < len(rest)):
                continue
            try:
                d = date(int(head.group(2)), month, int(day.group(1)))
            except ValueError:
                continue
            out.append((d, rest[i], href[0] if href[0].startswith("http") else base + href[0], " | ".join([rest[0], *rest[i + 1:]])[:300]))
    return out


PARSER = {"iweb": parse_iweb, "rss": parse_rss, "sitemap": parse_sitemap, "ihz": parse_ihz}
