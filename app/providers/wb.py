"""Провайдер Wildberries: реальные вызовы WB API + моки в testing_mode.

Реализованные методы соответствуют разделу «WB API» из проекта finance:
карточки, остатки, воронка продаж, цены, хранение, продажи (реализация),
детализация продаж (финансовый отчёт).
"""
import time
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests

from app.config import settings
from app.providers.base import BaseProvider
from app.providers.errors import WbApiError
from app.services import progress


def _upload_error_detail(resp) -> Optional[str]:
    """Читает ошибку WB в ответе upload/task (400) → короткое русское описание.

    WB присылает в теле {"title": ..., "errors": [...]} или {"data":
    [{"nmID": ..., "errors": [...]}]}. Возвращает None, если тело не читается.
    """
    try:
        body = resp.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    parts: list = []
    title = body.get("title")
    if title:
        parts.append(str(title))
    if isinstance(body.get("errors"), list):
        parts.extend(str(e) for e in body["errors"] if e)
    data = body.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            errs = item.get("errors") or []
            if isinstance(errs, list):
                for e in errs:
                    parts.append(f"{item.get('nmID', '—')}: {e}")
    elif isinstance(data, dict):
        errs = data.get("errors") or []
        if isinstance(errs, list):
            parts.extend(str(e) for e in errs if e)
    if not parts:
        return None
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return "; ".join(out)[:500]

# Русские заголовки для отчёта детализации продаж (финансовый API)
DETAIL_RU_COLUMNS = {
    "reportId": "Номер отчёта", "dateFrom": "Начало периода", "dateTo": "Конец периода",
    "createDate": "Дата формирования", "rrdId": "ID строки", "giId": "ID поставки",
    "nmId": "Артикул WB", "brandName": "Бренд", "vendorCode": "Артикул продавца",
    "title": "Название товара", "techSize": "Размер", "sku": "Баркод",
    "docTypeName": "Тип документа", "quantity": "Кол-во", "retailPrice": "Цена розничная",
    "retailAmount": "Вайлдберриз реализовал Товар (Пр)", "commissionPercent": "Размер кВВ, %",
    "officeName": "Склад", "saleDt": "Дата продажи", "orderDt": "Дата заказа покупателем",
    "ppvzSalesCommission": "Вознаграждение с продаж до вычета услуг поверенного, без НДС",
    "forPay": "К перечислению Продавцу за реализованный Товар",
    "deliveryService": "Услуги по доставке товара покупателю",
    "paidStorage": "Хранение", "penalty": "Общая сумма штрафов",
    "deduction": "Удержания", "additionalPayment": "Корректировка ВВ",
    "rebillLogisticCost": "Возмещение издержек по перевозке/складским операциям",
    "srid": "Уникальный идентификатор записи (SRID)",
    "orderUid": "ID заказа",
}

# Русские заголовки файла WB «Детализация продаж» -> ключи финансового отчёта.
# Обратный DETAIL_RU_COLUMNS + алиасы реальных заголовков выгрузки ЛК.
DETAIL_UPLOAD_RENAME = {v: k for k, v in DETAIL_RU_COLUMNS.items()}
DETAIL_UPLOAD_RENAME.update({
    "Артикул поставщика": "vendorCode",
    "Баркод": "sku",
    "Размер": "techSize",
    "Дата продажи": "saleDt",
    "Дата заказа покупателем": "orderDt",
    "Кол-во": "quantity",
    "Цена розничная": "retailPrice",
    "Вайлдберриз реализовал Товар (Пр)": "retailAmount",
    "К перечислению Продавцу за реализованный Товар": "forPay",
    "Вознаграждение ВВ": "ppvzSalesCommission",
    "Вознаграждение с продаж до вычета услуг поверенного, без НДС": "ppvzSalesCommission",
    "Услуги по доставке товара покупателю": "deliveryService",
    "Услуги по доставке товара покупателю (квВВ)": "deliveryService",
    "Хранение (пр)": "paidStorage",
    "Штраф": "penalty",
    "Удержанный штраф": "deduction",
    "Возмещение издержек по перевозке/складским операциям": "rebillLogisticCost",
    "Srid": "srid", "SRID": "srid",
    "Тип документа": "docTypeName",
    "Id корзины заказа": "orderUid", "ID заказа": "orderUid",
    "Склад": "officeName",
})

# Русские заголовки для отчёта реализации (v5 / reportDetailByPeriod)
SALES_RU_COLUMNS = {
    "date": "Дата", "nmId": "Артикул WB", "barcode": "Баркод", "supplierArticle": "Артикул продавца",
    "category": "Категория", "subject": "Предмет", "brand": "Бренд", "name": "Название",
    "quantity": "Кол-во", "totalPrice": "Цена", "priceWithDisc": "Цена со скидкой",
    "forPay": "К перечислению", "deliveredAmount": "Доставлено", "returnedAmount": "Возврат",
}

