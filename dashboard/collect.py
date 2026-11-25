#!/usr/bin/env python3
"""Collect marine/yachting headlines for a wall dashboard.

Reuses the same source list as Horizon (data/config.json) so the big screen and
the daily digest never drift apart. Deliberately dependency-free: standard
library only, so it runs on the system Python without uv.

Usage:
    python3 collect.py                       # uses ../data/config.json
    python3 collect.py -c ../data/config.json -o out/data.json --hours 24
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(value):
    """Strip HTML tags and collapse whitespace."""
    if not value:
        return ""
    text = TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def fetch(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if len(raw) > 8 * 1024 * 1024:
            raw = raw[: 8 * 1024 * 1024]
        return raw


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except Exception:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_feed(raw, fallback_name=""):
    """Parse RSS 2.0 or Atom into a list of raw item dicts."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    items = []

    # RSS 2.0 / RDF
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag != "item":
            continue
        title = clean_text(node.findtext("title"))
        link = clean_text(node.findtext("link"))
        date_text = node.findtext("pubDate") or node.findtext(DC_DATE) or ""
        desc = (
            node.findtext(CONTENT_NS)
            or node.findtext("description")
            or node.findtext("summary")
            or ""
        )
        publisher = ""
        source_node = node.find("source")
        if source_node is not None and source_node.text:
            publisher = clean_text(source_node.text)
        items.append(
            {
                "title": title,
                "url": link,
                "published": parse_date(date_text),
                "summary": clean_text(desc)[:400],
                "publisher": publisher,
            }
        )

    if items:
        return items

    # Atom
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag != "entry":
            continue
        title = clean_text(node.findtext(ATOM_NS + "title") or node.findtext("title"))
        link = ""
        for link_node in node.findall(ATOM_NS + "link") + node.findall("link"):
            if link_node.get("rel") in (None, "", "alternate"):
                link = link_node.get("href") or ""
                break
        date_text = (
            node.findtext(ATOM_NS + "published")
            or node.findtext(ATOM_NS + "updated")
            or node.findtext("published")
            or node.findtext("updated")
            or ""
        )
        desc = (
            node.findtext(ATOM_NS + "summary")
            or node.findtext(ATOM_NS + "content")
            or node.findtext("summary")
            or ""
        )
        items.append(
            {
                "title": title,
                "url": link,
                "published": parse_date(date_text),
                "summary": clean_text(desc)[:400],
                "publisher": "",
            }
        )
    return items


def google_news_url(config, hours):
    """Build the Google News RSS search URL, mirroring Horizon's own operator."""
    query = (config.get("query") or "").strip()
    if not query:
        return None
    if hours <= 100:
        operator = "when:{}h".format(hours)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        operator = "after:{}".format(since.strftime("%Y-%m-%d"))
    language = config.get("language") or "en"
    country = config.get("country") or "US"
    ceid = config.get("ceid") or "{}:{}".format(country, language)
    params = {
        "q": "{} {}".format(query, operator),
        "hl": language,
        "gl": country,
        "ceid": ceid,
    }
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def strip_publisher_suffix(title, publisher):
    """Google News titles read 'Headline - Publisher'; keep only the headline."""
    if not publisher:
        return title
    suffix = " - " + publisher
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


