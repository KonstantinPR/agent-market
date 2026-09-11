"""Провайдер Ozon: реальные вызовы Ozon API + моки.

Проверено по live-ключам пользователя (Client-Id 164497) 2026-09:
- карточки: асинхронный отчёт /v1/report/products/create -> poll -> download CSV
- остатки:  /v2/analytics/stock_on_warehouses
- цены:     /v5/product/info/prices (visibility=VISIBLE)
- продажи:  /v2/finance/realization (помесячно; v3 finance/transaction/list на этом
            аккаунте возвращает "obsolete method cannot be used")
- движение средств: /v1/finance/cash-flow-statement/list
"""
import io
import time
from datetime import date

import numpy as np
import pandas as pd
import requests

from app.config import settings
from app.providers.base import BaseProvider
from app.providers.errors import OzonApiError

OZON_API = "https://api-seller.ozon.ru"
OZON_RU_COLUMNS = {
    "date": "Дата", "offer_id": "Артикул (offer_id)", "name": "Наименование",
    "barcode": "Штрихкод", "sku": "SKU", "quantity": "Кол-во",
    "seller_price": "Цена продажи", "amount": "Сумма",
    "commission_ratio": "Доля комиссии", "commission": "Комиссия",
    "income": "К перечислению", "returns_qty": "Возвраты, шт",
    "delivery_amount": "Доставка, сумма", "delivery_standard_fee": "Доставка, комиссия",
    "delivery_total": "Доставка, итог", "return_total": "Возврат, итог",
}


