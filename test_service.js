"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");

const modules = fs
  .readdirSync(__dirname)
  .filter((name) => name.endsWith("_contract.py"))
  .map((name) => name.replace(/\.py$/, ""));

const result = spawnSync("python3", ["-m", "unittest", "-v", ...modules], {
  stdio: "inherit",
});
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
