import pytest
import requests

from app.config import settings
from app.providers.errors import MarketError, OzonApiError, WbApiError, translate_request_error
from app.providers.wb import WbProvider
from app.services.refresh import _OZ_MESSAGES, _WB_MESSAGES


def test_market_error_carries_status():
    err = MarketError("что-то", status_code=418)
    assert err.status_code == 418
    assert err.args[0] == "что-то"


def test_subclasses_are_market_errors():
    assert issubclass(WbApiError, MarketError)
    assert issubclass(OzonApiError, MarketError)


def _http_error(status_code: int, text: str = "boom") -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status_code
    return requests.HTTPError(text, response=resp)


def test_translate_wb_401_uses_known_message():
    wrapped = translate_request_error("wb", _http_error(401), _WB_MESSAGES)
    assert isinstance(wrapped, WbApiError)
    assert wrapped.status_code == 401
    assert "WB_API_KEY" in str(wrapped)


def test_translate_ozon_403_uses_known_message():
    wrapped = translate_request_error("ozon", _http_error(403), _OZ_MESSAGES)
    assert isinstance(wrapped, OzonApiError)
    assert wrapped.status_code == 403
    assert "Ozon API" in str(wrapped)


def test_translate_unknown_code_falls_back_to_status():
    wrapped = translate_request_error("wb", _http_error(500), _WB_MESSAGES)
    assert wrapped.status_code == 500
    assert "500" in str(wrapped)


def test_translate_without_status_uses_502():
    exc = requests.ConnectionError("no route")
    wrapped = translate_request_error("wb", exc, _WB_MESSAGES)
    assert wrapped.status_code == 502


@pytest.mark.skipif(not settings.wb_api_key, reason="WB_API_KEY не задан в .env")
def test_fail_fast_429_raises_without_retries(monkeypatch):
    provider = WbProvider(testing_mode=True)
    provider.fail_fast_429 = True

    class FakeResp:
        status_code = 429
        headers = {"X-Ratelimit-Retry": "3600"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())
    with pytest.raises(WbApiError) as exc:
        provider._session_get("https://example.invalid/x")
    assert exc.value.status_code == 429
    assert "лимит запросов" in str(exc.value)


def _post_resp(status_code, headers=None):
    resp = requests.Response()
    resp.status_code = status_code
    resp.headers = headers or {}
    return resp


def test_session_post_finance_rotates_to_second_key_on_429(monkeypatch):
    monkeypatch.setattr(settings, "wb_finance_api_key", "PRIMARY")
    monkeypatch.setattr(settings, "wb_finance_api_key_2", "SECONDARY")
    provider = WbProvider(testing_mode=True)
    provider.fail_fast_429 = True

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(headers["Authorization"])
        return _post_resp(429) if len(calls) == 1 else _post_resp(200)

    monkeypatch.setattr("requests.post", fake_post)
    resp = provider._session_post("https://example.invalid/x", {"a": 1}, finance=True)
    assert resp.status_code == 200
    assert calls == ["PRIMARY", "SECONDARY"]


def test_session_post_finance_429_both_keys_raise(monkeypatch):
    monkeypatch.setattr(settings, "wb_finance_api_key", "PRIMARY")
    monkeypatch.setattr(settings, "wb_finance_api_key_2", "SECONDARY")
    provider = WbProvider(testing_mode=True)
    provider.fail_fast_429 = True

    def fake_post(url, headers=None, json=None, timeout=None):
        return _post_resp(429, headers={"X-Ratelimit-Retry": "3600"})

    monkeypatch.setattr("requests.post", fake_post)
    with pytest.raises(WbApiError) as exc:
        provider._session_post("https://example.invalid/x", {"a": 1}, finance=True)
    assert exc.value.status_code == 429


def test_session_post_without_alt_key_does_not_rotate(monkeypatch):
    monkeypatch.setattr(settings, "wb_finance_api_key", "PRIMARY")
    monkeypatch.setattr(settings, "wb_finance_api_key_2", "")
    provider = WbProvider(testing_mode=True)
    provider.fail_fast_429 = True

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(headers["Authorization"])
        return _post_resp(429, headers={"X-Ratelimit-Retry": "3600"})

    monkeypatch.setattr("requests.post", fake_post)
    with pytest.raises(WbApiError):
        provider._session_post("https://example.invalid/x", {"a": 1}, finance=True)
    assert calls == ["PRIMARY"]