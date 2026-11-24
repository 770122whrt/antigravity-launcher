# Antigravity 代理检测启动器

分享版 2026-11-24 · Windows · 中文界面 · 模型探测集成在同一窗口

这个工具先检查代理能否访问 Google，再通过官方 Antigravity CLI 发起真实 Gemini 短请求。通过验证后，它给 Antigravity 桌面端及后台子进程设置进程级代理并启动客户端。主窗口也可以单独探测账号可选的 Gemini 模型。

## 1. 准备和安装

使用者需要自行准备以下组件及自己的登录信息。

| 组件 | 要求 |
| --- | --- |
| Windows | 10 或 11，使用有写入权限的用户目录 |
| Python | 3.10、3.11 或 3.12，包含 Tkinter 和 venv；本包依赖固定为 psutil 6.0.0 |
| Antigravity 桌面端 | 已安装的 Google 官方客户端 |
| Antigravity CLI | Google 官方独立 CLI，默认文件为 %LOCALAPPDATA%\agy\bin\agy.exe |
| 代理工具 | 已启用本机 HTTP 系统代理，通常为 SakuraCat 或 Clash Verge/Mihomo |
| 账号 | 由本人完成官方登录，模型可用范围和额度以该账号实际结果为准 |

Python、Antigravity 桌面端、官方 CLI 和 Mihomo 由各自官方渠道安装。Python 安装时保留 Tcl/Tk 支持。官方入口见文末。

1. 将整个 ZIP 解压到固定的可写目录，例如 E:\code for project\antigravity-launcher。先完成解压，再运行文件。
2. 双击“安装桌面入口.cmd”。脚本会选择已安装的 Python，在当前包目录创建 .venv，联网安装 psutil，并创建桌面“Antigravity 代理检测启动器”快捷方式。
3. 打开代理工具，启用“系统代理”。纯 PAC、仅 SOCKS、仅开启 TUN 的配置可能无法被识别。
4. 双击桌面快捷方式。启动器会立即开始验证，真实模型探测会使用少量账号额度。

安装脚本使用普通用户权限。它写入包目录和当前用户桌面。若同名快捷方式已经指向另一份安装，会停止并提示先重命名旧快捷方式。移动整个包目录后，应重新创建快捷方式。

安装依赖失败时，查看控制台的 pip 错误，确认终端能访问 PyPI 后重跑。已有 Python 的高级用户也可以在包目录运行“python install.py”。

## 2. 日常怎么用

- 先打开代理工具，再打开启动器。验证成功会自动启动 Antigravity。
- 提示 CLI 需要登录时，点“登录官方 CLI”，由本人完成浏览器授权。登录成功后正常退出 CLI，启动器会继续检测。
- Antigravity 已经打开时，先保存工作并正常退出，再让启动器重新启动它，进程才会继承本次代理设置。
- 想检查某个模型，在主窗口点“刷新列表”，选择型号，再点“探测所选模型”。结果和耗时显示在同一份日志中。“取消探测”取消这次检查。
- 点击窗口 ×，空闲时退出，检测中取消后退出。正在提供代理时，可选“完全退出并断开代理”或“关闭窗口并保留连接”。选保留连接后，可用原桌面快捷方式恢复窗口。
- 正常退出 Antigravity 后，本次辅助代理会清理。完全退出启动器也会断开它承载的客户端连接。

启动验证默认请求 gemini-3.8-flash-low。探针下拉框用于单次检查；桌面端自己的模型选择和启动验证型号分别设置。账号没有默认型号时，可按第 5 节指定 AGY_TEST_MODEL。

## 3. 探测原理

本地 HTTP 转发入口 → 独立 Mihomo 或已有系统代理 → Google 服务。

| 检查层 | 实际操作 | 可以得出的结论 |
| --- | --- | --- |
| 网络 | 请求 Google 的 generate_204，预期 HTTP 204 | 该代理能到达这个 Google 地址 |
| 条款版本 | 匿名读取 policies.google.com/terms?hl=en 的 Country version | Google 为此次请求默认展示的条款版本 |
| 模型 | 官方 agy 显式指定 Gemini 型号，要求只回复 AGY_OK | 此账号、型号、线路在本次请求中能否真正生成回复 |
| 路由证据 | 关联回环连接的进程和 CONNECT 目标；独立模式查询 Mihomo 连接链 | 相应进程是否连接了专用代理，以及可观测的节点链 |

启动验证要求 CLI 返回 SUCCESS、退出码为 0、回复恰好为 AGY_OK，并观察到 CLI 模型服务的代理连接。独立模型探针也会检查这条路由证据，缺少证据时明确提示。

桌面启动使用 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、grpc_proxy 和 Chromium 代理参数。TLS 使用系统信任链验证，转发器通过 CONNECT 转发加密流量。它不解密请求体。

## 4. 节点选择、超时和限制

启动时先尝试上次成功的节点，再尝试可识别的当前选中节点及其他候选，一轮最多 3 条。每次仍会重新验证。明确的网络错误或地区错误可以触发下一候选；登录、资格、额度、容量以及未明确归类的失败会结束本轮。

