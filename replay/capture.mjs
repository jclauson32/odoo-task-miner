// Replays a Chrome DevTools Recorder export and records every Odoo backend
// call, tagged with the recording step that was running when it fired.
//
//   node capture.mjs --recording rec.json --out network.json [--headless]
//        [--cookie session_id=abc] [--timeout 10000] [--settle 500] [--chrome /path/to/chrome]
//
// Output matches odoo_miner.models.NetworkLog. A partial log is written even
// if the replay fails, with completed=false and the failing step.

import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import puppeteer from 'puppeteer';
import { createRunner, parse, PuppeteerRunnerExtension, selectorToPElementSelector } from '@puppeteer/replay';

// Frontend-to-backend endpoints in the Odoo 18 web client
// (see addons/web/controllers/dataset.py and web/static/src/core/orm_service.js):
//   /web/dataset/call_kw/<model>/<method>      ORM calls (reads, writes, onchange)
//   /web/dataset/call_button/<model>/<method>  object buttons (type="object")
//   /web/action/load                           opening a menu/action
//   /mail/message/post                         a message or note in a record's chatter
const ODOO_RPC = /\/web\/dataset\/(call_kw|call_button)(\/|$)|\/web\/action\/load$|\/mail\/message\/post$/;

const { values: opts } = parseArgs({
  options: {
    recording: { type: 'string' },
    out: { type: 'string', default: 'network.json' },
    headless: { type: 'boolean', default: false },
    cookie: { type: 'string' },
    timeout: { type: 'string', default: '10000' },
    settle: { type: 'string', default: '500' },
    chrome: { type: 'string' },
    screenshots: { type: 'string' }, // folder: save a screenshot after every step
  },
});

if (!opts.recording) {
  console.error('Usage: node capture.mjs --recording rec.json --out network.json');
  process.exit(2);
}

function parseRpc(req) {
  const url = new URL(req.url());
  const call = { endpoint: url.pathname, model: null, method: null, args: null, kwargs: null };

  // call_kw and call_button URLs carry model and method in the path as a fallback.
  const m = url.pathname.match(/call_(?:kw|button)\/([^/]+)\/([^/]+)/);
  if (m) [call.model, call.method] = [m[1], m[2]];

  try {
    const body = JSON.parse(req.postData() || '{}');
    const p = body.params ?? body;
    call.model = p.model ?? call.model;
    call.method = p.method ?? call.method;
    call.args = Array.isArray(p.args) ? p.args : null;
    call.kwargs = p.kwargs && typeof p.kwargs === 'object' ? p.kwargs : null;
    if (url.pathname.endsWith('/web/action/load')) {
      call.method = 'action_load';
      call.kwargs = { action_id: p.action_id ?? null };
    }
    if (url.pathname.endsWith('/mail/message/post')) {
      // Posting to the chatter writes a message on the record: record it as the
      // message_post it is, not as nothing.
      call.model = p.thread_model ?? null;
      call.method = 'message_post';
      call.args = p.thread_id != null ? [p.thread_id] : null;
      const data = p.post_data ?? {};
      call.kwargs = { message_type: data.message_type ?? null, subtype_xmlid: data.subtype_xmlid ?? null };
    }
  } catch {
    // Non-JSON body: keep what the URL gave us.
  }
  return call;
}

// --- Selector choice --------------------------------------------------------
//
// The Recorder saves several alternative selectors per step, and the replay
// library normally races them: whichever matches first gets clicked. That
// breaks on Odoo when one alternative matches the wrong element, e.g. "a.focus"
// is whatever menu item happened to be highlighted while recording, and
// "aria/0.00" matches every empty price cell. Instead, we wait until some
// alternative matches exactly one visible element and use the most meaningful
// such selector, falling back to the library's race only if none is unique.

const SELECTOR_STEPS = new Set(['click', 'doubleClick', 'hover', 'change']);

// Classes that describe momentary UI state rather than identity.
const STATE_CLASS = /\.(focus|active|show|hover|o_hover|selected|o-hovered|o_selected_row)\b|:(focus|hover)\b/;

function selectorKind(sel) {
  const last = Array.isArray(sel) ? sel[sel.length - 1] : sel;
  return String(last);
}

