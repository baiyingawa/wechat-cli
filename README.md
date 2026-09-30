# wechat-cli（实验性项目）

面向**官方 Linux 微信客户端**的 Linux/WSL 桌面自动化工具。请求、响应及错误均使用 JSON。它通过常规 X11 鼠标/键盘输入与屏幕截图操作，不使用私有协议、代码注入或直接访问微信进程内存。专用的 3840x2160 Xvfb 显示屏可以呈现更多内容；操作通过可见界面变化确认，而非依赖固定等待时间。OCR 识别存在误差：**不要把视觉识别出的名称或消息当作稳定 ID 或绝对事实**。

当前版本：`v0.1.0`

## 设计重点

- **响应及时且节制：**尽可能使用 XDamage 与视觉变化等待替代固定 `sleep`。界面目标使用快速 OCR，面向用户的内容则使用识别质量更高的 RapidOCR。
- **安全优先：**所有操作均通过官方客户端界面完成。不使用私有协议、进程内存访问、代码注入，也不支持发送付款。变更操作需要幂等键；删除和退群操作还需要二次确认。
- **不承诺规避检测：**视觉自动化仍可能被微信或运行环境观察、限制或拦截。请仅在自己控制的账号和设备上使用，并遵守适用的平台规则。

## 安装与启动

适用于已在 Ubuntu 24.04 上安装 Linux 微信客户端，且客户端位于 `/opt/wechat/wechat` 或 `PATH` 中的环境：

```sh
sudo apt-get install xvfb x11vnc xclip tesseract-ocr tesseract-ocr-chi-sim
python3 -m pip install --user -e .
export WECHAT_DISPLAY=:99
export WECHAT_WIDTH=3840 WECHAT_HEIGHT=2160
wechat-cli call session.start
wechat-cli call session.login
```

如果发行版要求使用虚拟环境安装 `pip` 包，请先激活虚拟环境。需要时，`session.login` 会点击唯一可见的“登录”或“进入微信”按钮，并在等待手机扫码确认时返回本地截图路径；请在本机显示该图片后扫码确认，工具**不会**绕过登录。登录一次后再执行其他操作。WSL 环境请在目标 WSL 发行版中运行这些命令；可使用 `wslpath -w '/path/returned/by/session.login.png'` 在 Windows 中打开 WSL 截图。

`session.start` 会复用可访问的显示屏和已有客户端，不会删除其他 WSL 发行版。默认分辨率为 3840x2160，可用 `WECHAT_WIDTH` 与 `WECHAT_HEIGHT` 修改。`WECHAT_TIMEOUT` 控制语义化界面等待，`WECHAT_RETENTION_DAYS` 默认值为 15。窗口较小时可使用 `wechat-cli doctor` 与 `wechat-cli call ui.maximize`。本地 Unix socket 服务独占桌面自动化；CLI 命令会按需启动它。`wechat-cli service stop` 只停止自动化服务，**不会**停止微信或 Xvfb 显示屏。

内容读取使用 RapidOCR 的 ONNX 中文模型，适合小号界面文本；它与用于界面目标定位的快速 Tesseract 读取器相互独立。RapidOCR 不可用时会自动回退到官方 `tessdata_best` 模型。`metrics` 会报告各路径使用的识别配置。可按以下方式安装回退模型：

```sh
mkdir -p ~/.local/share/wechat-cli/tessdata_best
curl -L -o ~/.local/share/wechat-cli/tessdata_best/chi_sim.traineddata https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/chi_sim.traineddata
curl -L -o ~/.local/share/wechat-cli/tessdata_best/chi_sim_vert.traineddata https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/chi_sim_vert.traineddata
curl -L -o ~/.local/share/wechat-cli/tessdata_best/eng.traineddata https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/eng.traineddata
mkdir -p ~/.local/share/wechat-cli/tessdata_best/configs
cp /usr/share/tesseract-ocr/5/tessdata/configs/tsv ~/.local/share/wechat-cli/tessdata_best/configs/tsv
```

## MCP

