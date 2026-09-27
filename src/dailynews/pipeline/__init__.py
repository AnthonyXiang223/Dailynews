"""确定性流水线:采集 → 粗筛 → 生成(离线 fallback)。"""
from .compose import compose_digest, rewrite_item_links
from .filter import fetch_candidates, match_and_score, select_candidates
from .ingest import ingest_all, normalize_url, stub_articles, url_hash

__all__ = [
    "ingest_all",
    "normalize_url",
    "stub_articles",
    "url_hash",
    "fetch_candidates",
    "match_and_score",
    "select_candidates",
    "compose_digest",
    "rewrite_item_links",
]
