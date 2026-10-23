"""Contracts at the self-hosted HTTP transport and site-module boundaries."""
import json
import sys
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ptsitesignin import core
from ptsitesignin.function import FunctionSigner, service_url


SITE = {"url": "https://pt.example/", "cookie": "session=fixture", "ua": "fixture-agent"}


def _opener(payload):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(payload).encode()
    return Mock(open=Mock(return_value=response))


def test_function_uses_one_authenticated_request_and_ocr_contract():
    opener = _opener({"after": "<body>签到成功</body>"})
    signer = FunctionSigner("https://private.example", "fixture-token", 120, "https://ocr.example")
    rule = core.resolve_rule({"url": "https://open.cd"}, {})
    with patch("urllib.request.build_opener", return_value=opener):
        result = signer.sign(SITE, rule)
    assert result.status == "success"
    assert opener.open.call_count == 1
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://private.example/chromium/function?timeout=120000"
    assert request.get_header("Authorization") == "Bearer fixture-token"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert set(body) == {"code", "context"}
    assert body["context"]["ocr_url"] == "https://ocr.example/captcha/base64"
    assert body["context"]["cookie"] == SITE["cookie"]
    assert "token" not in request.full_url


def test_explicit_function_endpoint_and_reverse_proxy_prefix_are_preserved():
    assert service_url("https://host.example/prefix/", "/chromium/function") == "https://host.example/prefix/chromium/function"
    assert service_url("https://host.example/chromium/function", "/chromium/function") == "https://host.example/chromium/function"
    assert service_url("https://host.example/function", "/chromium/function") == "https://host.example/function"
    for address in ["https://host.example/stealth/bql", "https://host.example/stealth"]:
        try:
            FunctionSigner(address, "fixture")
        except ValueError:
            pass
        else:
            raise AssertionError("A BQL configuration must not silently become a Function URL")


def test_old_cloud_configuration_cannot_send_credentials_to_cloud():
    for host in ["browserless.io", "production-sfo.browserless.io"]:
        try:
            FunctionSigner("https://" + host, "fixture")
        except ValueError as error:
            assert "自建" in str(error)
        else:
            raise AssertionError("The self-hosted fork accepted a Cloud endpoint")


def test_browser_proxy_auth_is_separate_from_the_launch_url():
    opener = _opener({"after": "<body>今日已签到</body>"})
    signer = FunctionSigner("https://private.example", "fixture", browser_proxy="http://user:pass@proxy.example:7890")
    with patch("urllib.request.build_opener", return_value=opener):
        result = signer.sign(SITE, {"path": "/attendance.php", "mode": "open_page"})
    assert result.status == "already"
    request = opener.open.call_args.args[0]
    assert "user" not in request.full_url and "pass" not in request.full_url
    assert json.loads(request.data)["context"]["proxy_auth"] == {"username": "user", "password": "pass"}


def test_click_and_challenge_pages_are_not_success_receipts():
    signer = FunctionSigner("https://private.example", "fixture")
    rule = {"path": "/attendance.php", "mode": "open_page", "success_on_click": True}
    for payload in [{"after": "<body>立即签到</body>", "clicked": True},
                    {"after": "<title>Just a moment</title>", "clicked": True},
                    {"error": "challenge", "after": "<body>签到成功</body>"},
                    {"after": "<body>请先登录</body>"},
                    {"after": ""}]:
        with patch.object(signer, "_request", return_value=payload):
            assert signer.sign(SITE, rule).status == "failed"


def test_transport_failure_is_not_replayed_and_never_exposes_token():
    opener = Mock()
    opener.open.side_effect = TimeoutError("transport fixture")
    signer = FunctionSigner("https://private.example", "private-fixture-token")
    with patch("urllib.request.build_opener", return_value=opener):
        result = signer.sign(SITE, {"path": "/attendance.php"})
    assert result.status == "failed" and result.transient
    assert opener.open.call_count == 1
    assert "private-fixture-token" not in result.message


def test_http_challenge_on_403_or_503_can_reach_function_fallback():
    for status in [200, 403, 503]:
        session = SimpleNamespace(headers={}, get=lambda *a, **k: SimpleNamespace(
            status_code=status, text="<html><title>Just a moment...</title></html>"))
        with patch.dict(sys.modules, {"requests": SimpleNamespace(Session=lambda: session)}):
            outcome = core.http_sign_auto(SITE, core.resolve_rule(SITE, {}))
        assert outcome.status == "failed" and outcome.challenge == "cloudflare"
        fallback = core.fallback_rule({"path": "/attendance.php", "success_keywords": ["奖励已到账"]}, outcome.challenge)
        assert fallback["engine"] == "function" and fallback["success_keywords"] == ["奖励已到账"]


