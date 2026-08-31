import assert from "node:assert/strict";
import test from "node:test";

import worker, { dispatchWorkflow } from "../src/index.js";

test("fetch returns health status without dispatching", async () => {
  let calls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { calls += 1; throw new Error("unexpected dispatch"); };
  try {
    const response = await worker.fetch(new Request("https://example.test"), {});
    assert.equal(await response.text(), "codex-window-primer-scheduler is running");
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("unconfigured public template fails closed before network access", async () => {
  let calls = 0;
  await assert.rejects(
    dispatchWorkflow({ GITHUB_TOKEN: "test-token" }, async () => { calls += 1; }),
    /Configure the private GitHub runtime repository/,
  );
  assert.equal(calls, 0);
});

test("source never logs a token or GitHub response body", async () => {
  const fs = await import("node:fs/promises");
  const source = await fs.readFile(new URL("../src/index.js", import.meta.url), "utf8");
  assert.equal(source.includes("console.log"), false);
  assert.equal(source.includes("response.text"), false);
});
