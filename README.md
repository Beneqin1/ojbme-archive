# BME OJ 提交归档器

把 [oj.bmeonline.cn](http://oj.bmeonline.cn) 上**你自己账号（或已获授权账号）**的正确（Accepted / AC）提交源码抓取到本地，按题目去重、命名，并生成一个可以直接 `git init`、上传到 GitHub 的项目目录。

> 本工具**不会**执行 `git push`，**不会**执行 `gh repo create`，**不会**创建或修改任何远程仓库。
> 它只读取 OJ 的公开页面和你提供的 Cookie，只写本地文件。

---

## 安全边界（请先读）

1. **只抓取本人账号或已明确授权的提交。** 不要用它批量下载他人的代码。
2. **不绕过登录、验证码、权限控制。** 源码页需要登录时，工具只会提示你配置 Cookie；不会猜接口、不会伪造权限。
3. **不自动公开他人代码。** 生成的仓库默认在本地；是否上传、仓库设为公开还是私有，由你手动决定。
4. **遵守 OJ 服务条款、课程要求和学术诚信规范。** 提交源码的版权归原作者/题目提供方，本工具不主张版权。
5. **Cookie 等同密码。** 不要把它写进 `config.toml`、README、命令行历史、截图或任何会提交的文件。

---

## 快速开始

### 环境

- Windows 10/11
- Python **3.8+**（仅标准库，**不需要 pip install**）
- 可选：Git for Windows（只有 `--init-git` 才需要）

> TOML 配置文件需要 Python 3.11+ 自带的 `tomllib`；3.8–3.10 可安装 `tomli`，或者干脆不用配置文件、全部走命令行参数。

### 发行包目录结构

```text
OJ/
├─ run.bat                 # 双击启动
├─ README.md
├─ LICENSE
├─ .gitignore
├─ .gitattributes
├─ files/
│  ├─ bmeoj_archive.py     # 核心脚本
│  ├─ config.example.toml
│  └─ requirements.txt
├─ output/                 # 默认输出目录（空）
└─ tests/
   └─ test_offline.py      # 可选，离线测试
```

`run.bat` 会自动在根目录或 `files\` 下寻找 `bmeoj_archive.py`。源码布局（脚本在根目录）和发行包布局都能直接运行。

### 方式一：双击 `run.bat`

1. 双击 `run.bat`。
2. 按提示输入 **OJ 用户名**。
3. 按提示输入 **目标文件夹**（直接回车 = `output`）。
4. 等待抓取完成。窗口会显示新增/更新/跳过的题目数量。

`run.bat` 会自动寻找 Python（`BMEOJ_PYTHON` → `.venv` → `py -3` → `python`），设置 UTF-8，并在结束时暂停。

### 方式二：命令行

```bat
run.bat --user 11226128 --dir output
```

直接调用 Python（发行包中脚本位于 `files\`；源码布局去掉 `files\`）：

```bat
python files\bmeoj_archive.py --user 11226128 --dir output
```

### 先探测、不下载

```bat
run.bat --probe --user 11226128
```

探测模式会检查：用户是否存在、提交列表能否解析、源码页是否需要 Cookie，并把关键页面结构打印出来。它**不会**写入任何提交文件。

---

## 配置 Cookie

没有 Cookie 时，工具仍然可以列出公开的提交记录，但**无法读取源码**（OJ 会返回 `I am sorry, You could not view this code!`）。此时脚本会给出明确提示并以退出码 `4` 结束，不会崩溃。

本工具**不实现登录流程**，也不需要 CSRF token：它只复用你浏览器里已经登录的 Cookie，不会提交账号密码。

### 从浏览器复制

1. 用浏览器登录 `http://oj.bmeonline.cn/`。
2. 按 `F12` 打开开发者工具。
3. 打开 **Network（网络）**，刷新任意页面，点一个请求。
4. 在 **Request Headers（请求头）** 里找到 `Cookie:`，复制整行值（通常形如 `PHPSESSID=...`）。
5. 不要把 Cookie 粘贴到聊天、Issue、截图或代码里。

### 三种配置方式（任选其一）

**A. 环境变量（推荐，临时）**

```bat
set BMEOJ_COOKIE=PHPSESSID=你的值
run.bat --user 11226128
```

PowerShell：

```powershell
$env:BMEOJ_COOKIE = "PHPSESSID=你的值"
.\run.bat --user 11226128
```

**B. `config.local.toml`（推荐，长期）**

在**脚本所在目录**新建 `config.local.toml`（发行包为 `files\config.local.toml`；已被 `.gitignore` 忽略）：

```toml
[oj]
cookie = "PHPSESSID=你的值"
```

**C. `config.toml` 里禁止放 Cookie**

脚本检测到 `config.toml` 出现 `cookie` 或 `password` 会直接报错退出，避免误提交。

优先级：**命令行参数 > 环境变量 > `config.local.toml` > `config.toml` > 内置默认值**。

---

## 输出目录

默认目标目录是**工具根目录**（`run.bat` 所在目录）下的 `output/`：

```text
output/
├─ 1000/
│  └─ 1000 PA.cpp
├─ 1011/
│  └─ 1011 PA.cpp
├─ README.md
├─ manifest.json
├─ manifest.csv
├─ .gitignore
├─ topics.txt
├─ repo-description.txt
├─ config.example.toml
├─ LICENSE              # 仅 --license 时生成
├─ logs/
│  └─ archive_YYYYMMDD-HHMMSS.log
└─ _backup/             # 覆盖旧文件前的备份
```

- `logs/`、`_backup/` 已写入 `.gitignore`，不会进入仓库。
- 已存在的 `README.md` 如果不是本工具生成的，会先备份到 `_backup/` 再重新生成。

---

## 命名规则（重点）

旧脚本的文件形如：

```text
BME_OJ/1000/1000 PA.cpp
BME_OJ/1005/1005 PD.cpp
```

其中：

- `1000`、`1005` 是 OJ 的 **cid（题号组 ID）**；
- `PA`、`PD` 是 **P + 小题字母**（`pid=0 → A`，`pid=1 → B`，……）。

所以默认模板是：

```toml
filename_template = "{id} P{part}.cpp"
```

生成 `1000/1000 PA.cpp`，与旧文件一致。

> 你提到的字面写法 `"{id}x P{id}.cpp"` 会得到 `1000x P1000.cpp`，与旧文件不一致，因此**没有**作为默认值。如果你确实想要这种字面格式，改配置即可。

### 可用变量

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `{id}` | cid（题号组 ID） | `1011` |
| `{pid}` | 组内序号，0 起 | `0` |
| `{part}` | 小题字母 | `A` |
| `{letter}` | `{part}` 的别名 | `A` |
| `{problem_id}` | OJ 全局题目编号 | `1084` |
| `{title}` | 题名 | `数组是否有序` |
| `{slug}` | 题名的 ASCII slug | `array-order` |
| `{user}` | OJ 用户名 | `11226128` |
| `{submit_id}` | 提交 ID | `86958` |
| `{date}` | 提交日期 | `2026-11-21` |

### 文件名清洗与冲突

- Windows 非法字符 `< > : " / \ | ? *` 和控制字符会替换成 `_`。
- 同一目标文件夹内重名默认**跳过并写日志**；加 `--overwrite` 才覆盖。
- 同一次运行内如果两个题目生成同名文件，会自动追加 `_sid<提交ID>`。
- `--all` 模式下，如果模板里没有 `{submit_id}`，会自动追加 `_sid<提交ID>`，避免同题多次 AC 互相覆盖。

### 目录布局

```toml
layout = "problem"       # <目标>/<cid>/<文件名>，默认，与旧版一致
# layout = "submissions" # <目标>/submissions/<文件名>
# layout = "flat"        # <目标>/<文件名>
```

---

## 常用命令

```bat
:: 交互式（双击 run.bat 也可以）
run.bat

:: 指定用户和目标目录
run.bat --user 11226128 --dir output

:: 保留全部 AC（不只每题最新一次）
run.bat --user 11226128 --all

:: 强制重新下载并覆盖（会先备份旧文件）
run.bat --user 11226128 --force

:: 允许覆盖本地已有 .cpp（不强制重下未变化的提交）
run.bat --user 11226128 --overwrite

:: 只列计划，不下载、不写文件
run.bat --user 11226128 --dry-run

:: 探测页面结构 / 检查 Cookie
run.bat --probe --user 11226128

:: 指定要探测的提交
run.bat --probe --user 11226128 --sid 86958

:: 只处理前 3 道题（调试）
run.bat --user 11226128 --limit 3

:: 生成本地 git 仓库（不 push、不建远程库）
run.bat --user 11226128 --init-git

:: 额外生成 LICENSE（MIT，仅覆盖工具/文档）
run.bat --user 11226128 --license

:: 保留 OJ 追加的 /***** 尾部注释（默认删除）
run.bat --user 11226128 --keep-footer

:: 不抓题名（网络慢/离线调试时用占位标题）
run.bat --user 11226128 --no-titles
```

完整参数：`run.bat --help`。

---

## 增量、`--force` 与 `--overwrite`

默认行为：

1. 读取目标目录的 `manifest.json`。
2. 只抓取 **AC / Accepted** 的提交。
3. 每题只保留**最新一次** AC。
4. 如果清单里有该题、文件存在、提交 ID 未变 → **跳过**（不会重复下载）。
5. 如果本地已有文件但提交 ID 变了：
   - 没有 `--overwrite` / `--force` → **跳过**，并在日志里提示；
   - 有 `--overwrite` 或 `--force` → 覆盖，覆盖前备份到 `_backup/<时间戳>/`。
6. `--force` 会忽略“未变化”判断，重新下载并覆盖。

`--force` 隐含 `--overwrite`。

> 从 `--all` 切回默认模式时，旧的多余清单项不会自动删除；需要干净的历史时，删掉 `manifest.json` 重新跑一次即可（已下载的 `.cpp` 不会被自动删除）。

---

## `manifest.json` 字段

```json
{
  "schema_version": 1,
  "tool": { "name": "bmeoj_archive.py", "version": "1.0.0" },
  "generated_at": "2026-11-23T12:00:00+08:00",
  "source": {
    "base_url": "http://oj.bmeonline.cn",
    "user": "11226128",
    "mode": "latest",
    "layout": "problem",
    "filename_template": "{id} P{part}.cpp"
  },
  "stats": { "problems": 71, "submissions": 71, "bytes": 123456 },
  "entries": [
    {
      "problem": "1011 PA",
      "cid": "1011",
      "pid": 0,
      "part": "A",
      "problem_id": "1084",
      "title": "数组是否有序",
      "submit_id": "86958",
      "submitted_at": "2026-11-21 20:19:28",
      "language": "G++",
      "status": "Accepted",
      "filename": "1011/1011 PA.cpp",
      "problem_url": "http://oj.bmeonline.cn/problem.php?cid=1011&pid=0",
      "source_url": "http://oj.bmeonline.cn/showsource.php?id=86958",
      "sha256": "…",
      "bytes": 512,
      "fetched_at": "2026-11-23T12:00:00+08:00"
    }
  ]
}
```

`manifest.csv` 是同一份数据的表格版（UTF-8 with BOM，方便 Excel 打开）。

---

## `--init-git` 与手动上传 GitHub

`--init-git` **只做三件事**：

```text
git init
git add -A
git commit -m "Add BME OJ accepted submissions for <user> (YYYY-MM-DD)"
```

没有 `git push`，没有 `gh repo create`，没有远程仓库操作。

### 1. 本地初始化

```bat
run.bat --user 11226128 --dir output --init-git
```

如果本机没有配置 git 身份，脚本会用：

```text
git -c user.name=<用户名> -c user.email=<用户名>@users.noreply.github.com commit ...
```

只对这次 commit 生效，不写全局配置。

### 2. 在 GitHub 网页手动创建空仓库

1. 打开 <https://github.com/new>。
2. 填写仓库名，例如 `bmeoj-11226128`。
3. **不要**勾选 “Add a README file”“Add .gitignore”“Choose a license”（本地已经有了）。
4. 公开还是私有由你决定；不确定时选 **Private**。
5. 点击 **Create repository**。

### 3. 手动添加远程并 push

在目标目录执行（把 `<你的GitHub用户名>` 和仓库名换成实际值）：

```bat
cd output
git remote add origin https://github.com/<你的GitHub用户名>/bmeoj-11226128.git
git branch -M main
git push -u origin main
```

> `--init-git` 结束时也会打印这三条命令，但**不会**替你执行。push 需要你的 GitHub 凭据，应该由你本人在终端完成。

---

## 配置文件

复制 `config.example.toml`（发行包位于 `files\`）为同目录下的 `config.toml`（可提交），按需修改。常用项：

```toml
[oj]
base_url = "http://oj.bmeonline.cn"
request_interval = 1.5   # 秒；真实站点强制 >= 1.0
timeout = 30
retries = 3

[archive]
directory = "output"
layout = "problem"
filename_template = "{id} P{part}.cpp"
mode = "latest"           # latest | all
overwrite = false
strip_footer = true
write_license = false

[files]
write_manifest_csv = true
write_repo_files = true
```

`config.local.toml` 与脚本同目录，只放私有内容（Cookie），不要提交：

```toml
[oj]
cookie = "PHPSESSID=..."
```

---

## 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 退出码 `4`，提示需要 Cookie | 未配置 Cookie，或 Cookie 已过期。按上文重新复制；确认 Cookie 属于目标用户。 |
| `I am sorry, You could not view this code!` | 未登录、Cookie 失效，或该提交不属于当前账号。工具不会绕过权限。 |
| 退出码 `5`，用户不存在 | 检查用户名；`userinfo.php` 返回 `No such User!`。 |
| 提示“没有 AC/正确提交” | 该用户确实没有 AC，或提交被隐藏。先用浏览器确认。 |
| 题名是“题 1011 PA”占位符 | 题名页抓取失败。可用 `--probe --dump-html` 查看页面；不影响源码归档。 |
| 页面解析为 0 行 | OJ 页面结构可能变化。运行 `--probe --dump-html`，把 `_debug/` 里的 HTML 拿出来对照 `parse_status_page()`，不要猜接口。 |
| 请求超时 / 429 / 403 | 调大 `--interval`（如 `--interval 3`），减少并发；工具本身是串行的。 |
| `config.toml` 解析失败 | 需要 Python 3.11+ 的 `tomllib`；或安装 `tomli`；或删掉配置文件改用命令行参数。 |
| 中文乱码 | 用 `run.bat`（已 `chcp 65001`）；或先执行 `chcp 65001`。 |
| `git` 未找到 | 安装 Git for Windows，或把 `git.exe` 加入 PATH。 |
| 覆盖后想找回旧文件 | 看 `_backup/<时间戳>/`。 |

退出码：`0` 成功 / `2` 配置或参数 / `3` 网络 / `4` Cookie 或权限 / `5` 用户不存在 / `130` 用户取消。

---

## 从旧版迁移

- 旧 `auto_update.bat` 已被替换为**无凭据的兼容入口**：它只把参数转发给 `run.bat`，不再自动同步、不再自动 push。
- 旧 `files/oj_sync.py`、`files/git_push.py` 不再被调用；`files/` 里的历史文件保持原样。
- 旧脚本默认写入 `files/.../BME_OJ/<cid>/<cid> P<字母>.cpp`；新脚本默认写入 `output/<cid>/<cid> P<字母>.cpp`，命名规则一致。
- **重要安全提醒**：旧 `auto_update.bat` 和 `files/_oj_credentials.txt` 里曾有明文 OJ 凭据。请尽快在 OJ 修改密码，并删除/清空该凭据文件；不要把它们提交到任何仓库。
- 旧脚本“已存在 .cpp 一个字都不改”的行为保留为默认；只有显式 `--overwrite` / `--force` 才会覆盖。

---

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `run.bat` | Windows 启动器：找 Python、设 UTF-8、交互提示、透传参数。 |
| `files/bmeoj_archive.py` | 核心脚本：抓取、解析、去重、写文件、生成清单和仓库文件（源码布局中位于根目录）。 |
| `files/config.example.toml` | 公共配置示例（源码布局中位于根目录）。 |
| `files/requirements.txt` | 说明零必需依赖；可选依赖仅作注释。 |
| `README.md` | 本文件。 |
| `LICENSE` | MIT，仅覆盖工具与文档，不覆盖任何提交源码。 |
| `.gitignore` | 忽略 Cookie、日志、缓存、备份、输出内容等。 |
| `.gitattributes` | 保持 `run.bat` 为 CRLF，其余文本为 LF。 |
| `output/` | 默认输出目录（空，保留 `.gitkeep`）。 |
| `tests/test_offline.py` | 离线 mock OJ 测试，不需要联网。 |

> 发行包**不包含**旧 `files/` 里的历史脚本、调试文件、缓存和凭据，也不包含旧 `output/*.docx`。
> 不要把旧工作区整目录复制进来；旧 `files/_oj_credentials.txt` 和旧 `auto_update.bat` 曾含明文凭据，应删除或轮换。

运行测试：

```bat
python -m unittest discover -s tests -v
```

---

## 免责声明

- 本工具仅用于**个人学习记录与备份**，仅应抓取**本人账号或已获授权账号**的提交。
- 提交源码的版权归**原作者 / 题目提供方**所有；本工具及生成的 README 不主张对这些代码的版权。
- 工具本身的脚本与文档采用 MIT 许可（`--license` 生成的 `LICENSE` 会明确：该许可**不覆盖**任何提交源码）。
- 请遵守 oj.bmeonline.cn 的服务条款、课程要求和学术诚信规范。**不要**把他人的提交用于抄袭、代写、刷分或其他违规用途。
- 如果你是权利人并希望删除某段代码，请联系仓库所有者或提交 Issue。
