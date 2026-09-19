const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function page(detail) {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      innerHTML: '', textContent: '', hidden: false,
      addEventListener() {}, classList: { toggle() {}, add() {}, remove() {} },
    });
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: { querySelector: element, querySelectorAll: () => [] },
    setInterval() {}, setTimeout() {}, console,
    fetch: async path => ({ ok: true, json: async () => {
      if (path === '/api/status') return { demo_mode: true, nemotron: 'offline', elevenlabs: 'simulated' };
      if (path === '/api/mailbox') return {};
      if (path === '/api/board') return { open: {}, settled: {}, extraneous: [] };
      return detail;
    }}),
  });
  vm.runInContext(fs.readFileSync('web/app.js', 'utf8'), context);
  return { context, element };
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
