# CxKitty · 多任务学习助手

基于 [SocialSisterYi/CxKitty](https://github.com/SocialSisterYi/CxKitty) 的桌面 GUI 扩展版本，保留终端入口、原有数据格式与 GPL-3.0 许可证。当前版本为 **0.5.0**。

本仓库提供完整源码、测试、依赖声明、图形化打包工具及公开版构建脚本。仓库不附带账号、Cookie、密码、个人配置、AI 密钥、题库、历史日志或学习进度。测试中的账号和题目均为合成数据。

## 功能

- 浏览器式多任务标签页，各页独立运行、暂停、停止、选择账号和调整设置。
- 新页沿用最近一次成功手动登录的账号；原有页保持自己的会话。
- 手机号密码登录、扫码登录与本地会话恢复；二维码按整数像素缩放，保留静区。
- 登录后自动加载课程，选课后自动加载章节；可指定起始章节，跳过已完成章节。
- 视频、文档、文字单选/多选/判断章节测验；支持 JSON、SQLite、REST 题库以及 Chat Completions 兼容的 AI 接口。
- 按平台最新进度续接视频，区分登录失效和资源权限错误，导出运行结果。
- 防止同账号同课程重复启动，保护多页共用的缓存与进度文件。
- 使用 `xxt_helper.ico` 作为 EXE、窗口和任务栏图标。

尚不支持短信/学号登录、课程考试、直播任务、图片题、填空和简答题。任务完成以平台最终核验为准；网络、平台接口及模型服务变化可能影响功能。

## Windows EXE 使用

在本仓库的 **Releases** 下载 Windows x64 版本。推荐下载 `CxKitty-0.5.0-windows-x64.zip`，解压到有写入权限的目录后运行 `CxKitty.exe`，无需安装 Python。也可单独下载 `.exe`。

公开版没有附带配置或账号。首次启动保持未登录状态，AI URL、密钥、模型和自定义 REST 地址为空；视频使用 1 倍速、58 秒上报间隔，任务间隔 15 秒。登录、选课和点击“开始处理”后才执行任务。使用 AI 或题库答题前需自行配置。

使用过程中，用户自己产生的配置、会话、密钥和记录保存在 EXE 旁边。更新时只替换 EXE，保留自己的数据。另存数据可用 `--data-dir` 或 `CXKITTY_DATA_DIR`。同一应用内可开多个任务页；不要用不同进程同时写同一数据目录。

## 从源码运行

推荐 Windows 10/11 x64、Python 3.12。依赖声明支持 Python 3.10～3.14；具体系统需满足对应 PySide6 版本要求。

下载并解压源码，或克隆你自己的仓库后，在含 `main.py` 的目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

之后可双击 `start.bat`。无须创建配置文件：程序会采用内置默认值；修改后点击“保存设置”才写入 `config.yml`。若偏好编辑 YAML，可先复制 `config.example.yml` 为 `config.yml`。AI 密钥通过界面写入单独的 `ai_credentials.json`，不要加入配置模板或提交到仓库。

终端入口：

```powershell
.\.venv\Scripts\python.exe main.py --cli
```

只有终端环境时可仅安装 `requirements-cli.txt`。Poetry 用户也可使用 `poetry install`、`poetry run python main.py`。

## 操作说明

完整的按键、配置项、AI 接口、章节起点、数据兼容和打包工具说明见 [使用说明](docs/USER_GUIDE.md)。

基本顺序：登录 → 等待自动加载课程与章节 → 调整任务及题库/AI 设置 → 选择起始章节 → 开始处理。点击顶部“＋ 新建页”可同时处理另一门课程。关闭标签会先停止该页，关闭整个窗口会停止所有页。

## 打包与公开发布

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
# 图形化打包工具
.\.venv-build\Scripts\python.exe packager.py
# 生成适合 GitHub Release 的公开成品（推荐）
.\.venv-build\Scripts\python.exe build_public_release.py
```

`build_public_release.py` 会构建并自检实际 EXE，然后在 `release/v0.5.0/` 生成单独 EXE、带许可证的便携 ZIP、对应源码 ZIP、SHA-256 校验文件和发布说明。不会发布配置、账号、密钥、缓存、进度、自检模拟数据或包含本机路径的构建记录。版本目录已经存在时拒绝覆盖；可用 `--output` 指定新位置。

图形化打包工具在公开版中默认关闭所有“携带数据”选项。手动勾选复制数据只适合个人迁移；不要把那种构建目录直接公开。通用构建后端及独立打包工具仍然保留：

```powershell
.\.venv-build\Scripts\python.exe build_backend.py build --output dist --mode onefile --no-config --no-banks
.\.venv-build\Scripts\python.exe build_packager.py
```

通用后端会在本地生成构建日志及默认配置；公开发布时请使用专用脚本导出的文件。

上传源码与发布 Release 的逐步说明见 [GitHub 发布指南](docs/GITHUB_PUBLISH.md)。`PUBLIC_SOURCE_FILES.txt` 是本次公开源码的文件清单；添加模块、文档或资源后，应同步更新清单，才能进入专用脚本生成的源码 ZIP。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install opencv-python-headless numpy
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py --self-check artifacts/source-check.json
```

测试使用临时目录、模拟课程和本机模拟 AI 接口，不登录真实账号、不调用付费模型、不提交真实课程。二维码解码测试依赖 OpenCV/NumPy，未安装时跳过；它们不是运行程序所必需。仓库附带 Windows 测试工作流。

## Docker / 终端

```bash
docker build -t cxkitty .
docker run --rm -it -v "$PWD/data:/data" cxkitty
```

容器仅提供终端界面，运行数据写入挂载的 `/data`，不将宿主机账号和配置打进镜像。题库相对路径也以 `/data` 为基准。

## 许可证与来源

本项目保留上游 [GPL-3.0 许可证全文](LICENSE) 和 [来源及修改说明](NOTICE.md)，并提供 [第三方依赖说明](THIRD_PARTY_NOTICES.md)。发布修改版时，请保留许可证、上游署名及修改说明，提供与 EXE 对应的完整源码。使用者须遵守目标平台规则并对自己的账号操作负责。
