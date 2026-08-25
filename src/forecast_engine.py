"""Orquestador de pronóstico masivo por combinación PRDID-CUSTID-LOCID.

Toma el histórico diario (formato largo) leído de SAP IBP, ajusta TBATS o
el Modelo Gris Estacional por combinación, y produce dos tablas listas para
escribir de vuelta en IBP como key figures: Ex Post (valores ajustados
sobre el histórico) y Forecast (proyección a futuro).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from src.models import seasonal_grey, tbats_model

logger = logging.getLogger(__name__)

ModelChoice = Literal["auto", "tbats", "seasonal_grey"]

DIM_COLS = ["PRDID", "CUSTID", "LOCID"]


@dataclass
class RunConfig:
    model: ModelChoice = "auto"
    horizon_days: int = 14
    season_length: int = 7
    seasonal_periods_tbats: tuple = tbats_model.DEFAULT_SEASONAL_PERIODS
    min_obs_tbats: int = tbats_model.MIN_OBS_FOR_TBATS
    min_obs_grey: int = seasonal_grey.MIN_OBS_FOR_GM11
    # Piso de densidad para clasificar como demanda intermitente en vez de enrutar a
    # TBATS solo por tener suficientes DÍAS de calendario — heurística, no un umbral
    # académico validado contra los datos reales del cliente (ajustar si hace falta).
    min_nonzero_ratio: float = 0.15
    n_jobs: int = 1
    # Ver tbats_model.fit_and_forecast para el costo/beneficio medido de cada uno.
    tbats_use_box_cox: bool = False
    tbats_use_damped_trend: bool = True
    tbats_use_arma_errors: bool = False


@dataclass
class ComboResult:
    prdid: str
    custid: str
    locid: str
    model_used: str | None
    ex_post: pd.Series | None
    forecast: pd.Series | None
    error: str | None = None


@dataclass
class RunSummary:
    results: list[ComboResult] = field(default_factory=list)

    @property
    def ex_post_df(self) -> pd.DataFrame:
        return _stack(self.results, "ex_post")

    @property
    def forecast_df(self) -> pd.DataFrame:
        return _stack(self.results, "forecast")

    @property
    def errors_df(self) -> pd.DataFrame:
        rows = [
            {"PRDID": r.prdid, "CUSTID": r.custid, "LOCID": r.locid, "error": r.error}
            for r in self.results if r.error
        ]
        return pd.DataFrame(rows, columns=["PRDID", "CUSTID", "LOCID", "error"])

    @property
    def model_usage(self) -> pd.Series:
        used = [r.model_used for r in self.results if r.model_used]
        return pd.Series(used).value_counts() if used else pd.Series(dtype=int)


def _stack(results: list[ComboResult], attr: str) -> pd.DataFrame:
    frames = []
    for r in results:
        series = getattr(r, attr)
        if series is None:
            continue
        df = series.rename("VALUE").reset_index()
        df = df.rename(columns={df.columns[0]: "FECHA"})
        df["PRDID"], df["CUSTID"], df["LOCID"] = r.prdid, r.custid, r.locid
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["PRDID", "CUSTID", "LOCID", "FECHA", "VALUE"])
    return pd.concat(frames, ignore_index=True)[["PRDID", "CUSTID", "LOCID", "FECHA", "VALUE"]]


def _fit_one(prdid: str, custid: str, locid: str, series: pd.Series, cfg: RunConfig) -> ComboResult:
    series = series.sort_index()
    n = len(series)
    # nnz = observaciones REALES (no-cero). El largo del período (n) no basta para decidir
    # si hay señal suficiente: una combinación con 3 ventas dispersas en 540 días reconstruidos
    # tiene n=540 pero nnz=3 — no es una serie con historia sólida, es demanda intermitente.
    nnz = int((series != 0).sum())
    nnz_ratio = (nnz / n) if n else 0.0

    model = cfg.model
    if model == "auto":
        if nnz >= cfg.min_obs_tbats and nnz_ratio >= cfg.min_nonzero_ratio:
            model = "tbats"
        elif nnz >= cfg.min_obs_grey:
            model = "seasonal_grey"
        else:
            msg = (
                f"{nnz} observaciones no-cero en {n} días ({nnz_ratio:.0%}) — demanda intermitente, "
                "ya cubierta por Croston/Croston TSB nativo en IBP Advanced Demand (fuera de alcance "
                "de TBATS/Gris Estacional)."
            )
            logger.info("Combo %s/%s/%s: %s", prdid, custid, locid, msg)
            return ComboResult(prdid, custid, locid, "intermitente", None, None, error=msg)

    try:
        if model == "tbats":
            if nnz < cfg.min_obs_tbats:
                raise ValueError(f"TBATS requiere >= {cfg.min_obs_tbats} obs NO-CERO, la serie tiene {nnz} (en {n} días)")
            res = tbats_model.fit_and_forecast(
                series, cfg.horizon_days, cfg.seasonal_periods_tbats,
                cfg.tbats_use_box_cox, cfg.tbats_use_damped_trend, cfg.tbats_use_arma_errors,
            )
        else:
            if nnz < cfg.min_obs_grey:
                raise ValueError(f"Gris Estacional requiere >= {cfg.min_obs_grey} obs NO-CERO, la serie tiene {nnz}")
            res = seasonal_grey.fit_and_forecast(series, cfg.horizon_days, cfg.season_length)
        return ComboResult(prdid, custid, locid, model, res.ex_post, res.forecast)
    except Exception as exc:  # noqa: BLE001 — aislar fallas por combinación sin abortar el batch
        logger.warning("Combo %s/%s/%s falló con modelo %s: %s", prdid, custid, locid, model, exc)
        return ComboResult(prdid, custid, locid, None, None, None, error=str(exc))


def run_mass_forecast(
    history: pd.DataFrame,
    cfg: RunConfig,
    value_col: str = "CANTIDAD",
    on_progress: Callable[[int, int, ComboResult], None] | None = None,
) -> RunSummary:
    """Ejecuta el pronóstico masivo sobre un histórico en formato largo.

    ``history`` debe tener columnas PRDID, CUSTID, LOCID, FECHA (datetime) y
    ``value_col`` (cantidad histórica diaria). Series con huecos de fecha se
    completan a diario con 0 antes de ajustar.

    ``on_progress(completadas, total, ultimo_resultado)`` se llama después de
    CADA combinación (tanto en modo secuencial como en paralelo) — sin esto,
    un batch de cientos/miles de combinaciones no da ninguna señal de avance
    hasta terminar del todo.
    """
    required = set(DIM_COLS + ["FECHA", value_col])
    missing = required - set(history.columns)
    if missing:
        raise ValueError(f"Faltan columnas en el histórico: {sorted(missing)}")

    groups = list(history.groupby(DIM_COLS, sort=False))
    tasks = []
    for (prdid, custid, locid), g in groups:
        s = g.set_index("FECHA")[value_col].astype(float)
        s = s.asfreq("D", fill_value=0.0)
        tasks.append((prdid, custid, locid, s))

    total = len(tasks)
    results: list[ComboResult] = []

    if cfg.n_jobs <= 1:
        for p, c, l, s in tasks:
            r = _fit_one(p, c, l, s, cfg)
            results.append(r)
            if on_progress:
                on_progress(len(results), total, r)
    else:
        with ProcessPoolExecutor(max_workers=cfg.n_jobs) as pool:
            futures = {pool.submit(_fit_one, p, c, l, s, cfg): (p, c, l) for p, c, l, s in tasks}
            for fut in as_completed(futures):
                r = fut.result()
                results.append(r)
                if on_progress:
                    on_progress(len(results), total, r)

    return RunSummary(results=results)
