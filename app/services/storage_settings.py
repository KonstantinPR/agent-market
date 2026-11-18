# -*- coding: utf-8 -*-
"""Глобальные настройки хранилищ и ссылок проекта (T-80).

Раньше пути были зашиты: фото — ``C:\\YandexDisk\\ФОТОГРАФИИ``, аналитика —
``E:\\Мой диск`` (смонтированный Google Drive), отчёты — облачная папка
``/agent_market`` на Яндекс.Диске. Теперь они видны и редактируются в шторке
«Личные кабинеты», а сервисы читают актуальные значения отсюда.

Хранение: одна строка ``app_settings(key='project')`` в public, внутри JSON с
**переопределениями**. Нет ключа — берётся значение по умолчанию из config/.env
(для фото промежуточно учитывается и переменная окружения ``PHOTOS_ROOT``).
Так «Сбросить» возвращает исходное поведение, не теряя дефолты.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import settings

SETTING_KEY = "project"
ROOT_KEYS = ("photos_root", "analytics_root", "products_file", "reports_folder")
LINKS_KEY = "links"

#: Заголовки строк хранилищ (для UI и документации).
ROOT_TITLES = {
    "photos_root": "Фото товаров",
    "analytics_root": "Аналитика (Google Drive)",
    "products_file": "Каталог себестоимости (products.xlsx)",
    "reports_folder": "Отчёты на Яндекс.Диске",
}

#: Адрес облачной папки на Диске для открытия в браузере.
YANDEX_DISK_CLIENT = "https://disk.yandex.ru/client/disk"

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


# ── значения по умолчанию ────────────────────────────────────────────────────

#: Подпапка общего каталога внутри корня аналитики (T-82).
PRODUCTS_SUBDIR = "ОБЩИЕ"
PRODUCTS_NAME = "products.xlsx"


def products_file_default(analytics: Optional[str] = None) -> str:
    """Дефолтный путь к общему файлу каталога: ``<аналитика>/ОБЩИЕ/products.xlsx``."""
    base = analytics or settings.analytics_root
    return str(Path(base) / PRODUCTS_SUBDIR / PRODUCTS_NAME)


def default_roots() -> dict[str, str]:
    """Дефолтные корни из config/.env (фото — с учётом PHOTOS_ROOT)."""
    analytics = settings.analytics_root
    return {
        "photos_root": os.environ.get("PHOTOS_ROOT") or settings.photos_root,
        "analytics_root": analytics,
        "products_file": products_file_default(analytics),
        "reports_folder": settings.yandex_reports_dir,
    }


def _local_app_url() -> str:
    host = (settings.app_host or "").strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{settings.app_port}"


def default_links() -> list[dict]:
    """Встроенные ссылки проекта — их видно в панели с первого запуска."""
    return [
        {
            "id": "github",
            "title": "GitHub — репозиторий проекта",
            "url": "https://github.com/KonstantinPR/agent-market",
            "builtin": True,
        },
        {
            "id": "local",
            "title": "Приложение (локально)",
            "url": _local_app_url(),
            "builtin": True,
        },
        {
            "id": "gdrive",
            "title": "Google Drive — «Мой диск»",
            "url": "https://drive.google.com/drive/my-drive",
            "builtin": True,
        },
        {
            "id": "yandex",
            "title": "Яндекс.Диск — облако",
            "url": YANDEX_DISK_CLIENT,
            "builtin": True,
        },
    ]


# ── чтение/запись переопределений ────────────────────────────────────────────

def _load(db: Optional[Session] = None) -> dict[str, Any]:
    """Переопределения из app_settings. Битый JSON не роняет приложение."""
    own = db is None
    if own:
        from app.database import SessionLocal

        db = SessionLocal()
    try:
        row = db.execute(
            select(models.AppSetting).where(models.AppSetting.key == SETTING_KEY)
        ).scalar_one_or_none()
        raw = (row.value if row is not None else "") or ""
    finally:
        if own:
            db.close()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(db: Session, data: dict[str, Any]) -> None:
    row = db.get(models.AppSetting, SETTING_KEY)
    if row is None:
        row = models.AppSetting(key=SETTING_KEY, value="")
        db.add(row)
    row.value = json.dumps(data, ensure_ascii=False)
    db.commit()


def _reset_photos_index() -> None:
    """После смены корня фото старый индекс недействителен."""
    from app.services import photos as photos_service

    photos_service.set_index(None)


# ── валидация и нормализация ─────────────────────────────────────────────────

def _clean_path(raw: Any, title: str) -> str:
    value = str(raw or "").strip().strip('"')
    if not value:
        raise ValueError(f"{title}: путь не может быть пустым")
    if "\x00" in value:
        raise ValueError(f"{title}: недопустимый путь")
    return value


def normalize_folder(raw: Any) -> str:
    """Облачная папка Диска: ``agent_market`` → ``/agent_market``; без ``..``."""
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        raise ValueError("Папка на Диске не может быть пустой")
    parts = [p for p in value.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ValueError("Папка на Диске не может содержать «.» или «..»")
    return "/" + "/".join(parts) if parts else "/"


def join_folder(base: str, *parts: str) -> str:
    """Склеить облачный путь: ``/agent_market`` + ``Наш склад``."""
    chunks = [str(p or "").strip("/") for p in (base, *parts)]
    chunks = [c for c in chunks if c]
    return "/" + "/".join(chunks) if chunks else "/"


def _clean_company_rel(raw: Any) -> str:
    """Подпапка фирмы внутри analytics_root. Абсолютные пути запрещены."""
    value = str(raw or "").strip().strip('"')
    if not value:
        return ""
    if "\x00" in value:
        raise ValueError("Недопустимый путь папки аналитики")
    if re.match(r"^[A-Za-z]:", value) or value.startswith(("/", "\\")):
        raise ValueError("Укажите подпапку внутри корня аналитики, а не полный путь")
    parts = re.split(r"[\\/]+", value)
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("Папка аналитики не может содержать пустые сегменты или «..»")
    return value


def _clean_links(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("links должен быть списком ссылок")
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw[:100]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            raise ValueError("У ссылки должны быть название и адрес")
        if len(title) > 200:
            raise ValueError("Название ссылки длиннее 200 символов")
        if len(url) > 2000:
            raise ValueError("Адрес ссылки длиннее 2000 символов")
        if not _URL_RE.match(url):
            raise ValueError(f"Ссылка «{title}»: адрес должен начинаться с http:// или https://")
        link_id = str(item.get("id") or "").strip() or ("link-" + uuid4().hex[:10])
        if link_id in seen:
            link_id = "link-" + uuid4().hex[:10]
        seen.add(link_id)
        out.append({
            "id": link_id,
            "title": title,
            "url": url,
            "builtin": bool(item.get("builtin")),
        })
    return out


# ── действующие значения ─────────────────────────────────────────────────────

def _effective(data: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Действующие значения и дефолты с учётом переопределений.

    ``products_file`` по умолчанию лежит внутри действующего корня аналитики:
    если пользователь перенёс «Мой диск», файл каталога ищется там же, пока
    путь не переопределён отдельно.
    """
    defaults = default_roots()
    values = dict(defaults)
    for key in ROOT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()
    # Файл каталога не переопределён — он лежит внутри действующего корня
    # аналитики, поэтому следует за ним (а не за дефолтом из config).
    override = data.get("products_file")
    if not (isinstance(override, str) and override.strip()):
        values["products_file"] = products_file_default(values["analytics_root"])
    defaults["products_file"] = products_file_default(values["analytics_root"])
    return values, defaults


