"""Traduce los resultados de forecast_engine al formato de escritura de IBP."""
from __future__ import annotations

import pandas as pd

from src.ibp_client import IBPKeyFigureClient, WriteResult


def build_write_rows(df: pd.DataFrame, kf_name: str, period_field: str) -> list[dict]:
    """Convierte un DataFrame (PRDID, CUSTID, LOCID, FECHA, VALUE) en filas IBP.

    El formato numérico con 5 decimales replica el ejemplo oficial de
    SAP-samples (``"10.00000"``); IBP acepta el valor como string.
    """
    rows = []
    for r in df.itertuples(index=False):
        rows.append({
            "PRDID": r.PRDID,
            "CUSTID": r.CUSTID,
            "LOCID": r.LOCID,
            period_field: pd.Timestamp(r.FECHA).strftime("%Y-%m-%dT00:00:00"),
            kf_name: f"{max(r.VALUE, 0):.5f}",
        })
    return rows


def push_to_ibp(
    client: IBPKeyFigureClient,
    df: pd.DataFrame,
    kf_name: str,
    period_field: str = "PERIODID1_TSTAMP",
    do_commit: bool = True,
    poll: bool = True,
) -> list[WriteResult]:
    """Escribe un DataFrame de resultados (Forecast o Ex Post) como key figure en IBP."""
    rows = build_write_rows(df, kf_name, period_field)
    field_string = ["PRDID", "CUSTID", "LOCID", period_field, kf_name]
    return client.write_key_figures(rows, field_string, do_commit=do_commit, poll=poll)
