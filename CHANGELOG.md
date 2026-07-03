# 更新日志

## [1.1.0] - 2026-07-03

### 新增
- **批量录入指令** (`/批量录入`, `/batch`, `/bl`) — 粘贴游戏结算文本自动解析并批量更新积分
  - 支持格式：`【玩家：XXX】【登神之路+X】【觐见之梯+Y】`
  - 兼容 `登神之路` 和 `登神指路` 两种写法
  - 不存在的玩家自动跳过并提示
  - 需要白名单或管理员权限
- **图片输出模式** — 排行榜支持以图片形式展示
  - 新增 `输出模式 text|image` 指令（仅管理员）
  - 支持全局默认模式和群级独立设置
  - 图片渲染失败时自动降级为文本
- **每日推送图片支持** — 定时推送可配置为图片模式

### 改动
- `ladder_service.py` — 新增 `parse_batch_scores()`, `batch_add_scores()`, `get_leaderboard_players()`, `get_pilgrimage_leaderboard_players()`
- `main.py` — 新增 `cmd_batch_add_score()`, `cmd_output_mode()`, `_render_and_send()` 辅助方法
- `scheduler_service.py` — 支持图片模式推送，新增 `_push_image_mode()`
- `db_manager.py` — 新增 `group_settings` 表，`get_group_output_mode()`, `set_group_output_mode()`, `purge_old_score_history()`
- `message_formatter.py` — 帮助信息增加批量录入和输出模式说明
- `_conf_schema.json` — 新增 `output_mode`, `image_quality`, `image_format` 配置项

### 新增文件
- `image_renderer.py` — PIL 本地图片渲染器（渐变背景、金/银/铜排名边框、职业/信仰徽章）
- `fonts/DouyinSansBold.otf` — 内置 CJK 字体
- `templates/leaderboard.html` — 天梯榜 HTML 模板
- `templates/pilgrimage.html` — 觐见榜 HTML 模板
- `tests/test_batch_entry.py` — 批量录入测试（11个）
- `tests/test_image_renderer.py` — 图片渲染测试（8个）
- `tests/test_group_settings.py` — 群设置测试（9个）

### 测试
- 总计 **140 个测试全部通过**
