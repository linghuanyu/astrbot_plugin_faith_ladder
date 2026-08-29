"""
统一文案库。
所有用户可见消息集中管理，支持变量替换和随机选取。
"""

import random

# ========== 权限拒绝 ==========
PERMISSION_DENIED = {
    "god_only": "此等权柄，唯诸神方可执掌。",
}

# ========== 玩家不存在 ==========
PLAYER_NOT_FOUND = "{name}未引起寰宇诸神的注意。"

# ========== 输入错误 ==========
INPUT_ERRORS = {
    "score_not_int": "分数须为整数。",
    "days_not_int": "天数须为整数。",
    "days_not_positive": "天数须大于零。",
    "name_too_long": "名讳过长，至多 {max_len} 字。",
    "invalid_faith": "此信仰不存在。",
    "invalid_class": "此职业不存在。",
    "invalid_item_format": "未识别有效道具。格式：道具名*数量",
}

# ========== 成功确认 ==========
SUCCESS_MESSAGES = {
    "give_item": "神明降下神赐：{items}，归于 {name}",
    "take_item": "神明收回对你的赐予：{items}",
    "bind_qq": "神明向你瞥视：{name} ↔ {qq}",
    "reset_player": "{name} 的命运已重置（天梯: {ladder}, 觐见: {pilgrimage}）。",
    "reset_all": "本切片宇宙已重置，{count} 名玩家除名。",
    "clear_oath": "【{faith}】决定放你一马。",
    "add_whitelist": "{user_id} 已入诸神列表。",
    "remove_whitelist": "{user_id} 已出诸神列表。",
    "already_whitelisted": "已在诸神列表之中。",
}

# ========== 失败/异常 ==========
ERROR_MESSAGES = {
    "set_failed": "设置失败，请重试。",
    "bind_failed_qq_taken": "绑定失败：此 QQ 已属他人。",
    "accept_failed": "接受失败：{msg}",
    "take_item_failed": "收回失败：{name} 并无道具「{item}」",
    "ban_failed": "禁言失败：{e}",
    "kick_failed": "踢出失败：{e}",
    "operation_failed": "操作失败：{e}",
}

# ========== 道具相关 ==========
ITEM_MESSAGES = {
    "inventory_empty": "储物空间满满当当，嘻～",
    "item_insufficient": "小骗子，你哪来的这么多道具？",
    "item_not_found": "并无此道具：{name}",
    "item_deducted": "{item} 已扣除。",
    "item_received": "已收到：{item}",
}

# ========== 冷却消息 ==========
COOLDOWN_MESSAGES = {
    "generic": "冷却中，{seconds} 秒后可再试。",
}

# ========== 批量操作 ==========
BATCH_MESSAGES = {
    "all_success": "结算完成，{count} 人积分已变更。",
    "partial_skip": "结算完成：{success} 人积分已变更，{skip} 人不在命册，已略过。",
}

# ========== 录入玩家仪式化 ==========
REGISTER_PLAYER_TEMPLATE = """「{name}」已信仰{faith}
职业：{class}
信仰：{faith} | {specific_faith}
登神之路：{ladder} —— 凡人之始
觐见之梯：{pilgrimage} —— 初窥门径
愿神明不要愚弄你。"""


def get_message(category: str, key: str = None, **kwargs) -> str:
    """
    获取消息，支持变量替换。

    Args:
        category: 消息类别（如 "PERMISSION_DENIED", "INPUT_ERRORS"）
        key: 消息键（如 "god_only", "score_not_int"）
        **kwargs: 用于替换消息中的变量

    Returns:
        格式化后的消息字符串
    """
    messages = globals().get(category, {})
    if key:
        template = messages.get(key, "")
    else:
        template = messages if isinstance(messages, str) else ""

    if kwargs:
        return template.format(**kwargs)
    return template


def get_random_message(messages: list, **kwargs) -> str:
    """从消息列表中随机选取一条，支持变量替换。"""
    if not messages:
        return ""
    template = random.choice(messages)
    if kwargs:
        return template.format(**kwargs)
    return template
