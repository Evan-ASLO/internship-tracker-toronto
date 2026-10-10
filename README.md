# Internship Tracker — Canada

Verified-live internship board for all Canadian provinces and territories,
including remote roles only when the source explicitly supports Canadian
applicants. Normalized jobs carry five career-track tags: **Marketing,
Business Operations, Finance, Consulting, Software Engineering**.

**Live site:** https://internship-tracker-gamma-five.vercel.app

## How it works

Every hour, GitHub Actions runs the pipeline:

1. **Pull** — reads postings directly from employer applicant-tracking systems
   (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, SuccessFactors,
   optional Adzuna, and native employer APIs). Nothing is invented. Workday queries use
   tenant-issued Canadian location facets and paginate to exhaustion;
   SuccessFactors pagination continues until empty or repeated results.
2. **Filter** — keeps Canadian internships, co-ops, student roles, and true
   new-grad roles. Generic worldwide/US-only remote listings and geographic
   namesakes such as Ontario, California are rejected.
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

The displayed catalog is an early-career catalog: it preserves a small number
of pre-existing new-grad roles for stable user/history references. The
internship-growth target is reported separately and counts only explicit
internship, co-op, and student-placement titles. Newly added `PUBLIC_BOARDS`
cannot introduce new-grad-only roles.

### Posting-date provenance

Recent-date filters use only source-supplied publication timestamps. They never
use scrape time, `last_seen`, import time, or an ATS update timestamp as a
substitute for the employer's posting date.

| Source | `postedAt` provenance |
|---|---|
| Greenhouse | `first_published`; never `updated_at`. If the list omits it, the scraper performs a bounded lookup against the official public job-detail endpoint for otherwise eligible roles. |
| Lever | `createdAt`, the posting creation timestamp documented by the Postings API. |
| Ashby | `publishedAt`, the public posting's documented published timestamp. |
| SmartRecruiters | `releasedDate`, the public posting release timestamp. |
| Amazon | `posted_date` from the employer job API is date-only text, so it remains `postedText` and does not become `postedAt`. |
| Adzuna | `created`, the Adzuna provider-listing timestamp returned with the job result. |
| Workday | Relative `postedOn` text is retained as `postedText` only; `postedAt` remains unavailable. |
| SuccessFactors | The list source has no reliable publication timestamp, so `postedAt` remains unavailable. |

Missing, invalid, or future source timestamps normalize to `postedAt: null`.
Source text may still be retained in `postedText` for display, but cannot qualify
a job for the 24-hour, 7-day, or 30-day filters.

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

Trigger the automation manually from the **Actions** tab → *Refresh Canadian
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

To add or change a normalized producer track, edit `CATEGORIES` in
`scrape.py`. Consumer facets map the canonical category label and search
aliases separately; producer changes do not infer or overwrite employer
industry.

`Software Engineering` is assigned from role-title evidence rather than the
employer's industry. A software co-op at a bank remains a banking employer
while its career track is Software Engineering. Recognized title signals
include software engineer/developer/development, SWE/SDE, backend, frontend,
full-stack, application/web/mobile developers, systems development engineer,
and French software-developer equivalents. Generic engineering, data/ML,
DevOps/SRE, QA, firmware/hardware/security, business development, and
software sales/marketing titles are not swept into the track without explicit
software-engineering evidence.

### Optional Adzuna source

