#!/usr/bin/env node
// 用真实 Chromium 对签到页 HTML 做 Tab 焦点游走，数出「从页面起点到 Turnstile
// 复选框 iframe」的 Tab 次数——FlareSolverr tabs_till_verify 的真值校准工具。
//
// 用法：
//   1) 先用联调脚本把登录态签到页存下来：
//      PTSIGNIN_COOKIE='...' python3 scripts/ptsitesignin_live.py --site audiences.me \
//          --dry --save-html /tmp/audiences.html
//   2) 本地需要 playwright 与 Chromium（任意版本均可）：
//      node scripts/ptsitesignin_tabwalk.mjs /tmp/audiences.html [/tmp/other.html ...]
//
// 原理：禁脚本 + 断网络加载保存的 HTML（即 FlareSolverr 捕获 page_source 时的
// 渲染终态），往 .cf-turnstile 容器注入占位 iframe（与线上组件插入位置一致），
// 从 body 起点逐个真实发送 Tab 键，直到焦点落到该 iframe。
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';

if (!process.argv[2]) {
  console.error('用法: node scripts/ptsitesignin_tabwalk.mjs <签到页.html> [more.html ...]');
  process.exit(2);
}
let chromium;
try {
  ({ chromium } = createRequire(import.meta.url)('playwright'));
} catch {
  console.error('缺少 playwright：先 npm install playwright 并确保已有 Chromium');
  process.exit(2);
}

async function walk(file) {
  const html = (await fs.readFile(file, 'utf8'))
    .replace(/(<div\b[^>]*cf-turnstile[^>]*>)/i,
             '$1<iframe data-tabwalk="widget" src="about:blank" width="300" height="70"></iframe>');
  const tmp = `/tmp/tabwalk_${process.pid}.html`;
  await fs.writeFile(tmp, html);
  const browser = await chromium.launch({
    headless: true,
    // playwright 自带浏览器缺失/版本不匹配时，可用环境变量指向已有 Chromium
    executablePath: process.env.TABWALK_BROWSER || process.env.SIGNIN_TEST_BROWSER || undefined,
  });
  try {
    const page = await (await browser.newContext({ javaScriptEnabled: false })).newPage();
    await page.route('**/*', route =>
      route.request().url().startsWith('file://') ? route.continue() : route.abort());
    await page.goto('file://' + tmp);
    await page.evaluate(() => document.body.focus());
    for (let count = 1; count <= 150; count++) {
      await page.keyboard.press('Tab');
      const hit = await page.evaluate(() => {
        const active = document.activeElement;
        return {
          tag: active ? active.tagName.toLowerCase() : 'none',
          id: active ? (active.id || '') : '',
          inside: !!(active && active.closest &&
            active.closest('[data-tabwalk="widget"], .cf-turnstile')),
        };
      });
      if (hit.inside) {
        console.log(`${file}: TAB_COUNT=${count}（焦点落在 ${hit.tag}${hit.id ? '#' + hit.id : ''}）`);
        return;
      }
    }
    console.error(`${file}: 150 次 Tab 内未落到 Turnstile 组件，检查页面里是否有 .cf-turnstile`);
    process.exitCode = 1;
  } finally {
    await browser.close();
    await fs.unlink(tmp).catch(() => {});
  }
}

for (const file of process.argv.slice(2)) await walk(file);
