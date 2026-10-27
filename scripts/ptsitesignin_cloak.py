#!/usr/bin/env python3
"""通过远端 CloakBrowser（cloakserve CDP，nginx Basic Auth + 隧道）签到猪猪。

依赖：playwright（MoviePilot 自带；独立使用时 pip install playwright，无需下载本地浏览器）。

用法（环境变量）：
    CLOAK_ENDPOINT=https://https://<REDACTED-INSTANCE>   # BasicAuth 后的 CDP 端点
    CLOAK_AUTH=user:password                    # Basic Auth 凭据
    PTSIGNIN_COOKIE='...'                       # piggo.me 站点 Cookie
    CLOAK_PROXY=http://host:port                # 可选：浏览器出口代理（CDP query 参数）
    python3 scripts/ptsitesignin_cloak.py [--site piggo.me] [--path /attendance.php]

退出码：0 成功/已签，1 失败，2 配置缺失。
"""
import base64
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

SUCCESS_WORDS = ("签到成功", "簽到成功", "成功签到", "打卡成功", "获得")
ALREADY_WORDS = ("今日已签", "今天已签", "已经签到", "已签到", "已簽到", "明日")
BLOCK_WORDS = ("正在被调试", "正在被調試")
SAFELINE_CONFIRM = ("#sl-check", "#slg-box button", "div[id^='slg'] button")
TIMEOUT_SECONDS = 90


def flag(name: str) -> str:
    if name in sys.argv:
        index = sys.argv.index(name)
        return sys.argv[index + 1] if index + 1 < len(sys.argv) else ""
    return ""


def main() -> int:
    site = flag("--site") or "piggo.me"
    path = flag("--path") or "/attendance.php"
    endpoint = (os.environ.get("CLOAK_ENDPOINT") or "").strip().rstrip("/")
    auth = (os.environ.get("CLOAK_AUTH") or "").strip()
    cookie = (os.environ.get("PTSIGNIN_COOKIE") or "").strip()
    if not endpoint or not cookie:
        print("需要 CLOAK_ENDPOINT 与 PTSIGNIN_COOKIE 环境变量", file=sys.stderr)
        return 2

    headers = {}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    query = ""
    if os.environ.get("CLOAK_PROXY", "").strip():
        import urllib.parse
        query = "?proxy=" + urllib.parse.quote(os.environ["CLOAK_PROXY"].strip())

    cookies = []
    for item in cookie.split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if name:
            cookies.append({"name": name, "value": value, "domain": f".{site}", "path": "/"})

    def judge(text: str):
        if any(word in text for word in BLOCK_WORDS):
            return "failed", "雷池反调试拦截（该内核也被识别，需进一步排查）"
        if any(word in text for word in SUCCESS_WORDS):
            bonus = re.search(r"获得\s*[\d,.]+\s*(?:个|粒|枚|点)?\s*(?:魔力值|魔力|积分|爆米花)", text)
            return "success", "签到成功" + (f"（{bonus.group(0)}）" if bonus else "")
        if any(word in text for word in ALREADY_WORDS):
            return "already", "今日已经签到"
        return "", ""

    with sync_playwright() as manager:
        browser = manager.chromium.connect_over_cdp(
            f"{endpoint}/json/version{query}", headers=headers, timeout=30000)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.add_cookies(cookies)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"https://{site}{path}", wait_until="domcontentloaded", timeout=60000)
            # 雷池检测页自己的 JS 跑若干秒；确认按钮出现就点；签到页打开后自动完成签到
            clicked = 0
            for _ in range(TIMEOUT_SECONDS // 2):
                page.wait_for_timeout(2000)
                text = page.evaluate("document.body ? document.body.innerText : ''") or ""
                status, message = judge(text)
                if status:
                    print(f"结果：{status}（{message}）")
                    return 0 if status in {"success", "already"} else 1
                if clicked < 3:
                    for selector in SAFELINE_CONFIRM:
                        try:
                            button = page.locator(selector).first
                            if button.is_visible():
                                button.click(timeout=3000)
                                clicked += 1
                                break
                        except Exception:
                            continue
            text = page.evaluate("document.body ? document.body.innerText : ''") or ""
            title = page.title()
            if "登录" in title:
                print("结果：failed（停在登录页：Cookie 失效或会话环境不匹配）")
            else:
                print(f"结果：failed（{TIMEOUT_SECONDS} 秒未确认签到；标题「{title[:30]}」；"
                      f"正文「{' '.join(text.split())[:80]}」）")
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
