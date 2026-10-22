"""表情包候选曝光 / 爱用 / effective_use_count 排序（数据层契约）。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from neobot_app.emoji.service import EmojiEntry, EmojiService
from neobot_storage import create_engine, make_uow_factory
from neobot_storage.models import Base


def _png_bytes(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color=color).save(output, format="PNG")
    return output.getvalue()


def _engine_factory(tmp_path):
    engine = create_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'emoji_favorites.sqlite3').as_posix()}"
    )

    async def _setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    return engine, _setup


async def _import(service: EmojiService, name: str, color: str, text: str):
    return await service.add_image_bytes(
        _png_bytes(color), file_name=name, analysis_text=text
    )


def test_emoji_entry_defaults_and_effective_use_count():
    """新字段默认 0/False；排序权重 = use_count + exposure_score。"""
    entry = EmojiEntry(file_name="a.png", file_path=Path("a.png"), analysis_text="A")
    assert entry.exposure_score == 0.0
    assert entry.is_favorite is False
    assert entry.effective_use_count == 0.0

    used = EmojiEntry(
        file_name="b.png",
        file_path=Path("b.png"),
        analysis_text="B",
        use_count=2,
        exposure_score=1.5,
    )
    assert used.effective_use_count == 3.5


@pytest.mark.asyncio
async def test_record_candidate_round_adds_exposure_not_use_count(tmp_path):
    """候选轮每个编号 +0.5 曝光（去重、未知编号忽略），use_count 不变。"""
    engine, setup = _engine_factory(tmp_path)
    await setup()
    uow_factory = make_uow_factory(engine)
    service = EmojiService(data_dir=tmp_path, uow_factory=uow_factory)
    try:
        first = await _import(service, "a.png", "red", "猫猫")
        second = await _import(service, "b.png", "blue", "狗狗")

        await service.record_candidate_round([first.number, second.number, second.number], selected=first.number)
        assert service.get_entry(first.number).exposure_score == 0.5
        assert service.get_entry(second.number).exposure_score == 0.5
        assert service.get_entry(first.number).use_count == 0

        await service.record_candidate_round([first.number])
        await service.record_candidate_round([999])  # 未知编号不抛错、不改状态
        assert service.get_entry(first.number).exposure_score == 1.0
        assert service.get_entry(second.number).exposure_score == 0.5

        # 真实发送只加 use_count，曝光分保持不变
        await service.record_usage(first.number)
        assert service.get_entry(first.number).use_count == 1
        assert service.get_entry(first.number).exposure_score == 1.0

        async with uow_factory() as uow:
            record = await uow.emojis.get_by_hash(first.entry.file_hash)
        assert record is not None
        assert record.exposure_score == 1.0
        assert record.use_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_context_candidates_favorites_first_then_lowest_random(tmp_path):
    """爱用按 effective 排序在前；随机槽位取非爱用里 effective 最低的，不重叠。"""
    engine, setup = _engine_factory(tmp_path)
    await setup()
    service = EmojiService(data_dir=tmp_path, uow_factory=make_uow_factory(engine))
    try:
        entries = [
            await _import(service, f"{name}.png", color, text)
            for name, color, text in (
                ("a", "red", "A"),
                ("b", "blue", "B"),
                ("c", "green", "C"),
                ("d", "yellow", "D"),
                ("e", "purple", "E"),
            )
        ]
        nums = [entry.number for entry in entries]
        assert await service.set_favorite_emojis(nums[:2]) == {
            "ok": True,
            "favorites": nums[:2],
            "count": 2,
            "limit": 10,
        }
        await service.record_usage(nums[0])          # 爱用1 effective 1
        await service.record_candidate_round([nums[2]])   # 非爱用3 effective 0.5
        await service.record_candidate_round([nums[3]])
        await service.record_candidate_round([nums[3]])   # 非爱用4 effective 1.0

        candidates = service.pick_context_candidates(random_limit=2, favorite_limit=10)
        numbers = [number for number, _ in candidates]
        # 爱用：[2(0), 1(1)]；随机：5(0), 3(0.5)
        assert numbers == [nums[1], nums[0], nums[4], nums[2]]
        assert len(set(numbers)) == len(numbers)

        # favorite_limit 截断爱用，但爱用仍不占随机槽位
        capped = service.pick_context_candidates(random_limit=2, favorite_limit=1)
        assert [number for number, _ in capped] == [nums[1], nums[4], nums[2]]

        # 清空爱用后只剩随机组：两个旧爱用回到非爱用池（effective 分别是 1 和 0）
        assert (await service.set_favorite_emojis([]))["ok"] is True
        only_random = service.pick_context_candidates(random_limit=2, favorite_limit=10)
        assert [number for number, _ in only_random] == [nums[1], nums[4]]
        assert all(not entry.is_favorite for _, entry in only_random)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_favorite_emojis_replaces_group_persists_and_validates(tmp_path):
    """整组替换 + 落库 + 非法编号/超上限拒绝；空列表清空。"""
    engine, setup = _engine_factory(tmp_path)
    await setup()
    uow_factory = make_uow_factory(engine)
    service = EmojiService(
        data_dir=tmp_path, uow_factory=uow_factory, favorite_limit=2
    )
    try:
        entries = [
            await _import(service, f"{name}.png", color, text)
            for name, color, text in (
                ("a", "red", "A"),
                ("b", "blue", "B"),
                ("c", "green", "C"),
            )
        ]
        nums = [entry.number for entry in entries]

        result = await service.set_favorite_emojis(nums[:2])
        assert result["ok"] is True
        assert result["count"] == 2 and result["limit"] == 2
        assert [service.get_entry(n).is_favorite for n in nums] == [True, True, False]

        # 重新扫描（从数据库读回）后标记仍在
        await service._scan_folder()
        assert [service.get_entry(n).is_favorite for n in nums] == [True, True, False]

        # 整组替换：只留第二个
        await service.set_favorite_emojis([nums[1]])
        assert [service.get_entry(n).is_favorite for n in nums] == [False, True, False]

        # 非法编号：返回错误且不改状态
        invalid = await service.set_favorite_emojis([nums[1], 999])
        assert invalid["ok"] is False
        assert "999" in invalid["error"]
        assert invalid["invalid_numbers"] == [999]
        assert [service.get_entry(n).is_favorite for n in nums] == [False, True, False]

        # 超上限：返回错误且不改状态（limit=2）
        too_many = await service.set_favorite_emojis(nums)
        assert too_many["ok"] is False
        assert "最多 2 个" in too_many["error"]
        assert [service.get_entry(n).is_favorite for n in nums] == [False, True, False]

        # 空列表清空
        cleared = await service.set_favorite_emojis([])
        assert cleared == {"ok": True, "favorites": [], "count": 0, "limit": 2}
        assert [service.get_entry(n).is_favorite for n in nums] == [False, False, False]

        async with uow_factory() as uow:
            records = [await uow.emojis.get_by_hash(entry.entry.file_hash) for entry in entries]
        assert [record.is_favorite for record in records] == [False, False, False]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_and_search_use_effective_order_and_favorite_mark(tmp_path):
    """列表/分页/搜索按 effective 排序；list_text 与条目都带爱用标记。"""
    engine, setup = _engine_factory(tmp_path)
    await setup()
    service = EmojiService(data_dir=tmp_path, uow_factory=make_uow_factory(engine))
    try:
        a = await _import(service, "a.png", "red", "猫猫")
        b = await _import(service, "b.png", "blue", "狗狗")
        c = await _import(service, "c.png", "green", "兔兔")
        await service.set_favorite_emojis([a.number])
        await service.record_usage(c.number)             # c effective 1
        await service.record_candidate_round([b.number])  # b effective 0.5

        ordered = [number for number, _ in service.list_entries()]
        assert ordered == [a.number, b.number, c.number]

        page, total, has_more = service.list_entries_paginated(0, 2)
        assert [number for number, _ in page] == [a.number, b.number]
        assert (total, has_more) == (3, True)

        text = service.build_list_text()
        lines = text.splitlines()[1:]
        assert "[爱用]" in lines[0]
        assert "[爱用]" not in lines[1]
        assert text.count("[爱用]") == 1
        assert "共3个表情包" in text

        found = service.search_entries("猫")
        assert [number for number, _ in found] == [a.number]
        assert found[0][1].is_favorite is True
    finally:
        await engine.dispose()
