"""FlareSolverr 原生会话签到的契约测试：调用序列、严格提交条件与失败映射。"""
import json
import sys
import urllib.error
from unittest.mock import Mock, patch

from ptsitesignin import core
from ptsitesignin.flaresolverr import (FlareSolverrSigner, callback_form, service_url,
                                       turnstile_auto_submits)

SITE = {"url": "https://mua.example/", "cookie": "c_secure_uid=fixture; extra=1", "ua": ""}

# 仿 NexusPHP attendance.php 的真实结构：顶部导航若干链接 + 表单内
# class="cf-turnstile" 容器 + 无 name 的提交按钮（服务端只认 cf-turnstile-response）。
NAV = "".join(f'<a href="/page{i}.php">链接{i}</a>' for i in range(4))
WIDGET_PAGE = (
    "<html><head><script src='https://challenges.cloudflare.com/turnstile/v0/api.js' async defer></script></head>"
    f"<body>{NAV}<form method='post' action='attendance.php' style='display: inline-block;'>"
    "<table><tr><td class='rowhead'>Security Check</td>"
    "<td align='left'><div class='cf-turnstile' data-sitekey='0xfixture' data-theme='light'></div></td></tr>"
    "<tr><td class='toolbox' colspan='2' align='center'><input type='submit' value='签到' class='btn' /></td></tr>"
    "</table></form><a href='/logout.php'>退出</a></body></html>"
)
# 观众 / OurBits 形态：表单没有提交按钮，Turnstile 带 data-callback，验证完成由站点自动提交
CALLBACK_PAGE = (
    "<html><head><script src='https://challenges.cloudflare.com/turnstile/v0/api.js' async defer></script></head>"
    f"<body>{NAV}<form method='POST' id='attendance'>"
    "<input type='hidden' name='userId' id='myUserId' value='60532'>"
    "<div class='cf-turnstile' data-sitekey='0xfixture' data-callback='TurnstileCallback'>"
    "<div><div></div><input type='hidden' name='cf-turnstile-response' id='cf-chl-widget-x_response'></div></div>"
    "</form></body></html>"
)
SIGNED_PAGE = "<html><body>签到成功，连续签到 3 天</body></html>"
ALREADY_PAGE = "<html><body>对不起，今天已经签到过了</body></html>"
LOGIN_PAGE = "<html><body><form action='takelogin.php'><input name='username'><input name='password'></form></body></html>"


def _ok(html, **extra):
    return {"status": "ok", "message": "Challenge not detected!",
            "solution": {"url": "https://mua.example/attendance.php", "status": 200,
                         "response": html, "cookies": [], **extra}}


def _error(message):
    return {"status": "error", "message": "Error: " + message, "startTimestamp": 0, "endTimestamp": 90000}


