"""
统一配置读取（mixin）。

`self.config` 是 AstrBot 注入的配置字典；本模块把读取收敛成一个方法 `self._cfg(key)`，
默认值与类型以 `_conf_schema.json` 为唯一事实来源（实现在 plugin_config.py）。
好处：
- 默认值只写一份，不会再出现"帮助显示 600、实际生效 5"这类不一致
- WebUI 写成 null / 字符串的脏值在读取层统一转换或回落默认值
- 以后要把平铺配置改成嵌套分组，只需改这一处

约定：mixin 只放实现体，装饰器留在 main.py（见 commands/__init__.py）。
"""

from __future__ import annotations

from astrbot_plugin_faith_ladder.plugin_config import cfg_get


class ConfigMixin:
    """配置读取入口。"""

    def _cfg(self, key: str):
        """读取配置项：默认值与类型来自 `_conf_schema.json`。"""
        return cfg_get(self.config, key)
