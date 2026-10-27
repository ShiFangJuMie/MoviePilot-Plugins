"""FlareSolverr v3.5+ 原生会话签到：Turnstile 验证与表单提交在同一个浏览器会话内完成。

只使用官方 API，不修改 FlareSolverr：sessions.create / request.get(cookies) /
request.get(tabs_till_verify) / request.post(postData) / sessions.destroy。
其中 ``tabs_till_verify`` 是 v3.5.0 起官方支持的「按 N 次 Tab 聚焦 Turnstile
复选框后按 Space 勾选」解法，求解结果放在 ``solution.turnstile_token``。

官方在同一次 request.get 里 Cookie 会在「求解之后」才写入；而萝莉
（mua.xloli.cc）未登录访问签到页是 404、访问首页直接断连（Chrome 错误页
origin 为空，add_cookie 报 invalid cookie domain），因此拆成会话内多步：
在真实页面（login.php）写入 Cookie → 打开签到页 → 求解 → 同会话提交
（token 一次性有效，且服务端 siteverify 会核对提交 IP，必须与求解同源）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Mapping

from .core import (SignResult, append_hint, classify, cloudflare_pending, find_signin_form,
                   login_page, tabs_till_turnstile, target_url, turnstile_widget_in)


def turnstile_auto_submits(html: str) -> bool:
    """Turnstile 组件带 data-callback（观众 / OurBits 这类）时由站点 JS 自动提交表单。

    这类页面没有提交按钮：验证完成的一瞬间回调就 form.submit()，随后页面跳转。
    """
    for tag in re.findall(r"<div\b[^>]*cf-turnstile[^>]*>", html or "", re.IGNORECASE):
        if "data-callback" in tag:
            return True
    return False


def callback_form(html: str, base: str, default_path: str):
    """回调兜底用：从页面提取含 Turnstile 的表单，返回 (POST 地址, 字段) 或 None。

    不要求表单有提交按钮（回调站点本来就没有），字段取全部具名控件初值。
    """
    for match in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html or "", re.IGNORECASE | re.DOTALL):
        attrs_raw, inner = match.group(1), match.group(2)
        if "cf-turnstile-response" not in inner:
            continue
        attrs = {name.lower(): value.strip("\"'")
                 for name, value in re.findall(
                     r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", attrs_raw)}
        action = urllib.parse.urljoin(base + "/", attrs.get("action") or default_path)
        if urllib.parse.urlsplit(action).netloc != urllib.parse.urlsplit(base).netloc:
            return None
        fields = {}
        for tag in re.findall(r"<input\b[^>]*>", inner, re.IGNORECASE):
            item = {name.lower(): value.strip("\"'")
                    for name, value in re.findall(
                        r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", tag)}
            name = str(item.get("name") or "").strip()
            if not name or (item.get("type") or "").lower() in {"submit", "button", "image", "reset"}:
                continue
            fields[name] = str(item.get("value") or "")
        return action, fields
    return None


def service_url(address: str) -> str:
    """接受服务根地址或完整 /v1 端点；保留显式路径前缀（反向代理场景）。"""
    parsed = urllib.parse.urlsplit(str(address or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("FlareSolverr 地址必须是完整的 http:// 或 https:// 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("FlareSolverr 地址不能包含凭据、查询串或 URL 片段")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class FlareSolverrSigner:
    """在一个 FlareSolverr 会话里完成「带 Cookie 打开 → 解 Turnstile → 提交签到」。"""

    def __init__(self, address: str, timeout: int = 120, proxy: str = ""):
        self.endpoint = service_url(address)
        # 求解调用内部按 ~10 秒一轮重试（暂停 5 秒 + Tab 串 + 等响应）。
        # 单次求解预算封顶 75 秒：命中复选框通常 30 秒内完成，而 Cloudflare 隧道
        # 一类网关常在 100 秒左右切断长连接——页面加载加上预算必须留在界内，
        # 顶满反而拿不到服务端的错误原因。
        self.timeout = max(45, min(int(timeout or 120), 90))
        self.proxy = self._parse_proxy(proxy)

    @staticmethod
    def _parse_proxy(proxy: str) -> dict:
        """会话级出口代理（FlareSolverr 原生 sessions.create proxy，支持用户名密码）。

        Cloudflare 的验证判定综合浏览器与出口 IP：让 FlareSolverr 走与日常浏览器
        相同的代理出口，可以排除 IP 因素（audiences 这类站点值得这样试一次）。
        """
        if not str(proxy or "").strip():
            return {}
        parsed = urllib.parse.urlsplit(str(proxy).strip())
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
            raise ValueError("FlareSolverr 代理必须是 http://、https:// 或 socks5:// 地址")
        data = {"url": urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], "", "", ""))}
        if parsed.username:
            data["username"] = urllib.parse.unquote(parsed.username)
            data["password"] = urllib.parse.unquote(parsed.password or "")
        return data

    def _budget(self, seconds: int) -> int:
        return min(self.timeout, seconds)

    def _call(self, payload: Mapping[str, Any], budget: int) -> Any:
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        # MoviePilot → 自建服务的连接不走浏览器出口代理。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=budget + 20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # 求解失败（如 Tab 次数没落在复选框上）时 bottle 以 500 返回 JSON 错误体。
            body = error.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except ValueError:
                raise

    @staticmethod
    def _html(response: Any) -> str:
        solution = response.get("solution") if isinstance(response, Mapping) else None
        return str((solution or {}).get("response") or "") if isinstance(solution, Mapping) else ""

    @staticmethod
    def _cookies(cookie: str) -> list:
        pairs = []
        for item in cookie.split(";"):
            if "=" not in item:
                continue
            name, _, value = item.partition("=")
            if name.strip():
                pairs.append({"name": name.strip(), "value": value.strip()})
        return pairs

    def sign(self, site: Mapping[str, Any], rule: Mapping[str, Any]) -> SignResult:
        base = str(site.get("url") or "").strip()
        cookie = str(site.get("cookie") or "").strip()
        if not base or not cookie:
            return SignResult("failed", "站点地址或 Cookie 为空")
        url = target_url(base, str(rule.get("path") or "/attendance.php"))
        session = "ptsignin-" + uuid.uuid4().hex[:12]
        started = time.monotonic()
        notes = []
        try:
            create_payload = {"cmd": "sessions.create", "session": session}
            if self.proxy:
                create_payload["proxy"] = self.proxy
            created = self._call(create_payload, 30)
            if str(created.get("status")) != "ok":
                return SignResult("failed", "FlareSolverr 会话创建失败：" + self._reason(created))
            try:
                return self._sign_in_session(site, rule, base, url, session, cookie, notes, started)
            finally:
                try:
                    self._call({"cmd": "sessions.destroy", "session": session}, 30)
                except Exception:
                    pass
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            kind = type(cause).__name__ if isinstance(cause, BaseException) else "URLError"
            return SignResult("failed", f"FlareSolverr 连接中断或超时，签到结果未确认"
                                        f"（异常={kind}；耗时={time.monotonic() - started:.1f}秒）",
                              transient=True)
        except ValueError:
            return SignResult("failed", "FlareSolverr 返回了无效 JSON，签到结果未确认", transient=True)

    # Cookie 必须写在能正常渲染的同域页面上：Chrome 错误页（404 / 断连）origin 为空，
    # FlareSolverr 的 add_cookie 会报 invalid cookie domain。萝莉未登录访问签到页是
    # 404、访问首页直接断连，login.php 是 NexusPHP 最稳定的真实页面。
    PRIME_FALLBACKS = ("/login.php", "/")

    def _prime(self, base: str, session: str, cookie: str,
               rule: Mapping[str, Any]) -> tuple:
        """把站点 Cookie 写入会话浏览器，返回（是否成功，失败原因）。"""
        paths: list = []
        for candidate in (str(rule.get("flare_prime_path") or "").strip(), *self.PRIME_FALLBACKS):
            if candidate and candidate not in paths:
                paths.append(candidate)
        problem = ""
        for path in paths:
            response = self._call({"cmd": "request.get", "url": target_url(base, path),
                                   "session": session, "cookies": self._cookies(cookie),
                                   "maxTimeout": self._budget(90) * 1000}, self._budget(90))
            if str(response.get("status")) == "ok":
                return True, ""
            problem = self._reason(response)
            if "invalid cookie domain" not in problem.lower():
                return False, problem  # 换路径也解决不了的其他错误
        return False, problem

    def _reopen(self, session: str, url: str, rule: Mapping[str, Any]) -> SignResult:
        """重开签到页核验（回调提交 / 结果不确定时使用）。"""
        opened = self._call({"cmd": "request.get", "url": url, "session": session,
                             "maxTimeout": self._budget(90) * 1000}, self._budget(90))
        if str(opened.get("status")) != "ok":
            return SignResult("failed", "")
        return classify(self._html(opened), rule)

    def _open_sign_page(self, session: str, url: str) -> tuple:
        """打开签到页；雷池这类自放行检测页用官方 waitInSeconds 等它跳过去。"""
        page = ""
        for attempt in range(3):
            payload = {"cmd": "request.get", "url": url, "session": session,
                       "maxTimeout": self._budget(90) * 1000}
            if attempt:
                # 检测页由自己的 JS 跑几秒后放行重定向：等一拍再取页面
                payload["waitInSeconds"] = 8
            opened = self._call(payload, self._budget(90) + (8 if attempt else 0))
            if str(opened.get("status")) != "ok":
                return False, self._reason(opened), ""
            page = self._html(opened)
            if not cloudflare_pending(page):
                return True, "", page
        return False, "", page

    def _diagnostic_shot(self, session: str, url: str) -> None:
        """求解失败后对签到页拍一张诊断截图（页面会重载，但能看到组件形态）。

        存到 ``self._last_screenshot``（PNG 的 base64）；联调脚本负责落盘。
        任何失败都吞掉——诊断不能影响主流程与结果返回。
        """
        self._last_screenshot = ""
        try:
            shot = self._call({"cmd": "request.get", "url": url, "session": session,
                               "returnScreenshot": True,
                               "maxTimeout": self._budget(30) * 1000}, self._budget(30))
            image = str(((shot.get("solution") or {}).get("screenshot")) or "")
            if image:
                self._last_screenshot = image
        except Exception:
            pass

    def _sign_in_session(self, site: Mapping[str, Any], rule: Mapping[str, Any], base: str,
                         url: str, session: str, cookie: str, notes: list, started: float) -> SignResult:
        # 1) 预热：官方在同一次 request.get 里是「先打开、求解之后才写 Cookie 再重载」，
        #    而本站未登录时签到页 404、首页断连，所以先在真实页面上写入 Cookie，
        #    再单独打开签到页做提交前检查（登录态已在会话浏览器里）。
        primed, problem = self._prime(base, session, cookie, rule)
        if not primed:
            return SignResult("failed", "FlareSolverr 写入站点 Cookie 失败：" + problem)
        opened, problem, page = self._open_sign_page(session, url)
        if not opened:
            return SignResult("failed", "FlareSolverr 打开签到页失败：" + problem)
        if cloudflare_pending(page):
            return SignResult("failed", "FlareSolverr 未能通过站点的 Cloudflare / 雷池检测页"
                              + self._note(notes, started))
        judged = classify(page, rule)
        if judged.status in {"success", "already"}:
            return judged
        if login_page(page):
            return SignResult("failed", "签到页跳转到了登录页，Cookie 可能已失效")
        if not page:
            return SignResult("failed", "FlareSolverr 返回了空页面，未确认签到")

        # 2) 站点开了内嵌 Turnstile 时，用官方 tabs_till_verify 求解；
        #    次数由签到页 HTML 按焦点序推算，规则里的 flare_tabs 可手工覆盖。
        # 回调站点（组件带 data-callback，无提交按钮）验证完成时由站点 JS 自动提交。
        auto_submit = turnstile_auto_submits(page)
        manual_token = ""
        if turnstile_widget_in(page):
            tabs = rule.get("flare_tabs") or tabs_till_turnstile(page)
            if not tabs:
                return SignResult("failed", "无法从签到页推算 Tab 按键次数（页面结构变化），"
                                            "请在站点规则里用 flare_tabs 手工指定")
            notes.append(f"Tab次数={tabs}")
            solved = self._call({"cmd": "request.get", "url": url, "session": session,
                                 "tabs_till_verify": tabs,
                                 # TRAWL 扩展：求解页面内嵌的验证组件（官方 FlareSolverr
                                 # 会忽略未知字段，不影响 tabs_till_verify 路线）。
                                 "solveEmbeddedCaptchas": True,
                                 "maxTimeout": self._budget(75) * 1000}, self._budget(75))
            if str(solved.get("status")) != "ok":
                reason = self._reason(solved)
                if auto_submit and any(mark in reason.lower()
                                       for mark in ("stale", "no such element")):
                    # 回调在 FlareSolverr 读到 token 前就提交并跳转了页面——签到很可能
                    # 已经完成，重开签到页核验后再下结论，不能把 stale 当失败。
                    judged = self._reopen(session, url, rule)
                    if judged.status in {"success", "already"}:
                        return judged
                # 拍一张组件状态图供诊断（复用当前会话；不影响失败结论）
                self._diagnostic_shot(session, url)
                hint = f"（本次按 {tabs} 次 Tab 未命中复选框；页面若调整过，请更新 flare_tabs）"
                return SignResult("failed", "FlareSolverr 未通过 Turnstile 验证：" + reason + hint)
            solution = solved.get("solution") or {}
            manual_token = str(solution.get("turnstile_token") or "")
            fresh = self._html(solved)
            if fresh and not login_page(fresh):
                page = fresh
            if auto_submit:
                # 先认站点自己的提交：求解返回页 → 重开签到页；都不见结果才兜底。
                notes.append("验证=回调提交")
                judged = classify(page, rule)
                if judged.status in {"success", "already"}:
                    return judged
                judged = self._reopen(session, url, rule)
                if judged.status in {"success", "already"}:
                    return judged
                if not manual_token:
                    return SignResult("failed", "Turnstile 回调未完成签到，且未取得验证结果，"
                                                  "未提交签到" + self._note(notes, started))
            elif not manual_token:
                # 严格提交条件：没有验证结果就绝不发签到请求。
                return SignResult("failed", "FlareSolverr 未取得 Turnstile 验证结果，未提交签到" +
                                  self._note(notes, started))
            else:
                notes.append("验证=已通过")
        else:
            notes.append("验证=未开启")

        # 3) 提交签到表单。回调站点只有「站点提交没跑成」时才带 token 手工兜底
        #    （表单没有提交按钮，字段按含 Turnstile 的表单提取）；普通站点按
        #    提交按钮的表单 POST。token 一次性有效，取到后立即消费。
        if turnstile_widget_in(page):
            if not manual_token:
                return SignResult("failed", "Turnstile 验证结果缺失，未提交签到" + self._note(notes, started))
            if auto_submit:
                form = callback_form(page, base, str(rule.get("path") or ""))
                if not form:
                    return SignResult("failed", "回调未完成签到，也未找到可兜底的签到表单" +
                                      self._note(notes, started))
                action, fields = form
            else:
                form = find_signin_form(page, base, str(rule.get("submit_text") or ""))
                if not form or form["method"] != "POST":
                    return SignResult("failed", "未找到可安全提交的站内签到表单" +
                                      self._note(notes, started))
                action, fields = target_url(base, form["action"]), dict(form["data"])
            # token 最后写入：表单里的同名隐藏字段初值是空的，不能反过来盖掉它
            data = dict(fields)
            data["cf-turnstile-response"] = manual_token
        else:
            form = find_signin_form(page, base, str(rule.get("submit_text") or ""))
            if not form or form["method"] != "POST":
                return SignResult("failed", "未找到可安全提交的站内签到表单" + self._note(notes, started))
            action, data = target_url(base, form["action"]), dict(form["data"])
        posted = self._call({"cmd": "request.post", "url": action,
                             "session": session, "postData": urllib.parse.urlencode(data),
                             "maxTimeout": self._budget(60) * 1000}, self._budget(60))
        if str(posted.get("status")) != "ok":
            return SignResult("failed", "签到请求发送失败：" + self._reason(posted) + self._note(notes, started))
        after = self._html(posted)
        if not after:
            return SignResult("failed", "签到提交后未收到页面，签到结果未确认" + self._note(notes, started))
        if cloudflare_pending(after):
            return SignResult("failed", "签到提交后仍停在 Cloudflare 检测页" + self._note(notes, started))
        judged = classify(after, rule)
        if judged.status == "failed":
            return SignResult("failed", append_hint(judged.message, self._suffix(notes, started)))
        return judged

    @staticmethod
    def _reason(response: Any) -> str:
        message = str(response.get("message") or "").strip() if isinstance(response, Mapping) else ""
        # 诊断里只保留服务端的一句话原因，不带 cookie / token / 页面内容。
        if message:
            return message[:160]
        status = str(response.get("status") or "") if isinstance(response, Mapping) else ""
        return f"未知错误（status={status or '无响应'}）"

    @staticmethod
    def _suffix(notes: list, started: float) -> str:
        parts = list(notes)
        parts.append(f"耗时={time.monotonic() - started:.1f}秒")
        return "；".join(parts)

    @classmethod
    def _note(cls, notes: list, started: float) -> str:
        return "；" + cls._suffix(notes, started)