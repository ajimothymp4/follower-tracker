#!/usr/bin/env python3
"""Daily Instagram follower snapshot.

For every Active row in the Notion "IG Accounts" database this script:
  1. looks the account up through Instagram Business Discovery,
  2. logs today's numbers in the Notion "IG Snapshots" database (one row per account per day),
  3. updates the account row (Followers, 7-Day Change, 30-Day Change, ...),
  4. tracks each recent reel's views, likes and comments (Notion "Reel Report" + data/media.json),
  5. writes docs/data.json, which the widgets read.

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
from datetime import datetime, timedelta
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

ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "data" / "history.json"
WIDGET_FILE = ROOT / "docs" / "data.json"
KEEP_DAYS = 800          # history kept in the repo
WIDGET_HISTORY_DAYS = 90  # history sent to the widget
MEDIA_FILE = ROOT / "data" / "media.json"
MEDIA_LIMIT = 40          # recent posts pulled per account each run
REEL_TRACK_DAYS = 45      # reels younger than this are updated in Notion daily
REEL_KEEP_DAYS = 400      # reel history kept in the repo
WIDGET_REELS = 12         # reels sent to the widget per account
ENGAGEMENT_POSTS = 12     # posts used for the engagement rate


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


def reel_title(entry):
    first = (entry.get("caption") or "").strip().split("\n")[0].strip()
    when = parse_ts(entry["timestamp"]).astimezone(TZ).strftime("%b %-d")
    return (first[:90] + ("…" if len(first) > 90 else "")) if first else f"Reel · {when}"


def update_reels(store, handle, account_page_id, media, now):
    """Record today's numbers for each recent reel and keep the Notion Reel Report current."""
    day = now.date().isoformat()
    reels = store.setdefault(handle, {})
    for m in media:
        if not is_reel(m) or "timestamp" not in m:
            continue
        e = reels.setdefault(m["id"], {"series": {}})
        e.update({
            "timestamp": m["timestamp"],
            "permalink": m.get("permalink", ""),
            "caption": (m.get("caption") or "")[:500],
            "thumb": m.get("thumbnail_url") or m.get("media_url") or "",
        })
        e["series"][day] = {"v": m.get("view_count"), "l": m.get("like_count"),
                            "c": m.get("comments_count"), "at": now.isoformat(timespec="minutes")}

    if not REELS_DB:
        return
    track_from = now - timedelta(days=REEL_TRACK_DAYS)
    for media_id, e in reels.items():
        if parse_ts(e["timestamp"]) < track_from or day not in e["series"]:
            continue
        snap = e["series"][day]
        values = {"views": snap["v"], "likes": snap["l"], "comments": snap["c"],
                  "v24": checkpoint(e, "v24"), "v7": checkpoint(e, "v7"), "v30": checkpoint(e, "v30")}
        if e.get("pushed") == values and e.get("notion"):
            continue  # nothing changed since the last update
        props = {
            "Reel": {"title": [{"text": {"content": reel_title(e)}}]},
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
    reels = sorted(store.get(handle, {}).items(), key=lambda kv: kv[1]["timestamp"], reverse=True)
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


# ---------------------------------------------------------------- main

def main():
    now = datetime.now(TZ)
    today = now.date()
    day = today.isoformat()

    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
    media_store = json.loads(MEDIA_FILE.read_text()) if MEDIA_FILE.exists() else {}
    widget_accounts, failures = [], []

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
        })
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
