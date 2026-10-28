"""PT 签到核心逻辑：HTTP 引擎 + 自建浏览器共享工具 + 站点登录 / 最近动向。

本模块不含任何 MoviePilot 依赖，方便单独测试与复用。
Browserless 相关实现参考 tmzg0000/moviepilot-plugins 的 captchasignin 插件
（AGPL/MIT 见原仓库），在其基础上精简并与 HTTP 引擎合并为双引擎结构。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import random
import re
from pathlib import Path
import time
import urllib.parse
from dataclasses import dataclass
from html import unescape as _html_unescape
from typing import Any, Dict, List, Mapping, Optional, Sequence

DEFAULT_HTTP_PATH = "/attendance.php"
DEFAULT_SUCCESS_PATTERN = (r"(state['\"]?\s*[:=]\s*['\"]?success|签到成功|簽到成功|签到 成功|"
                          r"成功签到|签到完成|簽到完成|领取成功|領取成功|签到已成功)")
# 注意：不要在这里放「已连续签到」——TTG 用「您已连续签到 N 天」表示**签到成功**，
# 憨憨那类站点需要它时在自己的 already_keywords 里单独声明。
DEFAULT_ALREADY_PATTERN = (r"(今日已签|今天已签|已经签到|已签到过|已签到|已簽到|"
                           # NexusPHP 常见写法：鲸币 [ 使用 ]: 131,943.3 (签到已得64)
                           r"签到已得|已领取过|不要重复签到|无需重复签到|already|signed in today)")
COOKIE_INVALID_PATTERN = r"(用户登录|會員登入|请先登录|请登录|login\.php)"
# Cloudflare 挑战页特征（出现即说明请求未穿透盾，需改用 Browserless）
CLOUDFLARE_PATTERN = r"(just a moment|cf-chl-widget|cf_chl_opt|challenge-platform)"
# 雷池（SafeLine）WAF 的安全检测页：hdkyl.in、piggo.me 这类站点点签到后会先跳这里，
# 由页面自己的 JS 跑几秒后自动放行，行为和 Cloudflare 交互挑战一样，只是文案不同。
WAF_PATTERN = r"(正在进行安全检测|安全检测能力由|雷池\s*WAF|SafeLine)"
# 站点要求「二级验证 / 异地登录验证」：猪猪（piggo.me）在非常用网络或新浏览器上会先出这一页，
# 必须输入 2FA 动态码才能继续——插件没法自动完成，要明确提示而不是报「找不到签到按钮」。
SECOND_FACTOR_PATTERN = r"(异地登录安全验证|異地登錄安全驗證|二级验证|二級驗證|两步验证|兩步驗證|忘记二级验证|忘記二級驗證|2FA)"
# Cloudflare「Just a moment...」交互挑战的专属特征：这类挑战会在真实浏览器里自动执行 JS
# 并放行，只要留够等待时间即可，无需人工点击。注意不能用 challenge-platform /
# cf-chl-widget 判断——正常的 Turnstile 组件页也会带这些标记；挑战放行后页面会换成新
# 文档，_cf_chl_opt / chl_page 随之消失。
CF_INTERSTITIAL_PATTERN = r"(just a moment|_cf_chl_opt|chl_page)"
# 页面内嵌的 Cloudflare Turnstile 人机验证组件（观众 Audiences 的签到页就是这种）：验证结果
# 由页面 JS 在回调里生成，HTTP 直连拿不到，必须在真实浏览器里执行 JS 才能完成签到；注意与
# 上面的「Just a moment...」交互挑战区分开——这类正常页面不带 _cf_chl_opt / chl_page。
TURNSTILE_PATTERN = r"(cf-turnstile|turnstile/v0|turnstile\.min\.js|turnstile\.render)"
# 页面跳回登录入口时不能把验证完成当作签到完成。
LOGIN_PAGE_PATTERN = (r"(takelogin\.php|login\.php\?returnto|auth/sign-in|auth/login|"
                      r"shouldClearAuth|/login\?redirect|请先登录|請先登入|session expired)")
# 图片验证码 / 人机校验特征（HTTP 直连拿不到结果，需改用 Browserless）
CAPTCHA_PATTERN = (
    r"(captcha\.php|<img[^>]+captcha"
    r"|id\s*=\s*[\"']?(?:captcha|imagestring|imagehash|verifycode|imagecode)[\"']?"
    r"|name\s*=\s*[\"']?(?:imagestring|imagehash|verifycode|imagecode|answer|captcha)[\"']?"
    r"|验证码|驗證碼)"
)
# 签到奖励：单位 / 数量 / 动词抽成常量共用，新增币种只改 BONUS_UNITS 一处
BONUS_UNITS = "魔力值|魔力|爆米花|积分|做种积分|金币|憨豆|金贝|鲸币|奖励"
BONUS_AMOUNT = r"[\d,]+(?:\.\d+)?"
BONUS_COUNTERS = "个|粒|枚|点|個|顆"
BONUS_VERBS = "获得|獲得|獲取"
BONUS_PATTERNS = (
    rf"((?:{BONUS_VERBS})\s*{BONUS_AMOUNT}\s*(?:{BONUS_COUNTERS})?\s*(?:{BONUS_UNITS}))",
    rf"({BONUS_AMOUNT}\s*(?:{BONUS_COUNTERS})\s*(?:{BONUS_UNITS}))",
    rf"((?:{BONUS_UNITS})\s*[:：]?\s*\+?\s*{BONUS_AMOUNT})",
    rf"\+({BONUS_AMOUNT})\s*(?:{BONUS_UNITS})",
    rf"(?:签到已得|簽到已得)\s*({BONUS_AMOUNT})",
)
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SUCCESS_WORDS = ("签到成功", "簽到成功", "成功签到", "打卡成功")
ALREADY_WORDS = ("已签到", "已经签到", "今日已签", "今天已签", "已打卡", "already")
LOGIN_WORDS = ("用户登录", "會員登入", "login")

# 自建服务必须显式配置，不能沿用官方 Cloud 默认地址。
DEFAULT_BROWSERLESS_URL = ""

# ---------------------------------------------------------------- 内置站点规则
# mode 仅对 Function 浏览器引擎有效：
#   http          直接 HTTP 请求签到接口（默认引擎，无浏览器会话）
#   open_page     打开页面并点击签到按钮（无需验证码）
#   cloudflare    打开页面，等待并尝试完成 Cloudflare / Turnstile 验证
#   image         图片验证码签到
#   trigger_image 先点开弹窗再处理图片验证码（HDSky 类）
#   altcha        需先完成 Altcha 人机验证再点击提交（YemaPT 类）
DEFAULT_RULES: Dict[str, Dict[str, Any]] = {
    # ---- 以下为需要浏览器交互的站点预设（来自 captchasignin 参考实现）----
    "open.cd": {
        # 皇后：plugin_sign-in.php 的图片验证码表单，交给 Browserless 图片模式解。
        # 签到成功后页面会出现「[查看簽到記錄]」，验证码表单消失，按这个判成功。
        "engine": "function",
        "mode": "image",
        "path": "/plugin_sign-in.php",
        "preflight_path": "/",
        "verify_success_path": "/",
        # 最近动向在个人页；没有 id 会显示「选择用户」，read_activity 会用 Cookie 的
        # c_secure_uid 拼 ?id= 打开自己的资料页
        "activity_path": "/userdetails.php",
        "captcha_selector": "#frmSignin img",
        "captcha_input_selector": "#imagestring",
        "submit_selector": "#ok",
        "submit_method": "ajax",
        "success_json": {"field": "/state", "values": ["success"]},
        "success_keywords": ["查看簽到記錄", "簽到成功", "签到成功"],
        "already_keywords": ["查看簽到記錄", "已簽到", "已签到", "今日已签", "今日已簽"],
        "strict_status_keywords": True,
    },
    "p.t-baozi.cc": {
        "engine": "function",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img",
        "captcha_input_selector": "input[name='imagestring']",
        "submit_selector": "input[type='submit']",
        "already_keywords": ["签到成功"],
    },
    "pt.muxuege.org": {
        "engine": "function",
        "mode": "open_page",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit']",
        "submit_method": "dom_click",
        "submit_text": "立即签到",
    },
    "oshen.win": {
        "engine": "function",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
        "captcha_input_selector": "form input[name='imagestring']",
        "submit_selector": "form input[type='submit'][value='立即签到']",
        "submit_method": "ajax",
    },
    "ptsbao.club": {
        # 烧包：attendance.php 的图片验证码签到（和 oshen.win 同款 NexusPHP 表单）
        "engine": "function",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
        "captcha_input_selector": "form input[name='imagestring']",
        "submit_selector": "form input[type='submit'][value='立即签到']",
        "submit_method": "ajax",
        "success_keywords": ["签到成功", "簽到成功", "本次获得", "本次獲得"],
        "already_keywords": ["已经签到", "已經簽到", "已签到", "已簽到", "今日已签到"],
    },
    "pt.luckpt.de": {
        # 只做日常签到：/attendance.php。之前走 /medal_collection.php 领「纪念套装」的
        # 幸运星，勋章凑齐后按钮消失，反而把签到判成失败。若站点首页就是签到页，
        # fallback_rule / discover 会再兜底。
        "engine": "function",
        "mode": "open_page",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit'], a[href*='attendance']",
        "submit_method": "dom_click",
        "submit_text": "签到",
        "success_keywords": ["签到成功", "已获得", "本次获得"],
        "already_keywords": ["已签到", "今天已签到", "今日已签到"],
    },
    "yemapt.org": {
        "engine": "function",
        "mode": "altcha",
        "path": "/",
        "route_fragment": "#/user/growth?tab=checkIn",
        "altcha_checkbox_selector": "altcha-widget input[type='checkbox']",
        "altcha_payload_selector": "altcha-widget .altcha[data-state='verified'] input[name='altchaPayload']",
        "submit_selector": "button.ant-btn-primary.ant-btn-lg.ant-btn-block:not([disabled])",
        "submit_method": "altcha_click",
        "submit_text": "立即签到",
        "already_keywords": ["已签到，明日继续", "你的今日状态：已签到"],
        "preflight_wait": 8000,
        "wait": 5000,
    },
    "hdsky.me": {
        "engine": "function",
        "mode": "trigger_image",
        "path": "/index.php",
        "trigger_selector": "#showup",
        "captcha_selector": "#showupimg",
        "captcha_input_selector": "#imagestring",
        "submit_selector": "#showupbutton",
        "submit_method": "dom_click",
        "submit_text": "Let's Go",
        "success_keywords": ["已签到"],
    },
    "dstudio.me": {"engine": "function", "mode": "cloudflare", "path": "/attendance.php"},
    # ---- AGSVPT（agsvpt.com）：NexusPHP 签到页，页面上是一个「立即签到」表单提交按钮 ----
    # GET /attendance.php 只会把按钮渲染出来，不点按钮不会签到（实测未登录时 302 到
    # login.php?returnto=attendance.php），所以 HTTP 引擎必须用 POST；万一站点开了验证码 /
    # 撞上盾，自动回退到浏览器引擎时会按下面的选择器与按钮文字点「立即签到」。
    # 判定文案沿用默认正则（签到成功 / 已经签到），不额外收窄。
    "agsvpt.com": {
        "engine": "http",
        "method": "POST",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit'], button",
        "submit_method": "dom_click",
        "submit_text": "立即签到",
    },
    # ---- 萝莉（mua.xloli.cc）：NexusPHP 签到表单内嵌 Cloudflare Turnstile ----
    # 未登录直接访问 attendance.php 是 404（Chrome 错误页 origin 为空，Cookie 写不进
    # 浏览器），访问首页还会断连；Turnstile 只渲染在登录后的表单里。所以用 FlareSolverr
    # 会话：先在 login.php 写入 Cookie → 打开签到页 → 原生 tabs_till_verify 解内嵌
    # widget（次数由插件按页面焦点序推算）→ 同一会话 request.post 提交。token 一次性
    # 有效，且 NexusPHP 的 siteverify 会核对提交 IP——必须与求解同一个浏览器。
    "mua.xloli.cc": {
        "engine": "flaresolverr",
        "path": "/attendance.php",
        # 未登录时签到页 404、首页断连，Cookie 只能写在 login.php 这样的真实页面上
        "flare_prime_path": "/login.php",
        "submit_text": "签到",
    },
    "share.ilolicon.com": {
        "engine": "function",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_selector": "form[action*='attendance.php'] input[type='submit']",
    },
    # ---- TTG（totheglory.im）：没有 attendance.php，签到入口在站点页面上 ----
    # 探测：/attendance.php、/signin.php、/checkin.php 都是 404，站点在 Cloudflare 后面，
    # 所以走浏览器打开首页点「签到」。若你那边入口在别的页面，改 path 即可。
    "totheglory.im": {
        "engine": "http",
        "method": "GET",
        # 和 thsrite 的站点模块同一套流程：首页有绿色「已签到」就是今天签过了；
        # 否则从首页 JS 里取 signed_timestamp / signed_token，POST 到 /signed.php。
        "path": "/index.php",
        "already": "<b[^>]*>\\s*已签到\\s*</b>",
        "already_keywords": ["今天已签到过", "不要太贪", "已連續簽到", "您已連續簽到"],
        "signin_path": "/signed.php",
        "signin_method": "POST",
        "signin_token_patterns": {
            "signed_timestamp": "signed_timestamp:\\s*\"(\\d{10})\"",
            "signed_token": "signed_token:\\s*\"([^\"]+)\"",
        },
        "success_keywords": ["您已连续签到", "已連續簽到", "签到成功"],
    },
    # ---- HDKylin（hdkyl.in）：NexusPHP，签到在 /attendance.php ----
    # 站点在 Cloudflare 后面（访客请求直接 468），HTTP 直连多半要挑战，默认走浏览器。
    "hdkyl.in": {
        "engine": "function",
        "mode": "open_page",
        # 签到入口是首页的「签到」链接，点完会跳雷池 WAF 检测页，等放行后再读结果
        "path": "/index.php",
        "submit_selector": "a, input[type='submit'], button, [role='button'], span",
        "submit_method": "dom_click",
        "submit_text": "签到",
        "wait": 12000,
        "pending_keywords": ["正在进行安全检测", "安全检测"],
        "success_keywords": ["签到成功", "簽到成功", "签到完成", "已签到", "本次签到"],
        "already_keywords": ["今日已签到", "今天已签到", "已签到", "已簽到", "已领取"],
    },
    # ---- Piggo（piggo.me）：CloakBrowser 远程路线（2026-10-28 实测签到成功）----
    # 三个必要条件（缺一不可，详见 cloak.py 模块头与 README）：
    # 1. 站点 Cookie 必须新鲜整串（cf_clearance + sl-* + 会话）——站点在新登录时
    #    轮换会话令牌，旧串立即失效（换浏览器重登 = 旧 Cookie 作废）；
    # 2. 实例出口 IP 与登录浏览器一致：家宽 Clash 需对 piggo.me 拨 IPv4
    #    （hosts 钉 A 记录，用 piggo.me/cdn-cgi/trace 验证），否则会话按精确 IP 判游客；
    # 3. UA 与签发 Cookie 的浏览器同族（默认 Chrome；站点管理设了 UA 则优先）。
    # 不想用远程服务时，可自定义规则 {"engine":"playwright"} 走 MoviePilot 本机
    # 浏览器（出口同为家宽 v4 时上游实证可用）。
    "piggo.me": {
        "engine": "cloak",
        "path": "/attendance.php",
        # 照 tmzg 的做法：按钮消失不算成功，只认站点给的明确文字；
        # 已签到只看这几个（「已领取」是别的站点文案，容易误判）
        "success_keywords": ["签到成功", "簽到成功", "签到完成", "本次签到", "获得"],
        "already_keywords": ["今日已签到", "今天已签到", "已签到", "已簽到", "明日"],
    },
    # ---- 以下站点流程参考 thsrite「站点自动签到」的 sites/ 模块 ----
    # 只收录能用规则表达的：POST 固定字段、首页抓 CSRF、JSON 返回判定这几种。
    # ---- hdarea（hdarea.club）：POST sign_in.php，返回页带「此次签到您获得」 ----
    "hdarea.club": {
        "engine": "http",
        "method": "POST",
        "path": "/sign_in.php",
        "data": {"action": "sign_in"},
        "success_keywords": ["此次签到您获得"],
        "already_keywords": ["请不要重复签到哦"],
    },
    # ---- NexusHD（v6.nexushd.org）：POST signin.php ----
    "nexushd.org": {
        "engine": "http",
        "method": "POST",
        "path": "/signin.php",
        "data": {"action": "post", "content": ""},
        "success_keywords": ["本次签到获得"],
        "already_keywords": ["你今天已经签到过了"],
    },
    # ---- HDCTV（hdcity.city）：签到页 /sign 直接给结果 ----
    "hdcity.city": {
        "engine": "http",
        "method": "GET",
        "path": "/sign",
        "success_keywords": ["本次签到获得魅力"],
        "already_keywords": ["已签到"],
    },
    # ---- HDUPT（pt.hdupt.com）：GET added.php?action=qiandao ----
    "hdupt.com": {
        "engine": "http",
        "method": "GET",
        "path": "/added.php?action=qiandao",
        "success_keywords": ["本次签到获得魅力"],
        # 已签到时首页会把这个 span 打出来
        "already": "<span[^>]*id=\"yiqiandao\"",
    },
    # ---- PTT（pttime.org）：attendance.php 直接签 ----
    "pttime.org": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "success_keywords": ["签到成功"],
        "already_keywords": ["今日已签到", "今天已签到", "已签到"],
    },
    # ---- Hares（club.hares.top）：attendance.php?action=sign 返回 JSON ----
    "hares.top": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php?action=sign",
        "success_keywords": ["签到成功", "\"success\":true"],
        "already_keywords": ["已签到", "已经签到"],
    },
    # ---- PterClub（pterclub.com）：attendance-ajax.php 返回 JSON，status=1 即成功 ----
    "pterclub.com": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance-ajax.php",
        "success_keywords": ["\"status\":\"1\"", "签到已成功", "本次签到获得"],
        "already_keywords": ["您今天已经签到过了", "请勿重复刷新"],
    },
    # ---- 海胆（haidan.video）：signin.php 触发签到，回首页看按钮是否变「已经打卡」 ----
    "haidan.video": {
        "engine": "http",
        "method": "GET",
        # 签到后首页按钮会变成「已经打卡」，所以先读首页判断，再去 signin.php，
        # 拿不到结论时插件会自动回读首页确认（日志里「未匹配到签到成功标志」就是这么来的）
        "path": "/index.php",
        "already_keywords": ["已经打卡", "已打卡"],
        # 站点首页那个 id=modalBtn 的按钮点下去是 jQuery AJAX 打 signin.php，
        # 带 X-Requested-With 才认；不带会被跳到登录页（之前一直误报失败的原因）
        "signin_path": "/signin.php",
        "signin_method": "GET",
        "signin_headers": {"X-Requested-With": "XMLHttpRequest"},
        "success_keywords": ["已经打卡", "已打卡", "打卡成功", "签到成功"],
    },
    # ---- HDChina（hdchina.org）：首页取 x-csrf，POST plugin_sign-in.php?cmd=signin ----
    "hdchina.org": {
        "engine": "http",
        "method": "GET",
        "path": "/index.php",
        "already": "<a[^>]*class=\"label label-default\"[^>]*>已签到</a>",
        "signin_path": "/plugin_sign-in.php?cmd=signin",
        "signin_method": "POST",
        "signin_token_patterns": {"csrf": "<meta name=\"x-csrf\" content=\"([^\"]+)\""},
        "success_keywords": ["\"state\":\"success\"", "\"state\": \"success\""],
    },
    # ---- 52PT（52pt.site）/ CHDBits（ptchdbits.co）：bakatest 答题签到，随机选一个选项 ----
    # 两个站同一套 NexusPHP 插件：首页签到页给 questionid + 若干 choice[]，随便选一个提交即可。
    "52pt.site": {
        "engine": "http",
        "method": "GET",
        "path": "/bakatest.php",
        "already_keywords": ["今天已经签过到了"],
        "signin_path": "/bakatest.php",
        "signin_method": "POST",
        "signin_pick": "random",
        "signin_data": {"usercomment": "太难了！", "wantskip": "不会"},
        "success": "\\d+\\s*点魔力值",
    },
    "ptchdbits.co": {
        "engine": "http",
        "method": "GET",
        "path": "/bakatest.php",
        "already_keywords": ["今天已经签过到了"],
        "signin_path": "/bakatest.php",
        "signin_method": "POST",
        "signin_pick": "random",
        "signin_data": {"usercomment": "太难了！", "wantskip": "不会"},
        "success": "\\d+\\s*点魔力值",
    },
    # ---- U2（u2.dmhy.org）：showup 四选一答题，随机点一个提交按钮 ----
    "u2.dmhy.org": {
        "engine": "http",
        "method": "GET",
        "path": "/showup.php",
        "already": "<a[^>]*href=\"showup.php\"[^>]*>已签到</a>",
        "signin_path": "/showup.php?action=show",
        "signin_method": "POST",
        "signin_pick": "random",
        "signin_data": {"message": "一切随缘~"},
        "success_keywords": ["window.location.href = 'showup.php'"],
    },
    # ---- 朱雀（zhuque.in）：首页取 x-csrf-token，POST JSON 释放技能（就是它的签到）----
    "zhuque.in": {
        "engine": "http",
        "method": "GET",
        "path": "/",
        "signin_path": "/api/gaming/fireGenshinCharacterMagic",
        "signin_method": "POST",
        "signin_token_patterns": {"csrf": "<meta name=\"x-csrf-token\" content=\"([^\"]+)\""},
        "signin_headers": {"x-csrf-token": "{csrf}",
                           "Content-Type": "application/json; charset=utf-8"},
        "signin_json_raw": "{\"all\": 1, \"resetModal\": \"true\"}",
        "success_keywords": ["\"status\":200", "FIRE_GENSHIN_CHARACTER_MAGIC_SUCCESS"],
    },
    # ---- 高清杜比（hddolby.com）：NexusPHP 签到页，已签到时首页显示「鲸币 (签到已得N)」 ----
    "hddolby.com": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "success_keywords": ["签到成功", "签到完成", "领取成功"],
        "already_keywords": ["签到已得", "今日已签到", "已经签到", "请不要重复签到"],
    },
    # ---- SUNNYPT（sunnypt.top）：站点自带签到页 /user/attendance（Next.js + JSON API） ----
    # 头像菜单里的「每日签到」只是跳这个页面，不用再点菜单；
    # 已签到时页面上是「今日签到已完成 … 已连续签到 N 天」，按钮变灰的「今日已签到」。
    "sunnypt.top": {
        # 2026-10-28 迁移到 CloakBrowser（engine=cloak 实测真签成功）：
        # 站点无反自动化门槛，自建 Browserless 已无预设用户；cloak 引擎自带
        # dom_click 两段式点击，签到卡片异步加载由轮询预算覆盖。
        "engine": "cloak",
        "method": "GET",
        "path": "/user/attendance",
        "submit_method": "dom_click",
        "submit_selector": "button, [role='button'], a",
        # 未签到时按钮是「立即签到」，已签到时是灰的「今日已签到」——用完整的四个字更精准
        "submit_text": "立即签到",
        # Cookie 失效时站点会 302 到 /auth/sign-in?shouldClearAuth=true：
        # core 的 login_page() 已能识别，会直接报「Cookie 可能失效」而不是误报签到失败
        # 站点是 Next.js + WebSocket 推状态：点完页面要等接口/推送回来才变成「今日签到已完成」，
        # 所以判定靠轮询等状态词出现。
        # 不能用「点到就算成功」：已签到时按钮是灰的「今日已签到」，照样能被点到，
        # 于是天天误报签到成功。这里改成看页面状态：
        # 点之前就有「今日已签到」= 已签到（引擎先判状态再点）；
        # 点之后出现「今日签到已完成 / 已连续签到 N 天」= 本次签到成功。
        "success_keywords": ["本次获得", "签到成功"],
        # 日历图例里也有裸「已签到」，不能据此认定今天已签到。
        "already_keywords": ["今日已签到", "今日签到已完成", "今天已签到"],
        "strict_status_keywords": True,
    },
    # ---- 海棠PT（htpt.cc）：/attendance.php 的「每日签到」日历，点按钮签到 ----
    # 已签到时页面直接写「今日已签到 ✓（连续 N 天，今日获得 X 魔力值）」，没有表单；
    # 未签到时是按钮，按文字点。最近动向在 userdetails.php（id 从 Cookie 的 c_secure_uid 取）。
    # ---- 自由农场（0ff.cc / pt.0ff.cc）：滑块接口直连，签到逻辑在 sites/farm.py ----
    "0ff.cc": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "already_keywords": ["今日已签到", "已签到"],
        "success_keywords": ["签到成功", "已簽到"],
    },
    "pt.0ff.cc": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "already_keywords": ["今日已签到", "已签到"],
        "success_keywords": ["签到成功", "已簽到"],
    },
    "htpt.cc": {
        # 签到交给 sites/htpt.py：GET 签到页 → POST action=sign → 回读确认。
        # 实测这条路直接可用（表单无验证码），不必再开浏览器。
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "activity_path": "/userdetails.php",
        "already_keywords": ["今日已签到", "今日已经签到", "今天已签到"],
        "success_keywords": ["签到成功", "簽到成功"],
        "bonus": r"(今日获得\s*[\d,]+\s*魔力值)",
    },
    # ---- 憨憨（hhanclub.net）：日历式签到页，点「签到」后当天按钮变成「已领取」 ----
    # 页面是 Vue 渲染的日历，签到要点击完成；判定不认日历里过去几天的「已领取」，
    # 只认签到后的提示语（签到成功 / 领取成功），奖励取「本次签到获得 N 个憨豆」。
    "hhanclub.net": {
        "engine": "function",
        "mode": "open_page",
        "path": "/attendance.php",
        "submit_selector": "button, a[role='button'], div[role='button'], input[type='submit']",
        "submit_method": "dom_click",
        "submit_text": "签到",
        "success_keywords": ["签到成功", "簽到成功", "领取成功", "簽到完成", "签到完成"],
        # 点完按钮才做判定，页面上「本次签到获得…」就代表今天已经领到了
        "already_keywords": ["今日已签到", "今日已经签到", "今天已签到", "今日已領取",
                              "本次签到获得", "已连续签到"],
        "bonus": r"(本次签到获得\s*[\d,]+\s*个?憨豆)",
    },
    # ---- BTSCHOOL（btschool.club，学校）：签到按钮直接挂在首页，没有独立签到页 ----
    # 实测 /attendance.php、/signin.php、/checkin.php 等全是 404，首页上是一个「每日签到」
    # 提交按钮，所以直接 POST 首页即可；判定沿用默认正则（今天已签到 / 签到成功），
    # 「您获得 N 点魔力值」这类奖励是每天不同的，由 extract_bonus 每次从页面文本抓取，不写死。
    "btschool.club": {
        "engine": "http",
        "method": "GET",
        # 和 thsrite 的站点模块同一套判断：
        # 首页没有「每日签到」入口 = 今天已签；有就访问 addbonus，入口消失即签到成功。
        "path": "/index.php",
        "absent_keywords": ["每日签到"],
        "absent_status": "already",
        "signin_path": "/index.php?action=addbonus",
        "signin_absent_status": "success",
        "success_keywords": ["签到成功", "领取成功"],
    },
    # ---- 观众（audiences.me）：Cloudflare 整页挑战 + 签到表单内嵌 Turnstile ----
    # 访客访问任意页都会先出「Just a moment」整页挑战（FlareSolverr 原生可解），
    # 登录后的 attendance.php 表单里还有 Turnstile（同萝莉）。走 FlareSolverr 会话：
    # 预热时整页挑战自动解掉、cf_clearance 留在会话浏览器，再 tabs_till_verify 解
    # 表单内的组件并同会话提交。原 Function+auto 路线作为备注保留：如需回退，在
    # 自定义规则里写 {"engine":"function","mode":"cloudflare","submit_method":"auto"}。
    "audiences.me": {
        "engine": "flaresolverr",
        "path": "/attendance.php",
        "submit_text": "签到",
        # 官方 FlareSolverr 的浏览器被该站 sitekey 指纹拒绝（Verifying 死循环），
        # Browserless 也被 600010 拒绝；TRAWL（Camoufox 反检测 Firefox，FlareSolverr
        # 兼容 /v1）2026-10-27 实测通过：solveEmbeddedCaptchas 解内嵌组件 → 回调提交。
        # 萝莉 / OurBits 继续走全局配置的原生 FlareSolverr。
        # 服务地址不入库（私有部署域名属于敏感信息）：在自定义规则里给本站配
        # {"flare_url":"https://<你的 TRAWL 地址>/"} 指向 FlareSolverr 兼容服务。
        "flare_url": "",
        # Tab 次数由签到页 HTML 按焦点序自动推算（2026-10-26 已用真实浏览器
        # Tab 游走核对：推算 50 = 实测 50，无需钉值）。页面改版导致偏差时，用
        # scripts/ptsitesignin_tabwalk.mjs 校准后以 flare_tabs 钉住。
        "success_keywords": ["签到成功", "簽到成功", "成功签到", "签到完成", "领取成功"],
        "already_keywords": ["今日已签到", "今天已经签到", "您今天已经签到", "已经签到",
                             "已签到", "今日已领取", "已连续签到", "明日再来", "明天再来"],
    },
    # ---- OurBits（ourbits.club）：Cloudflare 整页挑战 + NexusPHP attendance ----
    # 访客访问任意页先出「Just a moment」整页挑战（实测 FlareSolverr 原生解掉），
    # 登录后按萝莉同款会话流程处理签到表单（如开 Turnstile 则自动求解）。
    "ourbits.club": {
        "engine": "flaresolverr",
        "path": "/attendance.php",
        "submit_text": "签到",
    },
    # ---- 未内置的站点默认走 HTTP 直连（无需浏览器交互）----
    # GET 打开签到页即完成签到，判定文案取自站点实际页面：
    #   签到成功 -> 「签到成功」；已签到 -> 「您今天已经签到过了」
    # already 只保留上述「已签到」原文案：签到成功页往往同时带「已签到 N 天」「您已连续
    # 签到 N 天」之类的状态行，一旦把裸「已签到」写进 already，新签到就会被误判成
    # 「今日已签到」。
    # 页面内嵌 Turnstile 的站点 HTTP 拿不到结果，会自动回退到 Browserless（见 judge_http）。
}


@dataclass(frozen=True)
class SignResult:
    status: str   # success / already / failed
    message: str
    challenge: str = ""   # "" / cloudflare / captcha：HTTP 失败时提示该站可改用浏览器引擎
    # 网络层失败（读超时 / 连接中断）时置 True：请求是否真的执行过未知，调用方可以补一次
    # 只读回查，避免把「已签到但响应丢失」误报成失败。
    transient: bool = False
    # 签到路径不存在（HTTP 404 / 405）时置 True：调用方可以换签到入口再试
    missing_path: bool = False


# ------------------------------------------------------------------ 公共工具

def normalize_host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.").rstrip(".")


def target_url(base_url: str, path: str, route_fragment: Optional[str] = None) -> str:
    target = urllib.parse.urljoin(base_url.rstrip("/") + "/", str(path).lstrip("/"))
    return target.split("#", 1)[0] + (route_fragment or "")


def site_state_key(site: Mapping[str, Any]) -> str:
    site_id = str(site.get("id") or "").strip()
    if site_id:
        return "id:" + site_id
    host = urllib.parse.urlparse(str(site.get("url") or "")).hostname
    return "host:" + host.lower() if host else ""


def parse_rules(raw: Any) -> Dict[str, Dict[str, Any]]:
    """解析自定义规则；键为域名或站点 ID。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as error:
        raise ValueError(f"站点规则必须是有效 JSON：{error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("站点规则必须是以域名或站点 ID 为键的 JSON 对象")
    result: Dict[str, Dict[str, Any]] = {}
    for key, rule in value.items():
        if isinstance(key, str) and isinstance(rule, Mapping):
            result[key.lower().strip()] = dict(rule)
    return result


def preset_for_host(host: str) -> Optional[Dict[str, Any]]:
    """按域名取内置预设：先精确匹配，再按「以 .<预设域名> 结尾」兜底。

    站点常用 `pt.` / `www.` / `bbs.` 前缀（例如学校是 `pt.btschool.club`，预设键写的是
    `btschool.club`），这样不必为每个子域各写一条；`open.cd` 的子域也一并覆盖。
    """
    preset = DEFAULT_RULES.get(host)
    if preset is not None:
        return preset
    for key, value in DEFAULT_RULES.items():
        if host.endswith("." + key):
            return value
    return None


def resolve_rule(site: Mapping[str, Any], custom_rules: Mapping[str, Mapping[str, Any]],
                 default_engine: str = "http") -> Dict[str, Any]:
    host = normalize_host(str(site.get("url") or ""))
    custom = (custom_rules.get(str(site.get("id"))) or custom_rules.get(host)
              or custom_rules.get("www." + host) or {})
    preset = preset_for_host(host)
    rule = dict(preset or {})
    rule.update(custom)
    # 只有用户在自定义规则里显式声明 engine 才算「手工指定」；内置预设声明的 http
    # 仍允许在遇盾 / 验证码时自动回退到浏览器引擎
    user_engine = str(custom.get("engine") or "").strip().lower()
    declared_engine = str(rule.get("engine") or "").strip().lower()
    rule["engine"] = declared_engine or str(default_engine or "http").lower()
    # Old browserless rules keep working, but always use our self-hosted Function backend.
    if rule["engine"] == "browserless":
        rule["engine"] = "function"
    if rule["engine"] not in {"http", "function", "playwright", "flaresolverr", "bql", "fox", "cloak"}:
        raise ValueError("签到引擎只能是 http、function、flaresolverr、bql、fox、cloak 或 playwright（兼容 browserless 别名）")
    rule["auto_fallback"] = not user_engine
    if rule["engine"] in {"http", "playwright", "flaresolverr", "cloak"}:
        rule.setdefault("path", DEFAULT_HTTP_PATH)
        rule.setdefault("success", DEFAULT_SUCCESS_PATTERN)
        rule.setdefault("already", DEFAULT_ALREADY_PATTERN)
    else:
        rule.setdefault("mode", "open_page")
        rule.setdefault("path", "/attendance.php")
        rule.setdefault("submit_selector", "input[type='submit']")
        rule.setdefault("already_keywords", [])
    return validate_rule(rule)


def validate_rule(rule: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(rule)
    if result["engine"] in {"http", "playwright", "flaresolverr", "cloak"}:
        path = str(result.get("path") or "")
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("签到路径必须是以 / 开头的站内路径")
        signin_path = str(result.get("signin_path") or "")
        if signin_path and (not signin_path.startswith("/") or signin_path.startswith("//")):
            raise ValueError("第二段签到地址必须是以 / 开头的站内路径")
        for retry_path in list(result.get("retry_paths") or []):
            item = str(retry_path or "")
            if item and (not item.startswith("/") or item.startswith("//")):
                raise ValueError("retry_paths 里的每个页面都必须是以 / 开头的站内路径")
        patterns = result.get("signin_token_patterns")
        if patterns is not None and not isinstance(patterns, Mapping):
            raise ValueError("signin_token_patterns 必须是 {字段: 正则} 形式的对象")
        if result["engine"] == "flaresolverr":
            tabs = result.get("flare_tabs")
            if tabs is not None and (not isinstance(tabs, int) or isinstance(tabs, bool)
                                     or not 1 <= tabs <= 200):
                raise ValueError("flare_tabs 必须是 1-200 的整数（Tab 按键次数的手工覆盖）")
            prime = str(result.get("flare_prime_path") or "").strip()
            if prime and (not prime.startswith("/") or prime.startswith("//") or "#" in prime):
                raise ValueError("flare_prime_path 必须是以 / 开头的站内路径")
            proxy = urllib.parse.urlsplit(str(result.get("flare_proxy") or "").strip())
            if proxy.scheme and (proxy.scheme not in {"http", "https", "socks5"} or not proxy.hostname):
                raise ValueError("flare_proxy 必须是 http://、https:// 或 socks5:// 地址")
            service = urllib.parse.urlsplit(str(result.get("flare_url") or "").strip())
            if service.scheme and (service.scheme not in {"http", "https"} or not service.hostname):
                raise ValueError("flare_url 必须是 http:// 或 https:// 地址")
        return result
    mode = result.get("mode")
    if mode not in {"open_page", "cloudflare", "image", "trigger_image", "altcha"}:
        raise ValueError("Browserless 签到方式必须为 open_page、cloudflare、image、trigger_image 或 altcha")
    path = str(result.get("path") or "")
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
        raise ValueError("签到路径必须是以 / 开头的站内路径")
    route_fragment = str(result.get("route_fragment") or "")
    if route_fragment and (not route_fragment.startswith("#/") or "\\" in route_fragment):
        raise ValueError("路由片段必须以 #/ 开头")
    if mode in {"image", "trigger_image"}:
        for name in ("captcha_selector", "captcha_input_selector", "submit_selector"):
            if not str(result.get(name) or "").strip():
                raise ValueError("图片验证码规则缺少 " + name)
    if mode == "trigger_image" and not str(result.get("trigger_selector") or "").strip():
        raise ValueError("弹窗图片验证码规则缺少 trigger_selector")
    if mode in {"image", "trigger_image", "altcha"} and str(
            result.get("submit_method") or "").lower() in {"none", "skip"}:
        raise ValueError("图片验证码 / Altcha 站点必须指定提交按钮，不能用 submit_method=none")
    if str(result.get("submit_method") or "").lower() == "turnstile_click" \
            and not str(result.get("submit_selector") or "").strip():
        raise ValueError("Turnstile 点击规则缺少 submit_selector")
    if mode == "altcha":
        for name in ("altcha_checkbox_selector", "altcha_payload_selector", "submit_selector"):
            if not str(result.get(name) or "").strip():
                raise ValueError("Altcha 规则缺少 " + name)
    # cloudflare / turnstile：仅允许自动检测，不允许手工指定 solve 类型
    return result


# 挑战类型 → Browserless 签到方式
FALLBACK_MODES = {"cloudflare": "cloudflare", "captcha": "image"}


def fallback_rule(rule: Mapping[str, Any], challenge: str) -> Dict[str, Any]:
    """由 HTTP 规则派生一份 Browserless 回退规则。

    用于「没有写规则的站点」：HTTP 直连遇 Cloudflare / 验证码时，自动改用浏览器引擎
    再试一次。Cloudflare 由自建浏览器等待与点击；图片验证码尝试常见表单选择器，
    并使用明确配置的 MoviePilot-OCR。原规则的提交与结果配置一并保留。
    """
    fallback: Dict[str, Any] = dict(rule, **{
        "engine": "function",
        "mode": FALLBACK_MODES.get(str(challenge), "open_page"),
        "path": str(rule.get("path") or DEFAULT_HTTP_PATH),
    })
    fallback.setdefault("submit_selector", "input[type='submit'], button, a, [role='button']")
    fallback.setdefault("submit_text", "签到")
    fallback.setdefault("already_keywords", [])
    if fallback["mode"] == "image":
        fallback.setdefault("captcha_selector", "form:has(input[name='imagestring']) img, form:has(input[name='captcha']) img")
        fallback.setdefault("captcha_input_selector", "input[name='imagestring'], input[name='captcha']")
    if rule.get("route_fragment"):
        fallback["route_fragment"] = rule["route_fragment"]
    # 让用户在 HTTP 规则里写的等待参数（如 cloudflare_wait）与点击目标（submit_selector /
    # submit_text，例如 AGSVPT 的「立即签到」）对自动回退同样生效
    for name in ("cloudflare_wait", "wait", "preflight_wait", "submit_selector", "submit_text",
                 "pre_click_selector", "pre_click_text"):
        if rule.get(name) is not None:
            fallback[name] = rule[name]
    return validate_rule(fallback)


# =============================================================== HTTP 引擎

def _is_transient(error: Exception) -> bool:
    """读超时 / 连接重置这类抖动错误：值得加长超时重试一次。"""
    name = type(error).__name__.lower()
    text_value = str(error).lower()
    if "timeout" in name or "timed out" in text_value:
        return True
    return any(key in text_value for key in ("connection aborted", "connection reset",
                                             "remotedisconnected", "remote end closed"))


def http_sign(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
              proxies: Optional[Dict[str, str]] = None) -> SignResult:
    """直接 HTTP 请求签到接口（NexusPHP attendance.php 等）。"""
    import requests

    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return SignResult("failed", "站点地址或 Cookie 为空")
    url = target_url(base_url, str(rule.get("path") or DEFAULT_HTTP_PATH))
    headers = {
        "User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
        "Referer": f"{base_url}/index.php",
        "Cookie": cookie,
        "Accept": "application/json, text/javascript, text/html, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        session = requests.Session()
        session.headers.update(headers)
        method = str(rule.get("method") or "GET").upper()
        # 规则里的 data：签到表单需要提交字段时用（例如首页「每日签到」按钮带 name）
        data = rule.get("data") if isinstance(rule.get("data"), Mapping) else None
        if method == "POST":
            response = session.post(url, data=data, timeout=timeout,
                                    allow_redirects=True, proxies=proxies)
        else:
            response = session.get(url, params=data, timeout=timeout,
                                   allow_redirects=True, proxies=proxies)
    except Exception as error:
        # 读超时 / 连接被重置大多是网络抖动，加长超时再试一次，别让站点整天算失败
        if _is_transient(error):
            try:
                longer = max(15, int(timeout * 1.5))
                session = requests.Session()
                session.headers.update(headers)
                if method == "POST":
                    response = session.post(url, data=data, timeout=longer,
                                            allow_redirects=True, proxies=proxies)
                else:
                    response = session.get(url, params=data, timeout=longer,
                                           allow_redirects=True, proxies=proxies)
            except Exception as retry_error:
                return SignResult("failed", f"请求异常：{retry_error}")
        else:
            return SignResult("failed", f"请求异常：{error}")
    content = response.text or ""
    judged = judge_http(content, rule)
    if judged.challenge:
        return judged
    if response.status_code >= 400:
        return SignResult("failed", f"请求失败（HTTP {response.status_code}）",
                          missing_path=response.status_code in {404, 405})
    return judged


def find_signin_form(html: str, base_url: str,
                     submit_text: str = "") -> Optional[Dict[str, Any]]:
    """从签到页里找签到表单，返回 ``{"action", "method", "data"}``；找不到返回 None。

    NexusPHP 的签到是「点表单按钮」触发的，光 GET 页面只会渲染按钮、不产生签到记录。
    这里优先找提交按钮文字含「签到 / 打卡 / 领取」（或规则的 submit_text）的表单，
    实在没有就退到「带提交按钮且 action 像签到」的那个。
    """
    wanted = str(submit_text or "").strip()
    best: Optional[Dict[str, Any]] = None
    fallback: Optional[Dict[str, Any]] = None
    for raw_attrs, inner in _FORM_RE.findall(html or ""):
        control = _submit_control(inner)
        if not control:
            continue
        attrs = _tag_attrs("<form " + raw_attrs + ">")
        action = same_site_path(base_url, attrs.get("action") or "") or \
            (urllib.parse.urlparse(base_url).path or "/")
        method = (attrs.get("method") or "POST").strip().upper()
        label = re.sub(r"\s+", "", plain_text(inner) + str(control.get("value") or ""))
        fields: Dict[str, str] = {}
        for tag in re.findall(r"<input\b[^>]*>", inner, re.IGNORECASE):
            item = _tag_attrs(tag)
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            kind = (item.get("type") or "text").lower()
            if kind in {"submit", "button", "image", "reset"} and \
                    not any(word in label for word in SIGNIN_LINK_WORDS):
                continue
            fields[name] = str(item.get("value") or "")
        if str(control.get("name") or "").strip():
            fields[str(control["name"])] = str(control.get("value") or "")
        form = {"action": action, "method": "POST" if method != "GET" else "GET",
                "data": fields}
        if wanted and wanted in label:
            return form
        if any(word in label for word in SIGNIN_LINK_WORDS):
            best = best or form
        elif "attendance" in action or "signin" in action or "checkin" in action:
            fallback = fallback or form
    return best or fallback


def http_sign_form(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                   proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """打开签到页、按页面上的表单 POST 提交一次，再用返回页判定。

    找不到表单（或表单是 GET 链）时返回 None，交给上层继续用 GET 结果。
    """
    import requests

    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return None
    path = str(rule.get("path") or DEFAULT_HTTP_PATH)
    page_url = target_url(base_url, path)
    headers = {
        "User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
        "Referer": f"{base_url}/index.php",
        "Cookie": cookie,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    try:
        session = requests.Session()
        session.headers.update(headers)
        page = session.get(page_url, timeout=timeout, allow_redirects=True, proxies=proxies)
        if page.status_code >= 400:
            return None
        form = find_signin_form(page.text or "", base_url, str(rule.get("submit_text") or ""))
        if not form or form["method"] != "POST":
            return None
        post_url = target_url(base_url, form["action"])
        response = session.post(post_url, data=form["data"], timeout=timeout,
                                allow_redirects=True, proxies=proxies)
    except Exception:
        return None
    if response.status_code >= 400:
        return None
    return judge_http(response.text or "", rule)


def turnstile_widget_in(html: str) -> bool:
    """页面是否内嵌 Cloudflare Turnstile 组件（区别于整页「Just a moment」挑战）。"""
    return bool(re.search(TURNSTILE_PATTERN, html or "", re.IGNORECASE))


class _FocusOrderParser:
    """按 Chromium 顺序焦点导航规则枚举 HTML 里的可聚焦元素。

    FlareSolverr v3.5 的原生 Turnstile 解法是「从页面开头按 N 次 Tab 聚焦到
    验证组件的 iframe，再按 Space 勾选」。N 就是 widget 之前的可聚焦元素数 + 1，
    这里从签到页 HTML 静态推算，避免在 FlareSolverr 上盲目试错（每次都烧满超时）。

    可见性只认 HTML 自带的信号（hidden / aria-hidden / 内联 display:none /
    visibility:hidden / 控件 disabled）；类 CSS 的 display:none 静态推导看不到，
    已知会少数几个——这类站点用 scripts/ptsitesignin_tabwalk.mjs 以真实浏览器
    校准后，在预设里用 flare_tabs 钉住实测值。
    """

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                 "meta", "param", "source", "track", "wbr"}
    CONTROL_TAGS = {"a", "area", "button", "input", "select", "textarea",
                    "iframe", "object", "embed", "summary"}
    DISABLED_TAGS = {"button", "input", "select", "textarea", "optgroup", "option", "fieldset"}
    INLINE_HIDDEN_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)

    def __init__(self) -> None:
        self.order = 0
        # (文档序号, tabindex)；只记录能参与 Tab 导航且未被隐藏的元素
        self.focusables: List[tuple] = []
        # Turnstile 容器（cf-turnstile / data-sitekey / 挑战 iframe / 隐藏 token 输入框）
        # 首次出现的文档序号；脚本标签不算——它在 <head> 里，位置不代表组件位置。
        self.marker = 0
        self._stack: List[tuple] = []  # (tag, 是否处于 hidden 祖先内)
        self._at_hidden = False

    # —— 极简容错解析：只需 start/end 标签与属性，不构建完整 DOM
    def feed(self, html: str) -> None:
        for match in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
                                 html or ""):
            closing, tag, raw_attrs = match.group(1), match.group(2).lower(), match.group(3) or ""
            attrs: Dict[str, str] = {}
            for name, _, value in re.findall(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)(\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?", raw_attrs):
                attrs[name.lower()] = value.strip("\"'")
            if closing:
                self._close(tag)
            else:
                hidden_self = self._open(tag, attrs)
                if tag not in self.VOID_TAGS:
                    # 隐藏容器的子元素同样不可聚焦，栈上要带着隐藏状态
                    self._stack.append((tag, self._at_hidden or hidden_self))
                    self._at_hidden = any(hidden for _, hidden in self._stack)

    def _close(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                self._at_hidden = any(hidden for _, hidden in self._stack)
                return

    def _open(self, tag: str, attrs: Dict[str, str]) -> bool:
        """记录一个开标签；返回它自身是否处于不可聚焦的隐藏状态。"""
        self.order += 1
        hidden_self = ("hidden" in attrs or attrs.get("aria-hidden") == "true"
                       or self.INLINE_HIDDEN_RE.search(attrs.get("style") or ""))
        if self.marker == 0 and self._is_marker(tag, attrs):
            self.marker = self.order
        if hidden_self or self._at_hidden:
            return True
        if "disabled" in attrs and tag in self.DISABLED_TAGS:
            return
        tabindex_raw = attrs.get("tabindex")
        if tabindex_raw is not None:
            try:
                tabindex = int(tabindex_raw.strip() or "0")
            except ValueError:
                tabindex = 0
            if tabindex < 0:
                return  # tabindex=-1 只能脚本聚焦，不参与 Tab 导航
        else:
            tabindex = 0
        if not self._focusable(tag, attrs, tabindex_raw is not None):
            return
        self.focusables.append((self.order, tabindex))

    @staticmethod
    def _is_marker(tag: str, attrs: Dict[str, str]) -> bool:
        if tag == "script":
            return False
        if tag == "input":
            if (attrs.get("type") or "").lower() == "hidden":
                return attrs.get("name", "").strip().lower() == "cf-turnstile-response"
            return "data-sitekey" in attrs
        classes = " " + (attrs.get("class") or "") + " "
        if " cf-turnstile " in classes or "data-sitekey" in attrs:
            return True
        return tag == "iframe" and "challenges.cloudflare.com" in (attrs.get("src") or "")

    @staticmethod
    def _focusable(tag: str, attrs: Dict[str, str], has_tabindex: bool) -> bool:
        if tag == "input":
            return (attrs.get("type") or "text").lower() != "hidden"
        if tag in {"a", "area"}:
            return bool(attrs.get("href")) or has_tabindex
        if tag == "summary":
            return True
        if tag in {"audio", "video"}:
            return "controls" in attrs or has_tabindex
        if tag in {"iframe", "object", "embed", "button", "select", "textarea"}:
            return True
        if (attrs.get("contenteditable") or "").lower() == "true":
            return True
        return has_tabindex


def tabs_till_turnstile(html: str) -> int:
    """推算 FlareSolverr ``tabs_till_verify``：widget iframe 前的可聚焦元素数 + 1。

    正整数即 Tab 次数；0 表示页面里找不到 Turnstile 组件（或解析失败），
    调用方此时应要求用户用 ``flare_tabs`` 手工指定。静态推导看不到 JS 后插入
    与 CSS 隐藏的元素，页面结构复杂时可手工覆盖。
    """
    parser = _FocusOrderParser()
    try:
        parser.feed(html or "")
    except Exception:
        return 0
    if not parser.marker:
        return 0
    # 正 tabindex 的元素无论在 DOM 哪个位置都排在焦点序最前面
    positives = sorted((entry for entry in parser.focusables if entry[1] > 0),
                       key=lambda entry: (entry[1], entry[0]))
    zeros_before = [entry for entry in parser.focusables
                    if entry[1] <= 0 and entry[0] < parser.marker]
    return len(positives) + len(zeros_before) + 1


def random_form_fields(html: str) -> Dict[str, str]:
    """从签到页表单里取隐藏字段 + 随机挑一个选项/按钮。

    对应两类「答题式」签到：

    * 52pt / CHDBits 的 bakatest：隐藏字段 ``questionid`` + 若干 ``choice[]``，随便选一个提交；
    * U2 的 showup：隐藏字段 ``req`` / ``hash`` / ``form`` + 若干提交按钮（选项），随机点一个。
    """
    hidden: Dict[str, str] = {}
    options: List[tuple] = []
    for tag in re.findall(r"<input\b[^>]*>", html or "", re.IGNORECASE):
        attrs = _tag_attrs(tag)
        name = str(attrs.get("name") or "").strip()
        if not name:
            continue
        kind = str(attrs.get("type") or "text").lower()
        value = str(attrs.get("value") or "")
        if kind in {"submit", "button", "image"}:
            options.append((name, value))
        elif kind in {"radio", "checkbox"} or "choice" in name:
            options.append((name, value))
        elif kind == "hidden":
            hidden[name] = value
    if not options:
        return hidden
    name, value = random.choice(options)
    hidden[name] = value
    return hidden


def http_two_step(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                  proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """第二段签到：先读首页，按规则抓 token / 直接访问签到地址，再判定。

    对应两类站点（都是 thsrite 的「站点自动签到」里验证过的流程）：

    * TTG 这种：首页 JS 里有 ``signed_timestamp`` / ``signed_token``，要 POST 到 ``/signed.php``；
    * 学校这种：首页有「每日签到」入口，访问 ``/index.php?action=addbonus`` 后入口消失即签到成功。

    规则字段：``signin_path``（第二段地址）、``signin_method``（默认有参数就 POST）、
    ``signin_data``（固定字段）、``signin_token_patterns``（{字段: 正则}，从首页抓）、
    ``signin_absent_status``（第二段页面上 absent_keywords 消失时判什么状态）。
    """
    import requests

    signin_path = str(rule.get("signin_path") or "").strip()
    if not signin_path:
        return None
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return None
    ua = str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT
    first_url = target_url(base_url, str(rule.get("path") or DEFAULT_HTTP_PATH))
    first = fetch_page(first_url, cookie, timeout, proxies, ua)
    if not first:
        return None

    data: Dict[str, str] = {}
    static = rule.get("signin_data")
    if isinstance(static, Mapping):
        data.update({str(key): str(value) for key, value in static.items()})
    patterns = rule.get("signin_token_patterns")
    if isinstance(patterns, Mapping):
        for name, pattern in patterns.items():
            match = re.search(str(pattern), first)
            if not match:
                # 抓不到 token 就别盲目提交，交给上层继续报原始失败原因
                return None
            data[str(name)] = match.group(1) if match.groups() else match.group(0)
    picks: Optional[Dict[str, str]] = None
    if str(rule.get("signin_pick") or "").lower() == "random":
        picks = random_form_fields(first)
        data.update(picks)

    method = str(rule.get("signin_method") or ("POST" if data else "GET")).upper()
    url = target_url(base_url, signin_path)
    headers = {"User-Agent": ua, "Cookie": cookie, "Referer": first_url,
               "Accept": "text/html,application/json,*/*;q=0.8"}
    extra_headers = rule.get("signin_headers")
    if isinstance(extra_headers, Mapping):
        for name, value in extra_headers.items():
            try:
                headers[str(name)] = str(value).format(**{k: v for k, v in data.items()
                                                         if isinstance(k, str) and k.isidentifier()})
            except (KeyError, IndexError, ValueError):
                headers[str(name)] = str(value)
    raw_body = rule.get("signin_json_raw")
    try:
        session = requests.Session()
        session.headers.update(headers)
        if raw_body:
            response = session.post(url, data=str(raw_body).encode("utf-8"),
                                    timeout=timeout, allow_redirects=True, proxies=proxies)
        elif method == "POST":
            response = session.post(url, data=data or None, timeout=timeout,
                                    allow_redirects=True, proxies=proxies)
        else:
            response = session.get(url, params=data or None, timeout=timeout,
                                   allow_redirects=True, proxies=proxies)
    except Exception:
        return None
    if response.status_code >= 400:
        return None

    # 第二段页面上「签到入口消失」也是一种成功信号（学校：addbonus 后首页不再有「每日签到」）
    probe = dict(rule)
    if rule.get("signin_absent_status"):
        probe["absent_status"] = rule["signin_absent_status"]
    judged = judge_http(response.text or "", probe)
    if judged.status in {"success", "already"}:
        return SignResult(judged.status, f"{judged.message}（{signin_path}）")
    # 答题式签到（52PT / CHDBits / U2）：答错不算签到成功，换一个选项再来，最多试 3 次
    if picks and str(rule.get("signin_pick") or "").lower() == "random":
        for _ in range(2):
            page = fetch_page(first_url, cookie, timeout, proxies, ua)
            if not page:
                break
            retry_data = {key: value for key, value in data.items() if key not in picks}
            retry_picks = random_form_fields(page)
            if not retry_picks:
                break
            retry_data.update(retry_picks)
            try:
                session = requests.Session()
                session.headers.update(headers)
                retry = session.post(url, data=retry_data or None, timeout=timeout,
                                     allow_redirects=True, proxies=proxies)
            except Exception:
                break
            if retry.status_code >= 400:
                break
            judged = judge_http(retry.text or "", probe)
            if judged.status in {"success", "already"}:
                return SignResult(judged.status, f"{judged.message}（{signin_path}）")
    return None


def http_confirm(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                 proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """回读签到页确认结果：有的站点提交后返回 302 / 空白页，只有再打开一次才看得到状态。"""
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return None
    path = str(rule.get("path") or DEFAULT_HTTP_PATH)
    html = fetch_page(target_url(base_url, path), cookie, timeout, proxies,
                      str(site.get("ua") or ""))
    if not html:
        return None
    return judge_http(html, rule)


# 常见签到入口路径：默认路径 404 时按顺序再试，仍然找不到就从站点首页自动发现
FALLBACK_SIGNIN_PATHS = ("/attendance.php", "/signin.php", "/sign_in.php", "/checkin.php",
                         "/plugin_sign-in.php", "/attendance.php?action=sign")
# 首页里「签到」入口的文字（链接文字或提交按钮文字）
SIGNIN_LINK_WORDS = ("签到", "簽到", "打卡")
_SIGNIN_LINK_RE = re.compile(
    r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_SUBMIT_RE = re.compile(r"<(?:input|button)\b[^>]*>", re.IGNORECASE)
_BUTTON_TEXT_RE = re.compile(r"<button\b[^>]*>(.*?)</button>", re.IGNORECASE | re.DOTALL)


def fetch_page(url: str, cookie: str, timeout: int = 30,
               proxies: Optional[Dict[str, str]] = None, user_agent: str = "") -> str:
    """GET 一个页面并返回 HTML（失败返回空串），用于发现签到入口。"""
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent or DEFAULT_USER_AGENT,
                            "Cookie": str(cookie or ""),
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception:
        return ""
    if response.status_code >= 400:
        return ""
    return response.text or ""


def same_site_path(base_url: str, href: str) -> str:
    """把页面里的链接转成同站路径；跨域、锚点、javascript: 一律丢弃。"""
    value = str(href or "").strip()
    if not value or value.startswith(("#", "javascript:", "mailto:", "data:")):
        return ""
    target = urllib.parse.urljoin(base_url.rstrip("/") + "/", value)
    parsed, base = urllib.parse.urlparse(target), urllib.parse.urlparse(base_url)
    if not parsed.hostname or parsed.hostname != base.hostname:
        return ""
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path if path.startswith("/") else ""


def discover_signin_path(html: str, base_url: str) -> str:
    """从首页 HTML 里找文字含「签到 / 打卡」的链接，返回同站路径（找不到返回空串）。

    链接文字里的空白先全部去掉，兼容「每 日 签 到」这类带空格的排版。
    """
    for href, label in _SIGNIN_LINK_RE.findall(html or ""):
        text = re.sub(r"\s+", "", plain_text(label))
        if not any(word in text for word in SIGNIN_LINK_WORDS):
            continue
        path = same_site_path(base_url, href)
        if path:
            return path
    return ""


def _submit_control(inner: str) -> Dict[str, str]:
    """表单里的提交控件属性（``<input type=submit>`` / ``<button>``），没有就返回空字典。"""
    for tag in _SUBMIT_RE.findall(inner or ""):
        attributes = _tag_attrs(tag)
        kind = (attributes.get("type") or "").lower()
        if tag.lower().startswith("<button"):
            if kind in {"", "submit"}:
                return attributes
        elif kind == "submit":
            return attributes
    return {}


def discover_signin_target(html: str, base_url: str) -> Dict[str, Any]:
    """找签到入口，返回 ``{"path", "method", "data"}``（找不到返回空字典）。

    先看表单：首页上直接挂一个「每日签到」提交按钮的站点（BTSCHOOL / 学校 就是这种）没有独立
    签到页，必须按表单的 action / method 提交，按钮带 name 时还要把它的字段一起发过去；
    表单里找不到再退回「找链接」。
    """
    body = html or ""
    for raw_attrs, inner in _FORM_RE.findall(body):
        control = _submit_control(inner)
        if not control:
            continue
        label = re.sub(r"\s+", "", plain_text(inner) + str(control.get("value") or ""))
        if not any(word in label for word in SIGNIN_LINK_WORDS):
            continue
        attrs = _tag_attrs("<form " + raw_attrs + ">")
        path = same_site_path(base_url, attrs.get("action") or "") or \
            (urllib.parse.urlparse(base_url).path or "/")
        method = (attrs.get("method") or "POST").strip().upper()
        target: Dict[str, Any] = {"path": path, "method": "GET" if method == "GET" else "POST"}
        name = str(control.get("name") or "").strip()
        if name:
            value = control.get("value")
            if value is None:
                match = _BUTTON_TEXT_RE.search(inner)
                value = re.sub(r"\s+", "", plain_text(match.group(1))) if match else ""
            target["data"] = {name: str(value or "")}
        return target
    found = discover_signin_path(body, base_url)
    return {"path": found, "method": "GET"} if found else {}


# ---------------------------------------------------------------- 本地浏览器引擎
# 用 MoviePilot 自带的 PlaywrightHelper 在**本机**开浏览器签到：
# 使用 MoviePilot 所在环境的出口；不创建远程浏览器会话。
_PAGE_CLICK_JS = """
(args) => {
  const [selector, text] = args;
  const nodes = Array.from(document.querySelectorAll(
    selector || "button, a, [role='button'], [role='menuitem'], input[type='submit'], li, span"));
  const hits = nodes
    .filter((node) => {
      const label = (node.value || node.textContent || "").trim();
      const visible = node.offsetParent !== null || node.getClientRects().length > 0;
      return visible && !node.disabled && node.getAttribute('aria-disabled') !== 'true' && label.includes(text);
    })
    .sort((left, right) => {
      const a = (left.value || left.textContent || "").trim().length;
      const b = (right.value || right.textContent || "").trim().length;
      return a - b;
    });
  if (!hits.length) return false;
  const target = hits[0];
  target.scrollIntoView({block: "center"});
  target.click();
  return true;
}
"""

# 两段式点击的第一步：只「标记 + 滚动到位」，真正的点击交给 page.click（真实鼠标事件，
# 抬不起 hover/冒泡的站点点得更像人）；真实点击被遮罩挡住时再退回 _PAGE_CLICK_JS 的 JS 点击。
_MARK_CLICK_JS = """
(args) => {
  const [selector, text] = args;
  const nodes = Array.from(document.querySelectorAll(
    selector || "input[type='submit'], button, a, [role='button']"));
  const wanted = (text || "").trim();
  const hits = nodes.filter((node) => {
    if (node.disabled) return false;
    const label = ((node.value || node.textContent || "") + " " + (node.getAttribute("aria-label") || "")
                   + " " + (node.getAttribute("title") || "")).trim();
    const visible = node.offsetParent !== null || node.getClientRects().length > 0;
    return visible && (!wanted || label.includes(wanted));
  });
  if (!hits.length) return false;
  hits.sort((left, right) => {
    const a = (left.value || left.textContent || "").trim();
    const b = (right.value || right.textContent || "").trim();
    return Number(b === wanted) - Number(a === wanted) || a.length - b.length;
  });
  hits[0].setAttribute("data-ptsignin-target", "1");
  hits[0].scrollIntoView({block: "center"});
  return true;
}
"""

_CLICK_MARKED_SELECTOR = '[data-ptsignin-target="1"]'


_CHALLENGE_PATTERN = re.compile(r"(just a moment|cf-chl|_cf_chl_opt|chl_page|"
                                r"正在进行安全检测|安全检测能力由|雷池\s*WAF|SafeLine|"
                                r"正在验证您是否是真人|verify you are human|checking your browser)",
                                re.IGNORECASE)
_CF_FRAME_RE = "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']"


def _page_has_challenge(html: str) -> bool:
    """页面是否还停在 Cloudflare / 雷池 的检测页。"""
    return bool(html) and bool(_CHALLENGE_PATTERN.search(html))


def _click_cf_checkbox(page: Any) -> bool:
    """试着点 Cloudflare/Turnstile 的「验证您是真人」勾选框（在 iframe 里）。"""
    try:
        frames = getattr(page, "frames", None)
        targets = [frame for frame in (frames or []) if "challenges.cloudflare.com" in
                   str(getattr(frame, "url", "") or "")]
        for frame in targets:
            for selector in ("input[type='checkbox']", "label", "#challenge-stage"):
                try:
                    frame.click(selector, timeout=1500)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    # 退化方案：用 frame_locator 点 iframe 内的勾选框
    try:
        page.frame_locator(_CF_FRAME_RE).locator("input[type='checkbox']").first.click(timeout=1500)
        return True
    except Exception:
        return False


def _wait_challenge_clear(page: Any, budget_ms: int) -> str:
    """等检测页自己放行，返回最后的页面 HTML。"""
    import time as _time

    deadline = _time.monotonic() + max(3, budget_ms / 1000)
    html = ""
    while _time.monotonic() < deadline:
        try:
            html = page.content() or ""
        except Exception:
            html = ""
        if not _page_has_challenge(html):
            return html
        _click_cf_checkbox(page)
        try:
            page.wait_for_timeout(1500)
        except Exception:
            _time.sleep(1.5)
    return html


def _load_playwright_helper():
    """取宿主浏览器助手：V3 优先稳定 SDK，V2 回退旧路径；都没有返回 None。"""
    try:  # MoviePilot V3
        from app.sdk.browser import PlaywrightHelper
        return PlaywrightHelper
    except ImportError:
        pass
    try:  # V3 当前浏览器适配器路径
        from app.adapters.network.browser import PlaywrightHelper
        return PlaywrightHelper
    except ImportError:
        pass
    try:  # MoviePilot V2
        from app.helper.browser import PlaywrightHelper
        return PlaywrightHelper
    except ImportError:
        return None


def playwright_available() -> bool:
    """MoviePilot 是否带 PlaywrightHelper（官方镜像默认带）。"""
    return _load_playwright_helper() is not None


def cookie_list_for_context(cookie: str, url: str) -> List[Dict[str, Any]]:
    """把 "a=1; b=2" 转成 Playwright ``context.add_cookies`` 需要的列表。

    猪猪这类双盾站点在验证过程中会下发新的 cf_clearance / sl_* Cookie：只有把账号 Cookie
    写进浏览器 Cookie 存储，这些新 Cookie 才能随后续请求一起发出去；用 MP 的固定
    ``extra_http_headers`` 注入则永远只带旧值，验证会一直循环。
    """
    from urllib.parse import urlparse

    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return []
    items: List[Dict[str, Any]] = []
    for chunk in str(cookie or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if not name:
            continue
        items.append({"name": name, "value": value.strip(), "url": f"{parsed.scheme}://{parsed.netloc}/"})
    return items


def playwright_paths(rule: Mapping[str, Any]) -> List[str]:
    """本机浏览器要依次尝试的页面：主 path 在前，retry_paths 依次兜底。

    有的站点（猪猪）首页只有「[签到已得…]」链接，真正的签到表单在 attendance.php，
    所以允许规则再给几个备用页面，找不到按钮就换下一个。
    """
    paths: List[str] = []
    primary = str(rule.get("path") or DEFAULT_HTTP_PATH).strip()
    candidates = [primary] + list(rule.get("retry_paths") or [])
    for value in candidates:
        item = str(value or "").strip()
        if item and item not in paths:
            paths.append(item)
    return paths or [DEFAULT_HTTP_PATH]


def playwright_sign(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 90,
                    proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """用本机浏览器签到：打开页面 → 预检已签到 → 按文字点按钮 → 读结果判定。

    返回 None 表示这台机器没有 PlaywrightHelper（交回上层用其它引擎）。
    """
    PlaywrightHelper = _load_playwright_helper()
    if PlaywrightHelper is None:
        return None
    # 老版本 MoviePilot 的 PlaywrightHelper 只有 get_page_source，没有 action：
    # 这种情况返回 None，让上层退回 Browserless，而不是硬报错
    if not hasattr(PlaywrightHelper, "action"):
        return None

    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url:
        return None
    paths = playwright_paths(rule)
    selector = str(rule.get("submit_selector") or "")
    submit_text = str(rule.get("submit_text") or "")
    wait_ms = int(rule.get("wait") or 4000)

    # CF / 雷池 都靠页面自己的 JS 跑完才放行：勾选式还要点一下 iframe 里的勾选框。
    # 预算取规则里的 cloudflare_wait（秒），没写就按 20 秒。
    try:
        challenge_budget = int(float(rule.get("cloudflare_wait") or 20)) * 1000
    except (TypeError, ValueError):
        challenge_budget = 20000
    # 挑战页本身也要能点勾选，给足超时
    wait_ms = max(wait_ms, 4000)

    cookie_mode = str(rule.get("cookie_mode") or "header").lower()

    def callback(page: Any) -> Dict[str, Any]:
        # 有的站点（猪猪）必须把 Cookie 写进浏览器存储：验证过程中新下发的
        # cf_clearance / sl_* 才能随后续请求发出，否则验证会一直循环。
        if cookie_mode == "browser" and cookie:
            try:
                items = cookie_list_for_context(cookie, base_url)
                if items:
                    page.context.add_cookies(items)
            except Exception:
                pass
        # MoviePilot 的 action() 里是 page.goto(url) 等 load：Cloudflare/雷池盾页永远等不到
        # load，30 秒必超时（日志里的 Page.goto: Timeout 30000ms 就是这个）。所以让它先打开
        # about:blank，真正的导航放在这里，只等 domcontentloaded，再自己轮询盾页是否放行。
        tried: List[str] = []
        last: Dict[str, Any] = {}
        for path in paths:
            url = target_url(base_url, path)
            tried.append(path)
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=max(15000, int(timeout * 1000)))
            except Exception:
                pass
            try:
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
            before = _wait_challenge_clear(page, challenge_budget)
            after = before
            clicked = False
            stuck = ""
            judged_before = classify(before, rule)
            if before and _page_has_challenge(before):
                # 还停在雷池 / Cloudflare 检测页：这时候页面上根本没有签到按钮，
                # 直接去点只会得到「没找到按钮」的误导性结论，换下一个备用页面。
                last = {"before": before, "after": before, "clicked": False,
                        "path": path, "stuck": "challenge"}
                continue
            if judged_before.status == "failed" and "二级验证" in str(judged_before.message or ""):
                # 异地登录验证页：需要人工/2FA，点按钮没用
                return {"before": before, "after": before, "clicked": False,
                        "path": path, "stuck": "second_factor",
                        "message": judged_before.message}
            if judged_before.status not in {"success", "already"}:
                until = time.monotonic() + min(
                    timeout, max(1, int(rule.get("button_wait") or 15000) / 1000))
                # DOMContentLoaded can precede a SPA's authenticated API response and button rendering.
                while time.monotonic() < until:
                    after = page.content() or ""
                    if login_page(after):
                        last = {"before": before, "after": after, "clicked": False,
                                "path": path, "login": True}
                        break
                    if classify(after, rule).status in {"success", "already"}:
                        break
                    if _page_has_challenge(after):
                        after = _wait_challenge_clear(page, challenge_budget)
                        if _page_has_challenge(after):
                            stuck = "challenge"
                            break
                        continue
                    # 两段式点击：先标记，再用真实鼠标事件点（被遮罩挡住时退回 JS 点击）
                    try:
                        marked = bool(page.evaluate(_MARK_CLICK_JS, [selector, submit_text]))
                    except Exception:
                        marked = False
                    if not marked:
                        page.wait_for_timeout(250)
                        continue
                    try:
                        page.click(_CLICK_MARKED_SELECTOR, timeout=10000)
                        clicked = True
                    except Exception:
                        try:
                            clicked = bool(page.evaluate(_PAGE_CLICK_JS, [selector, submit_text]))
                        except Exception:
                            clicked = False
                    try:
                        page.wait_for_timeout(max(1000, wait_ms))
                    except Exception:
                        pass
                    # 点完签到可能被踢到雷池 / Cloudflare 检测页，再等一次放行
                    after = _wait_challenge_clear(page, challenge_budget)
                    if classify(after, rule).status in {"success", "already"}:
                        break
                    if after and _page_has_challenge(after):
                        stuck = "challenge"
                        break
            else:
                after = before
                try:
                    after = page.content() or after
                except Exception:
                    pass
            last = {"before": before, "after": after, "clicked": clicked,
                    "path": path, "stuck": stuck}
            if last.get("login"):
                break
            if clicked or stuck == "challenge" or classify(after, rule).status in {"success", "already"}:
                break
            # 这一页既没点成也不像已签到：换下一个备用页面
        last["tried"] = tried
        last["paths"] = paths
        return last

    try:
        outcome = PlaywrightHelper().action(url="about:blank", callback=callback,
                                            cookies=None if cookie_mode == "browser" else cookie,
                                            ua=str(site.get("ua") or "").strip() or None,
                                            proxies=dict(proxies) if proxies else None,
                                            headless=True,
                                            timeout=max(60, int(timeout)))
    except Exception as error:
        return SignResult("failed", f"本地浏览器签到异常：{error}")
    if not outcome:
        return SignResult("failed", "本地浏览器没拿到页面（检查 MoviePilot 的浏览器依赖）")

    after = str(outcome.get("after") or "")
    tried = "、".join(str(item) for item in (outcome.get("tried") or []))
    if outcome.get("login") or login_page(after):
        return SignResult("failed", login_required_message("签到页跳转到了登录页"))
    if outcome.get("stuck") == "second_factor":
        return SignResult("failed", str(outcome.get("message")
                                        or "站点要求二级验证（异地登录验证）"))
    if outcome.get("challenge") or outcome.get("stuck") == "challenge" or cloudflare_pending(after):
        return SignResult("failed",
                          f"本机浏览器未通过 Cloudflare / WAF 验证（已试：{tried or '默认页面'}）："
                          f"检查 Cookie 里是否带 cf_clearance / sl_*，或把 cloudflare_wait 调大")
    judged = classify(after, rule)
    if judged.status in {"success", "already"}:
        prefix = "" if outcome.get("clicked") else "（页面已是签到完成状态）"
        return SignResult(judged.status, f"{judged.message}{prefix}")
    if not outcome.get("clicked"):
        return SignResult("failed",
                          f"本地浏览器没找到可点的签到按钮（已试：{tried or '默认页面'}；"
                          f"可在这条规则的 retry_paths 里补页面）")
    return judged


# ---------------------------------------------------------------- 站点模块
# 规则表达不了的站点（图片验证码、答题选海报、API Token）放在插件目录的 sites/ 下，
# 一个站一个文件。这里按文件路径加载，不依赖包名，测试里单独加载 core.py 也能用。
SITE_MODULE_DIR = Path(__file__).resolve().parent / "sites"
_SITE_MODULES: Optional[List[Any]] = None
_SITES_API: Any = None


def _sites_api() -> Any:
    """加载 sites/__init__.py 里的 SignContext 等辅助（避免相对导入）。"""
    global _SITES_API
    if _SITES_API is None:
        path = SITE_MODULE_DIR / "__init__.py"
        if not path.is_file():
            _SITES_API = False
        else:
            spec = importlib.util.spec_from_file_location("ptsitesignin_sites_api", path)
            module = importlib.util.module_from_spec(spec) if spec else None
            if module is None or spec.loader is None:
                _SITES_API = False
            else:
                spec.loader.exec_module(module)
                _SITES_API = module
    return _SITES_API or None


def load_site_modules(refresh: bool = False) -> List[Any]:
    """扫描 sites/ 目录，返回带 site_url + signin 的模块类（带缓存）。"""
    global _SITE_MODULES
    if _SITE_MODULES is not None and not refresh:
        return _SITE_MODULES
    modules: List[Any] = []
    if SITE_MODULE_DIR.is_dir():
        for path in sorted(SITE_MODULE_DIR.glob("*.py")):
            if path.name.startswith("__"):
                continue
            name = "ptsitesignin_site_" + re.sub(r"\W+", "_", path.stem)
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                continue
            for obj in vars(module).values():
                if isinstance(obj, type) and hasattr(obj, "site_url") and hasattr(obj, "signin"):
                    modules.append(obj)
                    break
    _SITE_MODULES = modules
    return modules


def site_module_for(url: str) -> Optional[Any]:
    """按站点 URL 找匹配的模块类。"""
    api = _sites_api()
    if api is None:
        return None
    for module in load_site_modules():
        matcher = getattr(module, "match", None)
        if callable(matcher):
            try:
                if matcher(url):
                    return module
            except Exception:
                continue
            continue
        if api.domain_matches(url, str(getattr(module, "site_url", "") or "")):
            return module
    return None


def run_site_module(site: Mapping[str, Any], timeout: int = 30,
                    proxies: Optional[Dict[str, str]] = None,
                    logger: Any = None, ocr_url: str = "") -> Optional[SignResult]:
    """有匹配的站点模块就执行它，返回结果；没有模块返回 None，交给规则引擎。"""
    api = _sites_api()
    if api is None:
        return None
    module = site_module_for(str(site.get("url") or ""))
    if module is None:
        return None
    if not str(site.get("cookie") or "").strip() and not str(site.get("token") or "").strip():
        return SignResult("failed", "站点没有 Cookie，无法签到")
    context = api.SignContext(site, timeout=timeout, proxies=proxies, logger=logger, ocr_url=ocr_url)
    try:
        ok, message = api.sign_via_module(module, site, context)
    finally:
        context.close()
    if ok:
        return SignResult("already" if "已签到" in message else "success", message or "签到成功")
    return SignResult("failed", message or "签到失败")


def http_sign_auto(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                   proxies: Optional[Dict[str, str]] = None) -> SignResult:
    """HTTP 签到：默认路径 404 / 405 时自动换签到入口，并把找法写进失败原因。

    站点用的签到路径五花八门（`/attendance.php`、`/signin.php`、自定义路由……）。以前路径写死，
    碰上没有 `/attendance.php` 的站点只会得到一句「请求失败（HTTP 404）」。现在的顺序是：
    先试规则里的 path → 从站点首页「签到」链接自动发现 → 再试几个常见路径。
    """
    result = http_sign(site, rule, timeout=timeout, proxies=proxies)
    if result.status in {"success", "already"}:
        return result
    # 页面能打开但没判定出结果：很多站点签到是「点表单按钮」触发的，
    # 这里按页面上的表单 POST 一次，再回读签到页确认，避免「签上了却报失败」。
    if not result.missing_path and not result.challenge:
        # 有的站点签到在第二个地址上（TTG 的 signed.php、学校的 addbonus），先按规则走完
        two_step = http_two_step(site, rule, timeout=timeout, proxies=proxies)
        if two_step is not None:
            return two_step
        # 第二段不认输时回读第一页再判一次：学校 addbonus 后首页入口消失、海胆之家首页变「已经打卡」
        if str(rule.get("signin_path") or "").strip():
            time.sleep(1.5)
            confirmed = http_confirm(site, rule, timeout=timeout, proxies=proxies)
            if confirmed is not None and confirmed.status in {"success", "already"}:
                return SignResult(confirmed.status, f"{confirmed.message}（回读签到页确认）")
        posted = http_sign_form(site, rule, timeout=timeout, proxies=proxies)
        if posted is not None and posted.status in {"success", "already"}:
            return SignResult(posted.status, f"{posted.message}（按页面表单 POST 提交）")
        confirmed = http_confirm(site, rule, timeout=timeout, proxies=proxies)
        if confirmed is not None and confirmed.status in {"success", "already"}:
            return SignResult(confirmed.status, f"{confirmed.message}（回读签到页确认）")
        if posted is not None and posted.status == "failed":
            return posted
    if not result.missing_path:
        return result
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    user_agent = str(site.get("ua") or "").strip()
    tried = [str(rule.get("path") or DEFAULT_HTTP_PATH)]
    # 首页里可能挂着「每日签到」表单（学校 BTSCHOOL 就是这种）或一个签到链接
    target = discover_signin_target(
        fetch_page(target_url(base_url, DEFAULT_INDEX_PATH), cookie, timeout, proxies, user_agent),
        base_url)
    attempts: List[Dict[str, Any]] = []
    discovered = str(target.get("path") or "")
    if discovered and discovered not in tried:
        attempt = dict(rule, path=discovered)
        if target.get("method"):
            attempt["method"] = str(target["method"])
        if target.get("data"):
            attempt["data"] = dict(target["data"])
        attempts.append(attempt)
    for path in FALLBACK_SIGNIN_PATHS:
        if path in tried or any(item.get("path") == path for item in attempts):
            continue
        attempts.append(dict(rule, path=path))
    if not attempts:
        return result
    for attempt in attempts[:4]:
        path = str(attempt.get("path") or "")
        tried.append(path)
        retry = http_sign(site, attempt, timeout=timeout, proxies=proxies)
        if not retry.missing_path:
            if retry.status in {"success", "already"}:
                return SignResult(retry.status, f"{retry.message}（签到入口：{path}）")
            return retry
    return SignResult("failed", f"没找到可用的签到入口（HTTP 404）：已试过 {'、'.join(tried)}；"
                                "可在自定义规则里用 path 指定该站的签到路径；若该站本身没有签到功能，"
                                "把它从「签到站点」移到「登录站点」即可（插件仍会登录并读最近动向）")


def plain_text(content: str) -> str:
    """去掉注释、script/style 与 HTML 标签并压缩空白，便于按文案匹配。

    最后再解一次 HTML 实体：站点源码里常把 `<` 写成 `&lt;`（例如「2026-10-17 03:52:21
    ( &lt; 1分前 )」），不解码的话结果里会原样带出 `&lt;`。
    """
    text = re.sub(r"<!--.*?-->", " ", content, flags=re.DOTALL)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # 标签名必须以字母开头，避免把站点相对时间里的 <（如 " < 1分前"）当成标签吞掉
    text = re.sub(r"</?[a-zA-Z][^>]*>", " ", text)
    text = _html_unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_reward_sentence(content: str) -> str:
    """只认「获得 X 点魔力值」这种明确的奖励句子。

    静默成功的站点（返回的是首页）侧栏也有「魔力值 [使用]: 242,141.0」这类统计数字，
    用宽泛的 extract_bonus 会把它们当成奖励，所以这里只匹配带「获得 / 签到已得」的说法。
    """
    match = re.search(REWARD_SENTENCE_PATTERN, str(content or ""), re.IGNORECASE)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def extract_bonus(content: str, rule: Mapping[str, Any]) -> str:
    """提取签到奖励描述（如「获得 22 粒爆米花」）。规则里的 bonus 字段可覆盖默认模板。"""
    custom = str(rule.get("bonus") or "").strip()
    patterns = (custom,) if custom else BONUS_PATTERNS
    for candidate in (content, plain_text(content)):
        for pattern in patterns:
            try:
                found = re.search(pattern, candidate, re.IGNORECASE)
            except re.error:
                continue
            if not found:
                continue
            values = [group for group in found.groups() if group]
            text = values[-1] if values else found.group(0)
            # 顺手去掉尾部的逗号 / 顿号（站点文案常写成「魔力值 65,」这类）
            return re.sub(r"\s+", " ", text).strip().strip(",，、;；")
    return ""


def message_bonus(message: Any) -> str:
    """从结果文案里取出奖励部分：「签到成功（获得 20 粒爆米花）」→「获得 20 粒爆米花」。

    详情页「结果」列用它显示实际到手的东西（魔力值 / 爆米花 / 奖励），比整句文案清爽。
    """
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    match = re.search(r"[（(]([^（()）]{1,80})[)）]\s*$", text)
    if not match:
        return ""
    bonus = match.group(1).strip().strip(",，、;；:：")
    if re.fullmatch(r"[\d,]+(?:\.\d+)?", bonus):
        return "获得 " + bonus
    return bonus


def snippet_around(text: str, keyword: str, width: int = 60) -> str:
    """截取 keyword 前后的一小段正文，便于定位页面实际内容。"""
    index = text.find(keyword)
    if index < 0:
        return ""
    start = max(0, index - width)
    end = min(len(text), index + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def page_hint(content: str) -> str:
    """截取响应摘要，便于判断站点到底返回了什么页面。"""
    title = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    body = plain_text(content)
    parts = []
    if title:
        parts.append("标题「" + re.sub(r"\s+", " ", title.group(1)).strip()[:60] + "」")
    if body:
        parts.append("正文「" + body[:120] + "」")
        # 签到卡片常在页面中后段，正文前 120 字全是站点头尾时再补一段签到相关片段
        for word in ("每日签到", "人机验证", "签到成功", "已签到", "签到"):
            if body.find(word) > 100:
                parts.append("片段「" + snippet_around(body, word) + "」")
                break
    return "；".join(parts) or "响应为空"


def judge_http(content: str, rule: Mapping[str, Any]) -> SignResult:
    # 完整页面匹配：Audiences 等站的判定文案位于页面较后位置，按 20000 字符截断会漏掉
    text = content[:200000]
    if re.search(CLOUDFLARE_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "站点返回 Cloudflare 验证页", challenge="cloudflare")
    if re.search(WAF_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "站点返回雷池 WAF 安全检测页", challenge="cloudflare")
    if re.search(COOKIE_INVALID_PATTERN, text, re.IGNORECASE) and "logout" not in text.lower():
        return SignResult("failed", "Cookie 无效或已过期")
    already = str(rule.get("already") or DEFAULT_ALREADY_PATTERN)
    success = str(rule.get("success") or DEFAULT_SUCCESS_PATTERN)
    # 判定文案只在去掉标签 / script/style 的可见正文上匹配，避免命中页面 JS 里的
    # 签到分支字符串。
    visible = plain_text(text)
    # 先判「签到成功」再判「今日已签到」：多数 NexusPHP 的成功页同时含「签到成功」与
    # 「已签到 N 天」，顺序反了就会把刚完成的签到误报成「今日已签到」。
    if re.search(success, visible, re.IGNORECASE):
        bonus = extract_bonus(text, rule)
        return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")
    if re.search(already, visible, re.IGNORECASE):
        bonus = extract_bonus(text, rule)
        return SignResult("already", f"今日已签到（{bonus}）" if bonus else "今日已签到")
    # 规则里写了关键词时（浏览器引擎用的那套），HTTP 判定也认，免得同一个预设两种引擎结论不同
    lower_visible = visible.lower()
    if any(str(word).lower() in lower_visible for word in (rule.get("success_keywords") or [])):
        bonus = extract_bonus(text, rule)
        return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")
    if any(str(word).lower() in lower_visible for word in (rule.get("already_keywords") or [])):
        bonus = extract_bonus(text, rule)
        return SignResult("already", f"今日已签到（{bonus}）" if bonus else "今日已签到")
    # 页面内嵌 Cloudflare Turnstile 人机验证（观众 Audiences 这类 Vue 签到页）：签到的判定
    # 文案由 JS 在验证回调里生成，HTTP 直连拿不到，只能交给浏览器引擎。放在成功 / 已签到
    # 之后判断，避免「已签到」页仍带 Turnstile 组件时被判成还需要验证。
    if re.search(TURNSTILE_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "页面需要完成 Cloudflare 人机验证（Turnstile）",
                          challenge="cloudflare")
    # NexusPHP JSON 返回
    try:
        data = json.loads(text.strip())
    except (ValueError, TypeError):
        data = None
    if isinstance(data, Mapping):
        state = str(data.get("state") or data.get("status") or "").lower()
        message = str(data.get("sign") or data.get("msg") or data.get("message") or "签到成功")
        if state in {"success", "ok", "true", "1"}:
            bonus = extract_bonus(text, rule)
            return SignResult("success", f"{message}（{bonus}）" if bonus else message)
        if state in {"false", "fail", "failed", "error"}:
            return SignResult("failed", str(data.get("msg") or data.get("message")
                                            or data.get("error") or "站点返回签到失败"))
    # 少数站点把结果只放在 <script> 里的 state 字段；此时才回退到原始文本。
    # 上面已经先排除了可见的「已签到」，不会把已签到页面脚本里的成功分支误判为新签到。
    if re.search(success, text, re.IGNORECASE):
        bonus = extract_bonus(text, rule)
        return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")
    if re.search(CAPTCHA_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "页面要求填写验证码", challenge="captcha")
    # 静默成功：有的站点签到完不给任何提示，只是把「每日签到」入口拿掉（BTSCHOOL/学校 就是这种）。
    # 规则里写了 absent_keywords 时，返回页面里这些文字全都不见了 → 视为已完成；奖励仍从整页
    # 原始 HTML 里抓（站点可能把它塞在 JS alert / 隐藏节点里，plain_text 看不到）。
    # 注意要在**原始 HTML** 里找这些文字：签到入口是 `<input value="每日签到">`，去标签后的
    # 可见正文里本来就没有这个词，用 visible 判断会把「入口还在」误当成「入口已消失」。
    absent = [str(item).strip() for item in (rule.get("absent_keywords") or [])
              if str(item).strip()]
    control = control_text(text)
    if absent and not login_page(content) and not cloudflare_pending(content) \
            and not any(word in visible or word in control for word in absent):
        status = "success" if str(rule.get("absent_status") or "").lower() == "success" else "already"
        prefix = "签到成功" if status == "success" else "今日已签到"
        bonus = extract_reward_sentence(text)
        return SignResult(status, f"{prefix}（{bonus}）" if bonus else prefix)
    # 兜底：主正则没命中，但页面出现了「已领取 / 本次签到 / 签到获得」这类签到后的状态字样，
    # 且页面上已经没有「立即签到」之类的按钮，就按已签到处理，别让正常签到报失败。
    if not login_page(content) and not cloudflare_pending(content) \
            and not re.search(SIGNIN_ACTION_PATTERN, visible):
        for match in re.finditer(STATE_HINT_PATTERN, visible, re.IGNORECASE):
            window = visible[max(0, match.start() - 50):match.start()]
            if re.search(RULE_CONTEXT_PATTERN, window):
                continue
            bonus = extract_reward_sentence(text) or extract_bonus(text, rule)
            note = f"页面出现「{match.group(1)}」"
            return SignResult("already",
                              f"今日已签到（{note}，{bonus}）" if bonus else f"今日已签到（{note}）")
    return SignResult("failed", f"未匹配到签到成功标志；{page_hint(text)}")


# ======================================================== 站点登录 + 最近动向

# 很多 NexusPHP 站点的站点 Cookie 会过期：签到失败其实是「没登录」。配置了账号后，
# 插件会在签到前用账号密码真实登录一次、换回新的 Cookie，签到引擎随后用这份新 Cookie；
# 登录后再打开用户页抓「最近动向」，作为该账号真实登录 / 活跃的证据。
DEFAULT_LOGIN_PATH = "/login.php"
DEFAULT_LOGIN_ACTION = "/takelogin.php"
DEFAULT_INDEX_PATH = "/index.php"
DEFAULT_USERDETAILS_PATH = "/userdetails.php"
DEFAULT_USERNAME_FIELD = "username"
DEFAULT_PASSWORD_FIELD = "password"
# 登录成功的特征：页面出现登出 / 退出入口，或拿到了 NexusPHP 的登录 Cookie
LOGIN_SUCCESS_PATTERN = r"(logout\.php|退出|登出|注销|我的控制面板|我的面板)"
# 登录失败特征：明确提示账号密码错误
LOGIN_FAILURE_PATTERN = (r"(用户名或密码错误|用户名或密碼錯誤|账号或密码错误|密碼錯誤"
                         r"|登录失败|登入失敗|invalid (?:username|password)|incorrect password)")
# 登录页要求人机校验时的特征
LOGIN_CAPTCHA_PATTERN = r"(captcha|imagestring|验证码|驗證碼|turnstile)"
# 用户页里「最近动向」相关关键词（含繁体、各站的不同叫法）
ACTIVITY_KEYWORDS = ("最近动向", "最近動向", "最新动向", "最新動向", "最近活动", "最近活動",
                     "最近访问", "最近訪問", "上次访问", "上次訪問", "本次访问", "上次签到",
                     "上次簽到", "上次活动", "上次活動", "上次登入", "上次登录", "上次造访",
                     "上次造訪", "最后访问", "最後訪問", "最后活动", "最後活動", "最后上线",
                     "最後上線", "最后活跃", "最後活躍", "最后造访", "最後造訪", "最近登入",
                     "最近登錄", "最近上线", "最近上線", "最近活跃", "最近活躍", "最后在线",
                     "最後在線")
# 兜底判定：主正则都没命中时，再看一遍页面里有没有「签到后的状态」字样。
# 很多站点用「已领取 / 本次签到 / 签到获得 / 已签」这类写法，或者把文案放在说明里，
# 所以下面会排除「规则 / 说明 / 首次 / 若 / 如果」附近的命中，避免把规则当成结果。
STATE_HINT_PATTERN = (r"(已签到|已簽到|今日已签|今天已签|已领取|已領取|领取成功|領取成功|"
                      r"签到成功|簽到成功|成功签到|签到完成|本次签到|签到获得|已签到过|"
                      r"不要重复签到|无需重复签到|already|signed in)")
RULE_CONTEXT_PATTERN = r"(规则|規則|說明|说明|首次|连续签到第|若|如果|条件|條件|奖励说明|奖励规则)"
# 页面上还留着「立即签到」这类按钮，说明这次没签上，不能按已签到处理
SIGNIN_ACTION_PATTERN = r"(立即签到|点击签到|點擊簽到|去签到|前往签到|马上签到|开始签到)"

# 站点常见的时间格式（NexusPHP 多为 YYYY-MM-DD HH:MM:SS）
DATETIME_PATTERN = r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)"
# 没有关键词命中时用的严格时间格式：必须带时分。裸日期（2026-09-18）在种子列表、
# 公告、排行榜里到处都是，拿它当「最近动向」只会给出一个看起来很旧的假值。
ACTIVITY_TIME_PATTERN = r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)"
# 关键词后面允许多长的间隔才算「紧跟着时间」：多数站点写成「上次访问：2026-…」，
# 但有的主题把标签和值分在两行、中间夹着图标 / 说明文字，60 字会漏掉，放宽到 160。
# 多个关键词都命中时只取「离时间最近」的那个，所以放宽窗口不会把旁边字段（加入日期之类）
# 的时间算成这个关键词的值。
ACTIVITY_WINDOW = 160
# 站点常常在时间后面再补一段相对时间，例如「最近动向：2026-10-16 23:44:21 ( < 1分前 )」
RELATIVE_TIME_PATTERN = r"\s*([（(]\s*[^（()）]{0,16}?前\s*[)）])"
_DATETIME_RE = re.compile(DATETIME_PATTERN)
_ACTIVITY_TIME_RE = re.compile(ACTIVITY_TIME_PATTERN)
_RELATIVE_TIME_RE = re.compile(RELATIVE_TIME_PATTERN)

_FORM_BLOCK_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_UID_RE = re.compile(r"userdetails\.php\?id=(\d+)", re.IGNORECASE)
# 「获得 87 点魔力值」这类明确的奖励句子（静默成功时只用它，避免误抓侧栏统计数字）
REWARD_SENTENCE_PATTERN = (rf"((?:{BONUS_VERBS}|签到已得|簽到已得)\s*{BONUS_AMOUNT}"
                           rf"\s*(?:{BONUS_COUNTERS})?\s*(?:{BONUS_UNITS}))")


@dataclass(frozen=True)
class LoginResult:
    status: str     # success / failed
    message: str
    cookie: str = ""


@dataclass(frozen=True)
class ActivityResult:
    status: str     # success / failed
    message: str
    last_seen: str = ""
    snippet: str = ""
    source: str = ""   # 从哪个候选页读到的，便于排查「最近动向」读不到的问题


def parse_accounts(raw: Any) -> Dict[str, Dict[str, Any]]:
    """解析站点账号配置；键为域名或站点 ID，值为账号对象。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as error:
        raise ValueError(f"站点账号必须是有效 JSON：{error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("站点账号必须是以域名或站点 ID 为键的 JSON 对象")
    result: Dict[str, Dict[str, Any]] = {}
    for key, account in value.items():
        if not isinstance(key, str) or not isinstance(account, Mapping):
            continue
        username = str(account.get("username") or "").strip()
        password = str(account.get("password") or "")
        if not username or not password:
            continue
        result[key.lower().strip()] = dict(account)
    return result


def account_for(site: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]]
                ) -> Optional[Dict[str, Any]]:
    """按站点 ID 或域名取出该站的登录账号，未配置时返回 None。"""
    if not accounts:
        return None
    site_id = str(site.get("id") or "").strip()
    if site_id and site_id in accounts:
        return dict(accounts[site_id])
    host = normalize_host(str(site.get("url") or ""))
    for key in (host, "www." + host):
        if key and key in accounts:
            return dict(accounts[key])
    return None


# MoviePilot「站点管理」(SiteOper) 保存的是 Cookie / ApiKey / Token / UA / 代理等，没有账号
# 密码输入框；账号密码可以写在站点管理的「附加信息」(note) 里（V3 是 JSON 对象，V2 是字符
# 串），插件会自动解析；同时兼容站点记录里以后可能出现的 username / password 字段。
SITE_ACCOUNT_FIELDS = ("username", "user", "account", "password", "passwd", "pwd",
                       "login_path", "login_action", "username_field", "password_field",
                       "fields", "activity_path")


def note_object(note: Any) -> Dict[str, Any]:
    """把站点管理的「附加信息」解析成字典（V3 本来就是对象，V2 是 JSON 字符串）。"""
    if isinstance(note, Mapping):
        return dict(note)
    if isinstance(note, str):
        raw = note.strip()
        if raw.startswith("{"):
            try:
                value = json.loads(raw)
            except ValueError:
                return {}
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def account_from_site(site: Mapping[str, Any]) -> Dict[str, Any]:
    """从 MoviePilot「站点管理」的站点记录里取登录凭证与登录选项（取不到时返回空字典）。"""
    account: Dict[str, Any] = {}
    for source in (note_object(site.get("note")), site):
        if not isinstance(source, Mapping):
            continue
        for key in SITE_ACCOUNT_FIELDS:
            value = source.get(key)
            if value not in (None, "") and key not in account:
                account[key] = value
    username = str(account.get("username") or account.get("user")
                   or account.get("account") or "").strip()
    password = str(account.get("password") or account.get("passwd") or account.get("pwd") or "")
    if username:
        account["username"] = username
    if password:
        account["password"] = password
    return account


def account_ready(account: Optional[Mapping[str, Any]]) -> bool:
    """凭证是否够做一次账号密码登录。"""
    if not account:
        return False
    return bool(str(account.get("username") or "").strip() and str(account.get("password") or ""))


def merge_accounts(*accounts: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """按优先级合并登录凭证，后面的覆盖前面的。"""
    merged: Dict[str, Any] = {}
    for account in accounts:
        if account:
            merged.update(dict(account))
    return merged


def _tag_attrs(tag: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        value = match.group(2)
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        attrs[match.group(1).lower()] = value
    return attrs


def find_login_form(html: str) -> Dict[str, Any]:
    """从登录页里挑出含密码框的表单，返回 {"action": str, "fields": {...}}。"""
    for raw_attrs, body in _FORM_BLOCK_RE.findall(html or ""):
        attrs = _tag_attrs("<form " + raw_attrs + ">")
        fields: Dict[str, str] = {}
        has_password = False
        for input_tag in _INPUT_RE.findall(body):
            field = _tag_attrs(input_tag)
            name = field.get("name")
            if not name:
                continue
            kind = (field.get("type") or "text").lower()
            if kind == "password":
                has_password = True
                continue
            if kind in {"submit", "button", "image", "file", "reset", "checkbox", "radio"}:
                continue
            fields[name] = field.get("value", "")
        if has_password:
            return {"action": attrs.get("action") or "", "fields": fields}
    return {"action": "", "fields": {}}


def cookie_header(session: Any) -> str:
    """把 requests.Session 的 Cookie 拼成请求头字符串。"""
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in session.cookies)


def _has_password_input(html: str) -> bool:
    return bool(re.search(r"<input\b[^>]+type\s*=\s*[\"']?password", html or "", re.IGNORECASE))


def login(site: Mapping[str, Any], account: Mapping[str, Any], timeout: int = 30,
          proxies: Optional[Dict[str, str]] = None) -> LoginResult:
    """用账号密码真实登录站点，换回新的 Cookie（NexusPHP takelogin.php 通用）。"""
    base_url = str(site.get("url") or "").rstrip("/")
    if not base_url:
        return LoginResult("failed", "站点地址为空")
    username = str(account.get("username") or "").strip()
    password = str(account.get("password") or "")
    if not username or not password:
        return LoginResult("failed", "未配置账号或密码")
    import requests
    login_path = str(account.get("login_path") or DEFAULT_LOGIN_PATH)
    login_action = str(account.get("login_action") or DEFAULT_LOGIN_ACTION)
    username_field = str(account.get("username_field") or DEFAULT_USERNAME_FIELD)
    password_field = str(account.get("password_field") or DEFAULT_PASSWORD_FIELD)
    headers = {"User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    session = requests.Session()
    try:
        page = session.get(target_url(base_url, login_path), headers=headers,
                           timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception as error:
        return LoginResult("failed", f"打开登录页失败：{error}")
    if page.status_code >= 400:
        return LoginResult("failed", f"打开登录页失败（HTTP {page.status_code}）")
    form = find_login_form(page.text or "")
    action = str(form.get("action") or "").strip()
    post_url = urllib.parse.urljoin(base_url + "/", action or login_action.lstrip("/"))
    data: Dict[str, str] = dict(form.get("fields") or {})
    extra = account.get("fields")
    if isinstance(extra, Mapping):
        for name, value in extra.items():
            data[str(name)] = str(value)
    data[username_field] = username
    data[password_field] = password
    try:
        response = session.post(post_url, data=data,
                                headers={**headers, "Referer": page.url,
                                         "Content-Type": "application/x-www-form-urlencoded"},
                                timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception as error:
        return LoginResult("failed", f"提交登录表单失败：{error}")
    if response.status_code >= 400:
        return LoginResult("failed", f"登录请求失败（HTTP {response.status_code}）")
    content = response.text or ""
    text = plain_text(content)
    cookie = cookie_header(session)
    if re.search(LOGIN_FAILURE_PATTERN, text, re.IGNORECASE):
        return LoginResult("failed", "登录被拒绝：账号或密码错误")
    if ("c_secure_uid" in cookie or "c_secure_pass" in cookie
            or re.search(LOGIN_SUCCESS_PATTERN, text, re.IGNORECASE)
            or (not _has_password_input(content) and "login.php" not in str(response.url))):
        return LoginResult("success", "登录成功", cookie)
    if re.search(LOGIN_CAPTCHA_PATTERN, content, re.IGNORECASE):
        return LoginResult("failed", "登录页要求验证码 / 人机校验，请改用站点 Cookie")
    return LoginResult("failed", "登录后仍停留在登录页：账号密码错误或站点要求两步验证")


def _normalize_time(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[/.]", "-", str(value or "").strip()))


def relative_suffix(text: str, start: int) -> str:
    """取出紧跟在时间后面的相对时间括号，例如「( < 1分前 )」。"""
    match = _RELATIVE_TIME_RE.match(text[start:start + 32])
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def is_zero_time(value: str) -> bool:
    """判断时间是不是被站点补成整零点（``2026-05-06 00:00:00``）。

    这种值多半是「只有日期」的字段（注册日期、加入日期、模板默认值）被补了零，
    不是真的最近动向；皇后那条假时间就是它。
    """
    moments = str(value or "").strip()
    return moments.endswith("00:00:00") or moments.endswith("00:00")


def activity_value(text: str, match: Any) -> str:
    """时间 + 站点自带的相对时间，例如「2026-10-16 23:44:21 ( < 1分前 )」。"""
    value = _normalize_time(match.group(1))
    suffix = relative_suffix(text, match.end())
    return f"{value} {suffix}" if suffix else value


def extract_activity(html: str) -> Dict[str, str]:
    """从用户页里抓「最近动向」片段与其中的时间，返回 keyword / snippet / last_seen。

    两个防呆点（都踩过坑）：

    1. 关键词后面必须紧跟时间才算命中。以前关键词附近没时间时会退回「全页第一个日期」，
       而种子列表 / 公告里的裸日期到处都是，于是「最近动向」被填成 `2026-09-18` 这种假值；
    2. 没有关键词命中就直接返回空，宁可页面提示「未读取到」（可以配 activity_path），
       也不把公告 / 活动通知里的日期当成最近动向；整零点时间同样不算——那是被补零的
       日期字段（注册日期、模板默认值）。
    """
    text = plain_text(html or "")
    best: Any = None
    for order, keyword in enumerate(ACTIVITY_KEYWORDS):
        # 同一个关键词可能出现在多处（导航、图例、说明里也会有），逐处检查
        start = 0
        while True:
            index = text.find(keyword, start)
            if index < 0:
                break
            start = index + 1
            match = _DATETIME_RE.search(text, index, index + ACTIVITY_WINDOW)
            if match is None:
                continue
            # 站点自己写的「(40分钟前)」最可信：新闻、公告里的裸日期没有这个后缀
            has_suffix = 0 if relative_suffix(text, match.end()) else 1
            # 被补成 00:00:00 的日期不是真实动向（皇后把注册日期补零当成了最近动向），
            # 这种命中直接跳过，宁可这页返回空，交给下一个候选页
            if is_zero_time(match.group(1)):
                continue
            gap = match.start() - (index + len(keyword))
            candidate = (has_suffix, gap, order, index, keyword, match)
            if best is None or candidate[:3] < best[:3]:
                best = candidate
    if best is not None:
        _, _, _, index, keyword, match = best
        snippet = text[max(0, index - 12):index + 140].strip()
        return {"keyword": keyword, "snippet": snippet,
                "last_seen": activity_value(text, match)}
    # 没有关键词命中时**不再退化成「全页第一个时间」**：站点首页的公告 / 活動通知里到处是
    # 日期——皇后的 index.php 就有「北京时间2026-05-06 00:00:00 至 2026-05-10 23:59:59
    # 开启全站Free」，被当成最近动向显示成了 2026-05-06 00:00:00。宁可返回空，让调用方
    # 换下一个候选页（userdetails.php 上有真正的「最近動向」）。
    return {}


def _uid_from_jwt(value: str) -> str:
    """从 JWT 或 `base64(payload.签名)` 这类凭据里取 user_id（烧包的 c_secure_pass 是后者）。"""
    token = str(value or '').strip()
    if not token:
        return ''
    candidates = [token.split('.')[0], token]
    for candidate in candidates:
        candidate = candidate.strip()
        if len(candidate) < 8:
            continue
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(candidate + '=' * (-len(candidate) % 4)).decode('utf-8', 'ignore')
            except Exception:
                continue
            match = re.search(r'"?user_?id"?\s*[:=]\s*"?(\d+)', decoded, re.IGNORECASE)
            if match:
                return match.group(1)
            if decoded.strip().isdigit():
                return decoded.strip()
    return ''


def cookie_uid(cookie: str) -> str:
    """从 Cookie 里解出自己的用户 ID（NexusPHP 惯例：``c_secure_uid`` 是 base64 过的 uid）。

    自定义主题的站点首页常常不挂 ``userdetails.php?id=`` 链接，这时插件拿不到 uid、也就打不开
    用户页（「最近动向」自然读不到）——所以优先用 Cookie 里这份 uid。
    Cookie 名不全统一，``c_secure_uid`` 之外还认 ``uid`` / ``c_uid`` / ``user_id`` 等；
    值可能是 base64（带或不带 ``=``、可能被 URL 编码一到两次），也可能直接是数字。
    """
    text = str(cookie or "")
    for name in ("c_secure_uid", "uid", "c_uid", "userid", "user_id", "c_secure_pass"):
        match = re.search(rf"(?:^|;\s*){name}=([^;]+)", text, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        # 可能被 URL 编码一到两次（%3D / %253D）
        for _ in range(3):
            unquoted = urllib.parse.unquote(value)
            if unquoted == value:
                break
            value = unquoted
        if value.isdigit():
            return value
        # 烧包的 c_secure_pass 是 "base64(payload).签名" 的 JWT 变体，payload 里有 user_id
        jwt_uid = _uid_from_jwt(value)
        if jwt_uid:
            return jwt_uid
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(value + "=" * (-len(value) % 4)).decode("utf-8", "ignore")
            except Exception:
                continue
            decoded = decoded.strip()
            if decoded.isdigit():
                return decoded
            jwt_uid = _uid_from_jwt(decoded)
            if jwt_uid:
                return jwt_uid
    return ""


def read_activity(site: Mapping[str, Any], cookie: str, timeout: int = 30,
                  proxies: Optional[Dict[str, str]] = None,
                  path: Optional[str] = None,
                  paths: Optional[Sequence[str]] = None) -> ActivityResult:
    """用登录态 Cookie 打开用户页，读取站点展示的「最近动向」。

    path 优先，paths 是调用方按站点规则补充的候选页面（比如签到页 / 校验页）。
    """
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(cookie or "").strip()
    if not base_url or not cookie:
        return ActivityResult("failed", "站点地址或 Cookie 为空")
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
                            "Cookie": cookie,
                            "Referer": base_url + DEFAULT_INDEX_PATH,
                            "Accept": "text/html,application/xhtml+xml,xml;q=0.9,*/*;q=0.8"})
    try:
        index = session.get(target_url(base_url, DEFAULT_INDEX_PATH),
                            timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception as error:
        return ActivityResult("failed", f"读取站点首页失败：{error}")
    if index.status_code >= 400:
        return ActivityResult("failed", f"读取站点首页失败（HTTP {index.status_code}）")
    index_html = index.text or ""
    if login_page(index_html):
        return ActivityResult("failed", "登录态无效：站点把请求跳回了登录页")
    if cloudflare_pending(index_html):
        return ActivityResult("failed", "站点首页返回 Cloudflare 验证页，未能读取最近动向")
    # 用户页优先：先试调用方给的 activity_path / 签到页，再试能定位到的 userdetails.php
    candidates: List[str] = []
    if path:
        candidates.append(str(path))
    for extra in paths or ():
        value = str(extra or "").strip()
        if value:
            candidates.append(value)
    match = _UID_RE.search(index_html)
    # 优先用 Cookie 里的 c_secure_uid：那才是「你自己」；首页里的 userdetails.php?id= 链接
    # 常常指向**别人**（论坛帖作者、最新注册用户、版主），拿它去开资料页会读到别人的「最近动向」
    # （皇后那条 2026-05-06 00:00:00 就是这么来的）。Cookie 里解不出 uid 时才退回首页链接。
    own_uid = cookie_uid(cookie)
    uid = own_uid or (match.group(1) if match else "")
    if uid:
        candidates.append(f"{DEFAULT_USERDETAILS_PATH}?id={uid}")
    # NexusPHP 的 userdetails.php 不带 id 时显示当前登录用户自己的资料——uid 解不出来
    # （自定义主题、Cookie 里没有 c_secure_uid）时靠这条兜底
    candidates.append(DEFAULT_USERDETAILS_PATH)
    candidates.append(DEFAULT_INDEX_PATH)
    seen: set = set()
    notes: List[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            page = session.get(target_url(base_url, candidate),
                               timeout=timeout, allow_redirects=True, proxies=proxies)
        except Exception as error:
            notes.append(f"{candidate}：请求失败（{type(error).__name__}）")
            continue
        body = page.text or ""
        if page.status_code >= 400:
            notes.append(f"{candidate}：HTTP {page.status_code}")
            continue
        if cloudflare_pending(body):
            # 撞盾时页面正文是挑战页，从中提取的「最近动向」是假的：直接换下一个候选页
            notes.append(f"{candidate}：Cloudflare 挑战页")
            continue
        if login_page(body):
            notes.append(f"{candidate}：被要求登录（Cookie 可能失效）")
            continue
        found = extract_activity(body)
        if found:
            return ActivityResult("success", f"已读取最近动向（{candidate}）",
                                  last_seen=found.get("last_seen", ""),
                                  snippet=found.get("snippet", ""),
                                  source=str(candidate))
        notes.append(f"{candidate}：{len(plain_text(body))} 字，没有「最近动向」")
    origin = (f"自己的 uid={own_uid}（取自 Cookie）" if own_uid
              else f"uid={uid}（取自首页链接）" if uid
              else "Cookie 里没解析到 uid 也没有首页链接")
    return ActivityResult("failed",
                          f"未在站点页面找到「最近动向」，{origin}（" + "；".join(notes) + "）")


# ---------------------------------------------------- 结果记录汇总（插件详情页用）
# 一轮签到会留下两类记录：签到记录（success / already / failed）与登录记录（login /
# login_failed）。同时勾了「签到站点」和「登录站点」的站点只跑一次（真实登录 + 签到 +
# 读最近动向），同一条记录带上 dual 标记后同时进两块统计。
SIGNIN_OK_STATUSES = {"success", "already"}
LOGIN_STATUSES = {"login", "login_failed"}
# 失败里属于「结果未知、需要人工确认」的那类：网络中断、二次核验没确认、token 注入没响应。
# 单独计数，避免和「Cookie 失效」这种明确失败混在一起。
UNCERTAIN_PATTERN = (r"(未确认|未能确认|响应丢失|未拿到可用响应|未显示签到成功状态"
                     r"|未拿到 Turnstile token|未识别到签到成功结果|未找到图片验证码)")


def record_uncertain(row: Mapping[str, Any]) -> bool:
    """这条失败记录是不是「结果未知、需人工确认」的那类。"""
    if str(row.get("status") or "") not in {"failed", "login_failed"}:
        return False
    if row.get("transient"):
        return True
    return bool(re.search(UNCERTAIN_PATTERN, str(row.get("message") or "")))


def is_login_record(row: Mapping[str, Any], strict: bool = False) -> bool:
    """这条记录要不要进「登录状态」。

    strict=True：只认真正的登录记录（纯登录，或签到站点同时也勾了登录站点）——详情页展示用，
    否则配了账号密码的纯签到站点会混进登录表，状态却显示「签到成功」，看着很怪。
    strict=False：还兼容 1.4.0 之前没有 dual 标记的历史记录（只要那一轮做过登录或读过动向就算）。
    """
    if bool(row.get("dual")) or str(row.get("status") or "") in LOGIN_STATUSES:
        return True
    if strict:
        return False
    return bool(str(row.get("login") or "").strip() or str(row.get("activity") or "").strip())


def is_signin_record(row: Mapping[str, Any]) -> bool:
    """这条记录要不要进「签到状态」：签到记录（含双选站点）算，纯登录记录不算。"""
    return str(row.get("status") or "") not in LOGIN_STATUSES


def record_meta(row: Mapping[str, Any]) -> Dict[str, str]:
    """记录的状态标签 / 颜色 / 图标；「未确认」的失败单独标色，提醒人工核对。"""
    status = str(row.get("status") or "")
    if record_uncertain(row):
        return {"label": "未确认", "color": "warning", "icon": "mdi-help-circle"}
    if status == "success":
        return {"label": "签到成功", "color": "success", "icon": "mdi-check-circle"}
    if status == "already":
        return {"label": "今日已签到", "color": "success", "icon": "mdi-check-circle"}
    if status == "login":
        return {"label": "登录成功", "color": "success", "icon": "mdi-login"}
    if status == "login_failed":
        return {"label": "登录失败", "color": "error", "icon": "mdi-alert-circle"}
    return {"label": "签到失败", "color": "error", "icon": "mdi-alert-circle"}


def summarize_records(records: Sequence[Mapping[str, Any]],
                      expected_sites: Sequence[str] = ()) -> Dict[str, int]:
    """汇总一轮记录：成功 / 失败 / 未确认 / 未记录。

    * 成功：success、already、login；
    * 未确认：结果未知、需人工核对的失败（与「失败」互斥，单独计数）；
    * 失败：其余可判定的失败（含 login_failed）；
    * 未记录：expected_sites（本轮勾选的站点）里一条记录都没有的站点数。
    """
    success = failed = uncertain = 0
    seen: set = set()
    for row in records:
        name = str(row.get("site") or "")
        if name:
            seen.add(name)
        status = str(row.get("status") or "")
        if record_uncertain(row):
            uncertain += 1
        elif status in SIGNIN_OK_STATUSES or status == "login":
            success += 1
        else:
            failed += 1
    missing = len({str(name) for name in expected_sites if str(name)} - seen)
    return {"total": len(list(records)), "success": success, "failed": failed,
            "uncertain": uncertain, "missing": missing}


def group_records_by_site(records: Sequence[Mapping[str, Any]]
                          ) -> Dict[str, List[Dict[str, Any]]]:
    """按站点名分组，保持首次出现的顺序（详情页按这个顺序渲染站点卡片）。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in records:
        name = str(row.get("site") or "未知站点")
        grouped.setdefault(name, []).append(dict(row))
    return grouped


def decode_entities(text: Any) -> str:
    """把文本里残留的 HTML 实体解码。

    用于展示层兜底：修复前存进历史的记录里是 `2026-10-17 03:52:21 ( &lt; 1分前 )`，
    显示时解码一次就能立刻变正常，不必等下一轮重新抓取。
    """
    return _html_unescape(str(text or ""))


def site_record_counts(records: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """每个站点的记录条数（详情页的「N 条记录」）。"""
    counts: Dict[str, int] = {}
    for row in records:
        name = str(row.get("site") or "未知站点")
        counts[name] = counts.get(name, 0) + 1
    return counts


# =========================================================== Browserless 引擎


def page_text(html: str) -> str:
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", html or "",
                     flags=re.IGNORECASE | re.DOTALL)
    # 同 plain_text：解 HTML 实体，避免结果里带出 `&lt;` / `&amp;` 这类源码写法
    visible = _html_unescape(re.sub(r"<[^>]+>", " ", visible))
    return re.sub(r"\s+", " ", visible).strip()


def first_keyword(text: str, keywords: Any) -> str:
    """返回 keywords 里第一个出现在 text 中的词（大小写不敏感），都不命中时返回空串。"""
    haystack = (text or "").lower()
    for word in keywords or []:
        needle = str(word).strip()
        if needle and needle.lower() in haystack:
            return needle
    return ""


def cloudflare_pending(html: str) -> bool:
    """页面是否仍停留在 Cloudflare / 雷池 WAF 的检测页（自动放行后即不再命中）。"""
    # Loaded JS libraries can retain these names after verification; inspect page text.
    body = page_text(html or "")
    if re.search(CF_INTERSTITIAL_PATTERN, body, re.IGNORECASE):
        return True
    return bool(re.search(WAF_PATTERN, body, re.IGNORECASE))


def control_text(html: str) -> str:
    """抽出页面上的「控件文字」：input 的 value、button/a/option 的文本等。

    签到入口大多是 `<input type=submit value="每日签到">` 或 `<a>签到</a>`，
    plain_text 会把标签连同属性一起去掉，直接用原始 HTML 判断又会被脚本 / 隐藏模板里的
    同样字样骗到。这个函数只取真正会显示在按钮上的文字。
    """
    body = str(html or "")
    parts = [match.group(1) for match in re.finditer(
        r"<input\b[^>]*\bvalue\s*=\s*[\"']([^\"']{1,40})[\"']", body, re.IGNORECASE)]
    for pattern in (r"<button\b[^>]*>(.{0,40}?)</button>", r"<a\b[^>]*>(.{0,40}?)</a>",
                    r"<option\b[^>]*>(.{0,40}?)</option>"):
        parts.extend(match.group(1) for match in re.finditer(
            pattern, body, re.IGNORECASE | re.DOTALL))
    return re.sub(r"\s+", " ", plain_text(" ".join(parts)))


def login_page(html: str) -> bool:
    """页面是否就是站点登录页。

    NexusPHP 在 Cookie 失效 / 未登录时会把 attendance.php 302 到
    ``login.php?returnto=attendance.php``；也有站点直接把登录表单塞进返回的页面。
    命中时要明确提示 Cookie 失效，而不是让人机验证超时背锅。
    """
    text = html or ""
    if re.search(LOGIN_PAGE_PATTERN, text, re.IGNORECASE):
        return True
    if "logout.php" in text.lower():
        return False
    return bool(re.search(r"name\s*=\s*[\"']?password", text, re.IGNORECASE)
                and re.search(r"name\s*=\s*[\"']?(?:username|email)", text, re.IGNORECASE))


def login_required_message(where: str) -> str:
    return (f"{where}：Cookie 无效或已过期，请在 MoviePilot 里重新登录该站并更新 Cookie；"
            "否则插件每次拿到的都只是登录页")


def append_hint(message: str, hint: str) -> str:
    """把现场诊断拼到失败原因后面（没有诊断时原样返回）。"""
    return f"{message}；{hint}" if hint else message


def _bonus_message(prefix: str, text: str, rule: Mapping[str, Any]) -> str:
    """成功 / 已签消息只带奖励数字，不再粘贴整页文字。"""
    bonus = extract_bonus(text, rule)
    return f"{prefix}（{bonus}）" if bonus else prefix


def classify(html: str, rule: Mapping[str, Any]) -> SignResult:
    full_text = page_text(html)
    lower = full_text.lower()
    # Piggo's normal navigation includes a link labelled 两步验证 on every logged-in page.
    auth_text = page_text(re.sub(r"<a\b[^>]*>.*?</a\s*>", " ", html or "",
                                 flags=re.IGNORECASE | re.DOTALL))
    if re.search(SECOND_FACTOR_PATTERN, auth_text, re.IGNORECASE):
        return SignResult("failed",
                          "站点要求二级验证（异地登录验证）：请在常用网络下打开站点完成一次验证，"
                          "或换一份完整的 Cookie（含 cf_clearance / 会话）再试")
    if any(word.lower() in lower for word in LOGIN_WORDS) and "logout" not in lower:
        return SignResult("failed", "Cookie 无效或已过期")
    if any(word.lower() in lower for word in rule.get("success_keywords", [])):
        return SignResult("success", _bonus_message("签到成功", full_text, rule))
    if any(word.lower() in lower for word in rule.get("already_keywords", [])):
        return SignResult("already", _bonus_message("今日已经签到", full_text, rule))
    # 静默成功的站点（学校这种）：入口消失即视为完成，浏览器引擎也要认
    absent = [str(item).strip() for item in (rule.get("absent_keywords") or [])
              if str(item).strip()]
    # 按钮文字常写在 value="..." 里，正文看不到；但脚本 / 隐藏模板里的字样不能算数，
    # 所以用「可见文字 + 控件文字」判断入口还在不在
    if absent and not any(word in full_text or word in control_text(html) for word in absent):
        status = "success" if str(rule.get("absent_status") or "").lower() == "success" else "already"
        label = "签到成功" if status == "success" else "今日已经签到"
        return SignResult(status, _bonus_message(label, full_text, rule))
    if not rule.get("strict_status_keywords") and any(word in lower for word in SUCCESS_WORDS):
        return SignResult("success", _bonus_message("签到成功", full_text, rule))
    if not rule.get("strict_status_keywords") and any(word in lower for word in ALREADY_WORDS):
        return SignResult("already", _bonus_message("今日已经签到", full_text, rule))
    return SignResult("failed", "未识别到签到成功结果；" + page_hint(html))


def classify_ajax_response(value: Any, rule: Mapping[str, Any]) -> Optional[SignResult]:
    if not value or value == "null":
        return None
    try:
        response = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if not isinstance(response, Mapping):
        return None
    status = response.get("status")
    if isinstance(status, int) and not 200 <= status < 400:
        return SignResult("failed", f"签到请求失败（HTTP {status}）")
    text = str(response.get("text") or "")
    success_json = rule.get("success_json") or {}
    if success_json:
        try:
            body = json.loads(text)
            current: Any = body
            for key in str(success_json.get("field") or "").lstrip("/").split("/"):
                current = current[key]
            if str(current) in {str(item) for item in success_json.get("values", [])}:
                message = str(body.get("sign") or body.get("msg") or "签到成功")
                bonus = extract_bonus(text, rule)
                if bonus and bonus not in message:
                    message = f"{message}（{bonus}）"
                return SignResult("success", message)
        except (KeyError, TypeError, ValueError):
            pass
    return classify(text, rule)
