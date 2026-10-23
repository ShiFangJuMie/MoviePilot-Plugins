"""Self-hosted Browserless Function transport; no BrowserQL or Cloud solver calls."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from .core import SignResult, classify, classify_ajax_response, cloudflare_pending, login_page, target_url


FUNCTION_CODE = Path(__file__).with_name("function.js").read_text(encoding="utf-8")


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
        if self.browser_proxy:
            proxy = urllib.parse.urlsplit(self.browser_proxy)
            if proxy.scheme not in {"http", "https", "socks5"} or not proxy.hostname:
                raise ValueError("浏览器代理必须是 http://、https:// 或 socks5:// 地址")
            if proxy.scheme == "socks5" and (proxy.username or proxy.password):
                raise ValueError("Chromium 不支持带用户名密码的 SOCKS5 代理，请使用 HTTP 代理")
            # Proxy credentials belong in page.authenticate, never in the launch URL.
            server = urllib.parse.urlunsplit((proxy.scheme, proxy.netloc.rsplit("@", 1)[-1], "", "", ""))
            launch = json.loads(query.get("launch") or "{}")
            if not isinstance(launch, dict) or not isinstance(launch.get("args", []), list):
                raise ValueError("Browserless launch 配置必须包含有效的 args 数组")
            launch["args"] = [arg for arg in launch.get("args", [])
                              if not str(arg).startswith("--proxy-server=")] + ["--proxy-server=" + server]
            query["launch"] = json.dumps(launch)
            if proxy.username:
                context["proxy_auth"] = {"username": urllib.parse.unquote(proxy.username),
                                         "password": urllib.parse.unquote(proxy.password or "")}
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
        try:
            result = self._request(context)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                return SignResult("failed", "自建 Browserless 鉴权失败，请检查 Token")
            if error.code == 404:
                return SignResult("failed", "自建 Browserless Function 接口返回 404，请检查根地址或 /chromium/function")
            return SignResult("failed", f"自建 Browserless HTTP {error.code}")
        except (TimeoutError, urllib.error.URLError, OSError):
            # Do not retry an uncertain submission here: the site may already have accepted it.
            return SignResult("failed", "自建浏览器连接中断或超时，签到结果未确认", transient=True)
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
            "navigation": "自建浏览器无法打开签到页",
            "timeout": "自建浏览器等待超时，签到结果未确认",
            "execution": "自建浏览器执行失败，签到结果未确认",
        }
        if result.get("error"):
            return SignResult("failed", errors.get(str(result["error"]), errors["execution"]))
        html = str(result.get("after") or "")
        if cloudflare_pending(html) or result.get("challenge_pending"):
            return SignResult("failed", errors["challenge"])
        if login_page(html):
            return SignResult("failed", "签到页跳转到了登录页，Cookie 可能已失效")
        if not html:
            return SignResult("failed", "自建浏览器返回空页面，未确认签到")
        judged = classify(html, effective)
        if judged.status in {"success", "already"}:
            return judged
        # Only a response from the configured sign-in endpoint may provide an AJAX receipt.
        if not effective.get("verify_success_path"):
            ajax = classify_ajax_response(result.get("ajax"), effective)
            if ajax is not None:
                return ajax
        return SignResult("failed", "浏览器已完成操作，但页面未确认签到成功")
