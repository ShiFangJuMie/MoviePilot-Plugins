"""CloakBrowser 引擎（cloak.py）单测：协议、鉴权、判定与失败路径。

不连真实服务：playwright 与发现请求全部用替身，验证调用契约。
"""
import sys
import types
import unittest.mock as mock
from types import SimpleNamespace

import pytest

from ptsitesignin import cloak as cloak_mod
from ptsitesignin import core

SITE = {"url": "https://piggo.me", "cookie": "pigo_np_web_session=abc; cf_clearance=xyz"}
RULE = core.resolve_rule({"url": "https://piggo.me"}, {})


def _install_fake_playwright(monkeypatch, page):
    """注册 playwright.sync_api.sync_playwright 替身，返回 browser 生命周期记录。"""

    class FakeBrowser:
        def __init__(self):
            self.closed = False
            self.contexts = [SimpleNamespace(pages=[page], add_cookies=mock.Mock(),
                                             new_cdp_session=mock.Mock(return_value=mock.Mock()))]

        def close(self):
            self.closed = True

    browser = FakeBrowser()
    manager = SimpleNamespace(chromium=SimpleNamespace(
        connect_over_cdp=mock.Mock(return_value=browser)))

    class FakeSync:
        def __enter__(self):
            return manager

        def __exit__(self, *args):
            return False

    package = types.ModuleType("playwright")
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = FakeSync
    package.sync_api = api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    return browser


class ScriptedPage:
    """按剧本吐 content() 的页面替身。"""

    def __init__(self, htmls):
        self._htmls = htmls
        self.goto = mock.Mock()
        self.context = SimpleNamespace(add_cookies=mock.Mock())
        self.evaluations = 0

    def wait_for_timeout(self, ms):
        return None

    def content(self):
        index = min(self.evaluations, len(self._htmls) - 1)
        html = self._htmls[index]
        if html == "__advance__":
            self.evaluations += 1
            html = self._htmls[min(self.evaluations, len(self._htmls) - 1)]
        return html

    def evaluate(self, script, *args):
        self.evaluations += 1
        return True


def test_piggo_preset_uses_cloak_engine():
    rule = core.resolve_rule({"url": "https://piggo.me"}, {})
    assert rule["engine"] == "cloak"
    assert rule["path"] == "/attendance.php"
    assert rule["success_keywords"]


def test_sunny_preset_uses_cloak_engine():
    # Sunny 2026-10-28 迁移：cloak 引擎实测真签成功，自建 Browserless 不再有预设用户
    rule = core.resolve_rule({"url": "https://sunnypt.top"}, {})
    assert rule["engine"] == "cloak"
    assert rule["path"] == "/user/attendance"
    assert rule["submit_method"] == "dom_click"
    assert rule["submit_text"] == "立即签到"


def test_split_basic_auth_prefers_url_credentials():
    address, basic = cloak_mod.split_basic_auth("https://cdp:secret@12730.example.com/")
    assert address == "https://12730.example.com"
    assert basic == "Y2RwOnNlY3JldA=="
    address, basic = cloak_mod.split_basic_auth("https://12730.example.com", "cdp:secret")
    assert address == "https://12730.example.com"
    assert basic == "Y2RwOnNlY3JldA=="
    address, basic = cloak_mod.split_basic_auth("https://12730.example.com")
    assert basic == ""


def test_discover_401_raises_immediately_without_retry(monkeypatch):
    calls = []
    error = cloak_mod.urllib.error.HTTPError("url", 401, "Unauthorized", None, None)

    class Resp:
        def __enter__(self):
            raise error

        def __exit__(self, *args):
            return False

    opener = mock.Mock()
    opener.open = lambda request, timeout=None: (calls.append(request.full_url), Resp())[1]
    monkeypatch.setattr(cloak_mod.urllib.request, "build_opener", lambda *a, **k: opener)
    with pytest.raises(PermissionError):
        cloak_mod.discover_ws_url("https://12730.example.com", "seed", "", "abc")
    assert len(calls) == 1  # fail2ban：401 绝不重试


def test_discover_client_error_does_not_retry(monkeypatch):
    # cloakserve 对非法 seed（如带点的域名）返回 400，重试没有意义
    calls = []
    error = cloak_mod.urllib.error.HTTPError("url", 400, "Bad Request", None,
                                             __import__("io").BytesIO(b'{"error": "Invalid fingerprint seed"}'))

    class Resp:
        def __enter__(self):
            raise error

        def __exit__(self, *args):
            return False

    opener = mock.Mock()
    opener.open = lambda request, timeout=None: (calls.append(1), Resp())[1]
    monkeypatch.setattr(cloak_mod.urllib.request, "build_opener", lambda *a, **k: opener)
    with pytest.raises(ConnectionError) as info:
        cloak_mod.discover_ws_url("https://12730.example.com", "seed", "", "abc")
    assert len(calls) == 1
    assert "Invalid fingerprint seed" in str(info.value)


