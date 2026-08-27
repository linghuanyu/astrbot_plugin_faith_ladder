"""
Message formatting utilities for the faith ladder plugin.
"""

from typing import List, Optional
from astrbot_plugin_faith_ladder.models import Player, VALID_CLASSES, VALID_PATHS


def _name_with_tag(player: Player) -> str:
    """Return player name with (弃誓者) tag if applicable."""
    tag = "(弃誓者)" if player.oathbreaker else ""
    return f"{player.player_name}{tag}"


def format_leaderboard(players: List[Player], limit: int = 10) -> str:
    """Format the leaderboard display."""
    if not players:
        return "暂无排名数据。"

    lines = ["==登神之路排行榜==", ""]
    displayed = min(len(players), limit)

    for rank, player in enumerate(players[:limit], 1):
        class_str = f"[{player.class_}]" if player.class_ else "[未设定]"
        faith_str = f"<{player.faith}>" if player.faith else "<未设定>"
        lines.append(
            f"{rank}. {_name_with_tag(player)}"
        )
        lines.append(
            f"   {class_str} {faith_str}"
        )
        lines.append(
            f"   登神之路: {player.ladder_score}"
        )
        lines.append(
            f"   觐见之梯: {player.pilgrimage_score}"
        )
        lines.append("")

    lines.append(f"--- 显示前 {displayed} 名 ---")
    return "\n".join(lines)


def format_pilgrimage_leaderboard(players: List[Player], limit: int = 10) -> str:
    """Format the pilgrimage leaderboard display."""
    if not players:
        return "暂无排名数据。"

    lines = ["==觐见之梯==", ""]
    displayed = min(len(players), limit)

    for rank, player in enumerate(players[:limit], 1):
        class_str = f"[{player.class_}]" if player.class_ else "[未设定]"
        faith_str = f"<{player.faith}>" if player.faith else "<未设定>"
        lines.append(
            f"{rank}. {_name_with_tag(player)}"
        )
        lines.append(
            f"   {class_str} {faith_str}"
        )
        lines.append(
            f"   觐见之梯: {player.pilgrimage_score}"
        )
        lines.append(
            f"   登神之路: {player.ladder_score}"
        )
        lines.append("")

    lines.append(f"--- 显示前 {displayed} 名 ---")
    return "\n".join(lines)


def format_player_card(
    player: Player,
    ladder_rank: int = 0,
    pilgrimage_rank: int = 0,
    init_ladder: int = 1000,
    init_pilgrimage: int = 100,
    statuses: list = None
) -> str:
    """Format a player's info card with rankings and statuses."""
    class_str = player.class_ if player.class_ else "未设定"
    faith_str = player.faith if player.faith else "未设定"
    oathbreaker_str = "(弃誓者)" if player.oathbreaker else ""

    # 初始积分视为未上榜
    if player.ladder_score == init_ladder:
        ladder_rank_str = "未上榜"
    elif ladder_rank > 0:
        ladder_rank_str = f"第{ladder_rank}名"
    else:
        ladder_rank_str = "未上榜"

    if player.pilgrimage_score == init_pilgrimage:
        pilgrimage_rank_str = "未上榜"
    elif pilgrimage_rank > 0:
        pilgrimage_rank_str = f"第{pilgrimage_rank}名"
    else:
        pilgrimage_rank_str = "未上榜"

    lines = [
        f"=== 玩家信息 ===",
        f"姓名: {player.player_name}{oathbreaker_str}",
        f"职业: {class_str}",
        f"信仰: {faith_str}",
        f"登神之路: {player.ladder_score}",
        f"登神之路排名: {ladder_rank_str}",
        f"觐见之梯: {player.pilgrimage_score}",
        f"觐见之梯排名: {pilgrimage_rank_str}",
    ]

    # 状态显示
    if statuses:
        lines.append("")
        lines.append("[状态]")
        for s in statuses:
            remaining = s['remaining_days']
            if remaining == 0:
                lines.append(f"{s['status_name']}: 今日到期")
            else:
                lines.append(f"{s['status_name']}: 剩余{remaining}天")

    return "\n".join(lines)


