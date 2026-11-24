const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class FakeElement {
  constructor() {
    this.textContent = '';
    this.innerHTML = '';
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.className = '';
    this.dataset = {};
    this.listeners = {};
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        if (force === undefined) force = !classes.has(name);
        if (force) classes.add(name);
        else classes.delete(name);
        return force;
      },
    };
    this.options = [];
  }

  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(option) { this.options.push(option); }
  replaceChildren() { this.children = []; this.innerHTML = ''; }
  querySelectorAll() { return []; }
}

function createConsole(fetchHandler) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeElement());
    return elements.get(id);
  };
  const powerButtons = ['start', 'shutdown', 'reboot'].map((action) => {
    const button = new FakeElement();
    button.dataset.action = action;
    return button;
  });
  const document = {
    getElementById: element,
    createElement: () => new FakeElement(),
    querySelectorAll: (selector) => selector === '.power-action' ? powerButtons : [],
  };
  const window = {
    crypto: { randomUUID: () => 'uuid-test' },
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 1,
    confirm: () => true,
  };
  const path = 'api/static/console/assets/console.js';
  const source = fs.readFileSync(path, 'utf8');
  const instrumented = source.replace(
    /\}\)\(\);\s*$/,
    'window.__consoleTest = { state, selectInstance, powerAction, reconcileInstance };\n})();',
  );
  assert.notEqual(instrumented, source, 'the test hook must match the console closure');
  vm.runInNewContext(instrumented, { document, window, fetch: fetchHandler });
  return { api: window.__consoleTest, elements, powerButtons };
}

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  };
}

test('borra la ficha anterior ante error de selección y permite reconciliar incertidumbre', async () => {
  const calls = [];
  const fetchHandler = async (path, options = {}) => {
    const method = options.method || 'GET';
    calls.push({ path, method });
    if (path === '/v1/instances/instance-a') {
      return response({
        instance_id: 'instance-a', name: 'VM A', status: 'ready', vmid: 40001,
        spec: { vcpus: 2, memory_mb: 2048, disk_gb: 50 },
      });
    }
    if (path === '/v1/instances/instance-a/power') {
      return response({ power_state: 'running', mutations_available: true });
    }
    if (path === '/v1/instances/instance-b') return response({ detail: 'fallo temporal' }, 503);
    if (path === '/v1/instances/instance-c') {
      return response({
        instance_id: 'instance-c', name: 'VM C', status: 'uncertain', vmid: 40003,
        spec: { vcpus: 2, memory_mb: 2048, disk_gb: 50 },
      });
    }
    if (path === '/v1/instances/instance-c/power') {
      return response({ detail: 'la observación está temporalmente bloqueada' }, 409);
    }
    if (path === '/v1/instances?status=all&limit=200') return response({ instances: [] });
    if (path === '/v1/instances/instance-c/reconcile' && method === 'POST') {
      return response({ operation_id: 'operation-c' });
    }
    if (path === '/v1/operations/operation-c') return response({ operation_id: 'operation-c', status: 'completed' });
    throw new Error(`Solicitud inesperada: ${method} ${path}`);
  };
  const { api, elements, powerButtons } = createConsole(fetchHandler);
  api.state.token = 'token-de-prueba';

  await api.selectInstance('instance-a');
  assert.equal(api.state.detailId, 'instance-a');
  assert.equal(elements.get('delete-button').disabled, false);

  await api.selectInstance('instance-b');
  assert.equal(api.state.selectedId, 'instance-b');
  assert.equal(api.state.detailId, null);
  assert.equal(elements.get('detail-content').classList.contains('hidden'), true);
  assert.equal(elements.get('delete-button').disabled, true);
  assert.ok(powerButtons.every((button) => button.disabled));
  const beforeBlockedAction = calls.length;
  await api.powerAction('reboot');
  assert.equal(calls.length, beforeBlockedAction);

  await api.selectInstance('instance-c');
  const reconcile = elements.get('reconcile-button');
  assert.equal(reconcile.classList.contains('hidden'), false);
  assert.equal(reconcile.disabled, false);
  await api.reconcileInstance();
  assert.ok(calls.some((call) => call.path === '/v1/instances/instance-c/reconcile' && call.method === 'POST'));
});
