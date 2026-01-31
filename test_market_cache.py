"""Tests for the market list cache (issue #45)."""

import time


from market_cache import MarketListCache


def test_cache_miss_on_empty():
    cache = MarketListCache(ttl_seconds=5)
    assert cache.get("open") is None


def test_set_and_get():
    cache = MarketListCache(ttl_seconds=5)
    data = [{"id": "1", "title": "test"}]
    entry = cache.set("open", data)

    assert entry["data"] is data
    assert entry["etag"].startswith('W/"')

    hit = cache.get("open")
    assert hit is not None
    assert hit["data"] is data


def test_different_status_filters_are_independent():
    cache = MarketListCache(ttl_seconds=5)
    cache.set("open", [{"id": "1"}])
    cache.set("all", [{"id": "1"}, {"id": "2"}])

    assert len(cache.get("open")["data"]) == 1
    assert len(cache.get("all")["data"]) == 2


def test_ttl_expiry():
    cache = MarketListCache(ttl_seconds=0.05)  # 50ms
    cache.set("open", [{"id": "1"}])

    assert cache.get("open") is not None
    time.sleep(0.06)
    assert cache.get("open") is None


def test_invalidate_clears_all_filters():
    cache = MarketListCache(ttl_seconds=60)
    cache.set("open", [{"id": "1"}])
    cache.set("all", [{"id": "1"}, {"id": "2"}])
    cache.set("RESOLVED", [])

    cache.invalidate()

    assert cache.get("open") is None
    assert cache.get("all") is None
    assert cache.get("RESOLVED") is None


def test_invalidate_bumps_generation():
    cache = MarketListCache(ttl_seconds=60)
    gen_before = cache.generation

    cache.invalidate()

    assert cache.generation == gen_before + 1


def test_invalidate_updates_last_modified():
    cache = MarketListCache(ttl_seconds=60)
    before = cache.last_modified

    time.sleep(0.01)
    cache.invalidate()

    assert cache.last_modified > before


def test_etag_changes_after_invalidation():
    cache = MarketListCache(ttl_seconds=60)
    data = [{"id": "1"}]

    entry1 = cache.set("open", data)
    etag1 = entry1["etag"]

    cache.invalidate()

    entry2 = cache.set("open", data)
    etag2 = entry2["etag"]

    assert etag1 != etag2, "ETag should change after invalidation even for same data"


def test_etag_format():
    cache = MarketListCache(ttl_seconds=60)
    entry = cache.set("open", [{"id": "1"}])
    etag = entry["etag"]

    # Weak ETag format: W/"..."
    assert etag.startswith('W/"')
    assert etag.endswith('"')
