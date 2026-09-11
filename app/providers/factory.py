"""Фабрика провайдеров маркетплейсов.

Единая точка создания WbProvider/OzonProvider: приложения и тесты подменяют
именно фабрику (или провайдера по параметру), не трогая модули-потребители.
"""

from app.providers.ozon import OzonProvider
from app.providers.wb import WbProvider


def get_wb_provider(with_fail_fast: bool = False, testing_mode=None) -> WbProvider:
    prov = WbProvider(testing_mode=testing_mode)
    prov.fail_fast_429 = with_fail_fast
    return prov


def get_oz_provider(with_fail_fast: bool = False, testing_mode=None) -> OzonProvider:
    prov = OzonProvider(testing_mode=testing_mode)
    prov.fail_fast_429 = with_fail_fast
    return prov