def roots(db: Optional[Session] = None) -> dict[str, str]:
    """Действующие корни: переопределение из UI → дефолт из config/.env."""
    values, _defaults = _effective(_load(db))
    return values


def photos_root(db: Optional[Session] = None) -> str:
    return roots(db)["photos_root"]


def analytics_root(db: Optional[Session] = None) -> str:
    return roots(db)["analytics_root"]


def reports_folder(db: Optional[Session] = None) -> str:
    return normalize_folder(roots(db)["reports_folder"])


def products_file(db: Optional[Session] = None) -> str:
    """Путь к общему файлу каталога себестоимости (T-82)."""
    return roots(db)["products_file"]


def yandex_folder_url(folder: str) -> str:
    folder = normalize_folder(folder)
    if folder == "/":
        return YANDEX_DISK_CLIENT
    return YANDEX_DISK_CLIENT + quote(folder, safe="/")


# ── публичный payload для UI ─────────────────────────────────────────────────

def _root_row(key: str, value: str, default: str, custom: bool) -> dict:
    row: dict[str, Any] = {
        "title": ROOT_TITLES[key],
        "value": value,
        "default": default,
        "custom": custom,
        "kind": "yandex" if key == "reports_folder" else "path",
    }
    if key == "reports_folder":
        row["url"] = yandex_folder_url(value)
    elif key == "products_file":
        row["kind"] = "file"
        path = Path(value)
        row["folder"] = str(path.parent)
        try:
            row["exists"] = path.is_file()
            if row["exists"]:
                st = path.stat()
                row["size"] = int(st.st_size)
                row["mtime"] = datetime.fromtimestamp(
                    st.st_mtime).isoformat(timespec="seconds")
        except OSError:
            row["exists"] = False
    else:
        try:
            row["exists"] = Path(value).is_dir()
        except OSError:
            row["exists"] = False
    return row


def companies_payload(db: Session, companies: Iterable[models.Company]) -> list[dict]:
    base = Path(analytics_root(db))
    out = []
    for company in companies:
        rel = (company.analytics_dir or "").strip()
        full = base / rel if rel else None
        out.append({
            "id": company.id,
            "name": company.name,
            "analytics_dir": rel,
            "path": str(full) if full is not None else "",
            "exists": bool(full is not None and full.is_dir()),
        })
    return out


