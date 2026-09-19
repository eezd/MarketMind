"""真实 PostgreSQL 回归验证；仅接收显式隔离测试库，所有写入回滚。"""

import os
from collections.abc import Iterator
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def connection() -> Iterator[Connection]:
    url = os.environ.get("MM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MM_TEST_DATABASE_URL to an isolated migrated PostgreSQL database")
    database = make_url(url).database or ""
    if not database.startswith("marketmind_") or not database.endswith("_verify"):
        pytest.fail("Integration tests require a marketmind_*_verify database")
    engine = create_engine(url)
    try:
        with engine.connect() as conn, conn.begin() as transaction:
            yield conn
            transaction.rollback()
    finally:
        engine.dispose()


def create_news(connection: Connection) -> tuple[str, str]:
    news_id, revision_id = str(uuid4()), str(uuid4())
    connection.execute(
        text("""INSERT INTO news_items (id,source_id,source_item_id,canonical_url,original_url)
        VALUES (:id,'00000000-0000-0000-0000-000000000001',:source_item,:url,:url)"""),
        {"id": news_id, "source_item": news_id, "url": f"https://example.invalid/{news_id}"},
    )
    connection.execute(
        text("""INSERT INTO news_revisions (id,news_id,title,body_text,body_status,content_hash)
        VALUES (:revision,:news,'','无标题的完整快讯正文','complete',:hash)"""),
        {"revision": revision_id, "news": news_id, "hash": sha256(b"complete").hexdigest()},
    )
    connection.execute(
        text("UPDATE news_items SET current_revision_id=:revision WHERE id=:news"),
        {"revision": revision_id, "news": news_id},
    )
    return news_id, revision_id


def test_complete_flash_without_title_is_preserved(connection: Connection) -> None:
    _, revision_id = create_news(connection)
    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    row = connection.execute(text("SELECT title,body_text FROM news_revisions WHERE id=:id"), {"id": revision_id}).one()
    assert row.title == ""
    assert row.body_text == "无标题的完整快讯正文"


def test_revision_cannot_be_overwritten(connection: Connection) -> None:
    _, revision_id = create_news(connection)
    with pytest.raises(IntegrityError) as error, connection.begin_nested():
        connection.execute(text("UPDATE news_revisions SET body_text='覆盖正文' WHERE id=:id"), {"id": revision_id})
    assert error.value.orig.sqlstate == "23514"
    assert (
        connection.execute(text("SELECT body_text FROM news_revisions WHERE id=:id"), {"id": revision_id}).scalar_one()
        == "无标题的完整快讯正文"
    )


def test_summary_cannot_replace_complete_revision(connection: Connection) -> None:
    news_id, revision_id = create_news(connection)
    summary_id = str(uuid4())
    connection.execute(
        text("""INSERT INTO news_revisions (id,news_id,title,summary,body_status,content_hash)
        VALUES (:id,:news,'','登录失效后仅有摘要','summary_only',:hash)"""),
        {"id": summary_id, "news": news_id, "hash": sha256(b"summary").hexdigest()},
    )
    with pytest.raises(IntegrityError) as error, connection.begin_nested():
        connection.execute(
            text("UPDATE news_items SET current_revision_id=:revision WHERE id=:news"),
            {"revision": summary_id, "news": news_id},
        )
    assert error.value.orig.sqlstate == "23514"
    assert (
        str(
            connection.execute(
                text("SELECT current_revision_id FROM news_items WHERE id=:id"), {"id": news_id}
            ).scalar_one()
        )
        == revision_id
    )
