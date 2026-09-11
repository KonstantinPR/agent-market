import pandas as pd

from app.config import settings


class BaseProvider:
    """Базовый провайдер: в testing_mode читает мок-CSV, иначе — реальный HTTP."""

    def __init__(self, testing_mode=None):
        self.testing = settings.testing_mode if testing_mode is None else testing_mode
        self.mock_dir = settings.mock_dir
        # Если True — при 429 сразу бросать ошибку (для массового обновления),
        # вместо ожидания и повторов (которые могут занять минуты).
        self.fail_fast_429 = False

    def read_mock(self, filename: str) -> pd.DataFrame:
        path = self.mock_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Мок-файл не найден: {path} — запустите scripts/load_sample.py")
        return pd.read_csv(path, sep=";", encoding="utf-8", dtype=str)

    def _filter_dates(self, df: pd.DataFrame, date_from, date_to, col="date") -> pd.DataFrame:
        df[col] = pd.to_datetime(df[col]).dt.date
        mask = (df[col] >= pd.Timestamp(date_from).date()) & (df[col] <= pd.Timestamp(date_to).date())
        return df.loc[mask].reset_index(drop=True)

    def _require_live_keys(self, what: str):
        if self.testing:
            return
        raise RuntimeError(
            f"Режим реальных API для {what} не настроен: TESTING_MODE=false, но ключи отсутствуют. "
            "Заполните ключи в .env или верните TESTING_MODE=true."
        )