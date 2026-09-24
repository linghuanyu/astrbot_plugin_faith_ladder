"""
统一文案库。
所有用户可见消息集中管理。
"""

# ========== 权限拒绝 ==========
PERMISSION_DENIED = {"god_only": "此等权柄，唯诸神方可执掌。"}

# ========== 玩家不存在 ==========
# 全库「找不到玩家」只用这一种说法，避免同一个意思出现五种写法
PLAYER_NOT_FOUND = "在本宇宙未寻找到（{name}）"

# ========== 输入错误 ==========
SCORE_NOT_INT = "分数须为整数。"
INVALID_ITEM_FORMAT = "未识别有效道具。格式：道具名*数量"

# ========== 批量操作 ==========
BATCH_ALL_SUCCESS = "结算完成，{count} 人积分已变更。"
BATCH_PARTIAL_SKIP = "结算完成：{success} 人积分已变更，{skip} 人不在本宇宙，已略过。"

# ========== 冷却消息 ==========
# 统一放在这里，避免同一条提示在不同指令里各写一遍、改一处漏一处
COOLDOWN_MSG = "排行榜冷却中，请 {seconds} 秒后再试。"
QUERY_COOLDOWN_MSG = "查询冷却中，请 {seconds} 秒后再试。"
BATCH_COOLDOWN_MSG = "批量录入冷却中，请 {seconds} 秒后再试。"
OATH_COOLDOWN_MSG = "冷却中，请 {seconds} 秒后再试。"

# ========== 功能开关（关闭时的提示） ==========
# 配置键 → 给玩家看的名字。键名与 _conf_schema.json 的 feature_* 一一对应
FEATURE_LABELS = {
    "feature_gift_enabled": "道具赠送",
    "feature_inventory_enabled": "储物空间",
    "feature_prayer_enabled": "祷词",
    "feature_scoreboard_enabled": "排行榜",
    "feature_qq_admin_enabled": "群管指令",
}

FEATURE_DISABLED_MSG = "「{label}」功能已被管理员关闭。"


def feature_disabled_message(feature_key: str) -> str:
    """功能被关闭时的统一提示。"""
    return FEATURE_DISABLED_MSG.format(label=FEATURE_LABELS.get(feature_key, feature_key))

# ========== 状态阻断 ==========
STATUS_BLOCKED_MSG = "你正处于「{status}」状态，暂时无法{action}。"
