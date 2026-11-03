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

Options: `&theme=light` / `&theme=dark` (the default follows the device), `&size=large` / `&size=small`, `&bg=none`.

## If something breaks

- **The workflow fails and GitHub emails you:** open the run log. Each account's error is also written to its **Status** column in Notion.
- **"Not found":** the handle is wrong, or the account is set to Personal instead of Business or Creator.
- **Token errors (code 190):** the Page token stopped working. This happens after a Facebook password change or if the app's access was removed. Generate a new Page token and replace the `IG_ACCESS_TOKEN` secret.
