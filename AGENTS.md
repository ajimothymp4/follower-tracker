# Follower Tracker — notes for coding agents

Shared instructions for any coding agent (Claude Code, Codex, or others) working in this repo.

## What this is
Instagram follower and reel tracker for Maximize Media clients.
- `tracker.py` runs nightly on GitHub Actions (`.github/workflows/daily.yml`). It looks up every Active
  account in the Notion "IG Accounts" database through Instagram Business Discovery, logs snapshots to
  Notion, keeps a Notion "Reel Report" current, and writes `docs/data.json`.
- `docs/index.html` is the widget page served by GitHub Pages and embedded in Notion pages. It reads
  `docs/data.json`. Link options select the view (see README.md).
- `data/history.json` (followers per day) and `data/media.json` (reels per day) are the stored history.
  They are written by the nightly job — never edit them by hand.

## docs/data.json shape
```
{ updated, accounts: [ { handle, label, name, picture, followers, posts, change7, change30,
  changeSinceStart, trackingSince, history: [{date, followers}], engagement,   // fraction, e.g. 0.0125
  avg30, reels30,   // avg views and count of reels posted in the last 30 days
  reels: [ { id, posted /* 2026-11-10T18:38:47+0000 */, link, thumb, title, views, likes, comments,
             v24, v7, v30, history: [{date, views}] } ] } ] }
```

## Widget link options (must keep working exactly as they do now)
handle (comma list), theme=light|dark (default follows device), size=large|small, chart=0, stats=1,
days=N, view=reels, count=N, feature=first, bg=none, view=overview (reels=N, details=0),
view=stat&stat=followers|growth7|avgviews|topreel|engagement|posts30, view=spotlight,
view=reelgrid (count=N, details=0), stat=avg30, fit=1, layout=row.

## Design
Reuse the CSS variables (--card, --ink, --muted, --up, --down, …), the Inter font, the 28px card radius,
both light and dark themes, and the helpers in docs/index.html (el, fmt, compact, avatar, person, chart,
svgEl). New views should look like the same family. Use real buttons and links, alt text, aria-labels,
and keep text contrast at 4.5:1 or better.

## Rules
- Work on a new branch named `overnight/<short-name>` from an up-to-date main. Never push to main and
  never merge your own pull request.
- One agent at a time in a working copy. Before starting: `git checkout main && git pull`.
- Never touch secrets, tokens, `.github/workflows/`, or `data/` unless the task explicitly says so.
- Existing widget links must render exactly as before. Take baseline screenshots before changing
  anything and compare after.
- Test locally: `python3 -m http.server` in `docs/`, screenshots with Playwright + headless Chromium
  (`npx playwright install chromium` if needed). Instagram images may not load offline; that's fine.
- Put preview screenshots in `previews/` on your branch and update README.md when you add options.
- Finish with `gh pr create`: what changed, example links, test results, decisions made, preview images.
