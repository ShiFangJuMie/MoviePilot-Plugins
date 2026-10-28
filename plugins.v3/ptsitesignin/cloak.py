"""CloakBrowser（cloakserve CDP）引擎：远程反检测 Chromium，专攻雷池这类
反自动化 WAF 后面、又要求「出口 IP 与登录浏览器一致」的站点（猪猪）。

与其它引擎的分工：
- 自建 Browserless（Function）：通用点击 / 填表，但普通 Chromium 会被雷池的
  反调试识别（「当前环境正在被调试」阻断页）。
- FlareSolverr / TRAWL：解 Cloudflare / Turnstile 为主，元素交互能力弱。
- CloakBrowser：C++ 源码级补丁的 Chromium，是实测唯一能过猪猪雷池挑战的
  远程内核；CDP 连接（playwright connect_over_cdp），需要 cloakserve 服务
  （可挂 nginx Basic Auth 后面，凭据写进地址即可）。

猪猪路线（2026-10-28 实测打通）的三个必要条件：
1. 站点 Cookie 用**新鲜**的整串（cf_clearance + sl-* + 会话 Cookie）——站点
   会在新登录时轮换会话令牌，旧串在服务端立即失效；
2. UA 与签发 Cookie 的浏览器同族（cf_clearance 绑 UA；默认按 Chrome 处理，
   站点管理里设了 UA 则优先用站点的）；
3. 浏览器实例出口 IP 与登录时一致——经家宽 Clash 代理时必须让目标域拨 IPv4
   （例如 Clash hosts 钉死 A 记录），否则会话按精确 IP 校验直接判游客。

本模块不依赖 MoviePilot，方便单独测试与复用。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional

from .core import (SignResult, _CLICK_MARKED_SELECTOR, _MARK_CLICK_JS, _PAGE_CLICK_JS,
                   classify, cookie_list_for_context, login_page)

# 会话按精确出口 IP 校验的站点，浏览器实例必须经家宽 v4 代理出网
DEFAULT_CLOAK_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
# 雷池交互挑战页的「确认」按钮（challenge.js 动态注入文案，直接按 id 点）
SL_CHECK_CLICK_JS = """
() => {
  const btn = document.getElementById('sl-check');
  if (btn && btn.offsetParent !== null) { btn.click(); return true; }
  return false;
}
"""
CF_CHECKBOX_FRAME = "challenges.cloudflare.com"
CHALLENGE_POLL_SECONDS = 3
DISCOVERY_TIMEOUT = 30


def split_basic_auth(address: str, auth: str = "") -> tuple:
    """把 https://user:pass@host/ 里的凭据转成 (净地址, Basic 头)。

    显式 auth="user:pass" 兜底；两者都没有时 Basic 头为空串（服务未挂鉴权）。
    """
    raw = str(address or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    basic = ""
    if parsed.username:
        basic = base64.b64encode(
            f"{urllib.parse.unquote(parsed.username)}:"
            f"{urllib.parse.unquote(parsed.password or '')}".encode()).decode()
        raw = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1],
                                       parsed.path, parsed.query, parsed.fragment))
    elif str(auth or "").strip():
        basic = base64.b64encode(str(auth).strip().encode()).decode()
    return raw.rstrip("/"), basic


def discover_ws_url(endpoint: str, seed: str, proxy: str, basic: str) -> str:
    """发现 cloakserve 实例的 webSocketDebuggerUrl。

    - 401 立即报错不重试（服务挂 nginx Basic Auth + fail2ban，反复 401 会封 IP）；
    - 其它 4xx（如 seed 非法）同样不重试——重试没有意义；
    - 网络类错误（隧道偶发 SSLEOF）退避重试三次。
    """
    params = {"fingerprint": seed}
    if str(proxy or "").strip():
        params["proxy"] = str(proxy).strip()
    query = "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": "Basic " + basic} if basic else {}
    request = urllib.request.Request(f"{endpoint}/json/version{query}", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=DISCOVERY_TIMEOUT) as response:
                return str(json.loads(response.read().decode()).get("webSocketDebuggerUrl") or "")
        except urllib.error.HTTPError as error:
            body = b""
            try:
                body = error.read()[:200]
            except Exception:
                pass
            if error.code == 401:
                raise PermissionError("CloakBrowser 鉴权失败（401）：检查地址里的凭据") from error
            if 400 <= error.code < 500:
                raise ConnectionError(
                    f"CloakBrowser 服务拒绝（{error.code}）：{body.decode('utf-8', 'replace')}") from error
            last_error = error
            time.sleep(4 * (attempt + 1))
        except Exception as error:  # 隧道偶发 SSLEOF：退避重试
            last_error = error
            time.sleep(4 * (attempt + 1))
    raise ConnectionError(f"CloakBrowser 发现请求连续失败：{type(last_error).__name__}")


def close_instance(endpoint: str, seed: str, basic: str) -> None:
    """免费层 1 并发：显式关掉本次指纹实例，避免残留连接卡死服务。"""
    headers = {"Authorization": "Basic " + basic} if basic else {}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(urllib.request.Request(f"{endpoint}/fingerprint/{seed}/close",
                                           headers=headers, method="POST"), timeout=15).read()
    except Exception:
        pass


def _attempt_once(sync_playwright, site: Mapping[str, Any], rule: Mapping[str, Any],
                  endpoint: str, seed: str, basic: str, timeout: int,
                  ua: str, proxy: str, logger: Any) -> SignResult:
    """单次实例生命周期：连接 → 注入 → 打开 → 等验证/点确认 → 判定。

    sync_playwright 是 playwright.sync_api.sync_playwright 工厂函数（测试注入替身）。
    """
    headers = {"Authorization": "Basic " + basic} if basic else {}
    ws_url = discover_ws_url(endpoint, seed, proxy, basic)
    if not ws_url:
        return SignResult("failed", "CloakBrowser 未返回 webSocketDebuggerUrl")
    with sync_playwright() as manager:
        browser = manager.chromium.connect_over_cdp(ws_url, headers=headers, timeout=60000)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            cdp = context.new_cdp_session(page)
            # UA 与 cf_clearance 绑定：每次运行都覆盖成同一 UA（规则 > 站点 > 默认 Chrome）
            cdp.send("Emulation.setUserAgentOverride",
                     {"userAgent": ua, "platform": "MacIntel", "acceptLanguage": "en,zh-CN,en-US"})
            base_url = str(site.get("url") or "").rstrip("/")
            cookie = str(site.get("cookie") or "").strip()
            if cookie:
                context.add_cookies(cookie_list_for_context(cookie, base_url))
            url = base_url + str(rule.get("path") or "/attendance.php")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except Exception as error:  # 挑战通过瞬间的导航竞态：容忍，靠轮询续判
                if logger:
                    logger.debug("PT站点签到：cloak goto 异常 %s", type(error).__name__)
            clicked = 0
            submits = 0
            turnstile = 0
            failures = 0
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    page.wait_for_timeout(CHALLENGE_POLL_SECONDS * 1000)
                    html = page.content() or ""
                    failures = 0
                except Exception:
                    # 挑战通过/点击后的导航会瞬时销毁执行上下文：容忍过渡期，继续轮询
                    failures += 1
                    if failures >= 6:
                        break
                    continue
                judged = classify(html, rule)
                if judged.status in {"success", "already"}:
                    return judged
                if login_page(html):
                    return SignResult(
                        "failed",
                        "CloakBrowser 会话未被接受（停在登录页）：优先检查站点 Cookie 是否"
                        "新鲜（站点换浏览器重登后旧 Cookie 立即失效）、出口 IP 是否与登录时"
                        "一致（家宽 Clash 需对目标域拨 IPv4）、UA 是否与签发 Cookie 的浏览器同族")
                # 雷池交互挑战出现「确认」按钮就点（脚本自己判断可见性，文案由 JS 注入不可靠）
                if clicked < 3:
                    try:
                        if page.evaluate(SL_CHECK_CLICK_JS):
                            clicked += 1
                            continue
                    except Exception:
                        pass
                # 站内签到按钮（submit_method=dom_click）：两段式点击——先标记再用真实
                # 鼠标事件点（被遮罩挡住时退回 JS 点击）；点完继续轮询等站点回调/页面刷新
                if submits < 3 and str(rule.get("submit_method") or "").lower() == "dom_click" \
                        and str(rule.get("submit_text") or "").strip():
                    try:
                        args = [str(rule.get("submit_selector") or ""),
                                str(rule.get("submit_text") or "").strip()]
                        if page.evaluate(_MARK_CLICK_JS, args):
                            try:
                                page.click(_CLICK_MARKED_SELECTOR, timeout=10000)
                                submits += 1
                            except Exception:
                                submits += 1 if page.evaluate(_PAGE_CLICK_JS, args) else 0
                    except Exception:
                        pass
                # Cloudflare 勾选式验证（规则 turnstile_click）：iframe 里的复选框点一下
                if turnstile < 3 and bool(rule.get("turnstile_click")):
                    for frame in getattr(page, "frames", []) or []:
                        if CF_CHECKBOX_FRAME in str(getattr(frame, "url", "") or ""):
                            try:
                                frame.locator("input[type='checkbox']").first.click(timeout=3000)
                                turnstile += 1
                            except Exception:
                                pass
                            break
            return SignResult("failed", f"CloakBrowser {timeout} 秒内未确认签到结果"
                                        "（多半停在 Cloudflare / 雷池验证页）")
        finally:
            try:
                browser.close()
            except Exception:
                pass
            close_instance(endpoint, seed, basic)


def cloak_sign(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 150, *,
               endpoint: str, proxy: str = "", auth: str = "",
               logger: Any = None) -> SignResult:
    """CloakBrowser 引擎入口：实例崩溃（免费层不稳）自动换 seed 重试一次。"""
    address, basic = split_basic_auth(endpoint, auth)
    if not address:
        return SignResult("failed", "未配置 CloakBrowser 地址")
    ua = str(rule.get("user_agent") or "").strip() or str(site.get("ua") or "").strip() \
        or DEFAULT_CLOAK_UA
    try:
        from playwright.sync_api import sync_playwright as _pw  # MoviePilot 自带 playwright
    except ImportError:
        return SignResult("failed", "缺少 playwright 依赖，无法连接 CloakBrowser")
    host = urllib.parse.urlsplit(str(site.get("url") or "")).hostname or "site"
    # cloakserve 的 fingerprint seed 只接受字母数字和连字符（域名里的点会被 400 拒绝）
    safe_host = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-") or "site"
    budget = max(60, min(int(timeout or 150), 150))
    last: Optional[SignResult] = None
    for attempt in range(2):
        # 稳定 seed（每站一个）：同一站每次都是"同一个浏览器"；崩溃重试时换新 seed 避免残留
        seed = f"ptsignin-{safe_host}" if attempt == 0 else f"ptsignin-{safe_host}-{int(time.time())}"
        try:
            return _attempt_once(_pw, site, rule, address, seed, basic,
                                 budget, ua, proxy, logger)
        except (PermissionError, ValueError) as error:
            return SignResult("failed", str(error))
        except Exception as error:
            last = SignResult("failed", f"CloakBrowser 实例异常（{type(error).__name__}），"
                                        "免费层偶发崩溃，可重试")
            if logger:
                logger.warning("PT站点签到：cloak 第 %d 次实例异常 %s", attempt + 1,
                               type(error).__name__)
            time.sleep(6)
    return last or SignResult("failed", "CloakBrowser 签到失败")
