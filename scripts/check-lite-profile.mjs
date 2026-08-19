#!/usr/bin/env node
/**
 * Validate the documented low-resource profile against package.json without
 * installing dependencies or starting the gateway.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const scripts = manifest.scripts ?? {};

const requirements = {
  "gateway:dev": "OPENCLAW_SKIP_CHANNELS=1",
  "gateway:dev:reset": "OPENCLAW_SKIP_CHANNELS=1",
  "test:startup:memory": "node scripts/check-cli-startup-memory.mjs",
  "test:perf:budget": "node scripts/test-perf-budget.mjs",
  "test:perf:hotspots": "node scripts/test-hotspots.mjs",
};

const missing = Object.entries(requirements)
  .filter(([name, fragment]) => !scripts[name]?.includes(fragment))
  .map(([name]) => name);

if (missing.length > 0) {
  console.error(`lite profile check failed; missing or changed scripts: ${missing.join(", ")}`);
  process.exit(1);
}

console.log("RCclaw lite profile checks passed");
