#!/usr/bin/env python3
"""Daily Instagram follower snapshot.

For every Active row in the Notion "IG Accounts" database this script:
  1. looks the account up through Instagram Business Discovery,
  2. logs today's numbers in the Notion "IG Snapshots" database (one row per account per day),
  3. updates the account row (Followers, 7-Day Change, 30-Day Change, ...),
  4. tracks every recent post's views, likes and comments (Notion "All Posts" + data/media.json),
  5. writes docs/data.json, which the widgets read (incl. every post from the last 30 days),
  6. on the first days of each month, writes a Monthly Report page per client in Notion.

Uses only the Python standard library. Configuration comes from environment variables
(see .github/workflows/daily.yml).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

GRAPH_VERSION = "v26.0"
NOTION_VERSION = "2022-06-28"
TZ = ZoneInfo(os.environ.get("TRACKER_TZ", "America/Indiana/Indianapolis"))

IG_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_USER_ID = os.environ["IG_USER_ID"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
ACCOUNTS_DB = os.environ["NOTION_ACCOUNTS_DB"]
SNAPSHOTS_DB = os.environ["NOTION_SNAPSHOTS_DB"]
REELS_DB = os.environ.get("NOTION_REELS_DB", "")
REPORTS_DB = os.environ.get("NOTION_REPORTS_DB", "")
# "2026-11" builds (or rebuilds) that month's report on this run. A month that isn't over yet
# becomes a Preview; a finished month becomes Final.
REPORT_MONTH = os.environ.get("REPORT_MONTH", "").strip()
WIDGET_BASE = "https://ajimothymp4.github.io/follower-tracker/"

ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "data" / "history.json"
WIDGET_FILE = ROOT / "docs" / "data.json"
KEEP_DAYS = 800          # history kept in the repo
WIDGET_HISTORY_DAYS = 90  # history sent to the widget
MEDIA_FILE = ROOT / "data" / "media.json"
MEDIA_LIMIT = 60          # recent posts pulled per account each run
REEL_TRACK_DAYS = 45      # reels younger than this are updated in Notion daily
REEL_KEEP_DAYS = 400      # reel history kept in the repo
WIDGET_REELS = 12         # reels sent to the widget per account
ENGAGEMENT_POSTS = 12     # posts used for the engagement rate
CONTENT_DAYS = 30         # posts sent to the widget's content view
REPORTS_FILE = ROOT / "data" / "reports.json"
REPORT_DAYS = 5           # the previous month's Final report is built on the first runs of the month


# ---------------------------------------------------------------- HTTP helpers

def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        # Never echo the URL: Graph API URLs carry the access token.
        detail = err.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)
            if isinstance(parsed.get("error"), dict):
                detail = parsed["error"].get("message", detail)
            else:
                detail = parsed.get("message", detail)
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"HTTP {err.code}: {str(detail)[:300]}") from None


def notion(method, path, body=None):
    """Notion call with a gentle pace (API limit ~3/sec) and retries on rate limits."""
    for attempt in range(5):
        time.sleep(0.35)
        try:
            return http(method, f"https://api.notion.com/v1/{path}", {
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": NOTION_VERSION,
            }, body)
        except RuntimeError as err:
            retryable = str(err).startswith(("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"))
            if not retryable or attempt == 4:
                raise
            time.sleep(2 ** attempt * 2)


def plain(prop):
    if not prop:
        return ""
    parts = prop.get("rich_text") or prop.get("title") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def rich(value):
    return {"rich_text": [{"text": {"content": str(value)[:1900]}}]} if value else {"rich_text": []}


def clean_handle(raw):
    raw = (raw or "").strip()
    match = re.search(r"instagram\.com/([A-Za-z0-9._]+)", raw)
    if match:
        raw = match.group(1)
    return raw.lstrip("@").strip().strip("/").lower()


# ---------------------------------------------------------------- Notion

def active_accounts():
    rows, cursor = [], None
    while True:
        body = {"filter": {"property": "Active", "checkbox": {"equals": True}}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion("POST", f"databases/{ACCOUNTS_DB}/query", body)
        rows += res["results"]
        if not res.get("has_more"):
            return rows
        cursor = res["next_cursor"]


def upsert_snapshot(account_page_id, handle, day, followers, posts, change):
    title = f"{handle} · {day}"
    props = {
        "Entry": {"title": [{"text": {"content": title}}]},
        "Account": {"relation": [{"id": account_page_id}]},
        "Date": {"date": {"start": day}},
        "Followers": {"number": followers},
        "Posts": {"number": posts},
        "Change": {"number": change},
    }
    found = notion("POST", f"databases/{SNAPSHOTS_DB}/query", {
        "filter": {"property": "Entry", "title": {"equals": title}}, "page_size": 1,
    })["results"]
    if found:
        notion("PATCH", f"pages/{found[0]['id']}", {"properties": props})
    else:
        notion("POST", "pages", {"parent": {"database_id": SNAPSHOTS_DB}, "properties": props})


def set_status(page_id, message):
    notion("PATCH", f"pages/{page_id}", {"properties": {"Status": rich(message)}})


# ---------------------------------------------------------------- Instagram

MEDIA_FIELDS = ("id,caption,media_type,media_product_type,timestamp,permalink,"
                "like_count,comments_count,view_count,thumbnail_url,media_url")


def lookup(handle):
    """Profile numbers plus recent posts. Falls back to profile only if the posts part fails."""
    base = "username,name,followers_count,media_count,profile_picture_url"
    for fields in (f"{base},media.limit({MEDIA_LIMIT}){{{MEDIA_FIELDS}}}", base):
        url = (f"https://graph.facebook.com/{GRAPH_VERSION}/{IG_USER_ID}?"
               + urllib.parse.urlencode({"fields": f"business_discovery.username({handle}){{{fields}}}",
                                         "access_token": IG_TOKEN}))
        try:
            return http("GET", url)["business_discovery"]
        except RuntimeError:
            if fields == base:
                raise
    return None


# ---------------------------------------------------------------- history math

def change_since(series, today, days):
    """Followers gained since the latest snapshot at least `days` days ago."""
    cutoff = (today - timedelta(days=days)).isoformat()
    older = [d for d in series if d <= cutoff]
    if not older:
        return None
    return series[today.isoformat()]["followers"] - series[max(older)]["followers"]


def previous_change(series, today):
    earlier = [d for d in series if d < today.isoformat()]
    if not earlier:
        return None
    return series[today.isoformat()]["followers"] - series[max(earlier)]["followers"]


# ---------------------------------------------------------------- reels

def parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


def is_reel(m):
    return m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO"


def engagement_rate(media, followers):
    """Typical (median) likes + comments per post, divided by followers, over recent posts.
    Median, so one viral reel doesn't inflate the number."""
    if not followers:
        return None
    rates = sorted(((m.get("like_count") or 0) + (m.get("comments_count") or 0)) / followers
                   for m in media[:ENGAGEMENT_POSTS] if m.get("like_count") is not None)
    if not rates:
        return None
    mid = len(rates) // 2
    rate = rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2
    return round(rate, 4)