def collect(config, hours):
    sources = []
    items = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    # Some event pages carry future dates (conferences next year); they are not news.
    future_limit = now + timedelta(days=1)

    feed_specs = []
    for feed in (config.get("sources") or {}).get("rss") or []:
        if feed.get("enabled"):
            feed_specs.append(
                {
                    "name": feed.get("name") or feed.get("url", ""),
                    "url": feed.get("url", ""),
                    "category": feed.get("category") or "other",
                    "kind": "rss",
                }
            )

    google = (config.get("sources") or {}).get("google_news") or {}
    if google.get("enabled"):
        url = google_news_url(google, hours)
        if url:
            feed_specs.append(
                {
                    "name": "Google News / trade press",
                    "url": url,
                    "category": google.get("category") or "premium",
                    "kind": "google_news",
                }
            )

    for spec in feed_specs:
        started = time.time()
        health = {
            "name": spec["name"],
            "category": spec["category"],
            "count": 0,
            "ok": False,
            "error": "",
            "ms": 0,
        }
        try:
            raw = fetch(spec["url"])
            parsed = parse_feed(raw, spec["name"])
            kept = 0
            for entry in parsed:
                if not entry["title"] or not entry["url"]:
                    continue
                published = entry["published"]
                if published is None or published < cutoff or published > future_limit:
                    continue
                title = strip_publisher_suffix(entry["title"], entry["publisher"])
                items.append(
                    {
                        "title": title,
                        "url": entry["url"],
                        "source": entry["publisher"] or spec["name"],
                        "category": spec["category"],
                        "published": published.isoformat(),
                        "published_ts": published.timestamp(),
                        "summary": entry["summary"],
                    }
                )
                kept += 1
                if kept >= 40:
                    break
            health["count"] = kept
            health["ok"] = True
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the wall
            health["error"] = "{}: {}".format(type(exc).__name__, exc)[:160]
        health["ms"] = int((time.time() - started) * 1000)
        sources.append(health)

    # de-duplicate by URL then by normalized title
    seen_urls = set()
    seen_titles = set()
    unique = []
    for item in sorted(items, key=lambda i: i["published_ts"], reverse=True):
        url_key = re.sub(r"[?#].*$", "", item["url"]).rstrip("/").lower()
        title_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", item["title"].lower())[:70]
        if url_key in seen_urls or (title_key and title_key in seen_titles):
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        item["id"] = hashlib.sha1(url_key.encode("utf-8")).hexdigest()[:12]
        unique.append(item)

    return sources, unique


def latest_digest(summaries_dir, limit=14):
    """Pull the headline list out of the newest Horizon Markdown digest."""
    if not summaries_dir or not os.path.isdir(summaries_dir):
        return None
    candidates = sorted(
        (
            name
            for name in os.listdir(summaries_dir)
            if name.startswith("horizon-") and name.endswith(".md")
        ),
        reverse=True,
    )
    if not candidates:
        return None
    path = os.path.join(summaries_dir, candidates[0])
    titles = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("### "):
                    continue
                text = line[4:].strip()
                text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
                text = re.sub(r"\s*[\u2b50\ufe0f]+\s*\d+(?:\.\d+)?/10\s*$", "", text)
                if text:
                    titles.append(text.strip())
                if len(titles) >= limit:
                    break
    except OSError:
        return None
    return {"file": candidates[0], "date": candidates[0][8:18], "titles": titles}


def main(argv=None):
    # Windows consoles default to a legacy code page; keep CJK source names printable.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c", "--config", default=os.path.join(repo, "data", "config.json")
    )
    parser.add_argument("-o", "--output", default=os.path.join(here, "data.json"))
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument(
        "--summaries", default=os.path.join(repo, "data", "summaries")
    )
    args = parser.parse_args(argv)

    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)

    sources, items = collect(config, args.hours)
    payload = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "generated_ts": time.time(),
        "window_hours": args.hours,
        "stats": {
            "items": len(items),
            "sources_ok": sum(1 for s in sources if s["ok"]),
            "sources_total": len(sources),
        },
        "sources": sources,
        "digest": latest_digest(args.summaries),
        "items": items,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    tmp = args.output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, args.output)

    print(
        "{items} items from {ok}/{total} sources -> {out}".format(
            items=payload["stats"]["items"],
            ok=payload["stats"]["sources_ok"],
            total=payload["stats"]["sources_total"],
            out=args.output,
        )
    )
    for source in sources:
        status = "ok" if source["ok"] else "FAIL"
        print(
            "  [{status:4s}] {count:2d}  {ms:5d}ms  {name}{err}".format(
                status=status,
                count=source["count"],
                ms=source["ms"],
                name=source["name"],
                err="" if source["ok"] else "  <- " + source["error"],
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