function rankSelector(sel) {
  const s = selectorKind(sel);
  if (s.startsWith('aria/')) {
    const label = s.slice(5).replace(/\[role=.*\]$/, '').trim();
    if (!label) return null;                          // "aria/" alone matches anything
    if (/^[\d.,\s%$-]+$/.test(label)) return 5;       // "4", "0.00": values, not names
    return 1;
  }
  if (s.startsWith('text/')) return 2;
  if (s.startsWith('xpath/')) return /@id=/.test(s) ? 0 : 4;
  const css = s.replace(/^pierce\//, '');
  if (STATE_CLASS.test(css)) return null;             // e.g. "a.focus"
  if (/^#[\w-]+/.test(css)) return 0;                 // ids are the most specific
  return 3;
}

async function countVisible(page, sel) {
  try {
    const handles = await page.$$(selectorToPElementSelector(sel));
    let visible = 0;
    for (const h of handles) {
      if (await h.isVisible().catch(() => false)) visible += 1;
      await h.dispose();
    }
    return visible;
  } catch {
    return 0; // invalid selector for this page
  }
}

async function pickSelector(page, selectors, timeoutMs) {
  const ranked = selectors
    .map((sel) => ({ sel, rank: rankSelector(sel) }))
    .filter((c) => c.rank !== null)
    .sort((a, b) => a.rank - b.rank);
  if (!ranked.length) return null;

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    let anyMatch = false;
    for (const { sel } of ranked) {
      const n = await countVisible(page, sel);
      if (n === 1) return sel;
      if (n > 1) anyMatch = true;
    }
    // Something matches but nothing uniquely: give the page a moment, then let
    // the library's race decide among the non-state selectors.
    if (anyMatch && Date.now() > deadline - timeoutMs / 2) break;
    await new Promise((r) => setTimeout(r, 150));
  }
  return null;
}

class NetworkCapture extends PuppeteerRunnerExtension {
  constructor(browser, page, options, settleMs) {
    super(browser, page, options);
    this.settleMs = settleMs;
    this.currentStep = -1;
    this.calls = [];
    this.pending = new Map(); // request -> call record
    this.chosenSelectors = new Map(); // step index -> selector actually used
    this.lastRpcActivity = 0;
  }

  // Wait until no Odoo backend call is in flight and none has started for
  // `settleMs`. Only RPCs count: Odoo keeps other connections open (live chat
  // bus, service worker), so "network idle" for the whole page rarely happens
  // and would make every step wait for the full timeout.
  async waitForRpcIdle() {
    const deadline = Date.now() + this.timeout;
    const start = Date.now();
    while (Date.now() < deadline) {
      const quietFor = Date.now() - Math.max(this.lastRpcActivity, start);
      if (this.pending.size === 0 && quietFor >= this.settleMs) return;
      await new Promise((r) => setTimeout(r, 50));
    }
  }

  async beforeAllSteps(flow) {
    await super.beforeAllSteps(flow);
    this.page.on('request', (req) => {
      if (req.method() !== 'POST' || !ODOO_RPC.test(new URL(req.url()).pathname)) return;
      const call = {
        step_index: this.currentStep,
        timestamp_ms: Date.now(),
        ...parseRpc(req),
        status: null,
        rpc_error: null,
      };
      this.calls.push(call);
      this.pending.set(req, call);
      this.lastRpcActivity = Date.now();
    });
    this.page.on('requestfailed', (req) => {
      const call = this.pending.get(req);
      if (!call) return;
      this.pending.delete(req);
      call.rpc_error = `request failed: ${req.failure()?.errorText ?? 'unknown'}`;
      this.lastRpcActivity = Date.now();
    });
    this.page.on('response', async (res) => {
      const call = this.pending.get(res.request());
      if (!call) return;
      this.pending.delete(res.request());
      this.lastRpcActivity = Date.now();
      call.status = res.status();
      try {
        const body = await res.json();
        if (body?.error) call.rpc_error = body.error.data?.message ?? body.error.message ?? 'error';
      } catch {
        // Body unavailable (redirect, navigation): status is enough.
      }
    });
  }

  async beforeEachStep(step, flow) {
    this.currentStep = originalIndex.get(step) ?? flow.steps.indexOf(step);
    await super.beforeEachStep(step, flow);
  }

  async runStep(step, flow) {
    if (SELECTOR_STEPS.has(step.type) && Array.isArray(step.selectors) && !step.frame?.length) {
      const chosen = await pickSelector(this.page, step.selectors, this.timeout);
      if (chosen) step = { ...step, selectors: [chosen] };
      this.chosenSelectors.set(this.currentStep, chosen ?? null);
    }
    return super.runStep(step, flow);
  }

  async afterEachStep(step, flow) {
    await super.afterEachStep(step, flow);
    // Let late requests (onchange, autosave) land on this step, not the next.
    await this.waitForRpcIdle();
    if (opts.screenshots) {
      const name = `step-${String(this.currentStep).padStart(3, '0')}.png`;
      await this.page.screenshot({ path: `${opts.screenshots}/${name}` }).catch(() => {});
    }
  }
}

if (opts.screenshots) await mkdir(opts.screenshots, { recursive: true });

const recordingText = await readFile(opts.recording, 'utf8');
const fullFlow = parse(JSON.parse(recordingText));
const timeout = Number(opts.timeout);

// Recordings started from a new tab begin with a navigation to chrome://newtab,
// which a fresh browser can't load. Skip browser-internal pages, but remember
// each remaining step's index in the original file so the capture still lines
// up with `odoo-miner ingest`.
const BROWSER_PAGE = /^(chrome|about|edge|brave|chrome-search):/;
const kept = fullFlow.steps
  .map((step, index) => ({ step, index }))
  .filter(({ step }) => !(step.type === 'navigate' && BROWSER_PAGE.test(step.url ?? '')));
const flow = { ...fullFlow, steps: kept.map((k) => k.step) };
const originalIndex = new Map(kept.map((k) => [k.step, k.index]));

// The replay library can reject promises it no longer awaits once a step has
// failed; note them instead of letting Node crash before the log is written.
process.on('unhandledRejection', (e) => console.error('(ignored late error)', e?.message ?? e));

const withTimeout = (promise, ms) =>
  Promise.race([promise, new Promise((resolve) => setTimeout(resolve, ms))]);

const browser = await puppeteer.launch({
  headless: opts.headless,
  executablePath: opts.chrome,
  // Chrome refuses to start its sandbox as root (e.g. inside containers).
  args: process.getuid?.() === 0 ? ['--no-sandbox'] : [],
});
const page = await browser.newPage();
page.setDefaultTimeout(timeout);

if (opts.cookie) {
  const firstNav = flow.steps.find((s) => s.type === 'navigate');
  if (!firstNav) {
    console.error('--cookie needs a navigate step in the recording to know the domain.');
    process.exit(2);
  }
  const [name, ...rest] = opts.cookie.split('=');
  await browser.setCookie({ name, value: rest.join('='), url: new URL(firstNav.url).origin });
}

const extension = new NetworkCapture(browser, page, { timeout }, Number(opts.settle));
const result = {
  schema_version: '1',
  recording: opts.recording,
  started_at: new Date().toISOString(),
  completed: true,
  failed_step: null,
  error: null,
  calls: extension.calls,
  selectors_used: {},
};

let exitCode = 0;
try {
  const runner = await createRunner(flow, extension);
  const ok = await runner.run();
  if (ok === false) throw new Error('Replay was aborted before finishing.');
} catch (e) {
  result.completed = false;
  result.failed_step = extension.currentStep;
  result.error = String(e?.message ?? e);
  exitCode = 1;
  // Save what the page looked like when it stopped, for debugging.
  try {
    result.failed_url = page.url();
    result.failure_screenshot = opts.out.replace(/\.json$/, '') + '.failure.png';
    const shot = page.screenshot({ path: result.failure_screenshot }).then(() => true);
    if (!(await withTimeout(shot.catch(() => false), 5000))) result.failure_screenshot = null;
  } catch {
    result.failure_screenshot = null;
  }
} finally {
  result.selectors_used = Object.fromEntries(
    [...extension.chosenSelectors].map(([i, sel]) => [i, sel ? selectorKind(sel) : null]),
  );
  await writeFile(opts.out, JSON.stringify(result, null, 2));
  await withTimeout(browser.close().catch(() => {}), 10000);
}

console.log(`${result.completed ? 'Completed' : 'Failed'}: ${result.calls.length} backend calls → ${opts.out}`);
process.exit(exitCode);
