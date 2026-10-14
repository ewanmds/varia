"use strict";

const state = { provider: null, account: "", token: "", approved: false };

const $ = (id) => document.getElementById(id);

// Escapes text coming from the server (market names, states, incident messages, ...) before
// it is interpolated into an innerHTML template. Any string that did not originate from our
// own fmt* formatters must go through this before landing in an innerHTML string.
function esc(v) {
  return String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function pickProvider() {
  // Prefer EIP-6963 announced providers, fall back to window.ethereum.
  if (window.__eip6963 && window.__eip6963.length) return window.__eip6963[0].provider;
  return window.ethereum || null;
}

// Collect EIP-6963 providers.
window.__eip6963 = [];
window.addEventListener("eip6963:announceProvider", (e) => {
  window.__eip6963.push(e.detail);
});
window.dispatchEvent(new Event("eip6963:requestProvider"));

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (state.token) headers["x-auth-token"] = state.token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
  return data;
}

function setWalletPill(connected) {
  const pill = $("wallet_pill");
  if (connected) {
    pill.textContent = state.account.slice(0, 6) + "..." + state.account.slice(-4);
    pill.className = "pill pill--on";
  } else {
    pill.textContent = "Wallet non connecté";
    pill.className = "pill pill--off";
  }
}

// -- Step 1: connect + auth -------------------------------------------------

$("btn_connect").addEventListener("click", async () => {
  try {
    const provider = pickProvider();
    if (!provider) { alert("Aucun wallet détecté (installe MetaMask)."); return; }
    state.provider = provider;
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    state.account = (accounts[0] || "").toLowerCase();
    if (!state.account) throw new Error("Aucun compte.");

    const challenge = await api("/api/auth/challenge", {
      method: "POST", body: JSON.stringify({ account_address: state.account }),
    });
    const signature = await provider.request({
      method: "personal_sign", params: [challenge.message, state.account],
    });
    const verified = await api("/api/auth/verify", {
      method: "POST",
      body: JSON.stringify({ account_address: state.account, nonce: challenge.nonce, signature }),
    });
    state.token = verified.auth_token;

    $("account_addr").textContent = state.account;
    $("account_row").classList.remove("hidden");
    setWalletPill(true);
    $("btn_approve").disabled = false;
    await refreshAgentStatus();
  } catch (e) {
    alert("Connexion échouée : " + e.message);
  }
});

// -- Step 2: approve agent --------------------------------------------------

// Hyperliquid signe ses actions utilisateur avec le chainId 0x66eee (Arbitrum Sepolia) :
// le wallet doit être sur ce réseau, sinon il refuse la signature EIP-712.
const HL_SIGNATURE_CHAIN = {
  chainId: "0x66eee",
  chainName: "Arbitrum Sepolia",
  nativeCurrency: { name: "ETH", symbol: "ETH", decimals: 18 },
  rpcUrls: ["https://sepolia-rollup.arbitrum.io/rpc"],
  blockExplorerUrls: ["https://sepolia.arbiscan.io"],
};

async function ensureSignatureChain(provider) {
  const current = String(await provider.request({ method: "eth_chainId" })).toLowerCase();
  if (current === HL_SIGNATURE_CHAIN.chainId) return;
  try {
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: HL_SIGNATURE_CHAIN.chainId }] });
  } catch (err) {
    if (Number(err && err.code) !== 4902) throw err;
    await provider.request({ method: "wallet_addEthereumChain", params: [HL_SIGNATURE_CHAIN] });
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: HL_SIGNATURE_CHAIN.chainId }] });
  }
}

$("btn_approve").addEventListener("click", async () => {
  try {
    await ensureSignatureChain(state.provider);
    const challenge = await api("/api/agent/approve_challenge", {
      method: "POST", body: JSON.stringify({ account_address: state.account }),
    });
    const signature = await state.provider.request({
      method: "eth_signTypedData_v4",
      params: [state.account, JSON.stringify(challenge.typed_data)],
    });
    await api("/api/agent/approve_submit", {
      method: "POST",
      body: JSON.stringify({ account_address: state.account, nonce: challenge.nonce, signature }),
    });
    await refreshAgentStatus();
  } catch (e) {
    alert("Approbation échouée : " + e.message);
  }
});

