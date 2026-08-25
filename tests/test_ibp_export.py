import pandas as pd

from src.ibp_export import build_write_rows


def test_build_write_rows_shape_and_format():
    df = pd.DataFrame({
        "PRDID": ["P1", "P2"],
        "CUSTID": ["C1", "C1"],
        "LOCID": ["L1", "L1"],
        "FECHA": pd.to_datetime(["2026-08-25", "2026-08-26"]),
        "VALUE": [12.3456, -1.0],
    })
    rows = build_write_rows(df, "ZEXTFORECASTQTY", "PERIODID1_TSTAMP")

    assert rows[0] == {
        "PRDID": "P1", "CUSTID": "C1", "LOCID": "L1",
        "PERIODID1_TSTAMP": "2026-08-25T00:00:00",
        "ZEXTFORECASTQTY": "12.34560",
    }
    # negative values are clipped to zero before writing back to IBP
    assert rows[1]["ZEXTFORECASTQTY"] == "0.00000"
