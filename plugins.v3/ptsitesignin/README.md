# PT站点签到（私有部署版）

基于 [qingyukb/MoviePilot-Plugins 的 PTSiteSignIn 1.9.3](https://github.com/qingyukb/MoviePilot-Plugins/tree/d1e6ad90ff949f14920cf18451abe9e3b5bb1bb6/plugins.v3/ptsitesignin) 改造，保留原作者署名与站点规则。已核对上游 [1.9.4](https://github.com/qingyukb/MoviePilot-Plugins/commit/2e4cb36dc895266adf027f25c847d09feb5500fc) 的改动，版本号同步为 **1.9.4**，便于 MoviePilot 从 1.9.3 检测更新。该次上游更新没有新增站点规则；本版已支持根地址、完整 Function 端点和空 Token，继续保留私有 Function 实现。私有改造通过插件名的“（私有部署版）”后缀区分，便于后续同步上游特性并重新应用改造。

浏览器任务只使用自己部署的 Browserless v2 **Function API**。不调用 BrowserQL、Cloud 求解器或其他公共解题服务，不会自动回退到官方 Cloud。

## 配置

| 配置项 | 填写方式 |
| --- | --- |
| 自建 Browserless 地址 | 根地址，例如 `http://browserless:3000`，或完整 `/chromium/function` 地址 |
| 自建 Browserless Token | 自建实例的 Token，通过 `Authorization: Bearer` 发送；未设置鉴权的实例可留空 |
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

猪猪（`piggo.me`）默认使用 `engine: function`、`mode: cloudflare`，从首页点击签到；验证阶段最多等待 60 秒，提交后最多等待 15 秒，出现结果时提前结束。Sunny 也默认使用 Function，等待异步签到卡片与可用按钮出现，不把日历图例里的“已签到”算作今日完成。两站都使用自建 Browserless 的出口代理，不启动 MoviePilot 本机浏览器。已有自定义规则若固定为 `engine: playwright`，须改为 `engine: function`，因为自定义规则优先于内置预设。

| mode | 行为 |
| --- | --- |
| `open_page` | 打开页面，按选择器与文字点击，检查实际结果 |
| `cloudflare` | 启用开源 Chromium 的 `launch.stealth`，等待 JS 验证；激活远端页面并滚动到验证框后有限次点击；支持雷池 `Confirm` 确认按钮；自动签到先等待原生提交，再按站内回调或表单兜底 |
| `image` | 截图图片验证码，调用自建 OCR，填入并提交 |
| `trigger_image` | 先打开验证码弹窗，再截图、识别与提交 |
| `altcha` | 点击 Altcha 控件，等验证载荷生成后提交 |

每个站点的一次浏览器尝试只发一个 Function 请求，内部完成验证、签到和回读。HTTP 403/503 的挑战正文会先识别，避免错误状态码阻断浏览器回退。

`preflight_path` 会先在同一会话检查已签到状态；`submit_method: ajax` 会提交真实表单并读取接口回执，包括文本回执。自动提交仅在取得验证结果、且尚未观察到提交请求时兜底；已调用回调或发出请求后不会补发第二次 POST。表单兜底仅允许同源目标。

观众的验证等待预算由 15 秒调整为 60 秒。建议将浏览器任务总超时设为 180 秒；各阶段共用总预算，超时前尽量返回诊断。失败原因会附加阶段、耗时、页面 HTTP 状态、验证结果是否取得、验证框点击次数和提交请求数，不输出实际验证码 token 或 Cookie。页面脚本中残留的 WAF 字样、猪猪导航里的“两步验证”链接不再单独作为失败依据。

导航和等待验证前会调用 `page.bringToFront()`，激活 Browserless 服务器中的站点标签页；不启动 MoviePilot 本机浏览器。日志记录页面可见性、焦点、验证框点击异常及验证资源加载结果。Turnstile 脚本加载超时会明确报告资源错误；MoviePilot 调用 Browserless 时的 TLS 提前断开单独报告，避免将网络故障统称为验证失败。连接异常后仍不自动重放签到请求。

若自建实例需要关闭 stealth，可在 Browserless 地址的 `launch` 查询参数中明确设置 `{"stealth":false}`；插件保留显式配置。stealth 是浏览器启动选项，不包含 Cloud 的付费求解器。

## 能力边界

- OCR 当前识别六位大写字母和数字，不能识别滑块、海报选图或 Turnstile。
- 普通 Chromium 能否通过 Cloudflare 取决于站点验证和实例出口。此版本没有付费 solve 等价能力，未通过会明确失败，不把点击或 HTTP 200 当作成功。
- 农场 HTTP 模块复用 Cookie 会话，保存验证接口下发的新 Cookie；如果页面仍要求滑块，报告需要专用适配。
- 天空的 HTTP/OCR 模块和浏览器图片模式均使用配置的自建 OCR。北洋保留原海报比对模块。
- 猪猪与 Sunny 使用自建 Browserless 或其浏览器代理的出口，仍需符合站点的异地登录校验；真实的登录页或二级验证页面会报告失败。
- 不确定的浏览器提交结果不会在传输层自动重放。插件的定时失败重试仍受“失败重试次数”配置控制。

## 本地安装与验证

保持插件 ID `PTSiteSignIn`，配置与历史命名空间延续原插件。将整个 `ptsitesignin` 目录安装到 MoviePilot 的 `/config/plugins/ptsitesignin`，包含 `function.js`；仅替换 `__init__.py` 不够。覆盖前保留现有插件副本，以便恢复。

这是本地改造版；从原插件市场更新同 ID 插件会覆盖本地改动。初次验证建议并发 1、关闭失败重试，先选一个站点执行，再检查实际签到页面。

测试位于仓库的 `tests/v3/ptsitesignin/`，涵盖 Function 请求协议、结果判定、HTTP 挑战回退、Cookie 会话及浏览器/OCR 交互。真实部署的 Browserless、OCR 和 PT 账号需要另行联调；离线用例不能证明线上 Cloudflare 必定通过。
