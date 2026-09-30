# Тикеты — agent_market

Единая очередь задач проекта. Каждый тикет — это задача, которую нужно
«закрыть» (реализовать и закоммитить). Тикет не перенумеровывается:
номер выдаётся один раз и не переиспользуется, даже если тикет закрыли,
не начав, или закрыли как неактуальный.

Счётчик: следующая свободная метка — `T-29`.

## Правила

- Метка тикета `T-<N>` — монотонный набор. Новый номер = из шапки счётчика,
  после выдачи счётчик увеличивается на 1.
- Приоритет: `low | medium | high`. Чем выше приоритет, тем раньше берётся.
- Состояние тикета определяется разделом, в котором он лежит:
  - `## Открытые` — в очереди, можно брать;
  - `## В работе` — активные тикеты (может быть несколько);
  - `## Заблокированные` — ждёт внешних данных/доступа/решения от владельца
    (в теле — короткое описание, что именно нужно);
  - `## Закрытые` — выполненные и отклонённые (отклонённый — с пометкой
    `(declined)` в заголовке записи).
- Формат записи тикета:
  ```markdown
  - [T-3] (high) Заголовок
    > Тело: что сделать, контекст, ссылки на файлы/строки.
  ```
- Закрытые тикеты хранятся компактно, без тела:
  ```markdown
  - [T-3] (closed) Заголовок — ссылка на коммит `abc1234`
  ```

## Как ведётся работа

- Разработка идёт только через тикеты: сначала читается этот файл,
  берётся открытый тикет с наивысшим приоритетом, переводится
  в «В работе», затем код + тесты + коммит `T-<N>: описание`.
- Коммит по закрытию тикета ссылается на его номер: `T-3: ...`.
- Если для тикета нужен токен, доступ или решение — тикет уходит
  в «Заблокированные» с описанием ожидания.
- Веб-интерфейс (вкладка «Тикеты» в приложении) работает с тем же файлом:
  добавление, смена статуса, закрытие с хешем коммита. Ручные правки файла
  и правки из интерфейса не конфликтуют. Избегать двусмысленностей. Подробное и длинное
- описание приемлемо, если оно будет давать точные и облегчающие интерпретацию 
- другим агентам объяснения.

## Открытые

- [T-1] (medium) Хранение: учитывать возвращённое кол-во, а не только проданное
  > Тело: storage_split (app/services/sync.py:515) прибавляет к остатку
  > проданное кол-во периода × 0.5 (sold_fraction). Для товаров, распроданных
  > к моменту расчёта, надо добавить и возвращённое количество (в оригинале:
  > «подумаем потом»). Нужно обсудить формулу и точку учета возвратов
  > (returns_qty из свода vs возвраты из детализации).
- [T-7] (high) Ozon: провайдер — детализация реализаций по постингам + выкупы
  > Тело: Исследование Ozon Seller API сделано (OpenAPI-спека, зеркало
  > MissiaL/ozon-api, 2026-09): для детализации (аналог wb_detail_rows) берём
  > GET POST /v1/finance/realization/posting (строки: item {offer_id, name,
  > sku, barcode}, posting_number, qty, seller_price_per_instance,
  > delivery_commission {amount, bonus, commission, compensation,
  > standard_fee, coinvestment, total}, return_commission). При 400 «The
  > requested report is too large» → фолбэк на асинхронный отчёт
  > /v1/report/realization/posting/create + poll /v1/report/info (как в
  > get_cards). Выкупы: /v1/finance/products/buyout (buyout_price, % выкупа,
  > удержания, vat_percent, posting_number, offer_id, quantity). v3
  > finance/transaction/list на аккаунте «обsolete» — НЕ использовать.
  > Добавить методы в app/providers/ozon.py (+ моки для testing) и проверить
  > на живых ключах Client-Id 164497. Страховка: если оба детальных метода
  > недоступны — строить детализацию из строк /v2/finance/realization
  > (уже проверен).
