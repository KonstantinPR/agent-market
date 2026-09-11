from io import BytesIO

import pandas as pd


def df_to_excel_stream(df: pd.DataFrame, sheet_name: str = "Данные") -> BytesIO:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
    buf.seek(0)
    return buf


def read_excel_bytes(data: bytes, sheet=0) -> pd.DataFrame:
    return pd.read_excel(BytesIO(data), sheet_name=sheet, dtype=str)