class _Recorder:
    """按脚本回放 FlareSolverr 响应并记录请求序列。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, payload, budget):
        self.requests.append((payload, budget))
        if not self.responses:
            raise AssertionError("no scripted response left; requests so far: "
                                 + json.dumps([r[0]["cmd"] for r in self.requests]))
        return self.responses.pop(0)


def _signer():
    return FlareSolverrSigner("https://flare.example/", 120)


def _rule(**extra):
    custom = {"engine": "flaresolverr", "path": "/attendance.php",
              "flare_prime_path": "/login.php", "submit_text": "签到"}
    custom.update(extra)
    return core.resolve_rule({"url": "https://mua.example/"}, {"mua.example": custom})


def test_service_url_accepts_root_and_v1():
    assert service_url("https://flare.example") == "https://flare.example/v1"
    assert service_url("https://flare.example/v1") == "https://flare.example/v1"
    assert service_url("https://flare.example/prefix/") == "https://flare.example/prefix/v1"
    for bad in ["flare.example", "https://user:pass@flare.example", "https://flare.example/v1?x=1"]:
        try:
            service_url(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid address accepted: {bad}")


def test_mua_preset_uses_flaresolverr_engine():
    rule = core.resolve_rule({"url": "https://mua.xloli.cc/"}, {})
    assert rule["engine"] == "flaresolverr"
    assert rule["path"] == "/attendance.php"
    assert rule["flare_prime_path"] == "/login.php"
    # 观众 / OurBits：整页挑战 + 表单 Turnstile，同一套 FlareSolverr 会话流程
    audiences = core.resolve_rule({"url": "https://audiences.me/"}, {})
    assert audiences["engine"] == "flaresolverr"
    assert audiences["path"] == "/attendance.php"
    # 观众的组件只认 TRAWL（Camoufox）指纹：预设专用 flare_url，其余站走全局配置
    assert audiences["flare_url"] == "https://https://<REDACTED-INSTANCE>/"
    ourbits = core.resolve_rule({"url": "https://ourbits.club/"}, {})
    assert ourbits["engine"] == "flaresolverr"
    assert ourbits["path"] == "/attendance.php"
    assert "flare_url" not in ourbits
    try:
        core.resolve_rule({"url": "https://mua.example/"},
                          {"mua.example": {"engine": "flaresolverr", "flare_url": "ftp://x"}})
    except ValueError as error:
        assert "flare_url" in str(error)
    else:
        raise AssertionError("flare_url must be an http(s) address")
    try:
        core.resolve_rule({"url": "https://mua.example/"},
                          {"mua.example": {"engine": "flaresolverr", "flare_tabs": 0}})
    except ValueError as error:
        assert "flare_tabs" in str(error)
    else:
        raise AssertionError("flare_tabs must be a positive integer")
    try:
        core.resolve_rule({"url": "https://mua.example/"},
                          {"mua.example": {"engine": "flaresolverr", "flare_prime_path": "login.php"}})
    except ValueError as error:
        assert "flare_prime_path" in str(error)
    else:
        raise AssertionError("flare_prime_path must be an in-site path")


def test_tabs_till_turnstile_counts_focus_order():
    # 4 个导航链接在 widget 前：第 5 次 Tab 落到组件 iframe 上
    assert core.tabs_till_turnstile(WIDGET_PAGE) == 5
    # 正 tabindex 无论在 DOM 哪里都排到焦点序最前
    positive = WIDGET_PAGE.replace("<a href='/logout.php'>退出</a>",
                                   "<a href='/logout.php' tabindex='2'>退出</a>")
    assert core.tabs_till_turnstile(positive) == 6
    # 禁用控件与隐藏元素不参与计数
    disabled = WIDGET_PAGE.replace(NAV, "<button disabled>x</button>" + NAV)
    assert core.tabs_till_turnstile(disabled) == 5
    # 内联 display:none 的容器整体不可聚焦（观众顶栏浮动按钮就是这种）
    inline_hidden = WIDGET_PAGE.replace(
        NAV, f"<nav style='display:none'>{NAV}</nav><a href='/x'>可见</a>")
    assert core.tabs_till_turnstile(inline_hidden) == 2
    visibility = WIDGET_PAGE.replace(
        '<a href="/page0.php">链接0</a>',
        "<span style='visibility:hidden'><a href='/page0.php'>链接0</a></span>")
    assert core.tabs_till_turnstile(visibility) == 4
    # 没有 Turnstile 组件（或解析不了）时返回 0，由调用方要求手工指定
    assert core.tabs_till_turnstile("<html><body><a href='/x'>x</a></body></html>") == 0
    assert core.turnstile_widget_in(WIDGET_PAGE) is True
    assert core.turnstile_widget_in("<html><body>普通页面</body></html>") is False


def test_full_session_flow_solves_and_posts_once():
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),   # 预热：login.php 上写入 Cookie
        _ok(WIDGET_PAGE),                           # 打开签到页
        _ok(WIDGET_PAGE, turnstile_token="fixture-token"),
        _ok(SIGNED_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    signer = _signer()
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = signer.sign(SITE, _rule())
    assert result.status == "success"
    commands = [payload["cmd"] for payload, _ in recorder.requests]
    assert commands == ["sessions.create", "request.get", "request.get", "request.get",
                        "request.post", "sessions.destroy"]
    prime, opened, solve, post = (recorder.requests[i][0] for i in (1, 2, 3, 4))
    # Cookie 写在预热页面上，签到页与求解请求复用会话浏览器里的登录态
    assert prime["cookies"] == [{"name": "c_secure_uid", "value": "fixture"},
                                {"name": "extra", "value": "1"}]
    assert prime["url"] == "https://mua.example/login.php"
    assert not opened.get("cookies") and not solve.get("cookies")
    assert prime["session"] == opened["session"] == solve["session"] == post["session"]
    assert solve["tabs_till_verify"] == 5
    assert solve["maxTimeout"] <= 90000
    # 只允许带有效 token 提交；token 值本身不落进结果消息
    fields = dict(pair.split("=", 1) for pair in post["postData"].split("&"))
    assert fields["cf-turnstile-response"] == "fixture-token"
    assert "fixture-token" not in result.message
    assert post["url"] == "https://mua.example/attendance.php"


def test_session_proxy_is_passed_to_sessions_create_with_credentials():
    signer = FlareSolverrSigner("https://flare.example/", 120,
                                proxy="http://user:pa%40ss@proxy.example:7890")
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(ALREADY_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        signer.sign(SITE, _rule())
    create = recorder.requests[0][0]
    assert create["proxy"] == {"url": "http://proxy.example:7890",
                               "username": "user", "password": "pa@ss"}
    for bad in ["ftp://proxy.example", "proxy.example:7890"]:
        try:
            FlareSolverrSigner("https://flare.example/", 120, proxy=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid proxy accepted: {bad}")


def test_flare_tabs_override_beats_static_count():
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(WIDGET_PAGE),
        _ok(WIDGET_PAGE, turnstile_token="fixture-token"),
        _ok(SIGNED_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule(flare_tabs=9))
    assert result.status == "success"
    assert recorder.requests[3][0]["tabs_till_verify"] == 9


def test_already_signed_page_never_solves_or_posts():
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(ALREADY_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "already"
    assert [payload["cmd"] for payload, _ in recorder.requests] == \
        ["sessions.create", "request.get", "request.get", "sessions.destroy"]


def test_solve_timeout_reports_tabs_hint_without_posting():
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(WIDGET_PAGE),
        _error("Error solving the challenge. Timeout after 90.0 seconds."),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "failed"
    assert "Turnstile" in result.message and "flare_tabs" in result.message
    commands = [payload["cmd"] for payload, _ in recorder.requests]
    assert "request.post" not in commands
    assert "fixture" not in result.message


def test_missing_token_never_submits():
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(WIDGET_PAGE),
        _ok(WIDGET_PAGE),  # 求解返回 ok 但没有 token
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "failed"
    assert "未取得 Turnstile 验证结果" in result.message
    assert "request.post" not in [payload["cmd"] for payload, _ in recorder.requests]


def test_login_redirect_reports_cookie_expiry():
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(LOGIN_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "failed"
    assert "Cookie 可能已失效" in result.message


def test_site_without_widget_posts_plain_form():
    plain = WIDGET_PAGE.replace("<div class='cf-turnstile' data-sitekey='0xfixture' data-theme='light'></div>", "")
    plain = plain.replace("<script src='https://challenges.cloudflare.com/turnstile/v0/api.js' async defer></script>", "")
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(plain),
        _ok(SIGNED_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "success"
    commands = [payload["cmd"] for payload, _ in recorder.requests]
    assert commands == ["sessions.create", "request.get", "request.get", "request.post", "sessions.destroy"]
    post = recorder.requests[3][0]
    assert "cf-turnstile-response" not in post["postData"]


def test_prime_falls_back_when_page_gives_error_origin():
    # 签到页 404 错误页上写 Cookie 会报 invalid cookie domain：应换预热页面
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _error("Error solving the challenge. Message: invalid cookie domain\\n  (Session info: chrome=152)"),
        _ok("<html><body>登录页</body></html>"),
        _ok(WIDGET_PAGE),
        _ok(WIDGET_PAGE, turnstile_token="fixture-token"),
        _ok(SIGNED_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    rule = _rule()
    rule = dict(rule, flare_prime_path="/attendance.php")  # 预设外路径先失败再回退
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, rule)
    assert result.status == "success"
    attempted = [payload["url"] for payload, _ in recorder.requests if payload["cmd"] == "request.get"]
    assert attempted[:2] == ["https://mua.example/attendance.php", "https://mua.example/login.php"]


def test_network_failure_is_transient_and_session_destroyed():
    recorder = _Recorder({"status": "ok", "message": "Session created successfully."})

    def failing(_self, payload, budget):
        recorder.requests.append((payload, budget))
        if payload["cmd"] == "sessions.destroy":
            return {"status": "ok"}
        if payload["cmd"] == "request.get":
            raise urllib.error.URLError("connection reset")
        return recorder.responses.pop(0)

    with patch.object(FlareSolverrSigner, "_call", failing):
        result = _signer().sign(SITE, _rule())
    assert result.status == "failed" and result.transient
    assert recorder.requests[-1][0]["cmd"] == "sessions.destroy"


def test_callback_page_detection_and_lenient_form_fields():
    assert turnstile_auto_submits(CALLBACK_PAGE) is True
    assert turnstile_auto_submits(WIDGET_PAGE) is False
    action, fields = callback_form(CALLBACK_PAGE, "https://mua.example/", "/attendance.php")
    assert action == "https://mua.example/attendance.php"
    assert fields == {"userId": "60532", "cf-turnstile-response": ""}
    assert callback_form(SIGNED_PAGE, "https://mua.example/", "/attendance.php") is None


def test_callback_site_relies_on_site_submission_without_manual_post():
    # 回调站点：求解后站点自己提交；重开签到页见到结果即返回，绝不手动 POST
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(CALLBACK_PAGE),
        _ok(CALLBACK_PAGE, turnstile_token="fixture-token"),
        _ok(SIGNED_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "success"
    commands = [payload["cmd"] for payload, _ in recorder.requests]
    assert commands == ["sessions.create", "request.get", "request.get", "request.get",
                        "request.get", "sessions.destroy"]
    assert "request.post" not in commands
    assert "fixture-token" not in result.message


def test_callback_solve_stale_element_still_verifies_success():
    # 回调在 FlareSolverr 读 token 前提交并跳转：stale 元素错误不代表签到失败
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(CALLBACK_PAGE),
        _error("Error solving the challenge. stale element reference: element is not attached"),
        _ok(SIGNED_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "success"
    assert "request.post" not in [payload["cmd"] for payload, _ in recorder.requests]


def test_callback_fallback_posts_token_when_site_did_not_submit():
    # 站点回调没跑成：带 token 按无按钮表单手工兜底提交（字段含 userId）
    recorder = _Recorder(
        {"status": "ok", "message": "Session created successfully."},
        _ok("<html><body>登录页</body></html>"),
        _ok(CALLBACK_PAGE),
        _ok(CALLBACK_PAGE, turnstile_token="fixture-token"),
        _ok(CALLBACK_PAGE),  # 重开仍见未完成页面
        _ok(SIGNED_PAGE),
        {"status": "ok", "message": "The session has been removed."},
    )
    with patch.object(FlareSolverrSigner, "_call", recorder):
        result = _signer().sign(SITE, _rule())
    assert result.status == "success"
    post = recorder.requests[5][0]
    fields = dict(pair.split("=", 1) for pair in post["postData"].split("&"))
    assert fields["cf-turnstile-response"] == "fixture-token"
    assert fields["userId"] == "60532"
    assert post["url"] == "https://mua.example/attendance.php"
