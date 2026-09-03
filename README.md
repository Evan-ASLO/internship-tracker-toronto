# Internship Tracker — Toronto

Verified-live internship board for the Greater Toronto Area, focused on four
business tracks: **Marketing, Business Operations, Finance, Consulting**.

**Live site:** https://internship-tracker-gamma-five.vercel.app

## How it works

Every hour, GitHub Actions runs the pipeline:

1. **Pull** — reads postings directly from employer applicant-tracking systems
   (Greenhouse, Lever, Ashby, Workday, and native employer APIs). Nothing is
   scraped from a job aggregator and nothing is invented.
2. **Filter** — keeps internship/co-op titles in the Toronto area, rejecting
   senior roles that merely contain a keyword like "campus".
3. **Verify** — re-fetches every posting URL and requires a live response.
   Closed requisitions are dropped before they reach the site.
4. **Publish** — commits the snapshot and redeploys, but *only when the
   postings actually changed*. `fetched_at` changes every run, so the workflow
   compares posting content rather than the raw file.

## Why the numbers can look low

The board never pads itself with invented listings. In the off-season a small
number is the market, not a gap in coverage. Anything older than 45 days is
flagged as stale rather than quietly presented as fresh.

## Layout

| Path | Purpose |
|---|---|
| `scrape.py` | Pull + filter + liveness verification |
| `site/index.html` | The single-file app (landing, board, tracker) |
| `site/live_jobs.json` | Current verified snapshot — the only data source |
| `.github/workflows/refresh.yml` | Hourly automation |

## Running it by hand

```bash
pip install requests
python scrape.py          # writes live_jobs.json
```

Trigger the automation manually from the **Actions** tab → *Refresh Toronto
internships* → *Run workflow*.

## Configuration

| Secret | Purpose |
|---|---|
| `VERCEL_TOKEN` | Deploys the site. Without it the workflow fails loudly rather than silently serving stale data. |

To add or change a track, edit `CATEGORIES` in `scrape.py` and the matching
array in `site/index.html`.