async function refreshAgentStatus() {
  try {
    const s = await api("/api/agent/status?account_address=" + state.account);
    $("agent_status_val").textContent = s.message || (s.approved ? "Agent approuvé." : "(en attente)");
    state.approved = !!s.approved;
    $("btn_start").disabled = !state.approved;
  } catch (e) {
    $("agent_status_val").textContent = "Erreur : " + e.message;
  }
}

// -- Step 3: start / stop / kill ---------------------------------------------

// L0 : le setup dure environ 20 s ; le bouton reste désactivé pendant tout ce temps (le
// 23/09, plusieurs clics pendant le setup avaient lancé plusieurs bots en parallèle).
state.starting = false;
$("btn_start").addEventListener("click", async () => {
  if (state.starting) return;
  state.starting = true;
  $("btn_start").disabled = true;
  showProgress("Démarrage du bot", "Préparation");
  try {
    await api("/api/bot/start", {
      method: "POST", body: JSON.stringify({ account_address: state.account }),
    });
    $("btn_stop").disabled = false;
  } catch (e) {
    $("btn_start").disabled = false;
    alert("Démarrage échoué : " + e.message);
  } finally {
    state.starting = false;
    showProgress(null);
  }
});

// Boîte de progression partagée : démarrage, arrêt, arrêt d'urgence (variante rouge).
// title = null pour la masquer. Textes posés via textContent (jamais innerHTML).
function showProgress(title, step, opts) {
  const box = $("start_progress");
  if (!title) { box.classList.add("hidden"); state.progressError = false; return; }
  const o = opts || {};
  $("start_title").textContent = title;
  $("start_step").textContent = step || "";
  box.classList.toggle("start-progress--danger", !!o.danger);
  box.classList.toggle("start-progress--error", !!o.error);
  state.progressError = !!o.error;
  box.classList.remove("hidden");
}

// Arrêt propre ou d'urgence en cours côté interface : "stop", "kill" ou null. L'animation reste
// visible jusqu'à la confirmation du serveur (requête terminée ET statut à jour).
state.stopping = null;
state.stopAcked = false;

// L'arrêt d'urgence reste disponible pendant un arrêt normal (qui ferme les positions et peut
// durer jusqu'à stop_flatten_timeout_s, 120 s par défaut) ;
// seul un arrêt d'urgence déjà en cours le désactive.
function setControlsBusy(kill) {
  $("btn_start").disabled = true;
  $("btn_stop").disabled = true;
  if (kill) $("btn_kill").disabled = true;
}

function stopFailed(title, danger, e) {
  state.stopping = null;
  state.stopAcked = false;
  showProgress(title, "Échec : " + e.message, { danger: danger, error: true });
  $("btn_kill").disabled = false;
  pollStatus();
}

$("btn_stop").addEventListener("click", async () => {
  if (state.stopping) return;
  state.stopping = "stop";
  state.stopAcked = false;
  setControlsBusy(false);
  showProgress("Arrêt du bot", "Annulation des ordres HL");
  try {
    await api("/api/bot/stop", { method: "POST", body: JSON.stringify({}) });
    state.stopAcked = true;
    pollStatus();
  } catch (e) {
    stopFailed("Arrêt du bot", false, e);
  }
});

$("btn_kill").addEventListener("click", async () => {
  if (state.stopping === "kill") return;
  if (!confirm("Tout fermer sur les deux venues ?")) return;
  state.stopping = "kill";
  state.stopAcked = false;
  setControlsBusy(true);
  showProgress("Arrêt d'urgence", "Annulation des ordres HL", { danger: true });
  try {
    await api("/api/bot/kill", { method: "POST", body: JSON.stringify({}) });
    state.stopAcked = true;
    pollStatus();
  } catch (e) {
    stopFailed("Arrêt d'urgence", true, e);
  }
});

// -- Seuil d'inefficience global -------------------------------------------------

// Vrai dès que l'utilisateur touche le curseur : le statut ne l'écrase plus jusqu'à « Appliquer ».
state.edgeDirty = false;
state.edgeLoaded = false;

