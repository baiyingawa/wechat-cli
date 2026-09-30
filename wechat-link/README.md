# wechat-link 原型

这是 Windows 端的第一阶段原型，提供窗口和托盘。它通过 `wsl.exe` 或 `ssh.exe` 调用目标端的 `session.link`，取得一次性 VNC 凭据，再启动 TigerVNC 查看器，无需手动输入密码。Windows 临时文件只包含 VNC 格式凭据，目录 ACL 限定当前用户访问；关闭查看器时删除临时文件，并调用 `session.link_close` 撤销连接。VNC 格式凭据可逆，应当像密码一样保护。

## 安装

1. 目标 WSL 或服务器安装并运行本项目，确保 `x11vnc`、微信显示屏和自动化服务可用。
2. Windows 安装 Python 3.11 或更新版本、TigerVNC，并执行：

   ```powershell
   py -m pip install -r wechat-link\requirements.txt
   ```

   TigerVNC 可从官网安装，或者运行 `winget install --id TigerVNC.TigerVNC --exact`。启动器检测 PATH、`C:\Program Files\TigerVNC` 和 `%LOCALAPPDATA%\TigerVNC` 中的 `vncviewer.exe`，不随源码分发第三方程序。

3. 双击仓库根目录的 `wechat-link.bat`。WSL 目标填写发行版名称，例如 `Ubuntu-24.04`；SSH 目标填写 `user@host`、IP 地址或 SSH 配置中的别名。SSH 使用 `BatchMode=yes`，需要事先配置密钥认证和 `known_hosts`，不会弹出密码或主机指纹输入提示。
4. 项目目录填写目标端仓库路径。WSL 首次启动会尝试通过 `wslpath` 自动找到本仓库；服务器填写服务器上的实际路径。目标已将项目安装到 Python 环境时，可以留空。Linux 端修改代码后需重启已有 `wechat-cli` 自动化服务。

配置保存到 `%APPDATA%\wechat-link\targets.json`，仅包含连接目标和显示比例。选择“显示比例”后重新连接生效；默认 50%，将 4K 画面导出为约 1080p。TigerVNC 本身没有通用缩放选项，原型使用 x11vnc 对传输画面进行缩放，鼠标坐标由服务端映射；自动化使用的真实显示屏仍为 3840×2160。弹窗也会随整张桌面显示。

WSL 直接使用 Windows 到 WSL 的 localhost 转发。SSH 目标会自动建立随机本地端口到目标 `127.0.0.1:<VNC端口>` 的转发，因此目标端的 VNC 不会对外监听。

`session.link` 默认选择空闲端口，每次生成新的凭据，等待首次认证连接最多 300 秒；认证查看器断开后进程退出。可用 `WECHAT_LINK_PORT` 指定固定端口。原来的 `session.gui`、`session.remote` 使用 `WECHAT_VNC_PORT`（默认 5909），与独立的一次性 link 分开。

## 中文输入验证

连接查看器后，先在微信输入框中点击取得焦点，再点击“测试中文粘贴”。启动器会调用目标端的 `ui.paste_text`，由 Linux X11 剪贴板写入中文并执行一次 `Ctrl+V`。这用于验证服务器环境的中文剪贴板链路；测试文本不会自动发送。

粘贴前会检查焦点是否属于可见微信窗口，否则返回 `FOCUS_UNVERIFIED`。连接建立后不要修改目标配置再执行粘贴；粘贴固定使用当前连接的目标。

### 本机验证结果

在 Windows TigerVNC 1.16.2、WSL Ubuntu 24.04、x11vnc 0.9.16 和 Xvfb `:99` 上实测：一次性凭据自动认证、WSL localhost 连接、英文按键、断开撤销通过。单独创建的 Linux Qt 文本窗口成功接收了命令通道粘贴的“你好世界中文输入”。Windows `VK_PACKET` Unicode 按键注入未被接收，VNC 中文剪贴板测试未通过；这不能代替真实 Windows 输入法的手动测试。当前微信尚未登录，因此微信聊天编辑器内的中文验证仍待登录后完成。第一版保留命令通道粘贴作为可用路径，不宣称原生 IME 已通过，也未验证 SSH 真实服务器连接。

可复现实验需要 WSL `python3-pyqt5` 和 Windows `pywinauto`。在目标显示屏上启动 `probe_qt.py --output /tmp/wechat-link-probe/input.txt`，再运行 `probe_windows.py --report probe-results.json`；该脚本只操作独立测试窗口，不发送微信消息。测试结束后关闭测试窗口。

原型暂不实现控制租约、自动化期间自动切换 view-only、文件收发和 noVNC。查看器与自动化共用输入设备，执行自动化任务时请暂时不要操作查看器。
