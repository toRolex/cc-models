# docs/assets 来源说明

本目录存放 README 引用的图标与界面截图，均不参与打包（包内不含 `docs/`），版本升级不需要重新发布。

## 图标

- `icon.png`（512×512）与 `icon-128.png`（128×128）：由图像生成模型（gpt-image-2.5，内置图像生成）按以下提示词生成后导出的位图，512 版由 1254×1254 原图用 `sips -Z 512` 重采样得到，128 版由原图缩放得到：

  > Use case: logo-brand. Asset type: cc-models open-source terminal CLI project icon for README. Create one original square icon, no letters or text, no watermark. A compact rounded terminal window contains three horizontal model-selection rows and a distinctive checkmark on the middle row, with a small prompt chevron. Flat vector-like geometry, precise thick lines, excellent small-size legibility. Deep navy rounded-square background with lavender and mint accents inspired by a terminal dark theme. Minimal, restrained professional developer-tool identity. Centered symbol with generous padding. Avoid Claude starburst, CC Switch branding, robots, brains, complex gradients or 3D effects.

- 生成的原始 1254×1254 位图保留在本地未跟踪的 `.pi/images/` 目录，仓库内只保留 512 与 128 两个派生版本。

## 截图

- `screenshot-model-list.png` / `.svg`：模型列表主界面，含重命名后的显示名。
- `screenshot-preview.png` / `.svg`：按 `p` 后的应用预览弹窗。

截图流程：在演示模式（`--demo`，临时配置、固定演示数据、不访问网络）下通过 Textual Pilot 驱动真实 TUI，用 `app.export_screenshot()` 导出 SVG，再由外部命令转成 PNG：

```sh
# SVG -> PNG（需要系统安装 librsvg 的 rsvg-convert，非 Python 依赖）
rsvg-convert -w 1600 screenshot-model-list.svg -o screenshot-model-list.png
rsvg-convert -w 1600 screenshot-preview.svg -o screenshot-preview.png
```

演示模式下的操作序列与导出断言（复现脚本存于仓库外的会话临时目录，未纳入仓库，避免依赖易变的运行时内部属性）：

1. 启动 `CCModelsApp(build_demo_context())`，`run_test(size=(110, 30))`。
2. 断言光标行模型 ID 为 `claude-opus-4-6`；按 `d` 设默认、`e` 重命名输入 `Opus (renamed)` 回车保存。
3. 导出第一张 SVG，断言内容含 `renamed`，且无弹窗打开。
4. `j j` 移动到未选中的 `claude-sonnet-4-6` 行，按空格选中并断言选中状态。
5. 按 `p` 打开应用预览，导出第二张 SVG，断言内容含 `应用预览`。

SVG 是截图的原始记录；PNG 由 SVG 转换得到。README 中的截图均为演示数据，不含真实模型目录或凭据。