在 Linux 或 WSL 的仓库根目录运行 `./mcp.sh` 可启动 Model Context Protocol 标准输入输出服务。脚本会自行定位项目并调用 `wechat-cli mcp`；支持 MCP 协议版本 `2024-11-05`、`2025-03-26` 与 `2025-06-18`，提供 `wechat_capabilities` 和 `wechat_call` 两个工具。后者接收现有方法名、参数对象、可选 `idempotency_key` 以及破坏性操作可选的 `confirm_token`，复用本地自动化服务的全部安全校验。

### 安装 MCP

1. 在目标 WSL 发行版中按上文安装项目。`mcp.sh` 需要 `python3` 及项目 Python 依赖。
2. 通过 `./start.sh` 或 `start-wsl.bat` 启动一次微信桌面会话并完成手动扫码登录。`start.sh` 会校验 `mcp.sh` 是否可执行且可用。MCP 会按需启动自动化服务，但不会绕过登录。
3. 将服务添加到 MCP 宿主配置中。Windows 上使用 `mcp-wsl.bat`：

```json
{"mcpServers":{"wechat-cli":{"command":"E:\\PROJECT\\wechat-cli\\mcp-wsl.bat"}}}
```

Linux 或已在 WSL 中运行的 MCP 宿主可直接指向 `mcp.sh`：

```json
{"mcpServers":{"wechat-cli":{"command":"/absolute/path/to/wechat-cli/mcp.sh"}}}
```

除非设置 `WECHAT_CLI_WSL_DISTRO`，`mcp-wsl.bat` 会使用 Windows 默认 WSL 发行版。两个启动器均为 stdio 服务：MCP 宿主会在连接时启动并拥有进程，`start.sh` 不能将其作为脱离终端的后台服务预启动。`stop-all.sh` 会在停止微信会话前终止当前由宿主启动的 `wechat-cli mcp` 进程。完成配置后，重启或重新连接 MCP 宿主，再先调用 `wechat_capabilities` 查看能力，然后使用 `wechat_call`。

## 启动与停止脚本

Windows 中运行 `start-wsl.bat` 会打开标题为 `wechatcli` 的命令行窗口，进入 Windows 默认 WSL 发行版并运行 `start.sh`。脚本开头会输出浏览器控制地址，随后启动 `:99` 微信会话与 `http://127.0.0.1:8765` 的本地 Demo，并校验 MCP 启动器。可在运行任一批处理文件前设置 `WECHAT_CLI_WSL_DISTRO` 选择特定发行版。

在交互终端中运行时，`start.sh` 会在服务就绪后打开 `wechatcli>` 控制台。使用 `/help` 查看常用短命令，`/exit` 关闭控制台但不停止服务。`/login` 会按需启动客户端、自动点击唯一识别出的“登录”按钮、保存登录界面与检测到的二维码截图，并在二维码变化时刷新字符预览；扫码后会自动检测登录完成，按 `Ctrl-C` 可停止等待。会话命令还包括 `/start`、`/logout`、`/status`、`/maximize`、`/reset`、`/gui on|off` 与 `/remote`；消息命令例如 `/chat False 111`、`/read False 30`、`/find False keyword`、`/recall False 111`。`/like False post_text` 可点赞，`/feed` 打开全局朋友圈。使用 `/methods` 查看底层能力名称，使用 `/help METHOD` 查看指定能力参数；完整能力仍可通过 `/call METHOD JSON_PARAMS` 或 `METHOD JSON_PARAMS` 调用。无人值守启动时设置 `WECHAT_NO_CONSOLE=1`。Demo 仅绑定 localhost，其他局域网设备无法访问。

控制台在终端中自动启用颜色与加粗：命令和提示符为青色，成功状态为绿色，错误为红色，登录等待提示为黄色，操作网址带下划线，JSON 结果按字段和值高亮。重定向或管道输出保持纯文本，CLI 的机器接口和 MCP 输出格式不变。设置 `NO_COLOR=1` 可关闭颜色与加粗（例如 `NO_COLOR=1 ./console.sh`）；`TERM=dumb` 也会关闭样式。二维码字符预览保持原样。

在 WSL 中运行 `stop-all.sh` 可停止本项目中由宿主启动的 MCP 进程、Demo、自动化服务、VNC、微信、Openbox 及 Xvfb 显示屏。Windows 中运行 `stop.all-wsl.bat` 会调用该脚本，然后执行 `wsl --shutdown`，这会停止全部 WSL 发行版。

