const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function harness(tabs = [{id: 7, url: 'https://omni.variational.io/'}]) {
  const executions = [];
  const current = {url: 'https://omni.variational.io/'};
  const context = vm.createContext({
    importScripts() {}, BOT_BRIDGE: {ports: [8792], token: 'test'},
    WebSocket: class {}, URL, setTimeout() {}, setInterval() {}, clearTimeout() {}, clearInterval() {},
    chrome: {
      tabs: {query: async () => tabs, get: async (id) => {
        if (!tabs.some(tab => tab.id === id)) throw new Error('No tab with id: ' + id);
        return current;
      }},
      scripting: {executeScript: async (request) => {
        executions.push(request); return [{result: [200, {ok: true}]}];
      }},
      action: {setBadgeText() {}, setTitle() {}, onClicked: {addListener() {}}},
      alarms: {create() {}, onAlarm: {addListener() {}}},
      runtime: {onStartup: {addListener() {}}, onInstalled: {addListener() {}}}
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser_extension/background.js'), 'utf8'), context);
  return {context, executions, current};
}

test('routes allowed request only to pinned Omni main frame', async () => {
  const h = harness();
  const result = await vm.runInContext('perform({method:"GET",path:"/api/me",body:null})', h.context);
  assert.equal(JSON.stringify(result), '[200,{"ok":true}]');
  assert.equal(h.executions[0].target.tabId, 7);
  assert.equal(h.executions[0].world, 'MAIN');
  h.current.url = 'https://example.com/';
  await assert.rejects(vm.runInContext('perform({method:"GET",path:"/api/me",body:null})', h.context), /fermé ou changé/);
  assert.equal(h.executions.length, 1);
});

test('rejects unexpected endpoints before executing any browser script', async () => {
  const h = harness();
  await assert.rejects(vm.runInContext('perform({method:"POST",path:"/api/withdraw",body:{}})', h.context), /interdite/);
  assert.equal(h.executions.length, 0);
});

test('does not silently choose a wallet from multiple Omni tabs', async () => {
  const h = harness([{id: 7}, {id: 8}]);
  await assert.rejects(vm.runInContext('perform({method:"GET",path:"/api/me",body:null})', h.context), /un seul onglet/);
  assert.equal(h.executions.length, 0);
});

test('explicit startup reselects a replacement tab, ongoing requests do not', async () => {
  const tabs = [{id: 7}];
  const h = harness(tabs);
  await vm.runInContext('perform({method:"GET",path:"/api/me",body:null})', h.context);
  tabs[0].id = 8;
  await assert.rejects(vm.runInContext('perform({method:"GET",path:"/api/positions",body:null})', h.context), /No tab/);
  await vm.runInContext('perform({method:"GET",path:"/api/me",body:null,new_session:true})', h.context);
  assert.equal(h.executions.length, 2);
  assert.equal(h.executions[1].target.tabId, 8);
});

test('a trading request cannot change the pinned session', async () => {
  const h = harness();
  await assert.rejects(vm.runInContext('perform({method:"POST",path:"/api/orders/new/market",body:{},new_session:true})', h.context), /interdite/);
  assert.equal(h.executions.length, 0);
});
