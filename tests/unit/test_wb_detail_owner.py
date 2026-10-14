"""Определение владельца детализации WB по oid (токен связки / имя файла)."""
import base64
import json

from app.api.wb import _report_oids
from app.services import cabinets as cabinet_service


def _jwt(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).rstrip(b"=").decode()
    return "hdr." + raw + ".sig"


def test_token_oid_reads_oid_from_jwt():
    assert cabinet_service.token_oid(_jwt({"oid": 250147786, "acc": 3})) == 250147786


def test_token_oid_garbage_is_none():
    assert cabinet_service.token_oid("") is None
    assert cabinet_service.token_oid("no-dots") is None
    assert cabinet_service.token_oid("a.!!!.c") is None
    assert cabinet_service.token_oid(_jwt({"acc": 3})) is None


def test_wb_oid_uses_any_wb_token():
    info = cabinet_service.CabinetInfo(
        id=1, code="c", name="n", schema="s", marketplace="wb",
        creds={"wb": {"standard": "", "finance": _jwt({"oid": 42})}},
    )
    assert cabinet_service.wb_oid(info) == 42
    ozon = cabinet_service.CabinetInfo(
        id=1, code="c", name="n", schema="s", marketplace="ozon", creds={})
    assert cabinet_service.wb_oid(ozon) is None


def test_report_oids_finds_marker():
    assert _report_oids(
        "Еженедельный детализированный отчет №865080870_250147786.zip") == {250147786}
    assert _report_oids("детализация_250147786 - 1.xlsx") == {250147786}
    assert _report_oids("a_29077.zip", "b_29077 - 1.xlsx") == {29077}
    # старый формат (нет маркера) и даты/годы oid не дают
    assert _report_oids("Детализация отчета 10098390.zip") == set()
    assert _report_oids("archive_2026_09.zip") == set()
    assert _report_oids("") == set()
