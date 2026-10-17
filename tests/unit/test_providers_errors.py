import pytest
import requests

from app.config import settings
from app.providers.errors import MarketError, OzonApiError, WbApiError, translate_request_error
from app.providers.wb import WbProvider, _upload_error_detail
from app.services.refresh import _OZ_MESSAGES, _WB_MESSAGES


def test_market_error_carries_status():
    err = MarketError("что-то", status_code=418)
    assert err.status_code == 418
    assert err.args[0] == "что-то"


def test_subclasses_are_market_errors():
    assert issubclass(WbApiError, MarketError)
    assert issubclass(OzonApiError, MarketError)


def test_retry_after_seconds_reads_headers():
    from app.providers.errors import retry_after_seconds

    assert retry_after_seconds({"Retry-After": "12"}) == 12
    assert retry_after_seconds({"X-Ratelimit-Reset": "60.5"}) == 60
    assert retry_after_seconds({"X-Ratelimit-Retry": "3600"}) == 3600
    assert retry_after_seconds({}) is None
    assert retry_after_seconds(None) is None


def test_ozon_fail_fast_429_carries_retry_after(monkeypatch):
    from app.providers.ozon import OzonProvider

    provider = OzonProvider(credentials={"client_id": "1", "api_key": "k"},
                            fail_fast_429=True)

    class FakeResp:
        status_code = 429
        headers = {"Retry-After": "42"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp())
    with pytest.raises(OzonApiError) as exc:
        provider._post("https://example.invalid/x", {})
    assert exc.value.status_code == 429
    assert exc.value.retry_after == 42


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


def test_upload_error_detail_parses_wb_body():
    class Resp:
        def json(self):
            return {"title": "Ошибка валидации",
                    "data": [{"nmID": 123456, "errors": ["Минимальная цена ..."]}]}

    detail = _upload_error_detail(Resp())
    assert "Ошибка валидации" in detail
    assert "123456: Минимальная цена" in detail


def test_upload_error_detail_none_for_non_json():
    class Resp:
        def json(self):
            raise ValueError("нет json")

    assert _upload_error_detail(Resp()) is None


def test_update_prices_raises_readable_wbapierror(monkeypatch):
    provider = WbProvider()
    resp = requests.Response()
    resp.status_code = 400
    resp.json = lambda: {"title": "Стоимость не может быть меньше минимальной цены"}

    def boom(url, payload, num_retries=6, finance=False, key=None):
        raise requests.HTTPError("400 Client Error", response=resp)

    monkeypatch.setattr(provider, "_session_post", boom)
    with pytest.raises(WbApiError) as exc:
        provider.update_prices([{"nmID": 123456, "price": 100, "discount": 0}])
    assert "Стоимость не может быть меньше минимальной цены" in str(exc.value)
    assert exc.value.status_code == 400


def test_update_prices_without_detail_re_raises_http(monkeypatch):
    provider = WbProvider()
    resp = requests.Response()
    resp.status_code = 400
    resp.json = lambda: lambda: None  # нечитаемое тело → HTTPError летит дальше

    def boom(url, payload, num_retries=6, finance=False, key=None):
        raise requests.HTTPError("400 Client Error", response=resp)

    monkeypatch.setattr(provider, "_session_post", boom)
    with pytest.raises(requests.HTTPError):
        provider.update_prices([{"nmID": 123456, "price": 100, "discount": 0}])


def test_request_with_net_retry_retries_transient_ssl(monkeypatch):
    from app.providers.errors import request_with_net_retry

    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.SSLError("bad record mac")
        return _post_resp(200)

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    resp = request_with_net_retry(
        "GET", "https://example.invalid/x", timeout=1, attempts=3, backoff=0)
    assert resp.status_code == 200
    assert len(calls) == 2


def test_request_with_net_retry_gives_up_after_attempts(monkeypatch):
    from app.providers.errors import request_with_net_retry

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(requests.exceptions.ConnectionError):
        request_with_net_retry(
            "POST", "https://example.invalid/x", json={}, timeout=1,
            attempts=3, backoff=0)
    assert len(calls) == 3


def test_session_get_absorbs_transient_ssl_error(monkeypatch):
    """Одиночный SSL-сбой выкачки не роняет шаг: _session_get повторяет запрос."""
    provider = WbProvider(credentials={"standard": "k"})

    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.SSLError("bad record mac")
        return _post_resp(200)

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    resp = provider._session_get("https://example.invalid/cards")
    assert resp.status_code == 200
    assert len(calls) == 2