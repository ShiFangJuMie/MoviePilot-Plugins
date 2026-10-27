"""Browserless Cloud 的 BrowserQL（BQL）引擎：付费 solve 求解 Turnstile + 代理出口。

与自建 Function 引擎的区别：BQL 是「整段 GraphQL 查询、执行完一次返回」的模型，
没有逐步交互；Turnstile token 由云端付费 ``solve`` 生成（本机指纹过不了的站点
——如 audiences——只能靠它），token 不会触发站点回调，必须由插件在同一个
reconnect 会话里重新打开签到页注入并触发提交（原作者 qingyukb 的实测结论）。

按用户要求**必须配置代理**：``proxy(url:["*"], server:...)`` 作为每个查询的第一步，
让所有请求走自己的家庭出口，而不是 Browserless Cloud 的共享 IP。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

from .core import (SignResult, classify, cloudflare_pending, login_page, target_url)

DEFAULT_CLOUD = "https://production-sfo.browserless.io"
RETRY_DELAY_SECONDS = 3


def bql_endpoint(address: str, token: str = "") -> str:
    """地址规范化成 /stealth/bql 端点；Token 走查询参数（Cloud 鉴权方式）。"""
    address, token = str(address or "").strip(), str(token or "").strip()
    if not address:
        raise ValueError("Browserless Cloud 地址不能为空")
    parsed = urllib.parse.urlparse(address)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Browserless Cloud 地址必须以 http:// 或 https:// 开头")
    if "browserless.io" not in parsed.hostname and not parsed.path:
        # 自填的完整端点原样保留；这里只处理「写根地址」的常见情况
        pass
    path = parsed.path.rstrip("/")
    if not path.endswith("/bql"):
        path = (path + "/bql") if path.endswith("/stealth") else (path + "/stealth/bql")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if token:
        query["token"] = token
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "",
                                    urllib.parse.urlencode(query), ""))


def cookie_objects(cookie_header: str, target: str) -> list:
    parsed = urllib.parse.urlparse(target)
    domain = parsed.hostname
    if not domain:
        return []
    cookies = []
    for item in cookie_header.split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if name:
            cookies.append({"name": name.strip(), "value": value.strip(), "domain": domain,
                            "path": "/", "secure": parsed.scheme == "https", "url": target})
    return cookies


def _proxy_server(proxy: str) -> str:
    """校验并规范化代理地址；凭据保留在 URL 里（BQL proxy 的 Basic Auth 约定）。"""
    value = str(proxy or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ValueError("BQL 引擎必须配置代理（http://、https:// 或 socks5://，"
                         "可含用户名密码）， Cloud 共享出口不用于签到")
    return value


# ---------------------------------------------------------------- 注入脚本

def _inject_script(rule: Mapping[str, Any], token: str) -> str:
    """第二段 evaluate：把 solve 的 token 回填页面并触发站点提交。

    原作者实测：solve 只生成 token 不触发回调；widget 常在 closed shadow DOM 里，
    页面 JS 读不到输入框。依次尝试：页面自有 token → 写入全部
    cf-turnstile-response 输入框（含 open shadow DOM）→ data-callback / 常见
    全局回调名 → 同源表单 POST 兜底（NexusPHP 契约：POST /attendance.php 带
    cf-turnstile-response）。返回诊断 JSON（不回传 token 本身）。
    """
    pending = json.dumps([str(item) for item in (rule.get("pending_keywords") or [])],
                         ensure_ascii=False)
    return f"""(async () => {{
      const solveToken = {json.dumps(token)};
      const pendingWords = {pending};
      const budget = 20000;
      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      const diag = {{src: null, cb: null, form: false, post: null, still: null}};
      const pick = () => {{
        const roots = [document];
        for (const node of document.querySelectorAll('*')) {{
          if (node.shadowRoot) roots.push(node.shadowRoot);
        }}
        let inputs = [];
        for (const root of roots) {{
          inputs = inputs.concat([...root.querySelectorAll(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]')]);
        }}
        return {{roots, inputs}};
      }};
      // 1) 等组件渲染（导航后异步渲染，过早注入会落空）
      let picked = pick();
      for (let i = 0; i < 40 && !picked.inputs.length; i++) {{
        await sleep(250);
        picked = pick();
      }}
      diag.rendered = picked.inputs.length;
      // 2) token 写入全部响应输入框；页面自己已产出 token 时优先用它
      let pageToken = '';
      try {{ pageToken = window.turnstile?.getResponse() || ''; }} catch (e) {{}}
      const useToken = pageToken || solveToken;
      diag.src = pageToken ? 'page' : 'solve';
      for (const input of picked.inputs) input.value = useToken;
      // 3) 调站点回调
      const names = picked.roots.flatMap(r => [...r.querySelectorAll('[data-callback]')]
        .map(n => n.getAttribute('data-callback')));
      names.push('cfCallback', 'TurnstileCallback', 'turnstileCallback',
                 'onTurnstileSuccess', 'onTurnstileVerify');
      const called = names.find(name => name && typeof window[name] === 'function');
      if (called) {{
        try {{ window[called](useToken); diag.cb = called; }} catch (e) {{ diag.cb = 'error'; }}
      }}
      // 4) 回调没跑成 / 页面仍 pending：同源表单 POST 兜底
      const stillPending = () => {{
        const text = document.body?.innerText || '';
        return pendingWords.length && pendingWords.some(w => text.includes(w));
      }};
      for (let i = 0; i < 20 && !diag.post; i++) {{
        await sleep(500);
        if (!stillPending()) break;
        if (diag.cb) continue;
        const input = pick().inputs[0];
        const form = (input && input.form) ||
          [...document.forms].find(f => /attendance|checkin|sign.?in/i.test(f.action)) ||
          [...document.forms].find(f => (f.method || 'get').toLowerCase() === 'post');
        if (!form) break;
        diag.form = true;
        const action = new URL(form.getAttribute('action') || location.href, location.href);
        if (action.origin !== location.origin) break;
        const data = new FormData(form);
        data.set('cf-turnstile-response', useToken);
        try {{
          const response = await fetch(action.href, {{method: 'POST', credentials: 'same-origin', body: data}});
          diag.post = response.status;
          document.documentElement.innerHTML = await response.text();
        }} catch (e) {{ diag.post = -1; }}
      }}
      diag.still = stillPending();
      return JSON.stringify(diag);
    }})()"""


FARM_SCRIPT = """(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const source = [...document.querySelectorAll('script[src]')]
    .map(s => s.getAttribute('src')).find(src => src && src.includes('/slide_check_'));
  if (!source) return JSON.stringify({step: 'no-script'});
  const script = await (await fetch(new URL(source, location.href))).text();
  const match = /["']([^"'\\s]*set_access_token-[^"'\\s]+)["']/.exec(script);
  if (!match) return JSON.stringify({step: 'no-token'});
  const response = await fetch(new URL(match[1], location.href), {headers: {Referer: location.href}});
  return JSON.stringify({step: 'hit', status: response.status});
})()"""


class BQLSigner:
    """Browserless Cloud（BQL / 付费 solve）签到器：强制代理出口。"""

    def __init__(self, address: str, token: str, timeout: int = 120, proxy: str = ""):
        # 用户明确要求：Cloud 的共享 IP 不用于签到，必须自带家庭代理出口
        self.proxy_server = _proxy_server(proxy)
        self.endpoint = bql_endpoint(address, token)
        self.token = str(token or "").strip()
        self.timeout = max(30, min(int(timeout or 120), 300))

    # -------------------------------------------------------------- 传输层

    def _request(self, variables: Mapping[str, Any], operation: str, query: str,
                 timeout: Optional[int] = None, endpoint: Optional[str] = None,
                 read_only: bool = False) -> Any:
        payload = json.dumps({"query": query, "operationName": operation,
                              "variables": variables}).encode()
        url = str(endpoint or self.endpoint)
        read_timeout = max(15, min(int(timeout or self.timeout), 300))
        # 只读请求允许网络抖动重试一次；会执行提交的请求绝不重放
        attempts = 2 if read_only else 1
        value: Any = None
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=payload,
                                             headers={"Content-Type": "application/json"},
                                             method="POST")
            started = time.monotonic()
            try:
                # Cloud API 的连接本身不走站点代理（那是浏览器出口）
                with urllib.request.build_opener(
                        urllib.request.ProxyHandler({})).open(request, timeout=read_timeout) as response:
                    value = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    return SignResult("failed", f"Browserless Cloud 鉴权失败（HTTP {exc.code}），请检查 API Token")
                if exc.code == 429:
                    return SignResult("failed", "Browserless Cloud 额度不足或请求过于频繁（HTTP 429）")
                if exc.code >= 500 and attempt < attempts:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                return SignResult("failed", f"Browserless Cloud HTTP {exc.code}（步骤 {operation}）")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt < attempts:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                kind = type(exc).__name__
                return SignResult("failed", f"Browserless Cloud 请求中断（{kind}，"
                                            f"步骤 {operation}，耗时 {time.monotonic() - started:.0f}s）",
                                  transient=True)
        errors = value.get("errors") or []
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, Mapping):
            data = {}
        if errors and not data:
            first = errors[0] if isinstance(errors[0], Mapping) else {}
            return SignResult("failed", "BQL 查询被拒绝：" +
                              str(first.get("message") or "未知错误")[:160])
        # 单步失败不判整轮失败（如 solve 超时但页面已签到）；错误留给上层诊断
        return data.get(operation) if isinstance(data.get(operation), Mapping) else data

    @staticmethod
    def _proxy_step() -> str:
        return ('proxy(url: ["*"], server: $proxyServer) { time }')

    @staticmethod
    def _step_errors(raw: Any) -> str:
        return ""  # GraphQL errors 已在 _request 汇总；步骤级诊断走返回字段

    # -------------------------------------------------------------- 查询

    def _preflight_query(self) -> str:
        return """mutation CheckInPreflight($cookies: [CookieInput!]! $url: String!
                   $proxyServer: String! $beforeWait: Float!) {
      %s
      cookies(cookies: $cookies) { cookies { name } }
      goto(url: $url, waitUntil: domContentLoaded) { status }
      waitForTimeout(time: $beforeWait) { time }
      before: html { html }
    }""" % self._proxy_step()

    def _solve_query(self) -> str:
        # solve 只生成 token；reconnect 保住浏览器（cookie / cf_clearance）供第二段复用
        return """mutation CheckInSolve($cookies: [CookieInput!]! $url: String!
                   $proxyServer: String! $beforeWait: Float! $solveTimeout: Float!
                   $reconnectTimeout: Float!) {
      %s
      cookies(cookies: $cookies) { cookies { name } }
      goto(url: $url, waitUntil: domContentLoaded) { status }
      waitForTimeout(time: $beforeWait) { time }
      solve: solve(timeout: $solveTimeout) { found solved time token }
      session: reconnect(timeout: $reconnectTimeout) { browserQLEndpoint }
    }""" % self._proxy_step()

    def _inject_query(self, rule: Mapping[str, Any]) -> str:
        steps = [
            self._proxy_step(),
            "cookies(cookies: $cookies) { cookies { name } }",
            "goto(url: $url, waitUntil: domContentLoaded) { status }",
            "waitForTimeout(time: $beforeWait) { time }",
            "submit: evaluate(content: $inject, timeout: $injectTimeout) { value }",
            "waitForTimeout(time: $wait) { time }",
            "after: html { html }",
        ]
        if rule.get("verify_success_path"):
            steps.append("verify: goto(url: $verifyUrl, waitUntil: domContentLoaded) { status }")
            steps.append("waitForTimeout(time: $verifyWait) { time }")
            steps.append("verifyPage: html { html }")
        return """mutation CheckInSubmit($cookies: [CookieInput!]! $url: String!
                   $proxyServer: String! $beforeWait: Float! $inject: String!
                   $injectTimeout: Float! $wait: Float!) {
      %s
    }""" % "\n      ".join(steps)

    def _click_query(self, rule: Mapping[str, Any]) -> str:
        steps = [
            self._proxy_step(),
            "cookies(cookies: $cookies) { cookies { name } }",
            "goto(url: $url, waitUntil: domContentLoaded) { status }",
            "waitForTimeout(time: $beforeWait) { time }",
            "before: html { html }",
        ]
        # 雷池确认按钮（piggo）：不存在时该步报错，其余步骤照常返回
        if any("安全检测" in str(word) for word in (rule.get("pending_keywords") or [])):
            steps.append('wafWait: waitForSelector(selector: "#sl-check", timeout: 8000,'
                         " visible: true) { selector }")
            steps.append('wafClick: click(selector: "#sl-check") { selector }')
        # Altcha（yemapt）：先勾选复选框触发工作量证明，再点提交
        altcha = str(rule.get("altcha_checkbox_selector") or "").strip()
        if altcha:
            steps.append(f"altchaWait: waitForSelector(selector: {json.dumps(altcha)},"
                         " timeout: 15000, visible: true) { selector }")
            steps.append(f"altchaClick: click(selector: {json.dumps(altcha)}) {{ selector }}")
            steps.append("waitForTimeout(time: 6) { time }")
        steps += [
            "target: waitForSelector(selector: $selector, timeout: $buttonWait, visible: true) { selector }",
            "submit: click(selector: $selector) { selector }",
            "waitForTimeout(time: $wait) { time }",
            "after: html { html }",
        ]
        return """mutation CheckInClick($cookies: [CookieInput!]! $url: String!
                   $proxyServer: String! $beforeWait: Float! $selector: String!
                   $buttonWait: Float! $wait: Float!) {
      %s
    }""" % "\n      ".join(steps)

    def _farm_query(self) -> str:
        return """mutation CheckInFarm($cookies: [CookieInput!]! $url: String!
                   $proxyServer: String! $beforeWait: Float! $wait: Float!) {
      %s
      cookies(cookies: $cookies) { cookies { name } }
      goto(url: $url, waitUntil: domContentLoaded) { status }
      waitForTimeout(time: $beforeWait) { time }
      farm: evaluate(content: $farm, timeout: 20000) { value }
      waitForTimeout(time: $wait) { time }
      reload: goto(url: $url, waitUntil: domContentLoaded) { status }
      after: html { html }
    }""" % self._proxy_step()

    # -------------------------------------------------------------- 流程

    def sign(self, site: Mapping[str, Any], rule: Mapping[str, Any]) -> SignResult:
        base = str(site.get("url") or "").strip()
        cookie = str(site.get("cookie") or "").strip()
        if not base or not cookie:
            return SignResult("failed", "站点地址或 Cookie 为空")
        started = time.monotonic()

        def note(extra: str = "") -> str:
            parts = [f"耗时={time.monotonic() - started:.1f}秒"]
            if extra:
                parts.insert(0, extra)
            return "；" + "；".join(parts)

        target = target_url(base, str(rule.get("path") or "/attendance.php"),
                            rule.get("route_fragment"))
        cookies = cookie_objects(cookie, target)
        if not cookies:
            return SignResult("failed", "站点 Cookie 格式无效")
        user_agent = str(site.get("ua") or "").strip()
        mode = str(rule.get("mode") or "cloudflare")

        # 预检：可能已签到，直接判定，省一次 solve 的额度
        preflight_variables = {"cookies": cookies, "url": target,
                               "proxyServer": self.proxy_server,
                               "beforeWait": min(float(rule.get("preflight_wait") or 4000) / 1000, 15)}
        if user_agent:
            preflight_variables["userAgent"] = user_agent
        preflight = self._request(preflight_variables, "CheckInPreflight",
                                  self._preflight_query(),
                                  timeout=self.timeout, read_only=True)
        if isinstance(preflight, SignResult):
            return preflight
        page = str((preflight.get("before") or {}).get("html") or "")
        if page:
            judged = classify(page, rule)
            if judged.status in {"success", "already"}:
                return judged
            if login_page(page):
                return SignResult("failed", "签到页跳转到了登录页，Cookie 可能已失效" + note())
            if cloudflare_pending(page) and mode != "cloudflare":
                return SignResult("failed", "页面停在 Cloudflare / 雷池检测页" + note())

        # 0ff 的滑块直连逻辑在页面上下文里执行（与 sites/farm.py 同一套流程）
        if "0ff." in (urllib.parse.urlparse(base).hostname or ""):
            return self._farm_flow(cookies, target, rule, note)
        if mode == "cloudflare":
            return self._solve_flow(base, target, cookies, rule, user_agent, note)
        return self._click_flow(cookies, target, rule, note)

    def _solve_flow(self, base: str, target: str, cookies: list, rule: Mapping[str, Any],
                    user_agent: str, note) -> SignResult:
        solve_timeout = min(float(rule.get("solve_timeout") or 60), 120)
        solved = self._request(
            {"cookies": cookies, "url": target, "proxyServer": self.proxy_server,
             "beforeWait": min(float(rule.get("preflight_wait") or 4000) / 1000, 15),
             "solveTimeout": solve_timeout, "reconnectTimeout": 120.0},
            "CheckInSolve", self._solve_query(),
            timeout=int(solve_timeout + self.timeout / 2) + 45)
        if isinstance(solved, SignResult):
            return solved
        solution = solved.get("solve") or {}
        token = str(solution.get("token") or "")
        if not solution.get("solved") or not token:
            # solve 没拿到 token：页面可能根本没挂挑战（重开核验），否则如实报失败
            after = str((solved.get("before") or solved.get("after") or {}).get("html") or "")
            if after:
                judged = classify(after, rule)
                if judged.status in {"success", "already"}:
                    return judged
            return SignResult("failed", "云端 solve 未取得 Turnstile token，未提交签到" + note())
        session = solved.get("session") or {}
        endpoint = str(session.get("browserQLEndpoint") or "")
        if endpoint and self.token:
            parsed = urllib.parse.urlparse(endpoint)
            endpoint = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "",
                                               urllib.parse.urlencode({"token": self.token}), ""))
        variables = {"cookies": cookies, "url": target, "proxyServer": self.proxy_server,
                     "beforeWait": min(float(rule.get("preflight_wait") or 4000) / 1000, 15),
                     "inject": _inject_script(rule, token), "injectTimeout": 40.0,
                     "wait": min(float(rule.get("wait") or 8), 30)}
        if rule.get("verify_success_path"):
            variables["verifyUrl"] = target_url(base, str(rule["verify_success_path"]))
            variables["verifyWait"] = min(float(rule.get("verify_wait") or 6), 20)
        result = self._request(variables, "CheckInSubmit", self._inject_query(rule),
                               timeout=self.timeout, endpoint=endpoint or None)
        if isinstance(result, SignResult):
            return result
        after = str((result.get("verifyPage") or result.get("after") or {}).get("html") or "")
        if not after:
            return SignResult("failed", "注入阶段未返回页面，签到结果未确认" + note())
        if cloudflare_pending(after):
            return SignResult("failed", "提交后仍停在 Cloudflare 检测页" + note())
        judged = classify(after, rule)
        if judged.status == "failed":
            return SignResult("failed", judged.message + note())
        return judged

    def _click_flow(self, cookies: list, target: str, rule: Mapping[str, Any], note) -> SignResult:
        selector = str(rule.get("submit_selector") or "input[type='submit'], button, a")
        result = self._request(
            {"cookies": cookies, "url": target, "proxyServer": self.proxy_server,
             "beforeWait": min(float(rule.get("preflight_wait") or 6000) / 1000, 15),
             "selector": selector,
             "buttonWait": min(float(rule.get("button_wait") or 20000) / 1000, 60),
             "wait": min(float(rule.get("wait") or 8000) / 1000, 30)},
            "CheckInClick", self._click_query(rule), timeout=self.timeout)
        if isinstance(result, SignResult):
            return result
        after = str((result.get("after") or {}).get("html") or "")
        if not after:
            return SignResult("failed", "点击流程未返回页面，签到结果未确认" + note())
        if cloudflare_pending(after):
            return SignResult("failed", "点击后仍停在 Cloudflare / 雷池检测页"
                              "（该站的反自动化检测可能拦截了云端浏览器）" + note())
        judged = classify(after, rule)
        if judged.status == "failed":
            return SignResult("failed", judged.message + note())
        return judged

    def _farm_flow(self, cookies: list, target: str, rule: Mapping[str, Any], note) -> SignResult:
        result = self._request(
            {"cookies": cookies, "url": target, "proxyServer": self.proxy_server,
             "beforeWait": 4.0, "farm": FARM_SCRIPT, "wait": 3.0},
            "CheckInFarm", self._farm_query(), timeout=self.timeout)
        if isinstance(result, SignResult):
            return result
        after = str((result.get("after") or {}).get("html") or "")
        if not after:
            return SignResult("failed", "农场流程未返回页面，签到结果未确认" + note())
        judged = classify(after, rule)
        if judged.status == "failed":
            return SignResult("failed", judged.message + note())
        return judged