- [T-8] (high) Ozon: таблицы OzonDetailRow/OzonBuyout + синк + API-эндпоинты
  > Тело: модели в app/models.py: OzonDetailRow (op_key = date+posting+sku;
  > posting_number, delivery_schema, offer_id, name, sku, barcode, quantity,
  > seller_price, amount, commission_ratio, commission, standard_fee, income,
  > return_qty, return_total, source, imported_at) и OzonBuyout (op_key =
  > date+posting+sku; buyout_price, buyout_percent, deduction_percent,
  > vat_percent, quantity, seller_price). Сервисы app/services/sync.py:
  > normalize_ozon_detail, normalize_ozon_buyout, upsert_ozon_detail_rows,
  > upsert_ozon_buyouts, oz_detail_summary_dataframe (свод по артикулам
  > = продажи WB-детализации + выкупы). app/services/refresh.py:
  > pull_oz_detail, pull_oz_buyout, record_api_pull('ozon', 'detail'/'buyout').
  > API app/api.py: POST /api/ozon/detail (окно from..to, месяц по умолчанию),
  > GET /api/ozon/detail-rows (фильтры/пагинация), GET /api/ozon/detail-summary,
  > /api/export/ozon/detail-rows, /api/export/ozon/detail-summary
  > (по образцу /api/export/wb/detail-*). Продажи source='ozon' НЕ трогаем —
  > детализация аддитивна (решение утверждено).
- [T-9] (high) UI — «Детализация продаж Ozon» по образцу WB
  > Тело: вкладка oz-detail 1-в-1 как tab-wb-detail (app/static/index.html,
  > app.js: registerColView('oz-detail', {rows, summary}), renderOzDetail,
  > downloadOzDetailExcel, uploadOzDetailToDisk, поиск, пагинация, «Выгрузить
  > в Excel» по видимым колонкам, «Загрузить на диск»; кнопка «Обновить базу»
  > в шапке маршрутизирует период для detail как в wbPullByTab).
