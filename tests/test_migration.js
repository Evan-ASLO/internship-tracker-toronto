const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const migration = require("../site/migration.js");

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
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
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

  assert.equal(values.size, 1);
  assert.equal(values.has("internship_tracker_user_state_v1"), true);
  assert.equal(loaded.error, null);
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
