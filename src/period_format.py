"""Formatea columnas de período al estilo con que IBP las muestra en pantalla
(p.ej. "MAR 2026" para mensual), en vez del timestamp crudo.

La granularidad de cada PERIODIDx_TSTAMP es específica de la Planning Area
(depende de cómo se configuró su Time Profile) — no se puede inferir del
número de columna (confirmado con datos reales: en este tenant PERIODID0 es
diario y PERIODID1 no lo es). Por eso se infiere directamente del
espaciado real entre las fechas distintas presentes en los datos.
"""
from __future__ import annotations

import pandas as pd

_MESES_ES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]

Granularity = str  # "day" | "week" | "month" | "quarter" | "year"


def infer_period_granularity(dates: pd.Series) -> Granularity:
    d = pd.Series(sorted(pd.to_datetime(dates.dropna().unique())))
    if len(d) < 2:
        return "day"

    deltas = d.diff().dropna().dt.days
    typical = int(deltas.mode().iloc[0]) if not deltas.empty else 1
    all_month_start = (d.dt.day == 1).all()
    all_quarter_start = all_month_start and d.dt.month.isin([1, 4, 7, 10]).all()

    if all_quarter_start and 80 <= typical <= 100:
        return "quarter"
    if all_month_start and 27 <= typical <= 31:
        return "month"
    if typical >= 360:
        return "year"
    if 6 <= typical <= 8:
        return "week"
    return "day"


def format_period_label(date: pd.Timestamp, granularity: Granularity) -> str:
    if pd.isna(date):
        return ""
    if granularity == "week":
        iso = date.isocalendar()
        return f"Sem {iso.week:02d} {iso.year}"
    if granularity == "month":
        return f"{_MESES_ES[date.month - 1]} {date.year}"
    if granularity == "quarter":
        q = (date.month - 1) // 3 + 1
        return f"Q{q} {date.year}"
    if granularity == "year":
        return str(date.year)
    return date.strftime("%d-%m-%Y")


def add_period_label_column(df: pd.DataFrame, date_col: str = "FECHA", label_col: str = "PERÍODO") -> pd.DataFrame:
    """Agrega una columna de período formateado, inferido de las fechas de ``df``.

    Solo para presentación (tablas/gráficos) — ``date_col`` no se toca, así
    que sigue sirviendo para cálculo y para el export a IBP.
    """
    if df.empty:
        out = df.copy()
        out[label_col] = pd.Series(dtype="object")
        return out
    granularity = infer_period_granularity(df[date_col])
    out = df.copy()
    out[label_col] = out[date_col].map(lambda d: format_period_label(d, granularity))
    return out
