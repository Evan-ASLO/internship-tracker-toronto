# Internship Tracker — Toronto

Verified-live internship board for the Greater Toronto Area, focused on four
business tracks: **Marketing, Business Operations, Finance, Consulting**.

**Live site:** https://internship-tracker-gamma-five.vercel.app

## How it works

Every hour, GitHub Actions runs the pipeline:

1. **Pull** — reads postings directly from employer applicant-tracking systems
   (Greenhouse, Ashby, Workday, SuccessFactors, optional Adzuna, and native
   employer APIs). Nothing is invented. Workday queries use
   tenant-issued Toronto/GTA/Ontario location facets and paginate to exhaustion;
   SuccessFactors pagination continues until empty or repeated results.
2. **Filter** — keeps internship/co-op titles in the Toronto area, rejecting
   senior roles that merely contain a keyword like "campus".
3. **Verify** — re-fetches every posting URL and requires a live response.
   Closed requisitions are dropped before they reach the site.
4. **Compare** — checks posting content against the live Vercel site, not git
   HEAD, so a failed deploy cannot make later runs incorrectly look current.
5. **Protect** — refuses a verified catalog drop greater than 25% compared
   with the higher of the checked-in and live-site baselines unless a manual
   run explicitly approves the seasonal change.
6. **Dual publish** — when the `CLOUDFLARE_DUAL_PUBLISH` Actions variable is
   `true`, HMAC-signs and sends a normalized snapshot to Cloudflare. The
   existing Vercel production project remains the default publication path and
   updates when live drift is detected or `force_deploy` is requested.
7. **Commit** — records the refreshed snapshots only after every required
   publication succeeds.

## Why the numbers can look low

The board never pads itself with invented listings. In the off-season a small
number is the market, not a gap in coverage. Anything older than 45 days is
flagged as stale rather than quietly presented as fresh.

Every scrape prints a **SOURCE COVERAGE** table with provider query totals,
pages, returned rows, product-filtered rows, unique rows, and verified-live
rows. Missing location facets, request errors, repeated pages, and safety-cap
truncation are reported explicitly so source coverage cannot silently degrade.
Workday collection and posting verification retry bounded transient TLS,
rate-limit, and server failures rather than treating one network error as a
closed job.

## Layout

| Path | Purpose |
|---|---|
| `scrape.py` | Pull + filter + liveness verification |
| `publish_snapshot.py` | Deterministic normalization, hashing, signing, and Cloudflare ingest |
| `catalog_guard.py` | Normalized-count drop protection with explicit override |
| `publication_gate.py` | Required Cloudflare/Vercel outcome validation |
| `site/index.html` | The single-file app (landing, board, tracker) |
| `site/migration.js` | Versioned saved/application persistence and export |
| `site/live_jobs.json` | Current verified snapshot — the only data source |
| `.github/workflows/refresh.yml` | Hourly automation |
| `docs/migration-contract.md` | Exact ingestion and browser export contracts |

## Running it by hand

```bash
pip install requests
python scrape.py          # writes live_jobs.json
python publish_snapshot.py --dry-run
```

Trigger the automation manually from the **Actions** tab → *Refresh Toronto
internships* → *Run workflow*.

## Configuration

| Secret | Purpose |
|---|---|
| `VERCEL_TOKEN` | Deploys the site. Without it the workflow fails loudly rather than silently serving stale data. |
| `VERCEL_ORG_ID` | Pins deployment to the existing Vercel account. |
| `VERCEL_PROJECT_ID` | Pins deployment to the existing production project. |
| `CLOUDFLARE_JOBS_INGEST_URL` | Full HTTPS URL for `/api/internal/jobs/snapshot`. |
| `CLOUDFLARE_JOBS_INGEST_SECRET` | Shared HMAC-SHA256 secret for snapshot authentication. |
| `ADZUNA_APP_ID` | Optional Adzuna developer application ID. The adapter is skipped when either Adzuna secret is absent. |
| `ADZUNA_APP_KEY` | Optional Adzuna developer application key. Never exposed to the browser or logs. |

Cloudflare publication is disabled unless the repository Actions variable
`CLOUDFLARE_DUAL_PUBLISH` is exactly `true`. A manual run may set
`allow_catalog_drop` only after reviewing a legitimate drop greater than 25%.

To add or change a track, edit `CATEGORIES` in `scrape.py` and the matching
array in `site/index.html`.

### Optional Adzuna source

Register at [developer.adzuna.com](https://developer.adzuna.com/signup) to
receive the official `app_id` and `app_key`, then add them as the encrypted
repository secrets `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`. The hourly workflow
passes them only to the scrape step. If either value is missing, the adapter
makes no request and reports `skipped` in source coverage.

The adapter uses the official Canada endpoint
`/v1/api/jobs/ca/search/1`, centered on Toronto with a 100 km radius. Every
second hour runs all five targeted 50-result queries: `internship`, `intern`,
`co-op/coop`, `student`, and the exact phrase `new grad`. The noisy standalone
term `graduate` is never queried.

On the alternating hour, the scraper carries the prior successful Adzuna slice
from the checked-in baseline and still applies normal deduplication and URL
verification. A successful active refresh atomically replaces that slice. A
partial provider failure reuses the prior slice for up to six hours; its
original refresh timestamp persists in top-level `source_state.adzuna`. After
the grace window, the source is marked `expired` and catalog guard requires
manual `allow_catalog_drop` review instead of silently publishing a wipe.

Normal operation is exactly 60 requests/day, 420/week, 1,800 per 30-day month,
and 1,860 per 31-day month, below Adzuna's published default limits of
25/minute, 250/day, 1,000/week, and 2,500/month. Transient retries consume the
same hard five-request active-run budget and may displace a later query, but
cannot increase the total. Per-query totals, returned rows, kept rows, caps,
reuse status, source age, and errors appear in coverage telemetry.

Before changing query allocation, manually dispatch *Refresh Toronto
internships* with `adzuna_dry_run=true`. That runs only the Adzuna adapter,
skips the refresh/publish/deploy/commit job, and uploads a seven-day sanitized
coverage JSON artifact. Set `force_adzuna_refresh=true` on a normal manual run
only when an immediate full Adzuna replacement is required outside the
two-hour cadence.

[Adzuna's terms](https://developer.adzuna.com/docs/terms_of_service) require
published listings to carry linked **Jobs by Adzuna** logo attribution
(minimum 116 × 23 px). They also state that ongoing commercial, government, or
academic use beyond a 14-day evaluation may require written consent or a
licence. The legacy Vercel cards, details, and tracker now render the bundled
official developer-portal asset at `site/assets/adzuna-logo.png`, with both the
**Jobs by** text and logo linked to `https://www.adzuna.ca/`, whenever
`src === "adzuna"`.

The Cloudflare v1 marker is exactly `source === "adzuna"`. Do not enable the
secrets until the Cloudflare jobs list, job detail, saved-job, and application
surfaces also render the same linked minimum-size attribution and the intended
use is confirmed with Adzuna.

## Moving browser data

The Profile view includes **Export / Move my data**. From this release onward,
saved jobs and application statuses persist under
`internship_tracker_user_state_v1` and can be downloaded as versioned JSON.
The export excludes filters, onboarding flags, and other browser settings. See
[`docs/migration-contract.md`](docs/migration-contract.md) for the exact schema.
If stored migration data is malformed or from a future schema version, writes
pause and the raw value is preserved under
`internship_tracker_user_state_recovery_v1` rather than being overwritten.