class OzonProvider(BaseProvider):
    """Данные Ozon (FBO/FBS) через API."""

    # ---------------------------------------------------------------- helpers
    def _headers(self) -> dict:
        if not settings.ozon_client_id or not settings.ozon_api_key:
            raise RuntimeError("OZON_CLIENT_ID / OZON_API_KEY не заданы в .env. Реальные вызовы Ozon API невозможны.")
        return {
            "Client-Id": str(settings.ozon_client_id),
            "Api-Key": settings.ozon_api_key,
            "Content-Type": "application/json",
        }

    def _post(self, url, payload, num_retries=4):
        for attempt in range(num_retries):
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=90)
            if resp.status_code == 429:
                if self.fail_fast_429:
                    raise OzonApiError(
                        "Превышен лимит запросов Ozon API, попробуйте позже.",
                        status_code=429,
                    )
                time.sleep(5 + attempt * 10)
                continue
            resp.raise_for_status()
            return resp
        raise OzonApiError(
            f"Ozon API вернул 429 (лимит запросов) после {num_retries} попыток: {url}",
            status_code=429,
        )

    # ------------------------------------------------------------ mock helpers
    def _mock_articles(self, n=20):
        return [f"JBG-{1000 + i}" for i in range(n)]

    # --------------------------------------------------------------- карточки
    def get_cards(self) -> pd.DataFrame:
        """Все карточки товара: отчёт /v1/report/products/create -> download CSV."""
        if self.testing:
            arts = self._mock_articles()
            return pd.DataFrame({
                "Ozon Product ID": [95000000 + i for i in range(len(arts))],
                "SKU": [f"1{f'{i:010d}'}" for i in range(len(arts))],
                "Offer ID": arts,
                "Name": [f"Товар {a}" for a in arts],
                "Barcode": [f"4{f'{i:010d}'}" for i in range(len(arts))],
                "Category": ["Обувь", "Одежда", "Аксессуары"][:1] * len(arts),
            })

        resp = self._post(f"{OZON_API}/v1/report/products/create", {
            "language": "DEFAULT", "offer_id": [], "search": "", "sku": [], "visibility": "ALL",
        })
        code = resp.json()["result"]["code"]

        for attempt in range(25):
            time.sleep(20)
            info = self._post(f"{OZON_API}/v1/report/info", {"code": code})
            result = info.json()["result"]
            status = result.get("status")
            if status == "success":
                content = requests.get(result["file"], timeout=300).content
                return pd.read_csv(io.BytesIO(content), sep=";", dtype=str)
            if status not in ("processing", "waiting"):
                raise RuntimeError(f"Ozon: отчёт по карточкам завершился статусом {status}")
        raise RuntimeError("Ozon: отчёт по карточкам не сформировался за отведённое время")

    # ---------------------------------------------------------------- остатки
    def get_stock(self) -> pd.DataFrame:
        """Остатки на складах: /v2/analytics/stock_on_warehouses."""
        if self.testing:
            rng = np.random.default_rng(12)
            arts = self._mock_articles()
            rows = []
            for a in arts:
                for w in ("FBO Ozon", "FBS Ozon"):
                    rows.append({
                        "sku": str(int(rng.integers(10000000, 99999999))),
                        "item_code": a, "item_name": f"Товар {a}",
                        "warehouse_name": w,
                        "free_to_sell_amount": int(rng.integers(0, 200)),
                        "reserved_amount": int(rng.integers(0, 30)),
                        "promised_amount": int(rng.integers(0, 20)),
                        "idc": round(float(rng.uniform(0, 1)), 3),
                    })
            return pd.DataFrame(rows)

        all_rows, offset = [], 0
        while True:
            resp = self._post(f"{OZON_API}/v2/analytics/stock_on_warehouses",
                              {"limit": 1000, "offset": offset, "warehouse_type": "ALL"})
            rows = resp.json().get("result", {}).get("rows", [])
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000
        df = pd.DataFrame(all_rows)
        if not df.empty and "sku" in df.columns:
            df["sku"] = df["sku"].astype(str)
        return df

    # ------------------------------------------------------------------- цены
    def get_prices(self) -> pd.DataFrame:
        """Цены: /v5/product/info/prices (visibility=VISIBLE), cursor-пагинация."""
        if self.testing:
            arts = self._mock_articles()
            rng_p = np.random.default_rng(13)
            price = rng_p.uniform(500, 4000, len(arts)).round(2)
            old = (price * 1.2).round(2)
            return pd.DataFrame({
                "offer_id": arts, "product_id": [95000000 + i for i in range(len(arts))],
                "price_price": price, "price_old_price": old,
                "price_min_price": (old * 0.7).round(2),
                "price_auto_action_enabled": False,
                "price_currency_code": "RUB",
            })

        items, cursor = [], ""
        while True:
            resp = self._post(f"{OZON_API}/v5/product/info/prices", {
                "cursor": cursor,
                "filter": {"offer_id": [], "product_id": [], "visibility": "VISIBLE"},
                "limit": 1000,
            })
            data = resp.json()
            page = data.get("items", [])
            items += page
            cursor = data.get("cursor", "") or ""
            if not cursor or not page:
                break
        if not items:
            return pd.DataFrame()
        return pd.json_normalize(items, sep="_", errors="ignore")

    # ------------------------------------------------------------- реализация
    def _parse_realization_rows(self, data, day: date) -> pd.DataFrame:
        """Строки реализации -> плоский датафрейм (см. OZON_RU_COLUMNS)."""
        rows = data.get("rows", [])
        if not rows:
            return pd.DataFrame()
        out = []
        for r in rows:
            item = r.get("item") or {}
            dc = r.get("delivery_commission") or {}
            rc = r.get("return_commission")
            qty = int(dc.get("quantity") or 0)
            seller_price = float(r.get("seller_price_per_instance") or 0) * qty
            standard_fee = float(dc.get("standard_fee") or 0)
            compare = float(dc.get("amount") or 0) + float(dc.get("bonus") or 0) - standard_fee
            income = float(dc.get("total") or compare)
            return_total = 0.0
            returns_qty = 0
            if rc and rc.get("total"):
                return_total = float(rc.get("total") or 0)
                income += return_total
                returns_qty += int(rc.get("quantity") or 0)
            commission = -(standard_fee if standard_fee else income * float(r.get("commission_ratio") or 0))
            out.append({
                "date": day, "offer_id": str(item.get("offer_id", "")).strip(),
                "name": item.get("name", ""), "barcode": item.get("barcode", ""),
                "sku": item.get("sku", ""), "quantity": qty,
                "seller_price": seller_price,
                "amount": float(dc.get("amount") or 0),
                "commission_ratio": float(r.get("commission_ratio") or 0),
                "commission": commission, "income": income, "returns_qty": returns_qty,
                "delivery_amount": float(dc.get("amount") or 0),
                "delivery_standard_fee": standard_fee,
                "delivery_total": float(dc.get("total") or 0),
                "return_total": return_total,
            })
        return pd.DataFrame(out)

    def get_realization(self, month: int, year: int) -> pd.DataFrame:
        """Отчёт о реализации за месяц: /v2/finance/realization (дата = конец отчёта)."""
        if self.testing:
            rng = np.random.default_rng(15)
            arts = self._mock_articles()
            rows = []
            for a in arts:
                qty = int(rng.integers(0, 4))
                if qty == 0:
                    continue
                price = float(rng.uniform(800, 3500))
                fee = price * float(rng.uniform(0.1, 0.5))
                rows.append({
                    "date": date(year, month, 28), "offer_id": a,
                    "name": f"Товар {a}", "barcode": f"4{f'{int(a[4:]):010d}'}", "sku": f"1{f'{int(a[4:]):010d}'}",
                    "quantity": qty, "seller_price": round(price * qty, 2),
                    "amount": round(price * qty * 0.2, 2),
                    "commission_ratio": round(fee / price, 4),
                    "commission": -round(fee * qty, 2), "income": round(price * qty - fee * qty, 2),
                    "returns_qty": 0, "delivery_amount": 0,
                    "delivery_standard_fee": round(fee * qty, 2), "delivery_total": round(price * qty * 0.8, 2),
                    "return_total": 0,
                })
            return pd.DataFrame(rows)

        resp = self._post(f"{OZON_API}/v2/finance/realization", {"month": month, "year": year})
        data = resp.json().get("result", {})
        stop = pd.Timestamp(data.get("header", {}).get("stop_date")).date()
        return self._parse_realization_rows(data, stop)

    def get_sales(self, date_from, date_to) -> pd.DataFrame:
        """Продажи за период как набор помесячных отчётов о реализации."""
        if self.testing:
            return self.get_realization(8, 2026)

        start_d = pd.Timestamp(date_from).date()
        end_d = pd.Timestamp(date_to).date()
        frames = []
        month = date(start_d.year, start_d.month, 1)
        while month <= end_d:
            df = self.get_realization(month.month, month.year)
            if not df.empty:
                frames.append(df)
            if month.month == 12:
                month = date(month.year + 1, 1, 1)
            else:
                month = date(month.year, month.month + 1, 1)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    # -------------------------------------------------------- движение средств
    def get_cash_flow(self, date_from, date_to) -> pd.DataFrame:
        """Движение средств: /v1/finance/cash-flow-statement/list (постранично)."""
        if self.testing:
            start = pd.Timestamp(date_from).date()
            end = pd.Timestamp(date_to).date()
            days = [start]
            while days[-1] < end:
                days.append(days[-1] + pd.Timedelta(days=1).to_pytimedelta())
            rng = np.random.default_rng(17)
            n = len(days)
            return pd.DataFrame({
                "period_begin": [str(d) for d in days],
                "begin_balance": rng.uniform(0, 5000, n).round(2),
                "payments_amount": rng.uniform(0, 100000, n).round(2),
                "services_total": rng.uniform(-20000, 30000, n).round(2),
                "end_balance": rng.uniform(0, 5000, n).round(2),
            })

        all_details, page = [], 1
        params = {
            "date": {"from": f"{date_from}T00:00:00.000Z", "to": f"{date_to}T23:59:59.999Z"},
            "page": page, "page_size": 1000, "with_details": True,
        }
        while True:
            resp = self._post(f"{OZON_API}/v1/finance/cash-flow-statement/list", params)
            result = resp.json().get("result", {})
            details = result.get("details", [])
            if not details:
                break
            all_details += details
            params["page"] += 1
            if len(details) < 1000:
                break
        rows = []
        for d in all_details:
            period = d.get("period", {})
            payments = d.get("payments") or []
            services = d.get("services") or {}
            others = d.get("others") or {}
            rows.append({
                "period_begin": period.get("begin"),
                "period_end": period.get("end"),
                "begin_balance": d.get("begin_balance_amount"),
                "payments_amount": (payments[0].get("payment") if payments else None),
                "delivery_total": (d.get("delivery") or {}).get("total"),
                "return_total": (d.get("return") or {}).get("total"),
                "services_total": services.get("total"),
                "others_total": others.get("total"),
                "end_balance": d.get("end_balance_amount"),
            })
        return pd.DataFrame(rows)