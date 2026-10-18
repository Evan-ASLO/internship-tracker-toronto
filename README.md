# Internship Tracker — Canada

Verified-live internship board for all Canadian provinces and territories,
including remote roles only when the source explicitly supports Canadian
applicants. Normalized jobs retain the existing **Marketing, Business
Operations, Finance, Consulting, and Software Engineering** tracks and add
overlapping technical-family tags derived from the posting title.

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

### Technical career tracks

Technical categories describe the role function, not the employer's industry.
A software co-op at a bank remains a banking employer. Jobs may carry multiple
technical tags; family counts therefore overlap and must not be summed as a
unique-job total.

The canonical schema-v1 values are:

- `Software Engineering`
- `Data Science & AI`
- `Cybersecurity`
- `IT & Infrastructure`
- `Quality & Test Engineering`
- `Hardware & Embedded Systems`
- `Electrical & Electronics Engineering`
- `Mechanical, Mechatronics & Robotics`
- `Civil & Structural Engineering`
- `Environmental Engineering`
- `Chemical, Process & Materials Engineering`
- `Industrial & Manufacturing Engineering`
- `Aerospace Engineering`
- `Biomedical Engineering`
- `Mining & Geological Engineering`
- `General Engineering`

Classification is title-evidence based. Bare `engineer`/`engineering` maps to
`General Engineering`, never `Software Engineering`. Specific signals can
overlap—for example, embedded software can be both Software Engineering and
Hardware & Embedded Systems, while mechatronics can be both Electrical &
Electronics Engineering and Mechanical, Mechatronics & Robotics. Generic
analyst, business-development, physical-security, product-management, and
business process re-engineering titles do not become technical solely because
an employer is technical or a broad keyword appears in the description.
French title evidence is supported for software, data/AI, IT, electrical,
mechanical, civil, environmental, process/materials, industrial, aerospace,
biomedical, mining/geological, and general engineering roles.

### Daily pre-law sources

Two additional schema-v1 career tracks cover student placements relevant
before law school:

- `Legal, Compliance & Governance`
- `Public Policy & Government Relations`

These tags require role or qualification evidence; they are not a blanket
claim that every legal-sounding job is open to a pre-law student. Newly added
roles are excluded when the source requires current JD/LLB/law-school
enrolment, completed legal education, articling eligibility, Law Society/bar
membership, or another legal licence. A preferred/asset mention alone does
not exclude a role. Law Clerk or Paralegal student roles may be included only
when the posting is a genuine placement; any mandatory specialized program or
credential is stated at the start of the normalized description.
Content-only classification for a corporate-communications title requires an
explicit source duty involving government-relations activities. The normalized
description retains a short factual classification-evidence note rather than
copying the advertisement.

`prelaw_sources.py` polls these official public sources:

| Source | Access and provenance | Refresh behavior |
|---|---|---|
| Government of Canada GC Jobs (`source=gcjobs`) | Public Service Commission public search/detail pages, stable `poster` IDs, official organization/location and real Pacific closing dates. Canada.ca permits non-commercial reproduction with accuracy, title/author/URL attribution; the tracker republishes only minimal factual metadata and an official link. GC Jobs exposes no trustworthy posting date, so `postedAt` and `postedText` remain null. | Complete public search once every 24 hours; 72-hour last-good grace for delayed/missed GitHub runs. Complete zero eligible rows replaces the slice. |
| Canadian Natural Oracle Campus (`source=oracle`) | Oracle's documented public Candidate Experience listing/detail API for the employer-owned CNRL Campus site. Stable ID is `CNRL-Campus:<Oracle Id>`; `ExternalPostedStartDate` is the posting date. “Until filled” remains a null deadline. | Same 24-hour interval and 72-hour grace. |

The hourly workflow remains unchanged; these sources self-throttle using the
captured snapshot's `source_state.daily_sources`. Off-cycle reuse keeps the
original successful timestamp, and a failed poll never renews it. After 72
hours without a complete poll, catalog guard blocks both publishers.
`force_pre_law_refresh` forces a due poll for controlled validation. GitHub
cron is best effort and may run late or miss an invocation.

Current discovery constraints are explicit:

- Job Bank's monthly archive is Open Government Licence–Canada but cannot
  establish current vacancy liveness. Job Bank's current service terms
  prohibit scripts, robots, screen scrapers, and automated query programs, so
  it is not collected.
- Ontario GOJobs advertises no public feed, presents a CAPTCHA to the release
  network, and disallows its bulk jobs/PDF routes in `robots.txt`; the tracker
  does not bypass those controls.
- General FSWEP inventories, generic careers pages, future application
  windows, and the Parliamentary Internship Programme before its announced
  opening are discovery leads, not specific live vacancies.

