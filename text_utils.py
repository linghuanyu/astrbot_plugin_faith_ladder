"""
消息文本处理：CQ 码、@ 提及、名片标签等共用正则与剥离函数。

放在插件根目录（与 item_utils.py 同级）是因为 main.py 与 commands/ 下的多个模块
都要用到；集中一处可避免各模块各写一份正则后逐渐漂移。
"""

import re
from typing import Optional, Tuple

# CQ 码，如 [CQ:at,qq=123456]
CQ_CODE_RE = re.compile(r'\[CQ:[^\]]+\]')

# @ 提及文本：aiocqhttp 适配器会把 "@昵称(QQ)" 拼进 message_str，
# 不剥掉的话「录入积分 @张三 100 50」会把目标解析成 "@张三(12345)"
AT_MENTION_RE = re.compile(r'@\S+')

# 名片开头【标签】包裹整个名片（取【】之后的内容）
CARD_BRACKET_RE = re.compile(r'^【[^】]*】\s*(.*)')

# 名片开头【标签】后跟其余内容（同时取出标签本身）
CARD_CONTENT_RE = re.compile(r'^【([^】]*)】\s*(.*)')

# 文本中任意位置的【标签】（用于提取具体信仰）
BRACKET_TAG_RE = re.compile(r'【([^】]+)】')

# 祷词归一化：去掉所有非单词字符（保留中英文与数字）
PRAYER_NORMALIZE_RE = re.compile(r'[^\w]')

# 「旧名 → 新名」的显式分隔符（改名类指令用）。
# 状态名与队名都允许含空格，所以需要一个用户能写出来的分隔符。
RENAME_SEPARATORS = ("→", "->")


def strip_mentions(text: str) -> str:
    """剥掉 CQ 码与 @ 提及文本，返回用户真正输入的内容。"""
    return AT_MENTION_RE.sub('', CQ_CODE_RE.sub('', text or '')).strip()


def split_rename_pair(rest: str) -> Optional[Tuple[str, str]]:
    """把「旧名 新名」拆成两段，拆不出来返回 None。

    「重命名状态」与「祈愿管理 重命名」共用：名字可以含空格（与「添加状态」一致，
    `添加状态 张三 你 好 3` 的状态名是「你 好」），所以优先认显式分隔符 `→`
    （也接受 `->`）；没写分隔符时要求正好两段，此时名字不能含空格。
    """
    for separator in RENAME_SEPARATORS:
        if separator in rest:
            old_name, _, new_name = rest.partition(separator)
            old_name, new_name = old_name.strip(), new_name.strip()
            return (old_name, new_name) if old_name and new_name else None

    tokens = rest.split()
    if len(tokens) != 2:
        return None
    return tokens[0], tokens[1]