# (hours after posting, extra hours allowed) — the overnight runs land once a day, so a
# checkpoint takes the first reading inside that window; outside it, the value stays blank.
CHECKPOINTS = {"v24": (24, 48), "v7": (24 * 7, 48), "v30": (24 * 30, 72)}


def checkpoint(entry, key):
    hours, slack = CHECKPOINTS[key]
    posted = parse_ts(entry["timestamp"])
    for snap in sorted(entry["series"].values(), key=lambda x: x["at"]):
        age = (datetime.fromisoformat(snap["at"]) - posted).total_seconds() / 3600
        if hours <= age <= hours + slack:
            return snap.get("v")
    return None


def reel_title(entry, kind="Reel"):
    first = (entry.get("caption") or "").strip().split("\n")[0].strip()
    when = parse_ts(entry["timestamp"]).astimezone(TZ).strftime("%b %-d")
    return (first[:90] + ("…" if len(first) > 90 else "")) if first else f"{kind} · {when}"


def post_type(m):
    if m.get("media_product_type") == "REELS":
        return "reel"
    return {"CAROUSEL_ALBUM": "carousel", "IMAGE": "photo", "VIDEO": "video"}.get(m.get("media_type"), "post")


def content_items(media, now):
    """Every post (reels, photos, carousels) from the last CONTENT_DAYS days, for view=content."""
    since = now - timedelta(days=CONTENT_DAYS)
    out = []
    for m in media:
        if "timestamp" not in m or parse_ts(m["timestamp"]) < since:
            continue
        kind = post_type(m)
        thumb = m.get("thumbnail_url") or ("" if m.get("media_type") == "VIDEO" else m.get("media_url")) or ""
        out.append({
            "id": m["id"], "posted": m["timestamp"], "link": m.get("permalink", ""), "thumb": thumb,
            "type": kind, "title": reel_title(m, kind.capitalize()),
            "views": m.get("view_count"), "likes": m.get("like_count"), "comments": m.get("comments_count"),
        })
    return out


