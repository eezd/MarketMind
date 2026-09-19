"""金十正文边界：图片仅保留链接，不能冒充完整文本或吞掉空正文。"""

from marketmind.adapters.jin10 import _article


def test_image_only_detail_preserves_links_without_claiming_full_text():
    article = _article(
        {"id": 1, "introduction": "图解摘要", "content": '<p><img src="https://img.jin10.com/chart.jpg"></p>'},
        detail=True,
    )
    assert article.body_status == "summary_only"
    assert article.body_text is None
    assert article.summary == "图解摘要"
    assert {"kind": "image", "url": "https://img.jin10.com/chart.jpg"} in article.source_tags
    assert {"gap": "image_only_content"} in article.source_tags


def test_cover_image_does_not_turn_missing_body_into_image_article():
    article = _article({"id": 2, "content": "", "web_thumbs": ["https://img.jin10.com/cover.jpg"]}, detail=True)
    assert article.body_status == "pending"
    assert article.body_text is None
