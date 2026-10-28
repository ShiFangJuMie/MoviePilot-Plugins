# PT站点签到（私有部署版）

基于 [qingyukb/MoviePilot-Plugins 的 PTSiteSignIn 1.9.3](https://github.com/qingyukb/MoviePilot-Plugins/tree/d1e6ad90ff949f14920cf18451abe9e3b5bb1bb6/plugins.v3/ptsitesignin) 改造，保留原作者署名与站点规则。已核对上游 [1.9.4](https://github.com/qingyukb/MoviePilot-Plugins/commit/2e4cb36dc895266adf027f25c847d09feb5500fc) 的改动，版本号同步为 **1.9.4**，便于 MoviePilot 从 1.9.3 检测更新。该次上游更新没有新增站点规则；本版已支持根地址、完整 Function 端点和空 Token，继续保留私有 Function 实现。私有改造通过插件名的“（私有部署版）”后缀区分，便于后续同步上游特性并重新应用改造。

浏览器任务只使用自己部署的 Browserless v2 **Function API**。不调用 BrowserQL、Cloud 求解器或其他公共解题服务，不会自动回退到官方 Cloud。

## 配置

| 配置项 | 填写方式 |
| --- | --- |
| 自建 Browserless 地址 | 根地址，例如 `http://browserless:3000`，或完整 `/chromium/function` 地址 |
| 自建 Browserless Token | 自建实例的 Token，通过 `Authorization: Bearer` 发送；未设置鉴权的实例可留空 |
| FlareSolverr 地址 | 可选，v3.5+ 官方版根地址，例如 `http://flaresolverr:8191`；内嵌 Turnstile 站点（萝莉）用它签到 |
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

### FlareSolverr 引擎（萝莉）

只用官方 API，不改 FlareSolverr 源码：`sessions.create` → 在真实页面写入 Cookie → 打开签到页 → `request.get` + `tabs_till_verify` 求解 → 同会话 `request.post` 提交 → `sessions.destroy`。

- **为什么拆多步**：官方在同一次 `request.get` 里 Cookie 是「求解之后」才写入的；而萝莉未登录访问 `attendance.php` 返回 404 空正文（Chrome 错误页 origin 为空，`add_cookie` 报 `invalid cookie domain`），访问首页甚至直接断连。Cookie 必须写在 `login.php` 这类访客可正常渲染的页面上（预设 `flare_prime_path`，失败时自动回退 `/login.php`、`/`）。
- **Tab 次数怎么来**：v3.5 的原生解法是「从页面开头按 N 次 Tab 聚焦到 Turnstile iframe，再按 Space 勾选」。插件按 Chromium 顺序焦点导航规则从签到页 HTML 推算 N（`cf-turnstile` 容器前的可聚焦元素数 + 1，正 `tabindex` 排最前、禁用与隐藏元素跳过；观众 / OurBits 已用真实浏览器 Tab 游走核对为精确值）。静态推导看不到类 CSS 隐藏的元素，偏差时用 `scripts/ptsitesignin_tabwalk.mjs` 以真实 Chromium 校准（`--dry --save-html` 存页面后喂给它），再把实测值写进规则的 `flare_tabs`。
- **雷池等自放行检测页**：打开签到页后若停在检测页，用官方 `waitInSeconds` 等它跳过去（最多重试三轮）。
- **为什么必须同会话提交**：NexusPHP 的 Turnstile 校验（siteverify）会带上提交表单客户端的 `remoteip`；token 由哪个浏览器解出，就必须由同一个出口 IP 提交。token 一次性有效，取到后立即消费。
- **严格提交条件**：没有取得 `turnstile_token` 绝不发签到请求；已签到 / Cookie 失效（跳登录页）/ 停在检测页都会在提交前明确报告。
- 部署要求：FlareSolverr 自身出口须符合站点预期（同代理段）；建议 `LANG=en-US`。求解调用的超时上限 90 秒（命中复选框通常 30 秒内完成），HTTP 500 的错误体会被解析成可读原因。

猪猪（`piggo.me`）默认使用 `engine: playwright`（MoviePilot 本机浏览器，出口即 MoviePilot 所在网络的出口）+ `cookie_mode: browser`：打开 `attendance.php`（`retry_paths` 里带 `/index.php` 兜底），等云盾 / 雷池检测页放行后按文字点「签到」。**为什么必须本机出口**：猪猪的 web 会话（`pigo_np_web_session`）按精确出口 IP 校验——会话在哪台设备 / 哪个 IP 下登录，就只认那个 IP；远程浏览器服务即使带上完整 Cookie，只要出口 IP 不同（典型：经代理对 piggo.me 拨了 IPv6，而登录浏览器走家宽 IPv4），应用层就会 302 回登录页。Cloudflare 与雷池两层放行都不受此影响。旧的自定义规则若固定了 `engine: function`（Browserless），猪猪会被「正在被调试」阻断页拦截，建议删除该自定义规则让预设生效。Sunny 默认使用 Function，等待异步签到卡片与可用按钮出现，不把日历图例里的“已签到”算作今日完成。

观众（`audiences.me`）与 OurBits（`ourbits.club`）默认使用 `engine: flaresolverr`：两站访客访问都会先出 Cloudflare「Just a moment」整页挑战（预热时由 FlareSolverr 原生解掉、`cf_clearance` 留在会话内），观众签到表单里还有内嵌 Turnstile，与萝莉同款 `tabs_till_verify` 求解 + 同会话提交。观众如需回退旧的 Function+auto 路线，在自定义规则里写 `{"engine":"function","mode":"cloudflare","submit_method":"auto"}`。0ff（`0ff.cc` / `pt.0ff.cc`）的滑块由 `sites/farm.py` 直连 `slide_check` 脚本里的验证接口完成，不消耗浏览器。

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
- **猪猪（piggo.me）会话按精确出口 IP 校验（2026-10-28 判别定论）**：把 Edge 的完整 Cookie 连同完全一致的 UA / sec-ch-ua 客户端提示 / Accept-Language / 相同家宽出口移植到远程浏览器（CloakBrowser，经家庭 Clash 代理）后，Cloudflare（借用的 cf_clearance 被接受）与雷池（挑战通过、接受注入的 sl-session）两层都放行，应用层仍 302 回登录页——因为 Clash 对 piggo.me（CF 有 AAAA 记录）拨 **IPv6**（`piggo.me/cdn-cgi/trace` 实证 ip=240e:...），而登录浏览器（Edge）走家宽 **IPv4**，会话绑定的精确 IP 对不上。原生 NexusPHP 的 Cookie 校验本是无绑定的 HMAC(user_id+expires)（本地源码核对），猪猪 fork 在其上叠加了 IP 校验。**推论**：①本机 Playwright 引擎（出口=MoviePilot 所在网络）与 Edge 同源时可用——这是上游对猪猪的既定做法，本插件预设已对齐；②远程浏览器路线要可用，须先让代理链对 piggo.me 拨 IPv4（如 Clash 的 direct 出站 `ip-version: ipv4` 或 hosts 固定 A 记录），用 `piggo.me/cdn-cgi/trace` 验证出口回到登录时的 IPv4；③用 myip.ipip.net 之类的旁站测「代理是否生效 / 家族是否一致」不可靠（其 AAAA 可用性会变，同一代理今天 v4 明天 v6）——判 IP 必须用目标站自己的 CF trace。另：雷池对无 stealth 的 CDP 浏览器（含真实 Edge）会给「当前环境正在被调试」阻断，Browserless 与 FlareSolverr/TRAWL 在猪猪上各有反调试 / 按钮不渲染的拦截，CloakBrowser 是唯一能过雷池的远程引擎。
- 农场 HTTP 模块复用 Cookie 会话，保存验证接口下发的新 Cookie；如果页面仍要求滑块，报告需要专用适配。
- 天空的 HTTP/OCR 模块和浏览器图片模式均使用配置的自建 OCR。北洋保留原海报比对模块。
- Sunny 使用自建 Browserless 或其浏览器代理的出口，仍需符合站点的异地登录校验；真实的登录页或二级验证页面会报告失败。
- 不确定的浏览器提交结果不会在传输层自动重放。插件的定时失败重试仍受“失败重试次数”配置控制。

## 本地安装与验证

保持插件 ID `PTSiteSignIn`，配置与历史命名空间延续原插件。将整个 `ptsitesignin` 目录安装到 MoviePilot 的 `/config/plugins/ptsitesignin`，包含 `function.js`；仅替换 `__init__.py` 不够。覆盖前保留现有插件副本，以便恢复。

这是本地改造版；从原插件市场更新同 ID 插件会覆盖本地改动。初次验证建议并发 1、关闭失败重试，先选一个站点执行，再检查实际签到页面。

测试位于仓库的 `tests/v3/ptsitesignin/`，涵盖 Function 请求协议、结果判定、HTTP 挑战回退、Cookie 会话及浏览器/OCR 交互；`test_flaresolverr.py` 校验 FlareSolverr 的调用序列、Tab 次数推算与严格提交条件。真实部署的 Browserless、OCR 和 PT 账号需要另行联调；离线用例不能证明线上 Cloudflare 必定通过。

FlareSolverr 路线可脱离 MoviePilot 单独联调：`scripts/ptsitesignin_live.py` 直载插件真实代码，Cookie 从 `PTSIGNIN_COOKIE` 环境变量或交互输入。`--site 域名` 选择站点（自动套用内置预设，也可 `--engine http|function|flaresolverr|module` 覆盖、`--rule-json` 附加规则）；FlareSolverr 引擎建议先 `--dry`（只写 Cookie、开签到页并输出「组件/Tab 次数/表单」诊断，不求解不提交），确认后再去掉 `--dry` 执行完整签到；`--tabs N` 手工覆盖 Tab 次数，`--save-html` 留档页面。
