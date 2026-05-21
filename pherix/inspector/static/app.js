/* Pherix inspector frontend — vanilla JS, no build step.
   Renders the journal: a filterable transaction list (left) and the selected
   transaction's effect timeline (right). Live mode polls so a demo animates. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  selected: null,        // selected txn_id
  live: false,
  timer: null,
  seenTxns: new Set(),   // for the "fresh row" flash in live mode
  firstLoad: true,
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
  const banners = {
    STUCK: `<div class="banner error">⚠ STUCK — a compensator was missing or failed. The journal cannot complete the unwind; an operator must intervene.</div>`,
    ROLLED_BACK: `<div class="banner undone">↩ Rolled back — every effect was undone via the backward fold. Nothing took effect.</div>`,
    PARTIAL: `<div class="banner error">◐ Partial — a staged irreversible failed mid-fire; the txn is unwinding.</div>`,
    STAGED: t.has_gate ? `<div class="banner pending">⏸ Held at the gate — an irreversible effect is awaiting approval before it can fire.</div>` : "",
  };
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
    <div class="timeline">${data.effects.map(renderEffect).join("")}</div>
  `;
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
    await loadList();
    if (state.selected) await loadDetail();
  } catch (e) { /* transient during a live run; next tick retries */ }
}

/* --- wire up --- */
function init() {
  ["#fState", "#fClient", "#fTool", "#fNoDry"].forEach((s) =>
    $(s).addEventListener("change", loadList));
  $("#liveToggle").addEventListener("click", () => setLive(!state.live));
  refresh();
}
document.addEventListener("DOMContentLoaded", init);
