import pandas as pd

from src.period_format import add_period_label_column, format_period_label, infer_period_granularity


def test_infers_daily():
    dates = pd.Series(pd.date_range("2026-08-01", periods=10, freq="D"))
    assert infer_period_granularity(dates) == "day"


def test_infers_weekly():
    dates = pd.Series(pd.date_range("2026-08-03", periods=8, freq="7D"))
    assert infer_period_granularity(dates) == "week"


def test_infers_monthly():
    dates = pd.Series(pd.date_range("2026-01-01", periods=6, freq="MS"))
    assert infer_period_granularity(dates) == "month"


def test_infers_quarterly():
    dates = pd.Series(pd.to_datetime(["2026-01-01", "2026-04-01", "2026-07-01", "2026-10-01"]))
    assert infer_period_granularity(dates) == "quarter"


def test_format_month_label_spanish():
    assert format_period_label(pd.Timestamp("2026-03-01"), "month") == "MAR 2026"


def test_format_week_label():
    assert format_period_label(pd.Timestamp("2026-03-02"), "week").startswith("Sem ")


def test_add_period_label_column_monthly():
    df = pd.DataFrame({
        "FECHA": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"]),
        "CANTIDAD": [1, 2, 3],
    })
    out = add_period_label_column(df)
    assert list(out["PERÍODO"]) == ["ENE 2026", "FEB 2026", "MAR 2026"]


def test_add_period_label_column_empty_df():
    df = pd.DataFrame(columns=["FECHA", "CANTIDAD"])
    out = add_period_label_column(df)
    assert "PERÍODO" in out.columns
    assert out.empty
