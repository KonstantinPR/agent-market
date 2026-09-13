import json
import re
from io import BytesIO

import pandas as pd

# Контрольные символы, которые openpyxl не принимает в ячейках (кроме \t\n\r)
_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_text(v: str) -> str:
    return _ILLEGAL_RE.sub("", v)


def _sanitize_value(v):
    """Значение ячейки для Excel: str без контрольных символов, list/dict -> json."""
    if isinstance(v, str):
        return _sanitize_text(v)
    if isinstance(v, (list, tuple, dict)):
        try:
            s = json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(v)
        return _sanitize_text(s)
    return v


def df_to_excel_stream(df: pd.DataFrame, sheet_name: str = "Данные") -> BytesIO:
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(_sanitize_value)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name=sheet_name, index=False)
    buf.seek(0)
    return buf


def read_excel_bytes(data: bytes, sheet=0) -> pd.DataFrame:
    return pd.read_excel(BytesIO(data), sheet_name=sheet, dtype=str)