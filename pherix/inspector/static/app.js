/* Pherix inspector frontend — vanilla JS, no build step.
   Renders the journal: a filterable transaction list (left) and the selected
   transaction's effect timeline (right). Live mode polls so a demo animates. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  selected: null,        // selected txn_id
  view: "journal",       // "journal" | "reliability"
  relDry: false,         // reliability: include dry-runs (off = honest default)
  live: false,
  timer: null,
  seenTxns: new Set(),   // for the "fresh row" flash in live mode
  firstLoad: true,
  // scrubber
  scrub: {
    effects: [],         // the effects array for the currently-loaded transaction
    head: -1,            // current playhead index into effects (-1 = off / not loaded)
    playing: false,
    playTimer: null,
  },
};

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function pill(tone, text) {
  return `<span class="pill t-${esc(tone)}">${esc(text)}</span>`;
}

/* --- filters / query string --- */
function query() {
  const p = new URLSearchParams();
  const st = $("#fState").value, cl = $("#fClient").value, tl = $("#fTool").value;
  if (st) p.set("state", st);
  if (cl) p.set("client_id", cl);
  if (tl) p.set("tool", tl);
  if ($("#fNoDry").checked) p.set("include_dry_run", "0");
  return p.toString();
}

/* --- stats + filter vocab --- */
async function loadStats() {
  const s = await getJSON("/api/stats");
  const byState = s.txns_by_state || {};
  const live = Object.entries(byState)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${k.toLowerCase()} <b>${n}</b>`)
    .join(" · ");
  $("#stats").innerHTML =
    `<span><b>${s.txn_total}</b> txns</span>` +
    `<span><b>${s.effect_total}</b> effects</span>` +
    (live ? `<span>${live}</span>` : "") +
    (s.has_verdicts ? `<span>policy <b>verdicts</b> ✓</span>` : "");
  fillSelect("#fClient", "client", s.clients || []);
  fillSelect("#fTool", "tool", s.tools || []);
}

function fillSelect(sel, label, values) {
  const el = $(sel);
  const cur = el.value;
  el.innerHTML = `<option value="">${label}: any</option>` +
    values.map((v) => `<option ${v === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
}

/* --- transaction list --- */
async function loadList() {
  const txns = await getJSON("/api/transactions?" + query());
  const list = $("#txnList");
  if (!txns.length) {
    list.innerHTML = `<div class="empty">no transactions match</div>`;
    return;
  }
  list.innerHTML = txns.map(renderTxnRow).join("");
  // remember which ids we've seen so the next poll can flash genuinely-new rows
  if (state.firstLoad) { txns.forEach((t) => state.seenTxns.add(t.txn_id)); state.firstLoad = false; }
  list.querySelectorAll(".txn").forEach((el) =>
    el.addEventListener("click", () => select(el.dataset.id)));
}

function renderTxnRow(t) {
  const fresh = state.live && !state.seenTxns.has(t.txn_id);
  if (fresh) state.seenTxns.add(t.txn_id);
  const flags = [];
  if (t.has_gate) flags.push(`<span class="flag t-blocked">gate</span>`);
  if (t.has_compensation) flags.push(`<span class="flag t-undone">unwind</span>`);
  if (t.has_failure) flags.push(`<span class="flag t-error">fail</span>`);
  if (t.dry_run) flags.push(`<span class="flag t-unknown">dry-run</span>`);
  const meta = [];
  meta.push(`${t.effect_count} eff`);
  if (t.client_id) meta.push(`@${esc(t.client_id)}`);
  meta.push(fmtTime(t.created_at));
  return `<div class="txn ${state.selected === t.txn_id ? "sel" : ""} ${fresh ? "fresh" : ""}" data-id="${esc(t.txn_id)}">
    <div class="txn-top">
      <span class="txn-id">${esc(t.txn_id)}</span>
      ${pill(t.tone, t.state)}
      <span class="flags">${flags.join("")}</span>
    </div>
    <div class="txn-meta">${meta.map((m) => `<span>${m}</span>`).join("")}</div>
  </div>`;
}

/* --- selected timeline --- */
async function select(txnId) {
  state.selected = txnId;
  document.querySelectorAll(".txn").forEach((el) =>
    el.classList.toggle("sel", el.dataset.id === txnId));
  await loadDetail();
}

async function loadDetail() {
  if (!state.selected) return;
  const detail = $("#detail");
  let data;
  try {
    data = await getJSON("/api/transactions/" + encodeURIComponent(state.selected));
  } catch (e) {
    detail.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  const t = data.transaction;
  const effects = data.effects || [];
  const banners = {
    STUCK: `<div class="banner error">⚠ STUCK — a compensator was missing or failed. The journal cannot complete the unwind; an operator must intervene.</div>`,
    ROLLED_BACK: `<div class="banner undone">↩ Rolled back — every effect was undone via the backward fold. Nothing took effect.</div>`,
    PARTIAL: `<div class="banner error">◐ Partial — a staged irreversible failed mid-fire; the txn is unwinding.</div>`,
    STAGED: t.has_gate ? `<div class="banner pending">⏸ Held at the gate — an irreversible effect is awaiting approval before it can fire.</div>` : "",
  };

  // reset scrubber state for this transaction
  stopPlay();
  state.scrub.effects = effects;
  state.scrub.head = -1;

  detail.innerHTML = `
    <div class="detail-head">
      <h2>${esc(t.txn_id)} ${pill(t.tone, t.state)} ${t.dry_run ? pill("unknown", "dry-run") : ""}</h2>
      <div class="blurb">${esc(t.blurb)}</div>
      <div class="kv">
        <span>${t.effect_count} effects</span>
        ${t.client_id ? `<span>client: ${esc(t.client_id)}</span>` : ""}
        ${t.replayed_from ? `<span>replay of: ${esc(t.replayed_from)}</span>` : ""}
        <span>created ${fmtTime(t.created_at)}</span>
        <span>updated ${fmtTime(t.updated_at)}</span>
      </div>
    </div>
    ${banners[t.state] || ""}
    ${renderScrubber(effects.length)}
    <div class="timeline">${effects.map(renderEffect).join("")}</div>
  `;

  // wire up scrubber controls (must run after innerHTML is set)
  initScrubber();
}

function renderEffect(e) {
  const toneVar = `--tone: var(--${e.tone});`;
  const keys = [];
  (e.read_keys || []).forEach((k) => keys.push(`<div class="keyline"><span class="rk">read</span> ${esc(JSON.stringify(k))}</div>`));
  (e.write_keys || []).forEach((k) => keys.push(`<div class="keyline"><span class="wk">write</span> ${esc(JSON.stringify(k))}</div>`));
  const args = (e.args && Object.keys(e.args).length)
    ? `<div class="args"><pre>${esc(JSON.stringify(e.args, null, 2))}</pre></div>` : "";
  const verdicts = (e.policy_verdicts || []).map((v) =>
    `<div class="verdict ${v.allow ? "allow" : "deny"}">
      <span class="ph">${esc(v.phase)}</span>
      <span class="vr">${v.allow ? "allow" : "DENY"}</span>
      <span class="rn">${esc(v.rule || v.kind || "")}</span>
      ${v.reason ? `<span class="rsn">— ${esc(v.reason)}</span>` : ""}
    </div>`).join("");
  return `<div class="effect ${e.undone ? "undone" : ""}" style="${toneVar}">
    <div class="e-top">
      <span class="e-idx">[${e.idx}]</span>
      <span class="e-tool">${esc(e.tool)}</span>
      <span class="e-res">${esc(e.resource)}</span>
      <span class="e-rev">${e.reversible ? "reversible" : "irreversible"}</span>
      ${pill(e.tone, e.verdict)}
      <span class="e-time">${fmtTime(e.ts)}</span>
    </div>
    <div class="e-blurb">${esc(e.blurb)}</div>
    ${keys.length ? `<div class="keys">${keys.join("")}</div>` : ""}
    ${verdicts ? `<div class="verdicts">${verdicts}</div>` : ""}
    ${args}
  </div>`;
}

/* --- session-replay scrubber --- */

/** Build the scrubber toolbar HTML and inject it before the timeline. */
function renderScrubber(count) {
  if (count === 0) return "";
  return `
    <div class="scrubber" id="scrubber">
      <div class="scrubber-btns">
        <button class="scrub-btn" id="sBtnStart"  title="jump to start">⏮</button>
        <button class="scrub-btn" id="sBtnBack"   title="step back">◀</button>
        <button class="scrub-btn" id="sBtnPlay"   title="play / pause">▶</button>
        <button class="scrub-btn" id="sBtnFwd"    title="step forward">▶▶</button>
        <button class="scrub-btn" id="sBtnEnd"    title="jump to end">⏭</button>
      </div>
      <input class="scrub-range" id="sRange" type="range"
             min="0" max="${count - 1}" value="0" step="1" />
      <span class="scrub-pos" id="sPosLabel">0 / ${count - 1}</span>
    </div>`;
}

/** Wire scrubber DOM events. Called once after loadDetail re-renders. */
function initScrubber() {
  const s = state.scrub;
  const n = s.effects.length;
  if (n === 0) return;

  // initialise to first effect visible
  setScrubHead(0);

  const sBtnPlay  = $("#sBtnPlay");
  const sBtnBack  = $("#sBtnBack");
  const sBtnFwd   = $("#sBtnFwd");
  const sBtnStart = $("#sBtnStart");
  const sBtnEnd   = $("#sBtnEnd");
  const sRange    = $("#sRange");

  sBtnPlay.addEventListener("click", () => togglePlay());
  sBtnBack.addEventListener("click", () => { stopPlay(); setScrubHead(s.head - 1); });
  sBtnFwd.addEventListener("click",  () => { stopPlay(); setScrubHead(s.head + 1); });
  sBtnStart.addEventListener("click",() => { stopPlay(); setScrubHead(0); });
  sBtnEnd.addEventListener("click",  () => { stopPlay(); setScrubHead(n - 1); });
  sRange.addEventListener("input",   () => { stopPlay(); setScrubHead(Number(sRange.value)); });
}

/** Move the playhead to position `pos` (clamped to valid range). */
function setScrubHead(pos) {
  const s = state.scrub;
  const n = s.effects.length;
  if (n === 0) return;
  pos = Math.max(0, Math.min(n - 1, pos));
  s.head = pos;

  // update range + label
  const sRange = $("#sRange");
  const sPosLabel = $("#sPosLabel");
  if (sRange) sRange.value = pos;
  if (sPosLabel) sPosLabel.textContent = `${pos} / ${n - 1}`;

  // apply CSS classes to each .effect node
  const nodes = document.querySelectorAll(".timeline .effect");
  nodes.forEach((el, i) => {
    el.classList.remove("scrub-past", "scrub-current", "scrub-future");
    if (i < pos)  el.classList.add("scrub-past");
    else if (i === pos) el.classList.add("scrub-current");
    else          el.classList.add("scrub-future");
  });

  // scroll current effect into view
  const cur = document.querySelector(".timeline .effect.scrub-current");
  if (cur) cur.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function togglePlay() {
  if (state.scrub.playing) stopPlay();
  else startPlay();
}

function startPlay() {
  const s = state.scrub;
  if (s.effects.length === 0) return;
  s.playing = true;
  const btn = $("#sBtnPlay");
  if (btn) { btn.textContent = "⏸"; btn.classList.add("active"); }
  // advance every 600ms; stop when we hit the last effect
  s.playTimer = setInterval(() => {
    if (s.head >= s.effects.length - 1) { stopPlay(); return; }
    setScrubHead(s.head + 1);
  }, 600);
}

function stopPlay() {
  const s = state.scrub;
  s.playing = false;
  if (s.playTimer) { clearInterval(s.playTimer); s.playTimer = null; }
  const btn = $("#sBtnPlay");
  if (btn) { btn.textContent = "▶"; btn.classList.remove("active"); }
}

/* --- reliability view --- */

const pct = (r) => `${(Number(r) * 100).toFixed(1)}%`;

function bar(tone, rate, label) {
  const w = Math.max(0, Math.min(100, Number(rate) * 100));
  return `<div class="rel-bar">
    <div class="rel-bar-label">${esc(label)} <b>${pct(rate)}</b></div>
    <div class="rel-bar-track"><div class="rel-bar-fill t-${esc(tone)}" style="width:${w}%"></div></div>
  </div>`;
}

function card(title, inner) {
  return `<section class="rel-card"><h3>${esc(title)}</h3>${inner}</section>`;
}

async function loadReliability() {
  const grid = $("#relGrid");
  let d;
  try {
    d = await getJSON("/api/reliability?include_dry_run=" + (state.relDry ? "1" : "0"));
  } catch (e) {
    grid.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  $("#relScope").textContent =
    `outcomes over ${d.outcomes.settled} settled txn(s)` +
    ` · effects over ${d.effects.total}` +
    ` · denials: ${d.scope.denials_scope}` +
    ` · dry-runs ${d.scope.include_dry_run ? "included" : "excluded"}`;

  const o = d.outcomes.rates;
  const outcomes = card("transaction outcomes",
    bar("ok", o.commit, "commit") +
    bar("undone", o.rollback, "rollback") +
    bar("error", o.partial, "partial") +
    bar("error", o.stuck, "stuck"));

  const e = d.effects.rates;
  const effects = card("effect outcomes",
    bar("blocked", e.gate, "gate") +
    bar("error", e.failure, "failure") +
    bar("undone", e.compensated, "compensated") +
    `<div class="rel-stat">gate incidence (txns w/ a gate): <b>${pct(d.effects.gate_incidence)}</b></div>` +
    `<div class="rel-stat">isolation conflicts recorded: <b>${d.conflict_total}</b></div>`);

  const tools = d.top_failing_tools.length
    ? d.top_failing_tools.map((t) =>
        `<div class="rel-row"><span class="rel-tool">${esc(t.tool)}</span>
          <span class="rel-counts">${t.failed ? `<span class="t-error">${t.failed} failed</span>` : ""}
          ${t.gated ? `<span class="t-blocked">${t.gated} gated</span>` : ""}</span></div>`).join("")
    : `<div class="empty small">no failing tools</div>`;

  const denials = d.denials.length
    ? d.denials.map((x) =>
        `<div class="rel-row"><span class="rel-count">${x.count}×</span>
          <span class="rel-reason">${esc(x.reason == null ? "(no reason given)" : x.reason)}</span></div>`).join("")
    : `<div class="empty small">no denials</div>`;

  const held = d.held_back.length
    ? d.held_back.map((h) =>
        `<div class="rel-row"><span class="rel-tool">${esc(h.txn_id)}</span>
          ${pill("blocked", h.state)}</div>`).join("")
    : `<div class="empty small">nothing held at the gate</div>`;

  grid.innerHTML =
    outcomes + effects +
    card("top-failing tools (failed / gated, never compensated)", tools) +
    card("denial reasons (all verdicts incl. dry-run)", denials) +
    card("held back at the gate", held);
}

/* --- view switching --- */
function setView(view) {
  state.view = view;
  $("#journalView").classList.toggle("hidden", view !== "journal");
  $("#reliabilityView").classList.toggle("hidden", view !== "reliability");
  document.querySelectorAll(".view-btn").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === view));
  refresh();
}

/* --- live mode --- */
function setLive(on) {
  state.live = on;
  $("#liveToggle").classList.toggle("on", on);
  $("#liveLabel").textContent = "live: " + (on ? "on" : "off");
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (on) state.timer = setInterval(refresh, 1500);
}

async function refresh() {
  try {
    await loadStats();
    if (state.view === "reliability") {
      await loadReliability();
    } else {
      await loadList();
      if (state.selected) await loadDetail();
    }
  } catch (e) { /* transient during a live run; next tick retries */ }
}

/* --- wire up --- */
function init() {
  ["#fState", "#fClient", "#fTool", "#fNoDry"].forEach((s) =>
    $(s).addEventListener("change", loadList));
  $("#liveToggle").addEventListener("click", () => setLive(!state.live));
  document.querySelectorAll(".view-btn").forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.view)));
  $("#relDry").addEventListener("change", (ev) => {
    state.relDry = ev.target.checked;
    loadReliability();
  });
  refresh();
}
document.addEventListener("DOMContentLoaded", init);
