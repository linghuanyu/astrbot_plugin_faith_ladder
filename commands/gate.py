"""
指令入口的统一闸门。

为什么集中：群访问控制、功能开关、状态阻断都是"这条指令现在该不该执行"的判断，
分散在 33 个入口里各写一遍必然会漏（此前 28 个实现体 + 8 个群管 handler 各自
校验权限，逐函数审查时就抓到过漏检）。所有判断都收在 `self._gate(event, action)`：

1. 群访问控制：`group_access_mode` = off / blacklist / whitelist，未启用的群**静默**跳过
   （不回复、也不 stop_event，插件完全不插手该群的消息）
2. 功能开关：`feature_*_enabled` 关掉时回一条明确的提示
3. 状态阻断：玩家身上的状态带 `block_actions`（如「沉默」禁止祷词/赠送）时回提示

`action` 是动作标识（见下面的 ACTION_* 表），由各实现体在调用闸门时传入。
约定：mixin 只放实现体，装饰器留在 main.py（见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from astrbot_plugin_faith_ladder.messages import STATUS_BLOCKED_MSG, feature_disabled_message

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


# 动作 → 功能开关配置键（不在表里的动作不受开关影响）
ACTION_FEATURE: Dict[str, str] = {
    "gift_send": "feature_gift_enabled",
    "gift_accept": "feature_gift_enabled",
    "gift_reject": "feature_gift_enabled",
    "query_inventory": "feature_inventory_enabled",
    "prayer": "feature_prayer_enabled",
    "scoreboard": "feature_scoreboard_enabled",
    "qq_admin": "feature_qq_admin_enabled",
}

# 可被「状态」阻断的动作 → 中文标签（用于「添加状态 … 阻断=祷词,赠送」与卡片展示）
ACTION_LABELS: Dict[str, str] = {
    "prayer": "祷词",
    "gift_send": "赠送",
    "gift_accept": "接受赠送",
    "query": "查询",
}

LABEL_ACTIONS: Dict[str, str] = {label: action for action, label in ACTION_LABELS.items()}

# 无阻断（用于显式清除）
BLOCK_NONE_LABELS = {"无", "没有", "none", "no", ""}


def parse_block_actions(raw: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """把用户写的阻断项解析成存库字符串。

    返回 `(存储值, 无法识别的项)`：
    - 存储值 = 逗号分隔的动作 id；显式写「无」时是空串（表示清除阻断）
    - `raw` 为 None（用户没写这段）时存储值也是 None —— **调用方要用返回的 unknown
      是否为空来判定错误**，不要把"结果 is None"当解析失败，否则不写阻断段会误报
    """
    if raw is None:
        return None, []
    items = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if not items:
        return "", []
    if len(items) == 1 and items[0].lower() in BLOCK_NONE_LABELS:
        return "", []

    actions: List[str] = []
    unknown: List[str] = []
    for item in items:
        if item in ACTION_LABELS:
            actions.append(item)
        elif item in LABEL_ACTIONS:
            actions.append(LABEL_ACTIONS[item])
        else:
            unknown.append(item)
    if unknown:
        return None, unknown
    return ",".join(dict.fromkeys(actions)), []


def format_block_actions(stored: Optional[str]) -> str:
    """存库字符串 → 展示文案（如「祷词、赠送」）；无阻断返回空串。"""
    if not stored:
        return ""
    labels = [ACTION_LABELS.get(a.strip(), a.strip()) for a in stored.split(",") if a.strip()]
    return "、".join(labels)


class GateMixin:
    """指令入口闸门。"""

    def _group_access_blocked(self, group_id: str) -> bool:
        """按配置判断该群是否被排除在插件之外。"""
        mode = str(self._cfg("group_access_mode") or "off").strip().lower()
        if mode not in ("blacklist", "whitelist"):
            return False  # off 或写错的值：一律放行（宁可失效也不要因为配置笔误把功能全关掉）

        raw = self._cfg("group_access_list") or []
        listed = {str(item).strip() for item in raw if str(item).strip()}
        group_key = str(group_id or "").strip()
        if not group_key:
            return False

        if mode == "whitelist":
            return group_key not in listed
        return group_key in listed

    def _feature_blocked(self, action: Optional[str]) -> Optional[str]:
        """功能开关判定：返回被关闭的功能提示，未关闭返回 None。"""
        feature_key = ACTION_FEATURE.get(action or "")
        if not feature_key:
            return None
        if self._cfg(feature_key):
            return None
        return feature_disabled_message(feature_key)

    async def _gate_actor_player(self, event: "AstrMessageEvent"):
        """闸门要判定的"发起人"玩家：QQ 绑定优先，回退名片宽松识别。

        识别不到就放行（fail-open）：用弱身份"把人拦下来"比放过去更糟——
        名片可以随便改，冒充者若能让别人被拦住，就成了新的骚扰手段；
        而冒充别人只会把冒充者自己挡住，没有伤害面。
        """
        resolver = getattr(self, "_resolve_self_player_lenient", None)
        if resolver is None:
            return None
        try:
            return await resolver(event)
        except Exception:
            return None

    async def _status_blocked(self, event: "AstrMessageEvent", action: Optional[str]) -> Optional[str]:
        """状态阻断判定：返回提示文案，未被阻断返回 None。"""
        if not action or action not in ACTION_LABELS:
            return None

        player = await self._gate_actor_player(event)
        if not player:
            return None

        statuses = await self.db_manager.get_player_statuses(player.group_id, player.player_id)
        for status in statuses:
            blocked = {a.strip() for a in str(status.get("block_actions") or "").split(",") if a.strip()}
            if action in blocked:
                return STATUS_BLOCKED_MSG.format(
                    status=status.get("status_name", ""), action=ACTION_LABELS[action]
                )
        return None

    async def _gate(self, event: "AstrMessageEvent", action: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """执行前的统一检查。

        返回 `(是否拦截, 提示文案)`；文案为 None 表示**静默**拦截（不回复任何内容）。
        """
        if self._group_access_blocked(self._get_group_id(event)):
            return True, None

        feature_msg = self._feature_blocked(action)
        if feature_msg:
            return True, feature_msg

        status_msg = await self._status_blocked(event, action)
        if status_msg:
            return True, status_msg

        return False, None
