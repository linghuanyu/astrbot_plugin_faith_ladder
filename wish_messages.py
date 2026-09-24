"""
祈愿试炼文案库。

调性对标《诸神愚戏》：一支队伍在神明的注视下凑齐，然后一起被记住三天。
文案只描述这件事，不涉及分数与道具——**发车除了那条以队名命名的状态，不给任何
数值产出**（这是刻意的：不给就没人能刷）。

占位符（按池取用，缺的参数不写即可）：
- open / remind: {team} {leader} {count} {capacity} {missing} {minutes}
- depart:        {team} {members} {days} {leader}
- voided:        {team} {members} {slot}
- timeout:       {team} {count} {capacity}
- swap:          {team} {out} {in}

全部可在配置里按池覆盖：`wish_messages_{池名}`（留空用内置池；填了就整池替换）。
"""

import random

WISH_MESSAGES = {
    # 开团播报：这条是群里唯一的新鲜事入口，必须一眼看出「谁开的、缺几人、什么日子」
    "open": [
        "{leader} 起了个愿：【{team}】，招 {capacity} 人，现 {count} 人，缺 {missing}。",
        "有人敲了敲神明的门：「{team}」——{leader} 领头，还缺 {missing} 人。",
        "【{team}】挂上了名册的一角。{leader} 站在最前面，身后 {count} 人，缺 {missing}。",
    ],
    # 缺人提醒：隔一段时间催一次，顺带报剩余时间
    "remind": [
        "【{team}】还在等：{count}/{capacity}，还缺 {missing} 人，剩 {minutes} 分钟。",
        "{team} 没散，只是还差 {missing} 个人。（还剩 {minutes} 分钟）",
    ],
    # 发车：整段里最重要的一句，要让群里看懂「你们一起被记住了三天」
    "depart": [
        "{team} 齐了。{members}——这 {days} 天里，你们是同一场试炼的人。",
        "神明看了一眼名单：{members}。【{team}】，{days} 天。",
        "{team} 成行。{members}，往后 {days} 天，你们共用同一个名字。",
    ],
    # 名额被别队抢走：明确告知「不是你们的问题」
    "voided": [
        "【{team}】未能成行：{slot} 那场试炼的名额已经用尽。{members}，改日再来。",
        "名额先被人占了。【{team}】散在风里，{members} 什么也没带走。",
    ],
    # 超时解散：只在队里有人时才播报，空队不吵人
    "timeout": [
        "【{team}】等了太久，散了。{count} 个人，没能凑成一场试炼。",
        "再没有人应【{team}】的愿，它自己熄了。",
    ],
    # 诸神换人：名单是公开的承诺，动了就要说一声
    "swap": [
        "【{team}】的名册动了一笔：{out} 换成 {in}。",
        "{out} 的位置由 {in} 接下——{team} 仍是那一场试炼。",
    ],
}

WISH_KINDS = tuple(WISH_MESSAGES.keys())

GENERIC_WISH_MESSAGES = {
    "open": ["【{team}】开始招募：{count}/{capacity}，缺 {missing} 人。"],
    "remind": ["【{team}】{count}/{capacity}，还缺 {missing} 人，剩 {minutes} 分钟。"],
    "depart": ["{team} 成行：{members}（{days} 天）。"],
    "voided": ["【{team}】未能成行：{slot} 的名额已用尽。"],
    "timeout": ["【{team}】超时解散。"],
    "swap": ["【{team}】名单变更：{out} → {in}。"],
}


def pick_wish_line(kind: str, config=None) -> str:
    """取一句文案。配置池为空视为"没配"，与其它文案池的约定一致。"""
    from astrbot_plugin_faith_ladder.plugin_config import cfg_get

    if kind not in WISH_KINDS:
        return ""

    pool = cfg_get(config or {}, f"wish_messages_{kind}") or []
    if pool:
        return random.choice(pool)

    builtin = WISH_MESSAGES.get(kind) or GENERIC_WISH_MESSAGES.get(kind) or []
    return random.choice(builtin) if builtin else ""
