const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function page(detail, { savedTheme, storageBlocked = false, mailbox = {} } = {}) {
  const storage = new Map(savedTheme ? [["callback-theme", savedTheme]] : []);
  const root = { dataset: { theme: "dark" } };
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      innerHTML: '', textContent: '', hidden: false,
      listeners: {}, addEventListener(event, handler) { this.listeners[event] = handler; }, classList: { toggle() {}, add() {}, remove() {} },
    });
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: { documentElement: root, querySelector: element, querySelectorAll: () => [] },
    localStorage: {
      getItem(key) { if (storageBlocked) throw new Error('Storage blocked'); return storage.get(key) ?? null; },
      setItem(key, value) { if (storageBlocked) throw new Error('Storage blocked'); storage.set(key, value); },
    },
    setInterval() {}, setTimeout() {}, console,
    fetch: async path => ({ ok: true, json: async () => {
      if (path === '/api/status') return { demo_mode: true, nemotron: 'offline', elevenlabs: 'simulated' };
      if (path === '/api/mailbox') return mailbox;
      if (path === '/api/board') return { open: {}, settled: {}, extraneous: [] };
      return detail;
    }}),
  });
  const init = fs.readFileSync('web/index.html', 'utf8').match(/<script id="theme-init">([\s\S]*?)<\/script>/)[1];
  vm.runInContext(init, context);
  vm.runInContext(fs.readFileSync('web/app.js', 'utf8'), context);
  return { context, element, root, storage };
}

const hold = {
  status: 'held', vendor_name: 'Vendor', subject: '<img src=x onerror=alert(1)>',
  sender: 'a@example.com', rationale: '<script>bad()</script>', phone_on_file: '+15555550100',
  body: '<b>untrusted</b>', signals: [], verifications: [],
};

test('hold detail escapes message and model text', async () => {
  const { context, element } = page(hold);
  await context.showDetail('hold-1');
  const html = element('#detail').innerHTML;
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script>'));
});

test('settled holds offer no new call; escalated holds offer retry', async () => {
  for (const status of ['approved', 'blocked', 'calling', 'verifying', 'escalated']) {
    const { context, element } = page({ ...hold, status });
    await context.showDetail('hold-1');
    assert.equal(element('#detail').innerHTML.includes('id="call"'), status === 'escalated');
  }
});

test('cleared invoices are never labelled paid', () => {
  const { context } = page(hold);
  const html = context.card({ id: 'm1', kind: 'message', status: 'cleared', sender: 'a@example.com' });
  assert.ok(html.includes('>cleared</span>'));
  assert.ok(!html.includes('>paid</span>'));
});

test('queue items are keyboard-accessible controls with a selected state', () => {
  const { context } = page(hold);
  const html = context.card({ id: 'hold-1', kind: 'hold', status: 'held', sender: 'a@example.com' });
  assert.ok(html.startsWith('<button type="button"'));
  assert.ok(html.includes('aria-pressed="false"'));
});

test('account comparison masks both accounts and includes the invoice amount', async () => {
  const { context, element } = page({ ...hold, vendor_account: '123456781234',
    extracted: { bank_account: '987654325678', amount: 9180 } });
  await context.showDetail('hold-1');
  const html = element('#detail').innerHTML;
  assert.ok(html.includes('•••• 1234'));
  assert.ok(html.includes('•••• 5678'));
  assert.ok(html.includes('$9,180.00'));
  assert.ok(!html.includes('123456781234'));
  assert.ok(!html.includes('987654325678'));
});

test('secondary signals remain available inside a native disclosure', async () => {
  const { context, element } = page({ ...hold, signals: [
    { fired: true, detail: 'Account changed' },
    { fired: true, detail: 'Routing changed' },
    { fired: true, detail: 'Sender is unknown' },
  ] });
  await context.showDetail('hold-1');
  const html = element('#detail').innerHTML;
  assert.ok(html.includes('<details class="more-signals"><summary>1 more signal</summary>'));
  assert.ok(html.indexOf('Account changed') < html.indexOf('<details'));
  assert.ok(html.indexOf('Sender is unknown') > html.indexOf('<details'));
});

test('all original primary tabs and queue filters are retained', () => {
  const source = fs.readFileSync('web/index.html', 'utf8');
  assert.deepEqual([...source.matchAll(/data-view="([^"]+)"/g)].map(match => match[1]), ['queue', 'vendors', 'engine']);
  const { context } = page(hold);
  const groups = JSON.parse(vm.runInContext('JSON.stringify(BUCKETS)', context));
  assert.deepEqual(groups.map(group => [group[0], group[2]?.map(item => item[0]) || []]), [
    ['open', ['needs_review', 'calling', 'escalated']],
    ['settled', ['accepted', 'denied']], ['extraneous', []],
  ]);
});


test('dark mode is the default and slider changes persist', () => {
  const { element, root, storage } = page(hold);
  const slider = element('#dark-mode');
  assert.equal(root.dataset.theme, 'dark');
  assert.equal(slider.checked, true);
  slider.listeners.change({ target: { checked: false } });
  assert.equal(root.dataset.theme, 'light');
  assert.equal(storage.get('callback-theme'), 'light');
  assert.equal(element('meta[name="theme-color"]').content, '#f4efe5');
  slider.listeners.change({ target: { checked: true } });
  assert.equal(root.dataset.theme, 'dark');
  assert.equal(storage.get('callback-theme'), 'dark');
});

test('saved light preference is restored before rendering', () => {
  const { element, root } = page(hold, { savedTheme: 'light' });
  assert.equal(root.dataset.theme, 'light');
  assert.equal(element('#dark-mode').checked, false);
});

test('theme slider works when browser storage is blocked', () => {
  const { context, root } = page(hold, { storageBlocked: true });
  assert.equal(root.dataset.theme, 'dark');
  assert.doesNotThrow(() => context.setTheme('light'));
  assert.equal(root.dataset.theme, 'light');
});


test('demo email stays visible offline and uses the active inbox when connected', async () => {
  const html = fs.readFileSync('web/index.html', 'utf8');
  assert.match(html, /id="mailaddr">skodyctpwd@uberip.com/);
  assert.doesNotMatch(html, /id="mailbar"[^>]*hidden/);
  const offline = page(hold);
  await offline.context.loadMailbox();
  assert.match(offline.element('#mail-status').textContent, /requires the local demo server/);
  const live = page(hold, { mailbox: { address: 'active@example.com', running: true, received: 3, poll_seconds: 6 } });
  await live.context.loadMailbox();
  assert.equal(live.element('#mailaddr').textContent, 'active@example.com');
  assert.equal(live.element('#mail-status').textContent, '3 received · checking every 6s');
});