function setEdgeSlider(v) {
  $("edge_slider").value = String(v);
  $("edge_val").textContent = fmtNum(v, 1);
}

$("edge_slider").addEventListener("input", () => {
  state.edgeDirty = true;
  $("edge_val").textContent = fmtNum(parseFloat($("edge_slider").value), 1);
  $("edge_msg").textContent = "";
});

$("btn_edge_apply").addEventListener("click", async () => {
  const bps = parseFloat($("edge_slider").value);
  if (!(bps >= 1 && bps <= 20)) { $("edge_msg").textContent = "Valeur hors de [1, 20] pb"; return; }
  try {
    const r = await api("/api/params/min_edge", { method: "POST", body: JSON.stringify({ bps: bps }) });
    state.edgeDirty = false;
    setEdgeSlider(r.min_edge_bps);
    $("edge_msg").textContent = "Appliqué à tous les marchés";
  } catch (e) {
    $("edge_msg").textContent = "Refusé : " + e.message;
  }
});

// -- Formatting ---------------------------------------------------------

function fmtUsd(v) {
  if (v === null || v === undefined) return "-";
  return Number(v).toLocaleString("fr-FR", { maximumFractionDigits: 2 }) + " $";
}
function fmtNum(v, digits) {
  if (v === null || v === undefined) return "-";
  return Number(v).toLocaleString("fr-FR", { maximumFractionDigits: digits === undefined ? 4 : digits });
}
function fmtAge(v) {
  if (v === null || v === undefined) return "-";
  return Number(v).toLocaleString("fr-FR", { maximumFractionDigits: 1 }) + " s";
}
function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleTimeString("fr-FR");
}

const STATE_LABEL = { cote: "cote", couverture: "couverture", pause: "pause", bloqué: "bloqué", sortie: "sortie", arrêt: "arrêt" };
const STATE_CLASS = { cote: "st-green", couverture: "st-blue", pause: "st-orange", sortie: "st-orange", bloqué: "st-red", "arrêt": "st-muted" };

function stateClass(st) {
  return STATE_CLASS[st] || "st-muted";
}

// -- Market rows (kept alive across polls so inputs aren't wiped) -----------

const marketRows = new Map();

function marketRow(m) {
  const tr = document.createElement("tr");
  tr.dataset.asset = m.asset;
  tr.innerHTML =
    `<td class="c-asset"></td>` +
    `<td class="c-mode"><select class="p-mode">` +
    `<option value="chase">chase</option><option value="lag_taker">lag_taker</option>` +
    `<option value="maker">maker</option></select></td>` +
    `<td class="c-state"></td><td class="c-reason"></td>` +
    `<td class="c-edge"></td>` +
    `<td class="c-var"></td><td class="c-age"></td><td class="c-wsage"></td><td class="c-hl"></td>` +
    `<td class="c-bboage"></td>` +
    `<td class="c-orders"></td><td class="c-poshl"></td><td class="c-posvar"></td>` +
    `<td class="c-delta"></td><td class="c-basis"></td><td class="c-basisfast"></td><td class="c-pending"></td>` +
    `<td class="c-params">` +
    `<input class="p-edge" type="number" step="0.5" min="1" max="20" placeholder="marge pb" />` +
    `<input class="p-fill" type="number" step="1" min="0" placeholder="taille $" />` +
    `<input class="p-maxpos" type="number" step="1" min="0" placeholder="pos max $" />` +
    `<button class="btn btn--small p-apply">Appliquer</button></td>` +
    `<td class="c-actions"><button class="btn btn--small p-toggle">On/Off</button>` +
    `<button class="btn btn--small p-unblock hidden">Débloquer</button></td>`;

  tr.querySelector(".p-apply").addEventListener("click", async () => {
    const body = {};
    const edge = tr.querySelector(".p-edge").value;
    const fill = tr.querySelector(".p-fill").value;
    const maxpos = tr.querySelector(".p-maxpos").value;
    if (edge !== "") {
      const v = parseFloat(edge);
      if (!(v >= 1 && v <= 20)) { alert("La marge doit être comprise entre 1 et 20 pb."); return; }
      body.min_edge_bps = v;
    }
    if (fill !== "") body.fill_usd = parseFloat(fill);
    if (maxpos !== "") body.max_position_usd = parseFloat(maxpos);
    try {
      await api(`/api/market/${m.asset}/params`, { method: "POST", body: JSON.stringify(body) });
    } catch (e) {
      alert("Paramètres refusés : " + e.message);
    }
  });
  // L5 : mode d'entrée modifiable à chaud (annule les ordres de l'ancien mode).
  const modeSel = tr.querySelector(".p-mode");
  modeSel.addEventListener("change", async () => {
    const mode = modeSel.value;
    if (mode === "lag_taker" &&
        !confirm("Mode prise sur retard : le bot enverra des ordres IOC (taker) sur HL. Continuer ?")) {
      modeSel.value = modeSel.dataset.current || "chase";
      return;
    }
    try {
      await api(`/api/market/${encodeURIComponent(m.asset)}/entry_mode`,
        { method: "POST", body: JSON.stringify({ mode: mode }) });
      modeSel.dataset.current = mode;
    } catch (e) {
      alert("Changement de mode refusé : " + e.message);
      modeSel.value = modeSel.dataset.current || "chase";
    }
  });
  tr.querySelector(".p-unblock").addEventListener("click", async () => {
    if (!confirm("Positions et delta vérifiés ? Le marché reprendra la cotation.")) return;
    try {
      await api(`/api/market/${encodeURIComponent(m.asset)}/unblock`, { method: "POST" });
    } catch (e) {
      alert("Déblocage refusé : " + e.message);
    }
  });
  tr.querySelector(".p-toggle").addEventListener("click", async () => {
    try {
      await api(`/api/market/${m.asset}/toggle`, { method: "POST" });
    } catch (e) {
      alert("Bascule échouée : " + e.message);
    }
  });
  return tr;
}