def update_reels(store, handle, account_page_id, media, now):
    """Record today's numbers for every recent post and keep the Notion "All Posts" database current.
    Posts younger than REEL_TRACK_DAYS are updated daily; older posts in the latest pull are added once."""
    day = now.date().isoformat()
    reels = store.setdefault(handle, {})
    for m in media:
        if "timestamp" not in m:
            continue
        e = reels.setdefault(m["id"], {"series": {}})
        e.update({
            "timestamp": m["timestamp"],
            "type": post_type(m),
            "permalink": m.get("permalink", ""),
            "caption": (m.get("caption") or "")[:500],
            "thumb": m.get("thumbnail_url") or ("" if m.get("media_type") == "VIDEO" else m.get("media_url")) or "",
        })
        e["series"][day] = {"v": m.get("view_count"), "l": m.get("like_count"),
                            "c": m.get("comments_count"), "at": now.isoformat(timespec="minutes")}

    if not REELS_DB:
        return
    track_from = now - timedelta(days=REEL_TRACK_DAYS)
    for media_id, e in reels.items():
        if day not in e["series"]:
            continue  # not in today's pull
        if parse_ts(e["timestamp"]) < track_from and e.get("notion"):
            continue  # older posts are added once, then left alone
        snap = e["series"][day]
        kind = e.get("type", "reel")
        values = {"type": kind, "views": snap["v"], "likes": snap["l"], "comments": snap["c"],
                  "v24": checkpoint(e, "v24"), "v7": checkpoint(e, "v7"), "v30": checkpoint(e, "v30")}
        if e.get("pushed") == values and e.get("notion"):
            continue  # nothing changed since the last update
        props = {
            "Post": {"title": [{"text": {"content": reel_title(e, kind.capitalize())}}]},
            "Type": {"select": {"name": kind.capitalize()}},
            "Handle": rich(handle),
            "Account": {"relation": [{"id": account_page_id}]},
            "Posted": {"date": {"start": parse_ts(e["timestamp"]).astimezone(TZ).isoformat(timespec="minutes")}},
            "Link": {"url": e["permalink"] or None},
            "Views": {"number": values["views"]},
            "Likes": {"number": values["likes"]},
            "Comments": {"number": values["comments"]},
            "Views 24h": {"number": values["v24"]},
            "Views 7d": {"number": values["v7"]},
            "Views 30d": {"number": values["v30"]},
            "Media ID": rich(media_id),
            "Last Updated": {"date": {"start": now.isoformat(timespec="minutes")}},
        }
        if e.get("notion"):
            try:
                notion("PATCH", f"pages/{e['notion']}", {"properties": props})
            except RuntimeError as err:
                if "HTTP 404" not in str(err) and "archived" not in str(err).lower():
                    raise
                e.pop("notion")  # row was deleted in Notion; recreate it below
        if not e.get("notion"):
            e["notion"] = notion("POST", "pages", {"parent": {"database_id": REELS_DB},
                                                   "properties": props})["id"]
        e["pushed"] = values


