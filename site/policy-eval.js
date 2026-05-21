/*
 * policy-eval.js — the browser's verdict mirror of pherix.core.policy.
 *
 * This is a faithful re-implementation of Policy.collect_verdicts (the
 * commit-time capture walk) so the governance UI can show live verdicts
 * client-side without a Python round-trip. It is NOT the source of truth — the
 * Python engine is. The two are pinned identical by
 * tests/test_governance_js_conformance.py, which runs this file under Node over
 * a battery of (spec, scenario, world) cases and asserts verdict-for-verdict
 * equality with pherix.governance.preview.preview. If you change a rule here,
 * change its Python twin and re-run that test — drift is a test failure.
 *
 * Semantics mirrored, in order, per effect (try_evaluate):
 *   1. allow/deny tool-name lists (deny wins; allow=null means allow-all)
 *   2. registered rules (one verdict each)
 *   3. caps (one verdict each; accumulate the running total only on Allow)
 * collect_verdicts resets the per-cap running totals, then folds forward over
 * the ordered journal. Disposition precedence: deny > cap > gate > allow.
 */
(function (root) {
  "use strict";

  // Canonical world-map key — must match preview._canon in Python:
  // json.dumps([resource, key], separators=(",", ":")) === JSON.stringify here
  // (no whitespace, arrays not tuples).
  function canon(resource, key) {
    return JSON.stringify([resource, key]);
  }

  function makeReader(world) {
    var table = {};
    (world || []).forEach(function (e) {
      table[canon(e.resource, e.key)] = e.value;
    });
    return function (resource, key) {
      var k = canon(resource, key);
      return Object.prototype.hasOwnProperty.call(table, k) ? table[k] : null;
    };
  }

  // -- rule templates: twins of pherix.governance.templates ------------------

  function refundIfPaid(p) {
    var tool = p.tool !== undefined ? p.tool : "refund_order";
    var table = p.table !== undefined ? p.table : "orders";
    var idArg = p.id_arg !== undefined ? p.id_arg : "order_id";
    var pkCol = p.pk_column !== undefined ? p.pk_column : "id";
    var statusCol = p.status_column !== undefined ? p.status_column : "status";
    var paid = p.paid_value !== undefined ? p.paid_value : "paid";
    var resource = p.resource !== undefined ? p.resource : "sql";

    var fn = function (effect, ctx) {
      if (effect.tool !== tool) return { allow: true };
      if (!(idArg in effect.args)) {
        return { allow: false, reason: "missing id arg" };
      }
      var orderId = effect.args[idArg];
      var live = ctx.read(resource, [table, pkCol, orderId, statusCol]);
      if (live !== paid) {
        return { allow: false, reason: "order not " + paid };
      }
      return { allow: true };
    };
    fn.ruleName = "refund_if_paid(" + tool + ")";
    return fn;
  }

  function argEqualsDenied(p) {
    var tool = p.tool;
    var arg = p.arg;
    var value = p.value !== undefined ? p.value : null;

    var fn = function (effect, ctx) {
      if (effect.tool !== tool) return { allow: true };
      if (!(arg in effect.args)) return { allow: true };
      if (value === null) return { allow: false, reason: "arg present" };
      if (effect.args[arg] === value) return { allow: false, reason: "arg matches" };
      return { allow: true };
    };
    fn.ruleName = "arg_equals_denied(" + tool + "." + arg + ")";
    return fn;
  }

  var TEMPLATES = {
    refund_if_paid: refundIfPaid,
    arg_equals_denied: argEqualsDenied,
  };

  // -- caps: twins of _CountCap / _SumCap ------------------------------------

  function countCap(tool, max) {
    return {
      ruleName: "Cap.count(tool=" + pyRepr(tool) + ", max=" + max + ")",
      appliesTo: function (e) {
        return e.tool === tool;
      },
      contribution: function () {
        return 1;
      },
      evaluate: function (e, running) {
        if (e.tool !== tool) return { allow: true };
        if (running + 1 > max) return { allow: false, reason: "count cap" };
        return { allow: true };
      },
    };
  }

  function sumCap(tool, field, max) {
    return {
      ruleName: "Cap.sum(tool=" + pyRepr(tool) + ", max=" + max + ")",
      appliesTo: function (e) {
        return e.tool === tool;
      },
      contribution: function (e) {
        var v = e.args[field];
        var n = parseFloat(v);
        return isNaN(n) ? 0 : n;
      },
      evaluate: function (e, running) {
        if (e.tool !== tool) return { allow: true };
        var cand = running + this.contribution(e);
        if (cand > max) return { allow: false, reason: "sum cap" };
        return { allow: true };
      },
    };
  }

  // Python repr() of a string uses single quotes — match it for rule_name parity.
  function pyRepr(s) {
    return "'" + String(s) + "'";
  }

  function buildCaps(spec) {
    return (spec.caps || []).map(function (c) {
      return c.kind === "count"
        ? countCap(c.tool, c.max)
        : sumCap(c.tool, c.field, c.max);
    });
  }

  function buildRules(spec) {
    return (spec.rules || []).map(function (r) {
      var factory = TEMPLATES[r.template];
      if (!factory) throw new Error("unknown rule template: " + r.template);
      return factory(r.params || {});
    });
  }

  // -- the fold --------------------------------------------------------------

  function preview(spec, scenario, world, gateOverride) {
    var read = makeReader(world);
    var ctx = { read: read, where: "commit" };
    var rules = buildRules(spec);
    var caps = buildCaps(spec);
    var allow = spec.allow === undefined ? null : spec.allow; // null = allow-all
    var deny = spec.deny || [];
    var gate =
      gateOverride !== undefined && gateOverride !== null
        ? gateOverride
        : spec.gate_irreversibles !== false;

    var capTotals = caps.map(function () {
      return 0;
    });

    var verdicts = [];
    var rows = [];

    scenario.forEach(function (s, index) {
      var effect = {
        tool: s.tool,
        args: s.args || {},
        reversible: s.reversible !== false,
        compensator: s.compensator !== undefined ? s.compensator : null,
      };
      var effVerdicts = [];

      // 1. allow/deny.
      if (deny.indexOf(effect.tool) !== -1) {
        effVerdicts.push({
          effect_index: index,
          allow: false,
          rule_name: null,
          reason: "tool is deny-listed",
        });
      } else if (allow !== null && allow.indexOf(effect.tool) === -1) {
        effVerdicts.push({
          effect_index: index,
          allow: false,
          rule_name: null,
          reason: "tool is not in the allow-list",
        });
      }

      // 2. rules.
      rules.forEach(function (rule) {
        var v = rule(effect, ctx);
        effVerdicts.push({
          effect_index: index,
          allow: v.allow,
          rule_name: rule.ruleName,
          reason: v.allow ? null : v.reason || null,
        });
      });

      // 3. caps — accumulate only on allow.
      caps.forEach(function (cap, ci) {
        var v = cap.evaluate(effect, capTotals[ci]);
        effVerdicts.push({
          effect_index: index,
          allow: v.allow,
          rule_name: cap.ruleName,
          reason: v.allow ? null : v.reason || null,
        });
        if (v.allow && cap.appliesTo(effect)) {
          capTotals[ci] += cap.contribution(effect);
        }
      });

      // disposition.
      var denies = effVerdicts.filter(function (v) {
        return !v.allow;
      });
      var capNames = caps.map(function (c) {
        return c.ruleName;
      });
      var nonCap = denies.filter(function (v) {
        return capNames.indexOf(v.rule_name) === -1;
      });
      var capDenies = denies.filter(function (v) {
        return capNames.indexOf(v.rule_name) !== -1;
      });

      var disposition, reasons;
      if (nonCap.length) {
        disposition = "deny";
        reasons = nonCap.map(function (v) {
          return v.reason || "denied";
        });
      } else if (capDenies.length) {
        disposition = "cap";
        reasons = capDenies.map(function (v) {
          return v.reason || "cap exceeded";
        });
      } else if (gate && !effect.reversible && effect.compensator === null) {
        disposition = "gate";
        reasons = ["irreversible, no compensator — blocks at commit"];
      } else {
        disposition = "allow";
        reasons = [];
      }

      rows.push({
        index: index,
        tool: effect.tool,
        disposition: disposition,
        reasons: reasons,
      });
      effVerdicts.forEach(function (v) {
        verdicts.push(v);
      });
    });

    var counts = { allow: 0, deny: 0, cap: 0, gate: 0 };
    rows.forEach(function (r) {
      counts[r.disposition] += 1;
    });

    return {
      rows: rows,
      verdicts: verdicts,
      is_clean: verdicts.every(function (v) {
        return v.allow;
      }),
      counts: counts,
    };
  }

  var api = { preview: preview, TEMPLATES: TEMPLATES };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api; // Node (CommonJS) — used by the conformance test.
  }
  root.PolicyEval = api; // Browser — used by governance.html.
})(typeof window !== "undefined" ? window : globalThis);
