"""Провайдер Wildberries: реальные вызовы WB API + моки в testing_mode.

Реализованные методы соответствуют разделу «WB API» из проекта finance:
карточки, остатки, воронка продаж, цены, хранение, продажи (реализация),
детализация продаж (финансовый отчёт).
"""
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests

from app.config import settings
from app.providers.base import BaseProvider
from app.providers.errors import WbApiError

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
    "docTypeName": "Тип документа", "orderUid": "ID заказа",
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

    # ---------------------------------------------------------------- helpers
    def _headers(self, finance: bool = False) -> dict:
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

    def _session_post(self, url, payload, num_retries=6, finance: bool = False):
        for attempt in range(num_retries):
            resp = requests.post(url, headers=self._headers(finance), json=payload, timeout=60)
            if resp.status_code == 429:
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
        return pd.DataFrame(items)

    # --------------------------------------------------------- воронка продаж
    def get_sales_funnel(self, date_from, date_to) -> pd.DataFrame:
        """Воронка продаж WB (analytics/v3/sales-funnel/products)."""
        if self.testing:
            arts = self._mock_articles()
            days = self._mock_dates(date_from, date_to)
            rng = np.random.default_rng(3)
            rows = []
            for d in days:
                for i, a in enumerate(arts):
                    views = int(rng.integers(20, 300))
                    rows.append({
                        "nmID": 530000 + i,
                        "date": str(d),
                        "brandName": f"Бренд {chr(65 + i % 3)}",
                        "title": f"Товар {a}",
                        "openCardCount": int(views * rng.uniform(0.2, 0.5)),
                        "addToCartCount": int(views * rng.uniform(0.05, 0.2)),
                        "orderCount": int(views * rng.uniform(0.01, 0.06)),
                        "ordersCountAvg": round(rng.uniform(1, 3), 2),
                        "cancelCount": 0,
                        "viewsCount": views,
                        "avgPrice": round(rng.uniform(1000, 3000), 2),
                        "revenue": 0,
                    })
            return pd.DataFrame(rows)

        url = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
        chunk, offset, frames = 1000, 0, []
        while True:
            payload = {
                "selectedPeriod": {"start": str(date_from), "end": str(date_to)},
                "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
                "skipDeletedNm": False,
                "orderBy": {"field": "orderCount", "mode": "asc"},
                "limit": chunk, "offset": offset,
            }
            resp = self._session_post(url, payload)
            products = resp.json().get("data", {}).get("products", [])
            if not products:
                break
            frames.append(pd.json_normalize(products, errors="ignore"))
            offset += chunk
            if len(products) < chunk:
                break
            time.sleep(20)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

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
        while True:
            params = {"limit": limit, "offset": offset}
            resp = self._session_get(url, params=params)
            data = resp.json().get("data", {}) or {}
            items = data.get("listGoods", [])
            if not items:
                break
            goods += items
            offset += limit
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
        url = "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"
        resp = self._session_post(url, {"data": items})
        body = resp.json()
        task_id = None
        if isinstance(body, dict):
            data = body.get("data") or {}
            if isinstance(data, dict):
                task_id = data.get("uploadId")
        return {"body": body, "task_id": task_id}

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

    # --------------------------------------------- продажи (v5 реализация)
    def get_sales_realization(self, date_from, date_to) -> pd.DataFrame:
        """Отчёт о реализации WB (statistics-api/v5/supplier/reportDetailByPeriod)."""
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
        resp = self._session_get(
            "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod",
            params=params,
        )
        df = pd.DataFrame(resp.json())
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
            if "rrdId" in data[-1]:
                rrd_id = data[-1]["rrdId"]
            else:
                break
        if not all_rows:
            return pd.DataFrame()
        return pd.DataFrame(all_rows)