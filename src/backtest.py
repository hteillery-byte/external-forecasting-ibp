"""Test Phase Periods — backtest real, alineado a la definición oficial de SAP IBP.

SAP IBP tiene un campo "Test Phase Periods" en la pestaña "Forecasting Steps"
del Forecast Model: reserva los últimos N períodos históricos como set de
prueba, entrena cada algoritmo solo con el resto, y compara el pronóstico
contra el valor real ya conocido del holdout. SAP recomienda esto por sobre
el Ex-Post (ajuste in-sample) para elegir el mejor algoritmo, porque el
Ex-Post puede sobreestimar la precisión (overfitting) — el Test Phase da
una medida honesta, fuera de muestra.

Referencia: SAP KBA 2701226 "Use of 'Test Phase Periods' in IBP Forecasting";
SAP Community, "Ex-Post forecast or Test Phase? How to determine the best
forecasting algorithm" (2023).
"""
from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from src.forecast_engine import DIM_COLS, RunConfig, _fit_one

logger = logging.getLogger(__name__)


@dataclass
class BacktestComboResult:
    prdid: str
    custid: str
    locid: str
    model_used: str | None
    mape: float | None  # métrica oficial pedida por el cliente
    mape_days_excluded: int  # días del test phase con real=0 -- MAPE indefinido ahí, se excluyen
    wmape: float | None  # métrica de respaldo, no reemplaza al MAPE
    n_test_days: int
    detail: pd.DataFrame | None  # FECHA, ACTUAL, FORECAST -- solo el período de test
    error: str | None = None


@dataclass
class BacktestSummary:
    results: list[BacktestComboResult] = field(default_factory=list)
    test_phase_periods: int = 0

    @property
    def summary_df(self) -> pd.DataFrame:
        rows = [
            {
                "PRDID": r.prdid, "CUSTID": r.custid, "LOCID": r.locid,
                "modelo": r.model_used, "MAPE_%": r.mape, "WMAPE_%": r.wmape,
                "dias_test": r.n_test_days, "dias_excluidos_mape": r.mape_days_excluded,
                "error": r.error,
            }
            for r in self.results
        ]
        cols = ["PRDID", "CUSTID", "LOCID", "modelo", "MAPE_%", "WMAPE_%", "dias_test", "dias_excluidos_mape", "error"]
        return pd.DataFrame(rows, columns=cols)

    @property
    def detail_df(self) -> pd.DataFrame:
        frames = []
        for r in self.results:
            if r.detail is None:
                continue
            d = r.detail.copy()
            d["PRDID"], d["CUSTID"], d["LOCID"] = r.prdid, r.custid, r.locid
            frames.append(d)
        if not frames:
            return pd.DataFrame(columns=["PRDID", "CUSTID", "LOCID", "FECHA", "ACTUAL", "FORECAST"])
        return pd.concat(frames, ignore_index=True)

    @property
    def overall_mape(self) -> float | None:
        """Promedio simple del MAPE por combinación (cada combo pesa igual)."""
        vals = [r.mape for r in self.results if r.mape is not None]
        return float(np.mean(vals)) if vals else None

    @property
    def overall_wmape(self) -> float | None:
        """WMAPE agrupado: sum(|error|)/sum(|real|) sobre TODAS las combinaciones —
        pondera por volumen en vez de tratar igual a un SKU chico que a uno grande."""
        d = self.detail_df
        if d.empty:
            return None
        denom = d["ACTUAL"].abs().sum()
        if denom == 0:
            return None
        return float((d["ACTUAL"] - d["FORECAST"]).abs().sum() / denom * 100)


def _mape(actual: np.ndarray, forecast: np.ndarray) -> tuple[float | None, int]:
    """MAPE clásico, excluyendo días con real=0 (división por cero indefinida)."""
    mask = actual != 0
    excluded = int((~mask).sum())
    if not mask.any():
        return None, excluded
    mape = float(np.mean(np.abs((actual[mask] - forecast[mask]) / actual[mask])) * 100)
    return mape, excluded


def _wmape(actual: np.ndarray, forecast: np.ndarray) -> float | None:
    denom = np.abs(actual).sum()
    if denom == 0:
        return None
    return float(np.abs(actual - forecast).sum() / denom * 100)


def _backtest_one(
    prdid: str, custid: str, locid: str, series: pd.Series, test_phase_periods: int, cfg: RunConfig,
) -> BacktestComboResult:
    series = series.sort_index()
    if len(series) <= test_phase_periods:
        return BacktestComboResult(
            prdid, custid, locid, None, None, 0, None, 0, None,
            error=(
                f"Historia ({len(series)} días) no alcanza para reservar "
                f"{test_phase_periods} días de Test Phase"
            ),
        )

    train = series.iloc[:-test_phase_periods]
    test = series.iloc[-test_phase_periods:]

    # El modelo se elige y entrena SOLO con lo que se sabría en ese momento (train) --
    # nunca mirando el set de prueba, igual que en un backtest real.
    train_cfg = replace(cfg, horizon_days=test_phase_periods)
    fit_result = _fit_one(prdid, custid, locid, train, train_cfg)

    if fit_result.error or fit_result.forecast is None:
        return BacktestComboResult(
            prdid, custid, locid, fit_result.model_used, None, 0, None, len(test), None,
            error=fit_result.error or "Sin forecast generado sobre el set de entrenamiento",
        )

    aligned_forecast = fit_result.forecast.reindex(test.index).fillna(0.0)
    actual_arr = test.to_numpy()
    forecast_arr = aligned_forecast.to_numpy()

    mape, excluded = _mape(actual_arr, forecast_arr)
    wmape = _wmape(actual_arr, forecast_arr)
    detail = pd.DataFrame({"FECHA": test.index, "ACTUAL": actual_arr, "FORECAST": forecast_arr})

    return BacktestComboResult(
        prdid, custid, locid, fit_result.model_used, mape, excluded, wmape, len(test), detail,
    )


def run_backtest(
    history: pd.DataFrame, test_phase_periods: int, cfg: RunConfig, value_col: str = "CANTIDAD",
) -> BacktestSummary:
    """Ejecuta el Test Phase (backtest real) sobre un histórico en formato largo.

    Reserva los últimos ``test_phase_periods`` días de cada combinación como set de
    prueba, entrena solo con el resto, y mide MAPE/WMAPE contra el valor real ya
    conocido del holdout.
    """
    required = set(DIM_COLS + ["FECHA", value_col])
    missing = required - set(history.columns)
    if missing:
        raise ValueError(f"Faltan columnas en el histórico: {sorted(missing)}")
    if test_phase_periods <= 0:
        raise ValueError(f"test_phase_periods debe ser > 0, recibido {test_phase_periods}")

    groups = list(history.groupby(DIM_COLS, sort=False))
    tasks = []
    for (prdid, custid, locid), g in groups:
        s = g.set_index("FECHA")[value_col].astype(float)
        s = s.asfreq("D", fill_value=0.0)
        tasks.append((prdid, custid, locid, s))

    if cfg.n_jobs <= 1:
        results = [_backtest_one(p, c, l, s, test_phase_periods, cfg) for p, c, l, s in tasks]
    else:
        results = []
        with ProcessPoolExecutor(max_workers=cfg.n_jobs) as pool:
            futures = {
                pool.submit(_backtest_one, p, c, l, s, test_phase_periods, cfg): (p, c, l)
                for p, c, l, s in tasks
            }
            for fut in as_completed(futures):
                results.append(fut.result())

    return BacktestSummary(results=results, test_phase_periods=test_phase_periods)
