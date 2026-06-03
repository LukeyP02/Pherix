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
  // replay: a screen-recordable walk of the journal as a state-transition movie
  replay: {
    txn: null,           // the transaction summary being replayed
    effects: [],         // the effects array
    conflicts: [],       // txn-level isolation conflicts (drives the clash flash)
    running: false,
    timers: [],          // pending setTimeout handles, cleared on stop
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

  // reset replay state for this transaction (drives the state-transition movie)
  stopReplay();
  state.replay.txn = t;
  state.replay.effects = effects;
  state.replay.conflicts = data.conflicts || [];

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
    ${renderReplayBar(t, effects.length)}
    ${renderScrubber(effects.length)}
    <div class="timeline">${effects.map(renderEffect).join("")}</div>
    <div id="lineage" class="lineage-slot"></div>
  `;

  // wire up scrubber controls (must run after innerHTML is set)
  initScrubber();
  initReplay();
  // lineage is a second, independent fetch so a hiccup never blanks the
  // timeline; it fills the slot above once it lands.
  loadLineage(state.selected);
}

/* --- provenance / lineage --- */
/** Fetch and render the causal read→write chains for the selected txn. */
async function loadLineage(txnId) {
  const slot = $("#lineage");
  if (!slot) return;
  let lin;
  try {
    lin = await getJSON("/api/lineage?txn=" + encodeURIComponent(txnId));
  } catch (e) {
    slot.innerHTML = "";  // provenance is a bonus view; stay silent on failure
    return;
  }
  // guard against a stale response landing after the user moved on
  if (state.selected !== txnId) return;
  slot.innerHTML = renderLineage(lin);
}

function renderLineage(lin) {
  const chains = lin.chains || [];
  if (!chains.length) {
    return `<details class="lineage"><summary>lineage <span class="ln-count">no writes to trace</span></summary>
      <div class="ln-caveat">${esc(lin.caveat || "")}</div></details>`;
  }
  const rows = chains.map(renderChain).join("");
  return `<details class="lineage">
    <summary>lineage <span class="ln-count">${chains.length} write${chains.length === 1 ? "" : "s"} traced</span></summary>
    <div class="ln-body">${rows}</div>
    <div class="ln-caveat">${esc(lin.caveat || "")}</div>
  </details>`;
}

function keyLabel(k) {
  // k = {resource, key, version}; render resource:key (vN) compactly
  const key = Array.isArray(k.key) ? k.key.join("/") : String(k.key);
  const v = k.version == null ? "" : ` v${k.version}`;
  return `${esc(k.resource)}:${esc(key)}${v}`;
}

function renderChain(c) {
  const writes = (c.writes || []).map((w) =>
    `<span class="ln-w">${keyLabel(w)}</span>`).join(" ");
  let reads;
  if (!(c.informed_by || []).length) {
    reads = `<div class="ln-none">no recorded prior reads — provenance starts here</div>`;
  } else {
    reads = (c.informed_by || []).map((i) => {
      // the strongest available claim, honestly tagged
      let tag;
      if (i.same_effect) tag = `<span class="ln-tag same">same call</span>`;
      else if (i.produced_by_external) tag = `<span class="ln-tag ext">origin pre-journal</span>`;
      else tag = `<span class="ln-tag prod">from ${esc(i.produced_by)}</span>`;
      return `<div class="ln-read">
        <span class="rk">read</span> ${keyLabel(i)}
        <span class="ln-by">by ${esc(i.tool)}</span> ${tag}
      </div>`;
    }).join("");
  }
  return `<div class="ln-chain">
    <div class="ln-head">
      <span class="e-idx">[${c.idx}]</span>
      <span class="e-tool">${esc(c.tool)}</span>
      ${pill(c.tone, c.verdict)}
      <span class="ln-wrote"><span class="wk">wrote</span> ${writes}</span>
    </div>
    <div class="ln-informed">${reads}</div>
  </div>`;
}

/** Normalise a read/write key (tuple [res,key,ver] or {resource,key,version})
 *  to a `resource|key` signature so it can be matched against a conflict. */
function keySig(k) {
  let res, key;
  if (Array.isArray(k)) { res = k[0]; key = k[1]; }
  else if (k && typeof k === "object") { res = k.resource; key = k.key; }
  else { return String(k); }
  const keyStr = Array.isArray(key) ? key.join("/") : String(key);
  return `${res}|${keyStr}`;
}

/** The set of `resource|key` signatures that an isolation conflict touches,
 *  built from the currently-loaded transaction's conflicts. */
function conflictSigs() {
  const sigs = new Set();
  (state.replay.conflicts || []).forEach((c) => sigs.add(keySig(c)));
  return sigs;
}

function renderEffect(e) {
  const toneVar = `--tone: var(--${e.tone});`;
  const clashes = conflictSigs();
  const keys = [];
  (e.read_keys || []).forEach((k) => {
    const cls = clashes.has(keySig(k)) ? " clash" : "";
    keys.push(`<div class="keyline${cls}"><span class="rk">read</span> ${esc(JSON.stringify(k))}</div>`);
  });
  (e.write_keys || []).forEach((k) => {
    const cls = clashes.has(keySig(k)) ? " clash" : "";
    keys.push(`<div class="keyline${cls}"><span class="wk">write</span> ${esc(JSON.stringify(k))}</div>`);
  });
  const args = (e.args && Object.keys(e.args).length)
    ? `<div class="args"><pre>${esc(JSON.stringify(e.args, null, 2))}</pre></div>` : "";
  const verdicts = (e.policy_verdicts || []).map((v) =>
    `<div class="verdict ${v.allow ? "allow" : "deny"}">
      <span class="ph">${esc(v.phase)}</span>
      <span class="vr">${v.allow ? "allow" : "DENY"}</span>
      <span class="rn">${esc(v.rule || v.kind || "")}</span>
      ${v.reason ? `<span class="rsn">— ${esc(v.reason)}</span>` : ""}
    </div>`).join("");
  return `<div class="effect ${e.undone ? "undone" : ""}" data-eidx="${esc(e.idx)}" style="${toneVar}">
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

/* --- state-transition REPLAY ------------------------------------------------
   The journal is a time series; commit is a forward fold, rollback a backward
   fold. The replay *animates* that fold on a screen-recordable timer, reading
   purely off the data already returned for the selected transaction — no new
   endpoints, no write paths. The inspector stays read-only.

   The walk:
     1. forward fold — each effect lands (`.applying`); a GATED effect BLOCKS
        with an amber barrier (`.blocked`); an effect whose key collides with a
        recorded isolation conflict flashes red (`.conflict-hit`).
     2. resolution — if the txn ultimately COMMITTED, a held gate CLEARS
        (`.cleared`); if it ROLLED_BACK / went PARTIAL / STUCK, the applied
        effects visibly UNWIND in reverse (`.unwinding`, settle to 'restored').
   Every transition is derived from the effect's persisted status + the txn
   state — never invented. ------------------------------------------------- */

// the per-step cadence (ms) — slow enough to read, paced for a screen capture
const REPLAY_STEP = 760;
const REPLAY_UNWIND = 620;

/** Build the replay control bar. Hidden when the txn has no effects. */
function renderReplayBar(t, count) {
  if (count === 0) return "";
  const unwinds = t.state === "ROLLED_BACK" || t.state === "PARTIAL" || t.state === "STUCK";
  const caption = unwinds
    ? "watch the forward fold land, then the backward fold unwind it"
    : "watch the journal fold forward, effect by effect";
  return `
    <div class="replay-bar" id="replayBar">
      <button class="replay-btn" id="replayBtn" type="button">
        <span class="glyph">▶</span><span class="lbl">Replay</span>
      </button>
      <span class="replay-caption">${esc(caption)}</span>
      <span class="replay-now" id="replayNow"></span>
    </div>`;
}

/** Wire the Replay button. Called once after loadDetail re-renders. */
function initReplay() {
  const btn = $("#replayBtn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    if (state.replay.running) stopReplay();
    else runReplay();
  });
}

