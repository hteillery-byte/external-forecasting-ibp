import numpy as np
import pandas as pd
import pytest

from src.backtest import run_backtest
from src.forecast_engine import RunConfig


def _series_row(prdid, custid, locid, n, base=20.0, trend=0.05):
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    weekday_effect = np.array([1.4, 1.0, 0.9, 0.9, 1.0, 1.3, 1.6])[dates.dayofweek]
    values = np.round((base + trend * np.arange(n)) * weekday_effect)
    return pd.DataFrame({"PRDID": prdid, "CUSTID": custid, "LOCID": locid, "FECHA": dates, "CANTIDAD": values})


def test_backtest_computes_mape_on_holdout_only():
    hist = _series_row("P1", "C1", "L1", 60)
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, test_phase_periods=10, cfg=cfg)

    assert summary.test_phase_periods == 10
    r = summary.results[0]
    assert r.n_test_days == 10
    assert r.mape is not None
    assert r.mape >= 0
    assert r.detail is not None
    assert len(r.detail) == 10
    # el holdout debe ser exactamente los últimos 10 días, nunca vistos en el entrenamiento
    assert r.detail["FECHA"].min() == hist["FECHA"].max() - pd.Timedelta(days=9)


def test_backtest_reports_insufficient_history():
    hist = _series_row("P1", "C1", "L1", 5)
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, test_phase_periods=10, cfg=cfg)

    r = summary.results[0]
    assert r.mape is None
    assert "no alcanza" in r.error


def test_backtest_excludes_zero_actual_days_from_mape():
    dates = pd.date_range("2026-01-01", periods=30, freq="D")
    values = np.concatenate([np.full(20, 10.0), np.zeros(10)])  # holdout (últimos 10) todo en cero
    hist = pd.DataFrame({"PRDID": "P1", "CUSTID": "C1", "LOCID": "L1", "FECHA": dates, "CANTIDAD": values})
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, test_phase_periods=10, cfg=cfg)

    r = summary.results[0]
    assert r.mape is None  # todos los días de test tienen real=0 -> MAPE indefinido
    assert r.mape_days_excluded == 10
    assert r.wmape is None  # sum(|real|)=0 en todo el holdout -> WMAPE también indefinido acá


def test_backtest_wmape_survives_partial_zero_actuals_where_mape_excludes_them():
    dates = pd.date_range("2026-01-01", periods=30, freq="D")
    # holdout (últimos 10 días): mezcla de reales cero y no-cero
    values = np.concatenate([np.full(20, 10.0), np.array([0, 0, 5, 5, 5, 0, 5, 5, 0, 5])])
    hist = pd.DataFrame({"PRDID": "P1", "CUSTID": "C1", "LOCID": "L1", "FECHA": dates, "CANTIDAD": values})
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, test_phase_periods=10, cfg=cfg)

    r = summary.results[0]
    assert r.mape_days_excluded == 4  # los 4 ceros del holdout
    assert r.mape is not None
    assert r.wmape is not None  # sum(|real|) > 0 en el holdout completo -> sí se puede calcular


def test_overall_mape_and_wmape_aggregate_across_combos():
    hist = pd.concat([
        _series_row("P1", "C1", "L1", 60, base=20.0),
        _series_row("P2", "C1", "L1", 60, base=5.0),
    ], ignore_index=True)
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, test_phase_periods=10, cfg=cfg)

    assert summary.overall_mape is not None
    assert summary.overall_wmape is not None
    assert len(summary.summary_df) == 2


def test_test_phase_periods_must_be_positive():
    hist = _series_row("P1", "C1", "L1", 30)
    with pytest.raises(ValueError):
        run_backtest(hist, test_phase_periods=0, cfg=RunConfig())
