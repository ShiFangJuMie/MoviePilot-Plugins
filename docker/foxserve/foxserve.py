# foxserve：Camoufox(Playwright) 签到服务
#
# 部署在 VPS 上的轻量 HTTP 服务：用 Playwright 驱动 Camoufox（Firefox 系反检测
# 浏览器，无 CDP 调试痕迹）完成 PT 站签到。与 FlareSolverr/TRAWL/Browserless
# 的差别：既有完整操控力（点击、等待、表单），又是反检测指纹——专为「雷池反
# 调试 + 需要点击」这类两者缺一不可的站点（猪猪）而生，也可承接其他交互站。
#
# 构建与运行（VPS 上）：
#   cd docker/foxserve && docker build -t foxserve .
#   docker run -d --name foxserve --shm-size=1g --restart=always \
#     -p 127.0.0.1:8100:8100 foxserve
#   # 经 cloudflared 隧道暴露后（如 https://https://<REDACTED-INSTANCE>/）供插件调用
#
# 接口：POST /sign  {"site": "piggo.me", "cookie": "a=1; b=2",
#                    "proxy": {"server": "http://host:port", "username": "", "password": ""}}
# 返回：{"status": "success|already|failed", "message": "..."}
#      GET /health → {"status": "ok", "version": "1"}
import re
import time
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="foxserve", version="1")

SUCCESS_WORDS = ("签到成功", "簽到成功", "成功签到", "打卡成功", "获得")
ALREADY_WORDS = ("今日已签", "今天已签", "已经签到", "已签到", "已簽到", "明日")
BLOCK_WORDS = ("正在被调试", "正在被調試")
# 雷池确认按钮：旧版 #sl-check，新版 slg-* 动态渲染（按钮在盒子里）
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
        return "failed", "雷池反调试拦截（Camoufox 也被识别，需进一步排查）"
    if any(word in text for word in SUCCESS_WORDS):
        return "success", "签到成功" + _bonus(text)
    if any(word in text for word in ALREADY_WORDS):
        return "already", "今日已经签到" + _bonus(text)
    return "", ""


def _bonus(text: str) -> str:
    match = re.search(r"获得\s*[\d,]+(?:\.\d+)?\s*(?:个|粒|枚|点)?\s*(?:魔力值|魔力|积分|爆米花)", text)
    return f"（{match.group(0)}）" if match else ""


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.post("/sign")
def sign(request: SignRequest) -> dict:
    from camoufox.sync_api import Camoufox

    url = f"https://{request.site}{request.path}"
    cookies = parse_cookies(request.cookie, f".{request.site}")
    kwargs: dict[str, Any] = {"headless": False, "humanize": True}
    if request.proxy and request.proxy.get("server"):
        kwargs["proxy"] = {"server": request.proxy["server"],
                           "username": request.proxy.get("username") or None,
                           "password": request.proxy.get("password") or None}
    started = time.monotonic()
    try:
        with Camoufox(**kwargs) as browser:
            page = browser.new_page()
            page.context.add_cookies(cookies)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # 雷池检测页自己的 JS 会跑若干秒；出现确认按钮就点（Camoufox 无 CDP
            # 痕迹，这个「人工验证」点击有机会通过）；签到页打开后自动完成签到
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
    except Exception as error:
        return {"status": "failed",
                "message": f"执行异常：{type(error).__name__}: {str(error)[:160]}",
                "elapsed": round(time.monotonic() - started, 1)}