def format_help(config: dict) -> str:
    """Format the help message."""
    cmd_sb = config.get("cmd_ladder", "天梯榜")
    cmd_pilgrimage = config.get("cmd_pilgrimage", "觐见榜")
    cmd_query = config.get("cmd_query", "查询")
    cmd_add = config.get("cmd_add_score", "录入积分")
    cmd_batch = config.get("cmd_batch_add_score", "批量录入")
    cmd_register = config.get("cmd_register_player", "录入玩家")
    cmd_class = config.get("cmd_set_class", "设置职业")
    cmd_admin = config.get("cmd_admin", "天梯榜管理")
    cmd_wl = config.get("cmd_whitelist", "白名单")
    cmd_help = config.get("cmd_help", "天梯榜帮助")

    ladder_cd = config.get("ladder_cooldown_seconds", 600)
    query_cd = config.get("query_cooldown_seconds", 600)
    output_mode = config.get("output_mode", "text")

    classes_str = "/".join(VALID_CLASSES)
    faiths_str = "/".join(VALID_PATHS)

    return (
        f"═══════════════════════\n"
        f"      诸 神 愚 戏\n"
        f"   信仰为局 众生为棋\n"
        f"═══════════════════════\n"
        f"\n"
        f"[窥探棋局]\n"
        f"{cmd_query} - 窥探自己在棋局中的位置（冷却 {query_cd}s）\n"
        f"{cmd_query} <玩家名> - 窥探指定玩家（诸神专用）\n"
        f"{cmd_query} @用户 - 窥探 @ 的用户（诸神专用，从名片识别）\n"
        f"\n"
        f"[排行榜] (诸神权限)\n"
        f"{cmd_sb} - 登神之路 · 凡人的阶梯（冷却 {ladder_cd}s）\n"
        f"{cmd_pilgrimage} - 觐见之梯 · 神明的凝视（冷却 {ladder_cd}s）\n"
        f"\n"
        f"[玩家管理] (诸神权限)\n"
        f"{cmd_register} @用户 [姓名] [命途] [职业] [分] [分] - 将新棋子推入棋局（@可自动提取名片信息）\n"
        f"{cmd_register} <姓名> <命途> <职业> [分] [分] - 或手动指定全部参数\n"
        f"  名片支持具体职业（如 酋长/织命师），系统自动识别对应命途和普通职业\n"
        f"{cmd_class} <玩家名> <职业> - 更改棋子身份\n"
        f"立誓 <玩家名> <命途> - 为棋子选择信仰\n"
        f"  职业: {classes_str} | 命途: {faiths_str}\n"
        f"\n"
        f"[积分管理] (诸神权限)\n"
        f"{cmd_add} <玩家名> <登神之路分变化> <觐见梯变化>\n"
        f"{cmd_batch} - 粘贴结算文本批量录入积分和道具\n"
        f"弃誓 <玩家名> [新命途] - 标记背誓者\n"
        f"\n"
        f"[储物空间]\n"
        f"查询储物空间 - 查看自己的道具（自动识别，需先绑定QQ）\n"
        f"查询储物空间 <玩家名> [玩家名2 ...] - 查看指定玩家道具（诸神批量）\n"
        f"赐予道具 <玩家名> <道具*数量> ... - 赐予道具（空格分隔多个）(诸神)\n"
        f"收回道具 <玩家名> <道具*数量> ... - 收回道具（不指定数量则全部收回）(诸神)\n"
        f"清除储物空间 <玩家名> [道具名|全部] - 清除储物空间（清空需加「全部」）(诸神)\n"
        f"\n"
        f"[赠送道具]（需先绑定QQ）\n"
        f"赠送道具 <接收方名> <道具*数量> - 赠送（发送方 QQ 鉴权，直接扣除，等待接收）\n"
        f"接受道具 - 接受赠送（所有人；诸神可指定接收玩家）\n"
        f"拒绝道具 - 拒绝赠送（所有人；诸神可指定接收玩家）\n"
        f"\n"
        f"[身份绑定]\n"
        f"绑定QQ @用户 - 为玩家绑定 QQ（诸神；防名片冒充）\n"
        f"\n"
        f"[状态] (诸神权限)\n"
        f"添加状态 <玩家名> <状态名> <天数> - 添加状态\n"
        f"移除状态 <玩家名> <状态名> - 移除指定状态\n"
        f"清除状态 <玩家名> - 清除所有状态\n"
        f"\n"
        f"[群管] (诸神权限)\n"
        f"禁言 <秒数> @用户 - 禁言（默认60秒）\n"
        f"解禁 @用户 - 解除禁言\n"
        f"踢人 @用户 - 踢出群聊\n"
        f"撤回 - 撤回引用消息\n"
        f"全员禁 / 全员解 - 全员禁言开关\n"
        f"设置精华 / 移除精华 - 精华消息管理（引用消息）\n"
        f"  保护: 群主和管理员不可被禁言/踢出\n"
        f"\n"
        f"[管理] (管理员权限)\n"
        f"{cmd_wl} add/remove/list\n"
        f"同步白名单 — 同步指定群成员到诸神列表\n"
        f"{cmd_admin} 重置/删除/改名/清空/清除弃誓\n"
        f"\n"
        f"初始之位：登神之路 1000 · 觐见之梯 100\n"
        f"愿神明不要愚弄你。\n"
        f"{cmd_help} - 显示本帮助"
    )


