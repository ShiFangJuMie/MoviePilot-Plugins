// Local browser fixtures; no PT sites, remote OCR service, or real credentials are used.
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const code = await fs.readFile(new URL('../../../plugins.v3/ptsitesignin/function.js', import.meta.url), 'utf8');
const run = (await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)).default;
let signs = 0;
let ocrCalls = 0;
let ocrAnswer = 'ABC123';
let signed = false;
let serverFailure;
const readBody = async (req) => {
  let body = '';
  for await (const chunk of req) body += chunk;
  return body;
};
const successScript = `fetch('/sign', {method:'POST', body:new URLSearchParams(new FormData(form))})
  .then(r=>r.json()).then(r=>{if(r.ok) document.body.textContent='签到成功';});`;
const svg = 'data:image/svg+xml;base64,' + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="180" height="50"><rect width="180" height="50" fill="white"/><text x="10" y="35" font-size="30">ABC123</text></svg>').toString('base64');
const imageForm = `<form id="form"><img id="captcha" src="${svg}"><input name="imagehash" value="same-challenge" type="hidden"><input id="answer" name="imagestring"><button id="submit">立即签到</button></form>
  <script>const form=document.getElementById('form');form.onsubmit=e=>{e.preventDefault();${successScript}};</script>`;
const pt = http.createServer(async (req, res) => {
  try {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    assert.match(req.headers.cookie || '', /session=fixture/);
    if (req.url === '/sign') {
      const body = await readBody(req);
      if (body.includes('imagestring')) {
        const form = new URLSearchParams(body);
        assert.equal(form.get('imagestring'), 'ABC123');
        assert.equal(form.get('imagehash'), 'same-challenge');
      }
      signs += 1;
      signed = true;
      res.setHeader('Content-Type', 'application/json');
      res.end('{"ok":true}');
    } else if (req.url === '/signed') {
      res.end(signed ? '<body>今日已签到</body>' : '<body>未签到</body>');
    } else if (req.url === '/image') {
      res.end(imageForm);
    } else if (req.url === '/trigger') {
      res.end(`<button id="trigger" onclick="document.getElementById('modal').hidden=false">打开验证码</button><div id="modal" hidden>${imageForm}</div>`);
    } else if (req.url === '/challenge') {
      res.statusCode = 403;
      res.end('<title>Just a moment...</title><script>window._cf_chl_opt={};setTimeout(()=>location.href="/plain",200)</script>');
    } else if (req.url === '/plain') {
      res.end(`<form id="form"><button id="submit">立即签到</button></form><script>const form=document.getElementById('form');form.onsubmit=e=>{e.preventDefault();${successScript}};</script>`);
    } else if (req.url === '/auto') {
      res.end(`<form id="form"><input type="hidden" name="cf-turnstile-response" id="response"></form><p>验证通过后将自动完成签到</p>
        <script>const form=document.getElementById('form');form.onsubmit=e=>{e.preventDefault();${successScript}};
        setTimeout(()=>document.getElementById('response').value='fixture-proof',200);
        setTimeout(()=>form.requestSubmit(),1700);</script>`);
    } else if (req.url === '/altcha') {
      res.end(`<altcha-widget></altcha-widget><form id="form"><button id="submit">立即签到</button></form><script>
        customElements.define('altcha-widget',class extends HTMLElement {connectedCallback(){const r=this.attachShadow({mode:'open'});
          r.innerHTML='<div class="altcha"><input type="checkbox"><input type="hidden" name="altchaPayload"></div>';
          r.querySelector('[type=checkbox]').onclick=()=>{r.querySelector('.altcha').dataset.state='verified';r.querySelector('[name=altchaPayload]').value='fixture-proof';};}});
        const form=document.getElementById('form');form.onsubmit=e=>{e.preventDefault();${successScript}};</script>`);
    } else if (req.url === '/slider') {
      res.end('<body>滑动认证</body>');
    } else if (req.url === '/disabled') {
      res.end('<button id="submit" disabled>立即签到</button>');
    } else if (req.url === '/already') {
      res.end('<body>今日已签到</body>');
    } else {
      res.statusCode = 404;
      res.end('Not found');
    }
  } catch (error) { serverFailure = error; res.statusCode = 500; res.end('fixture error'); }
});
const ocr = http.createServer(async (req, res) => {
  try {
    res.setHeader('Content-Type', 'application/json');
    assert.equal(req.headers.cookie, undefined, 'PT cookies must never reach the OCR service');
    if (req.method === 'POST') {
      assert.equal(req.url, '/captcha/base64');
      const body = JSON.parse(await readBody(req));
      assert.deepEqual(Object.keys(body), ['base64_img']);
      assert.equal(Buffer.from(body.base64_img, 'base64').subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
      ocrCalls += 1;
      res.end(JSON.stringify({ result: ocrAnswer }));
    } else res.end('{"message":"MoviePilot OCR API"}');
  } catch (error) { serverFailure = error; res.statusCode = 500; res.end('{}'); }
});
const listen = (server) => new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
await listen(pt);
await listen(ocr);
const base = `http://localhost:${pt.address().port}`;
const ocrUrl = `http://127.0.0.1:${ocr.address().port}/captcha/base64`;

// Adapt only the Puppeteer/Playwright naming differences; navigation, DOM, cookies,
// screenshotting and fetch all execute in a real isolated Chromium browser.
function handleAdapter(handle) {
  if (!handle) return null;
  return new Proxy(handle, { get(target, key) {
    if (key === 'asElement') return () => handleAdapter(target.asElement());
    if (key === 'screenshot') return async ({ encoding, ...options }) => {
      const image = await target.screenshot(options);
      return encoding === 'base64' ? image.toString('base64') : image;
    };
    const value = target[key];
    return typeof value === 'function' ? value.bind(target) : value;
  } });
}
function pageAdapter(page) {
  return new Proxy(page, { get(target, key) {
    if (key === 'browserContext') return () => ({ newPage: async () => pageAdapter(await target.context().newPage()) });
    if (key === 'setCookie') return (...cookies) => target.context().addCookies(cookies);
    if (key === 'evaluateOnNewDocument') return (fn) => target.addInitScript(fn);
    if (key === 'evaluateHandle') return async (...args) => handleAdapter(await target.evaluateHandle(...args));
    if (key === '$') return async (...args) => handleAdapter(await target.$(...args));
    if (key === 'waitForSelector') return async (selector, { visible, ...options }) =>
      handleAdapter(await target.waitForSelector(selector, { ...options, state: visible ? 'visible' : 'attached' }));
    const value = target[key];
    return typeof value === 'function' ? value.bind(target) : value;
  } });
}

let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.SIGNIN_TEST_BROWSER,
    headless: true, args: ['--disable-background-networking', '--disable-component-update', '--disable-sync'] });
  async function scenario(name, rule, expected, answer = 'ABC123') {
    signs = 0; ocrCalls = 0; signed = false; ocrAnswer = answer; serverFailure = undefined;
    const browserContext = await browser.newContext();
    await browserContext.route('**/*', (route) => {
      const url = new URL(route.request().url());
      return ['localhost', '127.0.0.1'].includes(url.hostname) ? route.continue() : route.abort();
    });
    try {
      const page = await browserContext.newPage();
      const result = await run({ page: pageAdapter(page), context: { url: base + '/' + name,
        base_url: base, cookie: 'session=fixture', ua: '', ocr_url: ocrUrl, timeout_ms: 15000,
        rule: { path: '/' + name, mode: 'open_page', submit_selector: '#submit', submit_text: '立即签到',
          preflight_wait: 100, wait: 1000, cloudflare_wait: 3, ...rule } } });
      if (serverFailure) throw serverFailure;
      expected(result);
      console.log(`PASS browser/${name}${answer ? '' : '/empty-ocr'}`);
    } finally { await browserContext.close(); }
  }
  const imageRule = { mode: 'image', captcha_selector: '#captcha', captcha_input_selector: '#answer' };
  await scenario('image', imageRule, (result) => {
    assert.match(result.after, /签到成功/); assert.equal(signs, 1); assert.equal(ocrCalls, 1);
  });
  await scenario('trigger', { ...imageRule, mode: 'trigger_image', trigger_selector: '#trigger' }, (result) => {
    assert.match(result.after, /签到成功/); assert.equal(signs, 1); assert.equal(ocrCalls, 1);
  });
  await scenario('image', imageRule, (result) => {
    assert.equal(result.error, 'ocr_empty'); assert.equal(signs, 0);
  }, '');
  await scenario('challenge', { mode: 'cloudflare' }, (result) => {
    assert.match(result.after, /签到成功/); assert.equal(signs, 1);
  });
  await scenario('auto', { mode: 'cloudflare', submit_method: 'auto', auto_wait: 2000,
    verify_success_path: '/signed', verify_wait: 1000 }, (result) => {
    assert.match(result.after, /今日已签到/); assert.equal(signs, 1, 'do not race the site callback');
  });
  await scenario('altcha', { mode: 'altcha', altcha_checkbox_selector: 'altcha-widget input[type="checkbox"]',
    altcha_payload_selector: 'altcha-widget .altcha[data-state="verified"] input[name="altchaPayload"]' }, (result) => {
    assert.match(result.after, /签到成功/); assert.equal(signs, 1);
  });
  await scenario('slider', {}, (result) => { assert.equal(result.error, 'slider'); assert.equal(signs, 0); });
  await scenario('disabled', {}, (result) => { assert.equal(result.error, 'button'); assert.equal(signs, 0); });
  await scenario('already', {}, (result) => { assert.match(result.after, /今日已签到/); assert.equal(signs, 0); });
} finally {
  if (browser) await browser.close();
  await Promise.all([new Promise(resolve => pt.close(resolve)), new Promise(resolve => ocr.close(resolve))]);
}
