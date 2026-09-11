# Agent Market

Веб-приложение для анализа прибыльности продаж на маркетплейсах
(Wildberries и Ozon). FastAPI + PostgreSQL + pandas; Excel-импорт/экспорт,
расчёт маржинальности, панельные выгрузки из API и кнопка массового обновления.

## Запуск

```powershell
venv\Scripts\python.exe app\main.py   # http://127.0.0.1:8000
```

Перед первым запуском выполнить `scripts/setup_pg.*` (создание БД) и
`scripts/load_sample.py` (демо-данные). Ключи API — в `.env` (см. `.env.example`
при наличии). `.env` в git не коммитится.

## Разработка

### Git (только локальный)

```powershell
git init
git add -A
git commit -m "Initial commit"
```

Ветка `master`, история хранится локально. В `.gitignore` исключены
`.env`, `venv/`, `data/`, `.pytest_cache/`, `.coverage`, `htmlcov/`, `*.log`.

### Тесты

Тесты пишутся в отдельную БД `<PG_DATABASE>` (по умолчанию
`agent_market_test`), рабочая БД не затрагивается. База создаётся
автоматически при первом запуске; постгрес-пользователь из `.env` должен
иметь право `CREATE DATABASE`.

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest                    # все тесты
venv\Scripts\python.exe -m pytest tests\unit -v      # unit
venv\Scripts\python.exe -m pytest tests\api -v       # API (TestClient + тестовая БД)
venv\Scripts\python.exe -m pytest tests\e2e -v       # сквозной сценарий (маркер e2e)
venv\Scripts\python.exe -m pytest --cov=app          # с покрытием
```

Структура:

- `tests/unit/` — чистые юнит-тесты (формулы, нормализация, планы, ошибки);
- `tests/api/` — HTTP-эндпоинты и фоновые задания обновления;
- `tests/e2e/` — сквозной цикл «панели + массовое обновление»;
- `tests/js/` — проверка `app/static/app.js` через `node --check` и Node `vm`.

Провайдеры маркетплейсов в тестах заменяются фейками (`tests/fakes.py`)
через единую точку `app/providers/factory.py`; реальные HTTP-вызовы не
выполняются. Координаты тестовой БД подменяются в `tests/conftest.py`
(переменная окружения `PG_DATABASE`).

### Проверка перед коммитом

```powershell
venv\Scripts\python.exe -m py_compile app\*.py app\services\*.py app\providers\*.py
node --check app\static\app.js
venv\Scripts\python.exe -m pytest
```