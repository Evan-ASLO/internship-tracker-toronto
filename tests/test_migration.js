const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const migration = require("../site/migration.js");

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
}

test("legacy export is URL-first, versioned, and contains only migration state", () => {
  const state = {
    schemaVersion: 1,
    savedJobs: [
      {
        url: "HTTPS://EXAMPLE.COM/jobs/42?b=2&a=1#apply",
        company: " Example Co ",
        title: " Finance   Intern ",
      },
    ],
    applications: [
      {
        url: "https://example.com/jobs/42?a=1&b=2",
        company: "Example Co",
        title: "Finance Intern",
        status: "interview",
      },
      {
        url: null,
        company: "Fallback Co",
        title: "Operations Intern",
        status: "applied",
      },
    ],
    it_onboarded_v1: "must not leak",
    filters: ["Finance"],
  };

  const exported = migration.createExport(state, new Date("2026-10-01T16:00:00Z"));

  assert.deepEqual(exported, {
    schemaVersion: 1,
    exportedAt: "2026-10-01T16:00:00.000Z",
    source: "internship-tracker-toronto",
    savedJobs: [
      {
        url: "https://example.com/jobs/42?a=1&b=2",
        company: "Example Co",
        title: "Finance Intern",
      },
    ],
    applications: [
      {
        url: null,
        company: "Fallback Co",
        title: "Operations Intern",
        status: "applied",
      },
      {
        url: "https://example.com/jobs/42?a=1&b=2",
        company: "Example Co",
        title: "Finance Intern",
        status: "interview",
      },
    ],
  });
  assert.equal(JSON.stringify(exported).includes("onboarded"), false);
  assert.equal(JSON.stringify(exported).includes("filters"), false);
});

test("state persists under one versioned key and round-trips", () => {
  const storage = memoryStorage();
  const state = {
    savedJobs: [{ url: "https://example.com/jobs/1", company: "A", title: "Intern" }],
    applications: [
      {
        url: "https://example.com/jobs/2",
        company: "B",
        title: "Analyst Intern",
        status: "offer",
      },
    ],
  };

  migration.writeState(storage, state);
  const loaded = migration.readState(storage);

  assert.equal(storage.values.size, 1);
  assert.equal(storage.values.has("internship_tracker_user_state_v1"), true);
  assert.equal(loaded.error, null);
  assert.equal(loaded.writeBlocked, false);
  assert.deepEqual(loaded.state, migration.normalizeState(state));
});

test("job references never use unstable array-index identifiers", () => {
  const ref = migration.referenceFromJob({
    id: "L17",
    url: "https://example.com/jobs/17",
    company: "Example",
    title: "Marketing Intern",
  });

  assert.deepEqual(ref, {
    url: "https://example.com/jobs/17",
    company: "Example",
    title: "Marketing Intern",
  });
  assert.equal(migration.referenceKey(ref), "url:https://example.com/jobs/17");
});

test("every current live job has a unique URL-first browser key", () => {
  const snapshot = JSON.parse(fs.readFileSync("live_jobs.json", "utf8"));
  const keys = snapshot.jobs.map((job) =>
    migration.referenceKey({
      url: job.url,
      company: job.company,
      title: job.title,
    })
  );

  assert.equal(new Set(keys).size, snapshot.jobs.length);
  assert.equal(keys.some((key) => /^L\d+$/.test(key)), false);
});

test("malformed legacy rows are isolated instead of blanking the board", () => {
  const good = {
    company: "Example",
    title: "Finance Intern",
    loc: "Toronto",
    geo: "Toronto",
    cats: [],
    url: "https://example.com/jobs/1",
    posted: "Posted Today",
    src: "greenhouse",
  };
  const result = migration.normalizeLegacyJobs(
    [
      good,
      { ...good, url: "/relative/jobs/2" },
      { ...good, cats: "Finance" },
      null,
    ],
    () => ["Finance"]
  );

  assert.equal(result.jobs.length, 1);
  assert.equal(result.dropped.length, 3);
  assert.equal(result.jobs[0].title, "Finance Intern");
  assert.deepEqual(result.jobs[0].cats, ["Finance"]);
});

test("legacy Adzuna rows preserve the source marker for attribution", () => {
  const result = migration.normalizeLegacyJobs(
    [
      {
        company: "Example",
        title: "Finance Intern",
        loc: "Toronto, Ontario",
        geo: "Toronto",
        cats: ["Finance"],
        url: "https://www.adzuna.ca/details/123",
        posted: "2026-10-01T12:00:00Z",
        src: "adzuna",
      },
    ],
    () => []
  );

  assert.equal(result.jobs.length, 1);
  assert.equal(result.jobs[0].src, "adzuna");
});

test("future-version state is backed up and never overwritten by the next write", () => {
  const raw = JSON.stringify({
    schemaVersion: 2,
    savedJobs: [{ url: "https://example.com/future", company: "A", title: "Future" }],
  });
  const storage = memoryStorage({ [migration.STORAGE_KEY]: raw });

  const loaded = migration.readState(storage);
  assert.equal(loaded.writeBlocked, true);
  assert.match(loaded.error, /Unsupported migration state schemaVersion/);
  assert.throws(
    () => migration.writeState(storage, migration.emptyState()),
    /cannot be updated safely/
  );
  assert.equal(storage.getItem(migration.STORAGE_KEY), raw);
  assert.equal(storage.getItem(migration.BACKUP_KEY), raw);
});

test("malformed stored JSON is preserved before writes are refused", () => {
  const raw = '{"schemaVersion":1,"savedJobs":[';
  const storage = memoryStorage({ [migration.STORAGE_KEY]: raw });

  const loaded = migration.readState(storage);
  assert.equal(loaded.writeBlocked, true);
  assert.throws(
    () =>
      migration.writeState(storage, {
        savedJobs: [{ url: "https://example.com/new", company: "N", title: "New" }],
        applications: [],
      }),
    /cannot be updated safely/
  );
  assert.equal(storage.getItem(migration.STORAGE_KEY), raw);
  assert.equal(storage.getItem(migration.BACKUP_KEY), raw);
});
