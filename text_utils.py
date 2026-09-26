"""
消息文本处理：CQ 码、@ 提及、名片标签等共用正则与剥离函数。

放在插件根目录（与 item_utils.py 同级）是因为 main.py 与 commands/ 下的多个模块
都要用到；集中一处可避免各模块各写一份正则后逐渐漂移。
"""

import re
from typing import Iterable, List, Optional, Tuple

# CQ 码，如 [CQ:at,qq=123456]
CQ_CODE_RE = re.compile(r'\[CQ:[^\]]+\]')

# 带 QQ 的 @ 提及文本：aiocqhttp 适配器会把 "@昵称(QQ)" 拼进 message_str，
# 不剥掉的话「录入积分 @张三 100 50」会把目标解析成 "@张三(12345)"。
#
# 只能兜住**昵称不含空格**的形态：拼进去的是群名片，而名片可以带空格
# （"@【混乱】子琅 渔夫 1000 100(2921544554)"），正则再怎么写都停在这个空格上，
# 整段剥离只能靠消息段原文（见 strip_mentions 的 mentions 参数）。
AT_MENTION_WITH_QQ_RE = re.compile(r'@\S*?\((?:all|\d+)\)')

# 没有 QQ 括号的 @ 提及（兜底，如 "@张三"）
AT_MENTION_RE = re.compile(r'@\S+')

# 宿主日志概要（get_message_outline）把 At 渲染成 "[At:QQ]"。message_str 里本该
# 没有它，但适配器/宿主版本一变就说不准——落进参数就会变成玩家名候选，防御性剥掉。
AT_PLACEHOLDER_RE = re.compile(r'\[At:[^\]]*\]')

# @ 提及文本被剥剩半截时的尾巴："100(2921544554)"
MENTION_TAIL_RE = re.compile(r'^.*\((\d+)\)$')

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


def collect_mention_texts(messages) -> List[Tuple[str, str]]:
    """收集消息段里所有 @ 段的 (昵称, QQ)——就是适配器写进 message_str 的原文。

    At 与 AtAll 都要收：前者的 qq 是用户号，后者是 "all"（@全体成员），两种文本
    都在 message_str 里出现过，都得从参数里剥干净。
    无 AstrBot 环境或段结构不同（老版本没有 AtAll）时返回空列表，交给正则兜底。
    """
    try:
        from astrbot.core.message.components import At
    except ImportError:
        return []
    try:
        from astrbot.core.message.components import AtAll
        at_classes = (At, AtAll)
    except ImportError:
        at_classes = (At,)

    found: List[Tuple[str, str]] = []
    for seg in messages or ():
        if isinstance(seg, at_classes):
            found.append((
                str(getattr(seg, "name", "") or ""),
                str(getattr(seg, "qq", "")),
            ))
    return found


def strip_mentions(text: str, mentions: Iterable[Tuple[str, str]] = ()) -> str:
    """剥掉 CQ 码、@ 提及与 @ 占位符，返回用户真正输入的内容。

    mentions 是消息段里的 (昵称, QQ)，**有事件时必须传**：适配器把**整张群名片**
    拼成 "@昵称(QQ)" 写进 message_str，而名片可以带空格
    （"@【混乱】子琅 渔夫 1000 100(2921544554)"）。正则只能剥到第一个空格，剩下的
    "渔夫 1000 100(2921544554)" 会被当成参数："渔夫"顶掉名片姓名、"100(2921544554)"
    直接成了玩家名（线上真出现过这两种）。精确整段替换是唯一可靠的做法——拼进去的
    就是同一个字符串。
    """
    text = CQ_CODE_RE.sub('', text or '')
    text = AT_PLACEHOLDER_RE.sub('', text)
    for name, qq in mentions:
        if name:
            text = text.replace(f"@{name}({qq})", " ")
        text = text.replace(f"@{qq}", " ")
    text = AT_MENTION_WITH_QQ_RE.sub('', text)
    return AT_MENTION_RE.sub('', text).strip()


def extract_args(
    message_str: str, cmd_name: str, mentions: Iterable[Tuple[str, str]] = ()
) -> str:
    """取命令名之后的参数文本（精确前缀匹配），并剥掉 @ 提及。

    AstrBot 的唤醒阶段会把唤醒前缀（"/"）剥掉，所以 handler 看到的 message_str
    以命令名开头；不是这条命令就返回空串（调用方会用别名再试一次）。
    """
    text = (message_str or "").strip()
    if not text.startswith(cmd_name):
        return ""
    return strip_mentions(text[len(cmd_name):], mentions)


def looks_like_mention_tail(word: str, at_qqs: Iterable[str] = ()) -> bool:
    """这个词是不是 @ 提及的残尾（"100(2921544554)" 这种形态）。

    strip_mentions 万一没兜住（适配器又换了形态），这种尾巴会被当成玩家名候选，
    录出一个假玩家。给了 at_qqs（本消息里所有 @ 的 QQ）时要求括号里的号码与其中
    之一相同；没给时退化为"看起来像 QQ"（≥5 位数字）——正常玩家名里不会有。
    """
    match = MENTION_TAIL_RE.match(word or "")
    if not match:
        return False
    digits = match.group(1)
    known = {str(qq) for qq in at_qqs if qq}
    if known:
        return digits in known
    return len(digits) >= 5


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

