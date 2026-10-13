# README 图片来源与复现

本目录的图标与界面截图不参与打包，修改图片不需要发布新的工具版本。

## 图标

`icon.png`（512×512）与 `icon-128.png`（128×128）来自内置生图工具 **gpt-image-2.5** 生成的同一张原图，经 `sips` 重采样。原始生成文件保留在本地 `.pi/images/12cf4fe1-1ca8-4d1d-8c28-c0dd81f5fe32.png`，不纳入发布包。

完整提示词：

> Use case: logo-brand. Asset type: replacement README app icon for cc-models, an open-source terminal tool for selecting upstream AI models and writing a model menu. Create one polished original square icon, no text, no letters, no watermark. Subject: a beautifully simplified dark terminal tile with three stacked model-option rows, the middle option distinctly selected by a mint checkmark and lavender highlight; a tiny terminal prompt chevron integrates into the header. Design should be substantially fresh and more refined than generic list clipart: confident bold geometric silhouette, balanced compact composition, rounded corners, minimal depth and crisp edges. Color palette: warm charcoal / very dark navy background, soft violet and luminous mint accents, subtle off-white highlights. Large central symbol with generous safe padding, excellent readability at 128px and 64px, professional restrained developer-tool identity. Render a single finished square icon filling the image, not a contact sheet or presentation mockup. Avoid robots, brains, Claude starburst, CC Switch logos, complex gradients, fine decorative details, photorealistic devices, drop shadows outside tile.

## 真实界面截图

- `screenshot-model-list.png` / `.svg`：勾选、显示名称和描述编辑、显式默认模型，以及通过 `m` 勾选「不加 1m」的排除列；终端尺寸 120×16。
- `screenshot-preview.png` / `.svg`：按 `p` 打开的真实应用预览；终端尺寸 120×66。

截图使用 `build_demo_context()` 的临时配置与固定模型，不访问网络、不修改真实 Claude Code 配置，也不执行应用。由 Textual Pilot 操作真实 TUI，`app.export_screenshot()` 导出 SVG，再由系统 `rsvg-convert` 转成 PNG。不是生图模型生成的界面，也不是系统窗口截图。

在仓库根目录复现（系统需安装 librsvg 提供的 `rsvg-convert`）：

```sh
env -u UV_INDEX uv run --locked python scripts/export_readme_screenshots.py
rsvg-convert -w 1600 docs/assets/screenshot-model-list.svg -o docs/assets/screenshot-model-list.png
rsvg-convert -w 1600 docs/assets/screenshot-preview.svg -o docs/assets/screenshot-preview.png
```

导出脚本会断言模型选择、默认值、重命名、「不加 1m」排除状态和预览弹窗中的输出 ID 与警告，并在结束后清理临时演示目录。PNG 是 SVG 的派生展示版本，SVG 保留真实界面的导出记录。