def format_gift_request(
    sender_name: str, receiver_name: str, item_name: str, grade, quantity: int
) -> str:
    """Format gift notification for receiver."""
    from astrbot_plugin_faith_ladder.ladder_service import format_item_display
    display = format_item_display(item_name, grade, quantity)
    return (
        f"{sender_name} 赠送 {receiver_name} {display}\n"
        f"接收方发送「接受道具」接受\n"
        f"接收方发送「拒绝道具」拒绝"
    )


def format_whitelist(entries: List[dict]) -> str:
    """Format whitelist display."""
    if not entries:
        return "诸神列表为空。"

    lines = ["=== 诸神列表 ===", ""]
    for i, entry in enumerate(entries, 1):
        lines.append(f"{i}. [{entry['entry_type']}] {entry['entry_id']}")
    lines.append(f"\n共 {len(entries)} 条记录")
    return "\n".join(lines)


def format_whitelist_combined(config_entries: List[dict], db_entries: List[dict]) -> str:
    """Format whitelist display combining config and DB sources."""
    if not config_entries and not db_entries:
        return "诸神列表为空。\n可通过 WebUI 配置 或 指令 /白名单 add 添加。"

    lines = ["=== 诸神列表 ==", ""]

    if config_entries:
        lines.append("[WebUI 配置]")
        for i, entry in enumerate(config_entries, 1):
            note = f" ({entry.get('note', '')})" if entry.get("note") else ""
            lines.append(f"  {i}. [{entry['entry_type']}] {entry['entry_id']}{note}")
        lines.append("")

    if db_entries:
        lines.append("[运行时添加]")
        start = len(config_entries) + 1
        for i, entry in enumerate(db_entries, start):
            lines.append(f"  {i}. [{entry['entry_type']}] {entry['entry_id']}")
        lines.append("")

    total = len(config_entries) + len(db_entries)
    lines.append(f"共 {total} 条记录（配置: {len(config_entries)}, 运行时: {len(db_entries)}）")
    return "\n".join(lines)


def format_score_result(
    player_name: str,
    ladder_delta: int,
    pilgrimage_delta: int,
    new_ladder: int,
    new_pilgrimage: int
) -> str:
    """Format score entry result."""
    ladder_str = f"+{ladder_delta}" if ladder_delta >= 0 else str(ladder_delta)
    pilgrimage_str = f"+{pilgrimage_delta}" if pilgrimage_delta >= 0 else str(pilgrimage_delta)

    return (
        f"积分录入成功!\n"
        f"玩家: {player_name}\n"
        f"登神之路: {ladder_str} -> {new_ladder}\n"
        f"觐见之梯: {pilgrimage_str} -> {new_pilgrimage}"
    )