def test_old_browserless_rules_route_to_function_and_fixed_http_stays_fixed():
    resolved = core.resolve_rule(SITE, {"pt.example": {"engine": "browserless", "mode": "cloudflare"}})
    assert resolved["engine"] == "function"
    assert resolved["mode"] == "cloudflare"
    assert not core.resolve_rule(SITE, {"pt.example": {"engine": "http"}})["auto_fallback"]


def test_image_mode_requires_ocr_before_a_browser_task_is_sent():
    signer = FunctionSigner("https://private.example", "fixture")
    with patch.object(signer, "_request") as send:
        result = signer.sign(SITE, {"path": "/attendance.php", "mode": "image"})
    assert result.status == "failed" and "OCR" in result.message
    send.assert_not_called()


def test_module_ocr_sends_only_base64_image_to_the_configured_service():
    api = core._sites_api()
    context = api.SignContext(SITE, ocr_url="https://ocr.example")
    context.image = Mock(return_value=b"fixture-image")
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.post.return_value.json.return_value = {"result": "ABC123"}
    with patch.dict(sys.modules, {"requests": SimpleNamespace(Session=lambda: session)}):
        answer = context.ocr("https://pt.example/captcha.png")
    assert answer == "ABC123"
    assert session.post.call_args.args == ("https://ocr.example/captcha/base64",)
    assert session.post.call_args.kwargs == {"json": {"base64_img": "Zml4dHVyZS1pbWFnZQ=="}, "timeout": 30}


def test_farm_reuses_set_cookie_from_its_verification_endpoint():
    class Session:
        def __init__(self):
            self.headers = {}
            self.values = {}
            self.cookies = SimpleNamespace(set=lambda name, value, **kw: self.values.update({name: value}))

        def get(self, url, **kwargs):
            if url.endswith("attendance.php"):
                body = '今日已签到' if self.values.get("verified") else '<script src="/slide_check_test.js"></script>slide_check'
            elif url.endswith(".js"):
                body = 'const endpoint="/set_access_token-test";'
            else:
                self.values["verified"] = "1"  # Set-Cookie from the independent verification endpoint.
                body = "ok"
            return SimpleNamespace(status_code=200, text=body)

        def close(self):
            pass

    with patch.dict(sys.modules, {"requests": SimpleNamespace(Session=Session)}), patch("time.sleep"):
        result = core.run_site_module({"url": "https://pt.0ff.cc", "cookie": "uid=fixture"})
    assert result.status in {"success", "already"}


def test_local_browser_unavailable_falls_back_with_site_and_rule():
    from ptsitesignin import PTSiteSignIn
    plugin = object.__new__(PTSiteSignIn)
    plugin._proxies_for = lambda info: None
    signer = Mock()
    signer.sign.return_value = core.SignResult("success", "签到成功")
    with patch("ptsitesignin.playwright_sign", return_value=None):
        result, engine = plugin._sign_with_engine(SITE, {"path": "/index.php"}, "playwright", signer, "fixture")
    assert result.status == "success" and engine == "function"
    assert signer.sign.call_args.args == (SITE, {"path": "/index.php", "engine": "function", "mode": "open_page"})


def test_self_hosted_configuration_round_trips_and_is_exposed_in_form():
    from ptsitesignin import PTSiteSignIn
    plugin = object.__new__(PTSiteSignIn)
    configured = {"browserless_url": "https://private.example", "browserless_token": "fixture",
                  "ocr_url": "https://ocr.example", "browser_proxy": "http://proxy.example:7890"}
    plugin.init_plugin(configured)
    try:
        with patch("ptsitesignin.SiteOper") as sites:
            sites.return_value.list_order_by_pri.return_value = []
            form, defaults = plugin.get_form()
        for key, value in configured.items():
            assert defaults[key] == value
        models = []
        def walk(value):
            if isinstance(value, dict):
                if "model" in value:
                    models.append(value["model"])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(form)
        assert set(configured) <= set(models)
        assert "browserless_token2" not in models
    finally:
        plugin.stop_service()
