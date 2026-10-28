# PT站点签到（私有部署版）

基于 [qingyukb/MoviePilot-Plugins 的 PTSiteSignIn 1.9.3](https://github.com/qingyukb/MoviePilot-Plugins/tree/d1e6ad90ff949f14920cf18451abe9e3b5bb1bb6/plugins.v3/ptsitesignin) 改造，保留原作者署名与站点规则。已核对上游 [1.9.4](https://github.com/qingyukb/MoviePilot-Plugins/commit/2e4cb36dc895266adf027f25c847d09feb5500fc) 的改动，版本号同步为 **1.9.4**，便于 MoviePilot 从 1.9.3 检测更新。该次上游更新没有新增站点规则；本版已支持根地址、完整 Function 端点和空 Token，继续保留私有 Function 实现。私有改造通过插件名的“（私有部署版）”后缀区分，便于后续同步上游特性并重新应用改造。

浏览器任务只使用自己部署的 Browserless v2 **Function API**。不调用 BrowserQL、Cloud 求解器或其他公共解题服务，不会自动回退到官方 Cloud。

## 配置

| 配置项 | 填写方式 |
| --- | --- |
| 自建 Browserless 地址 | 根地址，例如 `http://browserless:3000`，或完整 `/chromium/function` 地址 |
| 自建 Browserless Token | 自建实例的 Token，通过 `Authorization: Bearer` 发送；未设置鉴权的实例可留空 |
| FlareSolverr 地址 | 可选，v3.5+ 官方版根地址，例如 `http://flaresolverr:8191`；内嵌 Turnstile 站点（萝莉）用它签到 |
| CloakBrowser 地址 | 可选，cloakserve CDP 服务根地址（凭据可内嵌 `https://user:pass@host/`，或复用 Basic Auth 配置）；猪猪等雷池站点走它 |
| CloakBrowser 出口代理 | 可选，浏览器实例经它出网；会话按精确 IP 校验的站点（猪猪）必须与登录浏览器同出口（家宽代理需让目标域拨 IPv4） |
| MoviePilot-OCR 地址 | 根地址，例如 `http://moviepilot-ocr:9899`，或完整 `/captcha/base64` 地址 |
| 浏览器出口代理 | 可选，例如 `http://proxy:7890`；由 Browserless 容器连接，留空使用它自身的出口 |
| 浏览器任务超时 | 默认 120 秒，可按站点验证耗时调整到 180 秒；实例自身也必须允许对应时长 |

OCR 使用 [jxxghp/MoviePilot-OCR](https://github.com/jxxghp/MoviePilot-OCR) 的接口：`POST /captcha/base64`，请求为 `{"base64_img":"..."}`，响应为 `{"result":"ABC123"}`。MoviePilot 和 Browserless 容器都需要能访问这个地址。

浏览器内验证码先截图，再用同一浏览器上下文里的独立 OCR 页面提交图片，避免跨域限制。PT 页面一直保持打开，验证码、imagehash、Cookie 与提交处于同一会话。OCR 请求不携带 PT Cookie、站点地址或账号资料。

浏览器代理只影响 Function 路线；HTTP 和本机 Playwright 仍按 MoviePilot 站点的代理配置执行。HTTP 代理支持 URL 中的用户名密码，认证通过浏览器单独配置；SOCKS5 不支持用户名密码。代理地址由 Browserless 所在容器解析，不能使用仅对 MoviePilot 容器有效的 localhost 地址。

## 引擎与模式

- `engine: http`：直接请求站点接口。未在自定义规则中固定引擎时，遇 CF/WAF、验证码或无法确认结果可回退到自建 Function。
- `engine: function`：自建浏览器。旧的 `engine: browserless` 规则仍可使用，自动映射到 Function。
- `engine: playwright`：MoviePilot 本机浏览器；本机助手不可用时，使用已配置的自建 Function。
- `engine: flaresolverr`：v3.5+ 官方 FlareSolverr 的原生会话，专用于「签到表单内嵌 Turnstile」的站点（萝莉 `mua.xloli.cc` 内置此预设）。
- `engine: cloak`：远程 CloakBrowser（cloakserve CDP，反检测 Chromium）。MoviePilot 自带 playwright 直接 `connect_over_cdp`，实例经「CloakBrowser 出口代理」出网；401 / 4xx 立即报错不重试（防 fail2ban），实例崩溃自动换 seed 重试一次，雷池交互挑战的「确认」按钮自动点击。猪猪 `piggo.me` 内置此预设。

### FlareSolverr 引擎（萝莉）

只用官方 API，不改 FlareSolverr 源码：`sessions.create` → 在真实页面写入 Cookie → 打开签到页 → `request.get` + `tabs_till_verify` 求解 → 同会话 `request.post` 提交 → `sessions.destroy`。

- **为什么拆多步**：官方在同一次 `request.get` 里 Cookie 是「求解之后」才写入的；而萝莉未登录访问 `attendance.php` 返回 404 空正文（Chrome 错误页 origin 为空，`add_cookie` 报 `invalid cookie domain`），访问首页甚至直接断连。Cookie 必须写在 `login.php` 这类访客可正常渲染的页面上（预设 `flare_prime_path`，失败时自动回退 `/login.php`、`/`）。
- **Tab 次数怎么来**：v3.5 的原生解法是「从页面开头按 N 次 Tab 聚焦到 Turnstile iframe，再按 Space 勾选」。插件按 Chromium 顺序焦点导航规则从签到页 HTML 推算 N（`cf-turnstile` 容器前的可聚焦元素数 + 1，正 `tabindex` 排最前、禁用与隐藏元素跳过；观众 / OurBits 已用真实浏览器 Tab 游走核对为精确值）。静态推导看不到类 CSS 隐藏的元素，偏差时用 `scripts/ptsitesignin_tabwalk.mjs` 以真实 Chromium 校准（`--dry --save-html` 存页面后喂给它），再把实测值写进规则的 `flare_tabs`。
- **雷池等自放行检测页**：打开签到页后若停在检测页，用官方 `waitInSeconds` 等它跳过去（最多重试三轮）。
- **为什么必须同会话提交**：NexusPHP 的 Turnstile 校验（siteverify）会带上提交表单客户端的 `remoteip`；token 由哪个浏览器解出，就必须由同一个出口 IP 提交。token 一次性有效，取到后立即消费。
- **严格提交条件**：没有取得 `turnstile_token` 绝不发签到请求；已签到 / Cookie 失效（跳登录页）/ 停在检测页都会在提交前明确报告。
- 部署要求：FlareSolverr 自身出口须符合站点预期（同代理段）；建议 `LANG=en-US`。求解调用的超时上限 90 秒（命中复选框通常 30 秒内完成），HTTP 500 的错误体会被解析成可读原因。

猪猪（`piggo.me`）与 Sunny（`sunnypt.top`）默认使用 `engine: cloak`（远程 CloakBrowser，反检测 Chromium；实测还能原生解开全新的 Cloudflare 整页挑战）：猪猪打开 `attendance.php` 等 Cloudflare / 雷池放行（交互挑战的「确认」按钮自动点），「打开即签」自动完成；Sunny 打开 `/user/attendance` 等异步签到卡片，两段式点击「立即签到」后按状态词判定（不把日历图例里的“已签到”算作今日完成）。**猪猪生产三要素（2026-10-28 实测打通，缺一不可）**：①站点 Cookie 用新鲜整串（`cf_clearance` + `sl-*` + 会话）——站点在新登录时轮换会话令牌，换浏览器重登后旧 Cookie 立即失效；②实例出口 IP 与登录浏览器一致——家宽 Clash 需对 `piggo.me` 钉 IPv4（`hosts: {'piggo.me': <A记录>}`，用 `piggo.me/cdn-cgi/trace` 验证 `ip=` 为家宽 v4），否则会话按精确 IP 判游客；③UA 与签发 Cookie 的浏览器同族（引擎默认 Chrome 155 / macOS，站点管理里设了 UA 则优先）。不想用远程服务时，自定义规则 `{"engine":"playwright"}` 走 MoviePilot 本机浏览器（出口同为家宽 v4 时可用，上游即此做法）。YemaPT（`yemapt.org`）因会话每日失效已放弃签到（2026-10-28），其 Function/Altcha 预设保留但建议在站点选择里取消勾选；自建 Browserless 自此没有内置预设用户，仅服务自定义规则。

观众（`audiences.me`）与 OurBits（`ourbits.club`）默认使用 `engine: flaresolverr`：观众的组件只认 TRAWL（Camoufox）指纹，需在插件设置里填 **TRAWL 地址**（FlareSolverr 兼容 /v1 的自部署服务；未填时退回全局 FlareSolverr，会被该站组件拒绝）；两站访客访问都会先出 Cloudflare「Just a moment」整页挑战（预热时由 FlareSolverr 原生解掉、`cf_clearance` 留在会话内），观众签到表单里还有内嵌 Turnstile，与萝莉同款 `tabs_till_verify` 求解 + 同会话提交。观众如需回退旧的 Function+auto 路线，在自定义规则里写 `{"engine":"function","mode":"cloudflare","submit_method":"auto"}`。0ff（`0ff.cc` / `pt.0ff.cc`）的滑块由 `sites/farm.py` 直连 `slide_check` 脚本里的验证接口完成，不消耗浏览器。

| mode | 行为 |
| --- | --- |
| `open_page` | 打开页面，按选择器与文字点击，检查实际结果 |
| `cloudflare` | 启用开源 Chromium 的 `launch.stealth`，等待 JS 验证；激活远端页面并滚动到验证框后有限次点击；支持雷池 `Confirm` 确认按钮；自动签到先等待原生提交，再按站内回调或表单兜底 |
| `image` | 截图图片验证码，调用自建 OCR，填入并提交 |
| `trigger_image` | 先打开验证码弹窗，再截图、识别与提交 |
| `altcha` | 点击 Altcha 控件，等验证载荷生成后提交 |

每个站点的一次浏览器尝试只发一个 Function 请求，内部完成验证、签到和回读。HTTP 403/503 的挑战正文会先识别，避免错误状态码阻断浏览器回退。

`preflight_path` 会先在同一会话检查已签到状态；`submit_method: ajax` 会提交真实表单并读取接口回执，包括文本回执。自动提交仅在取得验证结果、且尚未观察到提交请求时兜底；已调用回调或发出请求后不会补发第二次 POST。表单兜底仅允许同源目标。

Function 引擎建议将浏览器任务总超时设为 180 秒；各阶段共用总预算，超时前尽量返回诊断。失败原因会附加阶段、耗时、页面 HTTP 状态、验证结果是否取得、验证框点击次数和提交请求数，不输出实际验证码 token 或 Cookie。页面脚本中残留的 WAF 字样、猪猪导航里的“两步验证”链接不再单独作为失败依据。

导航和等待验证前会调用 `page.bringToFront()`，激活 Browserless 服务器中的站点标签页；不启动 MoviePilot 本机浏览器。日志记录页面可见性、焦点、验证框点击异常及验证资源加载结果。Turnstile 脚本加载超时会明确报告资源错误；MoviePilot 调用 Browserless 时的 TLS 提前断开单独报告，避免将网络故障统称为验证失败。连接异常后仍不自动重放签到请求。

若自建实例需要关闭 stealth，可在 Browserless 地址的 `launch` 查询参数中明确设置 `{"stealth":false}`；插件保留显式配置。stealth 是浏览器启动选项，不包含 Cloud 的付费求解器。

## 能力边界

- OCR 当前识别六位大写字母和数字，不能识别滑块、海报选图或 Turnstile。
- 普通 Chromium 能否通过 Cloudflare 取决于站点验证和实例出口。此版本没有付费 solve 等价能力，未通过会明确失败，不把点击或 HTTP 200 当作成功。
- **观众（audiences.me）走 TRAWL（Camoufox）专用路线**：官方 FlareSolverr 浏览器被该站 sitekey 指纹拒绝（组件停在 Verifying 死循环），Browserless 也被 600010 拒绝；改用 TRAWL 的 FlareSolverr 兼容 `/v1`（预设 `flare_url` 指向 TRAWL 实例）并启用其 `solveEmbeddedCaptchas` 扩展解内嵌组件，2026-10-27 实测签到成功。TRAWL 部署见 [germondai/trawl](https://github.com/germondai/trawl)，需能被 MoviePilot 访问。
- **猪猪（piggo.me）远程路线已打通（2026-10-28，`engine: cloak` 实测签到成功）**。此前"四引擎全灭"是两个独立因素的叠加，均已定位并解决：①家庭 Clash 对 piggo.me（CF 有 AAAA）拨 **IPv6**，而登录浏览器走家宽 **IPv4**，猪猪会话按精确出口 IP 校验 → 应用层 302（用 `piggo.me/cdn-cgi/trace` 实证，已用 Clash `hosts` 钉 A 记录解决；用 myip 之类的旁站测出口家族不可靠，其 AAAA 可用性会变）；②站点在新登录时**轮换会话令牌**——换浏览器重登后旧 `pigo_np_web_session` 立即失效，测试用的旧 Cookie 全部已死。Cloudflare 借用的 `cf_clearance` 可跨浏览器复用（UA 同族即可）；雷池挑战 CloakBrowser 自动通过（「确认」按钮自动点）；Browserless（无头）与真实 Edge 被雷池反调试拦（「正在被调试」），FlareSolverr/TRAWL 连确认按钮都不渲染——CloakBrowser 是唯一能过雷池的远程内核。运维注意：MoviePilot 站点管理里的猪猪 Cookie 要保持新鲜整串；在其它浏览器重登猪猪会使它立即失效，需重新粘贴。
- 农场 HTTP 模块复用 Cookie 会话，保存验证接口下发的新 Cookie；如果页面仍要求滑块，报告需要专用适配。
- 天空的 HTTP/OCR 模块和浏览器图片模式均使用配置的自建 OCR。北洋保留原海报比对模块。
- Sunny 使用自建 Browserless 或其浏览器代理的出口，仍需符合站点的异地登录校验；真实的登录页或二级验证页面会报告失败。
- 不确定的浏览器提交结果不会在传输层自动重放。插件的定时失败重试仍受“失败重试次数”配置控制。

## 本地安装与验证

保持插件 ID `PTSiteSignIn`，配置与历史命名空间延续原插件。将整个 `ptsitesignin` 目录安装到 MoviePilot 的 `/config/plugins/ptsitesignin`，包含 `function.js`；仅替换 `__init__.py` 不够。覆盖前保留现有插件副本，以便恢复。

这是本地改造版；从原插件市场更新同 ID 插件会覆盖本地改动。初次验证建议并发 1、关闭失败重试，先选一个站点执行，再检查实际签到页面。

测试位于仓库的 `tests/v3/ptsitesignin/`，涵盖 Function 请求协议、结果判定、HTTP 挑战回退、Cookie 会话及浏览器/OCR 交互；`test_flaresolverr.py` 校验 FlareSolverr 的调用序列、Tab 次数推算与严格提交条件。真实部署的 Browserless、OCR 和 PT 账号需要另行联调；离线用例不能证明线上 Cloudflare 必定通过。

FlareSolverr 路线可脱离 MoviePilot 单独联调：`scripts/ptsitesignin_live.py` 直载插件真实代码，Cookie 从 `PTSIGNIN_COOKIE` 环境变量或交互输入。`--site 域名` 选择站点（自动套用内置预设，也可 `--engine http|function|flaresolverr|module` 覆盖、`--rule-json` 附加规则）；FlareSolverr 引擎建议先 `--dry`（只写 Cookie、开签到页并输出「组件/Tab 次数/表单」诊断，不求解不提交），确认后再去掉 `--dry` 执行完整签到；`--tabs N` 手工覆盖 Tab 次数，`--save-html` 留档页面。