function renderMarkets(markets) {
  const tbody = $("markets").querySelector("tbody");
  const seen = new Set();
  (markets || []).forEach((m) => {
    seen.add(m.asset);
    let tr = marketRows.get(m.asset);
    if (!tr) {
      tr = marketRow(m);
      marketRows.set(m.asset, tr);
      tbody.appendChild(tr);
    }
    tr.querySelector(".c-asset").textContent = m.asset + (m.enabled === false ? " (off)" : "");
    const modeSel = tr.querySelector(".p-mode");
    if (m.entry_mode && document.activeElement !== modeSel) {
      modeSel.value = m.entry_mode;
      modeSel.dataset.current = m.entry_mode;
    }
    const lag = m.lag || {};
    modeSel.title = m.entry_mode === "lag_taker"
      ? `prises ${lag.taken || 0}, remplies ${lag.filled || 0}, couvertes ${lag.hedged || 0}, signaux ${lag.signals || 0}`
      : "";
    const edgeEl = tr.querySelector(".c-edge");
    edgeEl.textContent = fmtNum(m.edge_now_bps, 1) + " / " + fmtNum(m.min_edge_bps, 1);
    const reached = m.edge_now_bps !== null && m.edge_now_bps !== undefined &&
      m.min_edge_bps !== null && m.min_edge_bps !== undefined && m.edge_now_bps >= m.min_edge_bps;
    edgeEl.className = "c-edge " + (reached ? "st-green" : "st-muted");
    edgeEl.title = m.edge_now_side === "buy" ? "côté achat HL" : (m.edge_now_side === "sell" ? "côté vente HL" : "");
    const stEl = tr.querySelector(".c-state");
    stEl.textContent = STATE_LABEL[m.state] || m.state;
    stEl.className = "c-state " + stateClass(m.state);
    tr.querySelector(".c-reason").textContent = m.reason || "-";
    tr.querySelector(".c-var").textContent = fmtNum(m.var_bid, 4) + " / " + fmtNum(m.var_ask, 4);
    tr.querySelector(".c-age").textContent = fmtAge(m.var_age);
    tr.querySelector(".c-var").title = m.var_ref === "ws" ? "référence : prix websocket Var" :
      (m.var_ref === "rfq" ? "référence : cotation RFQ" : "");
    tr.querySelector(".c-wsage").textContent = fmtAge(m.var_ws_age);
    tr.querySelector(".c-bboage").textContent = fmtAge(m.hl_bbo_age);
    tr.querySelector(".c-hl").textContent = fmtNum(m.hl_bid, 4) + " / " + fmtNum(m.hl_ask, 4);
    tr.querySelector(".c-orders").textContent = fmtNum(m.buy_px, 4) + " / " + fmtNum(m.sell_px, 4);
    tr.querySelector(".c-poshl").textContent = fmtNum(m.pos_hl, 4);
    tr.querySelector(".c-posvar").textContent = fmtNum(m.pos_var, 4);
    tr.querySelector(".c-delta").textContent = fmtUsd(m.delta_usd);
    tr.querySelector(".c-basis").textContent = fmtNum(m.basis, 4);
    tr.querySelector(".c-basisfast").textContent = fmtNum(m.basis_fast, 4);
    const pendEl = tr.querySelector(".c-pending");
    pendEl.textContent = fmtNum(m.pending_qty, 4);
    pendEl.style.color = m.pending_qty ? "var(--danger)" : "";
    tr.querySelector(".p-toggle").textContent = m.enabled === false ? "Activer" : "Désactiver";
    tr.querySelector(".p-unblock").classList.toggle("hidden", m.state !== "bloqué");
  });
  // Remove rows for markets that disappeared (bot restarted with a different config).
  for (const [asset, tr] of marketRows) {
    if (!seen.has(asset)) {
      tr.remove();
      marketRows.delete(asset);
    }
  }
}

