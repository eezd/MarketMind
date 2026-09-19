"""来源适配器与持久化层的公共数据边界。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Article(BaseModel):
    source_item_id: str | None
    canonical_url: str
    original_url: str
    title: str = ""
    summary: str | None = None
    body_text: str | None = None
    body_status: str
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    source_time_text: str | None = None
    source_timezone: str | None = None
    author: str | None = None
    source_tags: list[Any] = Field(default_factory=list)
    importance: int | None = None


class PageResult(BaseModel):
    articles: list[Article]
    next_url: str | None = None
    next_cursor: str | None = None
    exhausted: bool = False
    coverage_gap: dict[str, Any] | None = None
    raw_text: str
    response_status: int
    content_type: str
    fetched_at: datetime
    parser_version: str


class SourceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
