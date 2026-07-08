from recipe_normalizer.ratelimit import SlidingWindowLimiter


def test_allows_up_to_max_then_blocks() -> None:
    lim = SlidingWindowLimiter(max_events=3, window_s=60.0)
    assert all(lim.check("k") for _ in range(3))
    assert lim.check("k") is False


def test_keys_are_independent() -> None:
    lim = SlidingWindowLimiter(max_events=1, window_s=60.0)
    assert lim.check("a") is True
    assert lim.check("b") is True


def test_window_expiry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import recipe_normalizer.ratelimit as rl

    t = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: t[0])
    lim = SlidingWindowLimiter(max_events=1, window_s=10.0)
    assert lim.check("k") is True
    assert lim.check("k") is False
    t[0] += 11.0
    assert lim.check("k") is True


def test_reset_clears_state() -> None:
    lim = SlidingWindowLimiter(max_events=1, window_s=60.0)
    assert lim.check("k") is True
    assert lim.check("k") is False
    lim.reset()
    assert lim.check("k") is True
