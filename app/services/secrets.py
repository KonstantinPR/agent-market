"""Шифрование ключей маркетплейсов кабинетов (Fernet).

Ключ шифрования лежит в data/secrets.key (вне git, .gitignore) и создаётся
автоматически при первой шифровке. Если cryptography недоступен или файл
ключа не читается — encrypt() вернёт пустую строку, decrypt() вернёт пустой
dict (т.е. кабинет «без ключей» — корректное состояние для нового кабинета).
"""
import json
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = BASE_DIR / "data" / "secrets.key"

_cached: Optional[object] = None


def _fernet():
    """Возвращает ленивый Fernet для KEY_PATH (None — шифрование недоступно)."""
    global _cached
    if _cached is not None:
        return _cached
    try:
        from cryptography.fernet import Fernet
    except Exception:  # pragma: no cover - нет зависимости
        _cached = None
        return None
    try:
        data = KEY_PATH.read_bytes()
        if not data:
            raise OSError("empty key file")
    except OSError:
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = Fernet.generate_key()
        KEY_PATH.write_bytes(data)
    try:
        _cached = Fernet(data)
    except Exception:  # pragma: no cover - повреждённый файл
        _cached = None
    return _cached


def encrypt(obj: dict) -> str:
    """Сериализует dict в зашифрованную строку ('' если шифрование недоступно)."""
    f = _fernet()
    if f is None:
        return ""
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return f.encrypt(raw).decode("ascii")


def decrypt(stored: str) -> dict:
    """Расшифровывает creds кабинета. Битые/пустые значения -> пустой dict."""
    f = _fernet()
    if f is None or not stored:
        return {}
    try:
        data = f.decrypt(stored.encode("ascii"))
        out = json.loads(data.decode("utf-8"))
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}