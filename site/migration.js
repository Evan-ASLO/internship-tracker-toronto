(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.InternshipTrackerMigration = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SCHEMA_VERSION = 1;
  const STORAGE_KEY = "internship_tracker_user_state_v1";
  const BACKUP_KEY = "internship_tracker_user_state_recovery_v1";
  const SOURCE = "internship-tracker-toronto";
  const STATUSES = new Set(["applied", "interview", "offer", "rejected"]);

  function clean(value) {
    return value == null ? "" : String(value).trim().replace(/\s+/g, " ");
  }

  function compareStrings(a, b) {
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function canonicalUrl(value) {
    const raw = clean(value);
    if (!raw) return null;
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new Error("Job URL must use HTTP or HTTPS");
    }
    url.hash = "";
    url.hostname = url.hostname.toLowerCase();
    const sorted = [...url.searchParams.entries()].sort(([ak, av], [bk, bv]) =>
      ak === bk ? compareStrings(av, bv) : compareStrings(ak, bk)
    );
    url.search = "";
    sorted.forEach(([key, val]) => url.searchParams.append(key, val));
    return url.toString();
  }

  function normalizeReference(value) {
    const ref = value || {};
    const url = canonicalUrl(ref.url);
    const company = clean(ref.company);
    const title = clean(ref.title);
    if (!url && (!company || !title)) {
      throw new Error("A job reference needs a URL or company/title fallback");
    }
    return { url, company, title };
  }

  function referenceFromJob(job) {
    if (!job || job.sample) return null;
    return normalizeReference(job);
  }

  function referenceKey(value) {
    const ref = normalizeReference(value);
    if (ref.url) return `url:${ref.url}`;
    return `meta:${ref.company.toLowerCase()}\n${ref.title.toLowerCase()}`;
  }

  function normalizeLegacyJob(value, classify) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("Legacy job row must be an object");
    }

    const title = clean(value.title);
    const company = clean(value.company);
    const source = clean(value.src);
    if (!title || !company || !source) {
      throw new Error("Legacy job row is missing title, company, or source");
    }
    if (value.cats != null && !Array.isArray(value.cats)) {
      throw new Error("Legacy job categories must be an array");
    }

    const ref = normalizeReference({ url: value.url, company, title });
    if (!ref.url) throw new Error("Legacy live job needs an absolute URL");
    const suppliedCategories = (value.cats || []).map(clean).filter(Boolean);
    const inferredCategories =
      suppliedCategories.length || typeof classify !== "function" ? [] : classify(title);
    const categories = [...new Set(
      (suppliedCategories.length ? suppliedCategories : inferredCategories)
        .map(clean)
        .filter(Boolean)
    )];

    return {
      id: referenceKey(ref),
      title,
      company,
      loc: clean(value.loc),
      geo: clean(value.geo),
      cats: categories,
      url: ref.url,
      posted: clean(value.posted) || null,
      src: source,
      sample: false,
    };
  }

  function normalizeLegacyJobs(values, classify) {
    if (!Array.isArray(values)) throw new Error("Legacy jobs must be an array");
    const jobs = [];
    const dropped = [];
    values.forEach((value, index) => {
      try {
        jobs.push(normalizeLegacyJob(value, classify));
      } catch (error) {
        dropped.push({
          index,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    });
    return { jobs, dropped };
  }

  function dedupeReferences(values) {
    const refs = new Map();
    (Array.isArray(values) ? values : []).forEach((value) => {
      const ref = normalizeReference(value);
      refs.set(referenceKey(ref), ref);
    });
    return [...refs.values()].sort((a, b) => compareStrings(referenceKey(a), referenceKey(b)));
  }

  function normalizeState(value) {
    const raw = value || {};
    if (raw.schemaVersion != null && raw.schemaVersion !== SCHEMA_VERSION) {
      throw new Error(`Unsupported migration state schemaVersion: ${raw.schemaVersion}`);
    }

    const applications = new Map();
    (Array.isArray(raw.applications) ? raw.applications : []).forEach((value) => {
      const ref = normalizeReference(value);
      const status = clean(value.status).toLowerCase();
      if (!STATUSES.has(status)) throw new Error(`Unsupported application status: ${status}`);
      applications.set(referenceKey(ref), { ...ref, status });
    });

    return {
      schemaVersion: SCHEMA_VERSION,
      savedJobs: dedupeReferences(raw.savedJobs),
      applications: [...applications.values()].sort((a, b) =>
        compareStrings(referenceKey(a), referenceKey(b))
      ),
    };
  }

  function emptyState() {
    return { schemaVersion: SCHEMA_VERSION, savedJobs: [], applications: [] };
  }

  function readState(storage) {
    let raw = null;
    try {
      raw = storage.getItem(STORAGE_KEY);
      if (!raw) {
        return { state: emptyState(), error: null, raw: null, writeBlocked: false };
      }
      return {
        state: normalizeState(JSON.parse(raw)),
        error: null,
        raw,
        writeBlocked: false,
      };
    } catch (error) {
      return {
        state: emptyState(),
        error: error instanceof Error ? error.message : String(error),
        raw,
        writeBlocked: true,
      };
    }
  }

  function backupRawState(storage, raw) {
    if (raw == null) return null;
    if (storage.getItem(BACKUP_KEY) == null) storage.setItem(BACKUP_KEY, raw);
    return BACKUP_KEY;
  }

  function writeState(storage, value) {
    const existing = readState(storage);
    if (existing.writeBlocked) {
      let backupMessage = "";
      try {
        const backupKey = backupRawState(storage, existing.raw);
        if (backupKey) backupMessage = ` Raw data was preserved in ${backupKey}.`;
      } catch (error) {
        backupMessage = " Raw data could not be copied, so the original was left untouched.";
      }
      throw new Error(`Stored migration state cannot be updated safely.${backupMessage}`);
    }
    const state = normalizeState(value);
    storage.setItem(STORAGE_KEY, JSON.stringify(state));
    return state;
  }

  function createExport(value, now) {
    const state = normalizeState(value);
    const date = now instanceof Date ? now : new Date(now == null ? Date.now() : now);
    if (Number.isNaN(date.getTime())) throw new Error("Export time is invalid");
    return {
      schemaVersion: SCHEMA_VERSION,
      exportedAt: date.toISOString(),
      source: SOURCE,
      savedJobs: state.savedJobs,
      applications: state.applications,
    };
  }

  return {
    SCHEMA_VERSION,
    STORAGE_KEY,
    BACKUP_KEY,
    SOURCE,
    STATUSES: [...STATUSES],
    canonicalUrl,
    normalizeReference,
    referenceFromJob,
    referenceKey,
    normalizeLegacyJob,
    normalizeLegacyJobs,
    normalizeState,
    emptyState,
    readState,
    backupRawState,
    writeState,
    createExport,
  };
});
