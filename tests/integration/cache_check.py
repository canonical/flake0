import time


def warm_cache(n=4_000_000):
    total = 0
    for i in range(n):
        total += i * i
    return total


def test_cache_warmup_meets_deadline():
    start = time.monotonic()
    warm_cache()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"cache warm-up took {elapsed:.2f}s, deadline 1.0s"
