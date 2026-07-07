# 更新日志

## [1.6.0] - 2026-07-03

### 修复
- 图片配置 `image_format`/`image_quality` 现在实际生效（之前被忽略，永远输出 PNG）
- 批量录入增加冷却时间检查，防止无限制刷入
- `_render_and_send` 改用 `is_ladder` 参数代替函数指针比较
- `register_player` 不再直接访问 DB 私有属性 `_db`，新增公开 `commit()` 方法
- 帮助文本中"批量录入"改为使用配置变量，与其他命令一致
- 删除 `image_renderer.py` 中未使用的 `template_dir`/`_bg_image_path`/`_bg_image_cache`
- 添加字体缓存，避免每次渲染重复创建字体对象
- 删除 `scheduler_service.py` 中无意义的 `_()` i18n 占位函数
- 删除 `_push_image_mode` 未使用的 `config` 参数

## [1.5.0] - 2026-07-03

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
## [1.4.5] -
# 实现计划：信仰天梯插件图片输出功能

## 概述
为信仰天梯排行榜插件添加图片输出能力，支持天梯榜、觐见榜和每日推送以图片形式展示。

## 一、需要修改的文件

### 1. _conf_schema.json - 添加配置项
添加 output_mode、image_quality、image_format 三个配置项

### 2. 新建 templates/ 目录和 HTML 模板
- templates/leaderboard.html - 天梯榜模板
- templates/pilgrimage.html - 觐见榜模板

### 3. 新建 image_renderer.py - 图片渲染服务
封装 AstrBot 的 html_render API，提供图片渲染能力

### 4. ladder_service.py - 添加获取玩家列表方法
- get_leaderboard_players() - 返回 Player 列表用于图片渲染
- get_pilgrimage_leaderboard_players() - 返回 Player 列表

### 5. main.py - 主要改动
- __init__ 中初始化 ImageRenderer
- cmd_ladder() - 支持图片输出模式
- cmd_pilgrimage() - 支持图片输出模式
- 新增 cmd_output_mode() - 切换输出模式命令
- initialize() - 修改 send_to_group 回调支持图片发送

### 6. scheduler_service.py - 支持图片推送
- __init__ 添加 get_leaderboard_players 和 image_renderer 参数
- _do_daily_push() - 根据 output_mode 选择文本或图片推送

### 7. message_formatter.py - 更新帮助信息
format_help() 中添加输出模式说明

## 二、实施步骤

Phase 1: 基础设施
1. 创建 templates/ 目录
2. 创建 leaderboard.html 和 pilgrimage.html 模板
3. 创建 image_renderer.py

Phase 2: 配置扩展
4. 修改 _conf_schema.json 添加新配置项

Phase 3: 服务层修改
5. 修改 ladder_service.py 添加获取玩家列表方法
6. 修改 scheduler_service.py 支持图片推送

Phase 4: 主插件修改
7. 修改 main.py 初始化 ImageRenderer
8. 修改 cmd_ladder() 和 cmd_pilgrimage()
9. 添加 cmd_output_mode() 命令
10. 修改 initialize() 中的 send_to_group 回调

Phase 5: 帮助和测试
11. 更新 format_help() 帮助信息
12. 创建 tests/test_image_renderer.py

## 三、关键设计决策

1. 输出模式：全局配置，默认 text 保持向后兼容
2. 图片格式：默认 jpeg，可配置 png
3. 降级策略：图片渲染失败时自动降级为文本
4. 权限控制：输出模式切换仅限管理员
5. 调度推送：支持图片模式，发送时先发文本标题再发图片
