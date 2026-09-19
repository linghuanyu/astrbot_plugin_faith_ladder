"""
指令入口的统一闸门。

为什么集中：群访问控制、功能开关、状态阻断都是"这条指令现在该不该执行"的判断，
分散在 33 个入口里各写一遍必然会漏（此前 28 个实现体 + 8 个群管 handler 各自
校验权限，逐函数审查时就抓到过漏检）。这里给一个入口 `self._gate(event, action)`：

- 群访问控制：`group_access_mode` = off / blacklist / whitelist，未启用的群**静默**跳过
  （不回复、也不 stop_event，插件完全不插手该群的消息）
- 功能开关与状态阻断见 P4（同样收在这个函数里，调用点不必再改）

约定：mixin 只放实现体，装饰器留在 main.py（见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class GateMixin:
    """指令入口闸门。"""

    def _group_access_blocked(self, group_id: str) -> bool:
        """按配置判断该群是否被排除在插件之外。"""
        mode = str(self._cfg("group_access_mode") or "off").strip().lower()
        if mode not in ("blacklist", "whitelist"):
            return False  # off 或写错的值：一律放行（宁可生效也不要因为配置笔误把功能全关掉）

        raw = self._cfg("group_access_list") or []
        listed = {str(item).strip() for item in raw if str(item).strip()}
        group_key = str(group_id or "").strip()
        if not group_key:
            return False

        if mode == "whitelist":
            return group_key not in listed
        return group_key in listed

    async def _gate(self, event: "AstrMessageEvent", action: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """执行前的统一检查。

        返回 `(是否拦截, 提示文案)`；文案为 None 表示**静默**拦截（不回复任何内容）。
        """
        if self._group_access_blocked(self._get_group_id(event)):
            return True, None
        return False, None
