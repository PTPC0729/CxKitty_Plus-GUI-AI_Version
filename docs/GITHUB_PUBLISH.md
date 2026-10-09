# 将源码上传到 GitHub，并发布版本

## 1. 准备两组文件

- **源码目录**：包含本 README、main.py、cxapi/、tests/、docs/、依赖文件、打包脚本、LICENSE、xxt_helper.ico 和隐藏的 .github/、.gitignore 等。把该目录的内容作为仓库根目录。
- **Release 成品目录**：由 build_public_release.py 生成的 v0.5.0/，包含 EXE、便携 ZIP、源码 ZIP、SHA256SUMS.txt、RELEASE_NOTES.md 和通用验证结果。这些成品通过 Releases 上传，不加入源码仓库。

提交前不要在源码目录登录账号或保存个人设置。使用 Git 提交时 .gitignore 会排除常用私有数据；浏览器拖拽上传不会执行 .gitignore 规则，因此只能选择已整理的源码文件。不要上传父目录中的 session、config.yml、ai_credentials.json、dist 或历史记录。

## 2. 创建公开仓库

1. 登录 https://github.com ，点击右上角 “+” → “New repository”。
2. Owner 选择自己的账户，Repository name 可填 CxKitty-Desktop，也可自行命名。
3. Description 可填写“基于 CxKitty 的多任务桌面学习助手”。Visibility 选择 Public。
4. 不勾选初始化 README，不另选 .gitignore 和许可证模板：整理好的源码中已包含这些文件，且需要保留原来的 GPL-3.0 许可证。
5. 点击 Create repository。复制仓库的 HTTPS 地址，形如 https://github.com/你的用户名/CxKitty-Desktop.git。

## 3. 上传源码：Git 命令行（推荐）

安装 Git for Windows 后，在源码目录的空白处打开 PowerShell。下面命令中的路径、GitHub 用户名和邮箱需要替换成自己的值；不要在项目父目录执行 git add。

```powershell
Set-Location 'C:\Projects\CxKitty'
git init -b main
git config user.name '你的公开GitHub用户名'
git config user.email '你的GitHub无回复邮箱'
git add .
git status --short
git diff --cached --stat
```

若不希望提交记录公开私人邮箱，先在 GitHub Settings → Emails 开启 “Keep my email addresses private”，复制 GitHub 显示的 noreply 邮箱。user.name 和 user.email 会进入公开提交记录，因此使用准备公开的身份；这两条 git config 只作用于当前仓库。

确认暂存列表只含准备好的源码、测试、文档、许可证和图标，不含账号、设置、日志、EXE 等，然后执行：

```powershell
git commit -m "Release CxKitty desktop 0.5.0"
git remote add origin https://github.com/你的用户名/CxKitty-Desktop.git
git push -u origin main
```

首次 push 通常由 Git Credential Manager 打开浏览器，请登录并授权你自己的账户。GitHub 不接受账户密码作为 HTTPS Git 密码；不要把令牌写入 remote URL、源码或配置模板。

刷新仓库页面，确认根目录直接显示 main.py、README.md、cxapi/ 等，而不是多套一层源码文件夹。Actions 中的 Windows tests 工作流会自动运行；初次下载依赖可能需要几分钟。

若仓库已被初始化，先 clone 到另一个空目录，再将整理好的源码内容复制进去后提交，不要使用强制推送覆盖已有内容。

## 4. 不使用命令行：网页上传

源码体积小，也可在空仓库页面选择 “uploading an existing file”，或 Add file → Upload files：

1. 打开整理好的源码目录，选择其中的文件和子目录，包括 .github、.gitignore、.gitattributes、.dockerignore。
2. 把它们拖到网页上传区域，保留 cxapi/、tests/、docs/ 等层级。不要把压缩包当作源码上传。
3. 核对上传路径，填写提交说明，点击 Commit changes。若超过网页一次允许的文件数，可分批提交；每个源文件都远小于网页的单文件限制。

网页上传不会自动忽略个人数据，所以不要直接拖原来日常使用的项目目录。EXE 应上传到 Releases，而不是这里。

## 5. 发布 0.5.0 Release

1. 打开自己的仓库，点击右侧 Releases。没有入口时可直接在仓库网址后添加 /releases。
2. 点击 “Draft a new release” 或 “Create a new release”。
3. 在 Choose a tag / Select tag 输入 v0.5.0，选择创建新标签；Target 选择刚上传源码的 main。若该标签已存在，应确认它指向与 EXE 对应的源码提交，不要移动别人的既有标签。
4. Release title 填 “CxKitty 0.5.0 · 多任务桌面版”。
5. 打开成品目录的 RELEASE_NOTES.md，将内容复制进说明框。核对版本号。
6. 在附件区域上传：
   - CxKitty-0.5.0-windows-x64.zip（推荐给普通用户）
   - CxKitty-0.5.0-windows-x64.exe（单独 EXE）
   - CxKitty-0.5.0-source.zip（对应完整源码）
   - SHA256SUMS.txt（校验文件）
7. 如希望展示验证结果，还可附加 VERIFICATION.json；不要上传构建日志或从运行目录另行打出的包。
8. 正式版不勾选 “Set as a pre-release”，如界面提供可设置为最新版本。准备完成后点击 “Publish release”；尚未准备好可先 Save draft。
9. 发布后确认附件可以下载，版本标签指向正确源码。GitHub 还会自动提供该标签的 Source code (zip)/(tar.gz)，它们不同于手动附加的源码 ZIP，二者可以并存。

普通仓库网页上传的单文件大小限制不适用于 Release 附件。本项目 EXE 应始终从 Release 附件入口上传。

## 6. 检查下载是否完整

在下载目录打开 PowerShell：

```powershell
Get-FileHash .\CxKitty-0.5.0-windows-x64.exe -Algorithm SHA256
```

将 Hash 与 SHA256SUMS.txt 中对应文件的值比较，应完全一致。

## 7. 后续版本

修改并测试源码后，同步 app_identity.py、pyproject.toml 和发布说明中的版本号；新增/删除公开文件时更新 PUBLIC_SOURCE_FILES.txt。运行 build_public_release.py 生成新的完整成品，提交源码，再创建新的版本标签和 Release，例如 v0.5.1。不要用旧版本号覆盖旧附件，也不要将含自己账号数据的运行目录当作 Release。

项目原有 GPL-3.0 许可证、上游作者来源信息和与成品对应的源码应继续保留。本指南不要求公开你的私人邮箱、真实姓名、手机号或学习平台账号。

## 官方帮助

- 创建仓库：https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository
- 上传文件：https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository
- 发布 Release：https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository
- 保护提交邮箱：https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address
