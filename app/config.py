from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8")

    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "agent_market"

    sync_days_default: int = 30
    testing_mode: bool = True
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    wb_api_key: str = ""
    wb_finance_api_key: str = ""
    wb_finance_api_key_2: str = ""
    # Токен WB для связки «ИП Прудников · WB» (полный доступ: контент, аналитика,
    # статистика, финансы, цены). Один токен закрывает standard/finance/finance2.
    wb_api_token_ip_2: str = ""
    ozon_client_id: str = ""
    ozon_api_key: str = ""
    yandex_disk_token: str = ""

    default_net_cost: float = 500.0

    photos_root: str = r"C:\YandexDisk\ФОТОГРАФИИ"
    thumbs_dir: str = "data/thumbs"
    thumb_max_px: int = 800
    thumb_quality: int = 82

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def admin_database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/postgres"
        )

    @property
    def mock_dir(self) -> Path:
        return BASE_DIR / "data" / "mock"


settings = Settings()