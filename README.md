# cc-models

在终端里选择 CPA 模型，生成 Claude Code 的模型菜单。模型请求使用原始 ID，显示名称独立编辑。本工具不是代理，不修改 CPA 上游。

## 安装

**macOS（Homebrew）。** 目前还没有发布过 GitHub Release，也没有远端 tap 仓库。发布后，通过本地临时 tap 从 release 的 formula 资产安装（formula 随 release 上传，无需同步任何远端 tap）：

```sh
# 以 v0.1.0 为例，替换为实际已发布的版本
brew tap-new "$USER"/cc-models
mkdir -p "$(brew --repository "$USER"/cc-models)/Formula"
curl -fsSL -o "$(brew --repository "$USER"/cc-models)/Formula/cc-models.rb" \
  https://github.com/toRolex/cc-models/releases/download/v0.1.0/cc-models.rb
brew install "$USER"/cc-models/cc-models
```

原理：release workflow 在推送 `v<版本>` 标签时构建并校验 sdist/wheel，从本次构建的真实产物生成 `Formula/cc-models.rb` 与 `dist/checksums.txt` 并随 release 上传。formula 的 url 指向 `https://github.com/toRolex/cc-models/releases/download/v<版本>/cc_models-<版本>.tar.gz`，其 sha256 就是该 sdist 本身的校验和，因此从 release 资产安装不存在哈希错位问题。仓库里提交的 `Formula/cc-models.rb` 只是最近一次本地生成的快照（供 `--check` 门与阅读），不是 tap。上述命令在首次 Release 发布前会因下载 404 失败；任意平台可先用下面的本地源码安装。

**本地源码安装，无需发布 Release。** 当前仅验证 Unix 环境。 从源码直接安装可用的命令行入口：

```sh
pipx install .
# 或
uv tool install .
```

安装后直接使用 `cc-models` 命令：

```sh
cc-models --demo
```

开发环境用 uv 同步依赖：

```sh
uv sync --group dev
```

演示模式只使用临时配置和固定目录，不访问网络。退出后不影响真实 Claude Code 配置。

## 连接上游

先在 shell 中设置 `CPA_API_KEY`。不要把密钥提交进仓库。可以用隐藏输入避免密钥出现在命令历史里。

```sh
read -s CPA_API_KEY
export CPA_API_KEY
cc-models
```

默认读取 `~/.claude/settings.json`。配置文件必须已存在，且是严格 JSON 对象。启动时仅在本地状态文件不存在时导入已有 `modelPicker`。本地状态默认保存在 `~/.cc-models`。

在界面按 `c` 设置上游地址和令牌环境变量名称，然后按 `r` 拉取目录。地址可以填写服务根路径或 `/v1` 路径。令牌值不保存在本地状态。

命令行也可临时覆盖地址。所有全局选项放在子命令之前。

```sh
cc-models --base-url https://example.com/v1 refresh
cc-models --settings /path/to/user-config.json --state-dir /path/to/local-state list
```

非回环 HTTP 地址必须显式允许明文传输。TUI 配置提供确认选项，CLI 使用 `--allow-insecure-http`。不要向不可信地址发送凭据。重定向被拒绝。

若没有环境变量凭据，可以使用所选配置中的 `env.ANTHROPIC_AUTH_TOKEN` 或 `env.ANTHROPIC_API_KEY`，但仅在上游地址与该配置的地址相同时回退。

## 管理模型

顶部一行显示 `cc-models — CPA 模型选择器` 标题，背景保持终端默认。

- `j` / `k` 在表格中下移或上移。搜索框和弹窗获得焦点时，这两个键仍作为普通输入字符。
- `/` 聚焦搜索框，Esc 返回列表。
- 空格勾选或取消选择，焦点会跟随原模型 ID 而不是跳回首行。
- `e` 同时编辑已选模型的显示名称和描述。描述留空会清除描述，模型 ID 不变。
- `Ctrl+P` 打开命令/主题面板，选择 Theme 使用 Textual 原生主题选择器。面板只列出保留终端默认背景的 ANSI 主题（内置 `ansi-dark`、`ansi-light` 与默认的 `cc-models-mocha-ansi`）；绕过面板直接赋值非 ANSI 主题（如 dracula、latte）会被拒绝。默认主题使用终端默认背景和 Catppuccin Mocha 前景配色，局部控件保留实色背景。切换仅对当前进程生效，重启后恢复此默认主题。
- Ctrl+↑ 和 Ctrl+↓ 调整精选顺序。
- `d` 设置默认模型，再按一次恢复为保留原默认。
- `c` 打开端点配置，设置上游、令牌变量名称和是否替换内置模型列表（与命令/主题面板区分）。
- `r` 刷新模型目录。
- `p` 预览受管字段变更。
- `a` 打开应用确认弹窗。
- `q` 退出。

