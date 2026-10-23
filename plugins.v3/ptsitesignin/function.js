// Browserless v2 Function API runs in a browser context, not Node.js.
export default async function ({ page, context }) {
  const rule = context.rule;
  const started = Date.now();
  const deadline = started + context.timeout_ms - 5000;
  const remaining = () => Math.max(1, deadline - Date.now());
  const sleep = ms => new Promise(resolve => setTimeout(resolve, Math.min(ms, remaining())));
  const target = new URL(context.url);
  if (!['http:', 'https:'].includes(target.protocol)) return { error: 'navigation' };
  const words = {
    success: rule.success_keywords || [], already: rule.already_keywords || [],
    defaults: rule.strict_status_keywords ? [] :
      ['签到成功', '簽到成功', '成功签到', '打卡成功', '今日已签', '今天已签', '已经签到', '已签到', '已簽到'],
  };
  let stage = 'navigation';
  let ajax = null;
  let lastState = {};
  let navigationStatus = 0;
  let checkboxClicks = 0;
  let autoAction = 'none';
  const posts = [];
  const receiptPaths = new Set([target.pathname]);
  for (const path of [rule.response_path, rule.signin_path]) {
    if (path) {
      const url = new URL(path, target);
      if (url.origin === target.origin) receiptPaths.add(url.pathname);
    }
  }
  const signPosts = () => posts.filter(item => receiptPaths.has(item.path) || item.hasToken);
  const diagnostic = error => ({ stage, elapsed_ms: Date.now() - started,
    kind: lastState.kind || 'none', token: Boolean(lastState.token),
    frames: lastState.frames || 0, checkbox_clicks: checkboxClicks,
    requests: signPosts().length, http_status: navigationStatus, auto_action: autoAction,
    error_type: error?.name || '' });
  const failure = (error, exception) => ({ error, diagnostics: diagnostic(exception) });
  const finish = async () => ({ after: await page.content(), ajax, diagnostics: diagnostic() });
  function installDOM() {
    const visible = (node) => {
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const nodes = (selector, root = document) => {
      const found = [...root.querySelectorAll(selector)];
      for (const host of root.querySelectorAll('*')) {
        if (!host.shadowRoot) continue;
        found.push(...nodes(selector, host.shadowRoot));
        if (selector.startsWith(`${host.localName} `)) {
          found.push(...nodes(selector.slice(host.localName.length + 1), host.shadowRoot));
        }
      }
      return found;
    };
    window.__ptsigninDOM = {
      find(selector, text = '') {
        const candidates = nodes(selector).filter((node) => visible(node) && !node.disabled &&
          node.getAttribute('aria-disabled') !== 'true');
        if (!text) return candidates[0] || null;
        return candidates.map((node) => ({ node, label: String(node.value || node.textContent || '')
          .replace(/\s+/g, ' ').trim() })).filter(({ label }) => label.includes(text) && label.length <= 80)
          .sort((a, b) => a.label.length - b.label.length)[0]?.node || null;
      },
      nodes,
    };
  }

  const challengeFrames = () => page.frames().filter(frame => {
    try { return new URL(frame.url()).hostname === 'challenges.cloudflare.com'; } catch { return false; }
  });

  async function state() {
    const current = await page.evaluate(statusWords => {
      const nodes = window.__ptsigninDOM?.nodes || (selector => [...document.querySelectorAll(selector)]);
      const text = document.body?.innerText || '';
      const visible = document.title + '\n' + text;
      // Script names and a loaded challenge library are not proof that the page is still blocked.
      const interstitial = /just a moment|checking your browser|verify you are human|正在验证您是否是真人/i.test(visible);
      const waf = /正在进行安全检测|安全检测能力由|雷池\s*WAF/i.test(visible);
      let token = nodes('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')
        .map(node => node.value).find(Boolean) || '';
      if (!token) { try { token = window.turnstile?.getResponse() || ''; } catch { /* not rendered yet */ } }
      const widget = nodes('.cf-turnstile, input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')
        .length > 0;
      const lower = text.toLowerCase();
      return {
        completed: [...statusWords.success, ...statusWords.already, ...statusWords.defaults]
          .some(word => word && lower.includes(String(word).toLowerCase())),
        kind: waf ? 'waf' : interstitial ? 'cloudflare' : widget ? 'turnstile' : 'none',
        interstitial: interstitial || waf, widget, token: Boolean(token),
        slider: /滑动认证|拖动滑块验证|拖動滑塊驗證/.test(text),
        login: /\/(?:auth\/(?:sign-in|login)|login\.php|takelogin\.php)(?:\/|$)/i.test(location.pathname) ||
          Boolean(document.querySelector('input[type="password"]')),
      };
    }, words);
    current.frames = challengeFrames().length;
    if (current.kind === 'none' && current.frames) current.kind = 'turnstile';
    lastState = current;
    return current;
  }

  const clickedFrames = new Map();
  async function clickChallenge() {
    for (const frame of challengeFrames()) {
      const previous = clickedFrames.get(frame) || { count: 0, at: 0 };
      if (previous.count >= 3 || Date.now() - previous.at < 2500) continue;
      try {
        const checkbox = await frame.$('input[type="checkbox"]');
        try {
          if (checkbox && await checkbox.boundingBox()) {
            await checkbox.click();
          } else {
            // The checkbox may be inside a closed shadow root. Click only a visible widget frame.
            const element = await frame.frameElement();
            try {
              const box = await element.boundingBox();
              if (!box || box.width < 100 || box.height < 30) continue;
              await page.mouse.click(box.x + 30, box.y + Math.min(32, box.height / 2));
            } finally { await element.dispose(); }
          }
          checkboxClicks += 1;
          clickedFrames.set(frame, { count: previous.count + 1, at: Date.now() });
        } finally { if (checkbox) await checkbox.dispose(); }
      } catch { /* a challenge can replace its iframe while we inspect it */ }
    }
  }

  async function waitChallenge() {
    const until = Math.min(deadline, Date.now() + Number(rule.cloudflare_wait || 60) * 1000);
    while (Date.now() < until) {
      let current;
      try { current = await state(); } catch (error) {
        if (!/context.*destroyed|cannot find context/i.test(error.message || '')) throw error;
        await sleep(250); continue;
      }
      if (current.login || current.completed || current.slider) return current;
      if (!current.interstitial && (!(current.widget || current.frames) || current.token)) return current;
      await clickChallenge();
      await sleep(500);
    }
    return { ...lastState, pending: true };
  }

  async function click(selector, text = '') {
    const until = Math.min(deadline, Date.now() + Number(rule.button_wait || 15000));
    while (Date.now() < until) {
      let handle;
      try {
        handle = await page.evaluateHandle(args => window.__ptsigninDOM.find(...args), [selector, text]);
      } catch (error) {
        if (!/context.*destroyed|cannot find context/i.test(error.message || '')) throw error;
        await sleep(250); continue;
      }
      try {
        const element = handle.asElement();
        if (element) {
          // Once a click has started, do not replay it after a navigation/connection error.
          await element.click();
          return true;
        }
      } finally { await handle.dispose(); }
      const current = await state();
      if (current.completed || current.login) return false;
      await sleep(250);
    }
    return false;
  }

  async function navigate(url) {
    try {
      const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: Math.min(45000, remaining()) });
      navigationStatus = response?.status() || 0;
    } catch (error) {
      if (!/^https?:/.test(page.url()) || !await page.content().catch(() => '')) throw new Error('navigation');
      if (error.name !== 'TimeoutError' && !/ERR_ABORTED/.test(error.message || '')) throw error;
    }
  }

  async function registerForms() {
    const actions = await page.evaluate(() => [...document.forms].map(form => form.action));
    for (const action of actions) {
      const url = new URL(action, target);
      if (url.origin === target.origin) receiptPaths.add(url.pathname);
    }
  }

  async function readReceipt() {
    const response = signPosts().filter(item => item.response).at(-1)?.response;
    // A successful form POST can redirect. Its body is unavailable; inspect the destination page.
    if (response && (response.status() < 300 || response.status() >= 400)) {
      ajax = JSON.stringify({ status: response.status(), text: await response.text() });
    }
    return Boolean(ajax);
  }

  async function submitForm(automatic = false) {
    await registerForms();
    const result = await page.evaluate(async ({ rule, automatic, timeout }) => {
      const nodes = window.__ptsigninDOM.nodes;
      let form, button, token = '';
      if (automatic) {
        const text = document.body?.innerText || '';
        if (rule.pending_keywords?.length && !rule.pending_keywords.some(word => text.includes(word))) return { skipped: true };
        const inputs = nodes('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
        token = inputs.map(node => node.value).find(Boolean) || '';
        if (!token) { try { token = window.turnstile?.getResponse() || ''; } catch { /* not ready */ } }
        if (!token) return { error: 'challenge' };
        const names = nodes('[data-callback]').map(node => node.getAttribute('data-callback'));
        names.push('cfCallback', 'turnstileCallback', 'onTurnstileSuccess', 'onTurnstileVerify');
        const callback = names.find(name => name && typeof window[name] === 'function');
        if (callback) {
          window[callback](token);
          return { callback: true };
        }
        form = inputs.map(node => node.form).find(Boolean) ||
          [...document.forms].find(node => /attendance|checkin|sign.?in/i.test(node.action)) ||
          [...document.forms].find(node => node.method.toLowerCase() === 'post');
      } else {
        button = window.__ptsigninDOM.find(rule.submit_selector, rule.submit_text || '');
        form = button?.form || (button instanceof HTMLFormElement ? button : null);
        if (!form) return { error: 'submit' };
      }
      const action = new URL(form?.getAttribute('action') || rule.signin_path || location.href, location.href);
      if (action.origin !== location.origin) return { error: 'submit' };
      const data = form ? new FormData(form) : new FormData();
      if (button?.name && !button.disabled) data.append(button.name, button.value || '');
      if (automatic) data.set('cf-turnstile-response', token);
      const method = automatic ? 'POST' : (form.method || 'POST').toUpperCase();
      if (!['GET', 'POST'].includes(method)) return { error: 'submit' };
      if (method === 'GET') for (const [key, value] of data) action.searchParams.append(key, value);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      try {
        const response = await fetch(action.href, { method, credentials: 'same-origin',
          body: method === 'POST' ? data : undefined, signal: controller.signal });
        return { response: { status: response.status, text: await response.text() } };
      } finally { clearTimeout(timer); }
    }, { rule, automatic, timeout: remaining() });
    if (result.error) throw new Error(result.error);
    if (result.response) ajax = JSON.stringify(result.response);
    autoAction = result.callback ? 'callback' : result.response ? 'form' : 'none';
  }

  async function automaticSubmit() {
    stage = 'auto_submit';
    // Give the site's callback its own budget. A request already sent is never replayed.
    const until = Math.min(deadline - 5000, Date.now() + Number(rule.auto_wait || 20000));
    while (Date.now() < until) {
      const current = await state();
      if (current.completed || current.login || signPosts().length) return;
      if (current.interstitial || (current.widget && !current.token)) await clickChallenge();
      await sleep(250);
    }
    const current = await state();
    if (current.completed || current.login || signPosts().length) return;
    if (!current.token) throw new Error('challenge');
    await submitForm(true);
    // Calling a callback is also a submission attempt; do not follow it with a second POST.
  }

  async function waitResult(useReceipt = true, budget = Number(rule.wait || 3500)) {
    const until = Math.min(deadline, Date.now() + Math.max(1000, budget));
    while (Date.now() < until) {
      let current;
      try { current = await state(); } catch (error) {
        if (!/context.*destroyed|cannot find context/i.test(error.message || '')) throw error;
        await sleep(250); continue;
      }
      if (current.completed || current.login || current.slider) return current;
      if (useReceipt && await readReceipt()) return current;
      if (current.interstitial || ((current.widget || current.frames) && !current.token)) await clickChallenge();
      await sleep(250);
    }
    return state();
  }

  async function recognize() {
    stage = 'ocr_image';
    const image = await page.waitForSelector(rule.captcha_selector, { visible: true, timeout: Math.min(15000, remaining()) });
    await image.evaluate((node) => typeof node.decode === 'function' ? node.decode() : undefined);
    const base64 = await image.screenshot({ type: 'png', encoding: 'base64' });
    stage = 'ocr_http';
    const ocrPage = await page.browserContext().newPage();
    try {
      if (context.proxy_auth) await ocrPage.authenticate(context.proxy_auth);
      // MoviePilot-OCR has no CORS middleware. A separate same-origin page avoids changing
      // either service and leaves the PT page, challenge and imagehash alive in this session.
      await ocrPage.goto(new URL(context.ocr_url).origin, {
        waitUntil: 'domcontentloaded', timeout: Math.min(15000, remaining()),
      });
      const response = await ocrPage.evaluate(async ({ url, image, timeout }) => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);
        try {
          const result = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base64_img: image }), credentials: 'omit',
            signal: controller.signal,
          });
          if (!result.ok) return null;
          return await result.json();
        } finally { clearTimeout(timer); }
      }, { url: context.ocr_url, image: base64, timeout: Math.min(30000, remaining()) });
      if (!response) throw new Error('ocr_http');
      const answer = String(response.result || '').trim();
      if (!/^[A-Z0-9]{6}$/.test(answer)) throw new Error('ocr_empty');
      const input = await page.$(rule.captcha_input_selector);
      if (!input) throw new Error('ocr_image');
      await input.evaluate((node) => { node.value = ''; });
      await input.type(answer);
    } finally { await ocrPage.close().catch(() => {}); }
  }

  async function execute() {
    try {
      page.setDefaultTimeout(Math.min(15000, remaining()));
      if (context.ua) await page.setUserAgent(context.ua);
      if (context.proxy_auth) await page.authenticate(context.proxy_auth);
      const cookieUrl = new URL(context.base_url).origin + '/';
      const cookies = context.cookie.split(';').filter(item => item.includes('=')).map(item => {
        const index = item.indexOf('=');
        return { name: item.slice(0, index).trim(), value: item.slice(index + 1).trim(), url: cookieUrl };
      }).filter(item => item.name);
      await page.setCookie(...cookies);
      await page.evaluateOnNewDocument(installDOM);
      page.on('dialog', dialog => dialog.accept().catch(() => {}));
      page.on('request', request => {
        try {
          const url = new URL(request.url());
          if (request.method() === 'POST' && url.origin === target.origin) {
            posts.push({ request, path: url.pathname,
              hasToken: /cf-turnstile-response|turnstile[_-]?token/i.test(request.postData() || '') });
          }
        } catch { /* non-HTTP request */ }
      });
      page.on('response', response => {
        if (response.request().isNavigationRequest() && response.request().frame() === page.mainFrame()) {
          navigationStatus = response.status();
        }
        const item = posts.find(entry => entry.request === response.request());
        if (item) item.response = response;
      });

      if (rule.preflight_path) {
        stage = 'preflight';
        const preflight = new URL(rule.preflight_path, target);
        if (preflight.origin !== target.origin) return failure('navigation');
        await navigate(preflight.href);
        await sleep(Math.min(Number(rule.preflight_wait || 1000), 15000));
        const current = await waitChallenge();
        if (current.pending) return failure('challenge');
        if (current.login) return failure('login');
        if (current.completed) return finish();
      }
      stage = 'navigation';
      await navigate(target.href);
      await sleep(Math.min(Number(rule.preflight_wait || 1000), 15000));
      stage = 'challenge_before';
      let current = await waitChallenge();
      if (current.pending) return failure('challenge');
      if (current.login) return failure('login');
      if (current.completed) return finish();
      if (current.slider) return failure('slider');
      await registerForms();

      if (rule.pre_click_selector || rule.pre_click_text) {
        stage = 'button';
        if (!await click(rule.pre_click_selector || 'a, button, [role="button"]', rule.pre_click_text || '')) return failure('button');
        await sleep(800);
      }
      if (rule.mode === 'trigger_image') {
        stage = 'button';
        if (!await click(rule.trigger_selector)) return failure('button');
        await sleep(Number(rule.trigger_wait || 500));
      }
      if (['image', 'trigger_image'].includes(rule.mode)) await recognize();
      if (rule.mode === 'altcha') {
        stage = 'altcha';
        if (!await click(rule.altcha_checkbox_selector)) return failure('altcha');
        const until = Math.min(deadline, Date.now() + 30000);
        let ready = false;
        while (Date.now() < until) {
          ready = await page.evaluate(selector => window.__ptsigninDOM.nodes(selector)
            .some(node => String(node.value || '').trim()), rule.altcha_payload_selector);
          if (ready) break;
          await sleep(250);
        }
        if (!ready) return failure('altcha');
      }

      const method = rule.submit_method || 'click';
      stage = 'submit';
      if (['auto', 'turnstile_auto'].includes(method)) {
        await automaticSubmit();
      } else if (method === 'ajax') {
        await submitForm();
      } else if (!['none', 'skip'].includes(method)) {
        if (!await click(rule.submit_selector, rule.submit_text || '')) {
          current = await state();
          if (current.login) return failure('login');
          return current.completed ? finish() : failure('button');
        }
        if (method === 'confirm_click') {
          if (!await click('.layui-layer-btn0, .layui-layer-btn a', '确')) return failure('button');
        }
      }
      stage = 'challenge_after';
      current = await waitResult();
      if (current.interstitial || ((current.widget || current.frames) && !current.token && !current.completed)) {
        current = await waitChallenge();
        if (current.pending) return failure('challenge');
      }
      if (current.login) return failure('login');
      if (current.slider) return failure('slider');
      await readReceipt();
      if (rule.verify_success_path) {
        stage = 'verify';
        const verify = new URL(rule.verify_success_path, target);
        if (verify.origin !== target.origin) return failure('navigation');
        await navigate(verify.href);
        current = await waitResult(false, Number(rule.verify_wait || 5000));
        if (current.interstitial || ((current.widget || current.frames) && !current.token && !current.completed)) {
          current = await waitChallenge();
          if (current.pending) return failure('challenge');
        }
        if (current.login) return failure('login');
      }
      return finish();
    } catch (error) {
      const known = ['ocr_empty', 'ocr_http', 'ocr_image', 'navigation', 'challenge', 'submit'];
      return failure(Date.now() >= deadline ? 'timeout' : known.includes(error.message) ? error.message : 'execution', error);
    }
  }

  let timer;
  try {
    // Return diagnostics before Browserless's HTTP budget expires, including a stalled evaluate.
    return await Promise.race([execute(), new Promise(resolve => {
      timer = setTimeout(() => resolve(failure('timeout')), remaining());
    })]);
  } finally { clearTimeout(timer); }
}
