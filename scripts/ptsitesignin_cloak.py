#!/usr/bin/env python3
"""通过远端 CloakBrowser（cloakserve CDP，nginx Basic Auth + 隧道）签到猪猪。

依赖：playwright（MoviePilot 自带；独立使用时 pip install playwright，无需下载本地浏览器）。

关键设计：
- 稳定 fingerprint seed（默认 piggo-prod）：猪猪的会话深绑登录时浏览器的指纹，
  而 CloakBrowser 的指纹由 seed 决定——同一 seed 每次运行才是"同一个浏览器"，
  登录一次后会话即可长期复用（Cookie 罐持久化在本地）。
- Cookie 罐（默认 /tmp/piggo_cloak_cookies.txt）优先于 PTSIGNIN_COOKIE；
  每次运行结束自动保存最新 Cookie。
- 首次使用或会话失效时：设置 PIGGO_USER / PIGGO_PASS 环境变量（或 --login），
  客户端会在登录页自动填表提交（表单无图片验证码；若账号开了两步验证，
  再设 PIGGO_2FA=动态码）。注意站点规则：连续 10 次登录失败会封 IP。

用法：
    CLOAK_ENDPOINT=https://https://<REDACTED-INSTANCE> CLOAK_AUTH='user:pass' \
    PIGGO_USER='...' PIGGO_PASS='...' \
    python3 scripts/ptsitesignin_cloak.py --login      # 首次：登录 + 签到 + 存 Cookie
    # 之后每天：
    CLOAK_ENDPOINT=... CLOAK_AUTH=... python3 scripts/ptsitesignin_cloak.py

环境变量：CLOAK_ENDPOINT / CLOAK_AUTH 必填；CLOAK_SEED（默认 piggo-prod）、
CLOAK_PROXY、CLOAK_COOKIE_JAR、PIGGO_USER / PIGGO_PASS / PIGGO_2FA 可选。
退出码：0 成功/已签，1 失败，2 配置缺失。
"""
import base64
import os
import re
import signal
import sys
import time

from playwright.sync_api import sync_playwright

SUCCESS_WORDS = ("签到成功", "簽到成功", "成功签到", "打卡成功", "获得")
ALREADY_WORDS = ("今日已签", "今天已签", "已经签到", "已签到", "已簽到", "明日")
BLOCK_WORDS = ("正在被调试", "正在被調試")
SAFELINE_CONFIRM = ("#sl-check", "#slg-box button", "div[id^='slg'] button")
LOGIN_MARK = ("必须登录", "猪猪 :: 登录")
TIMEOUT_SECONDS = 90
JAR = os.environ.get("CLOAK_COOKIE_JAR", "/tmp/piggo_cloak_cookies.txt")
# CF 的 cf_clearance 绑 IP+UA：借 Edge 的通行证（或后续自己的）需要一致的 UA 串
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0")


def _watchdog(seconds: int = 300):
    """硬超时：隧道 TLS 握手可能无限卡死（旧版 playwright 没有覆盖该阶段的
    超时），到点直接退出，绝不挂死。"""
    def _fire(signum, frame):
        print("结果：failed（全局超时：链路长时间无响应，多为隧道抖动，可重试）",
              file=sys.stderr)
        sys.exit(1)
    signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)


def flag(name: str) -> str:
    if name in sys.argv:
        index = sys.argv.index(name)
        return sys.argv[index + 1] if index + 1 < len(sys.argv) else ""
    return ""


def load_cookie_source() -> tuple:
    """返回 (cookie 字符串, 来源标签)。Cookie 罐优先于环境变量。"""
    try:
        with open(JAR, encoding="utf-8") as handle:
            data = handle.read().strip()
        if "=" in data:
            return data, f"Cookie罐({JAR})"
    except OSError:
        pass
    return (os.environ.get("PTSIGNIN_COOKIE") or "").strip(), "PTSIGNIN_COOKIE"


def parse_cookies(header: str, site: str) -> list:
    cookies = []
    for item in header.split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if name:
            cookies.append({"name": name, "value": value, "domain": f".{site}", "path": "/"})
    return cookies


def save_cookie_jar(context, site: str) -> None:
    """把该浏览器上下文里站点的最新 Cookie（含 HttpOnly）存回罐子。"""
    try:
        cookies = context.cookies(f"https://{site}/")
        if not cookies:
            return
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        with open(JAR, "w", encoding="utf-8") as handle:
            handle.write(header)
        os.chmod(JAR, 0o600)
    except Exception:
        pass


