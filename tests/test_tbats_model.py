import numpy as np
import pandas as pd
import pytest

from src.models import tbats_model


def _weekly_series(n, base=20.0, trend=0.2):
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    weekday_effect = np.array([1.4, 1.0, 0.9, 0.9, 1.0, 1.3, 1.6])[dates.dayofweek]
    values = np.round((base + trend * np.arange(n)) * weekday_effect)
    return pd.Series(values, index=dates)


def test_requires_minimum_observations():
    y = _weekly_series(10)
    with pytest.raises(ValueError):
        tbats_model.fit_and_forecast(y, horizon_days=7)


def test_fits_and_forecasts():
    y = _weekly_series(40)
    res = tbats_model.fit_and_forecast(y, horizon_days=7)
    assert len(res.ex_post) == 40
    assert len(res.forecast) == 7
    assert (res.forecast >= 0).all()


def test_forecast_dates_are_contiguous_after_history():
    y = _weekly_series(30)
    res = tbats_model.fit_and_forecast(y, horizon_days=5)
    assert res.forecast.index[0] == y.index[-1] + pd.Timedelta(days=1)
    assert (res.forecast.index.to_series().diff().dropna() == pd.Timedelta(days=1)).all()
