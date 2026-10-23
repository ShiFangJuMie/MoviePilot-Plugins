"""Self-hosted Browserless Function transport; no BrowserQL or Cloud solver calls."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from .core import SignResult, classify, classify_ajax_response, cloudflare_pending, login_page, target_url


FUNCTION_CODE = Path(__file__).with_name("function.js").read_text(encoding="utf-8")


def diagnostic_hint(value: Any) -> str:
    """Only format known counters and enums; never include page text, URLs or tokens."""
    if not isinstance(value, Mapping):
        return ""
    stages = {"preflight": "预检", "navigation": "打开页面", "challenge_before": "验证前",
              "button": "等待按钮", "submit": "提交", "auto_submit": "自动提交",
              "challenge_after": "提交后", "verify": "回读核验", "altcha": "Altcha",
              "ocr_image": "验证码截图", "ocr_http": "OCR"}
    parts = []
    if value.get("stage") in stages:
        parts.append("阶段=" + stages[value["stage"]])
    elapsed = value.get("elapsed_ms")
    if isinstance(elapsed, (int, float)) and 0 <= elapsed <= 600000:
        parts.append(f"耗时={elapsed / 1000:.1f}秒")
    if value.get("kind") in {"cloudflare", "turnstile", "waf"}:
        parts.append("验证=" + value["kind"])
        if isinstance(value.get("token"), bool):
            parts.append("验证结果=" + ("已取得" if value["token"] else "未取得"))
    for key, label in (("frames", "验证框"), ("checkbox_clicks", "点击验证框"),
                       ("requests", "提交请求"), ("http_status", "页面HTTP")):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 999:
            parts.append(f"{label}={number}")
    if value.get("auto_action") in {"callback", "form"}:
        parts.append("提交方式=" + value["auto_action"])
    if value.get("error_type") in {"Error", "TypeError", "ReferenceError", "SyntaxError",
                                   "TimeoutError", "ProtocolError", "TargetCloseError", "AbortError"}:
        parts.append("异常=" + value["error_type"])
    return "；".join(parts)


def service_url(address: str, suffix: str) -> str:
    """Accept a service root or its complete endpoint, retaining an explicit path prefix."""
    parsed = urllib.parse.urlsplit(str(address or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("服务地址必须是完整的 http:// 或 https:// 地址")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("服务地址不能包含用户凭据或 URL 片段")
    path = parsed.path.rstrip("/")
    if path.endswith("/bql") or path.endswith("/stealth"):
        raise ValueError("本版本使用自建 Function API，请填写根地址或 /chromium/function")
    if not path.endswith(suffix) and not (suffix == "/chromium/function" and path.endswith("/function")):
        path += suffix
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


class FunctionSigner:
    """Execute a complete sign-in in one private browser session."""

    def __init__(self, address: str, token: str, timeout: int = 120,
                 ocr_url: str = "", browser_proxy: str = ""):
        self.endpoint = service_url(address, "/chromium/function")
        host = urllib.parse.urlsplit(self.endpoint).hostname or ""
        if host == "browserless.io" or host.endswith(".browserless.io"):
            raise ValueError("本版本使用自建浏览器，请将 Cloud 地址改为自己的 Browserless 地址")
        self.token = str(token or "").strip()
        self.timeout = max(15, min(int(timeout), 300))
        self.ocr_url = service_url(ocr_url, "/captcha/base64") if ocr_url else ""
        self.browser_proxy = str(browser_proxy or "").strip()

    def _request(self, context: dict) -> Any:
        parsed = urllib.parse.urlsplit(self.endpoint)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        # Use the same Bearer authentication as the self-hosted Function endpoint.
        query.pop("token", None)
        query["timeout"] = str(self.timeout * 1000)
        launch = json.loads(query.get("launch") or "{}")
        if not isinstance(launch, dict) or not isinstance(launch.get("args", []), list):
            raise ValueError("Browserless launch 配置必须包含有效的 args 数组")
        if context["rule"].get("mode") == "cloudflare":
            # OSS Chromium supports this launch option; it is not the paid /stealth/bql route.
            launch.setdefault("stealth", True)
        if self.browser_proxy:
            proxy = urllib.parse.urlsplit(self.browser_proxy)
            if proxy.scheme not in {"http", "https", "socks5"} or not proxy.hostname:
                raise ValueError("浏览器代理必须是 http://、https:// 或 socks5:// 地址")
            if proxy.scheme == "socks5" and (proxy.username or proxy.password):
                raise ValueError("Chromium 不支持带用户名密码的 SOCKS5 代理，请使用 HTTP 代理")
            # Proxy credentials belong in page.authenticate, never in the launch URL.
            server = urllib.parse.urlunsplit((proxy.scheme, proxy.netloc.rsplit("@", 1)[-1], "", "", ""))
            launch["args"] = [arg for arg in launch.get("args", [])
                              if not str(arg).startswith("--proxy-server=")] + ["--proxy-server=" + server]
            if proxy.username:
                context["proxy_auth"] = {"username": urllib.parse.unquote(proxy.username),
                                         "password": urllib.parse.unquote(proxy.password or "")}
        if launch:
            query["launch"] = json.dumps(launch)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                      urllib.parse.urlencode(query), ""))
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            url, data=json.dumps({"code": FUNCTION_CODE, "context": context}).encode(),
            headers=headers, method="POST")
        # The browser's egress proxy is separate from the connection to our own service.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=self.timeout + 15) as response:
            return json.loads(response.read().decode("utf-8"))

    def sign(self, site: Mapping[str, Any], rule: Mapping[str, Any]) -> SignResult:
        base = str(site.get("url") or "").strip()
        cookie = str(site.get("cookie") or "").strip()
        if not base or not cookie:
            return SignResult("failed", "站点地址或 Cookie 为空")
        effective = dict(rule, mode=rule.get("mode") or "open_page")
        effective.setdefault("submit_selector", "input[type='submit'], button, a")
        if effective["mode"] in {"image", "trigger_image"} and not self.ocr_url:
            return SignResult("failed", "图片验证码需要配置 MoviePilot-OCR 地址")
        context = {"url": target_url(base, str(effective.get("path") or "/attendance.php"),
                                     effective.get("route_fragment")),
                   "base_url": base, "cookie": cookie, "ua": str(site.get("ua") or ""),
                   "rule": effective, "ocr_url": self.ocr_url,
                   "timeout_ms": self.timeout * 1000}
        started = time.monotonic()
        try:
            result = self._request(context)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                return SignResult("failed", "自建 Browserless 鉴权失败，请检查 Token")
            if error.code == 404:
                return SignResult("failed", "自建 Browserless Function 接口返回 404，请检查根地址或 /chromium/function")
            return SignResult("failed", f"自建 Browserless HTTP {error.code}")
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            # Do not retry an uncertain submission here: the site may already have accepted it.
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            kind = type(cause).__name__ if isinstance(cause, BaseException) else "URLError"
            return SignResult("failed", f"自建浏览器连接中断或超时，签到结果未确认"
                              f"（异常={kind}；耗时={time.monotonic() - started:.1f}秒）", transient=True)
        except (ValueError, TypeError):
            return SignResult("failed", "自建浏览器配置无效或 Function 返回了无效 JSON")
        if not isinstance(result, dict):
            return SignResult("failed", "Function 返回格式错误，未取得签到结果")
        errors = {
            "challenge": "自建浏览器未通过 Cloudflare / Turnstile / WAF 验证，未确认签到",
            "slider": "页面仍要求滑块验证；MoviePilot-OCR 不识别滑块，需要站点专用规则",
            "ocr_http": "MoviePilot-OCR 请求失败，请检查浏览器到 OCR 服务的连通性",
            "ocr_empty": "MoviePilot-OCR 未识别出六位字母数字，未提交签到",
            "ocr_image": "未找到可截图的验证码图片，请检查站点选择器",
            "altcha": "Altcha 验证未完成，未提交签到",
            "button": "自建浏览器未找到可用的签到按钮，请检查选择器和页面登录状态",
            "submit": "未找到可安全提交的站内签到表单，未发送签到请求",
            "login": "签到页跳转到了登录页，Cookie 可能已失效",
            "navigation": "自建浏览器无法打开签到页",
            "timeout": "自建浏览器等待超时，签到结果未确认",
            "execution": "自建浏览器执行失败，签到结果未确认",
        }
        hint = diagnostic_hint(result.get("diagnostics"))
        suffix = "；" + hint if hint else ""
        if result.get("error"):
            return SignResult("failed", errors.get(str(result["error"]), errors["execution"]) + suffix)
        html = str(result.get("after") or "")
        if cloudflare_pending(html) or result.get("challenge_pending"):
            return SignResult("failed", errors["challenge"] + suffix)
        if login_page(html):
            return SignResult("failed", "签到页跳转到了登录页，Cookie 可能已失效")
        if not html:
            return SignResult("failed", "自建浏览器返回空页面，未确认签到")
        # Only a response from the configured sign-in endpoint may provide an AJAX receipt.
        ajax = None
        if not effective.get("verify_success_path"):
            ajax = classify_ajax_response(result.get("ajax"), effective)
            if ajax is not None and (ajax.status in {"success", "already"}
                                     or ajax.message.startswith("签到请求失败（HTTP")):
                return ajax
        judged = classify(html, effective)
        if judged.status in {"success", "already"}:
            return judged
        if ajax is not None:
            return SignResult("failed", ajax.message + suffix)
        return SignResult("failed", "浏览器已完成操作，但页面未确认签到成功" + suffix)
