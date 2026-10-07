import pytest

import app.services.yandex_disk as yd
from app.config import settings
from app.services.yandex_disk import YandexDiskError


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


def _mk_dir_resp():
    return FakeResponse(200, {"type": "dir"})


@pytest.fixture(autouse=True)
def _clear_token(monkeypatch):
    monkeypatch.setattr(settings, "yandex_disk_token", "tok-test")


@pytest.fixture()
def _mock_requests(monkeypatch):
    calls = {"get": [], "put": [], "delete": []}

    def _get(url, params=None, headers=None, timeout=None):
        calls["get"].append((url, params))
        if url.endswith("/resources/upload"):
            return FakeResponse(200, {"href": "https://upload/href"})
        if url.endswith("/resources/download"):
            return FakeResponse(200, {"href": "https://upload/href"})
        return _mk_dir_resp()

    def _put(url, params=None, headers=None, timeout=None, data=None):
        calls["put"].append((url, params))
        return FakeResponse(201)

    def _delete(url, params=None, headers=None, timeout=None):
        calls["delete"].append((url, params))
        return FakeResponse(202)

    monkeypatch.setattr(yd.requests, "get", _get)
    monkeypatch.setattr(yd.requests, "put", _put)
    monkeypatch.setattr(yd.requests, "delete", _delete)
    yield calls


def test_require_token_raises_when_empty(monkeypatch):
    monkeypatch.setattr(settings, "yandex_disk_token", "   ")
    with pytest.raises(YandexDiskError, match="не задан"):
        yd.require_token()


def test_upload_bytes_overwrites_and_uploads(_mock_requests):
    res = yd.upload_bytes("t", "/agent_market", "wb_stock.xlsx", b"XLSX")
    assert res == {"ok": True, "folder": "/agent_market", "name": "wb_stock.xlsx", "size": 4}
    upload_calls = [c for c in _mock_requests["get"] if c[0].endswith("/upload")]
    assert upload_calls[-1][1] == {"path": "/agent_market/wb_stock.xlsx", "overwrite": "true"}


def test_upload_creates_folder_when_missing(_mock_requests, monkeypatch):
    def _get(url, params=None, headers=None, timeout=None):
        if url.endswith("/resources/upload"):
            return FakeResponse(200, {"href": "https://upload/href"})
        return FakeResponse(404)

    monkeypatch.setattr(yd.requests, "get", _get)
    res = yd.upload_bytes("t", "/agent_market", "r.xlsx", b"data")
    assert res["ok"]
    mkdirs = [c for c in _mock_requests["put"] if c[1] == {"path": "/agent_market"}]
    assert mkdirs


def test_list_files_parses_items(_mock_requests, monkeypatch):
    body = {
        "_embedded": {"items": [
            {"type": "file", "name": "a.xlsx", "size": 10, "modified": "2026-09-14T00:00:00Z", "path": "disk:/agent_market/a.xlsx"},
            {"type": "dir", "name": "sub", "path": "disk:/agent_market/sub"},
        ]},
    }

    def _get(url, params=None, headers=None, timeout=None):
        return FakeResponse(200, body)

    monkeypatch.setattr(yd.requests, "get", _get)
    data = yd.list_files("t")
    assert len(data["files"]) == 1
    assert data["files"][0]["name"] == "a.xlsx"


def test_list_files_empty_when_folder_missing(_mock_requests, monkeypatch):
    def _get(url, params=None, headers=None, timeout=None):
        return FakeResponse(404)

    monkeypatch.setattr(yd.requests, "get", _get)
    assert yd.list_files("t") == {"folder": "/agent_market", "files": []}


def test_delete_file_ok(_mock_requests):
    yd.delete_file("t", "disk:/agent_market/old.xlsx")
    assert _mock_requests["delete"][0][1] == {"path": "disk:/agent_market/old.xlsx"}


def test_download_bytes_returns_content(_mock_requests, monkeypatch):
    def _get(url, params=None, headers=None, timeout=None):
        if url.endswith("/resources/download"):
            return FakeResponse(200, {"href": "https://upload/href"})
        content = FakeResponse(200)
        content.content = b"REAL"
        return content

    monkeypatch.setattr(yd.requests, "get", _get)
    assert yd.download_bytes("t", "disk:/agent_market/a.xlsx") == b"REAL"


def test_401_raises_known_message():
    with pytest.raises(YandexDiskError, match="просрочен"):
        yd._raise_api(FakeResponse(401, text="unauthorized"))


def test_404_raises_not_found():
    with pytest.raises(YandexDiskError, match="не найдены"):
        yd._raise_api(FakeResponse(404, text="not found"))


def test_http_error_without_special_code():
    with pytest.raises(YandexDiskError, match="503"):
        yd._raise_api(FakeResponse(503, text="overload"))