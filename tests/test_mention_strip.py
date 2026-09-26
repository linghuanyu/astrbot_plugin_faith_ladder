"""
@ 提及文本的剥离（纯函数）。

线上两次报障的根因都在这里：aiocqhttp 适配器把**整张群名片**拼成
"@昵称(QQ)" 写进 message_str，而名片可以带空格：

    "录入玩家 @【混乱】子琅 渔夫 1000 100(2921544554)"

旧实现只按第一个空白截断（`@\\S+`），剩下的 "渔夫 1000 100(2921544554)"
落进参数分类 —— 9-24 漏成「渔夫」顶掉名片姓名，9-26 漏成 "100(2314617721)"
直接落库成玩家名。正则永远做不到整段剥离，所以 strip_mentions 必须收到
消息段里的 (昵称, QQ) 原文，用精确替换来剥。
"""

from astrbot_plugin_faith_ladder.text_utils import (
    extract_args,
    looks_like_mention_tail,
    strip_mentions,
)

# 真实形态：名片【混乱】子琅 渔夫 1000 100（「渔夫」= 混乱·猎人的具体职业名）
LEAK_CARD = "【混乱】子琅 渔夫 1000 100"
LEAK_QQ = "2921544554"
LEAK_MSG = f"录入玩家 @{LEAK_CARD}({LEAK_QQ})"
LEAK_MENTIONS = [(LEAK_CARD, LEAK_QQ)]


class TestStripMentions:
    def test_multi_word_card_mention_is_removed_whole(self):
        assert extract_args(LEAK_MSG, "录入玩家", LEAK_MENTIONS) == ""

    def test_args_after_multi_word_mention_survive(self):
        args = extract_args(LEAK_MSG + " 虚无 战士", "录入玩家", LEAK_MENTIONS)
        assert args == "虚无 战士", "剩余尾巴不许混进参数"

    def test_mention_with_qq_and_components(self):
        assert extract_args("录入积分 @张三(1234567) 100 50", "录入积分",
                            [("张三", "1234567")]) == "100 50"

    def test_mention_without_components_falls_back_to_regex(self):
        """没有消息段可比对时（老宿主/异常），昵称不含空格的形态仍要剥掉。"""
        assert extract_args("录入积分 @张三(1234567) 100 50", "录入积分") == "100 50"

    def test_mention_without_nickname(self):
        """拿不到昵称时适配器可能只留下 @(QQ) 形态：整个剥掉，别把 QQ 带进参数。"""
        assert extract_args("录入玩家 @(1234567) 张三 生命 战士", "录入玩家") == "张三 生命 战士"

    def test_bare_mention(self):
        assert strip_mentions("@张三 100 50") == "100 50"

    def test_cq_code(self):
        assert strip_mentions("[CQ:at,qq=123456] 100 50") == "100 50"

    def test_at_all(self):
        assert strip_mentions(" @全体成员(all) 100 50") == "100 50"

    def test_log_outline_placeholder(self):
        """宿主日志概要把 At 渲染成 [At:QQ]；真进了参数也不能变成玩家名。"""
        assert strip_mentions("[At:2314617721] 虚无 战士") == "虚无 战士"

    def test_wrong_command_returns_empty(self):
        assert extract_args("录入玩家 张三 虚无 战士", "录入积分") == ""

    def test_alias_command_prefix(self):
        assert extract_args("register 张三 虚无 战士", "register") == "张三 虚无 战士"

    def test_empty_message(self):
        assert extract_args("", "录入玩家") == ""
        assert extract_args("录入玩家", "录入玩家") == ""


class TestMentionTail:
    def test_tail_with_matching_qq(self):
        assert looks_like_mention_tail(f"100({LEAK_QQ})", [LEAK_QQ])

    def test_other_qq_is_not_a_tail(self):
        """有 @ 名单时只认名单里的号码，别的括号数字不能误伤。"""
        assert not looks_like_mention_tail("复读机(2921544554)", ["1234567"])

    def test_tail_without_qq_list_uses_length(self):
        assert looks_like_mention_tail(f"100({LEAK_QQ})")
        assert not looks_like_mention_tail("复读机(2023)")

    def test_plain_names(self):
        assert not looks_like_mention_tail("子琅", [LEAK_QQ])
        assert not looks_like_mention_tail("100", [LEAK_QQ])
        assert not looks_like_mention_tail("", [LEAK_QQ])


class TestAtSegmentsFromEvent:
    """消息段 → @ 文本 / @ 目标（走真实插件方法，桩掉框架组件）。"""

    class _Event:
        def __init__(self, segments, self_id="1"):
            self._segments = segments
            self._self_id = self_id

        def get_messages(self):
            return self._segments

        def get_self_id(self):
            return self._self_id

    @staticmethod
    def _plugin():
        import astrbot_plugin_faith_ladder.main as m
        return m.FaithLadderPlugin(m.Context(), {})

    @staticmethod
    def _at(**kwargs):
        from astrbot.core.message.components import At
        return At(**kwargs)

    async def test_none_qq_is_skipped(self, stubbed_astrbot):
        """缺字段时 str(None)=="None" 也是真值：不跳过会被写进绑定并让 int() 抛错。"""
        plugin = self._plugin()
        event = self._Event([self._at(qq=None), self._at(qq="2314617721")])
        assert await plugin._get_at_user_id(event) == "2314617721"

    async def test_all_and_self_are_skipped(self, stubbed_astrbot):
        plugin = self._plugin()
        event = self._Event([self._at(qq="all"), self._at(qq="1")])
        assert await plugin._get_at_user_id(event) is None

    async def test_mention_texts_collects_every_at(self, stubbed_astrbot):
        """所有 @ 段都要收（含 @全体成员），它们都会出现在 message_str 里。"""
        plugin = self._plugin()
        event = self._Event([self._at(qq="all"), self._at(qq="777")])
        assert plugin._mention_texts(event) == [("", "all"), ("", "777")]

    async def test_mention_texts_survives_broken_event(self, stubbed_astrbot):
        plugin = self._plugin()

        class _Broken:
            def get_messages(self):
                raise RuntimeError("no messages")

        assert plugin._mention_texts(_Broken()) == []
