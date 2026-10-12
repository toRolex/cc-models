# 开发与质量门

本页面向贡献者，描述 `./scripts/verify.sh` 的每一道门和 CI 配置。

## 质量门

`./scripts/verify.sh` 统一执行以下门，任何一步失败即停止（不降级、不 continue-on-error）：

1. **锁定同步**：`uv sync --locked`，所有 uv 调用取消环境变量 `UV_INDEX`（可能是明文 HTTP 源）并加 `--locked` 防止后续命令重新解析依赖；开发工具（pytest、ruff、pyright）全部锁定在 `dev` 依赖组，不临时 `uv --with` 安装。
2. **Lint**：`ruff check .`。
3. **格式检查**：`ruff format --check .`。
4. **类型检查**：`pyright`，`typeCheckingMode = "basic"`（非 off，非 strict），`include = ["src", "tests", "scripts"]`，`pythonVersion = "3.11"`；配置在 `pyproject.toml` 的 `[tool.pyright]`，不使用全局 ignore 或 `report*=none` 消错。
5. **构建**：`uv build` 输出到固定的 `dist/`（先清空），在 pytest 之前执行，保证安装型测试、formula 生成与 release 上传使用同一批产物。
6. **验包**：`scripts/check_dist.py` 校验 sdist/wheel：文件名版本与 `pyproject` 一致且无本地版本段、sdist 条目不超白名单、不含本地/机器元数据（.jj/.git/.agents/.pi/.venv/__pycache__/direct_url.json 等）、wheel 含全部必要 `cc_models` 模块。不是仅 twine metadata 检查。
7. **formula 生成**：`scripts/gen_formula.py` 从 `dist/` 真实产物重新生成 `Formula/cc-models.rb` 与 `dist/checksums.txt`（formula 摘要即本次构建 sdist 的 sha256）。`--check` 模式在产物过期时失败。
8. **测试**：`pytest -q`。安装型测试（`tests/test_installed_cli.py`）用 `uv pip install` 把 `dist/` 里的真实 wheel 装进一次性 venv，断言打包后的 `cc-models` 入口和 `--version` 输出与 `pyproject` 版本一致。

测试配套见 `tests/test_check_dist.py`，含本地元数据、超白名单、本地版本段、缺模块等负例。

## CI

GitHub Actions（`.github/workflows/ci.yml`）：`pull_request` / `push main` / `workflow_dispatch` 触发；Ubuntu 上 Python 3.11 与 3.13 matrix；`contents: read` 最小权限；`concurrency` 取消同分支旧 run；`actions/checkout` 与 `astral-sh/setup-uv` 固定 commit SHA；调用同一 `./scripts/verify.sh`；设合理 timeout，无 continue-on-error，不使用 pull_request_target。

## Release

`.github/workflows/release.yml` 只在推送 `v*` 标签时触发：

1. 校验 tag 与 `pyproject.toml` 版本一致，跑全量 `./scripts/verify.sh`。
2. `scripts/gen_formula.py` 从本次构建的真实产物生成 formula 和 checksums。
3. `actions/attest-build-provenance` 为 sdist/wheel 签署 provenance。
4. `scripts/check_release_state.sh` 在任何上传前分类 release 状态：404 视为不存在并创建 draft；已有 draft 仅允许重跑（run_attempt > 1）覆盖；已有 public release 硬失败。其余 `gh api` 错误一律失败，绝不误判为「不存在」。
5. 上传 sdist、wheel、checksums.txt、formula 到 draft，然后 publish。

同标签的 workflow 串行运行，重跑时 guard 拒绝覆盖已公开版本。状态检查和发布不是原子操作，不要在 workflow 运行期间手动发布或修改同一 Release。

## Homebrew formula

`Formula/cc-models.rb` 是 `scripts/gen_formula.py` 生成的快照，供 `--check` 门和阅读，不是 tap。formula 的 url 指向 release sdist，sha256 即该 sdist 本身，因此从 release 资产安装不存在哈希错位。

依赖闭包从 `uv.lock` 的 universal 图出发，用 `packaging` 按 Homebrew 的 python@3.13 环境求值 marker；每个运行时依赖一个 Resource，sdist url 和摘要取自 PyPI JSON API 的 canonical 地址。

### 维护者更新 tap

首发为手动流程（不依赖 secrets）：

1. 打 `v<版本>` tag，等 release workflow 发布。
2. 从该 release 下载 `cc-models.rb` 资产，提交到 `toRolex/homebrew-tap` 仓库的 `Formula/` 目录。
3. 用 `brew audit --strict --skip-style toRolex/tap/cc-models` 和本机 `brew install toRolex/tap/cc-models` 验证。
4. 升级用户：`brew update && brew upgrade toRolex/tap/cc-models`。

如果未来改用自动化推送 tap，需要 bot token 作为 secret；首发量级不值得，暂不建。
