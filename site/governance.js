/*
 * governance.js — the builder controller.
 *
 * State-driven: `state` holds the policy spec, the sample journal, and the
 * world map. Any edit mutates `state` and calls render(), which (a) rebuilds the
 * dynamic editor rows and (b) re-runs PolicyEval.preview to refresh the live
 * verdicts and the exports. The preview path is identical to the Python engine
 * (pinned by test_governance_js_conformance); the JSON export loads via
 * pherix.governance.from_spec; the Python export mirrors spec.to_python.
 */
(function () {
  "use strict";

  // Starter library — mirrors pherix.governance.templates.STARTER_TEMPLATES.
  // The Python list is canonical; this is the browser copy a user loads from.
  var STARTERS = [
    {
      name: "spend-capped",
      description:
        "Let the agent run, but cap spend and side-effecting call count per txn.",
      allow: null,
      deny: [],
      caps: [
        { kind: "sum", tool: "charge", field: "amount", max: 1000 },
        { kind: "count", tool: "send_email", field: null, max: 5 },
      ],
      rules: [],
      gate_irreversibles: true,
    },
    {
      name: "read-only",
      description: "Allow only read tools; deny everything else.",
      allow: ["read_file", "sql_select", "http_get", "list_dir"],
      deny: [],
      caps: [],
      rules: [],
      gate_irreversibles: true,
    },
    {
      name: "approve-irreversibles",
      description:
        "Reversible work runs freely; uncompensable effects gate at commit.",
      allow: null,
      deny: [],
      caps: [],
      rules: [],
      gate_irreversibles: true,
    },
    {
      name: "refund-guarded",
      description: "Refund only if the order is 'paid' right now (TOCTOU-safe).",
      allow: null,
      deny: [],
      caps: [],
      rules: [
        {
          template: "refund_if_paid",
          params: {
            tool: "refund_order",
            table: "orders",
            id_arg: "order_id",
            pk_column: "id",
            status_column: "status",
            paid_value: "paid",
            resource: "sql",
          },
        },
      ],
      gate_irreversibles: true,
    },
  ];

  var TEMPLATE_NAMES = ["refund_if_paid", "arg_equals_denied"];
  var TEMPLATE_DEFAULTS = {
    refund_if_paid: {
      tool: "refund_order",
      table: "orders",
      id_arg: "order_id",
      pk_column: "id",
      status_column: "status",
      paid_value: "paid",
      resource: "sql",
    },
    arg_equals_denied: { tool: "update", arg: "tier", value: "enterprise" },
  };

  // -- state ----------------------------------------------------------------

  var state = {
    spec: deepCopy(STARTERS[0]),
    scenario: [
      { tool: "charge", args: { amount: 600 }, reversible: true, compensator: "refund" },
      { tool: "charge", args: { amount: 600 }, reversible: true, compensator: "refund" },
      { tool: "send_email", args: {}, reversible: false, compensator: null },
    ],
    world: [],
  };
  var exportTab = "json";

  function deepCopy(o) {
    return JSON.parse(JSON.stringify(o));
  }
  function $(id) {
    return document.getElementById(id);
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }
  function words(s) {
    return s.split(/\s+/).filter(Boolean);
  }
  function parseArgs(s) {
    if (!s.trim()) return {};
    try {
      return JSON.parse(s);
    } catch (e) {
      return { __invalid__: s };
    }
  }

  // -- render: static fields ------------------------------------------------

  function renderStarters() {
    var box = $("starters");
    STARTERS.forEach(function (s) {
      var b = el("button", "starter", s.name);
      b.title = s.description;
      b.onclick = function () {
        state.spec = deepCopy(s);
        render();
      };
      box.appendChild(b);
    });
  }

  function syncStaticInputs() {
    $("p-name").value = state.spec.name || "";
    $("p-desc").value = state.spec.description || "";
    $("p-allow").value = state.spec.allow ? state.spec.allow.join(" ") : "";
    $("p-deny").value = (state.spec.deny || []).join(" ");
    $("p-gate").checked = state.spec.gate_irreversibles !== false;
  }

  function wireStaticInputs() {
    $("p-name").oninput = function () {
      state.spec.name = this.value;
      refresh();
    };
    $("p-desc").oninput = function () {
      state.spec.description = this.value;
      refresh();
    };
    $("p-allow").oninput = function () {
      var w = words(this.value);
      state.spec.allow = w.length ? w : null;
      refresh();
    };
    $("p-deny").oninput = function () {
      state.spec.deny = words(this.value);
      refresh();
    };
    $("p-gate").onchange = function () {
      state.spec.gate_irreversibles = this.checked;
      refresh();
    };
  }

  // -- render: dynamic rows -------------------------------------------------

  function renderCaps() {
    var box = $("caps");
    box.innerHTML = "";
    state.spec.caps.forEach(function (cap, i) {
      var row = el("div", "row");

      var kind = el("select", "narrow");
      ["count", "sum"].forEach(function (k) {
        var o = el("option", null, k);
        o.value = k;
        if (cap.kind === k) o.selected = true;
        kind.appendChild(o);
      });
      kind.onchange = function () {
        cap.kind = this.value;
        if (cap.kind === "sum" && !cap.field) cap.field = "amount";
        render();
      };

      var tool = el("input");
      tool.type = "text";
      tool.placeholder = "tool";
      tool.value = cap.tool || "";
      tool.oninput = function () {
        cap.tool = this.value;
        refresh();
      };

      row.appendChild(kind);
      row.appendChild(tool);

      if (cap.kind === "sum") {
        var fld = el("input");
        fld.type = "text";
        fld.placeholder = "field";
        fld.value = cap.field || "";
        fld.oninput = function () {
          cap.field = this.value;
          refresh();
        };
        row.appendChild(fld);
      }

      var max = el("input", "narrow");
      max.type = "number";
      max.placeholder = "max";
      max.value = cap.max;
      max.oninput = function () {
        cap.max = Number(this.value);
        refresh();
      };
      row.appendChild(max);

      row.appendChild(removeBtn(state.spec.caps, i));
      box.appendChild(row);
    });
  }

  function renderRules() {
    var box = $("rules");
    box.innerHTML = "";
    state.spec.rules.forEach(function (rule, i) {
      var wrap = el("div");
      wrap.style.marginBottom = "0.55rem";

      var row = el("div", "row");
      var sel = el("select");
      TEMPLATE_NAMES.forEach(function (t) {
        var o = el("option", null, t);
        o.value = t;
        if (rule.template === t) o.selected = true;
        sel.appendChild(o);
      });
      sel.onchange = function () {
        rule.template = this.value;
        rule.params = deepCopy(TEMPLATE_DEFAULTS[this.value] || {});
        render();
      };
      row.appendChild(sel);
      row.appendChild(removeBtn(state.spec.rules, i));
      wrap.appendChild(row);

      var params = el("textarea");
      params.value = JSON.stringify(rule.params || {}, null, 0);
      params.placeholder = "params (JSON)";
      params.oninput = function () {
        try {
          rule.params = JSON.parse(this.value);
          this.style.borderColor = "";
        } catch (e) {
          this.style.borderColor = "var(--red)";
          return;
        }
        refresh();
      };
      wrap.appendChild(params);
      box.appendChild(wrap);
    });
  }

  function renderEffects() {
    var box = $("effects");
    box.innerHTML = "";
    state.scenario.forEach(function (s, i) {
      var row = el("div", "row");

      var tool = el("input");
      tool.type = "text";
      tool.placeholder = "tool";
      tool.value = s.tool || "";
      tool.oninput = function () {
        s.tool = this.value;
        refresh();
      };

      var args = el("input");
      args.type = "text";
      args.placeholder = '{"amount": 600}';
      args.value = Object.keys(s.args || {}).length ? JSON.stringify(s.args) : "";
      args.oninput = function () {
        s.args = parseArgs(this.value);
        refresh();
      };

      var rev = el("label", "check");
      rev.style.fontSize = "0.72rem";
      rev.style.color = "var(--ink-3)";
      var cb = el("input");
      cb.type = "checkbox";
      cb.checked = s.reversible !== false;
      cb.style.width = "auto";
      cb.onchange = function () {
        s.reversible = this.checked;
        refresh();
      };
      rev.appendChild(cb);
      rev.appendChild(document.createTextNode("rev"));

      var comp = el("input", "narrow");
      comp.type = "text";
      comp.placeholder = "comp";
      comp.title = "compensator name (blank = none)";
      comp.value = s.compensator || "";
      comp.oninput = function () {
        s.compensator = this.value || null;
        refresh();
      };

      row.appendChild(tool);
      row.appendChild(args);
      row.appendChild(rev);
      row.appendChild(comp);
      row.appendChild(removeBtn(state.scenario, i));
      box.appendChild(row);
    });
  }

  function renderWorld() {
    var box = $("world");
    box.innerHTML = "";
    state.world.forEach(function (w, i) {
      var row = el("div", "row");

      var res = el("input", "narrow");
      res.type = "text";
      res.placeholder = "sql";
      res.value = w.resource || "";
      res.oninput = function () {
        w.resource = this.value;
        refresh();
      };

      var key = el("input");
      key.type = "text";
      key.placeholder = '["orders","id",42,"status"]';
      key.value = JSON.stringify(w.key || []);
      key.oninput = function () {
        try {
          w.key = JSON.parse(this.value);
          this.style.borderColor = "";
        } catch (e) {
          this.style.borderColor = "var(--red)";
          return;
        }
        refresh();
      };

      var val = el("input", "narrow");
      val.type = "text";
      val.placeholder = "value";
      val.value = w.value !== undefined ? String(w.value) : "";
      val.oninput = function () {
        w.value = this.value;
        refresh();
      };

      row.appendChild(res);
      row.appendChild(key);
      row.appendChild(val);
      row.appendChild(removeBtn(state.world, i));
      box.appendChild(row);
    });
  }

  function removeBtn(arr, i) {
    var x = el("button", "x", "×");
    x.onclick = function () {
      arr.splice(i, 1);
      render();
    };
    return x;
  }

  // -- adders ---------------------------------------------------------------

  function wireAdders() {
    document.querySelectorAll("[data-add]").forEach(function (b) {
      b.onclick = function () {
        var kind = this.getAttribute("data-add");
        if (kind === "cap")
          state.spec.caps.push({ kind: "count", tool: "", field: null, max: 1 });
        else if (kind === "rule")
          state.spec.rules.push({
            template: "refund_if_paid",
            params: deepCopy(TEMPLATE_DEFAULTS.refund_if_paid),
          });
        else if (kind === "effect")
          state.scenario.push({
            tool: "",
            args: {},
            reversible: true,
            compensator: null,
          });
        else if (kind === "world")
          state.world.push({ resource: "sql", key: [], value: "" });
        render();
      };
    });
  }

  // -- preview --------------------------------------------------------------

  function refresh() {
    var result = window.PolicyEval.preview(state.spec, state.scenario, state.world);
    renderPreview(result);
    renderExport();
  }

  function renderPreview(result) {
    var counts = $("counts");
    counts.innerHTML = "";
    ["allow", "deny", "cap", "gate"].forEach(function (k) {
      var p = el("span", "pill " + k, "<b>" + result.counts[k] + "</b> " + k);
      counts.appendChild(p);
    });

    var clean = $("clean");
    if (result.is_clean) {
      clean.className = "clean yes";
      clean.textContent = "✓ clean — every effect is allowed under this policy";
    } else {
      clean.className = "clean no";
      clean.textContent = "▲ not clean — some effects would be denied, capped, or gated";
    }

    var body = $("pv-body");
    body.innerHTML = "";
    result.rows.forEach(function (r) {
      var tr = el("tr");
      tr.appendChild(el("td", null, String(r.index)));
      tr.appendChild(el("td", "tool", r.tool || "<i>(none)</i>"));
      tr.appendChild(
        el("td", null, '<span class="badge ' + r.disposition + '">' + r.disposition + "</span>")
      );
      tr.appendChild(el("td", "reason", (r.reasons || []).join("; ")));
      body.appendChild(tr);
    });
    if (!result.rows.length) {
      body.appendChild(el("tr", null, '<td colspan="4" class="reason">add an effect to preview</td>'));
    }
  }

  // -- export ---------------------------------------------------------------

  function specForExport() {
    // Normalise to the JSON shape pherix.governance.from_spec consumes.
    return {
      name: state.spec.name || "untitled-policy",
      description: state.spec.description || "",
      allow: state.spec.allow && state.spec.allow.length ? state.spec.allow : null,
      deny: state.spec.deny || [],
      caps: state.spec.caps.map(function (c) {
        return c.kind === "count"
          ? { kind: "count", tool: c.tool, max: c.max, field: null }
          : { kind: "sum", tool: c.tool, max: c.max, field: c.field };
      }),
      rules: state.spec.rules.map(function (r) {
        return { template: r.template, params: r.params || {} };
      }),
      gate_irreversibles: state.spec.gate_irreversibles !== false,
    };
  }

  function toPython(spec) {
    // Mirror of pherix.governance.spec.to_python (display only; the JSON is the
    // canonical export). Kept simple — the engine-faithful codegen is tested
    // on the Python side.
    var lines = [
      '"""Generated by Pherix governance UI — edit freely, this is just code."""',
      "",
      "from pherix.core.policy import Cap, Policy",
    ];
    var tmpls = Array.from(new Set(spec.rules.map(function (r) { return r.template; }))).sort();
    if (tmpls.length)
      lines.push("from pherix.governance.templates import " + tmpls.join(", "));
    lines.push("", "");
    lines.push(("# " + spec.name + " — " + spec.description).replace(/ — $/, ""));
    lines.push("policy = Policy.with_rules(");
    lines.push("    allow=" + (spec.allow ? setRepr(spec.allow) : "None") + ",");
    lines.push("    deny=" + (spec.deny.length ? setRepr(spec.deny) : "set()") + ",");
    lines.push("    rules=[");
    spec.rules.forEach(function (r) {
      var kw = Object.keys(r.params).map(function (k) {
        return k + "=" + pyVal(r.params[k]);
      }).join(", ");
      lines.push("        " + r.template + "(" + kw + "),");
    });
    lines.push("    ],");
    lines.push("    caps=[");
    spec.caps.forEach(function (c) {
      if (c.kind === "count")
        lines.push("        Cap.count(tool=" + pyVal(c.tool) + ", max=" + c.max + "),");
      else
        lines.push(
          "        Cap.sum(tool=" + pyVal(c.tool) +
          ", via=lambda a: float(a.get(" + pyVal(c.field) + ", 0) or 0), max=" + c.max + "),"
        );
    });
    lines.push("    ],");
    lines.push(")");
    lines.push("");
    return lines.join("\n");
  }
  function setRepr(arr) {
    return "{" + arr.map(pyVal).join(", ") + "}";
  }
  function pyVal(v) {
    if (typeof v === "string") return "'" + v + "'";
    if (v === null) return "None";
    if (v === true) return "True";
    if (v === false) return "False";
    return String(v);
  }

  function renderExport() {
    var spec = specForExport();
    var body = $("export-body");
    if (exportTab === "json") {
      body.textContent = JSON.stringify(spec, null, 2);
      $("export-note").textContent = "loads via pherix.governance.from_spec(...)";
    } else {
      body.textContent = toPython(spec);
      $("export-note").textContent = "a runnable module — import its `policy`";
    }
  }

  function wireExport() {
    document.querySelectorAll(".tab").forEach(function (t) {
      t.onclick = function () {
        exportTab = this.getAttribute("data-tab");
        document.querySelectorAll(".tab").forEach(function (x) {
          x.classList.toggle("active", x === t);
        });
        renderExport();
      };
    });
    $("download").onclick = function () {
      var spec = specForExport();
      var isJson = exportTab === "json";
      var content = isJson ? JSON.stringify(spec, null, 2) : toPython(spec);
      var name = (spec.name || "policy") + (isJson ? ".json" : ".py");
      var blob = new Blob([content], { type: "text/plain" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    };
    $("copy").onclick = function () {
      navigator.clipboard.writeText($("export-body").textContent);
      this.textContent = "copied";
      var self = this;
      setTimeout(function () {
        self.textContent = "copy";
      }, 1200);
    };
  }

  // -- full render ----------------------------------------------------------

  function render() {
    syncStaticInputs();
    renderCaps();
    renderRules();
    renderEffects();
    renderWorld();
    refresh();
  }

  // -- boot -----------------------------------------------------------------

  renderStarters();
  wireStaticInputs();
  wireAdders();
  wireExport();
  render();
})();