def test_cloak_sign_success_flow(monkeypatch):
    # 剧本：雷池挑战页（触发一次 #sl-check 点击）→ 签到成功页
    page = ScriptedPage(["<body>正在进行安全检测</body>",
                         "<body>签到成功，获得 20 个魔力值</body>"])
    browser = _install_fake_playwright(monkeypatch, page)
    monkeypatch.setattr(cloak_mod, "discover_ws_url",
                        lambda *a, **k: "ws://fake/devtools/browser/0")
    closes = []
    monkeypatch.setattr(cloak_mod, "close_instance",
                        lambda endpoint, seed, basic: closes.append(seed))
    result = cloak_mod.cloak_sign(SITE, RULE, timeout=60,
                                  endpoint="https://12730.example.com", proxy="")
    assert result.status == "success"
    assert browser.closed                       # 实例显式关闭
    # 稳定 seed：每站"同一个浏览器"；域名里的点会被 cloakserve 拒绝（400），须转连字符
    assert closes == ["ptsignin-piggo-me"]
    assert browser.contexts[0].add_cookies.called  # Cookie 注入进了浏览器存储


def test_cloak_sign_already_counts_as_done(monkeypatch):
    page = ScriptedPage(["<body>今日已签到，明日再来</body>"])
    _install_fake_playwright(monkeypatch, page)
    monkeypatch.setattr(cloak_mod, "discover_ws_url",
                        lambda *a, **k: "ws://fake/devtools/browser/0")
    monkeypatch.setattr(cloak_mod, "close_instance", lambda *a, **k: None)
    result = cloak_mod.cloak_sign(SITE, RULE, timeout=60,
                                  endpoint="https://12730.example.com")
    assert result.status == "already"


def test_cloak_sign_login_page_reports_stale_cookie(monkeypatch):
    page = ScriptedPage(["<title>猪猪 :: 登录</title><body>未登录! 请先登录 login.php</body>"])
    _install_fake_playwright(monkeypatch, page)
    monkeypatch.setattr(cloak_mod, "discover_ws_url",
                        lambda *a, **k: "ws://fake/devtools/browser/0")
    monkeypatch.setattr(cloak_mod, "close_instance", lambda *a, **k: None)
    result = cloak_mod.cloak_sign(SITE, RULE, timeout=60,
                                  endpoint="https://12730.example.com")
    assert result.status == "failed"
    assert "新鲜" in result.message or "Cookie" in result.message


def test_cloak_sign_missing_endpoint():
    result = cloak_mod.cloak_sign(SITE, RULE, endpoint="")
    assert result.status == "failed"
    assert "未配置" in result.message


def test_cloak_sign_clicks_submit_button(monkeypatch):
    # Sunny 型站点：页面出现「立即签到」按钮 → 两段式点击 → 站点回调后变已签状态
    unsigned = "<body><button>立即签到</button>今日 未签到</body>"
    signed = "<body>今日已签到</body>"
    page = ScriptedPage([unsigned])
    browser = _install_fake_playwright(monkeypatch, page)
    monkeypatch.setattr(cloak_mod, "discover_ws_url",
                        lambda *a, **k: "ws://fake/devtools/browser/0")
    monkeypatch.setattr(cloak_mod, "close_instance", lambda *a, **k: None)
    rule = dict(RULE, submit_method="dom_click", submit_selector="button",
                submit_text="立即签到")
    clicks = []
    state = {"clicked": False}

    def fake_click(selector, timeout=None):
        clicks.append(selector)
        state["clicked"] = True
        return True

    page.click = fake_click
    page.evaluate = lambda script, *a: ("data-ptsignin-target" in str(script)
                                        and "setAttribute" in str(script))
    page.content = lambda: signed if state["clicked"] else unsigned
    result = cloak_mod.cloak_sign({"url": "https://piggo.me", "cookie": "a=b"}, rule,
                                  timeout=60, endpoint="https://12730.example.com")
    assert result.status == "already"
    assert clicks, "必须发生真实点击（两段式第二步）"
    assert all("data-ptsignin-target" in selector for selector in clicks)
