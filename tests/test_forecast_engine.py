import numpy as np
import pandas as pd

from src.forecast_engine import RunConfig, run_mass_forecast


def _history_row(prdid, custid, locid, n, base=20.0):
    dates = pd.date_range("2026-06-01", periods=n, freq="D")
    weekday_effect = np.array([1.4, 1.0, 0.9, 0.9, 1.0, 1.3, 1.6])[dates.dayofweek]
    values = np.round(base * weekday_effect)
    return pd.DataFrame({
        "PRDID": prdid, "CUSTID": custid, "LOCID": locid,
        "FECHA": dates, "CANTIDAD": values,
    })


def test_auto_routes_short_series_to_seasonal_grey_and_reports_errors():
    hist = pd.concat([
        _history_row("P1", "C1", "L1", 10),   # too short for TBATS -> seasonal_grey
        _history_row("P2", "C1", "L1", 2),    # too short even for seasonal_grey -> error
    ], ignore_index=True)

    cfg = RunConfig(model="auto", horizon_days=5, n_jobs=1)
    summary = run_mass_forecast(hist, cfg)

    assert set(summary.model_usage.index) == {"seasonal_grey"}
    assert len(summary.errors_df) == 1
    assert summary.errors_df.iloc[0]["PRDID"] == "P2"

    fc = summary.forecast_df
    assert set(fc["PRDID"]) == {"P1"}
    assert (fc.groupby(["PRDID", "CUSTID", "LOCID"]).size() == 5).all()


def test_missing_required_columns_raises():
    import pytest
    bad = pd.DataFrame({"PRDID": ["P1"], "CUSTID": ["C1"]})
    with pytest.raises(ValueError):
        run_mass_forecast(bad, RunConfig())
