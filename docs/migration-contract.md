# Cloudflare dual-publish migration contract

## Normalized snapshot ingestion

`publish_snapshot.py` reads the verified `live_jobs.json` only after the scraper
has completed its per-URL liveness checks. It POSTs compact UTF-8 JSON to the
full URL stored in `CLOUDFLARE_JOBS_INGEST_URL`:

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-10-01T16:00:00Z",
  "snapshotHash": "64 lowercase hex characters",
  "jobs": []
}
```

`jobs` is sorted by `id`. `snapshotHash` is lowercase SHA-256 over the
canonical jobs-array JSON only: keys sorted, compact separators, UTF-8. It does
not include `generatedAt`, so identical job content has an identical hash.

Each normalized job has exactly:

```json
{
  "id": "64 lowercase hex characters",
  "externalId": "provider ID or null",
  "source": "greenhouse",
  "company": "d2l",
  "title": "Finance Intern",
  "location": "Toronto, ON",
  "region": "Toronto",
  "categories": ["Finance"],
  "url": "https://example.com/jobs/123",
  "postedAt": "2026-09-30T14:00:00Z",
  "postedText": "2026-09-30T10:00:00-04:00",
  "description": "Provider-supplied description snippet or null"
}
```

Identity rules:

1. When a reliable provider external ID exists, `id` is
   `sha256(source + "\n" + externalId)`.
2. Otherwise, when a canonical URL exists, `id` is
   `sha256(source + "\n" + canonicalUrl)`.
3. Only when no URL exists, the fallback input is normalized
   source/company/title/location.

Provider identity details:

- Greenhouse API IDs are used directly. When a URL also exposes `gh_jid` or a
  `/jobs/<id>` path, normalization fails if it disagrees with the API ID.
- Workday title-bearing URLs are parsed for their terminal requisition token
  and namespaced by tenant, for example `bmo:R260026123-1`.
- SuccessFactors terminal numeric requisitions are namespaced by host, for
  example `jobs.scotiabank.com:605898317`.
- Unrecognized Workday/SuccessFactors URL shapes still fall back to the full
  canonical URL. Those records can orphan saved/application references if the
  provider later edits the title-bearing URL. Tenant/host migrations can also
  create a new identity and require reconciliation.

All IDs and hashes are full 64-character lowercase hexadecimal strings.
Relative provider dates such as `Posted Today` remain in `postedText`;
`postedAt` is populated only for parseable absolute timestamps.

Cloudflare v1 field caps are enforced before publication:

| Field | Cap | Normalizer behavior |
|---|---:|---|
| `externalId` | 240 | Reject; identity-critical and never truncated |
| `source` | 80 | Reject; identity-critical and never truncated |
| `company` | 200 | Truncate display value after identity calculation |
| `title` | 240 | Truncate display value after identity calculation |
| `location` | 240 | Truncate display value after identity calculation |
| `region` | 120 | Truncate display value |
| `categories` | 24 | Sort/dedupe, then keep the first 24 |
| each category | 80 | Truncate display value |
| `url` | 2048 | Reject; identity-critical and never truncated |
| `postedText` | 240 | Parse `postedAt` first, then truncate display value |
| `description` | 12000 | Truncate display value |

When URL and external ID are absent, the fallback ID is calculated from the
full normalized source/company/title/location values before display-field
truncation, so truncation cannot collapse fallback identities.

The request includes:

```text
X-Ingest-Timestamp: <Unix epoch seconds>
X-Ingest-Signature: <lowercase hex HMAC-SHA256>
```

The signature input is exactly:

```text
${timestamp}.${rawBody}
```

using `CLOUDFLARE_JOBS_INGEST_SECRET`. The publisher rejects more than 1,000
jobs or a body larger than 2 MiB before making a request. Secrets and signature
inputs are never logged.

## Workflow ordering

The hourly/manual workflow preserves this order:

1. Capture the checked-in snapshot as a prior baseline.
2. Scrape and verify every posting URL.
3. Compare the new snapshot with the live Vercel site and separately detect
   whether git snapshots changed.
4. Compare normalized counts against the higher of the checked-in and live
   baselines. A drop greater than 25% blocks both publication paths unless a
   manually dispatched run sets `allow_catalog_drop`.
5. Attempt Cloudflare ingest only when the repository Actions variable
   `CLOUDFLARE_DUAL_PUBLISH` is exactly `true`.
6. Attempt the existing Vercel production deployment when live drift exists or
   `force_deploy` is set, even if an enabled Cloudflare ingest failed.
7. Fail the publication gate if the catalog guard, enabled Cloudflare ingest,
   or required Vercel deployment failed.
8. Commit and push refreshed snapshots only after that gate succeeds.

This retains the live-site comparison and force-deploy path and does not remove
or cut over the Vercel deployment. With Cloudflare disabled, its skipped step
is non-failing and the prior Vercel deploy/commit behavior remains active.

## Browser persistence and export

Before this migration, saved jobs and tracker columns existed only in memory;
there were no saved/application localStorage keys. From this release onward,
the old site stores only migration-relevant state in:

```text
internship_tracker_user_state_v1
```

The stored value is:

```json
{
  "schemaVersion": 1,
  "savedJobs": [
    {
      "url": "https://example.com/jobs/123",
      "company": "Example Co",
      "title": "Finance Intern"
    }
  ],
  "applications": [
    {
      "url": "https://example.com/jobs/456",
      "company": "Example Co",
      "title": "Operations Intern",
      "status": "interview"
    }
  ]
}
```

The **Export / Move my data** action downloads:

```json
{
  "schemaVersion": 1,
  "exportedAt": "2026-10-01T16:00:00.000Z",
  "source": "internship-tracker-toronto",
  "savedJobs": [],
  "applications": []
}
```

Each `savedJobs` entry has `url`, `company`, and `title`. Each `applications`
entry has those fields plus one of `applied`, `interview`, `offer`, or
`rejected`. URL is the primary match key; company/title are fallback metadata.
The receiving app maps statuses as follows:

| Legacy export | New app |
|---|---|
| `applied` | `APPLIED` |
| `interview` | `INTERVIEWING` |
| `offer` | `OFFER` |
| `rejected` | `REJECTED` |

The export does not read or include `it_onboarded_v1`,
`it_seen_landing_v1`, filters, or presentation state.

If the primary value is malformed or has a future `schemaVersion`, the site
does not write an empty replacement. Automatic writes pause, the original
primary value remains untouched, and a raw copy is stored (when browser storage
permits) under:

```text
internship_tracker_user_state_recovery_v1
```

The Profile view surfaces the failure and recovery key. Separately, malformed
rows in `live_jobs.json` are skipped per row; valid rows continue to render and
the board displays a warning with the number skipped.

## Adzuna source attribution contract

Adzuna legacy feed rows use:

```json
{
  "src": "adzuna",
  "external_id": "<Adzuna result id>"
}
```

Normalization preserves the exact Cloudflare v1 marker:

```json
{
  "source": "adzuna",
  "externalId": "<Adzuna result id>"
}
```

Every UI surface that displays such a job must render linked **Jobs by Adzuna**
attribution at least 116 × 23 px, with the Adzuna word represented by the
official logo image and linked to the relevant local domain
(`https://www.adzuna.ca/` for this Canada feed). This includes browse cards,
job details, saved jobs, and application/tracker rows.

The Vercel app uses the official developer-portal PNG bundled at
`site/assets/adzuna-logo.png`, downloaded from the first-party developer portal
at `https://developer.adzuna.com/images/adzuna_logo.png`. The **Jobs by** text
and the logo are separate links to `https://www.adzuna.ca/`. The Cloudflare UI
must implement the same conditional behavior using
`job.source === "adzuna"` before the repository secrets are enabled. The
browser saved/application export schema remains unchanged and intentionally
carries URL/company/title migration references, not provider presentation
metadata.