Register at [developer.adzuna.com](https://developer.adzuna.com/signup) to
receive the official `app_id` and `app_key`, then add them as the encrypted
repository secrets `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`. The hourly workflow
passes them only to the scrape step. If either value is missing, the adapter
makes no request and reports `skipped` in source coverage.

The adapter uses the official Canada endpoint
`/v1/api/jobs/ca/search/{page}`, scoped to Canada.
When at least two hours have elapsed since the last successful Adzuna refresh,
the adapter uses the five-request budget for high-signal depth:

1. `title_only=internship`, page 1
2. `title_only=internship`, page 2
3. `what_or=co-op coop`, page 1
4. `what_or=co-op coop`, page 2
5. `what_phrase=new grad`, page 1

The redundant `intern` query and noisy broad `student` query are intentionally
excluded. New-grad results must also contain `new grad` or `new graduate` in
the title; a description-only mention is rejected.

Before two hours have elapsed, the scraper carries the prior successful Adzuna
slice (including a successful empty result) from the checked-in baseline and
still applies normal deduplication and URL verification. Delayed runs refresh
when due regardless of the UTC hour; the overall catalog's `fetched_at` never
resets the Adzuna clock. Missing, malformed, or future successful-refresh
timestamps make the cache ineligible for reuse and trigger a fresh attempt.
A successful active refresh atomically replaces that slice. A
partial provider failure reuses the prior slice for up to six hours; its
original refresh timestamp persists in top-level `source_state.adzuna`. After
the grace window, the source is marked `expired` and catalog guard requires
manual `allow_catalog_drop` review instead of silently publishing a wipe.

At an uninterrupted two-hour cadence, normal operation uses 60 requests/day,
420/week, 1,800 per 30-day month,
and 1,860 per 31-day month, below Adzuna's published default limits of
25/minute, 250/day, 1,000/week, and 2,500/month. Transient retries consume the
same hard five-request active-run budget and may displace a later query, but
cannot increase the total. Per-query totals, returned rows, kept rows, caps,
reuse status, source age, and errors appear in coverage telemetry. Fresh-cache
reuse is reported as `reused-fresh`; a failed due refresh does not advance the
last-successful-refresh timestamp or renew the six-hour failure grace.

Before changing query allocation, manually dispatch *Refresh Canadian
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

### Verified free public employer boards

`PUBLIC_BOARDS` in `scrape.py` extends the existing connectors with this bounded,
verified batch of Canadian employers on Greenhouse, Lever, Ashby, and
SmartRecruiters. The
registry includes finance and business employers such as EQ Bank, BMO-adjacent
fintechs, Arc'teryx, Coveo, Geotab, Trulioo, Orennia, StepStone, Brattle,
Wealthsimple, and Waabi alongside engineering, aerospace, AI, healthcare, and
climate-tech employers. Every shipped row is freshly fetched from the
employer's official ATS endpoint; registry candidates are never copied into
the snapshot. Geotab's separate `internshiplist2000` Greenhouse board is
retained because the live postings are Geotab-branded, link Geotab's official
campus program, and use canonical Greenhouse application IDs.

These are documented, unauthenticated **read-only** interfaces:
[Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html),
[Lever Postings API](https://github.com/lever/postings-api), and
[Ashby Public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api),
plus SmartRecruiters' public company postings API.
They need no service key or paid plan. This does **not** make hosting, GitHub
Actions minutes, or other existing providers free. No application submission,
private ATS API, paid proxy, JobSpy aggregator scrape, LinkedIn scrape, or
automated search-engine scraping is part of this pipeline.

Tenant candidates were derived from real employer career links and a
discovery-only comparison against the MIT-labelled OpenApply tenant registry.
OpenApply rows and descriptions are not ingested or redistributed; each tenant
must pass official API schema, stable ID, canonical URL, and employer identity
checks. The linked OpenApply source repository does not carry a root LICENSE,
so its job content is not treated as redistributable source data.
SimplifyJobs is excluded entirely because its repository has no detected
license or explicit bulk-reuse permission.

The expansion deliberately avoids a separate fleet of per-company HTML
scrapers. Greenhouse, Lever, Ashby, and SmartRecruiters cover the added employers through shared
maintained adapters. Bounded tenant validation is the “slug discovery” step;
the scheduled job then calls only documented/public ATS JSON endpoints.
Apify and other paid cloud actors are not required for this coverage and were
not authorized. “Hidden endpoint” work is limited to documented public ATS
interfaces used by employer career pages—no private API, bot-warning bypass,
or blind guessed-slug flood.

Greenhouse returns its complete published list; the adapter checks its
`meta.total` and rejects prospect posts (`internal_job_id: null`). Ashby
requires `isListed: true` and considers primary and secondary locations.
Lever uses `skip`/`limit` pagination until a short/empty page, with repeated
IDs and the 20-page safety cap treated as incomplete rather than a successful
empty result. SmartRecruiters uses its documented `country=ca`, `limit`, and
`offset` parameters, validates the returned company identifier and public
visibility, and builds stable canonical job links from the public posting ID.
Existing bounded network retries and concurrency limits apply.

The Workday registry also replaces stale site names with verified official
sites for CIBC Campus, RBC Early Talent, Manulife Jobs, Sun Life Campus, and
OMERS External. General RBC postings were deliberately not added because titles
such as “Branch Manager Intern” are not student placements.

Internship, co-op, student, and true new-grad titles are eligible throughout
Canada. Structured provider country/address fields take priority. Explicit
Canada or US+Canada remote roles are eligible; generic worldwide, North America,
and US-only remote roles are not. Canadian secondary locations are eligible,
while Ontario, California and other geographic namesakes are explicitly
rejected. Categories remain tags, not a requirement: technical internships
are eligible and are not mislabelled as business-track roles.
Provider-supplied deadlines are honored where present; missing industry,
duration, season, eligibility, or deadline values are not invented.

New board records use `board:provider-id` identities, official posting/application
URLs, `source_target`, and an absolute `last_seen`. Existing source IDs stay
unchanged. Canonical official URLs and matching cross-source listings are
deduplicated before counting additions; replacing an Adzuna copy with a direct
posting is not a new opportunity.

Each new board records its last successful collection in
`source_state.public_boards`. On a failed, malformed, or truncated fetch, its
last-good slice may be reused for six hours **without renewing its timestamp**.
An expired/missing cache or an inconclusive posting check (for example 403,
429, or a timeout) blocks both publishers even when the total drop is small.
`allow_catalog_drop` does not bypass this completeness block. A complete empty
response replaces the slice immediately; explicit closed/404/410 postings are
removed, not retained indefinitely. `SOURCE_BASELINE_PATH` selects the captured
checked-in snapshot; locally it defaults to `live_jobs.json`.

To add a board, first follow a real employer careers link or verify a public
ATS posting's employer identity, confirm its public API/terms and exact token,
and check its current eligible listings. Then add a `(source, board, company)`
entry to `PUBLIC_BOARDS`, not a guessed slug, aggregator board, or duplicate
main-board entry. Separate employer-verified campus boards, such as Geotab's,
are allowed when their official posting content proves the employer identity.
Exercise parsing, pagination, failure/cache, eligibility, and identity tests.
Compare against one dated snapshot and verify official pages before reporting
net-new positions. Write candidate evidence outside the repository; never run
a full paid-provider refresh just to evaluate a new free board.

Targeted tests (from the repository root):

```text
python -m unittest discover -s tests -p "test_public_sources.py"
python -m unittest discover -s tests -p "test_adzuna_source.py"
python -m unittest discover -s tests -p "test_catalog_guard.py"
python -m unittest discover -s tests -p "test_publish_snapshot.py"
```

## Moving browser data

The Profile view includes **Export / Move my data**. From this release onward,
saved jobs and application statuses persist under
`internship_tracker_user_state_v1` and can be downloaded as versioned JSON.
The export excludes filters, onboarding flags, and other browser settings. See
[`docs/migration-contract.md`](docs/migration-contract.md) for the exact schema.
If stored migration data is malformed or from a future schema version, writes
pause and the raw value is preserved under
`internship_tracker_user_state_recovery_v1` rather than being overwritten.
