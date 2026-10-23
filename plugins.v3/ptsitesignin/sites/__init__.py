"""站点模块基类与运行上下文。

规则表达不了的站点（图片验证码、答题选图、API Token）放在这个目录里，一个站一个文件。
模块只需要定义一个带 ``site_url`` 和 ``signin(site, ctx)`` 的类，插件会自动发现并优先使用它。

示例：

    class Hdsky:
        site_url = "hdsky.me"

        def signin(self, site, ctx):
            page = ctx.page("https://hdsky.me/")
            if "已签到" in page:
                return True, "今日已签到"
            return False, "签到失败"
"""
from __future__ import annotations

import base64
import re
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class SignContext:
    """给站点模块用的小工具：带 Cookie 的 GET / POST、取图片、OCR。"""

    def __init__(self, site: Mapping[str, Any], timeout: int = 30,
                 proxies: Optional[Mapping[str, str]] = None, logger: Any = None,
                 ocr_url: str = "") -> None:
        self.site = site
        self.cookie = str(site.get("cookie") or "").strip()
        self.ua = str(site.get("ua") or "").strip() or DEFAULT_UA
        self.timeout = timeout
        self.proxies = dict(proxies) if proxies else None
        self.logger = logger
        self.ocr_url = str(ocr_url or "").strip()
        self._http_session = None

    # ---------------- 基础请求 ----------------
    def _session(self):
        import requests
        if self._http_session is not None:
            return self._http_session
        session = requests.Session()
        session.headers.update({"User-Agent": self.ua,
                                "Accept": "text/html,application/json,*/*;q=0.8"})
        host = urlparse(str(self.site.get("url") or "")).hostname or ""
        # A fixed Cookie header suppresses new Set-Cookie values. Keep a scoped cookie jar
        # so the farm's verification token survives and PT cookies never go to Douban/OCR.
        for item in self.cookie.split(";"):
            if "=" in item and host:
                name, value = item.strip().split("=", 1)
                if name:
                    session.cookies.set(name, value, domain=host, path="/")
        self._http_session = session
        return session

    def close(self) -> None:
        if self._http_session is not None:
            self._http_session.close()

    def get(self, url: str, **kwargs):
        try:
            return self._session().get(url, timeout=self.timeout, allow_redirects=True,
                                       proxies=self.proxies, **kwargs)
        except Exception:
            return None

    def post(self, url: str, data=None, json_body=None, headers=None, **kwargs):
        try:
            return self._session().post(url, data=data, json=json_body, headers=headers,
                                        timeout=self.timeout, allow_redirects=True,
                                        proxies=self.proxies, **kwargs)
        except Exception:
            return None

    def page(self, url: str, **kwargs) -> str:
        response = self.get(url, **kwargs)
        return (response.text if response is not None and response.status_code == 200 else "")

    def image(self, url: str, referer: str = "") -> bytes:
        headers = {"Referer": referer} if referer else None
        response = self.get(url, headers=headers)
        if response is None or response.status_code != 200:
            return b""
        return response.content or b""

    # ---------------- OCR（只使用明确配置的自建服务）----------------
    def ocr(self, image_url: str) -> str:
        if not self.ocr_url:
            return ""
        image = self.image(image_url)
        if not image:
            return ""
        try:
            import requests
            endpoint = self.ocr_url.rstrip("/")
            if not endpoint.endswith("/captcha/base64"):
                endpoint += "/captcha/base64"
            # This separate client carries only the image, never the site's cookies or headers.
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(endpoint, json={"base64_img": base64.b64encode(image).decode()},
                                        timeout=self.timeout)
                response.raise_for_status()
                answer = str(response.json().get("result") or "").strip()
                return answer if re.fullmatch(r"[A-Z0-9]{6}", answer) else ""
        except Exception:
            return ""


def domain_matches(url: str, site_url: str) -> bool:
    """站点 URL 是否属于模块声明的域名（含子域名）。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    site_url = str(site_url or "").lower()
    if not host or not site_url:
        return False
    return host == site_url or host.endswith("." + site_url)


def sign_via_module(module: Any, site: Mapping[str, Any], ctx: SignContext) -> Tuple[bool, str]:
    """调用模块的 signin，异常统一转成失败文案。"""
    try:
        state, message = module().signin(site, ctx)
        return bool(state), str(message or "")
    except Exception as error:  # 模块 bug 不该把整站点搞崩
        return False, f"站点模块出错：{error}"