/** Schedule a step on the replay clock, recording the handle so stopReplay
 *  can cancel everything still pending. */
function replayAt(ms, fn) {
  const h = setTimeout(fn, ms);
  state.replay.timers.push(h);
}

function replayNow(html) {
  const el = $("#replayNow");
  if (el) el.innerHTML = html;
}

function effectNode(idx) {
  return document.querySelector(`.timeline .effect[data-eidx="${idx}"]`);
}

/** Clear every replay-driven animation class so the timeline returns to its
 *  resting (data-faithful) render. */
function clearReplayClasses() {
  document.querySelectorAll(".timeline .effect").forEach((el) => {
    el.classList.remove("applying", "unwinding", "blocked", "cleared", "conflict-hit");
  });
  document.querySelectorAll(".timeline .replay-tag").forEach((el) => el.remove());
  document.querySelectorAll(".replay-conflict").forEach((el) => el.remove());
}

/** Does this effect collide with a recorded isolation conflict? */
function effectConflicts(e) {
  if (!(state.replay.conflicts || []).length) return false;
  const clashes = conflictSigs();
  const keys = [].concat(e.read_keys || [], e.write_keys || []);
  return keys.some((k) => clashes.has(keySig(k)));
}

/** Drop a small "restored / cleared / held" tag next to a tool name. */
function tagEffect(idx, kind, label) {
  const node = effectNode(idx);
  if (!node) return;
  const tool = node.querySelector(".e-tool");
  if (!tool || tool.parentElement.querySelector(`.replay-tag.${kind}`)) return;
  const tag = document.createElement("span");
  tag.className = `replay-tag ${kind}`;
  tag.textContent = label;
  tool.insertAdjacentElement("afterend", tag);
}

