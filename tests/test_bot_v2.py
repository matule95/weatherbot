"""Unit tests for bot_v2 helpers (no network)."""

import pytest

import bot_v2 as bv


def test_parse_temp_range_between():
    assert bv.parse_temp_range("Will it be between 46-47°F on March 7?") == (46.0, 47.0)


def test_parse_temp_range_or_below():
    assert bv.parse_temp_range("10°C or below on Tuesday") == (-999.0, 10.0)


def test_parse_temp_range_or_higher():
    assert bv.parse_temp_range("85°F or higher on Monday") == (85.0, 999.0)


def test_parse_temp_range_single():
    q = "Will the highest temperature in Chicago be 72°F on March 22?"
    r = bv.parse_temp_range(q)
    assert r == (72.0, 72.0)


def test_calc_ev_positive():
    assert bv.calc_ev(0.6, 0.4) > 0


def test_calc_ev_zero_price():
    assert bv.calc_ev(0.5, 0.0) == 0.0


def test_calc_kelly_fractional_scaled():
    k = bv.calc_kelly(0.55, 0.35)
    assert 0.0 <= k <= 1.0


def test_bucket_prob_tails():
    p = bv.bucket_prob(50.0, -999.0, 48.0, sigma=2.0)
    assert 0.0 <= p <= 1.0


def test_extract_yes_quotes_with_book():
    m = {"bestBid": "0.38", "bestAsk": "0.42", "outcomePrices": "[0.40, 0.60]"}
    bid, ask, mid, spread, has_book = bv.extract_yes_quotes(m)
    assert has_book is True
    assert bid == 0.38 and ask == 0.42
    assert spread == pytest.approx(0.04)


def test_extract_yes_quotes_mid_only():
    m = {"outcomePrices": "[0.35, 0.65]"}
    bid, ask, mid, spread, has_book = bv.extract_yes_quotes(m)
    assert has_book is False
    assert bid == ask == mid == 0.35


def test_check_market_resolved_yes(monkeypatch):
    class Resp:
        ok = True

        def json(self):
            return {"closed": True, "outcomePrices": "[0.99, 0.01]"}

    monkeypatch.setattr(bv.requests, "get", lambda *a, **k: Resp())
    assert bv.check_market_resolved("any-id") is True


def test_check_market_resolved_no(monkeypatch):
    class Resp:
        ok = True

        def json(self):
            return {"closed": True, "outcomePrices": "[0.02, 0.98]"}

    monkeypatch.setattr(bv.requests, "get", lambda *a, **k: Resp())
    assert bv.check_market_resolved("any-id") is False


def test_check_market_resolved_still_open(monkeypatch):
    class Resp:
        ok = True

        def json(self):
            return {"closed": False, "outcomePrices": "[0.5, 0.5]"}

    monkeypatch.setattr(bv.requests, "get", lambda *a, **k: Resp())
    assert bv.check_market_resolved("any-id") is None


def test_get_sigma_legacy_hrrr_key():
    bv._cal = {"chicago_hrrr": {"sigma": 3.5}}
    assert bv.get_sigma("chicago", "us_short") == 3.5


def test_snapshot_source_matches():
    assert bv._snapshot_source_matches({"best_source": "us_short"}, "us_short") is True
    assert bv._snapshot_source_matches({"best_source": "hrrr"}, "us_short") is True
    assert bv._snapshot_source_matches({"best_source": "ecmwf"}, "us_short") is False
