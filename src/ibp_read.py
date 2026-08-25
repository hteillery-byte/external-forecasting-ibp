"""Lee el histórico de demanda diaria desde IBP y lo normaliza al formato largo
(PRDID, CUSTID, LOCID, FECHA, CANTIDAD) que consume forecast_engine."""
from __future__ import annotations

import pandas as pd

from src.ibp_client import IBPKeyFigureClient

DIM_COLS = ["PRDID", "CUSTID", "LOCID"]


def read_history(
    client: IBPKeyFigureClient,
    kf_name: str,
    period_field: str = "PERIODID1_TSTAMP",
    filter_str: str | None = None,
    use_planning_api: bool = True,
) -> pd.DataFrame:
    select_fields = DIM_COLS + [period_field, kf_name]
    raw = client.read_key_figure(select_fields, filter_str=filter_str, use_planning_api=use_planning_api)
    if raw.empty:
        return pd.DataFrame(columns=DIM_COLS + ["FECHA", "CANTIDAD"])

    out = raw.rename(columns={period_field: "FECHA", kf_name: "CANTIDAD"})
    out["FECHA"] = pd.to_datetime(out["FECHA"]).dt.tz_localize(None)
    out["CANTIDAD"] = pd.to_numeric(out["CANTIDAD"], errors="coerce").fillna(0.0)
    for c in DIM_COLS:
        out[c] = out[c].astype(str)
    return out[DIM_COLS + ["FECHA", "CANTIDAD"]]
