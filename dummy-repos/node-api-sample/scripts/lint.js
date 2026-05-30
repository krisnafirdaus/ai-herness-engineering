"use strict";

// Dependency-free linter so the `lint` check needs no `npm install`.
// Flags: trailing whitespace, tab indentation, legacy declarations, long lines.

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const DIRS = ["src", "scripts", "test"];
const MAX_LEN = 120;
const violations = [];

function walk(dir) {
  if (!fs.existsSync(dir)) return;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else if (entry.name.endsWith(".js")) lintFile(full);
  }
}

function lintFile(file) {
  const rel = path.relative(ROOT, file);
  const lines = fs.readFileSync(file, "utf8").split("\n");
  lines.forEach((line, i) => {
    const n = i + 1;
    if (/\s+$/.test(line)) violations.push(`${rel}:${n}: trailing whitespace`);
    if (/\t/.test(line)) violations.push(`${rel}:${n}: tab character`);
    if (/(^|[^\w.])var\s+[\w$]/.test(line)) {
      violations.push(`${rel}:${n}: use let/const, not legacy var`);
    }
    if (line.length > MAX_LEN) violations.push(`${rel}:${n}: line too long (${line.length} > ${MAX_LEN})`);
  });
}

DIRS.forEach((d) => walk(path.join(ROOT, d)));

if (violations.length) {
  console.error("LINT FAILED:");
  violations.forEach((v) => console.error("  " + v));
  process.exit(1);
}
console.log("lint ok");
