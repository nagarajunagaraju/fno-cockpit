const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

/* Verify that the served HTML matches this JavaScript build. */
(() => {
  const required = ["loadPredictions","topBull","topBear","predMeta","backtestMeta",
    "filters","search","scan","guard","gStatus","mTrades","mPnl","mOpen","mCool",
    "gMsgs","loading","loginBtn","updated","notifBtn","pLog","pForce","mCancel",
    "mSave","saveRules","rulesForm","open","openEmpty","jTable"];
  const missing = required.filter(id => !document.getElementById(id));
  if (missing.length) console.error("Options Cockpit HTML/JS version mismatch. Missing IDs:", missing);
})();
const inr = (n, d = 0) => n == null ? "—" : "₹" + Number(n).toLocaleString("en-IN", {maximumFractionDigits: d, minimumFractionDigits: d});
const num = (n, d = 2) => n == null ? "—" : Number(n).toLocaleString("en-IN", {maximumFractionDigits: d, minimumFractionDigits: d});
const sgn = n => (n > 0 ? "+" : "") + num(n);
const cls = n => n > 0 ? "up" : n < 0 ? "dn" : "";
const BU = {"Long Buildup": "LB", "Short Buildup": "SB", "Short Covering": "SC", "Long Unwinding": "LU"};

let D = null, filter = "all", selected = null, plan = null, side = null, refreshMs = 5000;
let lastAlerts = {}, closingId = null, tags = [];

async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}


/* ---------------- next-session outlook ---------------- */
async function loadPredictions() {
  const b = $("#loadPredictions");
  b.disabled = true; b.textContent = "Calculating…";
  try {
    const r = await api("/api/predictions");
    const render = xs => xs.length ? `<table><thead><tr><th>Symbol</th><th>Score</th><th>Strength*</th><th>Entry ref.</th><th>Stop ref.</th></tr></thead><tbody>${xs.map(x => `<tr><td><b>${x.symbol}</b></td><td>${x.score}</td><td>${x.confidence_estimate_pct}%</td><td>${num(x.entry_reference)}</td><td>${num(x.stop_reference)}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">No qualified setup — NO TRADE.</p>`;
    $("#topBull").innerHTML = render(r.top_bullish);
    $("#topBear").innerHTML = render(r.top_bearish);
    $("#predMeta").textContent = `Universe ${r.universe_count} · Bullish ${r.bullish_count} · Bearish ${r.bearish_count} · No trade ${r.no_trade_count} · ${r.generated_at}`;
    $("#backtestMeta").textContent = r.disclaimer;
    const bt = await api("/api/backtest");
    $("#backtestMeta").textContent += bt.samples ? ` Walk-forward diagnostic: ${bt.samples} samples, directional hit rate ${bt.directional_hit_rate_pct}% (no costs/slippage).` : ` Backtest: ${bt.message || "not available"}`;
  } catch (e) { $("#predMeta").textContent = e.message; }
  finally { b.disabled = false; b.textContent = "Refresh outlook"; }
}
$("#loadPredictions").onclick = loadPredictions;