### 本地 Demo

也可在 WSL 中直接启动浏览器控制器：

```sh
wechat-cli demo
```

在本机打开 `http://127.0.0.1:8765`。Demo 根据与 CLI 相同的 schema 渲染所有已实现能力，串行创建任务，并将任务状态和结果持久化到 `~/.local/state/wechat-cli/demo.sqlite3`。页面首次打开时建立仅限本次进程的 HttpOnly、SameSite 会话 Cookie；API 校验 localhost `Host`、同源 `Origin`、JSON `Content-Type`，不允许跨源预检和跨站请求。页面使用 CSP nonce，任务内容通过 DOM `textContent` 写入，避免把聊天名称当作 HTML 执行。15 秒内相同的方法/参数提交会关联至原任务，不会重复点击。每个可执行任务记录 `enter`、`operate`、`reset` 阶段。一次聊天操作通过验证后，其 reset 阶段最多保持 3 分钟：同一聊天的下一任务会直接复用操作界面；其他聊天则会先重置前一界面再进入目标界面。Demo 只绑定 localhost。它会为需要幂等键的操作自动生成幂等键；破坏性确认操作需粘贴前一任务返回的 `confirm_token`，Demo 会从本地任务历史中恢复原始幂等键；重启时执行中的任务会标记为 `unknown`，不会自动重试。

如需远程查看：

```sh
wechat-cli call session.remote
ssh -L 5909:127.0.0.1:5909 USER@HOST
```

通过 SSH 隧道让 VNC 查看器连接 `localhost:5909`。`session.remote` 会在 JSON 中返回密码，**不要记录或转发该输出**。VNC 监听器仅绑定 localhost，VNC 的旧式密码认证不能替代 SSH。`~/.local/state/wechat-cli/` 下的凭据文件权限为 `0600`。在常规 WSL2 配置中，Windows 程序也可能访问 WSL 的本地端口。

## Windows 图形界面

可使用 Windows GUI 开关，从 Windows 手动操作同一个全屏 Xvfb 微信会话：

```sh
wechat-cli call session.gui --params '{"enabled":true}'
```

结果会包含一次性本地 VNC 密码。在 Windows VNC 查看器中连接 `127.0.0.1:5909`；常规 WSL2 安装会将 WSL localhost 端口转发到 Windows。查看器与 CLI 共享同一个客户端，因此自动化任务执行期间不要移动鼠标。结束后关闭图形访问：

```sh
wechat-cli call session.gui --params '{"enabled":false}'
```

此开关仅启动或停止本工具的 localhost `x11vnc` 进程，不会将显示屏暴露到局域网，也不会关闭微信或 Xvfb。

## 面向机器的调用

```sh
wechat-cli capabilities
wechat-cli call chat.open --params '{"chat":"False"}'
wechat-cli call ui.reset
wechat-cli call message.read --params '{"chat":"False","limit":30}'
wechat-cli call message.search --params '{"chat":"False","query":"keyword","limit":30}'
wechat-cli call message.send --params '{"chat":"False","text":"hello"}' --idempotency-key 'job-123-message-1'
wechat-cli call message.send_file --params '{"chat":"False","path":"/home/user/document.txt"}' --idempotency-key 'job-123-file-1'
wechat-cli call message.download --params '{"chat":"False","visual_digest":"0123456789abcdef0123456789abcdef","path":"/home/user/downloads/file.bin"}' --idempotency-key 'job-123-download-1'
wechat-cli call message.reply --params '{"chat":"False","quote_text":"hello","text":"reply"}' --idempotency-key 'job-123-reply-1'
wechat-cli call message.forward --params '{"chat":"False","text":"hello","to":"False"}' --idempotency-key 'job-123-forward-1'
wechat-cli call message.revoke --params '{"chat":"False","text":"hello"}' --idempotency-key 'job-123-revoke-1'
wechat-cli call message.pat --params '{"chat":"False","target":"other"}' --idempotency-key 'job-123-pat-1'
wechat-cli call message.pat_revoke --params '{"chat":"False"}' --idempotency-key 'job-123-pat-revoke-1'
wechat-cli call settings.voice_text
wechat-cli call favorite.search --params '{"query":"keyword"}'
wechat-cli call chat.pin --params '{"chat":"False","enabled":true}'
wechat-cli call chat.mute --params '{"chat":"False","enabled":false}'
wechat-cli call moments.feed.read --params '{}'
wechat-cli call moments.publish --params '{"text":"private sample","images":["/home/user/sample.png"],"visibility":"private"}' --idempotency-key 'job-123-moments-1'
wechat-cli call contact.add --params '{"identifier":"wxid_example","message":"hello"}' --idempotency-key 'job-123-contact-1'
wechat-cli call transfer.accept --params '{"chat":"False","text":"transfer memo"}' --idempotency-key 'job-123-transfer-1'
wechat-cli stdio
```

