"""Провайдер Ozon: реальные вызовы Ozon API + моки.

Проверено по live-ключам пользователя (Client-Id 164497) 2026-09:
- карточки: асинхронный отчёт /v1/report/products/create -> poll -> download CSV
- остатки:  /v2/analytics/stock_on_warehouses
- цены:     /v5/product/info/prices (visibility=VISIBLE)
- продажи:  /v2/finance/realization (помесячно; v3 finance/transaction/list на этом
            аккаунте возвращает "obsolete method cannot be used")
- детализация продаж по постингам: /v1/finance/realization/posting (при 400 «отчёт
            слишком большой» — асинхронный /v1/report/realization/posting/create + poll)
- выкупы:   /v1/finance/products/buyout
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

        try:
            resp = self._post(f"{OZON_API}/v2/finance/realization", {"month": month, "year": year})
        except (OzonApiError, requests.HTTPError) as e:
            # GET /v2/finance/realization перехватываем здесь, а не в _post, потому
            # что _post кидает именно requests.HTTPError (с .response).
            code = getattr(e, "status_code", None)
            if code is None:
                code = getattr(getattr(e, "response", None), "status_code", None)
            # "Report was not found" лежит в ТЕЛЕ ответа (e.response.text), а не в
            # str(e) (= "404 Client Error: ..."), поэтому проверяем оба места.
            detail = str(e) + " " + getattr(getattr(e, "response", None), "text", "")
            if code == 404 and "Report was not found" in detail:
                # Отчёт реализации за этот месяц ещё не сформирован (обычно для
                # текущего месяца) — пропускаем месяц, а не падаем с ошибкой.
                data = resp = None
                self.__last_month_skipped = (month, year)
            else:
                raise
        else:
            self.__last_month_skipped = None
            data = resp.json().get("result", {})
        if data is None:
            return pd.DataFrame()
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

    # ---------------------------------------------- детализация по постингам
    def _parse_realization_posting_rows(self, data, day: date) -> pd.DataFrame:
        """Строки /v1/finance/realization/posting -> плоский датафрейм.

        Схема строки: item {offer_id, name, sku, barcode}, order {posting_number,
        created_date}, delivery_commission/return_commission {quantity, amount,
        bonus, commission, compensation, standard_fee, total},
        seller_price_per_instance, commission_ratio.
        """
        rows = (data or {}).get("rows") or []
        if not rows:
            return pd.DataFrame()
        out = []
        for r in rows:
            item = r.get("item") or {}
            dc = r.get("delivery_commission") or {}
            rc = r.get("return_commission") or {}
            order = r.get("order") or {}
            qty = int(dc.get("quantity") or rc.get("quantity") or 0)
            price_per = float(r.get("seller_price_per_instance")
                              or dc.get("price_per_instance") or 0)
            seller_price = price_per * qty
            standard_fee = float(dc.get("standard_fee") or 0)
            amount = float(dc.get("amount") or 0)
            dc_total = float(dc.get("total") or 0)
            if not dc_total and (amount or standard_fee):
                dc_total = amount + float(dc.get("bonus") or 0) - standard_fee
            income = dc_total
            returns_qty = int(rc.get("quantity") or 0)
            return_total = float(rc.get("total") or 0)
            income += return_total
            ratio = float(r.get("commission_ratio") or 0)
            # Комиссию отражаем со знаком минус (как в реализации).
            commission = -(standard_fee if standard_fee else income * ratio)
            d = day
            created = str(order.get("created_date") or "")
            if created:
                try:
                    d = pd.Timestamp(created).date()
                except (ValueError, TypeError):
                    pass
            out.append({
                "date": d, "posting_number": str(order.get("posting_number") or "").strip(),
                "offer_id": str(item.get("offer_id") or "").strip(),
                "name": item.get("name") or "",
                "sku": item.get("sku") or "", "barcode": item.get("barcode") or "",
                "quantity": qty, "seller_price": seller_price, "amount": amount,
                "commission_ratio": ratio, "commission": commission,
                "standard_fee": standard_fee, "income": income,
                "return_qty": returns_qty, "return_total": return_total,
            })
        return pd.DataFrame(out)

    def _realization_posting_report(self, month: int, year: int) -> pd.DataFrame:
        """Фолбэк на асинхронный отчёт по постингам (слишком большой ответ)."""
        resp = self._post(f"{OZON_API}/v1/report/realization/posting/create",
                          {"month": month, "year": year})
        code = resp.json()["code"]
        for attempt in range(25):
            time.sleep(20)
            info = self._post(f"{OZON_API}/v1/report/info", {"code": code})
            result = info.json().get("result") or {}
            status = result.get("status")
            if status == "success":
                content = requests.get(result["file"], timeout=300).content
                return self._parse_realization_report_file(content, month, year)
            if status not in ("processing", "waiting"):
                raise RuntimeError(f"Ozon: отчёт по постингам завершился статусом {status}")
        raise RuntimeError("Ozon: отчёт по постингам не сформировался за отведённое время")

    def _parse_realization_report_file(self, content, month: int, year: int) -> pd.DataFrame:
        """Файл отчёта по постингам (csv/xlsx) -> датафрейм как из API.

        Реальный файл (live 2026-09): CSV с ',' и flattened-колонками
        (order_posting_number, item_offer_id, delivery_commission_*,
        return_commission_*, seller_price_per_instance, commission_ratio).
        """
        data = None
        try:
            data = pd.read_excel(io.BytesIO(content), engine="openpyxl")
        except Exception:
            pass
        if data is None:
            for sep in (",", ";"):
                try:
                    data = pd.read_csv(io.BytesIO(content), sep=sep, dtype=str)
                    if data.shape[1] > 1:
                        break
                except Exception:
                    continue
        if data is None or data.empty:
            return pd.DataFrame()
        data = data.where(data.notna(), None)
        cols = {str(c).strip().lower(): c for c in data.columns}

        def pick(keys):
            for k in keys:
                k = k.lower()
                if k in cols:
                    return cols[k]
                for name, col in cols.items():
                    if name.endswith("_" + k) or name == k:
                        return col
            return None

        flat = {"posting_number": ["order_posting_number", "posting_number",
                                    "номер отправления", "отправление"],
                "name": ["item_name", "наименование", "название", "название товара"],
                "offer_id": ["item_offer_id", "артикул продавца", "артикул", "offer id"],
                "sku": ["item_sku", "sku", "id товара"],
                "barcode": ["item_barcode", "штрихкод", "баркод"],
                "quantity": ["delivery_commission_quantity", "количество", "кол-во"],
                "amount": ["delivery_commission_amount", "сумма продажи", "сумма"],
                "standard_fee": ["delivery_commission_standard_fee"],
                "income": ["delivery_commission_total", "к перечислению", "итог",
                           "сумма к перечислению"],
                "return_qty": ["return_commission_quantity", "кол-во возвратов",
                               "возвраты кол-во"],
                "return_total": ["return_commission_total", "сумма возврата",
                                 "возврат сумма"]}
        mapping = {}
        for out, keys in flat.items():
            c = pick(keys)
            if c is not None:
                mapping[c] = out
        price_col = pick(["seller_price_per_instance", "цена продажи", "цена",
                          "цена за единицу"])
        ratio_col = pick(["commission_ratio", "доля комиссии"])
        bonus_col = pick(["delivery_commission_bonus"])
        sale_date_col = pick(["legal_entity_document_sale_date", "order_created_date",
                              "дата продажи", "дата"])
        if "offer_id" not in mapping.values():
            return pd.DataFrame()
        data = data.rename(columns=mapping)
        price = pd.to_numeric(data.pop(price_col), errors="coerce").fillna(0) \
            if price_col else pd.Series(0.0, index=data.index)
        ratio = pd.to_numeric(data.pop(ratio_col), errors="coerce").fillna(0) \
            if ratio_col else pd.Series(0.0, index=data.index)
        bonus = pd.to_numeric(data.pop(bonus_col), errors="coerce").fillna(0) \
            if bonus_col else pd.Series(0.0, index=data.index)
        sale_dates = None
        if sale_date_col is not None:
            sale_dates = pd.to_datetime(data.pop(sale_date_col), errors="coerce")
        keep = [c for c in data.columns if c in mapping.values()]
        data = data[keep]
        for c in ("quantity", "return_qty"):
            s = data[c] if c in data.columns else pd.Series(0, index=data.index)
            data[c] = pd.to_numeric(s, errors="coerce").fillna(0).astype(int)
        for c in ("amount", "standard_fee", "income", "return_total"):
            s = data[c] if c in data.columns else pd.Series(0.0, index=data.index)
            data[c] = pd.to_numeric(s, errors="coerce").fillna(0).astype(float)
        if "income" not in data.columns:
            data["income"] = data["amount"] + bonus - data["standard_fee"]
        income = data["income"]
        standard_fee = data["standard_fee"]
        commissions = -pd.Series(np.where(standard_fee.to_numpy() != 0,
                                          standard_fee.to_numpy(),
                                          (income * ratio).to_numpy()),
                                 index=data.index)
        data["income"] = income + data["return_total"]
        data["seller_price"] = price * data["quantity"]
        data["commission_ratio"] = ratio
        data["commission"] = commissions
        day = pd.Timestamp(year, month, 28).date()
        if sale_dates is not None:
            data["date"] = sale_dates.dt.date.fillna(day)
        else:
            data["date"] = day
        data["offer_id"] = data["offer_id"].astype(str).str.strip()
        data["posting_number"] = data["posting_number"].astype(str).str.strip()
        return data

    def get_realization_posting(self, month: int, year: int) -> pd.DataFrame:
        """Детализация реализаций по постингам за месяц (постинговый отчёт)."""
        if self.testing:
            rng = np.random.default_rng(21)
            arts = self._mock_articles()
            rows = []
            for a in arts:
                for _ in range(int(rng.integers(0, 3))):
                    price = float(rng.uniform(900, 4000))
                    qty = int(rng.integers(1, 3))
                    fee = price * float(rng.uniform(0.1, 0.45))
                    post = f"{int(rng.integers(10000000, 99999999))}-{int(rng.integers(1000, 9999))}-{int(rng.integers(1, 3))}"
                    rows.append({
                        "date": date(year, month, int(rng.integers(1, 28))),
                        "posting_number": post, "offer_id": a, "name": f"Товар {a}",
                        "sku": f"1{f'{int(a[4:]):010d}'}", "barcode": f"4{f'{int(a[4:]):010d}'}",
                        "quantity": qty, "seller_price": round(price * qty, 2),
                        "amount": round(price * qty, 2),
                        "commission_ratio": round(fee / price, 4),
                        "commission": -round(fee * qty, 2),
                        "standard_fee": round(fee * qty, 2),
                        "income": round(price * qty - fee * qty, 2),
                        "return_qty": 0, "return_total": 0,
                    })
            return pd.DataFrame(rows)

        resp = self._post(f"{OZON_API}/v1/finance/realization/posting",
                          {"month": month, "year": year})
        data = resp.json()
        stop = pd.Timestamp((data.get("header") or {}).get("stop_date")).date() \
            if (data.get("header") or {}).get("stop_date") else date(year, month, 28)
        df = self._parse_realization_posting_rows(data, stop)
        if df.empty or df["posting_number"].astype(str).str.strip().eq("").all():
            # Отчёт может быть слишком большим / постинги пусты -> асинхронный отчёт.
            try:
                return self._realization_posting_report(month, year)
            except RuntimeError:
                return df
        return df

    def get_sales_detail(self, date_from, date_to) -> pd.DataFrame:
        """Детализация реализаций по постингам за период (помесячно)."""
        if self.testing:
            return self.get_realization_posting(8, 2026)
        start_d = pd.Timestamp(date_from).date()
        end_d = pd.Timestamp(date_to).date()
        frames = []
        month = date(start_d.year, start_d.month, 1)
        while month <= end_d:
            df = self.get_realization_posting(month.month, month.year)
            if not df.empty:
                frames.append(df)
            if month.month == 12:
                month = date(month.year + 1, 1, 1)
            else:
                month = date(month.year, month.month + 1, 1)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    # ---------------------------------------------------------------- выкупы
    def get_buyout(self, date_from, date_to) -> pd.DataFrame:
        """Выкупы товаров: /v1/finance/products/buyout."""
        if self.testing:
            arts = self._mock_articles()
            rng = np.random.default_rng(23)
            rows = []
            for a in arts:
                for _ in range(int(rng.integers(0, 3))):
                    price = float(rng.uniform(900, 4000))
                    qty = int(rng.integers(1, 3))
                    rows.append({
                        "posting_number": f"{int(rng.integers(10000000, 99999999))}-{int(rng.integers(1000, 9999))}-{int(rng.integers(1, 3))}",
                        "offer_id": a, "name": f"Товар {a}",
                        "sku": f"1{f'{int(a[4:]):010d}'}",
                        "quantity": qty, "seller_price": round(price * qty, 2),
                        "buyout_price": round(price * 0.7, 2),
                        "amount": round(price * qty * 0.7, 2),
                        "deduction_by_category_percent": 0.0, "vat_percent": 20,
                    })
            return pd.DataFrame(rows)

        resp = self._post(f"{OZON_API}/v1/finance/products/buyout", {
            "date_from": pd.Timestamp(date_from).date().isoformat(),
            "date_to": pd.Timestamp(date_to).date().isoformat(),
        })
        products = (resp.json().get("result") or {}).get("products") or []
        if not products:
            return pd.DataFrame()
        out = []
        for p in products:
            qty = int(p.get("quantity") or 0)
            out.append({
                "posting_number": str(p.get("posting_number") or "").strip(),
                "offer_id": str(p.get("offer_id") or "").strip(),
                "name": p.get("name") or "",
                "sku": p.get("sku") or "",
                "quantity": qty,
                "seller_price": float(p.get("seller_price_per_instance") or 0) * qty,
                "buyout_price": float(p.get("buyout_price") or 0),
                "amount": float(p.get("amount") or 0),
                "deduction_by_category_percent": float(p.get("deduction_by_category_percent") or 0),
                "vat_percent": int(p.get("vat_percent") or 0),
            })
        return pd.DataFrame(out)