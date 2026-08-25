"""Lee el histórico de demanda diaria desde IBP y lo normaliza al formato largo
(PRDID, CUSTID, LOCID, FECHA, CANTIDAD) que consume forecast_engine."""
from __future__ import annotations

import re
from collections.abc import Callable

import pandas as pd

from src.ibp_client import IBPKeyFigureClient

DIM_COLS = ["PRDID", "CUSTID", "LOCID"]

# SAP Gateway OData v2 serializa fechas en JSON como "/Date(1735689600000)/"
# (ms epoch), no como ISO 8601 — pandas.to_datetime no lo reconoce directo.
_SAP_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def _parse_ibp_datetime(series: pd.Series) -> pd.Series:
    as_str = series.astype(str)
    ms = as_str.str.extract(_SAP_DATE_RE, expand=False)
    parsed = pd.to_datetime(ms.astype("Int64"), unit="ms", errors="coerce")

    missing = parsed.isna()
    if missing.any():
        # Fallback para tenants/servicios que sí devuelven ISO 8601 directo.
        parsed.loc[missing] = pd.to_datetime(as_str.loc[missing], errors="coerce")
    return parsed


def read_history(
    client: IBPKeyFigureClient,
    kf_name: str,
    period_field: str = "PERIODID1_TSTAMP",
    filter_str: str | None = None,
    use_planning_api: bool = True,
    max_rows: int | None = None,
    on_page: Callable[[int, int], None] | None = None,
    extra_select_fields: list[str] | None = None,
) -> pd.DataFrame:
    """``extra_select_fields``: propiedades adicionales a incluir en $select — necesario
    cuando ``filter_str`` referencia un atributo (p.ej. CATEGORY) que IBP exige tener
    también seleccionado, no solo filtrado."""
    select_fields = DIM_COLS + [period_field, kf_name] + (extra_select_fields or [])
    raw = client.read_key_figure(
        select_fields, filter_str=filter_str, use_planning_api=use_planning_api,
        max_rows=max_rows, on_page=on_page,
    )
    if raw.empty:
        return pd.DataFrame(columns=DIM_COLS + ["FECHA", "CANTIDAD"])

    out = raw.rename(columns={period_field: "FECHA", kf_name: "CANTIDAD"})
    out["FECHA"] = _parse_ibp_datetime(out["FECHA"])
    if out["FECHA"].dt.tz is not None:
        out["FECHA"] = out["FECHA"].dt.tz_localize(None)
    out["CANTIDAD"] = pd.to_numeric(out["CANTIDAD"], errors="coerce").fillna(0.0)
    for c in DIM_COLS:
        out[c] = out[c].astype(str)
    # IBP no garantiza orden cronológico en la paginación -- sin esto, cualquier
    # gráfico de líneas conecta los puntos en el orden de llegada (zigzag) y
    # `asfreq("D")` en forecast_engine puede comportarse mal sobre un índice no ordenado.
    out = out.sort_values(DIM_COLS + ["FECHA"]).reset_index(drop=True)
    return out[DIM_COLS + ["FECHA", "CANTIDAD"]]
