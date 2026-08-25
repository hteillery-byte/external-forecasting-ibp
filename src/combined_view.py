"""Vista combinada: encadena en una sola línea de tiempo por combinación

Real histórico → Ex Post (ajuste in-sample sobre todos los meses de
entrenamiento) → Test Phase (forecast ciego contra el holdout de calendario)
→ Forecast futuro (proyección pura, sin real, desde la fecha de corte).

No reimplementa nada de ajuste de modelos — solo junta en formato largo los
resultados que ya produce ``forecast_engine.run_mass_forecast`` (Ex Post +
Forecast) y ``backtest.run_backtest`` (Test Phase).
"""
from __future__ import annotations

import pandas as pd

from src.backtest import BacktestSummary
from src.forecast_engine import RunSummary

DIM_COLS = ["PRDID", "CUSTID", "LOCID"]
COLUMNS = DIM_COLS + ["FECHA", "SEGMENTO", "VALOR"]

REAL = "REAL"
EX_POST = "EX_POST"
TEST_PHASE_FORECAST = "TEST_PHASE_FORECAST"
FORECAST_FUTURO = "FORECAST_FUTURO"


def build_combined_view(
    history: pd.DataFrame,
    mass_summary: RunSummary,
    backtest_summary: BacktestSummary,
    value_col: str = "CANTIDAD",
) -> pd.DataFrame:
    """Arma la tabla larga (PRDID, CUSTID, LOCID, FECHA, SEGMENTO, VALOR).

    ``mass_summary`` debe venir de correr ``run_mass_forecast`` sobre el
    histórico de entrenamiento (todo lo anterior a la fecha de corte del
    forecast futuro) — su ``ex_post_df`` cubre "todos los meses de
    entrenamiento" y su ``forecast_df`` es la proyección futura pura.
    """
    frames = []

    real = history[DIM_COLS + ["FECHA", value_col]].rename(columns={value_col: "VALOR"}).copy()
    real["SEGMENTO"] = REAL
    frames.append(real)

    ex_post = mass_summary.ex_post_df.rename(columns={"VALUE": "VALOR"}).copy()
    ex_post["SEGMENTO"] = EX_POST
    frames.append(ex_post)

    test_phase = backtest_summary.detail_df.rename(columns={"FORECAST": "VALOR"}).drop(columns=["ACTUAL"]).copy()
    test_phase["SEGMENTO"] = TEST_PHASE_FORECAST
    frames.append(test_phase)

    forecast = mass_summary.forecast_df.rename(columns={"VALUE": "VALOR"}).copy()
    forecast["SEGMENTO"] = FORECAST_FUTURO
    frames.append(forecast)

    frames = [f[COLUMNS] for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True).sort_values(DIM_COLS + ["FECHA"]).reset_index(drop=True)