/* ---------------- tabs ---------------- */
$$(".tab").forEach(b => b.onclick = () => {
  $$(".tab").forEach(x => x.classList.toggle("active", x === b));
  $$(".tabpane").forEach(p => p.classList.toggle("hidden", p.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "journal") loadJournal();
  if (b.dataset.tab === "rules") renderRules();
});

/* ---------------- main loop ---------------- */
async function refresh() {
  try {
    D = await api("/api/dashboard");
    refreshMs = (D.rules.refresh_seconds || 5) * 1000;
    renderHeader(); renderGuard(); renderScanner(); renderOpen(); updatePlannerLock(); refreshSelected();
  } catch (e) {
    $("#updated").textContent = "Server offline";
  }
  setTimeout(refresh, refreshMs);
}

function renderHeader() {
  const m = $("#mode");
  m.textContent = D.mode; m.className = "badge " + D.mode.toLowerCase();
  if (D.index) $("#nifty").innerHTML = `NIFTY ${num(D.index.ltp)} <span class="${cls(D.index.chg)}">${sgn(D.index.chg)}%</span>`;
  $("#updated").textContent = D.error ? "Error: " + D.error : (D.updated ? "Updated " + D.updated : "");
  $("#loginBtn").classList.toggle("hidden", D.mode === "DEMO" || D.logged_in);
  const L = $("#loading");
  if (!D.ready) {
    L.classList.remove("hidden");
    L.innerHTML = !D.logged_in
      ? `<b>Kite login kavali.</b> Paina "Login with Kite" click cheyi.`
      : `<b>Loading history…</b> ${D.progress.loaded}/${D.progress.total} · ${D.progress.message}<br><span class="muted">First run of the day takes 2–3 minutes; later it's instant (cached).</span>`;
  } else if (D.progress.loaded < D.progress.total) {
    L.classList.remove("hidden");
    L.innerHTML = `Still loading: ${D.progress.loaded}/${D.progress.total} stocks. Scanner fills as it loads.`;
  } else L.classList.add("hidden");
}

function renderGuard() {
  const g = D.discipline;
  $("#guard").className = "guard " + g.status;
  $("#gStatus").textContent = {GREEN: "CLEAR TO TRADE", YELLOW: "CAUTION", RED: "STOP TRADING"}[g.status];
  $("#mTrades").textContent = `${g.trades_today} / ${g.max_trades}`;
  setBar("#bTrades", g.trades_today / g.max_trades);
  $("#mPnl").innerHTML = `<span class="${cls(g.day_pnl)}">${inr(g.day_pnl)}</span> <span class="muted">/ -${inr(g.max_daily_loss)}</span>`;
  setBar("#bPnl", g.day_pnl < 0 ? -g.day_pnl / g.max_daily_loss : 0);
  $("#mOpen").textContent = `${g.open_count} / ${g.max_open}`;
  setBar("#bOpen", g.open_count / g.max_open);
  $("#mCool").textContent = g.cooldown_until ? "till " + g.cooldown_until.slice(11, 16) : "—";
  $("#gMsgs").innerHTML = g.messages.map(m => `<li>${m}</li>`).join("");
}
function setBar(sel, f) {
  const el = $(sel); f = Math.max(0, Math.min(1, f));
  el.style.width = (f * 100) + "%";
  el.style.background = f >= 1 ? "var(--red)" : f >= .66 ? "var(--amber)" : "var(--green)";
}

/* ---------------- scanner ---------------- */
$$("#filters .chip").forEach(c => c.onclick = () => {
  filter = c.dataset.f; $$("#filters .chip").forEach(x => x.classList.toggle("active", x === c)); renderScanner();
});
$("#search").oninput = renderScanner;

function renderScanner() {
  if (!D) return;
  const q = $("#search").value.trim().toUpperCase();
  const rows = D.rows.filter(r =>
    (filter === "all" || r.bias === filter || r.buildup === filter) && (!q || r.symbol.includes(q)));
  $("#scan tbody").innerHTML = rows.map(r => {
    const w = Math.abs(r.score) / 2, left = r.score >= 0 ? 50 : 50 - w;
    const col = r.score >= 0 ? "var(--green)" : "var(--red)";
    const trade = r.bias === "BULLISH" ? "BUY CE" : r.bias === "BEARISH" ? "BUY PE" : "NO TRADE";
    return `<tr data-s="${r.symbol}" class="${r.symbol === selected ? "sel" : ""}">
      <td><b>${r.symbol}</b></td>
      <td class="r">${num(r.ltp)}</td>
      <td class="r ${cls(r.chg)}">${sgn(r.chg)}</td>
      <td class="r ${cls(r.oi_chg)}">${sgn(r.oi_chg)}</td>
      <td><span class="bu ${BU[r.buildup] || ""}">${r.buildup}</span></td>
      <td class="r">${num(r.rsi, 0)}</td>
      <td class="r">${num(r.atr)}</td>
      <td><div class="score"><div class="sbar"><i style="left:${left}%;width:${w}%;background:${col}"></i></div><span class="${cls(r.score)}">${r.score}</span></div></td>
      <td><span class="bias ${r.bias === "NO TRADE" ? "NO" : r.bias}">${trade}</span></td></tr>`;
  }).join("") || `<tr><td colspan="9" class="muted">No stocks match.</td></tr>`;
  $$("#scan tbody tr[data-s]").forEach(tr => tr.onclick = () => selectSymbol(tr.dataset.s));
}

/* ---------------- option chain ---------------- */
let C = null, sel = null, userEdited = false;

function suggest(type) {
  let best = null, bd = 9;
  for (const r of C.strikes) {
    const l = r[type];
    if (!l || l.delta == null || !l.ltp || (l.spread_pct != null && l.spread_pct > 5)) continue;
    const d = Math.abs(Math.abs(l.delta) - 0.55);
    if (d < bd) { bd = d; best = r.strike; }
  }
  return best;
}

async function selectSymbol(sym) {
  selected = sym; sel = null; plan = null; userEdited = false; renderScanner();
  $$("#pCheck input").forEach(i => i.checked = false);
  $("#pReason").value = ""; $("#pMsg").textContent = "";
  await loadChain();
  if (!C) return;
  sel = {type: C.suggested.type, strike: C.suggested.strike ?? suggest(C.suggested.type), expiry: C.expiry};
  renderChain(); await loadPlan();
}

async function loadChain() {
  try {
    const q = {symbol: selected}; if (sel) q.expiry = sel.expiry;
    C = await api("/api/chain?" + new URLSearchParams(q));
    $("#chainCard").classList.remove("hidden");
  } catch (e) { C = null; showPMsg(e.message, true); }
}

function renderChain() {
  if (!C) return;
  $("#cSym").textContent = `${C.symbol} · ${C.expiry}`;
  const st = [["Spot", num(C.spot)], ["ATM IV", C.atm_iv + "%"], ["HV20", (C.hv20 ?? "—") + "%"],
    ["PCR", C.pcr], ["Max pain", num(C.max_pain, 0)], ["Support (PE OI)", num(C.support, 0)],
    ["Resistance (CE OI)", num(C.resistance, 0)], ["DTE", C.dte + "d"]];
  $("#cStats").innerHTML = st.map(([k, v]) => `<span class="cstat">${k} <b>${v}</b></span>`).join("");
  const lot = C.lot_size;
  const maxOI = Math.max(1, ...C.strikes.flatMap(r => [r.CE?.oi || 0, r.PE?.oi || 0]));
  const sugC = suggest("CE"), sugP = suggest("PE");
  const oiCell = (l, side) => {
    if (!l) return `<td></td>`;
    const w = l.oi / maxOI * 100;
    return `<td class="r oibar"><i style="${side === "CE" ? "right" : "left"}:0;width:${w}%;background:${side === "CE" ? "var(--red)" : "var(--green)"}"></i><span>${num(l.oi / lot, 0)}</span></td>`;
  };
  const leg = (r, t) => {
    const l = r[t];
    if (!l) return ["<td></td>", "<td></td>", "<td></td>", "<td></td>"];
    const itm = (t === "CE" ? r.strike < C.spot : r.strike > C.spot) ? " itm" : "";
    const isSel = sel && sel.type === t && sel.strike === r.strike ? " sel" : "";
    const star = (t === "CE" ? sugC : sugP) === r.strike ? " ★" : "";
    return [`<td class="r${itm} ${cls(l.oi_chg)}">${l.oi_chg == null ? "…" : sgn(l.oi_chg)}</td>`,
      `<td class="r${itm}">${l.iv ?? "—"}</td>`, `<td class="r${itm}">${num(l.delta)}</td>`,
      `<td class="r pick${itm}${isSel}" data-t="${t}" data-k="${r.strike}">${num(l.ltp)}${star}</td>`];
  };
  $("#chain tbody").innerHTML = C.strikes.map(r => {
    const [cOc, cIv, cD, cL] = leg(r, "CE"), [pOc, pIv, pD, pL] = leg(r, "PE");
    const wallC = r.strike === C.resistance ? " wallC" : "", wallP = r.strike === C.support ? " wallP" : "";
    return `<tr class="${r.strike === C.atm ? "atm" : ""}">${oiCell(r.CE, "CE")}${cOc}${cIv}${cD}${cL}
      <td class="k${wallC}${wallP}">${num(r.strike, r.strike % 1 ? 1 : 0)}</td>${pL}${pD}${pIv}${pOc}${oiCell(r.PE, "PE")}</tr>`;
  }).join("");
  $$("#chain td.pick").forEach(td => td.onclick = () => {
    sel.type = td.dataset.t; sel.strike = +td.dataset.k; userEdited = false; renderChain(); loadPlan();
  });
  // planner selects
  $("#pStrike").innerHTML = C.strikes.map(r => `<option value="${r.strike}" ${sel && r.strike === sel.strike ? "selected" : ""}>${r.strike}</option>`).join("");
  $("#pExpiry").innerHTML = C.expiries.map(e => `<option ${e === C.expiry ? "selected" : ""}>${e}</option>`).join("");
}

/* ---------------- planner ---------------- */
async function loadPlan() {
  if (!sel) return;
  const q = {symbol: selected, strike: sel.strike, type: sel.type, expiry: sel.expiry};
  if (userEdited) { q.entry = $("#pEntry").value; q.sl = $("#pSl").value; }
  try {
    plan = await api("/api/plan?" + new URLSearchParams(q));
    renderPlan(); if ($("#pMsg").classList.contains("err")) $("#pMsg").textContent = "";
  } catch (e) { showPMsg(e.message, true); }
}

function renderPlan() {
  $("#pEmpty").classList.add("hidden"); $("#pBody").classList.remove("hidden");
  $("#pSym").textContent = plan.underlying;
  const b = $("#pBias"); b.textContent = plan.bias; b.className = "bias " + (plan.bias === "NO TRADE" ? "NO" : plan.bias);
  $("#pScore").textContent = "score " + plan.score;
  $("#pOpt").textContent = `BUY ${plan.symbol} @ ${num(plan.entry)}`;
  $("#pReasons").innerHTML = (plan.reasons || []).map(x => `<li>${x}</li>`).join("") || "<li>No strong signal</li>";
  $$("#pSide button").forEach(x => x.classList.toggle("on", x.dataset.side === plan.opt_type));
  if (!userEdited || document.activeElement !== $("#pEntry")) $("#pEntry").value = plan.entry;
  if (!userEdited || document.activeElement !== $("#pSl")) $("#pSl").value = plan.sl;
  $("#pStrike").value = plan.strike;
  $("#pT1").textContent = num(plan.t1); $("#pT2").textContent = num(plan.t2);
  $("#pRiskPct").textContent = `${num(plan.prem_risk)} (${plan.prem_risk_pct}%)`;
  $("#pGreeks").textContent = `${num(plan.delta)} · ${num(plan.theta)}`;
  $("#pIv").innerHTML = plan.iv && plan.hv20 ? `<span class="${plan.iv > 1.4 * plan.hv20 ? "dn" : ""}">${plan.iv}%</span> / ${plan.hv20}%` : "—";
  $("#pDte").innerHTML = `<span class="${plan.dte < 5 ? "dn" : ""}">${plan.dte}</span>`;
  $("#pLots").innerHTML = plan.lots > 0 ? `<span class="up">${plan.lots}</span> <span class="muted">(${plan.qty} qty)</span>` : `<span class="dn">0 · skip</span>`;
  $("#pRisk").textContent = `${inr(plan.risk_amount)} (${inr(plan.risk_budget)})`;
  $("#pOutlay").textContent = plan.lots ? inr(plan.outlay) : `${inr(plan.per_lot_outlay)}/lot`;
  $("#pRew").textContent = inr(plan.reward_t1);
  $("#pU").innerHTML = `Stock <b>${num(plan.spot)}</b> · exit if stock crosses <b class="dn">${num(plan.u_sl)}</b> · stock target <b class="up">${num(plan.u_t1)}</b> <span class="muted">(ATR ${num(plan.atr)})</span>`;
  const warns = [...plan.warnings];
  $("#pWarn").innerHTML = warns.map(w => `<div>⚠ ${w}</div>`).join("");
  $("#pWarn").classList.toggle("hidden", !warns.length);
  updatePlannerLock();
}

$$("#pSide button").forEach(b => b.onclick = () => {
  if (!C || !sel) return;
  sel.type = b.dataset.side; sel.strike = suggest(sel.type); userEdited = false; renderChain(); loadPlan();
});
$("#pStrike").onchange = () => { sel.strike = +$("#pStrike").value; userEdited = false; renderChain(); loadPlan(); };
$("#pExpiry").onchange = async () => {
  sel.expiry = $("#pExpiry").value; userEdited = false; await loadChain();
  if (!C.strikes.some(r => r.strike === sel.strike)) sel.strike = suggest(sel.type);
  renderChain(); loadPlan();
};
let t;
["#pEntry", "#pSl"].forEach(s => $(s).oninput = () => {
  userEdited = true; clearTimeout(t); t = setTimeout(loadPlan, 400);
});
$$("#pCheck input").forEach(i => i.onchange = updatePlannerLock);

function checklist() { const o = {}; $$("#pCheck input").forEach(i => o[i.dataset.k] = i.checked); return o; }

function updatePlannerLock() {
  if (!D || !plan) return;
  const g = D.discipline, all = Object.values(checklist()).every(Boolean);
  const btn = $("#pLog");
  btn.disabled = !g.can_trade || plan.lots === 0 || !all;
  btn.textContent = !g.can_trade ? "Locked by Discipline Guard" : plan.lots === 0 ? "Size too big for your risk" : !all ? "Tick the checklist first" : "Log Trade";
}

$("#pLog").onclick = () => submitTrade(false);
$("#pForce").onclick = () => {
  if (confirm("Rules break chesi trade teeskunnava? Journal lo 'rules broken' ga save avuthundi.")) submitTrade(true);
};
async function submitTrade(force) {
  try {
    const res = await api("/api/trades", {method: "POST", body: JSON.stringify({
      symbol: plan.underlying, expiry: plan.expiry, strike: plan.strike, type: plan.opt_type,
      entry: +$("#pEntry").value, sl: +$("#pSl").value,
      checklist: checklist(), reason: $("#pReason").value, force})});
    showPMsg(`Logged #${res.id}: BUY ${res.plan.lots} lot ${res.plan.symbol}. SL ${num(res.plan.sl)} Kite lo pettu!`, false);
    $$("#pCheck input").forEach(i => i.checked = false);
    refreshNow();
  } catch (e) { showPMsg(e.message, true); }
}
function showPMsg(m, err) { const el = $("#pMsg"); el.textContent = m; el.className = "pmsg " + (err ? "err" : "ok"); }
function refreshNow() { api("/api/dashboard").then(d => { D = d; renderGuard(); renderOpen(); updatePlannerLock(); }); }

async function refreshSelected() {
  if (!selected || !sel) return;
  await loadChain(); renderChain();
  if (!userEdited) loadPlan();
}

/* ---------------- open trades + alerts ---------------- */
function renderOpen() {
  const rows = D.open_trades;
  $("#openEmpty").classList.toggle("hidden", rows.length > 0);
  $("#open tbody").innerHTML = rows.map(t => `<tr>
    <td><b>${t.symbol}</b></td>
    <td class="r">${t.lots}</td><td class="r">${num(t.entry)}</td><td class="r">${num(t.sl)}</td>
    <td class="r">${num(t.t1)} / ${num(t.t2)}</td><td class="r">${num(t.ltp)}</td>
    <td class="r ${cls(t.upnl)}">${inr(t.upnl)}</td><td class="r ${cls(t.r_now)}">${num(t.r_now)}R</td>
    <td class="r">${num(t.spot)} / <span class="dn">${num(t.u_sl)}</span></td>
    <td>${t.alert ? `<span class="alert ${t.alert.includes("SL") ? "sl" : "t"}">${t.alert}</span>` : ""}</td>
    <td><button class="btn small" data-close="${t.id}">Close</button></td></tr>`).join("");
  $$("[data-close]").forEach(b => b.onclick = () => openClose(+b.dataset.close));
  rows.forEach(t => {
    if (t.alert && lastAlerts[t.id] !== t.alert) notify(`${t.symbol}: ${t.alert}`, t.alert.includes("SL"));
    lastAlerts[t.id] = t.alert;
  });
}

function notify(msg, urgent) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(); o.frequency.value = urgent ? 880 : 520; o.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + (urgent ? 0.6 : 0.25));
  } catch (e) {}
  if ("Notification" in window && Notification.permission === "granted") new Notification("F&O Cockpit", {body: msg});
}
$("#notifBtn").onclick = () => "Notification" in window && Notification.requestPermission().then(p => $("#notifBtn").textContent = p === "granted" ? "Alerts on" : "Alerts blocked");

