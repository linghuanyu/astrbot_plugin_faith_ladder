"""
数据库写入的串行化与多步事务。

背景：共享一条 aiosqlite 连接时，aiosqlite 只串行化**单条**语句。一条逻辑操作
的多个 await 之间，别的命令可以插进来执行并 commit()，把这里的半成品一起提交
掉——于是 register_player 文档里写的"原子提交"和"注册回滚"都不成立。
"""

import asyncio

import pytest

from astrbot_plugin_faith_ladder.ladder_service import LadderService


async def _let_others_run(times: int = 5):
    """把控制权交给事件循环若干轮，让"本该被挡住的"任务有机会跑到锁上。

    不依赖挂钟时间：被锁挡住的任务会停在 acquire 上，多转几轮也不会推进。
    """
    for _ in range(times):
        await asyncio.sleep(0)


class TestWriteSerialization:
    async def test_other_writers_wait_for_open_transaction(self, db_manager):
        """事务未结束前，别的写入不能执行（否则它的 commit 会提交掉别人的半成品）。"""
        inside = asyncio.Event()
        release = asyncio.Event()

        async def transaction_owner():
            async with db_manager.transaction():
                await db_manager.upsert_player("g1", "u1", "张三", commit=False)
                inside.set()
                await release.wait()

        async def other_writer():
            await inside.wait()
            await db_manager.upsert_player("g1", "u2", "李四")

        owner = asyncio.create_task(transaction_owner())
        other = asyncio.create_task(other_writer())
        await inside.wait()

        # 事务还开着：另一个写入必须被挡在锁上。
        # 用「等一小会儿确认它没跑完」而不是「转几轮事件循环」——后者在机制
        # 坏掉时也可能因为轮数不够而通过（反过来证明不了什么）。
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(other), timeout=0.3)
        assert not other.done(), "事务进行中，其它写入必须排队等待"

        release.set()
        await asyncio.wait_for(asyncio.gather(owner, other), timeout=5)
        assert await db_manager.get_player_by_name("g1", "李四") is not None, "排队结束后写入应当生效"

    async def test_reader_inside_transaction_does_not_deadlock(self, db_manager):
        """块内调用读方法（也走同一把锁）必须是可重入的，不能自我阻塞。"""
        async def body():
            async with db_manager.transaction():
                await db_manager.upsert_player("g1", "u1", "张三", commit=False)
                player = await db_manager.get_player_by_name("g1", "张三")
                assert player is not None, "事务内的读要能看见自己刚写的数据"

        await asyncio.wait_for(body(), timeout=5)

    async def test_explicit_commit_inside_transaction_does_not_deadlock(self, db_manager):
        """块内自己调 commit()（有些调用方会这么写）不能死锁。"""
        async def body():
            async with db_manager.transaction():
                await db_manager.upsert_player("g1", "u1", "张三", commit=False)
                await db_manager.commit()

        await asyncio.wait_for(body(), timeout=5)

    async def test_transaction_rolls_back_on_error(self, db_manager):
        """块内抛错 → 整块回滚，不能留下半个玩家。"""
        with pytest.raises(RuntimeError):
            async with db_manager.transaction():
                await db_manager.upsert_player("g1", "u1", "张三", commit=False)
                raise RuntimeError("boom")

        assert await db_manager.get_player_by_name("g1", "张三") is None


class TestRegisterPlayerAtomicity:
    async def test_qq_conflict_rolls_back_whole_registration(self, db_manager):
        """绑定竞态被撞上时，注册的四步写入必须一起撤销。

        这条是 register_player 用 transaction() 的直接理由：此前四步各自提交，
        "注册回滚"并不真的发生，库里会留下一个没有 QQ 绑定的半成品玩家。
        真实竞态发生在预检查与写入之间（两个绑定请求同时通过检查），
        所以这里让预检查"没看见"已有的绑定，把流程逼到写入时才发现冲突。
        """
        await db_manager.upsert_player("g1", "u1", "张三")
        await db_manager.set_player_qq("g1", "u1", "222")

        async def blind_precheck(*args, **kwargs):
            return None  # 预检查那一刻这个 QQ 还没被绑走

        db_manager.get_player_by_qq = blind_precheck

        service = LadderService(db_manager)
        ok, message = await service.register_player(
            "g1", "李四", "生命", "战士", 1000, 100, "admin", qq_id="222"
        )

        assert ok is False
        assert "注册回滚" in message, message
        assert await db_manager.get_player_by_name("g1", "李四") is None, "必须整体回滚"
        # 原有绑定不受影响
        assert (await db_manager.get_player_by_name("g1", "张三")).qq_id == "222"

    async def test_successful_registration_binds_everything(self, db_manager):
        service = LadderService(db_manager)
        ok, _ = await service.register_player(
            "g1", "李四", "生命", "战士", 1200, 60, "admin",
            qq_id="333", specific_faith="繁荣",
        )

        assert ok is True
        player = await db_manager.get_player_by_name("g1", "李四")
        assert player.class_ == "战士"
        assert player.faith == "生命"
        assert player.specific_faith == "繁荣"
        assert player.ladder_score == 1200
        assert player.pilgrimage_score == 60
        assert player.qq_id == "333"