const ARBS_HEAD = "<tr><th>Heure</th><th>Marché</th><th>HL</th><th>Var</th><th>Écart</th><th>PnL</th><th>Statut</th></tr>";
const ORDERS_HEAD = "<tr><th>Heure</th><th>Actif</th><th>Sens</th><th>Prix</th><th>Quantité</th><th>Montant</th><th>Glissement</th></tr>";

// Mode paire : ordres de la paire (lignes pair_fill du journal). edge_bps = -glissement.
function renderOrders(arbs) {
  const table = $("arbs");
  if (table.dataset.mode !== "pair") { table.querySelector("thead").innerHTML = ORDERS_HEAD; table.dataset.mode = "pair"; }
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  (arbs || []).forEach((a) => {
    const tr = document.createElement("tr");
    const slip = a.edge_bps === null || a.edge_bps === undefined ? null : -a.edge_bps;
    tr.innerHTML =
      `<td>${fmtTime(a.ts)}</td><td>${esc(a.market)}</td><td>${a.var_side === "buy" ? "achat" : "vente"}</td>` +
      `<td>${fmtNum(a.var_px, 3)}</td><td>${fmtNum(a.var_qty, 3)}</td>` +
      `<td>${fmtUsd((a.var_px || 0) * (a.var_qty || 0))}</td><td>${fmtPb(slip)}</td>`;
    tbody.appendChild(tr);
  });
}

function renderArbs(arbs) {
  const table = $("arbs");
  if (table.dataset.mode === "pair") { table.querySelector("thead").innerHTML = ARBS_HEAD; table.dataset.mode = ""; }
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  (arbs || []).forEach((a) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${fmtTime(a.ts)}</td><td>${esc(a.market)}</td>` +
      `<td>${esc(a.hl_side)} ${fmtNum(a.hl_px, 4)} x ${fmtNum(a.hl_sz, 4)}</td>` +
      `<td>${esc(a.var_side)} ${fmtNum(a.var_px, 4)} x ${fmtNum(a.var_qty, 4)}</td>` +
      `<td>${fmtNum(a.edge_bps, 2)} pb</td><td>${fmtUsd(a.pnl_usd)}</td><td>${esc(a.status)}</td>`;
    tbody.appendChild(tr);
  });
}

function renderIncidents(incidents) {
  const tbody = $("incidents").querySelector("tbody");
  tbody.innerHTML = "";
  (incidents || []).forEach((i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${fmtTime(i.ts)}</td><td>${esc(i.market)}</td><td>${esc(i.level)}</td><td>${esc(i.msg)}</td>`;
    tbody.appendChild(tr);
  });
}

