"""
群名片解析（纯函数，不依赖 AstrBot 与数据库，可直接单测）。

从群名片文本中提取具体信仰、命途、职业与玩家名。

这段逻辑原先住在 main.py 的插件类里，只能通过整插件实例调用，测试根本碰不到；
而它恰好是最容易出错的类型——三种信仰来源的优先级、具体职业名的前缀匹配、
非关键词与玩家名的划分。抽成纯函数后可以直接构造名片文本断言结果。

放在插件根目录，与 item_utils.py / text_utils.py 同级。
"""

from typing import Dict, List, Optional, Tuple

from astrbot_plugin_faith_ladder.models import (
    FAITH_TO_PATH,
    VALID_CLASSES,
    VALID_FAITHS,
    VALID_PATHS,
)
from astrbot_plugin_faith_ladder.text_utils import BRACKET_TAG_RE, CARD_BRACKET_RE, CARD_CONTENT_RE

# 具体职业映射表的元素类型：(信仰, 命途, 基础职业)
SpecificClassEntry = Tuple[str, Optional[str], str]


def extract_card_words(card: str) -> List[str]:
    """取出名片中的关键词：剥掉开头的【标签】，去掉纯数字词。

    "【繁荣】战士 张三" → ["战士", "张三"]
    """
    text = (card or "").strip()
    match = CARD_BRACKET_RE.match(text)
    remaining = match.group(1).strip() if match else text
    return [w for w in remaining.split() if not w.isdigit()]


def extract_specific_faith(card: str) -> Optional[str]:
    """从名片中任意位置的【标签】里取具体信仰名；不是有效信仰则返回 None。

    仅看标签，不看正文；"【欺诈】法师 李四" → "欺诈"。
    """
    match = BRACKET_TAG_RE.search(card or "")
    if match:
        tag = match.group(1).strip()
        if tag in FAITH_TO_PATH:
            return tag
    return None


def parse_card_info(card: str, sorted_specific_classes: List[Tuple[str, SpecificClassEntry]]) -> Dict[str, Optional[str]]:
    """解析群名片，返回 {"specific_faith", "faith", "class_", "player_name"}（均可为 None）。

    specific_faith 是具体信仰（16 个之一，如"繁荣"），faith 是由它推出的命途
    （6 个之一，如"生命"）。

    具体信仰有三个来源，按出现顺序取第一个命中的：
    1. 【标签】本身是具体信仰（如【繁荣】）；标签不在开头也会全文找
    2. 具体职业名（specific_classes.json 中每个具体职业都对应一个信仰）
    3. 名片中直接出现的具体信仰词

    标签里写命途（如【生命】）只补全命途，不算具体信仰——与正文里出现
    命途词的处理保持一致；否则名片写着命途却解析不出来。

    sorted_specific_classes 需按职业名长度降序排列（调用方负责），
    以保证"魔术师"先于更短的职业名匹配到。

    多个职业词同时出现时的取舍沿用了原实现：具体职业一旦命中就定下职业，
    普通职业词则后者覆盖前者；玩家名由剩余的非关键词拼接而成。
    """
    result: Dict[str, Optional[str]] = {
        "specific_faith": None, "faith": None, "class_": None, "player_name": None,
    }
    text = (card or "").strip()

    # 1. 开头的【标签】
    tag = None
    match = CARD_CONTENT_RE.match(text)
    if match:
        tag = match.group(1).strip()
        remaining = match.group(2).strip()
    else:
        # 标签不在开头（如 "Lv.9【生命】战士 张三"）：全文找一个，
        # 与 extract_specific_faith 的结论保持一致（同一个名片不该两个答案）；
        # 顺便把标签从待分类文本里摘掉，免得 "Lv.9【生命】战士" 整段被当成玩家名
        found = BRACKET_TAG_RE.search(text)
        if found:
            tag = found.group(1).strip()
            remaining = BRACKET_TAG_RE.sub(" ", text).strip()
        else:
            remaining = text

    if tag:
        if tag in VALID_FAITHS:
            result["specific_faith"] = tag
            result["faith"] = FAITH_TO_PATH.get(tag)
        elif tag in VALID_PATHS:
            # 命途写在标签里也要认：同一个词写在正文里本就能识别（见下方 words 循环），
            # 只认 16 个具体信仰会让「【生命】战士 张三」解析不出命途，
            # 录入时直接报"缺少必要参数: 命途"
            result["faith"] = tag

    # 2. 非数字词
    words = [w for w in remaining.split() if not w.isdigit()]

    name_parts: List[str] = []

    for word in words:
        # 具体职业：完全匹配，或以具体职业名开头（如"魔术师1218"）
        found_specific = False
        for specific_name, (specific_faith, specific_path, specific_class) in sorted_specific_classes:
            if word == specific_name:
                result["class_"] = specific_class
                if result["specific_faith"] is None:
                    result["specific_faith"] = specific_faith
                if result["faith"] is None:
                    result["faith"] = specific_path
                found_specific = True
                break
            elif word.startswith(specific_name) and len(word) > len(specific_name):
                result["class_"] = specific_class
                if result["specific_faith"] is None:
                    result["specific_faith"] = specific_faith
                if result["faith"] is None:
                    result["faith"] = specific_path
                remainder = word[len(specific_name):]
                if remainder and not remainder.isdigit():
                    name_parts.append(remainder)
                found_specific = True
                break

        if found_specific:
            continue

        # 普通职业：只记录职业，不推断信仰
        # （与原实现一致：后面出现的普通职业会覆盖前面的）
        if word in VALID_CLASSES:
            result["class_"] = word
            continue

        # 具体信仰词：补全信仰与命途，不计入玩家名
        if word in VALID_FAITHS:
            if result["specific_faith"] is None:
                result["specific_faith"] = word
            if result["faith"] is None:
                result["faith"] = FAITH_TO_PATH.get(word)
            continue

        # 命途词：只补全命途，不计入玩家名
        if word in VALID_PATHS:
            if result["faith"] is None:
                result["faith"] = word
            continue

        # 其余非关键词计入玩家名
        name_parts.append(word)

    if name_parts:
        result["player_name"] = "".join(name_parts)

    return result
