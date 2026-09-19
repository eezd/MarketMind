"""来源内提交有序、跨来源公平的新闻变更消费接口。"""

import base64
import binascii
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from marketmind.auth import Db, current_session
from marketmind.errors import ApiError
from marketmind.models import NewsChange, NewsRevision, Source

router = APIRouter(tags=["News changes"], dependencies=[Depends(current_session)])


@router.get("/news/changes")
def changes(
    db: Db,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
):
    sources = list(db.scalars(select(Source.id).order_by(Source.code)).all())
    offsets = {str(source): 0 for source in sources}
    turn = 0
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if value["version"] != 1 or set(value["offsets"]) != set(offsets):
                raise ValueError()
            if any(type(number) is not int or number < 0 for number in value["offsets"].values()):
                raise ValueError()
            offsets = value["offsets"]
            turn = value["turn"]
            if type(turn) is not int or not 0 <= turn < max(1, len(sources)):
                raise ValueError()
        except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError):
            raise ApiError(400, "invalid_cursor", "Invalid news change cursor") from None
    queues = {
        source: list(
            db.scalars(
                select(NewsChange)
                .where(NewsChange.source_id == source, NewsChange.change_id > offsets[str(source)])
                .order_by(NewsChange.change_id)
                .limit(limit)
            ).all()
        )
        for source in sources
    }
    positions = {source: 0 for source in sources}
    items = []
    idle = 0
    while sources and len(items) < limit and idle < len(sources):
        source = sources[turn]
        turn = (turn + 1) % len(sources)
        position = positions[source]
        if position >= len(queues[source]):
            idle += 1
            continue
        idle = 0
        row = queues[source][position]
        positions[source] += 1
        offsets[str(source)] = row.change_id
        items.append(
            {
                name: getattr(row, name)
                for name in ("source_id", "change_id", "news_id", "revision_id", "change_type", "observed_at")
            }
        )
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps({"version": 1, "offsets": offsets, "turn": turn}, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    # 即使本轮为空，也返回高水位以供轮询，而不是令调用方重新全量读取。
    return {"items": items, "next_cursor": next_cursor}


@router.get("/revisions/{identifier}")
def revision(identifier: UUID, db: Db):
    row = db.get(NewsRevision, identifier)
    if row is None:
        raise ApiError(404, "revision_not_found", "Revision does not exist")
    return {
        name: getattr(row, name)
        for name in (
            "id",
            "news_id",
            "title",
            "summary",
            "body_text",
            "body_status",
            "source_tags",
            "importance",
            "author",
            "published_at",
            "source_updated_at",
            "source_time_text",
            "source_timezone",
            "observed_at",
        )
    }
