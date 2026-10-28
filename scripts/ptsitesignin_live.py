#!/usr/bin/env python3
"""PT 站点签到独立联调脚本：脱离 MoviePilot 直接运行插件的各引擎实现。

直接加载插件仓库里真实的 ``core.py`` / ``flaresolverr.py`` / ``function.py``
和 ``sites/`` 站点模块（不依赖 MoviePilot），所以脚本行为与插件引擎逐字一致
——脚本验证通过即代表插件同配置下行为相同。

用法::

    # Cookie 统一从环境变量读（避免进 shell 历史），或交互输入（不回显）
    PTSIGNIN_COOKIE='uid=...' python3 scripts/ptsitesignin_live.py --site mua.xloli.cc

    # FlareSolverr 引擎先 --dry：只写 Cookie + 打开签到页并输出诊断，不求解不提交
    PTSIGNIN_COOKIE='...' python3 scripts/ptsitesignin_live.py --site audiences.me --dry

    # 覆盖引擎 / 附加规则（等同插件里的自定义站点规则 JSON）
    python3 scripts/ptsitesignin_live.py --site ourbits.club --engine flaresolverr
    python3 scripts/ptsitesignin_live.py --site mua.xloli.cc --rule-json '{"flare_tabs":23}'

    # 自建 Browserless Function 引擎（piggo / yemapt / sunnypt / audiences 等）
    PTSIGNIN_COOKIE='...' python3 scripts/ptsitesignin_live.py --site piggo.me

    # 站点模块（0ff 滑块等）；默认「有模块先走模块」，与插件一致
    PTSIGNIN_COOKIE='...' python3 scripts/ptsitesignin_live.py --site pt.0ff.cc

安全说明：Cookie 只发往你自己的 Browserless / FlareSolverr；脚本不打印 Cookie、
Turnstile token 或页面正文（--save-html 只写本地文件）。FlareSolverr 引擎的求解
与提交都发生在它的浏览器里，与本机出口无关；HTTP / 模块引擎可用 --proxy 指定出口。
"""
from __future__ import annotations

import argparse
import base64
import getpass
import importlib.util
import json
import logging
import os
import sys
import types
import urllib.parse
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / "ptsitesignin"
DEFAULT_FLARESOLVERR = os.environ.get("PTSIGNIN_FLARESOLVERR", "")
DEFAULT_BROWSERLESS = os.environ.get("PTSIGNIN_BROWSERLESS", "")
DEFAULT_BQL_CLOUD = "https://production-sfo.browserless.io"
# FlareSolverr 引擎预设没写 mode（表单/回调流不需要）；换 BQL 引擎时这些站按 Turnstile 求解流走
BQL_CLOUDFLARE_HOSTS = {"mua.xloli.cc", "ourbits.club", "audiences.me"}


def load_plugin_modules():
    """以包上下文直载插件子模块，绕过依赖 MoviePilot 的包 ``__init__``。"""
    package = types.ModuleType("ptsitesignin")
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules["ptsitesignin"] = package
    for name in ("core", "flaresolverr", "function", "bql"):
        spec = importlib.util.spec_from_file_location(f"ptsitesignin.{name}", PLUGIN_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"ptsitesignin.{name}"] = module
        spec.loader.exec_module(module)
        setattr(package, name, module)
    return package