def reel_month_stats(media, now):
    """Average views and number of reels posted in the last 30 days, from this run's recent posts."""
    since = now - timedelta(days=30)
    recent = [m for m in media if is_reel(m) and "timestamp" in m and parse_ts(m["timestamp"]) >= since]
    views = [m["view_count"] for m in recent if m.get("view_count") is not None]
    return (round(sum(views) / len(views)) if views else None), len(recent)

def widget_reels(store, handle, today):
    out = []
    reels = sorted(((k, v) for k, v in store.get(handle, {}).items() if v.get("type", "reel") == "reel"),
                   key=lambda kv: kv[1]["timestamp"], reverse=True)
    for media_id, e in reels[:WIDGET_REELS]:
        days = sorted(e["series"])
        last = e["series"][days[-1]]
        out.append({
            "id": media_id,
            "posted": e["timestamp"],
            "link": e["permalink"],
            "thumb": e["thumb"],
            "title": reel_title(e),
            "views": last.get("v"), "likes": last.get("l"), "comments": last.get("c"),
            "v24": checkpoint(e, "v24"), "v7": checkpoint(e, "v7"), "v30": checkpoint(e, "v30"),
            "history": [{"date": d, "views": e["series"][d].get("v")} for d in days[-30:]],
        })
    return out


# ---------------------------------------------------------------- monthly reports