编辑精选列表会保存本地意图，但不会自动写入 Claude Code。上游新增模型不会自动选中，缺失的已选模型仍保留。刷新失败不清空选择。空精选列表禁止应用。

应用只更新 `modelPicker` 和显式设置的默认 `model`。其它配置值保持不变，但 JSON 缩进和格式可能变化。显示名称只控制菜单，不保证状态栏和所有提示都使用名称。

## 预览、备份和恢复

```sh
cc-models list
cc-models refresh
cc-models apply --dry-run
cc-models apply --yes
cc-models backups
cc-models restore --backup /path/to/backup --yes
```

应用前在配置文件旁创建权限为 0600 的备份，再原子替换。恢复会还原整个配置文件，包括非受管字段，并先备份当前内容。恢复入口目前是 CLI。

同一配置目标的本工具实例共用写锁。内容变化会拒绝应用。CC Switch 等不遵守该锁的程序仍可能在最终检查后抢写，所以不要同时使用两个配置管理器。JSONC 注释不支持，非法文件不会被覆盖。

本工具只请求模型目录，不发送聊天或模型能力测试请求。模型调用是否可用仍由 CPA 上游决定。

## 质量门

`./scripts/verify.sh` 统一执行以下门，任何一步失败即停止（不降级、不 continue-on-error）：

1. **锁定同步**：`uv sync --locked`，所有 uv 调用取消环境变量 `UV_INDEX`（可能是明文 HTTP 源）并加 `--locked` 防止后续命令重新解析依赖；开发工具（pytest、ruff、pyright）全部锁定在 `dev` 依赖组，不临时 `uv --with` 安装。
2. **Lint**：`ruff check .`。
3. **格式检查**：`ruff format --check .`。
4. **类型检查**：`pyright`，`typeCheckingMode = "basic"`（非 off，非 strict），`include = ["src", "tests", "scripts"]`，`pythonVersion = "3.11"`；配置在 `pyproject.toml` 的 `[tool.pyright]`，不使用全局 ignore 或 `report*=none` 消错。
5. **构建**：`uv build` 输出到固定的 `dist/`（先清空），在 pytest 之前执行，保证安装型测试、formula 生成与 release 上传使用同一批产物。
6. **验包**：`scripts/check_dist.py` 校验 sdist/wheel：文件名版本与 `pyproject` 一致且无本地版本段、sdist 条目不超白名单、不含本地/机器元数据（.jj/.git/.agents/.pi/.venv/__pycache__/direct_url.json 等）、wheel 含全部必要 `cc_models` 模块。不是仅 twine metadata 检查。
7. **formula 生成**：`scripts/gen_formula.py` 从 `dist/` 真实产物重新生成 `Formula/cc-models.rb` 与 `dist/checksums.txt`（formula 摘要即本次构建 sdist 的 sha256）。
8. **测试**：`pytest -q`（其中安装型测试用 `uv pip install` 把 `dist/` 里的真实 wheel 装进一次性 venv，并断言打包后的 `cc-models` 入口）。

测试配套见 `tests/test_check_dist.py`，含本地元数据、超白名单、本地版本段、缺模块等负例。

## CI

GitHub Actions（`.github/workflows/ci.yml`）：`pull_request` / `push main` / `workflow_dispatch` 触发；Ubuntu 上 Python 3.11 与 3.13 matrix；`contents: read` 最小权限；`concurrency` 取消同分支旧 run；`actions/checkout` 与 `astral-sh/setup-uv` 固定 commit SHA；调用同一 `./scripts/verify.sh`；设合理 timeout，无 continue-on-error，不使用 pull_request_target。

## 验证

```sh
./scripts/verify.sh
```

测试使用临时配置、演示数据和本机测试 HTTP 服务，不修改真实配置。Textual Pilot 覆盖搜索、j/k 键位、勾选后的焦点身份、改名和清除描述、主题切换、默认模型、预览取消、确认应用和配置保存。