/* ---------------- close modal ---------------- */
async function openClose(id) {
  closingId = id;
  const t = D.open_trades.find(x => x.id === id);
  if (!tags.length) tags = (await api("/api/journal")).tags;
  $("#mTitle").textContent = `Close ${t.symbol}`;
  $("#mExit").value = t.ltp ?? "";
  $("#mNotes").value = "";
  $("#mTags").innerHTML = tags.map(x => `<button class="chip" data-tag="${x}">${x}</button>`).join("");
  $$("#mTags .chip").forEach(c => c.onclick = () => c.classList.toggle("active"));
  $("#modal").classList.remove("hidden");
}
$("#mCancel").onclick = () => $("#modal").classList.add("hidden");
$("#mSave").onclick = async () => {
  const mistakes = $$("#mTags .chip.active").map(c => c.dataset.tag);
  try {
    const r = await api(`/api/trades/${closingId}/close`, {method: "POST",
      body: JSON.stringify({exit_price: +$("#mExit").value || null, mistakes, notes: $("#mNotes").value})});
    $("#modal").classList.add("hidden");
    alert(`Closed: ${inr(r.pnl)} (${r.r_multiple}R)` + (r.pnl < 0 ? "\nCooldown start ayyindi. Break teesko." : ""));
    refreshNow();
  } catch (e) { alert(e.message); }
};

