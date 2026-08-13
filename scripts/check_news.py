#!/usr/bin/env python3
"""
Daily news check for the European Tyre Distribution Tracker.

Runs two independent checks that share one dedup set (a URL seen for either
list won't be re-flagged for the other):

1. Distribution entities (data/entities.json) -- wholesalers, buying groups,
   franchise networks -- written to data/latest_run.json, data/history.json,
   data/archive.json.
2. Competitor manufacturers (data/competitors.json) -- Michelin, Continental,
   Bridgestone, the major Chinese/Korean/Japanese/Indian makers, etc. --
   written to data/competitor_latest_run.json, data/competitor_history.json,
   data/competitor_archive.json.

Also emails a combined daily digest via Resend's HTTP API if RESEND_API_KEY
and ALERT_EMAIL_TO are set.

Run with: python scripts/check_news.py
"""
import json
import os
import random
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ENTITIES_PATH = DATA_DIR / "entities.json"
COMPETITORS_PATH = DATA_DIR / "competitors.json"
SEEN_PATH = DATA_DIR / "seen_links.json"
LATEST_RUN_PATH = DATA_DIR / "latest_run.json"
HISTORY_PATH = DATA_DIR / "history.json"
ARCHIVE_PATH = DATA_DIR / "archive.json"
COMP_LATEST_RUN_PATH = DATA_DIR / "competitor_latest_run.json"
COMP_HISTORY_PATH = DATA_DIR / "competitor_history.json"
COMP_ARCHIVE_PATH = DATA_DIR / "competitor_archive.json"
COMP_EARNINGS_PATH = DATA_DIR / "competitor_earnings.json"

MAX_ITEMS_PER_ENTITY = 8
MAX_SEEN_LINKS = 12000
MAX_HISTORY_RUNS = 60
MAX_ARCHIVE_ITEMS = 5000
CONCURRENCY = 4  # lowered from 10 -- a burst of 10 concurrent requests to news.google.com
                 # from a shared GitHub Actions IP looks like a bot flood and was very
                 # likely contributing to the 503s seen in practice
