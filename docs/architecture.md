# Архитектура agent_market

Как устроен код: слои, точки входа, где что менять. README отвечает на
«как запустить и что умеет продукт», этот документ — на «почему код разложен
именно так».

## Общая схема

```
браузер (app/static/app.js + index.html)
        │  fetch() к /api/*
        ▼
FastAPI router — app/api.py  (все эндпоинты, один модуль;
        │                 карта — docs/api.md, перегенерировать скриптом)
        │
        ├── app/providers/  — HTTP к маркетплейсам (wb.py, ozon.py)
        ├── app/services/   — бизнес-логика, единственное место расчётов
        └── app/models.py   — 25 ORM-моделей
                │
                ▼
        PostgreSQL (app/database.py)
```

Правило слоёв: `api.py` не считает, сервисы не знают про HTTP-ответы,
провайдеры не пишут в БД. Логика живёт только в `app/services/`.

## Ключевые файлы

Таблицу пересчитывает `python scripts/gen_api_map.py` (она между маркерами
ниже — строки меняются часто, руками не править). Тот же скрипт генерирует
[docs/api.md](api.md) — полную карту эндпоинтов из реестра роутов FastAPI;
`--check` возвращает exit 1, если документы устарели.

<!-- BEGIN:file-stats -->
| Файл | Строк | Роль |
|---|---|---|
| `app/static/app.js` | 6603 | весь фронтенд (вкладки, таблицы, fetch к /api) |
| `app/api.py` | 4167 | все HTTP-эндпоинты, один `APIRouter(prefix="/api")` |
| `app/services/sync.py` | 2462 | запись выгрузок WB/Ozon в БД |
| `app/services/pricing.py` | 1789 | автопилот цен: R1–R11, расчёт скидок |
| `tests/api/test_endpoints.py` | 1340 | HTTP-эндпоинты через TestClient |
| `app/services/refresh.py` | 1107 | `pull_*`-функции, фоновые задания, «Обновить WB/Ozon» |
| `app/static/style.css` | 1102 | стили (панели «Вид таблицы», тулбары, таблицы) |
| `app/services/replenish.py` | 1025 | подсортировка WB, план дефицита, скорость продаж |
| `app/providers/wb.py` | 976 | HTTP к Wildberries |
| `app/providers/ozon.py` | 934 | HTTP к Ozon |
| `app/static/index.html` | 910 | разметка: навигация `data-tab` + секции `tab-*` |
| `tests/unit/test_margin.py` | 878 | юнит-тесты маржинальности |
| `app/services/margin.py` | 772 | маржинальность по детализациям, группировка артикулоразмеров |
| `app/models.py` | 673 | SQLAlchemy-модели (схема БД) |
| `app/services/pdf_demand.py` | 623 | PDF «Потребность в товаре» |
| `tests/js/test_js_helpers.py` | 609 | проверка app.js через Node `vm` |
| `app/services/tickets.py` | 557 | реестр тикетов `TICKETS.md` (create/validate/report) |
| `app/services/dashboard.py` | 501 | сводные панели дашборда |
| `app/services/warehouse.py` | 467 | складские документы (приход/отгрузка, обороты) |
| `app/services/ozon_article.py` | 368 | резолвер артикула/размера Ozon (`base_article`) |
| `app/services/photos.py` | 255 | индекс фото на диске, выбор папки, fallback по префиксу |
| `scripts/load_sample.py` | 216 | демо-данные |
| `app/services/base_price.py` | 178 | рекомендуемая (базовая) цена |
| `app/services/thumbs.py` | 144 | миниатюры PDF в `data/thumbs/` |
| `app/database.py` | 144 | engine/session PostgreSQL |
| `app/services/excel_import.py` | 138 | разбор отредактированного Excel «Потребность» |
| `scripts/init_db.py` | 115 | создание схемы + идемпотентные ALTER |
| `app/services/yandex_disk.py` | 108 | загрузка отчётов на Яндекс.Диск |
| `app/services/funnel.py` | 98 | воронка продаж (агрегация) |
| `app/services/common.py` | 76 | общие хелперы (wildcard-фильтр `like_*`, окна) |
| `app/services/excel_io.py` | 73 | чтение/запись Excel |
| `app/config.py` | 55 | `Settings`, читается из `.env` |
| `app/providers/errors.py` | 48 | нормализованные ошибки апстримов |
| `app/main.py` | 44 | сборка FastAPI-приложения, `/`, статика |
| `app/providers/base.py` | 33 | базовый провайдер |
| `app/services/window.py` | 21 | окна дат |
| `app/providers/factory.py` | 20 | точка подмены провайдеров в тестах |
<!-- END:file-stats -->

Два самых больших модуля — `api.py` и `app.js`. Это известная особенность
проекта, а не случайность: разделение по фичам делалось в services, а
роуты и UI добавлялись рядом с существующими. Если появится задача
«разделить api.py» — это отдельный тикет.

Куда класть новый код:

- расчёт, обогащение данных, любая логика → `app/services/<фича>.py`;
- HTTP к маркетплейсу → `app/providers/` (через `factory.py`, чтобы тесты
  подменялись фейками);
