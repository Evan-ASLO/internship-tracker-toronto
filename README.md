# Internship Tracker — Toronto

Verified-live internship board for the Greater Toronto Area, focused on four
business tracks: **Marketing, Business Operations, Finance, Consulting**.

**Live site:** https://internship-tracker-gamma-five.vercel.app

## How it works

Every hour, GitHub Actions runs the pipeline:

1. **Pull** — reads postings directly from employer applicant-tracking systems
   (Greenhouse, Ashby, Workday, SuccessFactors, and native employer APIs). Nothing is
   scraped from a job aggregator and nothing is invented. Workday queries use
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

Cloudflare publication is disabled unless the repository Actions variable
`CLOUDFLARE_DUAL_PUBLISH` is exactly `true`. A manual run may set
`allow_catalog_drop` only after reviewing a legitimate drop greater than 25%.

To add or change a track, edit `CATEGORIES` in `scrape.py` and the matching
array in `site/index.html`.

## Moving browser data

The Profile view includes **Export / Move my data**. From this release onward,
saved jobs and application statuses persist under
`internship_tracker_user_state_v1` and can be downloaded as versioned JSON.
The export excludes filters, onboarding flags, and other browser settings. See
[`docs/migration-contract.md`](docs/migration-contract.md) for the exact schema.
If stored migration data is malformed or from a future schema version, writes
pause and the raw value is preserved under
`internship_tracker_user_state_recovery_v1` rather than being overwritten.
