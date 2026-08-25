import pandas as pd

from src.ibp_read import _parse_ibp_datetime


def test_parses_sap_odata_v2_date_format():
    s = pd.Series(["/Date(1735689600000)/", "/Date(1735776000000)/"])
    out = _parse_ibp_datetime(s)
    assert list(out.dt.date.astype(str)) == ["2025-01-01", "2025-01-02"]
    assert out.dt.tz is None


def test_falls_back_to_iso_strings():
    s = pd.Series(["2026-01-03T00:00:00", "2026-01-04T00:00:00"])
    out = _parse_ibp_datetime(s)
    assert list(out.dt.date.astype(str)) == ["2026-01-03", "2026-01-04"]


def test_handles_mixed_formats():
    s = pd.Series(["/Date(1735689600000)/", "2026-01-04T00:00:00"])
    out = _parse_ibp_datetime(s)
    assert out.notna().all()
