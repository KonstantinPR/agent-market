"""Детерминированные фейковые провайдеры для тестов.

Классы повторяют интерфейс реальных провайдеров (get_cards/get_prices/...),
но возвращают маленькие фиксированные DataFrame без сети.
Атрибут *_error позволяет симулировать сбой конкретного вида.
"""

from datetime import date

import pandas as pd
import requests

D1 = date(2026, 9, 1)


def _err(status_code, text="boom"):
    resp = requests.Response()
    resp.status_code = status_code
    raise requests.HTTPError(text, response=resp)


class FakeWb:
    fail_fast_429 = False

    cards_error = None
    stock_error = None
    funnel_error = None
    prices_error = None
    storage_error = None
    sales_error = None
    detail_error = None

    def get_cards(self):
        if self.cards_error:
            raise self.cards_error
        return pd.DataFrame({
            "vendorCode": ["TST-1", "TST-2"],
            "nmID": ["1001", "1002"],
            "title": ["Товар 1", "Товар 2"],
            "skus": ["2001", "2002"],
            "chrtId": [101, 102],
            "techSize": ["46", "47"],
        })

    def get_stock_report(self):
        if self.stock_error:
            raise self.stock_error
        return pd.DataFrame({
            "vendorCode": ["TST-1", "TST-2"],
            "warehouseName": ["Склад 1", "Склад 1"],
            "chrtId": [101, 102],
            "techSize": ["46", "47"],
            "quantity": [5, 7],
            "quantityFull": [9, 11],
            "inWayToClient": [2, 3],
            "inWayFromClient": [1, 0],
        })

    def get_sales_funnel(self, date_from, date_to):
        if self.funnel_error:
            raise self.funnel_error
        return pd.DataFrame({
            "nmID": ["1001", "1002"],
            "viewsCount": [100, 90],
            "openCardCount": [10, 9],
            "addToCartCount": [4, 3],
            "orderCount": [2, 2],
            "buyoutCount": [1, 2],
            "buyoutSum": [1000.0, 1800.0],
            "avgPrice": [1000, 900],
            "revenue": [2000, 1800],
        })

    def get_prices(self):
        if self.prices_error:
            raise self.prices_error
        return pd.DataFrame({"nmID": ["1001", "1002"], "price": [1100, 990]})

    prices_min_prices = None
    min_prices_error = None

    def get_min_prices(self, nm_ids):
        if self.min_prices_error:
            raise self.min_prices_error
        if self.prices_min_prices is not None:
            return self.prices_min_prices
        return {str(nm): 500.0 for nm in nm_ids}

    update_prices_error = None
    applied_prices = None

    def update_prices(self, items):
        if self.update_prices_error:
            raise self.update_prices_error
        self.applied_prices = items
        return {"uploadId": "fake-task-1", "task_id": "fake-task-1"}

    def get_storage_cost(self, number_last_days=7):
        if self.storage_error:
            raise self.storage_error
        return pd.DataFrame({
            "vendorCode": ["TST-1", "TST-2"],
            "nmId": [1001, 1002],
            "barcodesCount": [10, 12],
            "volume": [0.200, 0.350],
            "warehousePrice": [5000.0, 5400.0],
            "storagePricePerBarcode": [500.0, 450.0],
        })

    def get_sales_realization(self, date_from, date_to):
        if self.sales_error:
            raise self.sales_error
        return pd.DataFrame({
            "sa_name": ["TST-1", "TST-2"],
            "sale_dt": [str(D1), str(D1)],
            "quantity": [2, 3],
            "retail_amount": [2200, 2700],
            "ppvz_for_pay": [2000, 2500],
            "delivery_rub": [100, 120],
            "storage_fee": [20, 25],
            "ppvz_sales_commission": [80, 55],
            "return_amount": [0, 0],
            "additional_payment": [0, 0],
            "penalty": [0, 0],
            "deduction": [0, 0],
        })

    def get_sales_detail(self, date_from, date_to):
        if self.detail_error:
            raise self.detail_error
        return pd.DataFrame({
            "vendorCode": ["TST-1", "TST-2"],
            "saleDt": [str(D1), str(D1)],
            "quantity": [1, 1],
            "retailAmount": [1100, 1000],
            "forPay": [1000, 920],
            "deliveryService": [50, 45],
            "paidStorage": [10, 15],
            "ppvzSalesCommission": [40, 35],
            "returnedAmount": [0, 0],
            "additionalPayment": [5, 0],
            "penalty": [10, 0],
            "deduction": [0, 5],
        })