REQUEST_TIMEOUT = 15
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
# A real browser UA, not a self-declared crawler string. The previous UA
# ("...TyreDistributionTracker/1.0; +daily-check") used the same format real
# bots use to identify themselves -- that almost certainly made Google's rate
# limiting more aggressive against these requests, not less.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def news_feed_url(search_query: str) -> str:
    q = urllib.parse.quote(search_query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_entity_news(entity: dict) -> list[dict]:
    """Fetch and return up to MAX_ITEMS_PER_ENTITY items for one entity.
    Returns [] on any failure -- a single entity's feed failing should never
    take down the whole run. Retries with backoff on transient errors (503
    in particular has been observed in practice, likely rate-limiting from
    Google against the shared IP ranges GitHub Actions runners use)."""
    url = news_feed_url(entity["search_query"])
    last_exc = None
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            # small random delay before retrying, and before the very first
            # request too on later attempts -- spreads requests out over time
            # rather than firing them all in a tight burst
            time.sleep((2 ** attempt) + random.uniform(0, 1))
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            if resp.status_code in RETRY_STATUS_CODES:
                last_exc = requests.HTTPError(f"{resp.status_code} Server Error (attempt {attempt + 1})")
                continue
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            items = []
            for entry in feed.entries[:MAX_ITEMS_PER_ENTITY]:
                items.append({
                    "title": getattr(entry, "title", "(untitled)"),
                    "link": getattr(entry, "link", ""),
                    "pubDate": getattr(entry, "published", ""),
                })
            return items
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            last_exc = exc
            continue
    print(f"  [warn] fetch failed for {entity['name']!r} after {MAX_RETRIES} attempts: {last_exc}",
          file=sys.stderr)
    return []


def build_email_html(dist_items: dict, comp_items: dict, run_at: str,
                      dist_checked: int, comp_checked: int) -> str:
    def section(title, items_by_entity):
        if not items_by_entity:
            return f'<h2 style="color:#16233A;">{title}</h2><p>No new mentions found today.</p>'
        blocks = []
        for entity_name, items in items_by_entity.items():
            links = "".join(
                f'<li style="margin-bottom:4px;">'
                f'<a href="{item["link"]}" style="color:#2F6F62;">{_escape(item["title"])}</a>'
                f'<br><span style="color:#5B6478;font-size:12px;">{_escape(item.get("pubDate",""))}</span></li>'
                for item in items
            )
            blocks.append(
                f'<h3 style="margin:18px 0 6px;font-size:15px;color:#16233A;">{_escape(entity_name)}</h3>'
                f'<ul style="margin:0 0 4px;padding-left:18px;">{links}</ul>'
            )
        return f'<h2 style="color:#16233A;">{title}</h2>' + "".join(blocks)

    dist_new = sum(len(v) for v in dist_items.values())
    comp_new = sum(len(v) for v in comp_items.values())

    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;">
  <h1 style="color:#16233A;font-size:18px;">Tyre distribution — daily digest</h1>
  <p style="color:#5B6478;font-size:13px;">Check completed {_escape(run_at)}. Distribution: {len(dist_items)} of {dist_checked} entities had new mentions ({dist_new} article{'s' if dist_new != 1 else ''}). Competitors: {len(comp_items)} of {comp_checked} makers had new mentions ({comp_new} article{'s' if comp_new != 1 else ''}).</p>
  {section("Distribution entities", dist_items)}
  {section("Competitor manufacturers", comp_items)}
  <p style="color:#9AA2B1;font-size:11px;margin-top:24px;">Automated daily check — European Tyre Distribution Tracker.</p>
</div>"""


def _escape(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def send_digest_email(html: str, total_new: int) -> None:
    resend_key = os.environ.get("RESEND_API_KEY")
    to_email = os.environ.get("ALERT_EMAIL_TO")
    from_email = os.environ.get("ALERT_EMAIL_FROM", "Tyre Tracker <onboarding@resend.dev>")

    if not resend_key or not to_email:
        print("Email not configured (RESEND_API_KEY / ALERT_EMAIL_TO missing) — skipping email step.")
        return

    subject = f"Tyre tracker — {total_new} new mention{'s' if total_new != 1 else ''}" \
        if total_new else "Tyre tracker — daily check-in, nothing new today"

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={"from": from_email, "to": [to_email], "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code >= 300:
            print(f"  [warn] Resend API returned {resp.status_code}: {resp.text}", file=sys.stderr)
        else:
            print(f"Digest email sent to {to_email}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] email send failed: {exc}", file=sys.stderr)


def run_check(items: list[dict], seen: dict) -> dict:
    """Fetch news for one list of items (entities or competitors), skipping
    anything already in `seen`. Mutates `seen` in place. Returns
    {name: [fresh_item, ...]} for whatever's genuinely new."""
    new_items_by_name: dict[str, list[dict]] = {}
    if not items:
        return new_items_by_name

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        future_to_item = {pool.submit(fetch_entity_news, it): it for it in items}
        for future in as_completed(future_to_item):
            src = future_to_item[future]
            fetched = future.result()
            fresh = [it for it in fetched if it["link"] and it["link"] not in seen]
            if fresh:
                for it in fresh:
                    seen[it["link"]] = True
                extra = {}
                if "countries" in src:
                    extra["country"] = src["countries"][0]
                    extra["cluster"] = src.get("cluster", "")
                elif "category" in src:
                    extra["category"] = src["category"]
                new_items_by_name[src["name"]] = [{**it, **extra} for it in fresh]
    return new_items_by_name


def update_archive(archive_path: Path, new_items_by_name: dict, run_at: str,
                    extra_key: str) -> list[dict]:
    archive = load_json(archive_path, [])
    for name, items in new_items_by_name.items():
        for it in items:
            record = {
                "entity": name, "title": it["title"], "link": it["link"],
                "pubDate": it.get("pubDate", ""), "foundAt": run_at,
            }
            record[extra_key[0]] = it.get(extra_key[0], "")
            if len(extra_key) > 1:
                record[extra_key[1]] = it.get(extra_key[1], "")
            archive.append(record)
    if len(archive) > MAX_ARCHIVE_ITEMS:
        archive = archive[-MAX_ARCHIVE_ITEMS:]
    save_json(archive_path, archive)
    return archive


def update_history(history_path: Path, total_new: int, entities_with_news: int, run_at: str) -> None:
    history = load_json(history_path, [])
    history.insert(0, {"runAt": run_at, "totalNew": total_new, "entitiesWithNews": entities_with_news})
    save_json(history_path, history[:MAX_HISTORY_RUNS])


def _parse_rfc822(pub_date: str):
    """Returns a timezone-aware datetime, or None if unparseable/missing."""
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return None


def fetch_latest_earnings_mention(competitor: dict) -> tuple[dict | None, str]:
    """Layered search, official source preferred:
      1. If we know the competitor's own IR domain, search restricted to that
         domain (site:domain.com) -- this surfaces their own press releases /
         results pages rather than third-party coverage.
      2. Otherwise (or if layer 1 finds nothing dated), fall back to a plain
         earnings-news search -- deliberately simple (no OR, one phrase),
         matching the exact pattern already proven to work for the general
         entity search, rather than the compound-OR query used previously.
    Returns (best_item_or_None, which_layer_succeeded) -- the layer label is
    for logging only, so a real run's output tells us which path each result
    came from instead of us having to guess."""
    domain = competitor.get("irDomain")

    def _search(query: str) -> dict | None:
        items = fetch_entity_news({"name": competitor["name"], "search_query": query})
        dated = [(dt, it) for it in items if (dt := _parse_rfc822(it.get("pubDate")))]
        if not dated:
            return None
        dated.sort(key=lambda pair: pair[0], reverse=True)
        return dated[0][1]

    if domain:
        result = _search(f'site:{domain} earnings OR results')
        if result:
            return result, "official-site"

    result = _search(f'"{competitor["name"]}" earnings')
    if result:
        return result, "general-news"

    return None, "none"


def update_earnings_mentions(competitors: list[dict]) -> dict:
    """For each competitor, checks whether a newer earnings-related article
    has surfaced than whatever we currently have on file, and only overwrites
    if so -- so this is a monotonically-freshening 'latest known' cache, not
    an accumulating log (unlike the archive)."""
    existing = load_json(COMP_EARNINGS_PATH, {})
    layer_counts = {"official-site": 0, "general-news": 0, "none": 0}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        future_to_name = {
            pool.submit(fetch_latest_earnings_mention, c): c["name"] for c in competitors
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                mention, layer = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] earnings search failed for {name!r}: {exc}", file=sys.stderr)
                continue
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
            if not mention:
                continue
            new_dt = _parse_rfc822(mention.get("pubDate"))
            if new_dt is None:
                continue
            current = existing.get(name)
            current_dt = _parse_rfc822(current.get("pubDate")) if current else None
            if current_dt is None or new_dt > current_dt:
                existing[name] = {
                    "title": mention["title"],
                    "link": mention["link"],
                    "pubDate": mention["pubDate"],
                    "source": layer,
                    "checkedAt": datetime.now(timezone.utc).isoformat(),
                }

    print(f"  Earnings search breakdown: {layer_counts['official-site']} via official site, "
          f"{layer_counts['general-news']} via general news, {layer_counts['none']} found nothing.")

    save_json(COMP_EARNINGS_PATH, existing)
    return existing

    save_json(COMP_EARNINGS_PATH, existing)
    return existing


def main() -> None:
    entities = load_json(ENTITIES_PATH, [])
    competitors = load_json(COMPETITORS_PATH, [])
    if not entities and not competitors:
        print("No entities or competitors found — aborting.", file=sys.stderr)
        sys.exit(1)

    seen = load_json(SEEN_PATH, {})
    run_at = datetime.now(timezone.utc).isoformat()

    dist_new_items = run_check(entities, seen)
    comp_new_items = run_check(competitors, seen)

    dist_total_new = sum(len(v) for v in dist_new_items.values())
    comp_total_new = sum(len(v) for v in comp_new_items.values())

    # Cap the shared seen-links set so it doesn't grow forever.
    if len(seen) > MAX_SEEN_LINKS:
        seen = dict(list(seen.items())[-MAX_SEEN_LINKS:])
    save_json(SEEN_PATH, seen)

    # --- distribution outputs ---
    save_json(LATEST_RUN_PATH, {
        "runAt": run_at, "totalNew": dist_total_new,
        "entitiesChecked": len(entities), "entitiesWithNews": len(dist_new_items),
        "newItemsByEntity": dist_new_items,
    })
    update_history(HISTORY_PATH, dist_total_new, len(dist_new_items), run_at)
    dist_archive = update_archive(ARCHIVE_PATH, dist_new_items, run_at, ("country", "cluster"))

    # --- competitor outputs ---
    save_json(COMP_LATEST_RUN_PATH, {
        "runAt": run_at, "totalNew": comp_total_new,
        "entitiesChecked": len(competitors), "entitiesWithNews": len(comp_new_items),
        "newItemsByEntity": comp_new_items,
    })
    update_history(COMP_HISTORY_PATH, comp_total_new, len(comp_new_items), run_at)
    comp_archive = update_archive(COMP_ARCHIVE_PATH, comp_new_items, run_at, ("category",))
    comp_earnings = update_earnings_mentions(competitors)

    print(f"Distribution: {dist_total_new} new across {len(dist_new_items)} entities "
          f"({len(entities)} checked). Archive: {len(dist_archive)}.")
    print(f"Competitors: {comp_total_new} new across {len(comp_new_items)} makers "
          f"({len(competitors)} checked). Archive: {len(comp_archive)}.")
    print(f"Earnings mentions on file: {len(comp_earnings)} of {len(competitors)} competitors.")

    html = build_email_html(dist_new_items, comp_new_items, run_at, len(entities), len(competitors))
    send_digest_email(html, dist_total_new + comp_total_new)


if __name__ == "__main__":
    main()
