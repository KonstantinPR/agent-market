# Карта API

<!-- Сгенерировано: python scripts/gen_api_map.py -- не редактировать руками. -->

Всего эндпоинтов `/api/*`: **119**, групп: **27**. Источник — реестр роутов FastAPI (`app.main:app`), поэтому список всегда совпадает с кодом.

Общий префикс и общие `Depends` заданы в `app/api/__init__.py`. Все ответы — JSON или файл (`StreamingResponse`/`FileResponse`); `response_model` сейчас почти не используется — схему смотреть в теле хендлера.

## Содержание

- [cabinet](#cabinet) — 5 шт.
- [cabinets](#cabinets) — 1 шт.
- [cards](#cards) — 1 шт.
- [custom-stock](#custom-stock) — 1 шт.
- [dashboard](#dashboard) — 1 шт.
- [export](#export) — 23 шт.
- [funnel](#funnel) — 1 шт.
- [import](#import) — 4 шт.
- [margin](#margin) — 3 шт.
- [ozon](#ozon) — 16 шт.
- [prices](#prices) — 1 шт.
- [pricing](#pricing) — 5 шт.
- [products](#products) — 6 шт.
- [progress](#progress) — 1 шт.
- [promo](#promo) — 2 шт.
- [pulls](#pulls) — 1 шт.
- [refresh](#refresh) — 4 шт.
- [refresh-all](#refresh-all) — 1 шт.
- [replenish](#replenish) — 2 шт.
- [sales](#sales) — 1 шт.
- [stocks](#stocks) — 1 шт.
- [storage-cost](#storage-cost) — 1 шт.
- [sync](#sync) — 1 шт.
- [tickets](#tickets) — 8 шт.
- [warehouse](#warehouse) — 14 шт.
- [wb](#wb) — 10 шт.
- [yandex](#yandex) — 4 шт.

## cabinet

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/cabinet/current` | `api_cabinet_current` | Активная связка (по куке agent_cabinet). None — «как раньше» (public). |
| GET | `/api/cabinet/pricing-settings` | `api_pricing_settings_get` | Настройки автопилота цен активной связки (JSON) — {} если не заданы. |
| PUT | `/api/cabinet/pricing-settings` | `api_pricing_settings_put` | Сохраняет настройки автопилота цен за активной связкой. |
| POST | `/api/cabinet/pricing-settings/copy` | `api_pricing_settings_copy` | Копирует настройки автопилота текущей связки в другие связки. |
| POST | `/api/cabinet/select` | `api_cabinet_select` | Выбирает активную связку (кука agent_cabinet + last_cabinet_id). |

## cabinets

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/cabinets` | `api_cabinets` | Список фирм со связками текущего пользователя (с признаком активной связки). |

## cards

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/cards` | `api_cards` | Сырые карточки маркетплейса (marketplace_cards): одна строка = размер/SKU. |

## custom-stock

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/custom-stock` | `api_custom_stock` |  |

## dashboard

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/dashboard` | `api_dashboard` | Обзор: KPI из детализаций + топы + дельты цены + склад + свежесть. |

## export

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/export/dashboard` | `export_dashboard` | Дашборд одним Excel-файлом: KPI, топы, изменения цен, группы. |
| GET | `/api/export/margin/detail` | `export_margin_detail` |  |
| GET | `/api/export/margin/funnel` | `export_margin_funnel` |  |
| GET | `/api/export/margin/ozon-detail` | `export_margin_ozon_detail` |  |
| GET | `/api/export/ozon/accrual-rows` | `export_ozon_accrual_rows` |  |
| GET | `/api/export/ozon/buyout-rows` | `export_ozon_buyout_rows` | Экспорт сырых строк «Выкупов» Ozon в Excel. |
| GET | `/api/export/ozon/cashflow-rows` | `export_ozon_cashflow_rows` |  |
| GET | `/api/export/ozon/detail-rows` | `export_ozon_detail_rows` | Экспорт сырых строк «Детализации реализаций» Ozon в Excel. |
| GET | `/api/export/ozon/detail-summary` | `export_ozon_detail_summary` |  |
| GET | `/api/export/ozon/placement-rows` | `export_ozon_placement_rows` |  |
| GET | `/api/export/ozon/placement-summary` | `export_ozon_placement_summary` |  |
| GET | `/api/export/products` | `export_products` | Экспорт каталога «Наш склад → Товары» в Excel по видимым колонкам. |
| GET | `/api/export/replenish` | `export_replenish` |  |
| GET | `/api/export/replenish/pdf` | `export_replenish_pdf` | Карточки потребности в PDF из строк таблицы (базовый режим). |
| POST | `/api/export/replenish/pdf` | `export_replenish_pdf_from_excel` | PDF из строк Excel-файла (дропзона в меню PDF). |
| GET | `/api/export/sales` | `export_sales` |  |
| GET | `/api/export/wb/cards` | `export_wb_cards` | Экспорт карточек WB из БД (marketplace_cards) в Excel. |
| GET | `/api/export/wb/detail-rows` | `export_wb_detail_rows` | Экспорт сырых строк «Детализации продаж» WB (wb_detail_rows) в Excel. |
| GET | `/api/export/wb/detail-summary` | `export_wb_detail_summary` |  |
| GET | `/api/export/wb/funnel` | `export_wb_funnel` | Экспорт воронки продаж WB (funnel_metric) в Excel. |
| GET | `/api/export/wb/prices` | `export_wb_prices` | Экспорт текущих цен/скидок (price_snapshots) в Excel. |
| GET | `/api/export/wb/stock` | `export_wb_stock` | Экспорт остатков WB (последний срез stocks) в Excel. |
| GET | `/api/export/wb/storage` | `export_wb_storage` | Экспорт стоимости хранения WB (storage_costs) в Excel. |

## funnel

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/funnel` | `api_funnel` | Строки воронки продаж WB из funnel_metric. |

## import

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| POST | `/api/import/cards` | `import_cards` | Загрузка карточек товара маркетплейса из Excel. |
| POST | `/api/import/custom-stock` | `import_custom_stock` |  |
| POST | `/api/import/net-cost` | `import_net_cost` | Обновляет себестоимость (net_cost) товаров из файла с колонками article/net_cost. |
| POST | `/api/import/products` | `import_products` |  |

## margin

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/margin/detail` | `api_margin_detail` | Прибыльность по Детализации Продаж WB напрямую из wb_detail_rows. |
| GET | `/api/margin/funnel` | `api_margin_funnel` | Прибыльность по Воронке Продаж WB (данные funnel_metric, оценка). |
| GET | `/api/margin/ozon-detail` | `api_margin_ozon_detail` | Прибыльность по Детализации Продаж Ozon напрямую из ozon_detail_rows. |

## ozon

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| POST | `/api/ozon/accrual` | `ozon_accrual` | Начисления по товарам (аккруалы) Ozon за окно: /v1/finance/accrual/by-day. |
| GET | `/api/ozon/accrual-rows` | `api_ozon_accrual_rows` | Строки начислений Ozon (ozon_accruals) за окно + итоги по корзинам. |
| POST | `/api/ozon/buyout` | `ozon_buyout` |  |
| GET | `/api/ozon/buyout-rows` | `api_ozon_buyout_rows` | Сырые строки «Выкупов» Ozon (ozon_buyouts) с фильтром по артикулу. |
| POST | `/api/ozon/cards` | `ozon_cards` |  |
| POST | `/api/ozon/cashflow` | `ozon_cashflow` |  |
| GET | `/api/ozon/cashflow-rows` | `api_ozon_cashflow_rows` | Периоды «Движения средств» Ozon (ozon_cash_flows) за окно + итоги. |
| POST | `/api/ozon/detail` | `ozon_detail` |  |
| GET | `/api/ozon/detail-rows` | `api_ozon_detail_rows` | Сырые строки «Детализации реализаций» Ozon (ozon_detail_rows) с фильтрами. |
| GET | `/api/ozon/detail-summary` | `api_ozon_detail_summary` | Свод «Детализации реализаций» Ozon по артикулам (+ выкупы). |
| POST | `/api/ozon/placement` | `ozon_placement` |  |
| GET | `/api/ozon/placement-rows` | `api_ozon_placement_rows` | Сырые строки «Размещения (хранения)» Ozon (ozon_placements) с фильтрами. |
| GET | `/api/ozon/placement-summary` | `api_ozon_placement_summary` | Свод «Размещения (хранения)» Ozon по артикулам: дни, кол-во, объём, сумма. |
| POST | `/api/ozon/prices` | `ozon_prices` |  |
| POST | `/api/ozon/realization` | `ozon_realization` | Реализация за месяцы, покрывающие окно из шапки (или точный месяц). |
| POST | `/api/ozon/stock` | `ozon_stock` |  |

## prices

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/prices` | `api_prices` | Срез текущих цен/скидок из price_snapshots (последняя загрузка). |

## pricing

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| POST | `/api/pricing/apply` | `pricing_apply` | Применяет скидки через WB API upload/task и пишет журнал price_changes. |
| GET | `/api/pricing/defaults` | `pricing_defaults` |  |
| POST | `/api/pricing/export` | `pricing_export` | Рекомендации автопилота в Excel. Изменения в WB API НЕ вносятся. |
| GET | `/api/pricing/history` | `pricing_history` | Журнал решений автопилота (последние события). |
| POST | `/api/pricing/recommendations` | `pricing_recommendations` | Read-only расчёт рекомендаций по правилам R1-R10. body = настройки (перекрытие дефолтов). |

## products

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/products` | `api_products` | Каталог «Наш склад → Товары». |
| POST | `/api/products/preview` | `products_preview` | Пересчёт рекомендуемой (mode=recommended) или минимальной (mode=min) цены |
| GET | `/api/products/price-settings` | `products_price_settings` |  |
| POST | `/api/products/prices/apply` | `products_prices_apply` | Применяет рекомендуемые (mode=recommended) или минимальные (mode=min) цены |
| POST | `/api/products/refresh` | `products_refresh` | Обновляет общий каталог из карточек WB+Ozon (pull_catalog). |
| POST | `/api/products/replenishable` | `product_replenishable` | Переключает флаг «докупаемый» у товара (влияет на автопилот цен WB). |

## progress

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/progress` | `api_progress` | Прогресс текущей выкачки для зелёной строки состояния. |

## promo

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/promo/list` | `promo_list` | Сохранённые акции WB: актуальные и ближайшие (±90 дней), свежие сверху. |
| POST | `/api/promo/refresh` | `promo_refresh` | Одноразовое обновление акций WB (Календарь акций) в wb_promotions. |

## pulls

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/pulls` | `api_pulls` | Лог последних успешных загрузок из API маркетплейсов. |

## refresh

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/refresh` | `api_refresh_list` |  |
| POST | `/api/refresh` | `api_refresh` | Запускает фоновое обновление маркетплейса (wb\|ozon). Ошибки изолированы по видам. |
| GET | `/api/refresh/history` | `api_refresh_history` |  |
| GET | `/api/refresh/{job_id}` | `api_refresh_state` |  |

## refresh-all

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| POST | `/api/refresh-all` | `api_refresh_all` | «Обновить всё»: по очереди запускает обновление всех связок по их |

## replenish

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/replenish` | `api_replenish` | Потребность в товаре: спрос (детализации) + остатки (наш склад и МП). |
| POST | `/api/replenish/import-excel` | `replenish_import_excel` | Разбор Excel-файла с правками для PDF (дропзона в меню PDF). |

## sales

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/sales` | `api_sales` |  |

## stocks

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/stocks` | `api_stocks` |  |

## storage-cost

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/storage-cost` | `api_storage_cost` | Срез стоимости хранения WB из storage_costs (последняя загрузка). |

## sync

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| POST | `/api/sync/{code}` | `api_sync` |  |

## tickets

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/tickets` | `api_tickets` | Очередь тикетов из TICKETS.md. |
| POST | `/api/tickets` | `api_tickets_create` | Создаёт тикет в «Открытые». |
| POST | `/api/tickets/{tid}/block` | `api_ticket_block` |  |
| POST | `/api/tickets/{tid}/close` | `api_ticket_close` |  |
| POST | `/api/tickets/{tid}/decline` | `api_ticket_decline` |  |
| POST | `/api/tickets/{tid}/reopen` | `api_ticket_reopen` |  |
| POST | `/api/tickets/{tid}/start` | `api_ticket_start` |  |
| POST | `/api/tickets/{tid}/unblock` | `api_ticket_unblock` |  |

## warehouse

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| GET | `/api/warehouse/counterparties` | `api_cp_list` |  |
| POST | `/api/warehouse/counterparties` | `api_cp_save` |  |
| DELETE | `/api/warehouse/counterparties/{cp_id}` | `api_cp_delete` |  |
| GET | `/api/warehouse/docs` | `api_docs_list` |  |
| POST | `/api/warehouse/docs` | `api_doc_create` |  |
| DELETE | `/api/warehouse/docs/{doc_id}` | `api_doc_delete` |  |
| GET | `/api/warehouse/docs/{doc_id}/items` | `api_doc_items` |  |
| GET | `/api/warehouse/export/{kind}` | `api_wh_export` |  |
| POST | `/api/warehouse/fromdisk` | `api_wh_fromdisk` | Импорт всех .xlsx из папки «Наш склад/Приход или Отгрузка» на Диске. |
| POST | `/api/warehouse/import/counterparties` | `import_cp` |  |
| POST | `/api/warehouse/import/docs` | `import_docs` |  |
| GET | `/api/warehouse/stock` | `api_stock` |  |
| POST | `/api/warehouse/todisk` | `api_wh_todisk` | Сформировать Excel и загрузить на Яндекс.Диск в папку /agent_market/Наш склад/<kind>. |
| GET | `/api/warehouse/turnover` | `api_turnover` |  |

## wb

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| POST | `/api/wb/cards` | `wb_cards` |  |
| POST | `/api/wb/detail` | `wb_detail` |  |
| GET | `/api/wb/detail-rows` | `api_wb_detail_rows` | Сырые строки «Детализации продаж» WB (wb_detail_rows) с фильтрами и пагинацией. |
| GET | `/api/wb/detail-summary` | `api_wb_detail_summary` | Свод «Детализации продаж» WB по артикулам (сырые деньги операций, без маржи). |
| POST | `/api/wb/detail-upload` | `wb_detail_upload` | Ручная загрузка детализации продаж WB из Excel/zip (без finance-api, без лимита). |
| POST | `/api/wb/funnel` | `wb_funnel` |  |
| POST | `/api/wb/prices` | `wb_prices` |  |
| POST | `/api/wb/sales` | `wb_sales` |  |
| POST | `/api/wb/stock` | `wb_stock` |  |
| POST | `/api/wb/storage` | `wb_storage` |  |

## yandex

| Метод | Путь | Функция | Описание |
|---|---|---|---|
| DELETE | `/api/yandex/delete` | `yandex_delete` |  |
| GET | `/api/yandex/download` | `yandex_download` |  |
| GET | `/api/yandex/list` | `yandex_list` |  |
| POST | `/api/yandex/upload` | `yandex_upload` |  |
