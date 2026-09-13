"""
指令实现体模块 - 各职责指令处理。

约定（重要）：

1. 本包下的 mixin **只放实现体**，`@filter.command` / `@filter.regex` 等装饰器
   一律保留在 `main.py` 的 `FaithLadderPlugin` 上。AstrBot 只扫描插件类自身
   的方法来注册指令；把装饰器放进 mixin，注册时会沿 MRO 重复扫到，导致重复
   注册并中断后续指令的注册（表现为：部分指令回复两次，另一些完全不触发）。
2. 方法名统一以 `_impl` 结尾，避免与 `main.py` 中的同名方法互相静默覆盖。
3. 宿主类通过继承把本包的 mixin 与 `Star` 组合，例如
   `class FaithLadderPlugin(QueryCommandsMixin, SharedSendMixin, Star)`。
"""

from .query import QueryCommandsMixin
from .shared import SharedSendMixin

__all__ = ["QueryCommandsMixin", "SharedSendMixin"]