- новый эндпоинт → конец `app/api.py`, префикс `/api` уже у роутера;
- таблица во фронтенде → существующий `pagedTable` (памятка в `AGENTS.md`,
  раздел «UI таблиц»), колонки и `tip` — в `app/static/app.js`;
- миграция колонки → идемпотентный `ALTER ... IF NOT EXISTS` в
  `scripts/init_db.py`.

## Провайдеры

`app/providers/` изолирует HTTP маркетплейсов. Точка подмены одна —
`factory.py` с функциями `get_wb_provider()` / `get_oz_provider()`:
`tests/conftest.py::patch_factory` подменяет их на фейки из `tests/fakes.py`
через `monkeypatch`, поэтому реальные сетевые вызовы в тестах не
выполняются.

Ошибки нормализованы в `errors.py` (`MarketError`, `WbApiError`,
`OzonApiError`) — вызывающий код ловит общий тип и не знает, чей это был
апстрим. Отсюда же следует блокер T-26: Ozon отдаёт HTTP 429, и это
видно как один тип ошибки, а не как два разных пути.

## Данные и их источник

Модели в `app/models.py`. Ключевые группы:

- **Каталог** — `Product`, `ProductSize`, `ProductAlias`, `NmArticle`,
  `MarketplaceCard` (сырые карточки товара), `Counterparty`.
- **Продажи** — `Sale`, `WbDetailRow`, `OzonDetailRow` (детализация),
  `FunnelMetric` (воронка), `Promotion`.
- **Остатки** — `Stock`, `CustomStock`, `OzonPlacement`, `StorageCost`,
  `WarehouseDoc` / `WarehouseDocItem`.
- **Деньги** — `OzonCashFlow`, `OzonAccrual`, `OzonBuyout`.
- **Цены** — `PriceSnapshot` (`price_snapshots`), `PriceChange` (журнал
  решений), `BasePrice`-логика в `app/services/base_price.py`.
- **Служебное** — `RefreshRun`, `ApiPull`.

### Цены читаются из снимка БД, а не из API

`price_snapshots` — источник правды для расчёта рекомендаций; live API WB
используется как fallback. Это дало 8–9 с вместо минут на расчёт и
покрытие 100% цен / 99.6% скидок. Обратно не меняем: pandas-маски и
multiprocessing сознательно не оптимизировались.

## Обновление данных

`refresh.py` содержит `pull_*` функции — единый код записи в БД, который
используют и хендлеры панелей, и кнопки «Обновить WB / Ozon». Фоновые
задания идут через `threading` последовательно по видам: ошибка одного вида
не блокирует остальные. История запусков — `refresh_runs`.

## Тикеты как реестр

`TICKETS.md` — реестр проекта, а не блокировка. Номера в файле **не
хранятся**: метку выдаёт имя git-ветки (`t-<N>-<слаг>`), свободный номер
считается как max+1. Так нельзя разойтись со счётчиком, потому что
счётчика нет.

```powershell
.\venv\Scripts\python.exe -m app.services.tickets validate   # exit 1 = проблемы
.\venv\Scripts\python.exe -m app.services.tickets report     # что в работе / ждёт / сделано
.\venv\Scripts\python.exe -m app.services.tickets branch t-37-cenyi-wb
```

## Запутанные места

Здесь стоит знать заранее, прежде чем чинить что-то «непонятное»:

- **`api.py` и `app.js` — общие файлы для всех задач.** Правки с разных
  задач сталкиваются в одном месте; коммиты часто бандлятся.
- **Кодировка.** PowerShell 5.1 читает файлы как cp1251, и round-trip
  `Get-Content | Set-Content` портит кириллицу (бывали BOM и «krakozyabry»).
  Правила и команда самопроверки — в `AGENTS.md`, раздел «Кодировка».
- **Тесты бьют общую БД.** Параллельные `pytest` дедлочат
  `agent_market_test` — гонять полный набор в одиночку.
- **Известные падения.** T-35 (2 предсуществующих) и T-36 (3 зависящих от
  порядка прогонов) заведены как тикеты, а не починены «молча».
- **Кэш фото.** Индекс держится в памяти 10 минут, миниатюры — в
  `data/thumbs/` (папка не коммитится). После правок индекс сбрасывается
  по таймауту, а не сразу.

## Разработка

```powershell
.\venv\Scripts\python.exe -m pytest -m fast -q   # быстрый цикл (< 20 с)
.\venv\Scripts\python.exe -m pytest tests\unit -q # только unit
.\venv\Scripts\python.exe -m pytest               # всё (~2 мин)
node --check app\static\app.js
python scripts\gen_api_map.py --check            # docs не протухли?
```

Ручные проверки перед коммитом — единый чек-лист в [AGENTS.md](../AGENTS.md)
(раздел «Перед коммитом»): именно он канонический, этот файл его не дублирует.

Перед началом работы обязательно прочитать `TICKETS.md` и проверить
`git status` — в проекте могут идти параллельные задачи, и файлы
`app/api.py`, `app/static/app.js`, `app/services/replenish.py` пересекаются
между ними.