# Русские заголовки для реального отчёта v5 (reportDetailByPeriod, 92 колонки)
V5_RU_COLUMNS = {
    "realizationreport_id": "Номер отчёта", "date_from": "Начало периода",
    "date_to": "Конец периода", "create_dt": "Дата формирования",
    "suppliercontract_code": "Контракт", "rrd_id": "ID строки", "gi_id": "ID поставки",
    "nm_id": "Артикул WB", "brand_name": "Бренд", "sa_name": "Артикул продавца",
    "ts_name": "Размер", "barcode": "Баркод", "doc_type_name": "Тип документа",
    "quantity": "Кол-во", "retail_price": "Цена розничная",
    "retail_amount": "ВБ реализовал (Пр)", "sale_percent": "Скидка, %",
    "commission_percent": "КВВ, %", "delivery_amount": "Доставлено",
    "return_amount": "Возврат", "delivery_rub": "Логистика, руб",
    "ppvz_sales_commission": "Вознаграждение с продаж",
    "ppvz_for_pay": "К перечислению продавцу", "ppvz_reward": "Возмещение выкупа",
    "acquiring_fee": "Эквайринг", "payment_processing": "Услуги эквайринга",
    "acquiring_percent": "Эквайринг, %", "acquiring_bank": "Банк эквайринга",
    "ppvz_vw": "ВВ без НДС", "ppvz_vw_nds": "ВВ с НДС",
    "office_name": "Склад", "sale_dt": "Дата продажи", "order_dt": "Дата заказа",
    "rr_dt": "Дата отчёта", "shk_id": "ШК", "penalty": "Штрафы",
    "additional_payment": "Корректировка ВВ", "storage_fee": "Хранение, руб",
    "deduction": "Удержания", "rebill_logistic_cost": "Возмещение логистики",
    "subject_name": "Предмет", "category": "Категория", "kiz": "КиЗ",
    "srid": "SRID", "order_uid": "ID заказа",
}


