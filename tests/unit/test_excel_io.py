"""Юнит-тесты Excel-хелперов (sanitize + чтение)."""
import pandas as pd

from app.services.excel_io import read_excel_bytes, df_to_excel_stream


def test_df_to_excel_stream_strips_illegal_chars():
    df = pd.DataFrame({
        "srid": ["0102\x1d91mal\x1cware=", "ok\x1ftoken"],
        "name": ["Галстук", "Нормальная строка"],
        "qty": [1, 2],
    })
    buf = df_to_excel_stream(df)
    out = pd.read_excel(buf, sheet_name="Данные", dtype=str)
    assert out.loc[0, "srid"] == "010291malware="
    assert out.loc[1, "srid"] == "oktoken"


def test_df_to_excel_stream_serializes_dict_and_list():
    df = pd.DataFrame({
        "dimensions": [{"width": 10.0, "height": 1.0}, None],
        "characteristics": [[{"name": "Цвет", "value": "Серый"}], []],
        "plain": [None, "x"],
    })
    buf = df_to_excel_stream(df)
    out = pd.read_excel(buf, sheet_name="Данные", dtype=str)
    assert "width" in out.loc[0, "dimensions"]
    assert "Цвет" in out.loc[0, "characteristics"]


def test_df_to_excel_stream_empty_df():
    buf = df_to_excel_stream(pd.DataFrame())
    out = pd.read_excel(buf, sheet_name="Данные", dtype=str)
    assert out.empty


def test_read_excel_bytes_dtype_str():
    buf = df_to_excel_stream(pd.DataFrame({"a": [1.5]}))
    out = read_excel_bytes(buf.getvalue())
    assert isinstance(out.loc[0, "a"], str)


def test_direct_strip_of_tab_and_newline_kept(tmp_path):
    """\t, \n, \r — допустимы для openpyxl и не вырезаются."""
    df = pd.DataFrame({"t": ["a\tb", "c\nd"]})
    buf = df_to_excel_stream(df)
    out = pd.read_excel(buf, sheet_name="Данные", dtype=str)
    assert out.loc[0, "t"] == "a\tb"
    assert out.loc[1, "t"] == "c\nd"