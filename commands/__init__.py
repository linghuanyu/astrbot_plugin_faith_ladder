"""
指令实现体模块 - 按职责拆分的指令实现。

约定（重要）：

1. 本包下的 mixin **只放实现体**，`@filter.command` / `@filter.regex` 等装饰器
   一律保留在 `main.py` 的 `FaithLadderPlugin` 上。AstrBot 只扫描插件类自身
   的方法来注册指令；把装饰器放进 mixin，注册时会沿 MRO 重复扫到，导致重复
   注册并中断后续指令的注册（表现为：部分指令回复两次，另一些完全不触发）。
2. 实现体方法名统一以 `_impl` 结尾，避免与 `main.py` 中的同名方法互相静默覆盖。
3. 宿主类通过继承把本包的 mixin 与 `Star` 组合。跨模块共用的基础设施
   （身份识别、名片解析、配置读取等）仍留在 `main.py`，实现体通过 `self.` 调用。
4. 只被某个模块使用的辅助方法，随该模块一起放在这里；被多个模块共用的留在 main.py。

模块划分：

    query.py       查询、查询储物空间
    scoreboard.py  天梯榜、觐见榜
    score.py       录入积分、批量录入
    player.py      录入玩家、检测玩家、绑定QQ、换绑QQ、设置职业、立誓、弃誓
    inventory.py   赐予道具、收回道具、清除储物空间、添加/移除/清除状态
    gift.py        赠送道具、接受道具、拒绝道具
    admin.py       天梯榜管理、白名单、同步白名单、天梯榜帮助、白名单自动同步事件
    prayer.py      祷词触发
    shared.py      通用发送辅助（合并转发）
    config.py      统一配置读取（默认值来自 _conf_schema.json）
    gate.py        指令入口闸门（群访问控制；功能开关/状态阻断也接在这里）
    wager.py       神明的赌局（定时开局、限时入局、开奖）
    wish.py        祈愿试炼（组队、满员发车挂状态、诸神治理）
"""

from .query import QueryCommandsMixin
from .scoreboard import ScoreboardCommandsMixin
from .score import ScoreCommandsMixin
from .player import PlayerCommandsMixin
from .inventory import InventoryCommandsMixin
from .gift import GiftCommandsMixin
from .admin import AdminCommandsMixin
from .prayer import PrayerCommandsMixin
from .shared import SharedSendMixin
from .config import ConfigMixin
from .gate import GateMixin
from .wager import WagerMixin
from .wish import WishCommandsMixin

__all__ = [
    "QueryCommandsMixin",
    "ScoreboardCommandsMixin",
    "ScoreCommandsMixin",
    "PlayerCommandsMixin",
    "InventoryCommandsMixin",
    "GiftCommandsMixin",
    "AdminCommandsMixin",
    "PrayerCommandsMixin",
    "SharedSendMixin",
    "ConfigMixin",
    "GateMixin",
    "WagerMixin",
    "WishCommandsMixin",
]