def judge(text: str):
    if any(word in text for word in BLOCK_WORDS):
        return "failed", "雷池反调试拦截（该内核也被识别，需进一步排查）"
    if any(word in text for word in SUCCESS_WORDS):
        bonus = re.search(r"获得\s*[\d,.]+\s*(?:个|粒|枚|点)?\s*(?:魔力值|魔力|积分|爆米花)", text)
        return "success", "签到成功" + (f"（{bonus.group(0)}）" if bonus else "")
    if any(word in text for word in ALREADY_WORDS):
        return "already", "今日已经签到"
    return "", ""


def try_login(page, site: str, path: str) -> bool:
    """在登录页自动填表提交。返回是否仍需继续等待结果。"""
    user = os.environ.get("PIGGO_USER", "").strip()
    password = os.environ.get("PIGGO_PASS", "").strip()
    if not (user and password):
        return False
    try:
        page.fill("input[name='username']", user, timeout=8000)
        page.fill("input[name='password']", password, timeout=8000)
        code = os.environ.get("PIGGO_2FA", "").strip()
        if code:
            page.fill("input[name='two_step_code']", code, timeout=8000)
        page.click("input[type='button'][value='登录']", timeout=8000)
        page.wait_for_timeout(6000)
        return True
    except Exception as error:
        print(f"登录填表未完成：{type(error).__name__}", file=sys.stderr)
        return False


def main() -> int:
    _watchdog()
    site = flag("--site") or "piggo.me"
    path = flag("--path") or "/attendance.php"
    endpoint = (os.environ.get("CLOAK_ENDPOINT") or "").strip().rstrip("/")
    auth = (os.environ.get("CLOAK_AUTH") or "").strip()
    cookie, source = load_cookie_source()
    force_login = "--login" in sys.argv
    if not endpoint or (not cookie and not (force_login and os.environ.get("PIGGO_PASS"))):
        print("需要 CLOAK_ENDPOINT 与 Cookie（或 --login + PIGGO_USER/PIGGO_PASS）",
              file=sys.stderr)
        return 2

    headers = {}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    # 稳定 seed：指纹绑定会话，同一 seed 才是同一个浏览器
    seed = os.environ.get("CLOAK_SEED", "piggo-prod").strip() or "piggo-prod"
    import urllib.parse as _uparse
    params = {"fingerprint": seed}
    if os.environ.get("CLOAK_PROXY", "").strip():
        params["proxy"] = os.environ["CLOAK_PROXY"].strip()
    query = "?" + _uparse.urlencode(params)

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
            # UA 与 cf_clearance 绑定：每次运行都先覆盖成同一 UA（可用 CLOAK_UA 覆盖）
            user_agent = os.environ.get("CLOAK_UA", DEFAULT_UA).strip() or DEFAULT_UA
            page = context.pages[0] if context.pages else context.new_page()
            try:
                cdp = context.new_cdp_session(page)
                cdp.send("Emulation.setUserAgentOverride",
                         {"userAgent": user_agent, "platform": "MacIntel"})
            except Exception:
                pass
            if cookie:
                context.add_cookies(parse_cookies(cookie, site))
            page.goto(f"https://{site}{path}", wait_until="domcontentloaded", timeout=120000)
            # 登录页 + 有凭据 → 自动登录一次（会话即属于这个浏览器）
            title = page.title() or ""
            if force_login or any(mark in title for mark in LOGIN_MARK) or True:
                text_probe = ""
                try:
                    text_probe = page.evaluate("document.body ? document.body.innerText : ''") or ""
                except Exception:
                    pass
                if "必须登录" in text_probe or "猪猪 :: 登录" in title:
                    if try_login(page, site, path):
                        print("已提交登录表单，等待会话生效…")
            # 雷池检测页自己的 JS 跑若干秒；确认按钮出现就点；签到页自动完成签到
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
                    save_cookie_jar(context, site)
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
            save_cookie_jar(context, site)
            if "登录" in title:
                print("结果：failed（停在登录页：Cookie 失效且未能自动登录）")
            else:
                print(f"结果：failed（{TIMEOUT_SECONDS} 秒未确认签到）")
            return 1
        finally:
            try:
                browser.close()
            finally:
                try:
                    _rq.build_opener(_rq.ProxyHandler({})).open(
                        _rq.Request(f"{endpoint}/fingerprint/{seed}/close",
                                    headers=headers, method="POST"), timeout=15).read()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
