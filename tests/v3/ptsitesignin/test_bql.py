"""Browserless Cloud（BQL）引擎的契约测试：强制代理、solve 注入流程与严格提交条件。"""
import json
from unittest.mock import patch

from ptsitesignin import core
from ptsitesignin.bql import BQLSigner, bql_endpoint, cookie_objects

SITE = {"url": "https://mua.example/", "cookie": "c_secure_uid=fixture; extra=1", "ua": ""}


def _html(body):
    return f"<html><body>{body}</body></html>"


def _record(*responses):
    """按脚本回放 BQL 返回值并记录请求。"""
    calls = []

    def fake(self, variables, operation, query, **kwargs):
        calls.append({"operation": operation, "variables": dict(variables),
                      "query": query, "kwargs": kwargs})
        if len(calls) > len(responses):
            raise AssertionError("no scripted response; operations: " +
                                 str([c["operation"] for c in calls]))
        return responses[len(calls) - 1]

    return calls, fake


def _rule(**extra):
    custom = {"engine": "bql", "mode": "cloudflare", "path": "/attendance.php"}
    custom.update(extra)
    return core.resolve_rule({"url": "https://mua.example/"}, {"mua.example": custom})


def _signer():
    return BQLSigner("https://production-sfo.browserless.io", "fixture-key", 120,
                     proxy="http://user:pass@home.example:7890")


def test_endpoint_and_proxy_requirements():
    assert bql_endpoint("https://production-sfo.browserless.io", "KEY").endswith(
        "/stealth/bql?token=KEY")
    assert bql_endpoint("http://self.example/chromium/bql").endswith("/chromium/bql")
    cookies = cookie_objects("a=1; b=2", "https://mua.example/attendance.php")
    assert {"name": "a", "value": "1", "domain": "mua.example", "path": "/"} == {
        k: v for k, v in cookies[0].items() if k in {"name", "value", "domain", "path"}}
    for bad in ["", "proxy.example:7890", "ftp://proxy.example"]:
        try:
            BQLSigner("https://x.example/", "t", proxy=bad)
        except ValueError as error:
            assert "代理" in str(error)
        else:
            raise AssertionError(f"proxy required, got: {bad!r}")


def test_solve_flow_injects_token_via_reconnect_session():
    calls, fake = _record(
        {"before": {"html": _html("签到页")}},                          # 预检
        {"solve": {"found": True, "solved": True, "token": "fixture-token"},
         "session": {"browserQLEndpoint": "https://cloud.example/e/abc"}},  # solve
        {"after": {"html": _html("签到成功")}},                          # 注入
    )
    signer = _signer()
    with patch.object(BQLSigner, "_request", fake):
        result = signer.sign(SITE, _rule())
    assert result.status == "success"
    assert [c["operation"] for c in calls] == [
        "CheckInPreflight", "CheckInSolve", "CheckInSubmit"]
    solve, submit = calls[1], calls[2]
    # 每个查询的第一步都是代理：所有请求走家庭出口
    for call in calls:
        assert call["variables"]["proxyServer"] == "http://user:pass@home.example:7890"
        assert 'proxy(url: ["*"], server: $proxyServer)' in call["query"]
    # token 只进入注入脚本，不进入日志可见的查询变量以外的任何位置
    assert "fixture-token" in submit["variables"]["inject"]
    # 第二段打到 reconnect 返回的会话地址（补 token），复用同一浏览器
    assert submit["kwargs"]["endpoint"].startswith("https://cloud.example/e/abc")
    assert "token=fixture-key" in submit["kwargs"]["endpoint"]
    assert "fixture-token" not in result.message


def test_solve_without_token_never_submits():
    calls, fake = _record(
        {"before": {"html": _html("签到页")}},
        {"solve": {"found": False, "solved": False, "token": ""}},
    )
    with patch.object(BQLSigner, "_request", fake):
        result = _signer().sign(SITE, _rule())
    assert result.status == "failed"
    assert "未取得 Turnstile token" in result.message
    assert [c["operation"] for c in calls] == ["CheckInPreflight", "CheckInSolve"]


def test_preflight_already_signed_skips_solve():
    calls, fake = _record({"before": {"html": _html("今日已经签到")}})
    with patch.object(BQLSigner, "_request", fake):
        result = _signer().sign(SITE, _rule())
    assert result.status == "already"
    assert [c["operation"] for c in calls] == ["CheckInPreflight"]


def test_click_flow_includes_waf_and_altcha_steps():
    rule = _rule(engine="bql", mode="open_page", submit_selector="#submit",
                 pending_keywords=["正在进行安全检测"],
                 altcha_checkbox_selector="altcha-widget input[type='checkbox']")
    calls, fake = _record(
        {"before": {"html": _html("签到页")}},
        {"after": {"html": _html("签到成功")}},
    )
    with patch.object(BQLSigner, "_request", fake):
        result = _signer().sign(SITE, rule)
    assert result.status == "success"
    query = calls[1]["query"]
    assert '"#sl-check"' in query          # 雷池确认按钮
    assert "altcha-widget input[type='checkbox']" in query
    assert "submit: click(selector: $selector)" in query


def test_farm_flow_hits_slider_endpoint_in_page():
    rule = core.resolve_rule({"url": "https://pt.0ff.example/"}, {})
    site = {"url": "https://pt.0ff.example/", "cookie": "uid=fixture", "ua": ""}
    calls, fake = _record(
        {"before": {"html": _html("滑动认证")}},
        {"farm": {"value": json.dumps({"step": "hit", "status": 200})},
         "after": {"html": _html("签到成功")}},
    )
    with patch.object(BQLSigner, "_request", fake):
        result = _signer().sign(site, rule)
    assert result.status == "success"
    assert "slide_check_" in calls[1]["variables"]["farm"]
