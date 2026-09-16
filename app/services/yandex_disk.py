"""Работа с Яндекс.Диском через REST API cloud-api.yandex.net (без сторонних зависимостей).

Токен (OAuth) задаётся в .env: YANDEX_DISK_TOKEN (поле settings.yandex_disk_token).
"""
import requests

from app.config import settings

_DISK_URL = "https://cloud-api.yandex.net/v1/disk/resources"
_TIMEOUT = 60


class YandexDiskError(Exception):
    pass


def _token() -> str:
    tok = (settings.yandex_disk_token or "").strip()
    if not tok:
        raise YandexDiskError("Токен Яндекс.Диска не задан (YANDEX_DISK_TOKEN в .env)")
    return tok


def require_token() -> str:
    return _token()


def _headers(token: str) -> dict:
    return {"Authorization": f"OAuth {token}"}


def ensure_folder(token: str, path: str) -> None:
    if path in ("", "/"):
        return
    resp = requests.get(_DISK_URL, params={"path": path}, headers=_headers(token), timeout=_TIMEOUT)
    if resp.status_code == 200:
        if resp.json().get("type") == "dir":
            return
        raise YandexDiskError(f"'{path}' уже существует и не является папкой")
    if resp.status_code == 404:
        resp = requests.put(_DISK_URL, params={"path": path}, headers=_headers(token), timeout=_TIMEOUT)
        if resp.status_code not in (200, 201, 409):
            raise YandexDiskError(f"Не удалось создать папку '{path}': {resp.status_code}")
        return
    _raise_api(resp)


def list_files(token: str, folder: str = "/agent_market") -> dict:
    resp = requests.get(_DISK_URL, params={"path": folder, "limit": 200},
                        headers=_headers(token), timeout=_TIMEOUT)
    if resp.status_code == 404:
        return {"folder": folder, "files": []}
    _raise_api(resp)
    data = resp.json()
    items = []
    for it in data.get("_embedded", {}).get("items", []):
        if it.get("type") != "file":
            continue
        items.append({
            "name": it.get("name", ""),
            "size": it.get("size", 0),
            "modified": it.get("modified", ""),
            "path": it.get("path", ""),
        })
    return {"folder": folder, "files": items}


def upload_bytes(token: str, folder: str, name: str, data: bytes) -> dict:
    ensure_folder(token, folder)
    full = f"{folder}/{name}"
    upload = requests.get(_DISK_URL + "/upload", params={"path": full, "overwrite": "true"},
                          headers=_headers(token), timeout=_TIMEOUT)
    _raise_api(upload)
    href = upload.json().get("href")
    if not href:
        raise YandexDiskError("API Яндекса не вернул ссылку для загрузки")
    resp = requests.put(href, data=data, timeout=_TIMEOUT * 4)
    if resp.status_code not in (200, 201, 202):
        raise YandexDiskError(f"Ошибка загрузки файла '{name}': {resp.status_code}")
    return {"ok": True, "folder": folder, "name": name, "size": len(data)}


def download_bytes(token: str, path: str) -> bytes:
    download = requests.get(_DISK_URL + "/download", params={"path": path},
                            headers=_headers(token), timeout=_TIMEOUT)
    _raise_api(download)
    href = download.json().get("href")
    if not href:
        raise YandexDiskError("API Яндекса не вернул ссылку для скачивания")
    resp = requests.get(href, timeout=_TIMEOUT * 4)
    if resp.status_code != 200:
        raise YandexDiskError(f"Ошибка скачивания файла: {resp.status_code}")
    return resp.content


def delete_file(token: str, path: str) -> None:
    resp = requests.delete(_DISK_URL, params={"path": path}, headers=_headers(token), timeout=_TIMEOUT)
    if resp.status_code not in (200, 202, 204):
        _raise_api(resp)


def _raise_api(resp: requests.Response):
    if resp.status_code == 401:
        raise YandexDiskError("Токен Яндекс.Диска недействителен или просрочен (проверьте YANDEX_DISK_TOKEN)")
    if resp.status_code == 404:
        raise YandexDiskError("Файл/папка на Яндекс.Диске не найдены")
    if resp.status_code >= 400:
        raise YandexDiskError(f"Ошибка Яндекс.Диска: {resp.status_code} {resp.text[:200]}")