`stdio` 每行接收一个请求 JSON 对象，并每行返回一个响应 JSON 对象。例如：`{"id":"req-1","method":"message.read","params":{"chat":"False"}}`。响应包含 `ok`、`result` 或 `error` 和 `elapsed_ms`；参数 schema 请查阅 `capabilities`。持久服务串行处理命令。发送消息、转发、撤回、拍一拍和添加收藏均需要调用方生成 `idempotency_key`：以相同参数重复使用同一键会重放记录结果而不再次点击；使用同一键但不同参数会被拒绝。如果提交超时，结果未知，使用新键前请**检查界面和任何待发送草稿**。消息气泡/文件卡片只能确认提交，不代表已送达其他设备。文件选择会创建待发送附件；CLI 会验证附件、只点击一次发送，然后检查是否出现新的己方卡片。路径必须是微信客户端可访问的绝对 Linux 路径。只有精确且唯一的己方消息气泡或己方拍一拍提示可见，且原生操作可被唯一识别时才会执行撤回；过期消息不会被删除替代。

破坏性方法使用两次调用确认。第一次调用附带 `idempotency_key`；`CONFIRMATION_REQUIRED` 错误会返回一个 120 秒有效且只能使用一次的 `confirm_token`。使用完全相同的方法、参数和键，并附带 `--confirm-token` 重复调用。`message.delete` 与 `group.leave` 还会在点击前验证原生确认对话框。若“清空聊天记录”已被选中，退群操作会拒绝继续。

发送前会检查已有草稿。复制前先在剪贴板中设置临时校验标记，避免微信连续复制时因剪贴板所有者不变而误报超时；空草稿则必须通过整个编辑区的像素检查确认。已有不同内容不会被覆盖，粘贴后的内容仍须再次复制并与请求文本完全一致，无法确认时不会点击发送。

聊天和群参数必须与可见标题完全一致。后续群操作请使用 `group.create` 返回的 `chat` 值（包括成员数后缀）；这样可避免将 `False (2)` 这样的群误认为名为 `False` 的单聊。

打开聊天采用“点击搜索框、全选清空、输入名称、回车”的顺序。输入通过剪贴板校验；回车前以小区域像素检测确认搜索命中高亮已经出现，进入后再验证聊天标题和编辑区。不会因输入框刚变动就误判搜索结果已加载，也不依赖 OCR 定位搜索结果来点击。

`message.read` 默认读取当前最大化聊天窗口内尽可能多的可见气泡。指定 `limit`（1-30）会向上滚动、合并重叠视觉页面，最多返回该数量的最近检测消息。当内容或视口变化时，识别可能漏掉或重复消息。服务按方法选择等待上限：读取最多 30 条消息可等待更长时间，文件与登录操作也有独立上限；`service.status` 可通过控制 socket 快速查询服务是否正在执行桌面操作。`state.cleanup` 会删除超过 15 天的本地截图、日志、已完成请求结果和游标，并保留未知执行的幂等键；人工检查后可使用 `state.resolve` 标记 `executed` 或 `not_executed`，后者才允许同键重试。它**不会**删除聊天或其他账号数据。

`message.voice_text` 接收从 `message.read` 取得的一条可见语音气泡的 `chat` 和 `visual_digest`。如果相邻转写已显示则直接返回 OCR 结果；否则点击精确的语音转文字菜单项，并验证出现新的相邻转写。界面重绘会改变视觉摘要，选择气泡前请刷新 `message.read`。`settings.voice_text` 会在需要时启用官方客户端的自动语音转文字开关。

