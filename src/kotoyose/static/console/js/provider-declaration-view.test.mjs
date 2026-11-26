import assert from "node:assert/strict";
import test from "node:test";

import { declarationSummary } from "./provider-declaration-view.js";

test("declaration summary keeps names, hashes, commands, and artifact URLs as text", () => {
  const summary = declarationSummary({
    id: "fake-cli",
    display_name: "Fake <cli>",
    publisher: "Example & Co",
    sha256: "ab".repeat(32),
    commands: [{ argv: ["fake-cli", "login"] }, { argv: ["fake-cli", "acp", "--model", "{model}"] }],
    artifacts: [{ url: "https://example.com/fake-cli", sha256: "sha256:" + "cd".repeat(32) }],
  });
  assert.equal(summary.title, "Fake <cli>");
  assert.equal(summary.publisher, "Example & Co");
  assert.deepEqual(summary.commands, ["fake-cli login", "fake-cli acp --model {model}"]);
  assert.deepEqual(summary.artifacts, ["https://example.com/fake-cli sha256:" + "cd".repeat(32)]);
});
