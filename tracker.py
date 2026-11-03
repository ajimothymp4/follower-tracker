#!/usr/bin/env python3
"""Daily Instagram follower snapshot.

For every Active row in the Notion "IG Accounts" database this script:
  1. looks the account up through Instagram Business Discovery,
  2. logs today's numbers in the Notion "IG Snapshots" database (one row per account per day),
  3. updates the account row (Followers, 7-Day Change, 30-Day Change, ...),
  4. writes docs/data.json, which the follower-card widget reads.

Uses only the Python standard library. Configuration comes from environment variables
(see .github/workflows/daily.yml).
"""
import json
import os
import re
import sys
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

ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "data" / "history.json"
WIDGET_FILE = ROOT / "docs" / "data.json"
KEEP_DAYS = 800          # history kept in the repo
WIDGET_HISTORY_DAYS = 90  # history sent to the widget


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
    return http(method, f"https://api.notion.com/v1/{path}", {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
    }, body)


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

def lookup(handle):
    fields = (f"business_discovery.username({handle})"
              "{username,name,followers_count,media_count,profile_picture_url}")
    url = (f"https://graph.facebook.com/{GRAPH_VERSION}/{IG_USER_ID}?"
           + urllib.parse.urlencode({"fields": fields, "access_token": IG_TOKEN}))
    return http("GET", url)["business_discovery"]


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


# ---------------------------------------------------------------- main

def main():
    now = datetime.now(TZ)
    today = now.date()
    day = today.isoformat()

    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
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

        upsert_snapshot(page_id, handle, day, followers, posts, change_prev)
        notion("PATCH", f"pages/{page_id}", {"properties": {
            "Followers": {"number": followers},
            "7-Day Change": {"number": change_7},
            "30-Day Change": {"number": change_30},
            "Posts": {"number": posts},
            "Display Name": rich(ig.get("name")),
            "Profile Picture": {"url": ig.get("profile_picture_url")},
            "Last Updated": {"date": {"start": now.isoformat(timespec="minutes")}},
            "Status": rich("OK"),
        }})

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
        })
        print(f"  ✓ {handle}: {followers:,} followers")

    # prune and save
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for handle in history:
        history[handle] = {d: v for d, v in sorted(history[handle].items()) if d >= cutoff}
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=1, sort_keys=True))

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
