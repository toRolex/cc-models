# Claude Code 菜单美化图

- 交付：`claude-model-menu-polished.png`，1774×887 RGBA，alpha 范围 0–255。
- 内置生图工具，模型 `gpt-image-2.5`；不是未经修改的系统截图。
- 基于用户提供的真实 Claude Code 模型菜单截图；发送生图服务前在本地裁掉上方用户名、主机名、路径和标签页，只提交菜单区域。不将原始私密截图加入仓库。
- 视觉检查：保留 9 个菜单选项及其顺序，选中 gpt-6.1-sol；改为深蓝玻璃窗口与透明外背景。文字为模型重绘，不作为逐像素原始界面证据。
- 原始生成文件保留 `.pi/images/a34a385e-7f09-4675-9223-2e814aab9b11.png`，交付文件为其副本。

## 完整提示词

Use case: precise-object-edit. Asset type: README showcase image, AI-polished from a real Claude Code model menu screenshot. Input is already cropped to remove private user information. Preserve the real menu text, all nine visible choices, their exact order and model IDs, selected gpt-6.1-sol row and checkmark, explanatory text and keyboard footer. Do not invent additional interface, models, capabilities or personal data. Recompose this menu as a compact, elegant front-facing macOS terminal window, wide landscape about 2:1 with no huge empty space. Add a minimal rounded titlebar with three small traffic-light buttons and title exactly 'Claude Code · Model selection'. Change ugly flat gray to a sophisticated very dark midnight navy translucent glass terminal surface, ivory monospace text, muted lavender headings and restrained mint selected row. Subtle blue-violet glass translucency, not washed out gray, crisp highly legible typography. Generous small balanced padding. Outside the floating rounded terminal window: genuinely transparent alpha, no wallpaper, no checkerboard, no scenery. Soft short shadow only. No username, hostname, filesystem path, extra tabs, watermark or fake logos. This is a tasteful AI-enhanced presentation of the supplied real menu, not a new UI design.
