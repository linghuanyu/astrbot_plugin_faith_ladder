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