// -- Paire pétrole ------------------------------------------------------------

const PAIR_CLASS = { "ACTIF": "st-green", "PAUSE": "st-orange", "BLOQUÉ": "st-red", "ARRÊTÉ": "st-muted", "ATTENTE": "st-blue" };
state.pairDirty = false;

["pair_leg_usd", "pair_cycle_min", "pair_volume_cycle_min", "pair_loss"].forEach((id) => {
  $(id).addEventListener("input", () => { state.pairDirty = true; $("pair_msg").textContent = ""; });
});

$("btn_pair_apply").addEventListener("click", async () => {
  const body = {
    leg_usd: parseFloat($("pair_leg_usd").value),
    cycle_min: parseFloat($("pair_cycle_min").value),
    volume_cycle_min: parseFloat($("pair_volume_cycle_min").value),
    daily_loss_usd: parseFloat($("pair_loss").value),
  };
  if (Object.values(body).some((v) => !isFinite(v))) { $("pair_msg").textContent = "Valeurs manquantes"; return; }
  try {
    await api("/api/pair/params", { method: "POST", body: JSON.stringify(body) });
    state.pairDirty = false;
    $("pair_msg").textContent = "Appliqué (période au prochain décompte, taille au prochain cycle)";
  } catch (e) {
    $("pair_msg").textContent = "Refusé : " + e.message;
  }
});

$("btn_pair_resume").addEventListener("click", async () => {
  try {
    await api("/api/pair/resume", { method: "POST" });
    $("pair_msg").textContent = "Débloquée";
  } catch (e) {
    $("pair_msg").textContent = "Refusé : " + e.message;
  }
});

function fmtPb(v) {
  return v === null || v === undefined ? "-" : fmtNum(v, 2) + " pb";
}
function fmtCountdown(sec) {
  if (sec === null || sec === undefined) return "-";
  const t = Math.round(sec);
  return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0");
}

