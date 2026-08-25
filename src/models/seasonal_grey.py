"""Modelo Gris Estacional — Seasonal GM(1,1) (slide 19 — Textil Hogar, series cortas).

Grey system model pensado para "pocos datos": colecciones/temporadas con
apenas semanas de venta. A diferencia de TBATS, GM(1,1) puede ajustarse con
tan solo 4 observaciones. La variante estacional añade un índice estacional
clásico (ratio-to-moving-average) sobre el GM(1,1) base, que por diseño solo
captura tendencia.

Referencia: "A seasonal discrete grey forecasting model for fashion
retailing" (ScienceDirect, citado en la presentación).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_OBS_FOR_GM11 = 4
EPSILON = 1e-6  # piso para series con ceros; GM(1,1) exige valores > 0


def _gm11_fit(x0: np.ndarray) -> tuple[float, float]:
    """Ajusta GM(1,1) clásico. Devuelve (a, b) de dx1/dt + a*x1 = b."""
    x1 = np.cumsum(x0)
    z1 = 0.5 * (x1[1:] + x1[:-1])  # background values, k=2..n
    B = np.column_stack([-z1, np.ones_like(z1)])
    Y = x0[1:]
    (a, b), *_ = np.linalg.lstsq(B, Y, rcond=None)
    return float(a), float(b)


def _gm11_predict_x1(k: np.ndarray, x0_1: float, a: float, b: float) -> np.ndarray:
    """x1_hat(k) para k=1..N (1-indexado), fórmula de respuesta temporal de GM(1,1)."""
    if abs(a) < 1e-9:
        return x0_1 + b * (k - 1)
    return (x0_1 - b / a) * np.exp(-a * (k - 1)) + b / a


def _gm11_fit_and_extend(x0: np.ndarray, n_forecast: int) -> np.ndarray:
    """Devuelve x0_hat para las n observaciones originales + n_forecast futuras."""
    n = len(x0)
    a, b = _gm11_fit(x0)
    k = np.arange(1, n + n_forecast + 1)
    x1_hat = _gm11_predict_x1(k, x0[0], a, b)
    x0_hat = np.empty(n + n_forecast)
    x0_hat[0] = x0[0]
    x0_hat[1:] = x1_hat[1:] - x1_hat[:-1]
    return x0_hat


def _seasonal_indices(y: np.ndarray, season_length: int) -> np.ndarray:
    """Índices estacionales por ratio-to-moving-average, normalizados a media 1."""
    n = len(y)
    if season_length < 2 or n < 2 * season_length:
        return np.ones(season_length)

    window = season_length
    half = window // 2
    ma = np.full(n, np.nan)
    if window % 2 == 1:
        for t in range(half, n - half):
            ma[t] = y[t - half: t + half + 1].mean()
    else:
        for t in range(half, n - half):
            two_sided = np.concatenate([y[t - half:t + half], y[t - half + 1:t + half + 1]])
            ma[t] = two_sided.mean() / 2

    ratio = np.where(ma > EPSILON, y / np.where(ma > EPSILON, ma, np.nan), np.nan)
    idx = np.arange(n) % season_length
    seasonal = np.array([
        np.nanmean(ratio[idx == i]) if np.any(~np.isnan(ratio[idx == i])) else 1.0
        for i in range(season_length)
    ])
    seasonal = np.nan_to_num(seasonal, nan=1.0)
    seasonal = seasonal * (season_length / seasonal.sum()) if seasonal.sum() > 0 else np.ones(season_length)
    return seasonal


@dataclass
class SeasonalGreyResult:
    ex_post: pd.Series
    forecast: pd.Series
    seasonal_indices: np.ndarray
    season_length: int


def fit_and_forecast(y: pd.Series, horizon_days: int, season_length: int = 7) -> SeasonalGreyResult:
    """Ajusta el Modelo Gris Estacional sobre una serie diaria corta.

    Funciona con tan solo ``MIN_OBS_FOR_GM11`` observaciones (a diferencia
    de TBATS). Con menos de ``2 * season_length`` observaciones no hay
    suficiente historia para estimar estacionalidad de forma confiable, y el
    índice estacional se degrada a 1.0 (equivalente a GM(1,1) sin estacionalidad).
    """
    n = len(y)
    if n < MIN_OBS_FOR_GM11:
        raise ValueError(f"Serie con {n} observaciones, Gris Estacional requiere >= {MIN_OBS_FOR_GM11}.")

    values = y.to_numpy(dtype=float)
    seasonal = _seasonal_indices(values, season_length)
    idx = np.arange(n) % season_length
    deseasonalized = np.maximum(values / seasonal[idx], EPSILON)

    x0_hat_deseason = _gm11_fit_and_extend(deseasonalized, horizon_days)

    future_idx = (np.arange(n, n + horizon_days) % season_length)
    all_idx = np.concatenate([idx, future_idx])
    reseasonalized = np.clip(x0_hat_deseason * seasonal[all_idx], a_min=0, a_max=None)

    ex_post = pd.Series(reseasonalized[:n], index=y.index, name="ex_post")
    future_dates = pd.date_range(y.index[-1] + pd.Timedelta(days=1), periods=horizon_days, freq="D")
    forecast = pd.Series(reseasonalized[n:], index=future_dates, name="forecast")

    return SeasonalGreyResult(ex_post=ex_post, forecast=forecast, seasonal_indices=seasonal, season_length=season_length)
