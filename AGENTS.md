# Проект agent_market

Локальный агрегатор маркетплейсов Wildberries / Ozon: панель статистики продаж,
маржинальности, остатков, карточек, прайсов. Backend FastAPI (app/), фронтенд —
статические файлы (app/static/), БД PostgreSQL.

## Тикеты

Единая очередь задач проекта — файл `TICKETS.md` в корне репо.

- Перед началом любой разработки прочитать `TICKETS.md`.
- Работа идёт только через тикеты: взять открытый тикет с наивысшим
  приоритетом → перевести в «В работе» → код + тесты → коммит
  `T-<N>: описание` → перенести тикет в «Закрытые» (с хешем коммита).
- Если тикету нужны внешние данные/доступ (токен, доступ «Статистика»,
  решение владельца) → «Заблокированные» + описание ожидания.
- В «В работе» одновременно может быть несколько тикетов.
- Тикеты ведутся через вкладку «Тикеты» в веб-приложении (кнопки
  «Добавить/Взять в работу/Закрыть») или правкой `TICKETS.md` — оба
  способа работают с одним файлом.
- Новый номер тикета берётся из счётчика в шапке `TICKETS.md`,
  после выдачи счётчик увеличивается на 1.
- Если задача поступила устно («добавь тикет: …») — оформить её в файл
  и работать по нему же.

## Запуск

- Сервер: `& "C:\python_projects\agent_market\venv\Scripts\python.exe" app/main.py`
  из `C:\python_projects\agent_market` (адрес/порт — `settings.app_host/app_port`,
  по умолчанию `127.0.0.1:8000`).
- Detached (фоновый сервер, как запускается в сессиях):
  ```
  Start-Process -FilePath "C:\python_projects\agent_market\venv\Scripts\python.exe" `
    -ArgumentList "app/main.py" -WorkingDirectory "C:\python_projects\agent_market" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "C:\Users\1\AppData\Local\Temp\opencode\agent_market_server.log" `
    -RedirectStandardError "C:\Users\1\AppData\Local\Temp\opencode\agent_market_server.err.log"
  ```
- Остановка: найти процесс, слушающий порт 8000 (`Get-NetTCPConnection -LocalPort 8000 -State Listen`),
  и `Stop-Process -Id <pid> -Force`.
- Логи сервера: `C:\Users\1\AppData\Local\Temp\opencode\agent_market_server.log` / `.err.log`.

## Тесты

- Код в `tests/` (unit + api через TestClient).
- Команда: `& "C:\python_projects\agent_market\venv\Scripts\python.exe" -m pytest -q` из
  `C:\python_projects\agent_market`.
- Тестовые данные/фейки — `tests/fakes.py` (мок WB/Ozon API, когда `testing_mode`).
- Фронтенд-линта/typecheck нет; синтаксис JS при необходимости: `node --check app/static/app.js`.

## БД

- PostgreSQL, строка подключения — `settings.database_url` (по умолчанию `agent_market` на localhost:5432).
- Схема: `app/models.py` (SQLAlchemy), engine — `app/database.py`.
- Миграции/создание таблиц: `scripts/init_db.py` (`create_all` + идемпотентные
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` для новых колонок).

## Примечания

- Реальные ключи WB/Ozon: `app/config.py` (`Settings`, читается из `.env`):
  `wb_api_key`, `wb_finance_api_key`, `wb_finance_api_key_2` (резервный финансовый
  для ротации при 429), `ozon_client_id`, `ozon_api_key`. Ключи в логи не писать.
- Ротация финансовых ключей: `WbProvider._session_post(..., finance=True)` при ответе
  429 делает одну попытку вторым ключом (`WB_FINANCE_API_KEY_2`), затем как раньше.
  Тесты: `tests/unit/test_providers_errors.py` (мок `requests.post`).
- WB «Выкупы»/«Сумма выкупа» в воронке: WB не отдаёт их через текущий токен
  (`statistics-api .../api/v1/main` отвечает 404 — нужен доступ «Статистика»).
  Колонки показывают «—» и заметку; заполнятся после получения доступа и нового скачивания воронки.
- Ручная загрузка «Детализации продаж» WB (Excel/zip): `POST /api/wb/detail-upload`
  (обход лимитов finance-api; файл из ЛК WB).