False positives such as generic credit/market risk, data or sales governance,
driver/product/environmental compliance, scientific regulatory affairs,
regulatory facilities, physical security, and software/AI “policy” roles do
not receive pre-law tags without direct legal, public-policy, legislative, or
government-relations evidence.

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
| GC Jobs | No trustworthy publication timestamp is exposed; real closing date is retained only as producer `application_deadline`. |
| Oracle Candidate Experience | `ExternalPostedStartDate` from the official employer API. |

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
| `source_names.json` | Names-only target inventory; unconfigured names remain pending |
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

Lever's [API robots rules](https://api.lever.co/robots.txt) and
[posting-site robots rules](https://jobs.lever.co/robots.txt) specify a
one-second crawl delay. Shared GET transport enforces that minimum separately
for each host across listing/detail collection, normal liveness verification,
retries, redirects, and concurrent callers. A host lock is held through each
send and the next request waits at least one second after its response or
failed attempt. Waiting does not occupy the existing global request semaphore;
its eight-request limit, retry counts, and source budgets are unchanged.
Requests' native session/adapter handles redirects and cookies as before;
there is no new dependency or alternate access path.

The Workday registry also replaces stale site names with verified official
sites for CIBC Campus, RBC Early Talent, Manulife Jobs, Sun Life Campus, and
OMERS External. General RBC postings were deliberately not added because titles
such as “Branch Manager Intern” are not student placements.

The nationwide placement pass adds only measured-yield official sources:
Bell, Hydro One, and FortisBC on SuccessFactors, plus campus/employer Workday
sites for Autodesk, CAE, Canadian Tire, Intact Financial, Cenovus, TC Energy,
Colliers, Thomson Reuters, BMO Campus, OLG, Mastercard, and Rockwell
Automation. The technical placement pass adds measured-yield Workday sources
for ABB, Ciena, Cisco, General Motors, GE Vernova, Intel, Marvell, Motorola
Solutions, and NVIDIA. These sources use the same six-hour last-good retention
and source-specific publication block as `PUBLIC_BOARDS`. Workday locations
may fall back to the public posting URL's `/job/{location}/...` segment only
when that segment independently resolves to a recognized Canadian place.
Generic multi-location labels can inherit a provider-issued Canadian facet;
explicit foreign locations cannot.

The first configured employer from the names-only follow-up is
[SaskPower's official careers board](https://jobs.saskpower.com/), registered as
`successfactors:saskpower` in `RETAINED_SOURCES`, not a new adapter. A bounded
check on October 18, 2026 found three distinct Canadian summer-student postings:
Administration, Engineering, and Labourer, for May-August 2027 with a December
31, 2026 closing date. All three official posting pages returned HTTP 200 and
identified SaskPower. All three survive the unchanged normal filtering,
deduplication, liveness, and snapshot-normalization path. None overlapped the
427-record [successful producer snapshot at `8c54578`](https://raw.githubusercontent.com/Evan-ASLO/internship-tracker-toronto/8c54578d6c7d0944643e3a2da9853d1e1c8df99a/live_jobs.json),
fetched at `2026-10-18T02:18:32.802322+00:00`, or the public Vercel feed checked
with the same timestamp/count, using the existing URL/listing deduplication.
These are three competition postings, not a count of hiring seats or a promise
of additional published jobs. Minimal observed title/URL/location fixtures are
in `tests/fixtures/saskpower_source.json`; no job descriptions are copied.

SaskPower's [robots rules](https://jobs.saskpower.com/robots.txt) do not disallow
the public `/search/` and `/job/` paths or specify a crawl delay. The collector
does not use the disallowed `/services/` RSS route, application, or
talent-community paths. The public jobs site and
[corporate privacy statement](https://www.saskpower.com/footer/how-we-do-business/privacy)
were reviewed without authentication; the account-linked MySaskPower terms
page is not treated as a job-content reuse licence. No account, special
permission, or general redistribution right is inferred. These observations
are dated prepublication checks. The standard SuccessFactors queries, filters,
deduplication, six-hour cache expiry, source-specific publication block, and
unknown posting-date handling remain unchanged. The student search reported
three results on one page; out-of-range searches can recirculate recommendation
links, which the existing collector deduplicates and reports as repeated terms.
Configuration alone does not run the collector or update a published snapshot.

The same bounded follow-up repairs one legacy Workday target:
`workday:otpp.wd3/External` becomes
`workday:otppb.wd3/OntarioTeachers_Careers`. Ontario Teachers' official
[homepage](https://www.otpp.com/en-ca/) and
[intern page](https://www.otpp.com/en-ca/careers/early-talent/interns/) both link
that exact tenant/site. Its public search returned valid paginated job data
without capped terms or errors, but **zero current eligible placements** in
the October 18 check: internal-controls, senior, and other nonplacement search
matches are not internships. This is a verified endpoint repair, not an
additional job count or a new employer. It stays in the existing `WORKDAY`
configuration/guard family; it is not presented as a measured-yield expansion.
The exact `otppb` alias maps to the existing Ontario Teachers' Pension Plan
name, alongside the legacy `otpp` alias, without fuzzy entity merging.

The broader follow-up registers the following additional verified employer
boards through the existing adapters and retention guards. These are new
configured feeds, not repairs of existing employer targets:

| Source key | Verified official board | Net-new eligible/live records in the October 18 check |
|---|---|---:|
| `successfactors:hatch` | [Hatch](https://jobs.hatch.com/) | 14 |
| `successfactors:aecon` | [Aecon](https://jobs.aecon.com/) | 30 |
| `successfactors:kiewit` | [Kiewit](https://kiewitcareers.kiewit.com/) | 15 |
| `successfactors:cityofmississauga` | [City of Mississauga](https://jobs.mississauga.ca/) | 4 |
| `successfactors:cityofottawa` | [City of Ottawa](https://jobs-emplois.ottawa.ca/city-jobs/) | 2 |
| `successfactors:cityoftoronto` | [City of Toronto](https://jobs.toronto.ca/jobsatcity/) | 0 |
| `workday:bb.wd3/Student` | [BlackBerry Student](https://bb.wd3.myworkdayjobs.com/Student) | 0 |
| `lever:pointclickcare` | [PointClickCare](https://jobs.lever.co/pointclickcare/) | 0 |
| `lever:caseware` | [Caseware](https://jobs.lever.co/caseware/) | 0 |
| `lever:benchsci` | [BenchSci](https://jobs.lever.co/benchsci/) | 0 |

The final bounded check invoked those actual registered `TASKS`, SaskPower,
and the repaired Teachers task at `2026-10-18T18:49:56Z` through
`2026-10-18T18:50:44Z`: 971 raw records, 71 role-eligible, 68 Canada-eligible,
68 unique, 68 live, and 68 normalized records. All 68 remain net-new after
combined cross-source and baseline deduplication against the
[official public feed](https://internship-tracker-gamma-five.vercel.app/live_jobs.json)
rechecked at `2026-10-18T18:50:45Z`: 437 records, fetched at
`2026-10-18T12:21:43.743492+00:00`. There were no baseline overlaps, closed
postings, source errors, or capped terms in this bounded batch. The total
includes SaskPower's same three summer-student postings; the immutable
427-record historical baseline also has zero overlaps. This is dated
candidate evidence, not a predicted production total or publication.
All 68 source publication dates remain unknown (`postedAt: null`).

Toronto's 51 raw records, PointClickCare's 77, and Caseware's 45 contain no
currently eligible placements. BenchSci returns a valid empty public list;
BlackBerry's official
[careers page](https://www.blackberry.com/en/careers) links the Student board,
whose placement searches return valid zero-total Workday responses.
These healthy zero-yield feeds remain configured for future automatic
discovery but contribute **zero added jobs**. The Teachers repair also
continues to yield zero eligible placements.

Toronto and Ottawa use prefixed search bases and root-relative job links.
SuccessFactors now joins those links with standard URL resolution instead
of duplicating the prefix; root-level boards and Kiewit's separately
prefixed Ganotec job links retain their official paths. Current robots rules
were rechecked for every host, including both paced Lever hosts. Disallowed
application, login, community, facet-refresh, and `/services/` routes are not
collected; no access barrier or content-redistribution permission is inferred.
Minimal observed English/French title, URL, and location fixtures are in
`tests/fixtures/verified_employer_sources.json`, without job descriptions.
No new adapter, dependency, filter, budget, workflow, or publication contract
is introduced. The counts above describe dated prepublication validation,
not a guaranteed production total.

Workday and SuccessFactors searches include English and French placement terms
(`intern`, `co-op`, `student`, `stagiaire`, and `étudiant`). The Amazon
employer API also includes bounded Software Development Engineer and Applied
Science internship queries alongside the existing business-track queries.
Repeated results are deduplicated, and the previously published Workday
identity wins when a campus and general site expose the same requisition.

Internship, co-op, student, and true new-grad titles are eligible throughout
Canada. Structured provider country/address fields take priority. Explicit
Canada or US+Canada remote roles are eligible; generic worldwide, North America,
and US-only remote roles are not. Canadian secondary locations are eligible,
while Ontario, California and other geographic namesakes are explicitly
rejected. Categories remain tags, not a requirement: technical internships
are eligible and are not mislabelled as business-track roles.
Provider-supplied deadlines are honored where present; missing industry,
duration, season, eligibility, or deadline values are not invented.

New retained-source records use provider-scoped identities, official
posting/application URLs, `source_target`, and an absolute `last_seen`.
Greenhouse, Lever, Ashby, and SmartRecruiters board records remain
`board:provider-id`; Workday requisitions remain tenant-scoped. Existing source
IDs stay unchanged. Canonical official URLs and matching cross-source listings
are deduplicated before counting additions; replacing an Adzuna copy with a
direct posting is not a new opportunity.

Each retained source records its last successful collection in
`source_state.public_boards`. On a failed, malformed, or truncated fetch, its
last-good slice may be reused for six hours **without renewing its timestamp**.
An expired/missing cache or an inconclusive posting check (for example 403,
429, or a timeout) blocks both publishers even when the total drop is small.
`allow_catalog_drop` does not bypass this completeness block. A complete empty
response replaces the slice immediately; explicit closed/404/410 postings are
removed, not retained indefinitely. `SOURCE_BASELINE_PATH` selects the captured
checked-in snapshot; locally it defaults to `live_jobs.json`.

To add a source, first follow a real employer careers link or verify a public
ATS posting's employer identity, confirm its public API/terms and exact token,
and check its current eligible listings. Add shared public-API boards to
`PUBLIC_BOARDS`; add verified Workday or SuccessFactors sites to
`RETAINED_SOURCES`. A complete, valid current empty/nonplacement response is
eligible for future discovery, but must be reported separately from actual
added jobs; an unparsed landing page or failed request is not an empty feed.
Do not add a guessed slug, aggregator board, or duplicate
main-board entry. Separate employer-verified campus boards, such as Geotab's or
BMO's, are allowed when their official posting content proves the employer
identity and baseline-first deduplication preserves existing records.
Exercise parsing, pagination, failure/cache, eligibility, and identity tests.
Compare against one dated snapshot and verify official pages before reporting
net-new positions. Write candidate evidence outside the repository; never run
a full paid-provider refresh just to evaluate a new free board.

Targeted tests (from the repository root):

```text
python -m unittest discover -s tests -p "test_public_sources.py"
python -m unittest discover -s tests -p "test_source_pacing.py"
python -m unittest discover -s tests -p "test_adzuna_source.py"
python -m unittest discover -s tests -p "test_catalog_guard.py"
python -m unittest discover -s tests -p "test_publish_snapshot.py"
```

### Names-only source inventory

`source_names.json` contains the deduplicated company, named job-board/portal,
and ATS-platform names from all 29 supplied numbered lists. The names-only
fixture in `tests/fixtures/source_name_import.json` also checks the repeated
recommendations: 616 name occurrences, 565 distinct spellings, and 556 canonical
names. Slash-separated companies/divisions remain separate names; the official
bilingual `CBC/Radio-Canada` name remains intact. No priorities, industries,
approaches, example jobs, placeholder companies, or adapter identifiers are
imported.

`scrape.py` loads that file relative to its own location and joins the names to
the existing `TASKS` registry using exact names and explicit aliases, not
substring/fuzzy matching. Aliases include FSWEP, CBC, Intact/Intact Financial,
Ontario Lottery and Gaming/OLG, Canadian Natural/Canadian Natural Resources,
and the shortened board/platform recommendations. `MaRS` maps to
`MaRS Discovery District`, never the unrelated employer `Mars`. RBC divisions
and programs, HP/HPE, employer/university-portal names, Oracle/Oracle Recruiting
Cloud, and Government of Canada Jobs/Job Bank Canada stay distinct.

Inspect the complete maintained inventory **offline**, without requests,
snapshot writes, or publication:

```text
python scrape.py --list-source-names
python -m unittest discover -s tests -p "test_source_names.py"
```

Each entry reports `name`, `status`, and existing `source_keys`. `configured`
means an existing task is registered, not that a source is currently live,
complete, verified, or credential-enabled. Names without a configured target
report `pending_configuration` and have no source keys. This includes generic
ATS-platform names even when an adapter already exists: an adapter is not an
approved employer tenant. Normal runs print the configured/pending summary.
After this follow-up, 80 of the 556 requested names match registered
targets and 476 remain pending. Of the original 178 task keys, 177 remain
unchanged and the obsolete Ontario Teachers key is explicitly replaced by its
verified official key above. Eleven employer feeds are additional, including
SaskPower and the ten boards above; the maintained task count is 189.
Pending names never create requests or enter source completeness/freshness
gates. They require separate public-source/terms/access verification and exact
configuration through the existing source-addition procedure above; no URLs,
tenants, credentials, adapters, or permissions are inferred from a name.

## Moving browser data

The Profile view includes **Export / Move my data**. From this release onward,
saved jobs and application statuses persist under
`internship_tracker_user_state_v1` and can be downloaded as versioned JSON.
The export excludes filters, onboarding flags, and other browser settings. See
[`docs/migration-contract.md`](docs/migration-contract.md) for the exact schema.
If stored migration data is malformed or from a future schema version, writes
pause and the raw value is preserved under
`internship_tracker_user_state_recovery_v1` rather than being overwritten.
