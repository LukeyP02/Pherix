/*
 * Test-only Node harness for the JS verdict mirror.
 *
 * Usage: node governance_js_runner.cjs <policy-eval.js path> <cases.json path>
 *
 * Reads a battery of {spec, scenario, world} cases, runs PolicyEval.preview on
 * each, and prints a compact comparable structure to stdout for
 * tests/test_governance_js_conformance.py to diff against the Python engine.
 */
"use strict";
const fs = require("fs");

const evalPath = process.argv[2];
const casesPath = process.argv[3];

const PolicyEval = require(evalPath);
const cases = JSON.parse(fs.readFileSync(casesPath, "utf8"));

const out = cases.map(function (c) {
  const r = PolicyEval.preview(c.spec, c.scenario, c.world || []);
  return {
    rows: r.rows.map(function (row) {
      return [row.index, row.tool, row.disposition];
    }),
    verdicts: r.verdicts.map(function (v) {
      return [v.effect_index, v.allow, v.rule_name];
    }),
    is_clean: r.is_clean,
  };
});

process.stdout.write(JSON.stringify(out));
