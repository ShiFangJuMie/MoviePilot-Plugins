# foxserve：远程签到服务（带 Bearer Token 鉴权）
#
# 架构：NAS/MoviePilot --HTTPS隧道+Token--> foxserve --localhost--> 浏览器内核
#   内核二选一（环境变量 FOX_MODE）：
#     cloak    —— CloakBrowser 的 cloakserve（CDP 9222，源码级补丁的 Chromium）
#     camoufox —— Camoufox（Firefox 系反检测，默认）
#   CDP 端口永远只绑 VPS 本机/容器内网，公网只见 foxserve 一个带 Token 的 HTTP 口。
#
# 部署（VPS，见同目录 docker-compose.yml）：
#   docker compose up -d --build
#   环境变量 FOX_TOKEN 设一个自己的随机串（curl 生成：openssl rand -hex 16）
#
# 调用：
#   curl -H "Authorization: Bearer <FOX_TOKEN>" https://<隧道域名>/sign \
#     -H 'Content-Type: application/json' \
#     -d '{"site": "piggo.me", "cookie": "a=1; b=2"}'
#
# 安全：cloakserve 的 CDP 等于浏览器完全控制权（README 原话），绝不经隧道直出；
#       本服务只暴露 /sign 与 /health，且必须带 Token（FOX_TOKEN 未设置时仅限本机调试）。
import os
import re
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

TOKEN = os.environ.get("FOX_TOKEN", "").strip()
MODE = os.environ.get("FOX_MODE", "camoufox").strip().lower()
CDP_URL = os.environ.get("FOX_CDP_URL", "http://127.0.0.1:9222").strip()

app = FastAPI(title="foxserve", version="2")


@app.middleware("http")
async def require_token(request: Request, call_next):
    if TOKEN and request.url.path not in ("/docs", "/openapi.json"):
        header = request.headers.get("authorization") or ""
        if header != f"Bearer {TOKEN}":
            raise HTTPException(status_code=401, detail="token required")
    return await call_next(request)


SUCCESS_WORDS = ("签到成功", "簽到成功", "成功签到", "打卡成功", "获得")
ALREADY_WORDS = ("今日已签", "今天已签", "已经签到", "已签到", "已簽到", "明日")
BLOCK_WORDS = ("正在被调试", "正在被調試")
SAFELINE_CONFIRM = ["#sl-check", "#slg-box button", "div[id^='slg'] button"]
TIMEOUT_SECONDS = 90


class SignRequest(BaseModel):
    site: str
    cookie: str
    proxy: dict | None = None
    path: str = "/attendance.php"


def parse_cookies(header: str, domain: str) -> list[dict]:
    pairs = []
    for item in str(header or "").split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if name.strip():
            pairs.append({"name": name.strip(), "value": value,
                          "domain": domain, "path": "/"})
    return pairs


def judge(text: str) -> tuple[str, str]:
    if any(word in text for word in BLOCK_WORDS):
        return "failed", "雷池反调试拦截（该内核也被识别，需换内核或进一步排查）"
    if any(word in text for word in SUCCESS_WORDS):
        return "success", "签到成功" + _bonus(text)
    if any(word in text for word in ALREADY_WORDS):
        return "already", "今日已经签到" + _bonus(text)
    return "", ""


def _bonus(text: str) -> str:
    match = re.search(r"获得\s*[\d,]+(?:\.\d+)?\s*(?:个|粒|枚|点)?\s*(?:魔力值|魔力|积分|爆米花)", text)
    return f"（{match.group(0)}）" if match else ""


def open_page(request: "SignRequest") -> Any:
    """按 FOX_MODE 打开浏览器并导航到签到页，返回 (page, closer)。"""
    if MODE == "cloak":
        from playwright.sync_api import sync_playwright
        import urllib.parse
        manager = sync_playwright().start()
        query = ""
        if request.proxy and request.proxy.get("server"):
            query = "?proxy=" + urllib.parse.quote(request.proxy["server"])
        browser = manager.chromium.connect_over_cdp(CDP_URL + query, timeout=30000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        def closer():
            browser.close()
            manager.stop()
        return page, closer

    from camoufox.sync_api import Camoufox
    kwargs: dict[str, Any] = {"headless": False, "humanize": True}
    if request.proxy and request.proxy.get("server"):
        kwargs["proxy"] = {"server": request.proxy["server"],
                           "username": request.proxy.get("username") or None,
                           "password": request.proxy.get("password") or None}
    browser = Camoufox(**kwargs).__enter__()

    def closer():
        browser.__exit__(None, None, None)
    return browser.new_page(), closer


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": app.version, "mode": MODE,
            "token": bool(TOKEN)}


@app.post("/sign")
def sign(request: SignRequest) -> dict:
    url = f"https://{request.site}{request.path}"
    cookies = parse_cookies(request.cookie, f".{request.site}")
    started = time.monotonic()
    try:
        page, closer = open_page(request)
        try:
            page.context.add_cookies(cookies)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # 雷池检测页自己的 JS 会跑若干秒；出现确认按钮就点；签到页自动完成签到
            clicked = 0
            for _ in range(TIMEOUT_SECONDS // 2):
                time.sleep(2)
                text = page.evaluate("document.body ? document.body.innerText : ''") or ""
                status, message = judge(text)
                if status:
                    return {"status": status, "message": message,
                            "elapsed": round(time.monotonic() - started, 1)}
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
            status, message = judge(text)
            title = page.title()
            if not status:
                if "登录" in title:
                    status, message = "failed", "停在登录页：Cookie 可能失效或会话环境不匹配"
                else:
                    status, message = "failed", f"等待 {TIMEOUT_SECONDS} 秒未确认签到；标题「{title[:30]}」"
            return {"status": status, "message": message,
                    "elapsed": round(time.monotonic() - started, 1)}
        finally:
            closer()
    except Exception as error:
        return {"status": "failed",
                "message": f"执行异常：{type(error).__name__}: {str(error)[:160]}",
                "elapsed": round(time.monotonic() - started, 1)}
