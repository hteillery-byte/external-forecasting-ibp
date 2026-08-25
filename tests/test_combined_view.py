import numpy as np
import pandas as pd

from src.backtest import run_backtest
from src.combined_view import EX_POST, FORECAST_FUTURO, REAL, TEST_PHASE_FORECAST, build_combined_view
from src.forecast_engine import RunConfig, run_mass_forecast


def _series_row(prdid, custid, locid, start, n, base=20.0, trend=0.02):
    dates = pd.date_range(start, periods=n, freq="D")
    weekday_effect = np.array([1.4, 1.0, 0.9, 0.9, 1.0, 1.3, 1.6])[dates.dayofweek]
    values = np.round((base + trend * np.arange(n)) * weekday_effect)
    return pd.DataFrame({"PRDID": prdid, "CUSTID": custid, "LOCID": locid, "FECHA": dates, "CANTIDAD": values})


def test_combined_view_has_all_four_segments():
    full_history = _series_row("P1", "C1", "L1", "2024-01-01", 900)  # ~2024-01 a ~2026-06
    forecast_start = pd.Timestamp("2026-06-01")

    training_history = full_history[full_history["FECHA"] < forecast_start]
    cfg = RunConfig(model="seasonal_grey", horizon_days=60, n_jobs=1)
    mass_summary = run_mass_forecast(training_history, cfg)
    backtest_summary = run_backtest(full_history, "2025-01-01", "2025-05-31", cfg)

    combined = build_combined_view(training_history, mass_summary, backtest_summary)

    segments = set(combined["SEGMENTO"])
    assert segments == {REAL, EX_POST, TEST_PHASE_FORECAST, FORECAST_FUTURO}

    forecast_rows = combined[combined["SEGMENTO"] == FORECAST_FUTURO]
    assert forecast_rows["FECHA"].min() == forecast_start
    assert len(forecast_rows) == 60

    test_rows = combined[combined["SEGMENTO"] == TEST_PHASE_FORECAST]
    assert test_rows["FECHA"].min() == pd.Timestamp("2025-01-01")
    assert test_rows["FECHA"].max() == pd.Timestamp("2025-05-31")


def test_combined_view_empty_when_no_results():
    empty_history = pd.DataFrame(columns=["PRDID", "CUSTID", "LOCID", "FECHA", "CANTIDAD"])
    cfg = RunConfig(n_jobs=1)
    mass_summary = run_mass_forecast(empty_history, cfg)
    backtest_summary = run_backtest(empty_history, "2025-01-01", "2025-05-31", cfg)
    combined = build_combined_view(empty_history, mass_summary, backtest_summary)
    assert combined.empty
    assert list(combined.columns) == ["PRDID", "CUSTID", "LOCID", "FECHA", "SEGMENTO", "VALOR"]
