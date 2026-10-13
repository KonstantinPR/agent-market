"""Исключения для ошибок маркетплейс-API.

Провайдеры кидают WbApiError / OzonApiError вместо сырых requests-исключений,
что даёт стабильные типы для проверок в тестах и понятные HTTP-ответы.
"""

from typing import Optional

import requests


class MarketError(RuntimeError):
    """Базовая ошибка обращения к API маркетплейса."""

    status_code = 502
    code = "market_error"
    default_message = "Ошибка обращения к API"

    def __init__(self, message=None, status_code=None, code=None, retry_after=None):
        super().__init__(message or self.default_message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        # Секунды до повтора (из заголовка Retry-After/X-Ratelimit-*), если API
        # их прислал. Воркер обновления использует для паузы перед повтором.
        self.retry_after = retry_after


class WbApiError(MarketError):
    """Ошибка обращения к WB API."""

    code = "wb_api_error"
    default_message = "Ошибка обращения к WB API"


class OzonApiError(MarketError):
    """Ошибка обращения к Ozon API."""

    code = "ozon_api_error"
    default_message = "Ошибка обращения к Ozon API"


def translate_request_error(market: str, exc: requests.RequestException,
                            messages: dict) -> MarketError:
    """Превращает requests-исключение в MarketError по HTTP-коду ответа."""
    cls = WbApiError if market == "wb" else OzonApiError
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code in messages:
        return cls(messages[code], status_code=code)
    if code is not None:
        return cls(f"Ошибка обращения к API ({code}): {exc}", status_code=code)
    return cls(f"Ошибка обращения к API: {exc}")


def retry_after_seconds(headers) -> Optional[int]:
    """Секунды ожидания из заголовков rate limit (None, если их нет).

    Провайдеры отдают значение в MarketError.retry_after — воркер обновления
    ждёт ровно столько перед повтором упавшего из-за лимита шага.
    """
    if not headers:
        return None
    for header in ("X-Ratelimit-Retry", "X-Ratelimit-Reset", "Retry-After"):
        try:
            raw = headers.get(header)
        except AttributeError:
            return None
        if raw is None:
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return None