- [T-10] (medium) UI — гармонизировать oz-cards/oz-stock/oz-prices/oz-realization под эталон
  > Тело: убрать дублирующие формы-тулбары «Скачать Excel + в БД» из oz-вкладок,
  > единая кнопка «Обновить базу» в шапке (ozPullByTab как wbPullByTab);
  > реальные таблицы просмотра из БД для cards/stock/prices/realization
  > (поиск, колонки, пагинация, экспорт по видимым колонкам, диск).
  > Починить renderCards для oz-cards (ссылается на несуществующий
  > #ozCardsTable, app.js:1305). cashflow остаётся export-only под кнопкой шапки.
- [T-11] (medium) «Магия → Обновить Ozon»: детализация + выкупы разом
  > Тело: app/services/refresh.py _PLAN['ozon'] + шаги detail/buyout
  > (include_detail для ozon, период = из шапки, по умолчанию предыдущий
  > месяц; для realization — месяцы, покрывающие окно). Всё в одном фоне
  > (start_refresh уже сериализует).
- [T-13] (medium) Автопилот цен WB: рекомендации() принимает date_from/date_to
  > Тело: recommendations() принимает date_from/date_to (шапка) + фолбэк
  > window_days; джойн полного среза funnel_metric (рейтинги, выкупы,
  > Тело: app/static/index.html + app.js + style.css. Тулбар: msg (productsMsg), tip,
  > поиск «Артикул содержит…» (productsLike), чекбоксы «С размерами» (productsSizes),
  > «Показывать остатки» (productsStocks), «Перезапись» (productsOverwrite),
  > toolbar-right: pager, «Вид таблицы»
  > (registerColView('products', {storageKey:'productsCols', headers, optional})),
  > «Выгрузить в Excel», «Загрузить на диск». Кнопка «Обновить» в шапке
  > (#btnUpdateCatalog) рядом с «Применить», видима только на вкладке products
  > (syncHeaderForTab) → POST /api/products/refresh → отчёт в msg.
  > Раскрывающееся меню «Установить цены» (тоггл панели: якоря себестоимости/объёма
  > + округление, localStorage products_price_settings, рекалькуляция таблицы).
  > Обрезка длинных полей до 50 символов + «…» (наименование, состав).
  > Экспорт по видимым колонкам. Сохранить загрузчики товаров/себестоимости.
  > node --check app/static/app.js.
- [T-22] (low) Будущее (в обсуждение): «Карточка товара» по клику на строку в «Товары»
  > Тело: клик по строке каталога → отдельный экран/модал «Карточка товара»: полная
  > информация (наименование, бренд, предмет, состав, объём, фото), движения
  > (приход/отгрузка, продажи WB+Ozon), прибыль, остатки (склад + МП), себестоимость,
  > рекомендуемая цена, история цен (price_changes), размеры/баркоды. После базового
  > каталога. Пока НЕ реализуем.
- [T-23] (low) Будущее (в обсуждение): рекомендуемая цена раздельно по маркетплейсам
  > Тело: сейчас базовая цена общая (base_price = f(себестоимость, объём)). В будущем
  > — свои коэффициенты/формула для WB, Ozon и др. + связка со скидками «Автопилота
  > цен WB» (Прибыльность). Обсудить структуру настроек и экспорт. Пока НЕ реализуем.

## В работе

- [T-28] (medium) Автопилот цен: прозрачный вклад акций WB в целевую скидку
  > Тело: в T-25 было видно только «добор применён» (да/—), а сколько п.п. добавила
  > акция — нигде: `delta_discount` смешивает решение R1-R10 с добавлением акции,
  > поэтому вклад акции вычтем отдельно. Три новых поля строки в `_promo_pass`:
  > `promo_delta_discount` (п.п., сколько добавило правило R11; 0.0 — не влияло),
  > `promo_gap_pp` (разрыв до уровня тира; считается от целевой скидки и
  > пересчитывается после добора, чтобы показывать «хватило ли»), и
  > `promo_boost_gain` (буст наивысшего ranging-тира, в который попадает скидка
  > строки — в отличие от `promo_tier_boost`, это уровень самой акции).
  > Причина переписана: было «добор участия до N%», что читалось как ошибка для
  > строк, уже выше тира (например скидка 27% + добор «до 1%»); теперь
  > «акция WB «Название»: +N п.п. к скидке (уровень N%, буст ×M)».
  > Решения владельца: добор остался плоским (+promo_push_pct), срезание скидки
  > при выходе из акции НЕ делаем — per-product требований WB не отдаёт, порог
  > был бы выдумкой. Добавлено в экспорт Excel + колонки/подсказки UI (v57),
  > 6 новых тестов.

- [T-12] (high) Автопилот цен WB: UI по эталону «Детализация Продаж WB»
  > Тело: тулбар msg/tip/поиск/«Пересчитать»/pager/«Вид таблицы»/«Экспорт
  > в Excel»/«Загрузить на диск»; кнопка «Применить в WB» (disabled-заглушка
  > до T-16); colView base+optional как в margin-detail; подсказка-тип.
- [T-13] (high) Автопилот цен WB: обогащение данных (воронка + детализация)
  > Тело: recommendations() принимает date_from/date_to (шапка) + фолбэк
  > window_days; джойн полного среза funnel_metric (рейтинги, выкупы,
  > отмены ₽, конверсии, вишлисты, share/avg_orders_per_day, stock_wb/mp) и
  > фактических денег из wb_detail_rows через margin_service (return_rate,
  > margin_pct, commission/logistics/storage_per_one, income/revenue_per_one);
  > новые колонки + экспорт + totals.
- [T-14] (medium) Автопилот цен WB: модель решений — модуляторы R1–R10
  > Тело: rating_min, buyout_min_pct, cancel_ratio_max, отд. возвраты из
  > детализации; cap/блок RAISE при низком рейтинге/выкупе/высокой доле
  > отмен; смелее RAISE при высоких выкупах+рейтинге+марже; новые настройки
  > в PRICING_DEFAULTS + labels/hints; юнит-тесты.
- [T-15] (medium) Автопилот цен WB: минимальная цена WB
  > Тело: провайдер get_recommended_prices() (GET /public/v1/info/price,
  > live-проба схемы), clamp target_vis ≥ max(floor_price, min_price),
  > колонка min_price; при отсутствии мин. цен — apply заблокирован.
- [T-16] (medium) Автопилот цен WB: кнопка «Применить скидки в WB»
  > Тело: confirm-диалог + POST /api/pricing/apply (даты+настройки), результат
  > в pricingMsg, журнал/cooldown; обновить подсказку «изменения НЕ вносятся».

## Заблокированные

- [T-26] (medium) Ozon: нативная группировка товаров вместо своей по артикулу
  > Тело: Seller API отдаёт HTTP 429 на `/v2/analytics/stock_on_warehouses`,
  > `/v3/product/info/list` и на предполагаемые `/v1|v2/product/group/*` — наличие
  > нативного поля группировки не подтверждено. Пока 429, своя группировка по
  > `base_article` (T-24) остаётся основной.
  > Ждём: снятие лимита/окна запросов к Ozon API, затем проверка схемы product/group.
- [T-27] (medium) Ozon: размер в карточках — пробросить атрибут из отчёта по товарам
  > Тело: `normalize_oz_cards` кладёт в `marketplace_cards.size` пустую строку, размер
  > для кросс-листингованных с WB товаров выводится из хвоста артикула
  > (JBG-08283-A12753-29 → 29). У Ozon-собственных товаров хвост — это цвет
  > (TIE-BIGBAN-17-PINKMILK), поэтому размер пуст: placements 68%, stocks 81%,
  > detail 85% строк (при `marketplace_cards` — всего 1.8%). Правка `build_offer_map`
  > (добирать размер из артикула при пустом размере карточки) даёт 0 прироста —
  > проверили на 41 577 офферах.
  > Проба: `scripts/probe_ozon_card_attrs.py` (своя ограниченная попытка, не ждёт
  > отчёт до 25×20 с). Если в CSV `/v1/report/products/create` есть колонка размера —
  > пробросить её в `normalize_oz_cards`, затем перебэкфиллить.
  > Ждём: снятие лимита Ozon (сейчас 429 / отчёт не готов за 40 с — проба падает).

## Закрытые

- [T-25] (closed) WB «Календарь акций» → Автопилот цен, правило R11 (таблица `wb_promotions`, `pull_wb_promotions` + шаг плана [−30д, +90д], `pricing._promo_pass`, `POST /api/promo/refresh`, `GET /api/promo/list`, UI «Акции WB», 16 тестов) — commit `a0eb41e`
- [T-24] (closed) Ozon: своды и маржа по базовому артикулу товара + режим «в разрезе размеров» (резолвер `ozon_article`, `base_article`/`size` в 7 таблицах, бэкфилл 90 821 строка, дашборд/размещение/детализация; переключатель у каждого свода свой, в панели фильтров) — commit `a0eb41e`

- [T-21] (closed) Каталог: UI «Наш склад → Товары» по эталону «Детализация Продаж WB» — commit d13822f
- [T-19] (closed) Каталог: API-эндпоинты (/api/products, refresh, preview, price-settings, export) — commit `b5ce906`
- [T-18] (closed) Каталог: синк карточек WB+Ozon в общий каталог (идентификация, алиасы, overwrite) — commit `e5d6a37`
- [T-20] (closed) Каталог: модуль рекомендуемой (базовой) цены base_price — commit `3d46241`
- [T-17] (closed) Каталог «Наш склад → Товары»: схема БД + миграция — commit `bc5c756`, миграция `c0b6c3a`
- [T-7] (closed) Ozon: провайдер — детализация реализаций по постингам + выкупы (get_sales_detail/get_buyout, фолбэк на /v1/report/*, live-проверка на Client-Id 164497) — commit `44b29ae`
- [T-8] (closed) Ozon: таблицы OzonDetailRow/OzonBuyout + синк + API-эндпоинты — commit `44b29ae`
- [T-9] (closed) UI — вкладка «Детализация продаж Ozon» (oz-detail) по образцу WB (rows/summary, экспорт, диск, поиск) — commit `44b29ae`
- [T-10] (closed) UI — oz-cards/oz-stock/oz-prices/oz-realization: полные таблицы из БД (поиск, свёртка, «Вид таблицы», экспорт по видимым колонкам, диск); pull_oz_cards пишет marketplace_cards, /api/prices и экспорт учитывают marketplace — commit `44b29ae`
- [T-11] (closed) «Магия → Обновить Ozon»: детализация реализаций + выкупы через include_detail (plan steps detail/buyout, чекбокс в модалке для ozon) — commit `44b29ae`
- [T-5] (closed) WB API → Воронка продаж: все поля отчёта (карточка, остатки, конверсии, WB Клуб, время доставки, raw JSON) в базу и UI — commit `2a34949`
- [T-6] (closed) UI — единый тулбар во всех разделах WB API (дубли кнопок убраны, «Обновить базу» в шапке для всех wb-вкладок) — commit `d1c974b`
- [T-4] (closed) WB API → Воронка продаж: автоподбор ключа для отчёта + унификация UI с «Детализация продаж» — commit `a1ce2db`
- [T-3] (closed) UI — «Вид таблицы» во все разделы (WB API + Прибыльность) + экспорт по видимым колонкам + «Загрузить на диск» в Прибыльности — commit `ae302eb`
- [T-2] (closed) UI — гармонизировать разделы WB API с «Детализация продаж» — `d7bc7b0`
