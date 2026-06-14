"""
Message formatting utilities for the faith ladder plugin.
"""

from typing import List, Optional
from astrbot_plugin_faith_ladder.models import Player, VALID_CLASSES, VALID_FAITHS


def format_leaderboard(players: List[Player], limit: int = 10) -> str:
    """Format the leaderboard display."""
    if not players:
        return "暂无排名数据。"

    lines = ["==天梯排行榜==", ""]
    displayed = min(len(players), limit)

    for rank, player in enumerate(players[:limit], 1):
        class_str = f"[{player.class_}]" if player.class_ else "[未设定]"
        faith_str = f"<{player.faith}>" if player.faith else "<未设定>"
        lines.append(
            f"{rank}. {player.player_name} {class_str} {faith_str}"
        )
        lines.append(
            f"   天梯积分: {player.ladder_score} | 觐见之梯: {player.pilgrimage_score}"
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
            f"{rank}. {player.player_name} {class_str} {faith_str}"
        )
        lines.append(
            f"   觐见之梯: {player.pilgrimage_score} | 天梯积分: {player.ladder_score}"
        )

    lines.append("")
    lines.append(f"--- 显示前 {displayed} 名 ---")
    return "\n".join(lines)


def format_player_card(player: Player) -> str:
    """Format a player's info card."""
    class_str = player.class_ if player.class_ else "未设定"
    faith_str = player.faith if player.faith else "未设定"

    return (
        f"=== 玩家信息 ===\n"
        f"姓名: {player.player_name}\n"
        f"职业: {class_str}\n"
        f"信仰: {faith_str}\n"
        f"天梯积分: {player.ladder_score}\n"
        f"觐见之梯: {player.pilgrimage_score}\n"
    
    )


def format_help(config: dict) -> str:
    """Format the help message."""
    cmd_sb = config.get("cmd_ladder", "天梯榜")
    cmd_pilgrimage = config.get("cmd_pilgrimage", "觐见榜")
    cmd_query = config.get("cmd_query", "查询")
    cmd_add = config.get("cmd_add_score", "录入积分")
    cmd_register = config.get("cmd_register_player", "录入玩家")
    cmd_class = config.get("cmd_set_class", "设置职业")
    cmd_admin = config.get("cmd_admin", "天梯榜管理")
    cmd_wl = config.get("cmd_whitelist", "白名单")
    cmd_help = config.get("cmd_help", "天梯榜帮助")

    ladder_cd = config.get("ladder_cooldown_seconds", 600)
    query_cd = config.get("query_cooldown_seconds", 600)
    push_time = config.get("daily_push_time", "07:00")
    push_enabled = config.get("daily_push_enabled", True)
    push_info = f"每日 {push_time} 自动推送" if push_enabled else "未开启"

    classes_str = "/".join(VALID_CLASSES)
    faiths_str = "/".join(VALID_FAITHS)

    return (
        f"=== 信仰游戏天梯排行榜 ===\n"
        f"\n"
        f"[查询]\n"
        f"{cmd_query} <玩家名> - 查询玩家天梯分与觐见分（冷却 {query_cd}s）\n"
        f"\n"
        f"[排行榜] (白名单权限)\n"
        f"{cmd_sb} - 天梯排行榜（冷却 {ladder_cd}s）\n"
        f"{cmd_pilgrimage} - 觐见之梯（冷却 {ladder_cd}s）\n"
        f"\n"
        f"[玩家管理] (白名单权限)\n"
        f"{cmd_register} <姓名> <信仰> <职业> <天梯分> <觐见分> - 录入新玩家\n"
        f"{cmd_class} <玩家名> <职业> <信仰> - 修改职业信仰\n"
        f"  职业: {classes_str} | 信仰: {faiths_str}\n"
        f"\n"
        f"[积分管理] (白名单权限)\n"
        f"{cmd_add} <玩家名> <天梯分变化> <觐见梯变化>\n"
        f"\n"
        f"[管理] (管理员权限)\n"
        f"{cmd_wl} add/remove/list\n"
        f"{cmd_admin} reset/resetall/delete/rename/clear\n"
        f"\n"
        f"推送: {push_info} | 初始积分: 天梯1000 觐见100\n"
        f"{cmd_help} - 显示本帮助"
    )


def format_whitelist(entries: List[dict]) -> str:
    """Format whitelist display."""
    if not entries:
        return "白名单为空。"

    lines = ["=== 白名单 ===", ""]
    for i, entry in enumerate(entries, 1):
        lines.append(f"{i}. [{entry['entry_type']}] {entry['entry_id']}")
    lines.append(f"\n共 {len(entries)} 条记录")
    return "\n".join(lines)


def format_whitelist_combined(config_entries: List[dict], db_entries: List[dict]) -> str:
    """Format whitelist display combining config and DB sources."""
    if not config_entries and not db_entries:
        return "白名单为空。\n可通过 WebUI 配置 或 指令 /白名单 add 添加。"

    lines = ["=== 白名单 ==", ""]

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
        f"天梯积分: {ladder_str} -> {new_ladder}\n"
        f"觐见之梯: {pilgrimage_str} -> {new_pilgrimage}"
    )