function setReplayRunning(on) {
  state.replay.running = on;
  const btn = $("#replayBtn");
  if (btn) {
    btn.querySelector(".glyph").textContent = on ? "⏸" : "▶";
    btn.querySelector(".lbl").textContent = on ? "Stop" : "Replay";
    btn.classList.toggle("active", on);
  }
  const detail = $("#detail");
  if (detail) detail.classList.toggle("replaying", on);
}

/** Cancel a running replay and reset the timeline to its resting render. */
function stopReplay() {
  state.replay.timers.forEach((h) => clearTimeout(h));
  state.replay.timers = [];
  if (state.replay.running) {
    clearReplayClasses();
    replayNow("");
  }
  setReplayRunning(false);
}

/** Run the state-transition movie for the loaded transaction. */
function runReplay() {
  const { txn, effects } = state.replay;
  if (!effects.length) return;
  stopReplay();              // idempotent reset before we start
  clearReplayClasses();
  setReplayRunning(true);

  const unwinds = txn.state === "ROLLED_BACK" || txn.state === "PARTIAL" || txn.state === "STUCK";
  const committed = txn.state === "COMMITTED";

  let clock = 0;
  // track which indices we actually *applied* (so the unwind reverses the
  // right ones), and any index left held at the gate.
  const applied = [];
  let gatedIdx = null;

  // --- phase 1: the forward fold ---
  effects.forEach((e) => {
    const idx = e.idx;
    const conflict = effectConflicts(e);
    clock += REPLAY_STEP;
    replayAt(clock, () => {
      const node = effectNode(idx);
      if (!node) return;
      node.classList.remove("applying", "blocked", "conflict-hit");
      void node.offsetWidth;   // restart the animation if re-triggered

      if (conflict) {
        node.classList.add("conflict-hit");
        node.querySelectorAll(".keyline.clash").forEach((kl) => void kl.offsetWidth);
        injectConflictBanner();
        replayNow(`<b>[${idx}] ${esc(e.tool)}</b> — isolation conflict: the version it read no longer matches the world`);
      } else if (e.status === "GATED") {
        node.classList.add("blocked");
        gatedIdx = idx;
        replayNow(`<b>[${idx}] ${esc(e.tool)}</b> — held at the gate (irreversible, needs approval)`);
      } else {
        node.classList.add("applying");
        if (e.reversible || e.status === "APPLIED" || e.status === "COMPENSATED") applied.push(idx);
        replayNow(`<b>[${idx}] ${esc(e.tool)}</b> — ${esc(e.blurb)}`);
      }
      node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  });

  // --- phase 2: the resolution ---
  clock += REPLAY_STEP;
  replayAt(clock, () => {
    if (gatedIdx !== null) {
      const node = effectNode(gatedIdx);
      if (committed && node) {
        // the human approved → the gate clears and the held effect lands
        node.classList.remove("blocked");
        void node.offsetWidth;
        node.classList.add("cleared");
        tagEffect(gatedIdx, "cleared", "cleared");
        replayNow(`<b>[${gatedIdx}]</b> approved at the gate — the irreversible step fires`);
      } else if (node) {
        // never approved → it stays held; nothing irreversible happened
        tagEffect(gatedIdx, "held", "held");
        replayNow(`<b>[${gatedIdx}]</b> never approved — the irreversible step never fired`);
      }
    }
  });

  if (unwinds && applied.length) {
    // the backward fold: reverse the applied effects, newest first
    const order = applied.slice().reverse();
    clock += REPLAY_STEP;
    order.forEach((idx) => {
      clock += REPLAY_UNWIND;
      replayAt(clock, () => {
        const node = effectNode(idx);
        if (!node) return;
        node.classList.remove("applying", "cleared");
        void node.offsetWidth;
        node.classList.add("unwinding");
        tagEffect(idx, "restored", "restored");
        node.scrollIntoView({ block: "nearest", behavior: "smooth" });
        replayNow(`unwinding <b>[${idx}]</b> — the backward fold restores the prior state`);
      });
    });
    clock += REPLAY_UNWIND;
    replayAt(clock, () => {
      const verb = txn.state === "STUCK" ? "STUCK — the unwind could not complete"
        : txn.state === "PARTIAL" ? "partial — unwinding after a mid-fire failure"
        : "rolled back — nothing took effect";
      replayNow(`<b>${esc(verb)}</b>`);
    });
  } else {
    clock += REPLAY_STEP;
    replayAt(clock, () => {
      const verb = committed ? "committed cleanly — the forward fold completed"
        : txn.state === "STAGED" ? "staged — irreversibles awaiting the gate"
        : esc(txn.state.toLowerCase());
      replayNow(`<b>${verb}</b>`);
    });
  }

  // hand back the controls once the movie ends
  clock += REPLAY_STEP;
  replayAt(clock, () => setReplayRunning(false));
}

/** Surface a one-line conflict banner above the timeline (idempotent). */
function injectConflictBanner() {
  if ($(".replay-conflict")) return;
  const c = (state.replay.conflicts || [])[0];
  if (!c) return;
  const key = Array.isArray(c.key) ? c.key.join("/") : String(c.key);
  const banner = document.createElement("div");
  banner.className = "replay-conflict";
  banner.innerHTML =
    `⚡ Isolation conflict on <b>${esc(c.resource)}:${esc(key)}</b> — ` +
    `read at v${esc(c.version_at_read)}, world now at v${esc(c.version_now)}` +
    (c.version_expected != null ? ` (expected v${esc(c.version_expected)})` : "");
  const timeline = $(".timeline");
  if (timeline) timeline.parentNode.insertBefore(banner, timeline);
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