class FakeOz:
    fail_fast_429 = False

    cards_error = None
    stock_error = None
    prices_error = None
    realization_error = None
    cashflow_error = None

    def get_cards(self):
        if self.cards_error:
            raise self.cards_error
        return pd.DataFrame({
            "Ozon Product ID": ["95000001", "95000002"],
            "Offer ID": ["OZ-1", "OZ-2"],
            "Name": ["Ozon 1", "Ozon 2"],
            "Barcode": ["3001", "3002"],
            "Category": ["Обувь", "Обувь"],
        })

    def get_stock(self):
        if self.stock_error:
            raise self.stock_error
        return pd.DataFrame({
            "item_code": ["OZ-1", "OZ-2"],
            "warehouse_name": ["FBO", "FBO"],
            "free_to_sell_amount": [4, 6],
        })

    def get_prices(self):
        if self.prices_error:
            raise self.prices_error
        return pd.DataFrame({
            "offer_id": ["OZ-1", "OZ-2"],
            "product_id": ["P1", "P2"],
            "price_price": [1200, 800],
            "price_old_price": [1500, 1000],
            "price_min_price": [1000, 700],
        })

    def get_realization(self, month, year):
        if self.realization_error:
            raise self.realization_error
        return pd.DataFrame({
            "date": [f"{year:04d}-{month:02d}-01"] * 2,
            "offer_id": ["OZ-1", "OZ-2"],
            "quantity": [3, 1],
            "returns_qty": [0, 0],
            "seller_price": [1300, 900],
            "income": [1200, 800],
            "commission": [100, 100],
            "delivery_amount": [10, 10],
        })

    def get_cash_flow(self, date_from, date_to):
        if self.cashflow_error:
            raise self.cashflow_error
        return pd.DataFrame({"date": [str(D1)], "amount": [5000.0]})

    detail_error = None
    buyout_error = None

    def get_placement(self, date_from, date_to):
        return pd.DataFrame({
            "date": [str(D1), str(D1)],
            "sku": ["3001", "3002"],
            "offer_id": ["OZ-1", "OZ-2"],
            "warehouse": ["FBO", "FBO"],
            "paid_quantity": [2, 1],
            "paid_volume": [120.0, 60.0],
            "storage": [0.0, 0.0],
        })

    def get_sales_detail(self, date_from, date_to):
        if self.detail_error:
            raise self.detail_error
        return pd.DataFrame({
            "date": [str(D1), str(D1)],
            "posting_number": ["PZ-1", "PZ-2"],
            "offer_id": ["OZ-1", "OZ-2"],
            "name": ["Ozon 1", "Ozon 2"],
            "sku": ["3001", "3002"],
            "barcode": ["3001", "3002"],
            "quantity": [2, 1],
            "seller_price": [1300, 900],
            "amount": [2560.0, 800.0],
            "commission_ratio": [0.11, 0.10],
            "commission": [-281.6, -80.0],
            "standard_fee": [-38.4, -20.0],
            "income": [2280.0, 700.0],
            "return_qty": [0, 0],
            "return_total": [0.0, 0.0],
        })

    def get_buyout(self, date_from, date_to):
        if self.buyout_error:
            raise self.buyout_error
        return pd.DataFrame({
            "posting_number": ["PZ-1", "PZ-2"],
            "offer_id": ["OZ-1", "OZ-2"],
            "name": ["Ozon 1", "Ozon 2"],
            "sku": ["3001", "3002"],
            "quantity": [2, 1],
            "seller_price": [1300, 900],
            "buyout_price": [1250, 880],
            "amount": [2500.0, 880.0],
            "deduction_by_category_percent": [12.5, 0.0],
            "vat_percent": [20, 20],
        })


class FakeWbPricesFail(FakeWb):
    def get_prices(self):
        _err(429, "Too Many Requests")