function renderPair(p, s) {
  const card = $("pair_card");
  // Le tableau des marchés n'a pas de sens en mode paire.
  $("markets_card").classList.toggle("hidden", !!p);
  // En mode paire, le panneau reste visible avant le démarrage (état ARRÊTÉ).
  card.classList.toggle("hidden", !p && s.mode !== "pair");
  if (!p) {
    if (s.mode === "pair") {
      $("pair_state").textContent = "ARRÊTÉ";
      $("pair_state").className = "pair-state st-muted";
      $("pair_reason").textContent = "bot non démarré";
      $("btn_pair_resume").classList.add("hidden");
    }
    return;
  }
  const st = $("pair_state");
  st.textContent = p.state || "?";
  st.className = "pair-state " + (PAIR_CLASS[p.state] || "st-muted");
  $("pair_reason").textContent = p.reason || "";
  $("btn_pair_resume").classList.toggle("hidden", p.state !== "PAUSE" && p.state !== "BLOQUÉ");
  $("pair_side").textContent = p.side === "A" ? "A : short WTI / long Brent"
    : (p.side === "B" ? "B : long WTI / short Brent" : "aucun");
  const pos = p.pos_usd || {};
  $("pair_legs").textContent = "WTI " + fmtUsd(pos.wti) + " · Brent " + fmtUsd(pos.brent);
  const tvSource = p.signal_mode === "trend_hold" && p.trend_source === "tradingview";
  $("pair_ratio_label").textContent = tvSource ? "Ratio TradingView · clôture 1m" : "Ratio WTI / Brent";
  $("pair_ratio").textContent = fmtNum(tvSource ? p.signal_ratio : p.ratio, 4);
  const trend = p.trend;
  $("pair_z").textContent = p.signal_mode === "trend_hold"
    ? (trend && trend.ready ? "tendance " + fmtNum(trend.slope_bps_min, 2) + " pb/min · R² " + fmtNum(trend.r2, 2)
      : (p.warmup_entry_side ? "analyse tendance en cours · entrée immédiate autorisée" : "préchauffage tendance " + (trend ? trend.window_min : "?") + " min"))
    : (p.ready && p.z !== null && p.z !== undefined ? "z = " + fmtNum(p.z, 2) : "préchauffage");
  $("pair_next_label").textContent = p.signal_mode === "trend_hold" ? "Prochain contrôle" : "Prochain cycle";
  const heldDue = p.signal_mode === "trend_hold" && p.side && p.last_cycle_at && p.params
    ? Math.max(0, p.last_cycle_at + p.params.cycle_min * 60 - Date.now() / 1000) : 0;
  $("pair_next").textContent = fmtCountdown(Math.max(p.next_cycle_in_s || 0, heldDue));
  $("pair_next_volume").textContent = p.volume_cycle_min > 0
    ? (p.next_volume_cycle_in_s == null ? "attente position" : fmtCountdown(p.next_volume_cycle_in_s)) : "désactivé";
  $("pair_cycles").textContent = String(p.cycles || 0);
  $("pair_vol_session").textContent = fmtUsd(p.volume_session_usd);
  $("pair_vol_day").textContent = fmtUsd(p.volume_day_usd);
  $("pair_pnl").textContent = fmtUsd(p.pnl_session_usd);
  $("pair_pnl_sub").textContent = "non réalisé " + fmtUsd(p.pnl_unrealized_usd);
  $("pair_pnl_day").textContent = fmtUsd(s.daily_pnl);
  const cost = p.cost_per_m;
  $("pair_cost").textContent = cost === null || cost === undefined ? "-" : fmtNum(cost, 1) + " $";
  $("pair_cost_box").className = "light " + (cost === null || cost === undefined ? "" : (cost > 80 ? "st-red" : (cost < 70 ? "st-green" : "")));
  $("pair_slip").textContent = fmtPb(p.avg_slip_bps);
  $("pair_half").textContent = fmtPb(p.avg_half_spread_bps);
  $("pair_signal_mode").textContent = p.signal_mode === "trend_hold"
    ? (tvSource ? "TradingView 1m · " + (trend ? trend.window_min : "?") + " min" : "Tendance / conservation")
    : "Volume / retour à la moyenne";
  if (p.volume_cycle_required && p.volume_cycle_min > 0) {
    $("pair_signal_mode").textContent += " · volume obligatoire " + p.volume_cycle_min + " min";
  }
  $("pair_quote_cost").textContent = fmtPb(p.quoted_roundtrip_cost_bps);
  const prm = p.params;
  if (prm && !state.pairDirty) {
    $("pair_leg_usd").value = prm.leg_usd;
    $("pair_cycle_min").value = prm.cycle_min;
    $("pair_volume_cycle_min").value = prm.volume_cycle_min || 0;
    $("pair_loss").value = prm.daily_loss_usd;
  }
}

// -- Status polling ---------------------------------------------------------