`moments.publish` 接收恰好一张已有本地图片和可选文本。使用 `visibility:"private"` 发布私密朋友圈，使用 `visibility:"public"` 或 `"contacts"` 选择客户端原生公开选项，使用 `visible_tags` / `visible_contacts` 设置“谁可以看”，使用 `hidden_contacts` 设置“不给谁看”。指定可见名单和不可见名单不能同时使用。已探索的 Linux 客户端图片发布器不支持纯文本发布，工具不会伪造成功结果。

## 当前覆盖范围

| 区域 | 可用方法 | 限制 |
| --- | --- | --- |
| 会话与诊断 | `session.start`, `session.status`, `session.login`, `session.logout`, `session.remote`, `session.gui`, `doctor`, `ui.windows`, `ui.tree`, `ui.screenshot`, `ui.maximize`, `ui.reset` | 需要手动手机登录；`session.gui` 仅通过 localhost VNC 暴露同一会话；`ui.reset` 关闭主窗口详情面板并返回会话列表；该客户端没有可用的 AT-SPI 无障碍树。 |
| 账号 | `account.profile`, `account.refresh` | 在 `~/.local/state/wechat-cli/account/avatar.png` 保存显示名称、微信号与打开后的头像。刷新最多每两小时一次；空闲五分钟且缓存至少一天未更新、没有二级微信窗口时，服务会尝试刷新。 |
| 聊天 | `chat.open`, `chat.list`, `chat.pin`, `chat.mute` | 匹配可见显示名称和 OCR 行，不使用不可变账号 ID。 |
| 消息 | `message.read`, `message.search`, `message.send`, `message.send_file`, `message.download`, `message.reply`, `message.forward`, `message.revoke`, `message.pat`, `message.pat_revoke`, `message.delete`, `message.voice_text` | `message.pat` 查找最近可见头像；必要时最多向上搜索三页。`message.pat_revoke` 仅在可见己方拍一拍提示的悬停控件可被唯一读取时执行。搜索最多覆盖视觉读取到的 30 条近期消息；OCR 和易变的视觉摘要可能失败。下载绝不会覆盖已有文件。 |
| 历史 | `history.manage`, `state.cleanup` | 报告配置的本地保留期；不支持迁移和导出。 |
| 联系人 | `contact.info`, `contact.list`, `contact.remark`, `contact.add`, `contact.accept`, `contact.delete` | 添加/接受操作要求精确可见的资料或待处理请求及幂等键。删除要求二次确认和原生对话框验证。 |
| 收藏 | `favorite.list`, `favorite.search`, `favorite.add`, `favorite.delete` | 搜索/列表仅为视觉观察；删除要求精确唯一结果和两次确认。 |
| 群聊 | `group.create`, `group.members`, `group.invite`, `group.remove`, `group.announcement`, `group.rename`, `group.announcement_set`, `group.leave` | 创建/邀请/改名/发布公告需要幂等键。移除成员和退群要求二次确认及原生对话框验证。成员单元格仅为视觉观察。 |
| 转账 | `transfer.accept` | 仅支持接收精确可见的入账转账；不支持发送付款。 |
| 朋友圈 | `moments.open`, `moments.read`, `moments.feed.open`, `moments.feed.read`, `moments.pinned.read`, `moments.like`, `moments.unlike`, `moments.comment`, `moments.comment_delete`, `moments.pin`, `moments.publish` | 全局朋友圈与联系人朋友圈为独立视图。`moments.pinned.read` 仅点击唯一可见的顶部置顶行；发布目前恰好支持一张图片。 |
| 设置 | `settings.voice_text` | 检查并启用客户端“通用”设置中的自动语音转文字。 |

公众号、小程序、视频号和通话不在范围内；多账号、持续监听、自动回复与导出仍暂缓。该项目仍为实验性源码，客户端 UI 更新后视觉自动化可能失败。

## 测试

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

真实界面验证应使用已同意的专用测试联系人，并避免删除联系人或数据。`False` 仅是开发环境中的测试联系人，并非内置账号名称。