class WbProvider(BaseProvider):
    """Реальные данные Wildberries через API."""

    def __init__(self, testing_mode=None, credentials: Optional[dict] = None,
                 fail_fast_429=False):
        super().__init__(testing_mode)
        # Creds кабинета: {"standard":..,"finance":..,"finance2":..}.
        # Пустые -> фолбэк на settings (старое поведение; тесты/скрипты).
        self.creds = credentials or {}
        self.fail_fast_429 = fail_fast_429

    # ---------------------------------------------------------------- helpers
    def _headers(self, finance: bool = False, key: Optional[str] = None) -> dict:
        if key is None:
            key = (self.creds.get("finance") or self.creds.get("finance2")
                   if finance else self.creds.get("standard"))
        if key is None:
            key = settings.wb_finance_api_key if finance else settings.wb_api_key
        if not key:
            raise RuntimeError(
                ("WB_FINANCE_API_KEY" if finance else "WB_API_KEY")
                + " не задан в .env. "
                + ("Финансовый отчёт требует отдельного токена с правом «Финансы»."
                   if finance else "Реальные вызовы WB API невозможны.")
            )
        return {
            "Authorization": key,
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    def _session_get(self, url, params=None, num_retries=6, finance: bool = False):
        for attempt in range(num_retries):
            resp = requests.get(url, headers=self._headers(finance), params=params, timeout=60)
            if resp.status_code == 429:
                if self.fail_fast_429:
                    raise WbApiError(
                        "Превышен лимит запросов WB API: доступен 1 запрос в "
                        f"{self._retry_hint(resp)}. Попробуйте позже.",
                        status_code=429,
                    )
                self._wait_rate_limit(resp)
                continue
            resp.raise_for_status()
            return resp
        raise WbApiError(
            f"WB API вернул 429 (лимит запросов) после {num_retries} попыток: {url}",
            status_code=429,
        )

    def _session_post(self, url, payload, num_retries=6, finance: bool = False,
                      key: Optional[str] = None):
        for attempt in range(num_retries):
            resp = requests.post(url, headers=self._headers(finance, key),
                                 json=payload, timeout=60)
            if resp.status_code == 429:
                alt = self.creds.get("finance2")
                if alt is None:
                    alt = settings.wb_finance_api_key_2 if finance else None
                if alt:
                    resp = requests.post(
                        url,
                        headers={
                            "Authorization": alt,
                            "Content-Type": "application/json",
                            "accept": "application/json",
                        },
                        json=payload,
                        timeout=60,
                    )
                    if resp.status_code != 429:
                        resp.raise_for_status()
                        return resp
                if finance or self.fail_fast_429:
                    raise WbApiError(
                        "finance-api превысил лимит: доступен 1 запрос в "
                        f"{self._retry_hint(resp)}. Финансовый отчёт обновлять редко.",
                        status_code=429,
                    )
                self._wait_rate_limit(resp)
                continue
            resp.raise_for_status()
            return resp
        raise WbApiError(
            f"WB API вернул 429 (лимит запросов) после {num_retries} попыток: {url}",
            status_code=429,
        )

    @staticmethod
    def _retry_hint(resp) -> str:
        for header in ("X-Ratelimit-Retry", "X-Ratelimit-Reset", "Retry-After"):
            if header in resp.headers:
                try:
                    secs = int(float(resp.headers[header]))
                except (TypeError, ValueError):
                    continue
                if secs >= 3600:
                    return f"{secs / 3600:.1f} ч"
                if secs >= 60:
                    return f"{secs // 60} мин"
                return f"{secs} сек"
        return "некоторое время"

    def _wait_rate_limit(self, resp):
        """Ждёт ровно столько, сколько просит WB в заголовке X-Ratelimit-Retry."""
        secs = 15
        for header in ("X-Ratelimit-Retry", "X-Ratelimit-Reset", "Retry-After"):
            if header in resp.headers:
                try:
                    secs = int(float(resp.headers[header]))
                except (TypeError, ValueError):
                    pass
                break
        time.sleep(min(max(secs, 5), 600))

    # ------------------------------------------------------------ mock datasets
    def _mock_articles(self, n=20):
        return [str(1000 + i) for i in range(n)]

    def _mock_dates(self, date_from, date_to):
        start = pd.Timestamp(date_from).date()
        end = pd.Timestamp(date_to).date()
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    # ------------------------------------------------------- карточки товара
    def get_cards(self, text_search: str = None) -> pd.DataFrame:
        """Все карточки товара (content/v2/get/cards/list), cursor-пагинация."""
        if self.testing:
            arts = self._mock_articles()
            brands = ["Бренд A", "Бренд B", "Бренд C"]
            return pd.DataFrame({
                "nmID": [530000 + i for i in range(len(arts))],
                "vendorCode": arts,
                "brand": [brands[i % 3] for i in range(len(arts))],
                "title": [f"Товар {a}" for a in arts],
                "subject": [["Обувь", "Одежда", "Аксессуары"][i % 3] for i in range(len(arts))],
                "skus": [f"2{f'{i:010d}'}" for i in range(len(arts))],
                "price": np.random.default_rng(7).uniform(500, 4000, len(arts)).round(2),
                "discountedPrice": np.random.default_rng(8).uniform(400, 3500, len(arts)).round(2),
            })

        limit, updated_at, nm_id = 100, None, None
        cards = []
        progress.report(label="Карточки WB", stage="запрос API", unit="тов.")
        while True:
            payload = {
                "settings": {
                    "sort": {"ascending": True},
                    "cursor": {"limit": limit, "updatedAt": updated_at, "nmID": nm_id},
                    "filter": {"textSearch": text_search, "withPhoto": -1},
                }
            }
            resp = self._session_post("https://content-api.wildberries.ru/content/v2/get/cards/list", payload)
            data = resp.json()
            cursor = data.get("cursor", {})
            total = cursor.get("total", 0)
            if total == 0:
                break
            cards += data.get("cards", [])
            # cursor.total — общее число карточек: единственный случай, где
            # выкачка WB даёт честный процент сразу после первой страницы.
            progress.report(stage="стр. " + str(len(cards) // limit + 1),
                            done=len(cards), total=total)
            updated_at = cursor.get("updatedAt", updated_at)
            nm_id = cursor.get("nmID", nm_id)
            if len(data.get("cards", [])) < limit:
                break

        df = pd.json_normalize(
            cards, "sizes",
            ["vendorCode", "colors", "brand", "nmID", "dimensions", "characteristics", "title", "subject"],
            errors="ignore",
        )
        if "skus" in df.columns:
            df["skus"] = df["skus"].apply(lambda x: ", ".join(map(str, x)) if isinstance(x, list) else x)
        for col in ("sizes", "colors"):
            if col in df.columns:
                df = df.drop(columns=[col])
        return df

    # --------------------------------------------------------------- остатки
    def get_stock_report(self) -> pd.DataFrame:
        """Остатки на складах WB (analytics/v1/stocks-report/wb-warehouses)."""
        if self.testing:
            rng = np.random.default_rng(11)
            arts = self._mock_articles()
            warehouses = ["Коледино", "Подольск", "Тула"]
            rows = []
            for a in arts:
                for w in warehouses:
                    rows.append({
                        "warehouseName": w, "vendorCode": a, "nmId": 530000 + int(a) - 1000,
                        "barcode": f"2{f'{int(a)-1000:010d}'}",
                        "quantityFull": int(rng.integers(0, 300)),
                        "quantity": int(rng.integers(0, 250)),
                        "inWayToClient": int(rng.integers(0, 40)),
                        "inWayFromClient": int(rng.integers(0, 10)),
                    })
            return pd.DataFrame(rows)

        resp = self._session_post(
            "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
            {"limit": 250000, "offset": 0},
        )
        items = resp.json().get("data", {}).get("items", [])
        progress.report(label="Остатки WB", done=len(items))
        return pd.DataFrame(items)

    # --------------------------------------------------------- воронка продаж
    def get_sales_funnel(self, date_from, date_to) -> pd.DataFrame:
        """Воронка продаж WB (analytics/v3/sales-funnel/products).

        Токен подбирается автоматически из трёх WB-ключей (см. _pick_funnel_key):
        права на этот отчёт могут быть только у одного из них.
        """
        if self.testing:
            arts = self._mock_articles()
            days = self._mock_dates(date_from, date_to)
            rng = np.random.default_rng(3)
            rows = []
            for d in days:
                for i, a in enumerate(arts):
                    views = int(rng.integers(20, 300))
                    adds = int(views * rng.uniform(0.05, 0.2))
                    orders = int(views * rng.uniform(0.01, 0.06))
                    buyouts = int(orders * rng.uniform(0.5, 0.9))
                    price = round(rng.uniform(1000, 3000), 2)
                    p_views = int(views * 0.7)
                    p_adds = int(adds * 0.7)
                    p_orders = int(orders * 0.7)
                    p_buyouts = int(buyouts * 0.7)
                    stat = {
                        "period": {"start": str(date_from), "end": str(date_to)},
                        "openCount": views, "cartCount": adds, "orderCount": orders,
                        "orderSum": round(orders * price, 2),
                        "cancelCount": 0, "cancelSum": 0,
                        "buyoutCount": buyouts, "buyoutSum": round(buyouts * price, 2),
                        "avgPrice": price,
                        "avgOrdersCountPerDay": round(rng.uniform(0.1, 2), 2),
                        "shareOrderPercent": round(rng.uniform(0, 20), 2),
                        "addToWishlist": int(views * rng.uniform(0.01, 0.05)),
                        "timeToReady": {"days": 1, "hours": int(rng.integers(2, 12)),
                                        "mins": int(rng.integers(0, 59))},
                        "localizationPercent": 100,
                        "conversions": {
                            "addToCartPercent": round(100 * adds / views, 2) if views else 0,
                            "cartToOrderPercent": round(100 * orders / adds, 2) if adds else 0,
                            "buyoutPercent": round(100 * buyouts / orders, 2) if orders else 0,
                        },
                        "wbClub": {
                            "orderCount": 0, "orderSum": 0, "buyoutCount": 0,
                            "buyoutSum": 0, "cancelCount": 0, "cancelSum": 0,
                            "avgPrice": 0, "buyoutPercent": 0, "avgOrderCountPerDay": 0,
                        },
                    }
                    past_ = dict(stat)
                    past_["period"]["start"] = str(date_from - timedelta(days=30))
                    past_["period"]["end"] = str(date_to - timedelta(days=30))
                    past_["openCount"] = p_views
                    past_["cartCount"] = p_adds
                    past_["orderCount"] = p_orders
                    past_["orderSum"] = round(p_orders * price, 2)
                    past_["buyoutCount"] = p_buyouts
                    past_["buyoutSum"] = round(p_buyouts * price, 2)
                    def _dyn(a0, b0):
                        return round(100 * (a0 - b0) / b0, 2) if b0 else 0
                    comp = {
                        "openCountDynamic": _dyn(views, p_views),
                        "cartCountDynamic": _dyn(adds, p_adds),
                        "orderCountDynamic": _dyn(orders, p_orders),
                        "orderSumDynamic": _dyn(round(orders * price, 2), round(p_orders * price, 2)),
                        "buyoutCountDynamic": _dyn(buyouts, p_buyouts),
                        "buyoutSumDynamic": _dyn(round(buyouts * price, 2), round(p_buyouts * price, 2)),
                        "cancelCountDynamic": 0, "cancelSumDynamic": 0,
                        "avgOrdersCountPerDayDynamic": _dyn(stat["avgOrdersCountPerDay"], past_["avgOrdersCountPerDay"]),
                        "avgPriceDynamic": 0,
                        "shareOrderPercentDynamic": _dyn(stat["shareOrderPercent"], past_["shareOrderPercent"]),
                        "addToWishlistDynamic": _dyn(stat["addToWishlist"], past_["addToWishlist"]),
                        "timeToReadyDynamic": {"days": 0, "hours": 0, "mins": 0},
                        "localizationPercentDynamic": 0,
                        "conversions": {
                            "addToCartPercent": _dyn(stat["conversions"]["addToCartPercent"], past_["conversions"]["addToCartPercent"]),
                            "cartToOrderPercent": _dyn(stat["conversions"]["cartToOrderPercent"], past_["conversions"]["cartToOrderPercent"]),
                            "buyoutPercent": _dyn(stat["conversions"]["buyoutPercent"], past_["conversions"]["buyoutPercent"]),
                        },
                        "wbClubDynamic": {"orderCount": 0, "orderSum": 0, "buyoutCount": 0,
                                          "buyoutSum": 0, "cancelCount": 0, "cancelSum": 0,
                                          "avgPrice": 0, "buyoutPercent": 0, "avgOrderCountPerDay": 0},
                    }
                    row = {
                        "product.nmID": 530000 + i,
                        "product.vendorCode": a,
                        "product.brandName": f"Бренд {chr(65 + i % 3)}",
                        "product.title": f"Товар {a}",
                        "product.subjectId": 100 + i % 5,
                        "product.subjectName": "Куртки",
                        "product.tags": ["новинка", "sale"] if i % 2 else [],
                        "product.productRating": round(rng.uniform(6, 10), 1),
                        "product.feedbackRating": round(rng.uniform(3.5, 5.0), 2),
                        "product.stocks.wb": int(rng.integers(0, 15)),
                        "product.stocks.mp": int(rng.integers(0, 8)),
                        "product.stocks.balanceSum": round(rng.uniform(0, 40000), 2),
                        "statistic.selected.period.start": str(date_from),
                        "statistic.selected.period.end": str(date_to),
                        "statistic.selected.openCount": views,
                        "statistic.selected.cartCount": adds,
                        "statistic.selected.orderCount": orders,
                        "statistic.selected.orderSum": round(orders * price, 2),
                        "statistic.selected.cancelCount": 0,
                        "statistic.selected.cancelSum": 0,
                        "statistic.selected.buyoutCount": buyouts,
                        "statistic.selected.buyoutSum": round(buyouts * price, 2),
                        "statistic.selected.avgPrice": price,
                        "statistic.selected.avgOrdersCountPerDay": stat["avgOrdersCountPerDay"],
                        "statistic.selected.shareOrderPercent": stat["shareOrderPercent"],
                        "statistic.selected.addToWishlist": stat["addToWishlist"],
                        "statistic.selected.timeToReady.days": 1,
                        "statistic.selected.timeToReady.hours": stat["timeToReady"]["hours"],
                        "statistic.selected.timeToReady.mins": stat["timeToReady"]["mins"],
                        "statistic.selected.localizationPercent": 100,
                        "statistic.selected.conversions.addToCartPercent":
                            stat["conversions"]["addToCartPercent"],
                        "statistic.selected.conversions.cartToOrderPercent":
                            stat["conversions"]["cartToOrderPercent"],
                        "statistic.selected.conversions.buyoutPercent":
                            stat["conversions"]["buyoutPercent"],
                        "product": {"nmID": 530000 + i, "vendorCode": a, "brandName": f"Бренд {chr(65 + i % 3)}",
                                    "title": f"Товар {a}", "subjectId": 100 + i % 5,
                                    "subjectName": "Куртки",
                                    "tags": ["новинка", "sale"] if i % 2 else []},
                        "statistic": {"selected": stat, "past": past_, "comparison": comp},
                    }
                    rows.append(row)
            df = pd.DataFrame(rows)
            df["_raw"] = rows
            return df

        url = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
        key = self._pick_funnel_key(url)
        chunk, offset, frames, raw_rows = 1000, 0, [], []
        progress.report(label="Воронка WB", stage="запрос API", unit="стр.")
        while True:
            payload = {
                "selectedPeriod": {"start": str(date_from), "end": str(date_to)},
                "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
                "skipDeletedNm": False,
                "orderBy": {"field": "orderCount", "mode": "asc"},
                "limit": chunk, "offset": offset,
            }
            resp = self._session_post(url, payload, key=key)
            products = resp.json().get("data", {}).get("products", [])
            if not products:
                break
            frames.append(pd.json_normalize(products, errors="ignore"))
            raw_rows.extend(products)
            offset += chunk
            # общий объём неизвестен — показываем страницы и строки без %
            progress.report(stage=f"стр. {len(frames)}", done=len(raw_rows))
            if len(products) < chunk:
                break
            time.sleep(20)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        df["_raw"] = raw_rows
        return df

    def _pick_funnel_key(self, url: str) -> str:
        """Первый непустой WB-токен с доступом к отчёту воронки (не 403).

        Порядок: WB_FINANCE_API_KEY_2, WB_API_KEY, WB_FINANCE_API_KEY.
        Права на analytics/v3/sales-funnel могут быть только у одного из них;
        квитируем 403 (нет прав) и переходим к следующему.
        """
        candidates = [self.creds.get("finance2"), self.creds.get("standard"),
                      self.creds.get("finance"),
                      settings.wb_finance_api_key_2, settings.wb_api_key,
                      settings.wb_finance_api_key]
        payload = {
            "selectedPeriod": {"start": "2026-01-01", "end": "2026-01-02"},
            "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
            "skipDeletedNm": False,
            "orderBy": {"field": "orderCount", "mode": "asc"},
            "limit": 1, "offset": 0,
        }
        last_err = None
        for k in candidates:
            if not k:
                continue
            try:
                r = requests.post(url, headers=self._headers(key=k), json=payload, timeout=60)
            except requests.RequestException as e:  # noqa: BLE001
                last_err = e
                continue
            if r.status_code == 403:
                continue
            if r.status_code == 429:
                self._wait_rate_limit(r)
                return k
            if r.status_code >= 400:
                last_err = WbApiError(
                    f"WB API: {r.status_code} {r.text[:200]}", status_code=r.status_code)
                continue
            return k
        detail = str(last_err)[:200] if last_err else "все ключи вернули 403 (нет прав)"
        raise WbApiError(
            "Ни один из WB-токенов (WB_API_KEY / WB_FINANCE_API_KEY / "
            f"WB_FINANCE_API_KEY_2) не дал доступ к отчёту воронки продаж: {detail}",
            status_code=403,
        )

    # ------------------------------------------------------------------ цены
    def get_prices(self) -> pd.DataFrame:
        """Цены и скидки WB (discounts-prices-api/v2/list/goods/filter)."""
        if self.testing:
            arts = self._mock_articles()
            rng_price = np.random.default_rng(9)
            rng_disc = np.random.default_rng(10)
            price = rng_price.uniform(500, 4000, len(arts)).round(2)
            discounted = (price * (1 - rng_disc.uniform(0, 0.4, len(arts)))).round(2)
            return pd.DataFrame({
                "nmID": [530000 + i for i in range(len(arts))],
                "vendorCode": arts,
                "price": price,
                "discountedPrice": discounted,
                "discount": ((price - discounted) / price * 100).round(1),
            })

        url = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"
        limit, offset, goods = 1000, 0, []
        progress.report(label="Цены WB", stage="запрос API", unit="поз.")
        while True:
            params = {"limit": limit, "offset": offset}
            resp = self._session_get(url, params=params)
            data = resp.json().get("data", {}) or {}
            items = data.get("listGoods", [])
            if not items:
                break
            goods += items
            offset += limit
            progress.report(stage=f"стр. {offset // limit}", done=len(goods))
            if len(items) < limit:
                break
            if offset > 0:
                time.sleep(0.7)

        if not goods:
            return pd.DataFrame()
        df = pd.json_normalize(goods, "sizes", ["vendorCode", "nmID"], errors="ignore")
        for col in ("price", "discountedPrice"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        if "price" in df.columns:
            df["discount"] = 0.0
            mask = df["price"] > 0
            df.loc[mask, "discount"] = (
                (df.loc[mask, "price"] - df.loc[mask, "discountedPrice"]) / df.loc[mask, "price"] * 100
            )
        return df

    def update_prices(self, items) -> dict:
        """Обновление цен/скидок WB (discounts-prices-api/v2/upload/task).

        items: список {"nmID": int, "price": float, "discount": float}.
        В тестовом режиме результат сохраняется в self.applied_prices.
        """
        if self.testing:
            self.applied_prices = items
            return {"uploadId": "mock-123", "task_id": "mock-123"}
        if not items:
            return {"body": {"data": {}}, "task_id": None}
        url = "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"
        try:
            resp = self._session_post(url, {"data": items})
        except requests.HTTPError as exc:  # noqa: BLE001
            detail = _upload_error_detail(exc.response) if exc.response is not None else None
            if detail:
                raise WbApiError(
                    f"WB API отклонил задачу изменения цен: {detail}",
                    status_code=getattr(exc.response, "status_code", 400),
                ) from exc
            raise
        body = resp.json()
        task_id = None
        if isinstance(body, dict):
            data = body.get("data") or {}
            if isinstance(data, dict):
                task_id = data.get("uploadId")
        return {"body": body, "task_id": task_id}

    # -------------------------------------------------------- акции (календарь)
    # Документация: dp-calendar-api /api/v1/calendar/*, токен категории
    # «Цены и скидки» (тот же WB_API_KEY). Лимит: 10 запросов / 6 сек,
    # всплеск 5 → между запросами делаем паузу 0.7с.
    _PROMO_URL = "https://dp-calendar-api.wildberries.ru/api/v1/calendar"

    @staticmethod
    def _promo_window(start_date, end_date) -> str:
        """Дата в формате WB: YYYY-MM-DDTHH:MM:SSZ (UTC-полдень)."""
        from datetime import timezone

        def _iso(d, hour=12):
            dt = d if isinstance(d, datetime) else datetime.combine(d, datetime.min.time())
            return dt.replace(tzinfo=timezone.utc, hour=hour, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        return _iso(pd.Timestamp(start_date).date()), _iso(pd.Timestamp(end_date).date())

    @staticmethod
    def _table_page(data, key):
        """Список записей из ответа акций: явный ключ, любой list-ключ или сам data.

        Схемы ответов календаря акций менялись; для `nomenclatures` WB не
        документирует обёртку, поэтому ищем первый list в data (иначе None).
        """
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return None
        if key is not None and isinstance(data.get(key), list):
            return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
        return None

    def _promo_paginate(self, path: str, key: str, params: dict, chunk: int = 1000):
        """GET-страницы календаря акций с паузой под rate-limit. Возвращает list."""
        items, offset = [], 0
        progress.report(label="Акции WB", stage="запрос API", unit="стр.")
        while True:
            p = dict(params)
            p["limit"] = chunk
            p["offset"] = offset
            resp = self._session_get(self._PROMO_URL + path, params=p)
            page = self._table_page(resp.json().get("data"), key)
            if not isinstance(page, list) or not page:
                break
            items += page
            offset += len(page)
            progress.report(stage=f"стр. {offset // chunk + 1}", done=len(items))
            if len(page) < chunk:
                break
            time.sleep(0.7)
        return items

    def _mock_promotions(self):
        today = date.today()
        return [
            {
                "id": 1, "name": "ХИТЫ ГОДА",
                "startDateTime": (today - timedelta(days=2)).isoformat() + "T12:00:00Z",
                "endDateTime": (today + timedelta(days=5)).isoformat() + "T23:59:59Z",
                "type": "auto",
            },
            {
                "id": 2, "name": "Скидки выходного дня",
                "startDateTime": (today - timedelta(days=1)).isoformat() + "T00:00:00Z",
                "endDateTime": (today + timedelta(days=30)).isoformat() + "T23:59:59Z",
                "type": "auto",
            },
            {
                "id": 3, "name": "Распродажа категории",
                "startDateTime": (today + timedelta(days=3)).isoformat() + "T00:00:00Z",
                "endDateTime": (today + timedelta(days=14)).isoformat() + "T23:59:59Z",
                "type": "regular",
            },
        ]

    def _mock_promotion_details(self, promo_ids):
        base = {p["id"]: p for p in self._mock_promotions()}
        rng = np.random.default_rng(41)
        out = []
        for pid in promo_ids:
            promo = base.get(int(pid), base.get(1))
            in_total = int(rng.integers(5, 40))
            not_total = int(rng.integers(0, 20))
            in_left = int(in_total * rng.uniform(0.5, 0.9))
            not_left = int(not_total * rng.uniform(0.3, 0.9))
            out.append({
                "id": promo["id"], "name": promo["name"],
                "description": "Мок-акция для тестов автопилота",
                "advantages": ["Плашка", "Баннер"],
                "startDateTime": promo["startDateTime"], "endDateTime": promo["endDateTime"],
                "inPromoActionLeftovers": in_left, "inPromoActionTotal": in_total,
                "notInPromoActionLeftovers": not_left, "notInPromoActionTotal": not_total,
                "participationPercentage": round(in_total / max(in_total + not_total, 1) * 100, 1),
                "type": promo["type"], "exceptionProductsCount": int(rng.integers(0, 5)),
                "ranging": [
                    {"condition": "productsInPromotion", "participationRate": 10, "boost": 7},
                    {"condition": "calculateProducts", "participationRate": 20, "boost": 17},
                    {"condition": "allProducts", "participationRate": 35, "boost": 30},
                ],
            })
        return out

    def _mock_promotion_nomenclatures(self, promo_id, in_action):
        arts = self._mock_articles()
        rng = np.random.default_rng(42 + int(promo_id) % 100)
        rows = []
        for i, a in enumerate(arts):
            in_a = bool(rng.integers(0, 2)) == bool(in_action)
            rows.append({
                "nmID": 530000 + i, "vendorCode": a, "brandName": f"Бренд {chr(65 + i % 3)}",
                "title": f"Товар {a}", "inAction": in_a,
                "discount": round(float(rng.uniform(5.0, 25.0)), 1) if in_a else 0.0,
                "price": round(float(rng.uniform(500, 4000)), 2),
            })
        return rows

    def get_promotions(self, start_date=None, end_date=None, all_promo: bool = False) -> pd.DataFrame:
        """Список акций WB (/api/v1/calendar/promotions).

        all_promo=False — акции, доступные для участия; True — все акции.
        Возвращает колонки: id, name, startDateTime, endDateTime, type.
        """
        start = start_date or date.today() - timedelta(days=30)
        end = end_date or date.today() + timedelta(days=30)
        if self.testing:
            return pd.DataFrame(self._mock_promotions())
        from_dt, to_dt = self._promo_window(start, end)
        items = self._promo_paginate(
            "/promotions", "promotions",
            {"startDateTime": from_dt, "endDateTime": to_dt, "allPromo": str(bool(all_promo)).lower()},
        )
        if not items:
            return pd.DataFrame()
        return pd.DataFrame(items)

    def get_promotion_details(self, promo_ids) -> pd.DataFrame:
        """Детальная информация об акциях (/api/v1/calendar/promotions/details).

        Батчами по ≤100 ID, колонки: id, name, description, advantages,
        startDateTime, endDateTime, inPromoActionLeftovers/Total,
        notInPromoActionLeftovers/Total, participationPercentage, type,
        exceptionProductsCount, ranging (JSON-строка).
        """
        ids = [int(p) for p in promo_ids if p is not None and str(p).strip().lstrip("-").isdigit()]
        if not ids:
            return pd.DataFrame()
        if self.testing:
            return pd.DataFrame(self._mock_promotion_details(ids))
        items = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            resp = self._session_get(
                self._PROMO_URL + "/promotions/details",
                params=[("promotionIDs", str(p)) for p in chunk],
            )
            data = resp.json().get("data") or {}
            page = data.get("promotions") if isinstance(data, dict) else data
            if isinstance(page, list):
                items += page
            if i + 100 < len(ids):
                time.sleep(0.7)
        if not items:
            return pd.DataFrame()
        df = pd.json_normalize(items, errors="ignore")
        if "ranging" in df.columns:
            df["ranging"] = df["ranging"].apply(
                lambda v: (__import__("json").dumps(v, ensure_ascii=False, default=str)
                           if isinstance(v, list) else "")
            )
        return df

    def get_promotion_nomenclatures(self, promo_id, in_action: bool = False) -> pd.DataFrame:
        """Товары для участия в акции (/api/v1/calendar/promotions/nomenclatures).

        Неприменим для автоакций. in_action=True — уже участвуют, False — кандидаты.
        Возвращаемые колонки зависят от WB; ключи нормализуются к нижнему регистру.
        """
        if promo_id is None:
            return pd.DataFrame()
        if self.testing:
            return pd.DataFrame(self._mock_promotion_nomenclatures(promo_id, in_action))
        items = self._promo_paginate(
            "/promotions/nomenclatures", None,
            {"promotionID": int(promo_id), "inAction": str(bool(in_action)).lower()},
        )
        if not items:
            return pd.DataFrame()
        df = pd.json_normalize(items, errors="ignore")
        df.columns = [str(c).strip().lower() for c in df.columns]
        return df

    def get_min_prices(self, nm_ids) -> dict:
        """Минимальные витринные цены WB (публичный API v1/info/price).

        Возвращает {nmID: минимальная цена в руб}. Публичный эндпоинт — без
        ключа; на недоступность/частичный ответ тихо возвращает то, что есть.
        """
        ids = [str(n) for n in nm_ids if n is not None and str(n).strip()]
        if not ids:
            return {}
        if self.testing:
            rng = np.random.default_rng(77)
            return {nm: round(float(rng.uniform(500, 1200)), 2) for nm in ids}
        out: dict = {}
        url = "https://public-dc0.wildberries.ru/api/v1/info/price"
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            for attempt in range(4):
                try:
                    resp = requests.get(
                        url, params={"quantity": 1, "nm": ",".join(chunk)}, timeout=30
                    )
                except requests.RequestException:
                    if attempt == 3:
                        return out
                    time.sleep(2)
                    continue
                if resp.status_code == 429:
                    self._wait_rate_limit(resp)
                    continue
                if resp.status_code >= 400:
                    break
                try:
                    for item in resp.json():
                        nm = str(item.get("nmID", ""))
                        if not nm:
                            continue
                        lim = (item.get("priceLimits") or {}).get("minPrice")
                        if lim:
                            out[nm] = round(max(int(lim), 0) / 100, 2)
                except (ValueError, TypeError):
                    break
                break
        return out

    # ---------------------------------------------------------------- хранение
    def get_storage_cost(self, number_last_days: int = 7, is_mean: bool = True) -> pd.DataFrame:
        """Стоимость хранения (seller-analytics-api/v1/paid_storage, асинхронный отчёт)."""
        if self.testing:
            arts = self._mock_articles()
            warehouses = ["Коледино", "Подольск"]
            rng = np.random.default_rng(5)
            rows = []
            for a in arts:
                for w in warehouses:
                    barcodes = int(rng.integers(10, 80))
                    rows.append({
                        "vendorCode": a, "nmId": 530000 + int(a) - 1000, "warehouse": w,
                        "warehousePrice": round(rng.uniform(500, 9000), 2),
                        "barcodesCount": barcodes,
                        "volume": round(rng.uniform(0.1, 0.6), 3),
                    })
            df = pd.DataFrame(rows)
            if is_mean:
                df["storagePricePerBarcode"] = df["warehousePrice"] / df["barcodesCount"]
            return df

        date_from = (datetime.now() - timedelta(days=number_last_days)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")

        resp = self._session_get(
            "https://seller-analytics-api.wildberries.ru/api/v1/paid_storage",
            params={"dateFrom": date_from, "dateTo": date_to},
        )
        task_id = resp.json()["data"]["taskId"]

        progress.report(label="Хранение WB", stage="формируется отчёт…")
        while True:
            status_resp = self._session_get(
                f"https://seller-analytics-api.wildberries.ru/api/v1/paid_storage/tasks/{task_id}/status"
            )
            status = status_resp.json()["data"]["status"]
            if status == "done":
                break
            if status == "error":
                raise RuntimeError("WB: не удалось сформировать отчёт по хранению")
            time.sleep(10)
        progress.report(stage="скачивание…")

        download_resp = self._session_get(
            f"https://seller-analytics-api.wildberries.ru/api/v1/paid_storage/tasks/{task_id}/download"
        )
        df = pd.DataFrame(download_resp.json())
        if is_mean and {"vendorCode", "warehousePrice", "barcodesCount"} <= set(df.columns):
            df = df.groupby("vendorCode").agg(
                {"warehousePrice": "mean", "barcodesCount": "mean", "volume": "mean", "nmId": "first"}
            ).reset_index()
            df["storagePricePerBarcode"] = df["warehousePrice"] / df["barcodesCount"]
        return df

    # --------------------------------------------- продажи (v5 были отключены WB 15.07.2026)
    def get_sales_realization(self, date_from, date_to) -> pd.DataFrame:
        """DEPRECATED: statistics-api v5/supplier/reportDetailByPeriod отключён
        WB (release-notes#498). Продажи WB теперь пересчитываются из строк
        детализации (finance-API) — см. sync.rebuild_sales_from_detail/sales_from_detail_df.
        Метод оставлен только для обратной совместимости (моки тестов, старые скрипты)."""
        if self.testing:
            arts = self._mock_articles()
            days = self._mock_dates(date_from, date_to)
            rng = np.random.default_rng(14)
            rows = []
            for d in days:
                for i, a in enumerate(arts):
                    qty = int(rng.integers(0, 6))
                    if qty == 0:
                        continue
                    price = round(rng.uniform(800, 3500), 2)
                    rows.append({
                        "date": str(d), "nmId": 530000 + i, "barcode": f"2{f'{i:010d}'}",
                        "supplierArticle": a, "brand": f"Бренд {chr(65 + i % 3)}",
                        "quantity": qty, "totalPrice": round(price * qty, 2),
                        "priceWithDisc": round(price * qty * 0.82, 2),
                        "forPay": round(price * qty * 0.74, 2),
                        "returnedAmount": 0,
                    })
            return pd.DataFrame(rows)

        params = {
            "dateFrom": str(date_from),
            "dateTo": str(date_to),
            "rrdid": 0,
            "limit": 100000,
        }
        # один запрос на весь период: отдельной страницы нет, но этап «запрос
        # отчёта» длительный — держим его в строке состояния
        progress.report(label="Продажи WB", stage="запрос отчёта…", unit="стр.")
        resp = self._session_get(
            "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod",
            params=params,
        )
        df = pd.DataFrame(resp.json())
        progress.report(stage="получено", done=len(df))
        if "supplierArticle" in df.columns and "vendorCode" in df.columns:
            df["supplierArticle"] = df["supplierArticle"].fillna(df["vendorCode"])
        return df

    # -------------------------------------------- детализация продаж (финансы)
    def get_sales_detail(self, date_from, date_to) -> pd.DataFrame:
        """Детализация продаж WB (finance-api/v1/sales-reports/detailed), пагинация по rrdId."""
        if self.testing:
            arts = self._mock_articles()
            days = self._mock_dates(date_from, date_to)
            rng = np.random.default_rng(21)
            rows = []
            for d in days:
                for i, a in enumerate(arts):
                    qty = int(rng.integers(0, 6))
                    if qty == 0:
                        continue
                    price = round(rng.uniform(800, 3500), 2)
                    rows.append({
                        "dateFrom": str(date_from), "dateTo": str(date_to),
                        "rrdId": len(rows) + 1, "nmId": 530000 + i, "vendorCode": a,
                        "brandName": f"Бренд {chr(65 + i % 3)}", "title": f"Товар {a}",
                        "techSize": "XS", "sku": f"2{f'{i:010d}'}",
                        "quantity": qty, "retailPrice": price,
                        "retailAmount": round(price * qty, 2),
                        "ppvzSalesCommission": round(-price * qty * 0.15, 2),
                        "forPay": round(price * qty * 0.74, 2),
                        "deliveryService": -200.0, "paidStorage": -50.0,
                        "saleDt": str(d), "orderDt": str(d), "officeName": "Коледино",
                    })
            return pd.DataFrame(rows)

        url = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
        all_rows, rrd_id = [], 0
        progress.report(label="Детализация WB", stage="запрос API", unit="стр.")
        while True:
            payload = {"dateFrom": str(date_from), "dateTo": str(date_to),
                       "limit": 100000, "rrdId": rrd_id}
            resp = self._session_post(url, payload, finance=True)
            if resp.status_code == 204:
                break
            data = resp.json()
            if not data:
                break
            all_rows += data
            progress.report(stage=f"стр. {len(all_rows) // 100000 + 1}",
                            done=len(all_rows))
            if "rrdId" in data[-1]:
                rrd_id = data[-1]["rrdId"]
            else:
                break
        if not all_rows:
            return pd.DataFrame()
        return pd.DataFrame(all_rows)