async function pollStatus() {
  try {
    const s = await api("/api/status");

    const banner = $("mode_banner");
    if (s.paper) {
      banner.textContent = "MODE PAPER";
      banner.className = "mode-banner mode-banner--paper";
    } else {
      banner.textContent = "MODE RÉEL";
      banner.className = "mode-banner mode-banner--live";
    }

    const pairMode = s.mode === "pair";
    document.body.classList.toggle("mode-pair", pairMode);
    $("app_title").innerHTML = pairMode
      ? 'Bot paire pétrole <span class="muted">USOILP / UKOILP · Var</span>'
      : 'Bot arbitrage <span class="muted">HL ↔ Var</span>';
    $("arbs_title").textContent = pairMode ? "Derniers ordres de la paire" : "Derniers arbitrages";
    document.title = pairMode ? "Bot paire pétrole" : "Bot arbitrage HL ↔ Var";
    $("step_launch").textContent = pairMode ? "2" : "3";

    renderPair(s.pair, s);
    renderMarkets(s.markets);
    if (pairMode) renderOrders(s.arbs); else renderArbs(s.arbs);
    renderIncidents(s.incidents);

    $("light_session").querySelector(".light-val").textContent = s.session_ok === true ? "OK" : (s.session_ok === false ? "coupée" : "à vérifier");
    $("light_session").className = "light " + (s.session_ok === true ? "st-green" : (s.session_ok === false ? "st-red" : "st-muted"));
    $("light_ws").querySelector(".light-val").textContent = s.hl_ws_ok ? "OK" : "muet";
    $("light_ws").className = "light " + (s.hl_ws_ok ? "st-green" : "st-red");
    $("light_strikes").querySelector(".light-val").textContent =
      String(s.strikes || 0) + " (10 min) / " + String(s.strikes_24h === null || s.strikes_24h === undefined ? "?" : s.strikes_24h) + " (24 h)";
    $("light_strikes").className = "light " + (s.quotes_suspended ? "st-red" : "st-muted");
    $("light_margin").querySelector(".light-val").textContent =
      fmtNum((s.hl_margin_ratio || 0) * 100, 1) + " %";
    const rate = s.hl_rate;
    $("light_hlrate").querySelector(".light-val").textContent =
      rate ? fmtNum(rate.headroom, 0) + (s.hl_cancel_only ? " (annulation seule)" : "") : "?";
    $("light_hlrate").className = "light " + (s.hl_cancel_only ? "st-red" : (rate ? "st-green" : "st-muted"));
    $("light_balance").querySelector(".light-val").textContent = fmtUsd(s.var_balance);
    const sp = s.session_pnl;
    $("light_session_pnl").querySelector(".light-val").textContent = fmtUsd(sp);
    $("light_session_pnl").className = "light " + (sp === null || sp === undefined ? "st-muted" : (sp < 0 ? "st-red" : "st-green"));
    $("session_sub").textContent = s.session_started_at
      ? "depuis " + fmtTime(s.session_started_at) + " · " + (s.session_trades || 0) + " ligne(s)" : "";
    const pnlEl = $("light_pnl").querySelector(".light-val");
    pnlEl.textContent = fmtUsd(s.daily_pnl);
    $("light_pnl").className = "light " + ((s.daily_pnl || 0) < 0 ? "st-red" : "st-green");

    // emergency === false : arrêt normal (bouton « Arrêter » ou fichier STOP) qui ferme les
    // positions ; même état « arrêté » côté serveur, mais ce n'est pas un arrêt d'urgence.
    const emergency = !!s.killed && s.emergency !== false;
    if (s.killed) {
      $("s_error").textContent = emergency
        ? "Arrêt d'urgence déclenché : " + (s.kill_reason || "")
        : "Arrêt demandé : fermeture des positions en cours (nouveaux essais automatiques)";
      $("s_error").classList.remove("hidden");
    } else {
      $("s_error").classList.add("hidden");
    }

    if (!state.edgeDirty && s.min_edge_bps !== null && s.min_edge_bps !== undefined) {
      setEdgeSlider(s.min_edge_bps);
      state.edgeLoaded = true;
    }

    if (state.stopping === "stop") {
      if (s.stopping) showProgress("Arrêt du bot", s.stopping);
      if (state.stopAcked && !s.running) { state.stopping = null; state.stopAcked = false; showProgress(null); }
    } else if (state.stopping === "kill") {
      if (s.stopping) showProgress("Arrêt d'urgence", s.stopping, { danger: true });
      if (state.stopAcked && (s.killed || !s.running)) { state.stopping = null; state.stopAcked = false; showProgress(null); }
    } else if (s.stopping) {
      showProgress(emergency ? "Arrêt d'urgence" : "Arrêt du bot", s.stopping, { danger: emergency });
    } else if (s.starting) {
      showProgress("Démarrage du bot", s.starting);
    } else if (!state.starting && !state.progressError) {
      showProgress(null);
    }

    const busy = !!state.stopping || !!s.stopping;
    // L'arrêt d'urgence reste disponible pendant un arrêt normal, même quand celui-ci ferme les
    // positions (killed vrai, emergency faux).
    const killing = state.stopping === "kill" || (!!s.stopping && emergency);
    // Mode paire : aucun agent HL, seul le wallet connecté (session signée) est exigé.
    const ready = pairMode ? !!state.token : state.approved;
    $("btn_start").disabled = busy || s.running || state.starting || !!s.starting || !ready;
    $("btn_stop").disabled = busy || !s.running;
    $("btn_kill").disabled = killing;
  } catch (_) { /* ignore transient */ }
}

setInterval(pollStatus, 1000);
pollStatus();
