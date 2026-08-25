import numpy as np
import pandas as pd
import pytest

from src.backtest import run_backtest
from src.forecast_engine import RunConfig


def _series_row(prdid, custid, locid, start, n, base=20.0, trend=0.05):
    dates = pd.date_range(start, periods=n, freq="D")
    weekday_effect = np.array([1.4, 1.0, 0.9, 0.9, 1.0, 1.3, 1.6])[dates.dayofweek]
    values = np.round((base + trend * np.arange(n)) * weekday_effect)
    return pd.DataFrame({"PRDID": prdid, "CUSTID": custid, "LOCID": locid, "FECHA": dates, "CANTIDAD": values})


def test_backtest_uses_explicit_calendar_dates_not_trailing_days():
    # Historia hasta HOY (simulado bien en el futuro respecto a la ventana de test),
    # para probar que el holdout se ancla a fechas de calendario y no a "los últimos N
    # días de lo cargado" -- justo el bug que se corrigió.
    hist = _series_row("P1", "C1", "L1", "2024-01-01", 600)  # llega hasta ~2025-08-23
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)

    summary = run_backtest(hist, "2025-01-01", "2025-05-31", cfg)

    r = summary.results[0]
    assert r.detail["FECHA"].min() == pd.Timestamp("2025-01-01")
    assert r.detail["FECHA"].max() == pd.Timestamp("2025-05-31")
    assert r.n_test_days == 151  # ene(31)+feb(28)+mar(31)+abr(30)+may(31) en 2025 (no bisiesto)


def test_backtest_reports_insufficient_history_before_test_start():
    hist = _series_row("P1", "C1", "L1", "2025-01-15", 30)  # arranca DESPUÉS de test_start
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, "2025-01-01", "2025-05-31", cfg)

    r = summary.results[0]
    assert r.mape is None
    assert "Sin historia disponible antes de" in r.error


def test_backtest_ignores_data_after_test_end():
    # Datos que se extienden mucho más allá del test_end no deberían afectar el resultado.
    hist = _series_row("P1", "C1", "L1", "2024-01-01", 700)
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, "2025-01-01", "2025-05-31", cfg)
    r = summary.results[0]
    assert r.detail["FECHA"].max() == pd.Timestamp("2025-05-31")


def test_backtest_excludes_zero_actual_days_from_mape():
    dates = pd.date_range("2024-06-01", periods=30, freq="D")
    values = np.concatenate([np.full(20, 10.0), np.zeros(10)])  # los últimos 10 días (holdout) en cero
    hist = pd.DataFrame({"PRDID": "P1", "CUSTID": "C1", "LOCID": "L1", "FECHA": dates, "CANTIDAD": values})
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)

    test_start, test_end = dates[-10], dates[-1]
    summary = run_backtest(hist, test_start, test_end, cfg)

    r = summary.results[0]
    assert r.mape is None  # todos los días de test tienen real=0 -> MAPE indefinido
    assert r.mape_days_excluded == 10
    assert r.wmape is None  # sum(|real|)=0 en todo el holdout -> WMAPE también indefinido acá


def test_backtest_wmape_survives_partial_zero_actuals_where_mape_excludes_them():
    dates = pd.date_range("2024-06-01", periods=30, freq="D")
    values = np.concatenate([np.full(20, 10.0), np.array([0, 0, 5, 5, 5, 0, 5, 5, 0, 5])])
    hist = pd.DataFrame({"PRDID": "P1", "CUSTID": "C1", "LOCID": "L1", "FECHA": dates, "CANTIDAD": values})
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)

    summary = run_backtest(hist, dates[-10], dates[-1], cfg)

    r = summary.results[0]
    assert r.mape_days_excluded == 4
    assert r.mape is not None
    assert r.wmape is not None


def test_overall_mape_and_wmape_aggregate_across_combos():
    hist = pd.concat([
        _series_row("P1", "C1", "L1", "2024-01-01", 200, base=20.0),
        _series_row("P2", "C1", "L1", "2024-01-01", 200, base=5.0),
    ], ignore_index=True)
    cfg = RunConfig(model="seasonal_grey", n_jobs=1)
    summary = run_backtest(hist, "2024-06-01", "2024-06-10", cfg)

    assert summary.overall_mape is not None
    assert summary.overall_wmape is not None
    assert len(summary.summary_df) == 2


def test_test_end_before_test_start_raises():
    hist = _series_row("P1", "C1", "L1", "2024-01-01", 30)
    with pytest.raises(ValueError):
        run_backtest(hist, "2025-05-31", "2025-01-01", RunConfig())


def test_on_progress_called_once_per_combo():
    hist = pd.concat([
        _series_row("P1", "C1", "L1", "2024-01-01", 200),
        _series_row("P2", "C1", "L1", "2024-01-01", 200),
    ], ignore_index=True)

    calls = []
    run_backtest(
        hist, "2024-06-01", "2024-06-10", RunConfig(model="seasonal_grey", n_jobs=1),
        on_progress=lambda done, total, result: calls.append((done, total, result.prdid)),
    )

    assert len(calls) == 2
    assert [c[0] for c in calls] == [1, 2]
    assert all(c[1] == 2 for c in calls)
