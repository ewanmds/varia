"use strict";
importScripts("bridge_config.js");
let socket = null;
let connecting = false;
let pinnedTab = null;
let heartbeat = null;

const paths = new Set([
  "GET /api/me", "GET /api/positions", "GET /api/portfolio",
  "GET /api/orders/v2", "GET /api/metadata/supported_assets",
  "POST /api/quotes/indicative", "POST /api/orders/new/market",
  "POST /api/quotes/accept", "POST /api/settlement_pools/set_leverage"
]);

function badge(text, title) {
  chrome.action.setBadgeText({text});
  chrome.action.setTitle({title});
}

async function perform(message) {
  if (!paths.has(message.method + " " + message.path)) throw new Error("Requête interdite");
  if (message.new_session) {
    if (message.method !== "GET" || message.path !== "/api/me") throw new Error("Sélection de session interdite");
    pinnedTab = null;
  }
  if (pinnedTab === null) {
    const tabs = await chrome.tabs.query({url: "https://omni.variational.io/*"});
    if (tabs.length !== 1) throw new Error("Garder un seul onglet Omni ouvert dans ce Chrome");
    pinnedTab = tabs[0].id;
  }
  const tab = await chrome.tabs.get(pinnedTab);
  if (!tab.url || new URL(tab.url).origin !== "https://omni.variational.io") {
    throw new Error("L'onglet Omni sélectionné a été fermé ou changé");
  }
  const rows = await chrome.scripting.executeScript({
    target: {tabId: pinnedTab}, world: "MAIN",
    args: [message.method, message.path, message.body],
    func: async (method, path, body) => {
      if (location.origin !== "https://omni.variational.io") throw new Error("Origine incorrecte");
      const options = {method, credentials: "include", headers: {"Content-Type": "application/json"}};
      if (body !== null) options.body = JSON.stringify(body);
      // Abort ne confirme jamais la non-exécution d'un ordre : le bot relit les positions.
      options.signal = AbortSignal.timeout(25000);
      const response = await fetch(path, options);
      let data = null;
      try { data = await response.json(); } catch (_) {}
      return [response.status, data];
    }
  });
  if (!rows[0] || !Array.isArray(rows[0].result)) throw new Error("Aucune réponse de l'onglet Omni");
  return rows[0].result;
}

async function connect() {
  if (connecting || (socket && socket.readyState < 2)) return;
  connecting = true;
  for (const port of BOT_BRIDGE.ports) {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/api/browser/bridge`);
    const accepted = await new Promise((resolve) => {
      const timeout = setTimeout(() => { ws.close(); resolve(false); }, 4000);
      ws.onopen = () => ws.send(JSON.stringify({token: BOT_BRIDGE.token}));
      ws.onmessage = async (event) => {
        let message;
        try { message = JSON.parse(event.data); } catch (_) { return; }
        if (message.type === "ready") {
          clearTimeout(timeout);
          socket = ws;
          badge("ON", `Bot pétrole : connecté au port ${port}`);
          heartbeat = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: "ping"}));
          }, 20000);
          resolve(true);
          return;
        }
        if (!message.id) return;
        let reply;
        try { reply = {id: message.id, result: await perform(message)}; }
        catch (error) { reply = {id: message.id, error: String(error.message || error)}; }
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(reply));
      };
      ws.onerror = () => { clearTimeout(timeout); resolve(false); };
      ws.onclose = () => {
        clearTimeout(timeout);
        if (socket === ws) {
          socket = null;
          clearInterval(heartbeat);
          badge("OFF", "Bot pétrole : serveur local déconnecté");
          setTimeout(connect, 3000);
        }
        resolve(false);
      };
    });
    if (accepted) break;
  }
  connecting = false;
  if (!socket) badge("OFF", "Bot pétrole : lancer start-real.cmd, puis cliquer ici");
}

chrome.alarms.create("reconnect", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.action.onClicked.addListener(async () => {
  if (socket) return;
  pinnedTab = null;
  await connect();
});
connect();