def public_payload(
    db: Session,
    companies: Optional[Iterable[models.Company]] = None,
    data: Optional[dict[str, Any]] = None,
) -> dict:
    """Всё, что нужно панели «Ссылки и хранилища»."""
    if data is None:
        data = _load(db)
    values, defaults = _effective(data)
    roots_out = {
        key: _root_row(key, values[key], defaults[key], key in data)
        for key in ROOT_KEYS
    }
    links = data.get(LINKS_KEY)
    if not isinstance(links, list):
        links = default_links()
    # T-82: статус последней синхронизации каталога из файла (ленивый импорт —
    # products_file сам читает настройки хранилищ).
    try:
        from app.services import products_file as products_file_service

        products_sync = products_file_service.public_state()
    except Exception:  # noqa: BLE001 — статус не должен ломать панель
        products_sync = {}
    return {
        "roots": roots_out,
        "links": links,
        "companies": companies_payload(db, companies or []),
        "products_sync": products_sync,
    }


# ── сохранение ───────────────────────────────────────────────────────────────

def save(db: Session, payload: dict, companies: Optional[Iterable[models.Company]] = None) -> dict:
    """Сохранить корни/ссылки. Возвращает новый публичный payload.

    Значение, совпавшее с дефолтом, хранится как отсутствие переопределения —
    тогда «Сбросить» и ручная правка не плодят одинаковых записей.
    """
    if not isinstance(payload, dict):
        raise ValueError("Ожидался объект с настройками")
    data = _load(db)
    defaults = default_roots()
    # T-82: дефолт файла каталога следует за новым корнем аналитики из этого же
    # запроса — иначе «значение равно дефолту» сравнивалось бы со старым путём.
    new_analytics = payload.get("analytics_root")
    if isinstance(new_analytics, str) and new_analytics.strip():
        defaults["products_file"] = products_file_default(new_analytics.strip())
    photos_changed = False
    for key in ROOT_KEYS:
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw is None:
            data.pop(key, None)
            if key == "photos_root":
                photos_changed = True
            continue
        title = ROOT_TITLES[key]
        value = normalize_folder(raw) if key == "reports_folder" else _clean_path(raw, title)
        old = data.get(key) if isinstance(data.get(key), str) else defaults[key]
        if key == "photos_root" and old != value:
            photos_changed = True
        if value == defaults[key]:
            data.pop(key, None)
        else:
            data[key] = value
    if LINKS_KEY in payload:
        raw_links = payload.get(LINKS_KEY)
        if raw_links is None:
            data.pop(LINKS_KEY, None)
        else:
            links = _clean_links(raw_links)
            if links == default_links():
                data.pop(LINKS_KEY, None)
            else:
                data[LINKS_KEY] = links
    _write(db, data)
    if photos_changed:
        _reset_photos_index()
    return public_payload(db, companies=companies, data=data)


def reset(
    db: Session,
    key: str,
    companies: Optional[Iterable[models.Company]] = None,
) -> dict:
    """Сбросить один корень или список ссылок к значениям по умолчанию."""
    if key not in ROOT_KEYS and key != LINKS_KEY:
        raise ValueError(f"Неизвестная настройка: {key}")
    data = _load(db)
    data.pop(key, None)
    _write(db, data)
    if key == "photos_root":
        _reset_photos_index()
    return public_payload(db, companies=companies, data=data)


def save_company_dir(
    db: Session,
    company: models.Company,
    rel: Any,
    companies: Optional[Iterable[models.Company]] = None,
) -> dict:
    """Сохранить подпапку аналитики фирмы (companies.analytics_dir)."""
    value = _clean_company_rel(rel)
    company.analytics_dir = value
    db.commit()
    db.refresh(company)
    return public_payload(db, companies=companies)


# ── открытие локальной папки в проводнике ────────────────────────────────────

def allowed_paths(db: Session, companies: Iterable[models.Company]) -> list[Path]:
    """Корни, внутри которых разрешено открывать папки."""
    values = roots(db)
    out: list[Path] = []
    for key in ("photos_root", "analytics_root"):
        try:
            out.append(Path(values[key]).resolve())
        except OSError:
            continue
    base = Path(values["analytics_root"])
    # T-82: папку с общим файлом каталога тоже разрешаем открывать.
    try:
        out.append(Path(values["products_file"]).parent.resolve())
    except OSError:
        pass
    for company in companies:
        rel = (company.analytics_dir or "").strip()
        if not rel:
            continue
        try:
            out.append((base / rel).resolve())
        except OSError:
            continue
    return out


def reveal(db: Session, raw_path: Any, companies: Iterable[models.Company]) -> str:
    """Открыть папку в проводнике. Только внутри настроенных хранилищ."""
    value = str(raw_path or "").strip().strip('"')
    if not value:
        raise ValueError("Не передан путь")
    try:
        target = Path(value).resolve()
    except OSError as exc:
        raise ValueError(f"Некорректный путь: {exc}") from exc
    allowed = allowed_paths(db, companies)
    if not any(target == root or root in target.parents for root in allowed):
        raise ValueError("Путь находится вне настроенных хранилищ")
    if not target.exists():
        raise ValueError("Папка не найдена")
    opener = getattr(os, "startfile", None)
    if opener is None:
        raise ValueError("Открытие папок поддерживается только в Windows")
    try:
        opener(str(target))
    except OSError as exc:
        raise ValueError(f"Не удалось открыть папку: {exc}") from exc
    return str(target)