def month_bounds(ym):
    y, m = map(int, ym.split("-"))
    start = date(y, m, 1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def rt(text, url=None, bold=False, color=None):
    item = {"type": "text", "text": {"content": str(text)[:1900]}}
    if url:
        item["text"]["link"] = {"url": url}
    if bold or color:
        item["annotations"] = {"bold": bold, "color": color or "default"}
    return item


def block(kind, parts, **extra):
    body = {"rich_text": parts}
    body.update(extra)
    return {"object": "block", "type": kind, kind: body}


def n(v):
    return f"{v:,}" if isinstance(v, (int, float)) else "—"


def month_summary(series, media, ym):
    """Numbers for one account and one month, from follower snapshots and this run's recent posts."""
    start, end = month_bounds(ym)
    days = sorted(d for d in series if start.isoformat() <= d <= end.isoformat())
    posts = [m for m in media if "timestamp" in m
             and start <= parse_ts(m["timestamp"]).astimezone(TZ).date() <= end]
    if not days and not posts:
        return None
    f_start = series[days[0]]["followers"] if days else None
    f_end = series[days[-1]]["followers"] if days else None
    growth = (f_end - f_start) if days else None
    reels = [m for m in posts if post_type(m) == "reel"]
    views = [m["view_count"] for m in reels if m.get("view_count") is not None]
    rates = sorted(((m.get("like_count") or 0) + (m.get("comments_count") or 0)) / f_end
                   for m in posts if m.get("like_count") is not None and f_end)
    mid = len(rates) // 2
    engagement = None if not rates else round(rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2, 4)
    ranked = sorted(posts, key=lambda m: (m.get("view_count") or -1, m.get("like_count") or 0), reverse=True)
    return {
        "start": start, "end": end, "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
        "f_start": f_start, "f_end": f_end, "growth": growth,
        "growth_pct": round(growth / f_start, 4) if growth is not None and f_start else None,
        "posts": posts, "reels": len(reels),
        "views": sum(views) if views else None,
        "avg_views": round(sum(views) / len(views)) if views else None,
        "likes": sum(m.get("like_count") or 0 for m in posts),
        "comments": sum(m.get("comments_count") or 0 for m in posts),
        "engagement": engagement, "top": ranked[:3],
    }


def report_blocks(handle, s, ym, now, final, prev):
    month_name = s["start"].strftime("%B %Y")
    lines = []
    if s["growth"] is not None:
        sign = "+" if s["growth"] >= 0 else ""
        pct = f" ({sign}{s['growth_pct'] * 100:.1f}%)" if s["growth_pct"] is not None else ""
        lines.append(f"Followers: {n(s['f_start'])} → {n(s['f_end'])}, {sign}{n(s['growth'])}{pct}")
    lines.append(f"Posts: {len(s['posts'])} ({s['reels']} reel{'' if s['reels'] == 1 else 's'})")
    if s["views"] is not None:
        lines.append(f"Reel views: {n(s['views'])} total, {n(s['avg_views'])} average per reel")
    lines.append(f"Likes: {n(s['likes'])} · Comments: {n(s['comments'])}")
    if s["engagement"] is not None:
        lines.append(f"Engagement: {s['engagement'] * 100:.1f}% per post (median)")
    out = [block("callout", [rt("\n".join(lines))], icon={"type": "emoji", "emoji": "📈"}, color="gray_background")]

    if prev:
        bits = []
        if prev.get("growth") is not None and s["growth"] is not None:
            bits.append(f"follower growth {n(s['growth'])} vs {n(prev['growth'])}")
        if prev.get("avg_views") is not None and s["avg_views"] is not None:
            bits.append(f"avg views per reel {n(s['avg_views'])} vs {n(prev['avg_views'])}")
        bits.append(f"posts {len(s['posts'])} vs {prev.get('posts', '—')}")
        out.append(block("paragraph", [rt("Compared with last month: ", bold=True), rt("; ".join(bits) + ".")]))

    if s["top"]:
        out.append(block("heading_2", [rt("Top posts")]))
        for m in s["top"]:
            when = parse_ts(m["timestamp"]).astimezone(TZ).strftime("%b %-d")
            stats = (f"{n(m['view_count'])} views · " if m.get("view_count") is not None else "") + \
                f"{n(m.get('like_count'))} likes · {n(m.get('comments_count'))} comments · {when}"
            out.append(block("numbered_list_item", [rt(reel_title(m, post_type(m).capitalize()), m.get("permalink")),
                                                     rt(" — " + stats)]))

    rows = [["Date", "Post", "Type", "Views", "Likes", "Comments"]]
    for m in sorted(s["posts"], key=lambda m: m["timestamp"], reverse=True)[:99]:
        rows.append([parse_ts(m["timestamp"]).astimezone(TZ).strftime("%b %-d"),
                     (reel_title(m, post_type(m).capitalize()), m.get("permalink")),
                     post_type(m).capitalize(), n(m.get("view_count")), n(m.get("like_count")),
                     n(m.get("comments_count"))])
    out.append(block("heading_2", [rt(f"All posts in {month_name} ({len(s['posts'])})")]))
    if len(rows) > 1:
        out.append({"object": "block", "type": "table", "table": {
            "table_width": 6, "has_column_header": True, "has_row_header": False,
            "children": [{"object": "block", "type": "table_row", "table_row": {"cells": [
                [rt(c[0], c[1])] if isinstance(c, tuple) else [rt(c)] for c in r]}} for r in rows]}})
    else:
        out.append(block("paragraph", [rt("No posts this month.")]))

    notes = [f"Views and likes are as of {now.strftime('%b %-d, %Y')}; posts from late in the month have had "
             "less time to collect views. Views include paid views and Facebook views on cross-posted reels."]
    if s["first_day"] and s["first_day"] > s["start"].isoformat():
        notes.append(f"Follower tracking for this month starts {date.fromisoformat(s['first_day']).strftime('%b %-d')}.")
    if not final:
        notes.append(f"Preview: {month_name} isn't over yet. The final report is created automatically "
                     f"at the start of next month.")
    out.append(block("paragraph", [rt(" ".join(notes), color="gray")]))
    out.append(block("paragraph", [rt("Live content widget", f"{WIDGET_BASE}?handle={handle}&view=content")]))
    return out


def write_report(reports, ym, inp, now, final):
    """Create one account's report page for month ym, replacing an earlier preview of the same month."""
    s = month_summary(inp["series"], inp["media"], ym)
    if s is None:
        return False
    month_name = s["start"].strftime("%B %Y")
    title = f"{inp['label']} · {month_name}" + ("" if final else f" (preview through {now.strftime('%b %-d')})")
    prev_ym = (s["start"] - timedelta(days=1)).strftime("%Y-%m")
    prev = reports.get(prev_ym, {}).get(inp["handle"])
    prev = prev if prev and prev.get("status") == "Final" else None
    top = s["top"][0] if s["top"] else None
    props = {
        "Report": {"title": [{"text": {"content": title}}]},
        "Account": {"relation": [{"id": inp["page_id"]}]},
        "Month": {"date": {"start": s["start"].isoformat(), "end": s["end"].isoformat()}},
        "Status": {"select": {"name": "Final" if final else "Preview"}},
        "Followers Start": {"number": s["f_start"]}, "Followers End": {"number": s["f_end"]},
        "Follower Growth": {"number": s["growth"]}, "Growth %": {"number": s["growth_pct"]},
        "Posts": {"number": len(s["posts"])}, "Reels": {"number": s["reels"]},
        "Total Views": {"number": s["views"]}, "Avg Views per Reel": {"number": s["avg_views"]},
        "Total Likes": {"number": s["likes"]}, "Total Comments": {"number": s["comments"]},
        "Engagement": {"number": s["engagement"]},
        "Top Post": {"url": top.get("permalink") if top else None},
        "Generated": {"date": {"start": now.isoformat(timespec="minutes")}},
    }
    old = reports.get(ym, {}).get(inp["handle"])
    page = notion("POST", "pages", {"parent": {"database_id": REPORTS_DB}, "properties": props,
                                    "icon": {"type": "emoji", "emoji": "📊"},
                                    "children": report_blocks(inp["handle"], s, ym, now, final, prev)})
    if old and old.get("page"):
        try:
            notion("PATCH", f"pages/{old['page']}", {"archived": True})
        except RuntimeError:
            pass  # already deleted in Notion
    reports.setdefault(ym, {})[inp["handle"]] = {
        "page": page["id"], "status": "Final" if final else "Preview",
        "growth": s["growth"], "avg_views": s["avg_views"], "posts": len(s["posts"]),
        "views": s["views"], "f_end": s["f_end"],
    }
    return True


def run_reports(report_inputs, now, failures):
    if not REPORTS_DB:
        return
    reports = json.loads(REPORTS_FILE.read_text()) if REPORTS_FILE.exists() else {}
    today = now.date()
    jobs = []
    if REPORT_MONTH:
        _, end = month_bounds(REPORT_MONTH)
        jobs.append((REPORT_MONTH, end < today, True))
    if today.day <= REPORT_DAYS:
        prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        if prev_month != REPORT_MONTH:
            jobs.append((prev_month, True, False))
    for ym, final, forced in jobs:
        made = 0
        for inp in report_inputs:
            existing = reports.get(ym, {}).get(inp["handle"])
            if not forced and existing and existing.get("status") == "Final":
                continue
            try:
                made += write_report(reports, ym, inp, now, final)
            except Exception as err:  # noqa: BLE001 - a report problem must not stop the nightly update
                failures.append(f"{inp['label']}: {ym} report: {err}")
                print(f"  ! {inp['handle']}: {ym} report failed: {err}")
        if made:
            print(f"{ym} reports written: {made} ({'Final' if final else 'Preview'})")
    REPORTS_FILE.write_text(json.dumps(reports, indent=1, sort_keys=True))


# ---------------------------------------------------------------- main

def main():
    now = datetime.now(TZ)
    today = now.date()
    day = today.isoformat()

    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
    media_store = json.loads(MEDIA_FILE.read_text()) if MEDIA_FILE.exists() else {}
    widget_accounts, failures, report_inputs = [], [], []

    accounts = active_accounts()
    print(f"{len(accounts)} active account(s)")

    for row in accounts:
        page_id = row["id"]
        props = row["properties"]
        handle = clean_handle(plain(props.get("Handle")))
        label = plain(props.get("Name")) or handle
        if not handle:
            set_status(page_id, "Missing handle")
            failures.append(f"{label}: missing handle")
            continue
        try:
            ig = lookup(handle)
        except Exception as err:  # noqa: BLE001 - report every failure, keep going
            msg = str(err)
            if "Invalid user id" in msg or "Cannot find User" in msg or "(#110)" in msg:
                msg = "Not found. Is the handle right, and is the account Business or Creator?"
            set_status(page_id, f"Error {day}: {msg}")
            failures.append(f"{label}: {msg}")
            print(f"  ✗ {handle}: {msg}")
            continue

        followers = ig.get("followers_count")
        posts = ig.get("media_count")
        series = history.setdefault(handle, {})
        series[day] = {"followers": followers, "posts": posts}

        change_prev = previous_change(series, today)
        change_7 = change_since(series, today, 7)
        change_30 = change_since(series, today, 30)
        first_day = min(series)
        change_start = followers - series[first_day]["followers"]

        media = (ig.get("media") or {}).get("data", [])
        engagement = engagement_rate(media, followers)
        avg30, reels30 = reel_month_stats(media, now)

        upsert_snapshot(page_id, handle, day, followers, posts, change_prev)
        notion("PATCH", f"pages/{page_id}", {"properties": {
            "Engagement Rate": {"number": engagement},
            "Avg Reel Views (30d)": {"number": avg30},
            "Followers": {"number": followers},
            "7-Day Change": {"number": change_7},
            "30-Day Change": {"number": change_30},
            "Posts": {"number": posts},
            "Display Name": rich(ig.get("name")),
            "Profile Picture": {"url": ig.get("profile_picture_url")},
            "Last Updated": {"date": {"start": now.isoformat(timespec="minutes")}},
            "Status": rich("OK"),
        }})

        try:
            update_reels(media_store, handle, page_id, media, now)
        except Exception as err:  # noqa: BLE001 - reel tracking must never block follower tracking
            failures.append(f"{label}: reels: {err}")
            print(f"  ! {handle}: reel tracking failed: {err}")

        recent = sorted(d for d in series if d >= (today - timedelta(days=WIDGET_HISTORY_DAYS)).isoformat())
        widget_accounts.append({
            "handle": ig.get("username", handle),
            "label": label,
            "name": ig.get("name") or "",
            "picture": ig.get("profile_picture_url") or "",
            "followers": followers,
            "posts": posts,
            "change7": change_7,
            "change30": change_30,
            "changeSinceStart": change_start,
            "trackingSince": first_day,
            "history": [{"date": d, "followers": series[d]["followers"]} for d in recent],
            "engagement": engagement,
            "avg30": avg30,
            "reels30": reels30,
            "reels": widget_reels(media_store, handle, today),
            "content": content_items(media, now),
        })
        report_inputs.append({"handle": ig.get("username", handle), "label": label, "page_id": page_id,
                              "series": series, "media": media})
        print(f"  ✓ {handle}: {followers:,} followers")

    # prune and save
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for handle in history:
        history[handle] = {d: v for d, v in sorted(history[handle].items()) if d >= cutoff}
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=1, sort_keys=True))

    reel_cutoff = now - timedelta(days=REEL_KEEP_DAYS)
    for handle in media_store:
        media_store[handle] = {k: v for k, v in media_store[handle].items()
                               if parse_ts(v["timestamp"]) >= reel_cutoff}
    MEDIA_FILE.write_text(json.dumps(media_store, indent=1, sort_keys=True))

    run_reports(report_inputs, now, failures)

    WIDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    WIDGET_FILE.write_text(json.dumps({
        "updated": now.isoformat(timespec="minutes"),
        "accounts": widget_accounts,
    }, indent=1))

    if failures:
        print("\nSome accounts failed:\n  " + "\n  ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
