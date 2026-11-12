# Follower Tracker

Every morning, GitHub Actions takes a daily snapshot of Instagram follower counts for every Active account in the Notion **IG Accounts** database. It logs those snapshots to **IG Snapshots** and updates the follower cards widget.

## How it works

- `tracker.py` looks up each account with Instagram Business Discovery, then writes the results to Notion and to `docs/data.json`.
- `.github/workflows/daily.yml` runs the script every day at 7:17 AM Indianapolis time. You can also run it by hand: go to **Actions → Daily Instagram snapshot → Run workflow**.
- `docs/index.html` is the widget, served by GitHub Pages and embedded in Notion.
- `data/history.json` stores the daily history, which is used to calculate the 7- and 30-day changes.

## Repository secrets (Settings → Secrets and variables → Actions)

| Name | Value |
|---|---|
| `IG_ACCESS_TOKEN` | The never-expiring Page token for "Maximize Media with AJ Kallas" |
| `NOTION_TOKEN` | The Internal Integration Secret from notion.so/profile/integrations |

The Notion integration needs access to the **Instagram Tracker** page. To grant it, open the page and go to **••• → Connections → add the integration**.

## Adding a client

Add a row to **IG Accounts** with their handle (without the @) and check **Active**. Their account must be a Business or Creator account. They'll appear after the next run.

## Widget links (paste into a Notion /embed block)

| What | URL |
|---|---|
| One client, large card | `https://<user>.github.io/<repo>/?handle=ajkallasvideo` |
| All clients | `https://<user>.github.io/<repo>/` |
| All clients, first one large | `https://<user>.github.io/<repo>/?feature=first` |
| Several specific accounts | `?handle=one,two,three` |
| Overview: followers, chart, weekly change, and reel stats | `?handle=tyler_lingle&view=overview` |
| One number in a compact tile | `?handle=tyler_lingle&view=stat&stat=growth7` |
| Top reel of the last 30 days | `?handle=remingtontramsey&view=spotlight` |
| Overview with the newest reels underneath | `?handle=remingtontramsey&view=overview&reels=6` |
| Just the newest reels, nothing else | `?handle=remingtontramsey&view=reelgrid` |
| Any reel view sized to fit the embed, no scrolling | add `&fit=1` |
| Reels in a single row (3 by default) | add `&layout=row` |

Options: `&theme=light` / `&theme=dark` (the default follows the device), `&size=large` / `&size=small`, `&chart=0`, `&stats=1` (engagement), `&days=N`, `&view=reels` (`&count=N`), `&bg=none`.

| View | What it shows | Works with |
|---|---|---|
| `view=reels` | Recent reels grid with avg views, top reel, and engagement | `count=N` |
| `view=overview` | The large card (follower count, growth chart, weekly change) plus a row with avg views of the last 6 reels, top reel views in the last 30 days, and engagement. Stats without data are hidden. | `chart=0`, `days=N`, `reels=N` (adds the newest N reels underneath, max 12), `details=0` (reel tiles show only thumbnail + views) |
| `view=reelgrid` | Only the newest reels as tiles (thumbnail, views, caption, date, likes, comments). No follower numbers, handle, or stats. | `count=N` (default 6), `details=0` (thumbnail + views only) |
| `view=stat&stat=NAME` | A compact tile with one big number, a caption, and the handle. It fits embeds as small as 260×160. | `stat=` one of the names below |
| `view=spotlight` | The most-viewed reel posted in the last 30 days, with thumbnail, caption, date, views, likes, comments, and 7-day views. The whole card opens the reel. | |

All views accept `theme` and `bg=none`.

**`fit=1`** (reels, reelgrid, overview with reels): the card fills the embed and the reel tiles resize so everything fits without scrolling. It picks the column count that gives the biggest tiles, and hides captions, then dates/likes, when tiles get small. Works with one account per embed.

**`layout=row`** (reels, reelgrid, overview with reels): all reels in one row. Shows 3 reels unless `count=` (or `reels=` on the overview) says otherwise. Combine with `fit=1` to size the row to the embed.

The reels and overview stat rows show four numbers: avg views of the last 6 reels, top reel (30 days), engagement, and avg views of reels posted in the last 30 days.

`stat=` names: `avg30` (avg views, reels posted in the last 30 days), `followers`, `growth7` (change this week; new accounts show change since tracking started), `avgviews` (last 6 reels), `topreel` (top reel views, last 30 days), `engagement`, `posts30` (reels posted in the last 30 days; shows "9+" when every reel the widget receives is that recent). If `stat` is left out, the tile shows `followers`.

## If something breaks

- **The workflow fails and GitHub emails you:** open the run log. Each account's error is also written to its **Status** column in Notion.
- **"Not found":** the handle is wrong, or the account is set to Personal instead of Business or Creator.
- **Token errors (code 190):** the Page token stopped working. This happens after a Facebook password change or if the app's access was removed. Generate a new Page token and replace the `IG_ACCESS_TOKEN` secret.
