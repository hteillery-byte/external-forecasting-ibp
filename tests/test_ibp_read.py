import pandas as pd

from src.ibp_read import _parse_ibp_datetime, read_history


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


class _FakeClient:
    def __init__(self):
        self.last_select_fields = None

    def read_key_figure(self, select_fields, **kwargs):
        self.last_select_fields = select_fields
        return pd.DataFrame({
            "PRDID": ["P1"], "CUSTID": ["C1"], "LOCID": ["L1"],
            "PERIODID1_TSTAMP": ["2026-01-01T00:00:00"], "ZACTUALSQTYDAY": ["5"],
        })


def test_extra_select_fields_are_included_for_category_filter():
    client = _FakeClient()
    read_history(client, "ZACTUALSQTYDAY", extra_select_fields=["CATEGORY"])
    assert "CATEGORY" in client.last_select_fields


def test_extra_select_fields_defaults_to_none():
    client = _FakeClient()
    read_history(client, "ZACTUALSQTYDAY")
    assert client.last_select_fields == ["PRDID", "CUSTID", "LOCID", "PERIODID1_TSTAMP", "ZACTUALSQTYDAY"]