def flaresolverr_dry(signer, core, flare, rule, base_url, cookie, save_html, screenshot):
    """只执行「写 Cookie → 打开签到页」，输出与求解相关的全部诊断。"""
    base = base_url.rstrip("/")
    url = core.target_url(base, rule["path"])
    session = "ptsignin-live-" + uuid.uuid4().hex[:8]
    created = signer._call({"cmd": "sessions.create", "session": session}, 30)
    if str(created.get("status")) != "ok":
        print("[x] FlareSolverr 会话创建失败：" + signer._reason(created))
        return 2
    try:
        primed, problem = signer._prime(base, session, cookie, rule)
        if not primed:
            print("[x] Cookie 写入失败：" + problem)
            return 2
        print("[1] Cookie 已写入会话浏览器（预热页面成功）")
        # 与引擎同一条打开路径：雷池这类自放行页会带 waitInSeconds 重试
        opened, problem, page = signer._open_sign_page(session, url)
        if not opened:
            print("[x] 打开签到页失败：" + problem)
            return 2
        print(f"[2] 签到页已打开（页面 {len(page)} 字符）")
        if save_html:
            Path(save_html).write_text(page, encoding="utf-8")
            print(f"    页面已保存：{save_html}")
        if screenshot:
            import base64
            shot = signer._call({"cmd": "request.get", "url": url, "session": session,
                                 "returnScreenshot": True, "maxTimeout": signer._budget(60) * 1000},
                                signer._budget(60))
            image = str(((shot.get("solution") or {}).get("screenshot")) or "")
            if image:
                Path(screenshot).write_bytes(base64.b64decode(image))
                print(f"    页面截图已保存：{screenshot}")
            else:
                print("[!] 未取得截图（FlareSolverr 未返回 screenshot 字段）")
        if core.login_page(page):
            print("[!] 签到页跳到了登录页：Cookie 无效或已过期，先更新站点 Cookie")
            return 2
        if core.cloudflare_pending(page):
            print("[!] 页面停在 Cloudflare / 雷池检测页：若为雷池人工验证（需点击确认），"
                  "FlareSolverr 无法点击，该站应保持 Browserless 路线")
            return 2
        judged = core.classify(page, rule)
        print(f"[3] 页面判定：{judged.status}（{judged.message[:80]}）")
        if judged.status in {"success", "already"}:
            print("[=] 今天已经签到，无需继续")
            return 0
        if core.turnstile_widget_in(page):
            tabs = rule.get("flare_tabs") or core.tabs_till_turnstile(page)
            auto = flare.turnstile_auto_submits(page)
            kind = "回调自动提交（无提交按钮，验证完成后由站点自己提交）" if auto else "表单提交"
            print(f"[4] 检测到 Turnstile 组件（{kind}）；本次将按 {tabs} 次 Tab 聚焦复选框"
                  + ("（手工指定）" if rule.get("flare_tabs") else "（按页面焦点序推算）"))
            if not tabs:
                print("[!] 无法推算 Tab 次数：请在浏览器里手工数 Tab 后用 --tabs 指定")
                return 2
            if auto:
                form = flare.callback_form(page, base, rule["path"])
                if form:
                    print(f"[5] 兜底表单可用：action={urllib.parse.urlsplit(form[0]).path}，"
                          f"字段={sorted(form[1])}")
                print("[=] 诊断完成。去掉 --dry 再跑一次即执行「求解 + 等站点提交 / 兜底」")
                return 0
        else:
            print("[4] 页面没有 Turnstile 组件（站点可能未开启验证），可直接提交")
        form = core.find_signin_form(page, base, str(rule.get("submit_text") or ""))
        if form:
            print(f"[5] 找到签到表单：action={form['action']}，字段={sorted(form['data']) or '（无）'}")
            print("[=] 诊断完成。去掉 --dry 再跑一次即执行「求解 + 提交」")
            return 0
        print("[!] 未找到签到表单：页面结构与预期不符，用 --save-html 留档检查")
        return 2
    finally:
        try:
            signer._call({"cmd": "sessions.destroy", "session": session}, 30)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="PT 站点签到独立联调脚本")
    parser.add_argument("--site", required=True, help="站点域名，如 mua.xloli.cc / piggo.me")
    parser.add_argument("--engine", choices=["http", "function", "flaresolverr", "bql", "fox", "module"],
                        help="覆盖引擎（默认按预设；有站点模块时先走模块，与插件一致）")
    parser.add_argument("--fox-url", default=os.environ.get("PTSIGNIN_FOX_URL", ""),
                        help="foxserve（VPS 上 Playwright+Camoufox 服务）根地址")
    parser.add_argument("--bql-url", default=DEFAULT_BQL_CLOUD,
                        help="Browserless Cloud 根地址（BQL 引擎）")
    parser.add_argument("--bql-token", default=os.environ.get("PTSIGNIN_BQL_TOKEN", "").strip(),
                        help="Browserless Cloud API Token；也可用环境变量 PTSIGNIN_BQL_TOKEN")
    parser.add_argument("--fs", default=DEFAULT_FLARESOLVERR, help="FlareSolverr 根地址")
    parser.add_argument("--browserless", default=DEFAULT_BROWSERLESS, help="自建 Browserless 根地址")
    parser.add_argument("--browserless-token",
                        default=os.environ.get("PTSIGNIN_BROWSERLESS_TOKEN", "").strip(),
                        help="自建 Browserless Token；也可用环境变量 PTSIGNIN_BROWSERLESS_TOKEN 携带。"
                             "以 Authorization: Bearer 请求头发送，实例未启用鉴权时留空")
    parser.add_argument("--ocr", default="", help="MoviePilot-OCR 地址（图片验证码站点需要）")
    parser.add_argument("--proxy", default="", help="HTTP / 模块引擎的出口代理，如 http://proxy:7890")
    parser.add_argument("--timeout", type=int, default=90, help="单次任务预算秒数（默认 90）")
    parser.add_argument("--ua", default=os.environ.get("PTSIGNIN_UA", "").strip(),
                        help="站点 UA（部分站点会话与 UA 绑定，如 yemapt）；也可用环境变量 PTSIGNIN_UA")
    parser.add_argument("--flare-proxy", default="",
                        help="FlareSolverr 会话级出口代理（与日常浏览器同出口可排除 IP 因素），"
                             "支持 http(s)/socks5 与用户名密码")
    parser.add_argument("--flare-auth", default=os.environ.get("PTSIGNIN_FLARE_AUTH", "").strip(),
                        help="浏览器服务 Basic Auth 凭据 user:password（nginx 鉴权场景）；"
                             "也可用环境变量 PTSIGNIN_FLARE_AUTH")
    parser.add_argument("--tabs", type=int, help="FlareSolverr 引擎：手工覆盖 Tab 按键次数")
    parser.add_argument("--rule-json", default="{}", help="附加规则覆盖，等同插件的自定义站点规则 JSON")
    parser.add_argument("--dry", action="store_true", help="FlareSolverr 引擎：只诊断不求解不提交")
    parser.add_argument("--save-html", metavar="PATH", help="（--dry）把签到页 HTML 保存到本地文件")
    parser.add_argument("--screenshot", metavar="PATH",
                        help="（--dry）把签到页截图保存为 PNG，便于查看验证组件的真实渲染状态")
    parser.add_argument("--save-after", metavar="PATH",
                        help="（Function 引擎）把执行后的最终页面 HTML 保存到本地文件，便于失败排查")
    args = parser.parse_args()

    cookie = os.environ.get("PTSIGNIN_COOKIE", "").strip()
    if not cookie:
        cookie = getpass.getpass(f"粘贴 {args.site} 的站点 Cookie：").strip()
    if "=" not in cookie:
        print("Cookie 格式不对，应为 name=value; name2=value2 形式")
        return 2
    try:
        overrides = dict(json.loads(args.rule_json or "{}"))
    except ValueError as error:
        print("--rule-json 不是合法 JSON：" + str(error))
        return 2
    if args.tabs:
        overrides["flare_tabs"] = args.tabs
    if args.flare_proxy:
        overrides["flare_proxy"] = args.flare_proxy
    if args.engine:
        overrides["engine"] = args.engine
        if args.engine == "bql" and args.site in BQL_CLOUDFLARE_HOSTS:
            # FlareSolverr 预设不写 mode；BQL 引擎下这些站走 Turnstile solve 流
            overrides.setdefault("mode", "cloudflare")
            overrides.setdefault("submit_text", "签到")
        if args.engine == "bql" and args.site == "piggo.me":
            # piggo 预设的 mode=cloudflare 是 Function 引擎语义（点签到后等雷池放行）；
            # BQL 下走点击流：点签到 → 点雷池确认 → 等结果
            overrides["mode"] = "open_page"

    package = load_plugin_modules()
    core = package.core
    base_url = f"https://{args.site}/"
    site = {"url": base_url, "cookie": cookie, "ua": args.ua.strip()}
    try:
        rule = core.resolve_rule(site, {args.site: overrides} if overrides else {})
    except ValueError as error:
        print("规则无效：" + str(error))
        return 2

    # 与插件一致：有站点模块先走模块（除非 --engine 指定了别的引擎）
    use_module = args.engine == "module" or (args.engine is None and
                                             core.site_module_for(base_url) is not None)
    proxies = None
    if args.proxy and (use_module or rule["engine"] == "http"):
        proxies = {"http": args.proxy, "https": args.proxy}
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger = logging.getLogger("ptsitesignin_live")

    if use_module:
        print(f"站点模块路线（{args.site}），超时 {args.timeout}s")
        result = core.run_site_module(site, timeout=args.timeout, proxies=proxies,
                                      logger=logger, ocr_url=args.ocr)
        engine = "module"
        if result is None:
            print("（没有匹配的站点模块，改按规则引擎执行）")
    if not use_module or result is None:
        engine = rule["engine"]
        if engine == "flaresolverr":
            print(f"FlareSolverr 路线：{rule.get('flare_url') or args.fs}（签到页={rule['path']}，"
                  f"预热页={rule.get('flare_prime_path') or '自动回退'}）")
            signer = package.flaresolverr.FlareSolverrSigner(
                str(rule.get("flare_url") or args.fs), args.timeout,
                proxy=str(rule.get("flare_proxy") or ""), auth=args.flare_auth)
            if args.dry:
                try:
                    return flaresolverr_dry(signer, core, package.flaresolverr, rule,
                                            base_url, cookie, args.save_html, args.screenshot)
                except (TimeoutError, OSError) as error:
                    # 隧道抖动（TLS EOF / 连接重置）是瞬时故障：稍等重跑即可
                    print(f"[x] FlareSolverr 连接中断（{type(error).__name__}）：隧道瞬时抖动，稍后重跑")
                    return 2
            result = signer.sign(site, rule)
            shot = getattr(signer, "_last_screenshot", "")
            if shot:
                shot_path = args.screenshot or f"/tmp/ptsitesignin_{args.site}_widget.png"
                Path(shot_path).write_bytes(base64.b64decode(shot))
                print(f"组件诊断截图已保存：{shot_path}")
        elif engine == "function":
            print(f"自建 Browserless 路线：{args.browserless}（mode={rule.get('mode')}，签到页={rule['path']}）")
            signer = package.function.FunctionSigner(
                args.browserless, args.browserless_token, args.timeout,
                ocr_url=args.ocr, browser_proxy=args.proxy)
            result = signer.sign(site, rule)
            after = str(getattr(signer, "_last_raw", {}).get("after") or "")
            save_after = args.save_after
            if not save_after and result.status == "failed" and after:
                save_after = f"/tmp/ptsitesignin_{args.site}_after.html"
            if save_after and after:
                Path(save_after).write_text(after, encoding="utf-8")
                print(f"最终页面已保存：{save_after}（{len(after)} 字符）")
            shot = str(getattr(signer, "_last_raw", {}).get("shot") or "")
            if shot:
                shot_path = f"/tmp/ptsitesignin_{args.site}_waf.png"
                Path(shot_path).write_bytes(base64.b64decode(shot))
                print(f"验证失败截图已保存：{shot_path}")
        elif engine == "bql":
            if not args.bql_token:
                print("缺少 Browserless Cloud Token：用 --bql-token 或环境变量 PTSIGNIN_BQL_TOKEN")
                return 2
            if not args.proxy:
                print("BQL 引擎必须配置代理（--proxy http://user:pass@host:port），"
                      "Cloud 共享出口不用于签到")
                return 2
            print(f"Browserless Cloud BQL 路线：{args.bql_url}（mode={rule.get('mode')}，"
                  f"签到页={rule['path']}，代理出口=已启用，"
                  f"token {len(args.bql_token)}位 {args.bql_token[:4]}...）")
            signer = package.bql.BQLSigner(args.bql_url, args.bql_token, args.timeout,
                                           proxy=args.proxy)
            result = signer.sign(site, rule)
        elif engine == "fox":
            print(f"foxserve 路线：{args.fox_url}（远程 Playwright 服务，鉴权={'Bearer' if os.environ.get('PTSIGNIN_FOX_TOKEN') else '无'})")
            payload = json.dumps({"site": args.site, "cookie": cookie,
                                  "path": rule.get("path") or "/attendance.php",
                                  "proxy": ({"server": args.proxy} if args.proxy else None)}).encode()
            import urllib.request as _rq
            headers = {"Content-Type": "application/json"}
            fox_token = os.environ.get("PTSIGNIN_FOX_TOKEN", "").strip()
            if fox_token:
                headers["Authorization"] = "Bearer " + fox_token
            request = _rq.Request(args.fox_url.rstrip("/") + "/sign", data=payload,
                                  headers=headers, method="POST")
            try:
                with _rq.build_opener(_rq.ProxyHandler({})).open(request, timeout=200) as response:
                    data = json.loads(response.read().decode())
                result = core.SignResult(str(data.get("status") or "failed"),
                                         str(data.get("message") or "未知结果"))
            except Exception as error:
                result = core.SignResult("failed", f"foxserve 请求失败：{type(error).__name__} {error}")
        else:
            print(f"HTTP 路线：{rule.get('method', 'GET')} {rule['path']}")
            result = core.http_sign_auto(site, rule, timeout=args.timeout, proxies=proxies)
    print(f"结果（engine={engine}）：{result.status}")
    print(result.message)
    return 0 if result.status in {"success", "already"} else 1


if __name__ == "__main__":
    sys.exit(main())
