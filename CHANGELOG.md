# 更新清单

## v0.2.0

### 新增

- WechatOnCloud 可选 Docker 后端：在指定微信实例容器内运行 worker，复用现有 `:1` 桌面和微信客户端。
- `wechat-cli woc install CONTAINER` 与 `woc status CONTAINER`，支持安装、更新适配及检查容器状态。
- 实时目标切换：`wechat-cli target`、`target local`、`target woc CONTAINER`；交互控制台提供 `/target`，机器接口提供 `target.status` 和 `target.select`。
- Demo 顶部目标切换控件；CLI、控制台、Demo 和 MCP 共享当前目标，无需因切换重启进程或微信。
- WOC 控制台登录二维码读取和字符预览；安装器支持自定义可信 PyPI 镜像并优先使用 wheel。

### 调整

- 独立模式保留 Xvfb 和本地自动化服务，移除 VNC 部署、凭据管理、端口配置及 `session.gui`、`session.remote`、`/gui`、`/remote` 接口。图形访问改由 WechatOnCloud 提供。
- 已执行和已排队任务固定原目标；进入聊天、操作及延迟重置沿用相同目标，去重和工作面复用按目标隔离。
- 实时切换前检查 WOC worker，失败保留原目标；宿主目标选择不会影响容器内 worker 的本地路由。
- `stop-all.sh` 按当前实时目标停止服务，WOC 模式保留容器、微信与桌面。
- 移除 wechat-link 原型，中文 README 改为推荐 WechatOnCloud，并补充安装、切换、路径和部署说明。
- 忽略本机 WechatOnCloud 部署脚本与目录，避免将本地配置提交到仓库。

### 验证与限制

- 163 项 Python 测试通过；Demo 前端安全回归、Shell 语法和补丁空白检查通过。
- 在 WSL 中实测 WOC 安装、桌面连接、服务启停与 RapidOCR 推理；同一 stdio 进程完成 WOC → 独立 → WOC 切换，原微信进程保持运行。
- 切换操作目标不会迁移账号数据或登录态。文件与截图路径属于所选运行环境。
- WOC 浏览器与自动化仍共享输入，任务执行期间需要暂停手动操作；本版不提供网页输入租约。