/* ---------------- journal ---------------- */
async function loadJournal() {
  const j = await api("/api/journal");
  tags = j.tags;
  const s = j.stats;
  $("#stats").innerHTML = s.closed ? [
    ["Closed trades", s.closed], ["Win rate", s.win_rate + "%"],
    ["Total P&L", `<span class="${cls(s.total_pnl)}">${inr(s.total_pnl)}</span>`], ["Avg R", `<span class="${cls(s.avg_r)}">${s.avg_r}R</span>`],
  ].map(([k, v]) => `<div class="stat"><div class="muted">${k}</div><div class="v">${v}</div></div>`).join("")
    : `<div class="stat" style="grid-column:1/-1">Inka closed trades levu. Trades log chesi close chesaka ikkada nee patterns kanipistayi.</div>`;
  if (s.closed) {
    const box = (title, x) => `<div><div class="muted">${title}</div><div class="v"><b>${x.count}</b> trades</div>
      <div>Avg <b class="${cls(x.avg_r)}">${x.avg_r ?? "—"}R</b></div><div>P&L <b class="${cls(x.pnl)}">${inr(x.pnl)}</b></div></div>`;
    $("#ruleCmp").innerHTML = `<div class="cmp">${box("Rules followed", s.rules_followed)}${box("Rules broken", s.rules_broken)}</div>`;
    $("#mistakes").innerHTML = s.mistakes.length ? s.mistakes.map(m =>
      `<div class="mrow"><span>${m.tag} <span class="muted">×${m.count}</span></span><b class="${cls(m.pnl)}">${inr(m.pnl)}</b></div>`).join("")
      : `<p class="muted">No mistakes tagged yet.</p>`;
  } else { $("#ruleCmp").innerHTML = ""; $("#mistakes").innerHTML = ""; }
  $("#jTable tbody").innerHTML = j.trades.map(t => `<tr>
    <td>${t.opened_at.replace("T", " ").slice(0, 16)}</td><td><b>${t.symbol}</b></td>
    <td class="up">${t.side}</td><td class="r">${num(t.entry)}</td>
    <td class="r">${t.exit_price ? num(t.exit_price) : "open"}</td><td class="r ${cls(t.pnl)}">${t.pnl != null ? inr(t.pnl) : "—"}</td>
    <td class="r">${t.r_multiple != null ? t.r_multiple + "R" : "—"}</td>
    <td>${t.rules_ok ? '<span class="up">✓</span>' : '<span class="dn">✗</span>'}</td>
    <td>${t.mistakes.map(m => `<span class="tagpill">${m}</span>`).join("")}</td>
    <td class="muted">${t.reason || ""}</td></tr>`).join("") || `<tr><td colspan="10" class="muted">No trades yet.</td></tr>`;
}

/* ---------------- rules ---------------- */
const RULE_LABELS = {
  capital: "Trading capital (₹)", risk_per_trade_pct: "Max risk per trade (% of capital)",
  max_trades_per_day: "Max trades per day", max_daily_loss_pct: "Daily loss limit (% of capital)",
  cooldown_minutes: "Cooldown after a loss (minutes)", max_open_positions: "Max open positions",
  atr_sl_mult: "Stock SL = ATR ×", min_score: "Min |score| to trade", max_premium_pct: "Max premium per trade (% of capital)", refresh_seconds: "Refresh every (seconds)",
};
function renderRules() {
  $("#rulesForm").innerHTML = Object.entries(RULE_LABELS).map(([k, l]) =>
    `<label>${l}<input name="${k}" type="number" step="any" value="${D.rules[k]}"></label>`).join("");
}
$("#saveRules").onclick = async () => {
  const body = {}; $$("#rulesForm input").forEach(i => body[i.name] = i.value);
  D.rules = await api("/api/settings", {method: "POST", body: JSON.stringify(body)});
  $("#rulesMsg").textContent = "Saved.";
  setTimeout(() => $("#rulesMsg").textContent = "", 2000);
};

refresh();
