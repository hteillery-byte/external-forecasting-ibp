import numpy as np
import pandas as pd
import pytest

from src.models import seasonal_grey


def _weekly_series(n, base=20.0, trend=0.2):
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    weekday_effect = np.array([1.4, 1.0, 0.9, 0.9, 1.0, 1.3, 1.6])[dates.dayofweek]
    values = np.round((base + trend * np.arange(n)) * weekday_effect)
    return pd.Series(values, index=dates)


def test_requires_minimum_observations():
    y = _weekly_series(3)
    with pytest.raises(ValueError):
        seasonal_grey.fit_and_forecast(y, horizon_days=7)


def test_fits_short_series():
    y = _weekly_series(10)
    res = seasonal_grey.fit_and_forecast(y, horizon_days=5, season_length=7)
    assert len(res.ex_post) == 10
    assert len(res.forecast) == 5
    assert (res.forecast >= 0).all()
    assert (res.ex_post >= 0).all()


def test_forecast_dates_are_contiguous_after_history():
    y = _weekly_series(14)
    res = seasonal_grey.fit_and_forecast(y, horizon_days=3, season_length=7)
    assert res.forecast.index[0] == y.index[-1] + pd.Timedelta(days=1)
    assert (res.forecast.index.to_series().diff().dropna() == pd.Timedelta(days=1)).all()


def test_seasonal_indices_normalized_to_season_length():
    y = _weekly_series(28)
    res = seasonal_grey.fit_and_forecast(y, horizon_days=1, season_length=7)
    assert res.seasonal_indices.sum() == pytest.approx(7.0, rel=1e-6)


def test_handles_zeros_without_crashing():
    dates = pd.date_range("2026-01-01", periods=12, freq="D")
    y = pd.Series([0.0, 0.0, 5.0, 0.0, 3.0, 0.0, 0.0, 1.0, 0.0, 4.0, 0.0, 2.0], index=dates)
    res = seasonal_grey.fit_and_forecast(y, horizon_days=4, season_length=7)
    assert np.isfinite(res.forecast.to_numpy()).all()
