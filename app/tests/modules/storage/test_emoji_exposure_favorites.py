"""表情包曝光分/爱用：迁移 0029 往返 + 仓库读写与排序。

旧库（迁移到 0028 的库）必须原样保留 use_count 等既有列，新列拿默认值；
不跑迁移的测试环境走 Base.metadata.create_all，列定义与迁移后的表结构一致。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest_asyncio

from neobot_storage.uow import make_uow_factory

from .archive_test_utils import create_memory_engine, create_schema


def _alembic_config(url: str):
    from alembic.config import Config

    from neobot_storage import engine as storage_engine

    pkg_dir = Path(storage_engine.__file__).resolve().parent
    cfg = Config(str(pkg_dir / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(pkg_dir / "alembic"))
    return cfg


def _emoji_columns(db: Path) -> dict[str, tuple]:
    connection = sqlite3.connect(str(db))
    try:
        rows = connection.execute("PRAGMA table_info(emojis)").fetchall()
    finally:
        connection.close()
    return {str(row[1]): row for row in rows}


def test_migration_0029_round_trip_keeps_existing_rows(tmp_path: Path) -> None:
    """0028 → head：新列带默认值且旧行数据不丢；downgrade 只删列，可往返。"""
    from alembic import command

    from neobot_storage.engine import sqlite_url

    db = tmp_path / "emoji_0029.db"
    cfg = _alembic_config(sqlite_url(db))

    command.upgrade(cfg, "0028")
    assert "exposure_score" not in _emoji_columns(db)
    assert "is_favorite" not in _emoji_columns(db)

    connection = sqlite3.connect(str(db))
    try:
        connection.execute(
            "INSERT INTO emojis "
            "(file_hash, file_name, file_path, use_count, created_at, updated_at, version) "
            "VALUES ('hash-old', 'old.png', 'old.png', 7, "
            "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000', 3)"
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(cfg, "head")
    columns = _emoji_columns(db)
    assert columns["exposure_score"][3] == 1, "exposure_score 必须非空"
    assert columns["exposure_score"][4] == "0", "既有行默认 0"
    assert columns["is_favorite"][3] == 1, "is_favorite 必须非空"
    assert columns["is_favorite"][4] == "0", "既有行默认 false"

    connection = sqlite3.connect(str(db))
    try:
        row = connection.execute(
            "SELECT file_hash, use_count, version, exposure_score, is_favorite "
            "FROM emojis WHERE file_hash = 'hash-old'"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("hash-old", 7, 3, 0.0, 0), "旧库既有数据必须原样保留"

    command.downgrade(cfg, "0028")
    downgraded = _emoji_columns(db)
    assert "exposure_score" not in downgraded
    assert "is_favorite" not in downgraded
    connection = sqlite3.connect(str(db))
    try:
        kept = connection.execute(
            "SELECT file_hash, use_count FROM emojis WHERE file_hash = 'hash-old'"
        ).fetchone()
    finally:
        connection.close()
    assert kept == ("hash-old", 7)

    command.upgrade(cfg, "head")
    assert "exposure_score" in _emoji_columns(db)
    assert "is_favorite" in _emoji_columns(db)


@pytest_asyncio.fixture
async def uow_factory():
    engine = create_memory_engine()
    await create_schema(engine)
    yield make_uow_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def repository(uow_factory):
    async with uow_factory() as uow:
        await uow.emojis.set("hash-a", file_name="a.png", file_path="a.png")
        await uow.emojis.set("hash-b", file_name="b.png", file_path="b.png")
        await uow.commit()
    return uow_factory


async def test_repository_exposure_and_favorites_round_trip(repository) -> None:
    """add_exposure/set_favorites 落库；increment_usage 只动 use_count。"""
    uow_factory = repository
    async with uow_factory() as uow:
        await uow.emojis.add_exposure("hash-a")           # 0.5
        await uow.emojis.add_exposure("hash-a", 1.5)      # +1.5 = 2.0
        await uow.emojis.increment_usage("hash-b")
        await uow.emojis.set_favorites(["hash-b"])
        await uow.commit()

    async with uow_factory() as uow:
        first = await uow.emojis.get_by_hash("hash-a")
        second = await uow.emojis.get_by_hash("hash-b")
    assert first is not None and second is not None
    assert (first.use_count, first.exposure_score, first.is_favorite) == (0, 2.0, False)
    assert (second.use_count, second.exposure_score, second.is_favorite) == (1, 0.0, True)

    # 排序权重 = use_count + exposure_score：a=2.0 排在 b=1.0 之后
    async with uow_factory() as uow:
        ordered = await uow.emojis.list(order_by_use_count=True)
        searched = await uow.emojis.search("png")
    assert [record.file_hash for record in ordered] == ["hash-b", "hash-a"]
    assert [record.file_hash for record in searched] == ["hash-b", "hash-a"]

    # 整组替换：空列表清空
    async with uow_factory() as uow:
        await uow.emojis.set_favorites([])
        await uow.commit()
    async with uow_factory() as uow:
        second = await uow.emojis.get_by_hash("hash-b")
    assert second.is_favorite is False
