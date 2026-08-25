"""Wrapper de TBATS (slide 9 — corto plazo/día, multi-estacionalidad).

TBATS captura simultáneamente estacionalidad semanal (día de semana) y,
cuando hay suficiente historia, estacionalidad anual — algo que SES/DES/TES
nativos de IBP Advanced Demand no cubren. Usa el paquete `tbats`
(implementación de De Livera, Hyndman & Snyder).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from tbats import TBATS

DEFAULT_SEASONAL_PERIODS = (7,)  # día de semana; agregar 365.25 con >= 2 años de historia
MIN_OBS_FOR_TBATS = 21  # ~3 ciclos semanales mínimo para que el ajuste sea estable


@dataclass
class TbatsFitResult:
    ex_post: pd.Series  # valores ajustados in-sample, mismo índice que la serie de entrada
    forecast: pd.Series  # valores proyectados para el horizonte solicitado
    seasonal_periods: tuple


def fit_and_forecast(
    y: pd.Series,
    horizon_days: int,
    seasonal_periods: tuple = DEFAULT_SEASONAL_PERIODS,
    use_box_cox: bool = False,
    use_damped_trend: bool = True,
    use_arma_errors: bool = False,
) -> TbatsFitResult:
    """Ajusta TBATS sobre una serie diaria y devuelve Ex Post + Forecast.

    ``y`` debe tener índice de fechas diarias contiguas (sin huecos) y
    valores no negativos. Series con menos de ``MIN_OBS_FOR_TBATS``
    observaciones no se ajustan aquí — el orquestador debe enrutarlas al
    Modelo Gris Estacional (ver seasonal_grey.py).

    Cada uno de los 3 componentes opcionales se fija explícitamente en vez
    de dejar que TBATS haga grid search sobre ellos (grid search completo
    puede tardar 1-2+ min por serie, no escala a miles de combinaciones).
    Benchmark real (serie sintética de 3 años, período semanal, ver
    CLAUDE.md): con los 3 apagados, ~4s/combinación.

    - ``use_box_cox`` (default False): estabiliza varianza cuando la
      dispersión crece con el nivel. Costo: ~1.6x (~6.4s). Beneficio bajo
      salvo que la demanda sea muy heteroscedástica.
    - ``use_damped_trend`` (default **True**): evita que la tendencia se
      extrapole sin freno en horizontes largos (relevante para el caso de
      uso real de este proyecto: 60 días). Costo: ~2.4x (~9.7s). Recomendado
      activo salvo que el horizonte sea muy corto.
    - ``use_arma_errors`` (default False): modela autocorrelación residual —
      ayuda más a precisión de 1 paso que a un forecast de varias semanas.
      Costo: **~8x (~32s), el más caro de los tres por lejos**. Dejar
      apagado a nivel masivo salvo que el horizonte sea muy corto.
    """
    if len(y) < MIN_OBS_FOR_TBATS:
        raise ValueError(
            f"Serie con {len(y)} observaciones, TBATS requiere >= {MIN_OBS_FOR_TBATS}. "
            "Usar seasonal_grey para series cortas."
        )

    periods = tuple(p for p in seasonal_periods if p < len(y) / 2) or (min(7, len(y) // 2),)

    # n_jobs=1: forecast_engine ya paraleliza por combinación (ProcessPoolExecutor);
    # dejar que TBATS spawnee su propio pool interno por serie duplica el paralelismo.
    estimator = TBATS(
        seasonal_periods=list(periods), show_warnings=False, n_jobs=1,
        use_box_cox=use_box_cox, use_trend=True,
        use_damped_trend=use_damped_trend, use_arma_errors=use_arma_errors,
    )
    fitted_model = estimator.fit(y.to_numpy(dtype=float))

    ex_post = pd.Series(fitted_model.y_hat, index=y.index, name="ex_post")

    future_index = pd.date_range(y.index[-1] + pd.Timedelta(days=1), periods=horizon_days, freq="D")
    forecast_values = fitted_model.forecast(steps=horizon_days)
    forecast = pd.Series(np.clip(forecast_values, a_min=0, a_max=None), index=future_index, name="forecast")

    return TbatsFitResult(ex_post=ex_post, forecast=forecast, seasonal_periods=periods)
