// Browserless v2 Function API: the function runtime is browser-based, not Node.js.
export default async function ({ page, context }) {
  const rule = context.rule;
  const deadline = Date.now() + context.timeout_ms - 3000;
  const remaining = () => Math.max(1, deadline - Date.now());
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, Math.min(ms, remaining())));
  const target = new URL(context.url);
  if (!['http:', 'https:'].includes(target.protocol)) return { error: 'navigation' };
  const words = {
    success: rule.success_keywords || [],
    already: rule.already_keywords || [],
    defaults: rule.strict_status_keywords ? [] :
      ['签到成功', '簽到成功', '成功签到', '打卡成功', '今日已签', '今天已签', '已经签到', '已签到', '已簽到'],
  };
  let stage = 'navigation';
  let ajax = null;
  const receiptPath = rule.response_path || rule.signin_path || rule.path;

  // Select visible controls, including Altcha's open shadow root. Never infer success from a click.
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

  async function state() {
    return page.evaluate((statusWords) => {
      const text = document.body?.innerText || '';
      const source = document.documentElement?.outerHTML || '';
      let token = [...document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')]
        .map((node) => node.value).find(Boolean) || '';
      if (!token) {
        try { token = window.turnstile?.getResponse() || ''; } catch { /* widget may still be loading */ }
      }
      return {
        completed: [...statusWords.success, ...statusWords.already, ...statusWords.defaults]
          .some((word) => word && text.includes(word)),
        interstitial: /just a moment|_cf_chl_opt|chl_page|正在进行安全检测|安全检测能力由|雷池\s*WAF|checking your browser/i.test(source),
        widget: Boolean(document.querySelector('.cf-turnstile, input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"]')),
        token: Boolean(token),
        slider: /滑动认证|拖动滑块验证|拖動滑塊驗證/.test(text),
        login: Boolean(document.querySelector('input[type="password"]')),
      };
    }, words);
  }

  const clickedFrames = new Set();
  async function clickChallenge() {
    for (const frame of page.frames()) {
      if (!frame.url().includes('challenges.cloudflare.com') || clickedFrames.has(frame)) continue;
      try {
        const checkbox = await frame.$('input[type="checkbox"]');
        if (checkbox) {
          await checkbox.click();
          clickedFrames.add(frame);
          continue;
        }
        // Closed shadow roots hide the checkbox from CSS; use the visible widget frame.
        const element = await frame.frameElement();
        const box = await element.boundingBox();
        if (box && box.width >= 100 && box.height >= 30) {
          await page.mouse.click(box.x + 30, box.y + Math.min(32, box.height / 2));
          clickedFrames.add(frame);
        }
      } catch { /* navigation may replace the challenge frame */ }
    }
  }

  async function waitChallenge() {
    const until = Math.min(deadline, Date.now() + Number(rule.cloudflare_wait || 30) * 1000);
    let current;
    while (Date.now() < until) {
      try { current = await state(); } catch { await sleep(300); continue; }
      if (current.login || current.completed) return current;
      const frameExists = page.frames().some((frame) => frame.url().includes('challenges.cloudflare.com'));
      if (!current.interstitial && (!(current.widget || frameExists) || current.token)) return current;
      await clickChallenge();
      await sleep(1200);
    }
    return { ...current, pending: true };
  }

  async function click(selector, text = '') {
    // Resolve in the page to support open shadow roots, then use a native browser click.
    const handle = await page.evaluateHandle((args) => window.__ptsigninDOM.find(...args), [selector, text]);
    try {
      const element = handle.asElement();
      if (!element) return false;
      await element.click();
      return true;
    } finally { await handle.dispose(); }
  }

  async function navigate(url) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: Math.min(45000, remaining()) });
    } catch {
      // A challenge may keep navigation pending while an otherwise usable page is already present.
      if (!await page.content().catch(() => '')) throw new Error('navigation');
    }
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

  try {
    page.setDefaultTimeout(Math.min(15000, remaining()));
    if (context.ua) await page.setUserAgent(context.ua);
    if (context.proxy_auth) await page.authenticate(context.proxy_auth);
    const cookieUrl = new URL(context.base_url).origin + '/';
    const cookies = context.cookie.split(';').filter((item) => item.includes('=')).map((item) => {
      const index = item.indexOf('=');
      return { name: item.slice(0, index).trim(), value: item.slice(index + 1).trim(), url: cookieUrl };
    }).filter((item) => item.name);
    await page.setCookie(...cookies);
    await page.evaluateOnNewDocument(installDOM);
    page.on('dialog', (dialog) => dialog.accept().catch(() => {}));
    page.on('response', async (response) => {
      try {
        const url = new URL(response.url());
        const expected = new URL(receiptPath || target.pathname, target);
        if (rule.success_json && url.origin === target.origin && url.pathname === expected.pathname &&
            response.request().method() === 'POST') {
          ajax = JSON.stringify({ status: response.status(), text: await response.text() });
        }
      } catch { /* no usable receipt from this response */ }
    });
    await navigate(target.href);
    await sleep(Math.min(Number(rule.preflight_wait || 1000), 15000));
    stage = 'challenge';
    let current = await waitChallenge();
    if (current.pending) return { error: 'challenge' };
    if (current.completed || current.login) return { after: await page.content() };
    if (current.slider) return { error: 'slider' };

    if (rule.pre_click_selector || rule.pre_click_text) {
      stage = 'button';
      if (!await click(rule.pre_click_selector || 'a, button, [role="button"]', rule.pre_click_text || '')) {
        return { error: 'button' };
      }
      await sleep(800);
    }
    if (rule.mode === 'trigger_image') {
      stage = 'button';
      if (!await click(rule.trigger_selector)) return { error: 'button' };
      await sleep(Number(rule.trigger_wait || 500));
    }
    if (['image', 'trigger_image'].includes(rule.mode)) await recognize();
    if (rule.mode === 'altcha') {
      stage = 'altcha';
      if (!await click(rule.altcha_checkbox_selector)) return { error: 'altcha' };
      const until = Math.min(deadline, Date.now() + 30000);
      let ready = false;
      while (Date.now() < until) {
        ready = await page.evaluate((selector) => window.__ptsigninDOM.nodes(selector)
          .some((node) => String(node.value || '').trim()), rule.altcha_payload_selector);
        if (ready) break;
        await sleep(250);
      }
      if (!ready) return { error: 'altcha' };
    }

    const method = rule.submit_method || 'click';
    stage = 'button';
    // Natural verification invokes the site's own callback. An additional form submission
    // would race that callback and may sign twice; auto mode only waits for its receipt.
    if (!['auto', 'turnstile_auto', 'none', 'skip'].includes(method)) {
      if (!await click(rule.submit_selector, rule.submit_text || '')) return { error: 'button' };
      if (method === 'confirm_click') {
        await sleep(200);
        if (!await click('.layui-layer-btn0, .layui-layer-btn a', '确')) return { error: 'button' };
      }
    }

    stage = 'challenge';
    await sleep(Math.max(1000, Number(rule.wait || 3500)));
    current = await waitChallenge();
    if (current.pending) return { error: 'challenge' };
    if (current.slider) return { error: 'slider' };
    if (['auto', 'turnstile_auto'].includes(method) && !current.completed && !current.login) {
      const until = Math.min(deadline, Date.now() + Number(rule.auto_wait || 20000));
      while (Date.now() < until) {
        await sleep(500);
        current = await state();
        if (current.completed || current.login) break;
      }
    }
    if (rule.verify_success_path) {
      stage = 'navigation';
      const verify = new URL(rule.verify_success_path, target);
      if (verify.origin !== target.origin) return { error: 'navigation' };
      await navigate(verify.href);
      await sleep(Math.max(1000, Number(rule.verify_wait || 1000)));
      current = await waitChallenge();
      if (current.pending) return { error: 'challenge' };
    }
    return { after: await page.content(), ajax };
  } catch (error) {
    const known = ['ocr_empty', 'ocr_http', 'ocr_image', 'navigation'];
    return { error: Date.now() >= deadline ? 'timeout' : known.includes(error.message) ? error.message : stage || 'execution' };
  }
}
