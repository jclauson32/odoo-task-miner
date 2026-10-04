// Replays a Chrome DevTools Recorder export and records every Odoo backend
// call, tagged with the recording step that was running when it fired.
//
//   node capture.mjs --recording rec.json --out network.json [--headless]
//        [--cookie session_id=abc] [--timeout 10000] [--settle 500] [--chrome /path/to/chrome]
//
// Output matches odoo_miner.models.NetworkLog. A partial log is written even
// if the replay fails, with completed=false and the failing step.

import { readFile, writeFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import puppeteer from 'puppeteer';
import { createRunner, parse, PuppeteerRunnerExtension } from '@puppeteer/replay';

// Frontend-to-backend endpoints in the Odoo 18 web client
// (see addons/web/controllers/dataset.py and web/static/src/core/orm_service.js):
//   /web/dataset/call_kw/<model>/<method>      ORM calls (reads, writes, onchange)
//   /web/dataset/call_button/<model>/<method>  object buttons (type="object")
//   /web/action/load                           opening a menu/action
const ODOO_RPC = /\/web\/dataset\/(call_kw|call_button)(\/|$)|\/web\/action\/load$/;

const { values: opts } = parseArgs({
  options: {
    recording: { type: 'string' },
    out: { type: 'string', default: 'network.json' },
    headless: { type: 'boolean', default: false },
    cookie: { type: 'string' },
    timeout: { type: 'string', default: '10000' },
    settle: { type: 'string', default: '500' },
    chrome: { type: 'string' },
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
  } catch {
    // Non-JSON body: keep what the URL gave us.
  }
  return call;
}

class NetworkCapture extends PuppeteerRunnerExtension {
  constructor(browser, page, options, settleMs) {
    super(browser, page, options);
    this.settleMs = settleMs;
    this.currentStep = -1;
    this.calls = [];
    this.pending = new Map(); // request -> call record
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
    });
    this.page.on('response', async (res) => {
      const call = this.pending.get(res.request());
      if (!call) return;
      this.pending.delete(res.request());
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
    this.currentStep = flow.steps.indexOf(step);
    await super.beforeEachStep(step, flow);
  }

  async afterEachStep(step, flow) {
    await super.afterEachStep(step, flow);
    // Let late requests (onchange, autosave) land on this step, not the next.
    try {
      await this.page.waitForNetworkIdle({ idleTime: this.settleMs, timeout: this.timeout });
    } catch {
      // Long-polling (bus) can keep the network busy; move on.
    }
  }
}

const recordingText = await readFile(opts.recording, 'utf8');
const flow = parse(JSON.parse(recordingText));
const timeout = Number(opts.timeout);

const browser = await puppeteer.launch({
  headless: opts.headless,
  executablePath: opts.chrome,
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
} finally {
  await writeFile(opts.out, JSON.stringify(result, null, 2));
  await browser.close();
}

console.log(`${result.completed ? 'Completed' : 'Failed'}: ${result.calls.length} backend calls → ${opts.out}`);
process.exit(exitCode);