def format_inventory(player_name: str, items: list) -> str:
    """Format player inventory display.
    items: [{"item_name": str, "grade": str|None, "quantity": int}, ...]
    """
    from astrbot_plugin_faith_ladder.ladder_service import format_item_display

    if not items:
        return f"=== 储物空间 ===\n玩家: {player_name}\n\n储物空间为空。"

    lines = [f"=== 储物空间 ===", f"玩家: {player_name}", ""]
    for item in items:
        lines.append(format_item_display(item["item_name"], item["grade"], item["quantity"]))
    return "\n".join(lines)


def format_prayer_trigger(player_name: str, player_faith: str, prayer_faith: str, delta: int, config: dict = None) -> str:
    """格式化祷词触发回复。比较玩家具体信仰与祷词具体信仰。

    Args:
        player_name: 玩家名
        player_faith: 玩家的具体信仰名（如"欺诈"）
        prayer_faith: 祷词对应的具体信仰名（如"欺诈"）
        delta: 觐见分变化值（显示用）
        config: 插件配置（用于获取自定义文案）
    """
    import random
    from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH

    # 检查玩家信仰是否匹配祷词信仰
    faith_matches = player_faith == prayer_faith

    if faith_matches:
        # 匹配：使用玩家的信仰名
        god_name = player_faith

        # 优先查信仰专属配置，回退到通用配置
        if delta > 0:
            config_key = f"prayer_trigger_messages_positive_{prayer_faith}"
            fallback_key = "prayer_trigger_messages_positive"
            default_msgs = ["{god}今日心情不错，觐见+{delta}"]
            template_vars = {"god": god_name, "delta": f"+{delta}"}
        elif delta < 0:
            config_key = f"prayer_trigger_messages_negative_{prayer_faith}"
            fallback_key = "prayer_trigger_messages_negative"
            default_msgs = ["{god}今日心情不佳，觐见{delta}"]
            template_vars = {"god": god_name, "delta": str(delta)}
        else:
            config_key = f"prayer_trigger_messages_neutral_{prayer_faith}"
            fallback_key = "prayer_trigger_messages_neutral"
            default_msgs = ["{god}听到了你的祈祷，但未起波澜"]
            template_vars = {"god": god_name}

        # 从配置获取文案池：信仰专属 → 通用 → 默认
        messages = None
        if config:
            messages = config.get(config_key) or config.get(fallback_key)
        if not messages:
            messages = default_msgs

        template = random.choice(messages)
        result = template.format(**template_vars)
        msg = f"神明看到了你的祈祷\n{result}"

    else:
        # 不匹配（渎神）
        prayer_path = FAITH_TO_PATH.get(prayer_faith, prayer_faith)

        if delta == 0:
            # 渎神但随机到 0：宽宏大量
            msg = f"{player_faith}看到了你对{prayer_faith}的祈祷，决定对你进行惩罚……\n但神明宽宏大量，放过了你这次渎神"
        else:
            # 渎神扣分
            config_key = "prayer_trigger_messages_mismatch"
            default_msgs = ["{god}冷笑：蝼蚁也敢觊觎{prayer}的领域？觐见{delta}"]
            messages = config.get(config_key, default_msgs) if config else default_msgs
            if not messages:
                messages = default_msgs

            template_vars = {"god": player_faith, "prayer": prayer_faith, "delta": str(delta)}
            template = random.choice(messages)
            result = template.format(**template_vars)
            msg = f"{player_faith}看到了你对{prayer_faith}的祈祷，决定对你进行惩罚\n{result}"

    # 测试模式：始终附加说明（因为目前不实际改分）
    msg += "\n（本次结果暂时不会影响实际分数）"

    return msg
