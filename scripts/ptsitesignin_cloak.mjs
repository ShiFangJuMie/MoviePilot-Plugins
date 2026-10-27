#!/usr/bin/env node
// 通过远端 CloakBrowser（cloakserve，CDP over 隧道 + nginx Basic Auth）签到猪猪。
// 客户端只需 node + playwright-core，浏览器在 VPS 上。
//
// 用法（环境变量）：
//   CLOAK_ENDPOINT=https://https://<REDACTED-INSTANCE>     # nginx+BasicAuth 后的 CDP 端点
//   CLOAK_AUTH=user:password                      # Basic Auth 凭据
//   PTSIGNIN_COOKIE='...'                         # piggo.me 站点 Cookie
//   CLOAK_PROXY=http://host:port                  # 可选：让浏览器走指定出口
//   node scripts/ptsitesignin_cloak.mjs [--site piggo.me] [--path /attendance.php]
//
// 退出码：0 成功/已签，1 失败。
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)('playwright-core');

const args = process.argv.slice(2);
const flag = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const site = flag('--site') || 'piggo.me';
const path_ = flag('--path') || '/attendance.php';
const endpoint = process.env.CLOAK_ENDPOINT;
const auth = process.env.CLOAK_AUTH || '';
const cookie = process.env.PTSIGNIN_COOKIE || '';
if (!endpoint || !cookie) {
  console.error('需要 CLOAK_ENDPOINT 与 PTSIGNIN_COOKIE 环境变量');
  process.exit(2);
}

const headers = auth ? { Authorization: 'Basic ' + Buffer.from(auth).toString('base64') } : {};
let query = '';
if (process.env.CLOAK_PROXY) query = '?proxy=' + encodeURIComponent(process.env.CLOAK_PROXY);

const cookies = cookie.split(';').filter(item => item.includes('=')).map(item => {
  const index = item.indexOf('=');
  return { name: item.slice(0, index).trim(), value: item.slice(index + 1).trim(),
           domain: `.${site}`, path: '/' };
}).filter(item => item.name);

const TIMEOUT_SECONDS = 90;
const SAFELINE_CONFIRM = ['#sl-check', '#slg-box button', "div[id^='slg'] button"];

const browser = await chromium.connectOverCDP(endpoint.replace(/\/$/, '') + '/json/version' + query, { headers, timeout: 30000 });
try {
  const context = browser.contexts()[0] || await browser.newContext();
  await context.addCookies(cookies);
  const page = context.pages()[0] || await context.newPage();
  await page.goto(`https://${site}${path_}`, { waitUntil: 'domcontentloaded', timeout: 60000 });

  // 雷池检测页自己的 JS 跑若干秒；确认按钮出现就点；签到页打开后自动完成签到
  let clicked = 0;
  for (let i = 0; i < TIMEOUT_SECONDS / 2; i++) {
    await page.waitForTimeout(2000);
    const text = await page.evaluate(() => document.body ? document.body.innerText : '').catch(() => '');
    if (/正在被调试|正在被調試/.test(text)) {
      console.log('结果：failed（雷池反调试拦截，该内核也被识别）'); process.exit(1);
    }
    if (/签到成功|獲得|获得/.test(text)) {
      console.log('结果：success（' + text.replace(/\s+/g, ' ').slice(0, 100) + '）'); process.exit(0);
    }
    if (/今日已签|今天已签|已经签到|已签到|明日/.test(text)) {
      console.log('结果：already（' + text.replace(/\s+/g, ' ').slice(0, 100) + '）'); process.exit(0);
    }
    if (clicked < 3) {
      for (const selector of SAFELINE_CONFIRM) {
        const button = page.locator(selector).first();
        if (await button.isVisible().catch(() => false)) {
          await button.click({ timeout: 3000 }).catch(() => {});
          clicked += 1;
          break;
        }
      }
    }
  }
  const text = await page.evaluate(() => document.body ? document.body.innerText : '').catch(() => '');
  const title = await page.title().catch(() => '');
  if (/登录/.test(title)) { console.log('结果：failed（停在登录页：Cookie 失效或会话环境不匹配）'); process.exit(1); }
  console.log(`结果：failed（${TIMEOUT_SECONDS} 秒未确认签到；标题「${title.slice(0, 30)}」；` +
              `正文「${text.replace(/\s+/g, ' ').slice(0, 80)}」）`);
  process.exit(1);
} finally {
  await browser.close();
}
