# Архитектура agent_market

Как устроен код: слои, точки входа, где что менять. README отвечает на
«как запустить и что умеет продукт», этот документ — на «почему код разложен
именно так».

## Общая схема

```
браузер (app/static/app.js + index.html)
        │  fetch() к /api/*
        ▼
FastAPI router — app/api.py  (112 эндпоинтов, один большой модуль)
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

| Файл | Строк | Роль |
|---|---|---|
| `app/api.py` | 3820 | все HTTP-эндпоинты, `prefix="/api"` |
| `app/static/app.js` | — | весь фронтенд в одном файле |
| `app/services/sync.py` | 2247 | запись выгрузок WB/Ozon в БД |
| `app/services/pricing.py` | 1604 | автопилот цен: R1–R10, расчёт скидок |
| `app/services/refresh.py` | 963 | массовое обновление, фоновые задания |
| `app/services/margin.py` | 785 | маржинальность, группировка артикулоразмеров |
| `app/services/replenish.py` | 699 | подсортировка, скорость продаж |
| `app/services/dashboard.py` | 451 | сводные панели |
| `app/services/tickets.py` | 456 | реестр тикетов `TICKETS.md` |
| `app/services/pdf_demand.py` | 495 | выгрузка потребности в PDF |
| `app/providers/wb.py` | 916 | HTTP к Wildberries |
| `app/providers/ozon.py` | 885 | HTTP к Ozon |

Два самых больших модуля — `api.py` и `app.js`. Это известная особенность
проекта, а не случайность: разделение по фичам делалось в services, а
роуты и UI добавлялись рядом с существующими. Если появится задача
«разделить api.py» — это отдельный тикет.

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
  `Get-Content | Set-Content` портит кириллицу. Править файлы только
  через редактор/инструменты, не PowerShell-потоками.
- **Тесты бьют общую БД.** Параллельные `pytest` дедлочат
  `agent_market_test` — гонять полный набор в одиночку.
- **Известные падения.** T-35 (2 предсуществующих) и T-36 (3 зависящих от
  порядка прогонов) заведены как тикеты, а не починены «молча».
- **Кэш фото.** Индекс держится в памяти 10 минут, миниатюры — в
  `data/thumbs/` (папка не коммитится). После правок индекс сбрасывается
  по таймауту, а не сразу.

## Разработка

```powershell
.\venv\Scripts\python.exe -m pytest tests\unit -q      # быстрый цикл
.\venv\Scripts\python.exe -m pytest                    # всё
node --check app\static\app.js
```

Ручные проверки перед коммитом:

```powershell
git status --short                 # нет ли чужих незакоммиченных файлов
.\venv\Scripts\python.exe -m app.services.tickets validate
```

Перед началом работы обязательно прочитать `TICKETS.md` и проверить
`git status` — в проекте могут идти параллельные задачи, и файлы
`app/api.py`, `app/static/app.js`, `app/services/replenish.py` пересекаются
между ними.
