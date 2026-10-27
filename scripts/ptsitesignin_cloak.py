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
import signal
import sys
import time

from playwright.sync_api import sync_playwright


def _watchdog(seconds: int = 300):
    """硬超时：隧道 TLS 握手可能无限卡死（旧版 playwright 没有覆盖该阶段的
    超时），到点直接退出，绝不挂死。"""
    def _fire(signum, frame):
        print("结果：failed（全局超时：链路长时间无响应，多为隧道抖动，可重试）",
              file=sys.stderr)
        sys.exit(1)
    signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)

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
    _watchdog()
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
    # 每次运行用独立的 fingerprint seed：免费层只有 1 个并发会话，
    # 复用默认 seed 会撞上残留连接（表现为 WS 排队挂死）；结束时会显式 close。
    seed = f"pt{int(time.time())}"
    import urllib.parse as _uparse
    params = {"fingerprint": seed}
    if os.environ.get("CLOAK_PROXY", "").strip():
        params["proxy"] = os.environ["CLOAK_PROXY"].strip()
    query = "?" + _uparse.urlencode(params)

    # 先自己发现 webSocketDebuggerUrl 再连 ws：旧版 Playwright 的
    # connect_over_cdp 会给 http 地址自动追加 /json/version（传完整路径会拼出
    # /json/version/json/version），而且 query 参数（?proxy=）也要挂在发现请求上。
    import json as _json
    import urllib.request as _rq
    ws_url = ""
    for attempt in range(3):  # 隧道偶发 SSLEOF：发现请求退避重试
        try:
            discovery = _rq.Request(f"{endpoint}/json/version{query}", headers=headers)
            with _rq.build_opener(_rq.ProxyHandler({})).open(discovery, timeout=30) as response:
                ws_url = str(_json.loads(response.read().decode()).get("webSocketDebuggerUrl") or "")
            break
        except Exception:
            if attempt == 2:
                print("结果：failed（发现请求连续失败：隧道抖动，可稍后重试）", file=sys.stderr)
                return 1
            time.sleep(6 * (attempt + 1))
    if not ws_url.startswith("ws"):
        print("结果：failed（未能取得 webSocketDebuggerUrl，检查端点/凭据）", file=sys.stderr)
        return 1

    def close_seed() -> None:
        try:
            close = _rq.Request(f"{endpoint}/fingerprint/{seed}/close", headers=headers, method="POST")
            _rq.build_opener(_rq.ProxyHandler({})).open(close, timeout=15).read()
        except Exception:
            pass

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
        # 隧道偶发抖动，WS 建立给足预算；失败提示里带上可重试说明
        try:
            browser = manager.chromium.connect_over_cdp(ws_url, headers=headers, timeout=60000)
        except Exception as error:
            print(f"结果：failed（CDP 连接失败：{type(error).__name__}；隧道偶发抖动，可直接重试）",
                  file=sys.stderr)
            return 1
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.add_cookies(cookies)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"https://{site}{path}", wait_until="domcontentloaded", timeout=60000)
            # 雷池检测页自己的 JS 跑若干秒；确认按钮出现就点；签到页打开后自动完成签到。
            # 挑战通过时页面会跳转——跳转瞬间 evaluate 会抛
            # "Execution context was destroyed"，那是好事，继续轮询新页面即可。
            clicked = 0
            for _ in range(TIMEOUT_SECONDS // 2):
                page.wait_for_timeout(2000)
                try:
                    text = page.evaluate("document.body ? document.body.innerText : ''") or ""
                except Exception:
                    continue
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
            text = ""
            title = ""
            try:
                text = page.evaluate("document.body ? document.body.innerText : ''") or ""
                title = page.title()
            except Exception:
                pass
            print(f"最终标题：{title}")
            print(f"正文摘要：{' '.join(text.split())[:200]}")
            dump = os.environ.get("CLOAK_DUMP")
            if dump:
                try:
                    with open(dump, "w", encoding="utf-8") as handle:
                        handle.write(page.content())
                    print(f"页面已保存：{dump}")
                except Exception:
                    pass
            if "登录" in title:
                print("结果：failed（停在登录页：Cookie 失效或会话环境不匹配）")
            else:
                print(f"结果：failed（{TIMEOUT_SECONDS} 秒未确认签到）")
            return 1
        finally:
            try:
                browser.close()
            finally:
                close_seed()


if __name__ == "__main__":
    sys.exit(main())
