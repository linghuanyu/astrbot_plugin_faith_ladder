"""
消息文本处理：CQ 码、@ 提及、名片标签等共用正则与剥离函数。

放在插件根目录（与 item_utils.py 同级）是因为 main.py 与 commands/ 下的多个模块
都要用到；集中一处可避免各模块各写一份正则后逐渐漂移。
"""

import re

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


def strip_mentions(text: str) -> str:
    """剥掉 CQ 码与 @ 提及文本，返回用户真正输入的内容。"""
    return AT_MENTION_RE.sub('', CQ_CODE_RE.sub('', text or '')).strip()
