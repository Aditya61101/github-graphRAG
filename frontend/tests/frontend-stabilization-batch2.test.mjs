import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { createServer } from "vite";

let server, guidance;
before(async () => {
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  guidance = await server.ssrLoadModule("/src/lib/adr-guidance.ts");
});
after(async () => {
  await server?.close();
});
const storage = () => {
  const data = new Map();
  const writes = [];
  return {
    data,
    writes,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
      writes.push({ key, value });
    },
  };
};

test("versioned guidance keys safely scope identifiers without credentials", () => {
  assert.equal(
    guidance.adrGuidanceKey("user:alice/a", "repo_abc-1"),
    "decisionguard:adr-guidance:v1:user%3Aalice%2Fa:repo_abc-1"
  );
  assert.equal(guidance.adrGuidanceKey("", "repo_a"), null);
  assert.equal(guidance.adrGuidanceKey("alice", "../repo_a"), null);
  assert.notEqual(
    guidance.adrGuidanceKey("alice", "repo_a"),
    guidance.adrGuidanceKey("bob", "repo_a")
  );
  assert.notEqual(
    guidance.adrGuidanceKey("alice", "repo_a"),
    guidance.adrGuidanceKey("alice", "repo_b")
  );
});

test("reading eligibility does not write; presentation claims persist immediately exactly once", () => {
  const target = storage();
  const key = guidance.adrGuidanceKey("claim-once", "repo_a");
  assert.equal(guidance.hasSeenADRGuidance(key, target), false);
  assert.equal(target.writes.length, 0);
  assert.equal(guidance.claimADRGuidance(key, target), true);
  assert.deepEqual(target.writes, [{ key, value: "shown" }]);
  assert.equal(guidance.claimADRGuidance(key, target), false);
  assert.equal(target.writes.length, 1);
  assert.equal(guidance.hasSeenADRGuidance(key, target), true);
});

test("an existing persisted marker suppresses presentation without another write", () => {
  const target = storage();
  const key = guidance.adrGuidanceKey("persisted", "repo_a");
  target.data.set(key, "shown");
  assert.equal(guidance.claimADRGuidance(key, target), false);
  assert.equal(target.writes.length, 0);
});

test("storage exceptions fall back to one claim per runtime and do not break the page", () => {
  const key = guidance.adrGuidanceKey("blocked-storage", "repo_a");
  const target = {
    getItem() {
      throw new Error("blocked");
    },
    setItem() {
      throw new Error("quota");
    },
  };
  assert.equal(guidance.hasSeenADRGuidance(key, target), false);
  assert.equal(guidance.claimADRGuidance(key, target), true);
  assert.equal(guidance.claimADRGuidance(key, target), false);
});

test("guidance requires completed indexing, successful current empty inventory, permission, and no deletion", () => {
  const eligible = {
    status: "COMPLETED",
    canManage: true,
    inventory: [],
    succeeded: true,
    fetching: false,
    deletionEpoch: 0,
    cleanup: false,
  };
  assert.equal(guidance.isADRGuidanceEligible(eligible), true);
  for (const status of [
    "PENDING",
    "PROCESSING",
    "INDEXING",
    "FAILED",
    "UNKNOWN",
    "",
  ]) {
    assert.equal(
      guidance.isADRGuidanceEligible({ ...eligible, status }),
      false
    );
  }
  for (const status of ["PENDING", "PROCESSING", "COMPLETED", "FAILED"]) {
    assert.equal(
      guidance.isADRGuidanceEligible({
        ...eligible,
        inventory: [{ id: "a", status }],
      }),
      false
    );
  }
  for (const override of [
    { canManage: false },
    { inventory: undefined },
    { succeeded: false },
    { fetching: true },
    { deletionEpoch: 1 },
    { cleanup: true },
  ]) {
    assert.equal(
      guidance.isADRGuidanceEligible({ ...eligible, ...override }),
      false
    );
  }
});