模型请求的 CLI 时限为 45 秒，外层总时限为 65 秒。模型列表读取限时 24 秒。清理、读取代理配置和客户端初始化另需时间。取消列表读取可能要等本次限时结束。

独立模式复用本机配置中的节点，由新启动的 Mihomo 负责读取。核心只监听回环地址，禁用自己的 TUN，并尽可能绑定物理默认出口。识别不到兼容配置时，启动器回退到“现有系统代理（共享入口）”，此时只能检查当前线路。

当前自动识别两个配置位置：

- %USERPROFILE%\.config\com.vortex.helper\config.yaml
- %APPDATA%\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml

独立模式需要找到可执行的 Mihomo 核心，配置中有内联 proxies，并且 mixed-port 匹配当前系统代理。只含远程 proxy-providers、带控制接口认证、复杂代理组、企业 VPN、多网卡或非默认安装，可能识别不完整。可用第 5 节的路径覆盖或使用共享入口。

条款页显示 China，甚至手动切成 United States，都不足以判断 Gemini 是否能用。判断以真实模型请求为准。某个模型成功也只代表该型号当时的短请求成功；长上下文、工具调用、其他型号和持续对话需要另行验证。

| 现象 | 处理 |
| --- | --- |
| No capacity available、503 | 服务端报告容量不足，稍后重试或探测其他型号 |
| 超时、一直 Working | 查看具体模型的探测结果；单次超时只能证明未在时限内完成 |
| 401、invalid_grant、需要登录 | 在“登录官方 CLI”中重新授权 |
| oauth2.googleapis.com/token 连接超时 | 核对登录进程是否经代理启动，并确认代理能连接登录接口 |
| 地区限制 | 根据该账号及服务可用范围排查；成功的网页检查不能推翻模型接口拒绝 |
| 429、quota、resource exhausted | 根据返回信息处理额度或限流，等待恢复 |
| 非回环 TCP 连接提示 | 该连接未经过专用回环端口；系统 TUN 可能另行承载，路由证据尚不完整 |
| 找不到客户端、CLI 或节点 | 检查安装位置、系统代理和下表的路径配置 |

## 5. 自定义安装路径

一般使用默认自动识别即可。特殊安装可在 Windows“用户环境变量”里配置，再完全退出并重开启动器。

| 变量 | 内容 |
| --- | --- |
| AGY_DESKTOP | Antigravity.exe 的完整路径 |
| AGY_CLI | 官方独立 agy.exe 的完整路径 |
| AGY_MIHOMO | mihomo.exe 或 verge-mihomo.exe 的完整路径 |
| AGY_PROXY_CONFIG | 含内联 proxies 和 mixed-port 的兼容本地 YAML 文件路径 |
| AGY_TEST_MODEL | 启动验证使用的 Gemini 型号 ID，可从探针刷新出的列表中选择 |

例如把 AGY_CLI 设置为 D:\Apps\agy\agy.exe。只填写自己安装的官方程序路径。更新客户端或 CLI 后，如果参数或接口发生变化，需要重新验证兼容性。

## 6. 本地数据、分享和卸载

运行后，包目录会产生“上次成功.json”“运行记录.txt”和“检测工作区”。日志可能包含节点名称、型号及错误类别。官方 CLI 还可能在自己的用户数据目录保存会话记录。

每个接收者使用自己的代理和账号。这个发布 ZIP 只包含启动器源码、安装脚本、依赖清单和本文档。再次转发时优先发送原始 ZIP；若从使用过的目录重新打包，应排除 .venv、__pycache__、日志、成功节点记录、检测工作区及备份。

辅助代理临时目录位于 %TEMP%\AntigravityLauncher-Mihomo，正常退出时清理。异常结束后，下次启动会尝试清理可验证归属的旧目录。

恢复普通启动时，先正常退出客户端和启动器，再使用 Antigravity 原有入口。卸载本工具时，关闭它的窗口及后台代理，删除桌面快捷方式和解压目录即可。Python、代理工具、Google 软件与登录信息由各自软件管理。

## 7. 包版本和验证范围

本包基于已有 Windows 启动器源码制作，包含模型探测整合与关闭行为修复。分享版移除了原机器专用的 Mihomo 路径，并增加第 5 节的四个路径/型号覆盖项（AGY_MIHOMO 原已支持）。本机数据与历史备份由 .gitignore 排除；转发时使用 dist 中的分享 ZIP。

打包验证覆盖 Python 语法、依赖、导入、主窗口控件、路径覆盖和 ZIP 内容排查。已实际完成隔离环境安装，并在临时桌面目录创建、回读验证快捷方式。主窗口的列表、结果回传和取消使用离线模型替身验收。此前本机已实测代理、CLI 回复、桌面多轮对话和退出清理。接收者首次运行仍需在自己的网络和账号下验证。

官方参考资料，核对日期为 2026-11-24：

- [Python Windows 下载及版本说明](https://www.python.org/downloads/windows/)
- [Antigravity 官方下载](https://antigravity.google/download)
- [Antigravity CLI 安装与登录](https://antigravity.google/docs/install?tab=cli)
- [官方 CLI headless 模式](https://antigravity.google/docs/cli/headless)
- [Mihomo 项目](https://github.com/MetaCubeX/mihomo)
