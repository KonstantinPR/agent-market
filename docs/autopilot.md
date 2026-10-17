# Автопилот цен Wildberries — документация

Раздел «Прибыльность ▸ Автопилот цен WB». Движок: `app/services/pricing.py`.
Тесты: `tests/unit/test_pricing.py`. UI-параметры: `app/static/app.js`.

Автопилот по каждому товару WB решает, **оставить ли скидку как есть, поднять
цену или опустить**, и на сколько. Применение меняет **только скидку** —
базовая цена карточки (price) не трогается.

---

## 1. Из чего складывается решение

Для каждого товара собираются сигналы:

| Сигнал | Источник | Производная метрика |
|---|---|---|
| Остатки | `stock` (quantity + in_way, последний срез) | `DOC` — дней на складе |
| Продажи | `sale` (источники кроме «detail»), окно анализа | скорость `v_now`, тренд, `v_proj` |
| Воронка | последний срез `funnel_metric` | конверсия, «в корзине», рейтинги, средняя цена продажи |
| Детализация | `margin_detail` за окно | фактическая маржа, возвраты, средний чек |
| Unit-экономика | продажи за 90 дней (fallback) | комиссия, логистика, хранение, себестоимость → `floor_price` |
| Цены WB | провайдер (прайс) | базовая `price`, текущая скидка `discount`, витрина `cur_vis` |
| Мин. цена WB | провайдер (`price_minimum` по nmID) | `min_price` — клампинг LOWER (опция) |
| Докупаемость | товар (`replenishable`) | различие «регулярные поставки» / «последние единицы» |
| Ценность | товар (`value` из products.xlsx, колонка `pure_value`) | `value_scale` — ограничивает скидку при LOWER |

Итоговая строка решения содержит: `action` (RAISE / LOWER / HALVE / HOLD / SKIP),
`target_discount` и `target_vis` (целевая скидка и витринная цена после неё),
`margin_pct_at_target` (ожидаемая маржа), `reason` (почему).
В строку также выводятся справочные `value` (ценность) и `supplier` (поставщик) из каталога.

---

## 2. Окна и период анализа

- Окно по умолчанию — `window_days = 14` дней, якорь на «сегодня».
- Либо период из шапки: `date_from` / `date_to` (перекрывает дефолт).
- Если истории продаж меньше окна — берётся сколько есть; если покрыто
  `< min_days_with_sales (5)` дней → строка «Мало истории продаж WB».
- Скорость считается по **двум соседним окнам** одинаковой ширины:
  - `now` = последние `cover` дней запрошенного периода;
  - `prev` = `cover` дней до `now` (сразу перед ним).

---

## 3. Производные метрики (формулы)

Обозначения: `price` — базовая цена WB; `cur_disc` — текущая скидка, %;
`cur_vis = price × (1 − cur_disc/100)` — текущая витринная цена.

```
v_now   = Σ(quantity − returns_qty за окно now) / cover        # шт/день
v_prev  = Σ(quantity − returns_qty за окно prev) / cover
trend   = v_now / v_prev                                        # 1.0, если v_prev = 0
```

**Прогноз скорости** (`project_velocity`), тренд/сезонность:

```
если не season_adj или v_now ≤ 0:        v_proj = v_now
если v_prev ≤ 0:                         v_proj = v_now
иначе:                                   g = clamp(v_now / v_prev, 0.5, 2.0)
                                         v_proj = v_now × g^season_damp   # 0.5 — компромисс
```

**Остаток и DOC:**

```
stock   = Σ(quantity + in_way) по товару на последний срез
DOC     = stock / v_proj               # дней хватит запаса
          (если v_proj ≤ 0 или stock нет — DOC считается ∞)
```

**Воронка:**

```
conv     = orders / views × 100, если views > 0, иначе 0          # %
backlog  = max(adds − orders, 0)                                   # «в корзине, но не купили»
hot      = (conv ≥ hot_conv_pct) ИЛИ (adds ≥ hot_backlog_factor × orders)
returns_ratio = (returns_qty_now + cancelled) / qty_sold_now       # пена возвратов/отмен
avg_price     = средняя цена из воронки (реальные продажи)
```

**Unit-экономика** (за `fallback_window_days = 90` дн):

```
comm_rate      = |commission| / revenue, кап 0.5
logistics_unit = |logistics| / qty          # ₽/шт
storage_unit   = |storage|    / qty         # ₽/шт
other_unit     = |services|   / qty         # ₽/шт
```

**Пол цены (break-even)** — ниже автопилот никогда не опускается:

```
floor = (logistics_unit + storage_unit + other_unit + net_cost)
        / (1 − comm_rate − floor_margin_pct/100)
        # только если знаменатель > 0.05
```

**Потолок скидки для товара:**

```
max_disc_item = min(max_discount_pct, (1 − floor/price) × 100)   # при use_margin
              = max_discount_pct                                   # без use_margin
```

**Базы для расчёта целевой цены:**

```
eff      = min(cur_vis, avg_price), если avg_price > 0, иначе cur_vis   # база для LOWER
factual  = avg_price, если avg_price > 0, иначе eff                      # для правила «половинного капа»
```

**Рейтинг по отзывам → ограничение скидки при снижении** (ценный товар):

```
rating_scale = 1.0,                если rating ≤ 0 или rating ≤ min_rating_reviews
             = 0.0,                если rating ≥ 5.0
             = (5 − rating)/(5 − min_rating_reviews),      иначе (линейно)
```

При `use_reviews` выключен — `rating_scale = 1.0` (рейтинг не ограничивает).

**Ценность/себестоимость → ограничение скидки при снижении** (ценный товар):

```
value_scale = 1.0,                если value ≤ 0 или net_cost ≤ 0 (нет данных)
            = 1.0,                если value/net_cost ≤ value_ratio_low
            = 0.0,                если value/net_cost ≥ value_ratio_high
            = (value_ratio_high − value/net_cost) / (value_ratio_high − value_ratio_low), иначе
```

`value` — ценность товара из каталога (products.xlsx, колонка `pure_value`),
`net_cost` — себестоимость. Чем выше ценность относительно себестоимости, тем
«ценнее» товар и тем меньше скидка при снижении.

Оба фактора объединяются в общий множитель снижения:
`item_scale = rating_scale × value_scale`. Если `item_scale ≤ 0.01` — срабатывает
LOWER-обязательный шаг «ценный товар» (п.4.3 и п.4.6).

**Противовес (prefer_raise)** сдвигает все границы решения:

```
bias       = prefer_raise_bias (0.15), если prefer_raise включён, иначе 0
doc_low'   = doc_low  × (1 + bias)
doc_high'  = doc_high × (1 + bias)
max_drop'  = max_drop_pct × (1 − bias)     # шаг снижения мягче
```

---

## 4. Дерево решений (по порядку для каждого товара)

Номер — порядок проверки в коде; на первом сработавшем условии — `return`.

### 0. Отсевы ДО решения

| Условие | Результат |
|---|---|
| Товар вне WB-карточек (нет nmID в списке цен) | не строка/в `non_wb` |
| Нет карточки/цен WB в прайсе | `SKIP` — «нет карточки/цен WB» |
| Нет «сигнала жизни» (продажи/остаток/воронка) и `show_zero = false` | товар скрыт (`hidden_dead`) |
| Без «сигнала жизни», но `show_zero = true` (или при **Применить**) | см. п.4.1 `HALVE` |

> «Мёртвый товар» (HALVE) — нет ни одного сигнала. «Мёртвый запас» (п.4.3) —
> есть остаток, но продаж не было N дней — это разные вещи, пути разные.

### 4.1. Мёртвый товар — HALVE («скидка ÷2»)

```
half   = ⌊cur_disc / 2⌋
target = max(min(half, cur_disc), dead_min_discount)     # dead_min_discount = 1%
step   = cur_disc − target
если step < min_delta_pp → HOLD «скидка уже минимальна»
иначе → HALVE, target_discount = target
```

Цепочка: `50 → 25 → 12 → 6 → 3 → 1`. Каждый прогон «Применить» делает один шаг,
пока скидка не станет минимальной. Это мягкий противовес автоскидкам WB
(цена постепенно восстанавливается у мёртвых).
**HALVE не попадает под кулдаун** (см. п.7).

### 4.2. Базовые проверки живого товара

| Условие | Результат |
|---|---|
| `use_inventory` и нет данных об остатках | `SKIP` — «нет данных об остатках» |
| `use_inventory` и остаток = 0 | `HOLD` — «распродан» |
| `use_margin` и нет unit-экономики (нет продаж/доходов) | `SKIP` — «нет unit-экономики» |
| `use_returns` и `returns_ratio > return_penalty (0.3)` | `SKIP` — «возвраты/отмены, спрос мыльный» |
| `use_orders`, много «в корзине» (`backlog по порогу hot_backlog_factor`) и `conv < low_conv_pct (0.7%)` | `SKIP` — «много в корзинах, но не покупают» |

### 4.3. Мёртвый запас (`use_sales`, продажи ≈ 0, есть остаток)

Условие входа: `v_proj ≤ 0`.

```
если last_sale_days_ago ≥ dead_stock_days (7):
    если item_scale ≤ 0.01 (рейтинг 5.0 или ценность ≥ порога):  LOWER-обязательный шаг «ценный товар» (п.5)
    иначе:
        target_vis = max(floor, eff × (1 − max_drop'×item_scale/100), min_price)
        target_vis = min(target_vis, cur_vis)
        target_vis = max(target_vis, price × (1 − max_disc_item/100))
        new_disc   = (1 − target_vis/price) × 100
        если new_disc − cur_disc ≥ min_delta_pp → LOWER (обычный)
        иначе → LOWER-обязательный шаг «мёртвый запас»
иначе (продажи были недавно, но в окне нули):
        → LOWER-обязательный шаг «продаж нет (менее порога)»
```

### 4.4. Факторы выключены (`use_sales` = off, `use_inventory` = off)

Ни один сигнал не даёт направления, но **правило «есть остаток — коррекция
обязательна»** всё равно действует:

```
без остатка        → HOLD (нет сигнала)
остаток > 0        → LOWER-обязательный шаг, если DOC ≥ target_doc
                     RAISE-обязательный шаг, если DOC < target_doc
```
Автопилот цен Wildberries — документация
Раздел «Прибыльность ▸ Автопилот цен WB». Движок: app/services/pricing.py. Тесты: tests/unit/test_pricing.py. UI-параметры: app/static/app.js.

Автопилот по каждому товару WB решает, оставить ли скидку как есть, поднять цену или опустить, и на сколько. Применение меняет только скидку — базовая цена карточки (price) не трогается.

1. Из чего складывается решение
Для каждого товара собираются сигналы:

Сигнал	Источник	Производная метрика
Остатки	stock (quantity + in_way, последний срез)	DOC — дней на складе
Продажи	sale (источники кроме «detail»), окно анализа	скорость v_now, тренд, v_proj
Воронка	последний срез funnel_metric	конверсия, «в корзине», рейтинги, средняя цена продажи
Детализация	margin_detail за окно	фактическая маржа, возвраты, средний чек
Unit-экономика	продажи за 90 дней (fallback)	комиссия, логистика, хранение, себестоимость → floor_price
Цены WB	провайдер (прайс)	базовая price, текущая скидка discount, витрина cur_vis
Мин. цена WB	провайдер (price_minimum по nmID)	min_price — клампинг LOWER (опция)
Докупаемость	товар (replenishable)	различие «регулярные поставки» / «последние единицы»
Ценность	товар (value из products.xlsx, колонка pure_value)	value_scale — ограничивает скидку при LOWER
Итоговая строка решения содержит: action (RAISE / LOWER / HALVE / HOLD / SKIP), target_discount и target_vis (целевая скидка и витринная цена после неё), margin_pct_at_target (ожидаемая маржа), reason (почему). В строку также выводятся справочные value (ценность) и supplier (поставщик) из каталога.

2. Окна и период анализа
Окно по умолчанию — window_days = 14 дней, якорь на «сегодня».
Либо период из шапки: date_from / date_to (перекрывает дефолт).
Если истории продаж меньше окна — берётся сколько есть; если покрыто < min_days_with_sales (5) дней → строка «Мало истории продаж WB».
Скорость считается по двум соседним окнам одинаковой ширины:
now = последние cover дней запрошенного периода;
prev = cover дней до now (сразу перед ним).
3. Производные метрики (формулы)
Обозначения: price — базовая цена WB; cur_disc — текущая скидка, %; cur_vis = price × (1 − cur_disc/100) — текущая витринная цена.

v_now   = Σ(quantity − returns_qty за окно now) / cover        # шт/день
v_prev  = Σ(quantity − returns_qty за окно prev) / cover
trend   = v_now / v_prev                                        # 1.0, если v_prev = 0
Прогноз скорости (project_velocity), тренд/сезонность:

если не season_adj или v_now ≤ 0:        v_proj = v_now
если v_prev ≤ 0:                         v_proj = v_now
иначе:                                   g = clamp(v_now / v_prev, 0.5, 2.0)
                                         v_proj = v_now × g^season_damp   # 0.5 — компромисс
Остаток и DOC:

stock   = Σ(quantity + in_way) по товару на последний срез
DOC     = stock / v_proj               # дней хватит запаса
          (если v_proj ≤ 0 или stock нет — DOC считается ∞)
Воронка:

conv     = orders / views × 100, если views > 0, иначе 0          # %
backlog  = max(adds − orders, 0)                                   # «в корзине, но не купили»
hot      = (conv ≥ hot_conv_pct) ИЛИ (adds ≥ hot_backlog_factor × orders)
returns_ratio = (returns_qty_now + cancelled) / qty_sold_now       # пена возвратов/отмен
avg_price     = средняя цена из воронки (реальные продажи)
Unit-экономика (за fallback_window_days = 90 дн):

comm_rate      = |commission| / revenue, кап 0.5
logistics_unit = |logistics| / qty          # ₽/шт
storage_unit   = |storage|    / qty         # ₽/шт
other_unit     = |services|   / qty         # ₽/шт
Пол цены (break-even) — ниже автопилот никогда не опускается:

floor = (logistics_unit + storage_unit + other_unit + net_cost)
        / (1 − comm_rate − floor_margin_pct/100)
        # только если знаменатель > 0.05
Потолок скидки для товара:

max_disc_item = min(max_discount_pct, (1 − floor/price) × 100)   # при use_margin
              = max_discount_pct                                   # без use_margin
Базы для расчёта целевой цены:

eff      = min(cur_vis, avg_price), если avg_price > 0, иначе cur_vis   # база для LOWER
factual  = avg_price, если avg_price > 0, иначе eff                      # для правила «половинного капа»
Рейтинг по отзывам → ограничение скидки при снижении (ценный товар):

rating_scale = 1.0,                если rating ≤ 0 или rating ≤ min_rating_reviews
             = 0.0,                если rating ≥ 5.0
             = (5 − rating)/(5 − min_rating_reviews),      иначе (линейно)
При use_reviews выключен — rating_scale = 1.0 (рейтинг не ограничивает).
Ценность/себестоимость → ограничение скидки при снижении (ценный товар):

value_scale = 1.0,                если value ≤ 0 или net_cost ≤ 0 (нет данных)
            = 1.0,                если value/net_cost ≤ value_ratio_low
            = 0.0,                если value/net_cost ≥ value_ratio_high
            = (value_ratio_high − value/net_cost) / (value_ratio_high − value_ratio_low), иначе
value — ценность товара из каталога (products.xlsx, колонка pure_value), net_cost — себестоимость.
Чем выше ценность относительно себестоимости, тем «ценнее» товар и тем меньше скидка при снижении.
Оба фактора объединяются: item_scale = rating_scale × value_scale. Если item_scale ≤ 0.01 — LOWER-обязательный шаг «ценный товар».

Противовес (prefer_raise) сдвигает все границы решения:

bias       = prefer_raise_bias (0.15), если prefer_raise включён, иначе 0
doc_low'   = doc_low  × (1 + bias)
doc_high'  = doc_high × (1 + bias)
max_drop'  = max_drop_pct × (1 − bias)     # шаг снижения мягче
4. Дерево решений (по порядку для каждого товара)
Номер — порядок проверки в коде; на первом сработавшем условии — return.

0. Отсевы ДО решения
Условие	Результат
Товар вне WB-карточек (нет nmID в списке цен)	не строка/в non_wb
Нет карточки/цен WB в прайсе	SKIP — «нет карточки/цен WB»
Нет «сигнала жизни» (продажи/остаток/воронка) и show_zero = false	товар скрыт (hidden_dead)
Без «сигнала жизни», но show_zero = true (или при Применить)	см. п.4.1 HALVE
«Мёртвый товар» (HALVE) — нет ни одного сигнала. «Мёртвый запас» (п.4.3) — есть остаток, но продаж не было N дней — это разные вещи, пути разные.

4.1. Мёртвый товар — HALVE («скидка ÷2»)
half   = ⌊cur_disc / 2⌋
target = max(min(half, cur_disc), dead_min_discount)     # dead_min_discount = 1%
step   = cur_disc − target
если step < min_delta_pp → HOLD «скидка уже минимальна»
иначе → HALVE, target_discount = target
Цепочка: 50 → 25 → 12 → 6 → 3 → 1. Каждый прогон «Применить» делает один шаг, пока скидка не станет минимальной. Это мягкий противовес автоскидкам WB (цена постепенно восстанавливается у мёртвых). HALVE не попадает под кулдаун (см. п.7).

4.2. Базовые проверки живого товара
Условие	Результат
use_inventory и нет данных об остатках	SKIP — «нет данных об остатках»
use_inventory и остаток = 0	HOLD — «распродан»
use_margin и нет unit-экономики (нет продаж/доходов)	SKIP — «нет unit-экономики»
use_returns и returns_ratio > return_penalty (0.3)	SKIP — «возвраты/отмены, спрос мыльный»
use_orders, много «в корзине» (backlog по порогу hot_backlog_factor) и conv < low_conv_pct (0.7%)	SKIP — «много в корзинах, но не покупают»
4.3. Мёртвый запас (use_sales, продажи ≈ 0, есть остаток)
Условие входа: v_proj ≤ 0.

    если last_sale_days_ago ≥ dead_stock_days (7):
    если item_scale ≤ 0.01 (рейтинг 5.0 или ценность ≥ порога):  LOWER-обязательный шаг «ценный товар» (п.5)
    иначе:
        target_vis = max(floor, eff × (1 − max_drop'×item_scale/100), min_price)
        target_vis = min(target_vis, cur_vis)
        target_vis = max(target_vis, price × (1 − max_disc_item/100))
        new_disc   = (1 − target_vis/price) × 100
        если new_disc − cur_disc ≥ min_delta_pp → LOWER (обычный)
        иначе → LOWER-обязательный шаг «мёртвый запас»
иначе (продажи были недавно, но в окне нули):
        → LOWER-обязательный шаг «продаж нет (менее порога)»
4.4. Факторы выключены (use_sales = off, use_inventory = off)
Ни один сигнал не даёт направления, но правило «есть остаток — коррекция обязательна» всё равно действует:

без остатка        → HOLD (нет сигнала)
остаток > 0        → LOWER-обязательный шаг, если DOC ≥ target_doc
                     RAISE-обязательный шаг, если DOC < target_doc
4.5. Дефицит — doc < doc_low'
Зона дефицита — повод поднять цену (уменьшить скидку).

Горячий спрос (только при use_orders): claim = (conv ≥ hot_conv_pct) ИЛИ hot_backlog. Проверяется качество (только при use_quality): не прошли _raise_quality_gate → SKIP skipped_quality.

raise_pct = raise_pct_replenishable (10%), если товар докупаемый (repl)
          = max_raise_pct (15%),               если последние единицы
            × boost                            (качество ≥ 2 сильных сигналов → ×1.25)

target_vis = min(price, cur_vis × (1 + raise_pct/100))
new_disc   = (1 − target_vis/price) × 100
new_disc   = ПРАВИЛО_ПОЛОВИННОГО_КАПА(cur_disc, new_disc, factual, net_cost)   # п.5.2
target_vis = price × (1 − new_disc/100)

если cur_disc − new_disc ≥ min_delta_pp → RAISE
иначе → RAISE-обязательный шаг «дефицит и горячий спрос»
Докупаемый дефицит без горячего спроса («темп важнее»): теперь это всегда RAISE-обязательный шаг (п.5) — 1 п.п. вверх. (Раньше был HOLD; по правилу «есть остаток — коррекция обязательна» — изменён.)

4.6. Перезапас — doc ≥ doc_high'
Повод опустить цену (уменьшить скидку = увеличить скидку… проще: сделать витрину ниже).

если item_scale ≤ 0.01 (рейтинг 5.0 или ценность ≥ порога):  LOWER-обязательный шаг «ценный товар» (п.5)

k = 0.5
        × 0.8, если use_orders и conv < 1.0
        × 0.8, если use_season и trend < 1.0        # тренд падает — глубже
        × 1.2, если use_season и trend > 1.0        # тренд растёт — мягче
        clamp(0.2..1.2)

factor = max(1 − max_drop'×item_scale/100, (target_doc / doc)^k)
target_vis = max(floor, eff × factor, price × (1 − max_disc_item/100), min_price)
target_vis = min(target_vis, cur_vis)
new_disc   = (1 − target_vis/price) × 100
если new_disc − cur_disc ≥ min_delta_pp → LOWER
иначе → LOWER-обязательный шаг «перезапас» (+ «· мин. цена WB … ₽», если зажало мин. ценой)
Числовой смысл (target_doc/doc)^k: при слабом перезапасе (doc недалеко от цели) цель экономичная; при огромном doc множитель стремится к нулю и цель упирается в max_drop'/пол/мин.цену.

4.7. Нормальные остатки — doc_low' ≤ doc < doc_high'
Все «тихие» товары с остатком обязательно двигают скидку на шаг:

если doc ≥ target_doc (30) → LOWER-обязательный шаг (1 п.п.)
иначе                       → RAISE-обязательный шаг (1 п.п.)
5. Два жёстких правила пользователя
5.1. «Есть остаток → изменение скидки обязательно» (_mandatory_step)
HOLD в движке допустим только когда:

остатка нет (распродан / скрыт / фактор off при stock ≤ 0), или
цена не может измениться ни в одну сторону (пол ≥ цены, мин.цена ≥ цены, потолок скидки достигнут И скидка уже 0/минимальна).
Во всех остальных случаях ставится минимальный шаг:

step  = max(min_delta_pp, 1.0)   п.п.   (даже если min_delta_pp больше — не меньше 1 п.п.)

LOWER (снизить цену):
    target_vis = max(price × (1 − (cur_disc + step)/100),
                     floor,
                     price × (1 − max_disc_item/100),
                     min_price)
    target_vis = min(target_vis, cur_vis)          # не выше текущей витрины
    new_disc   = (1 − target_vis/price) × 100
    ок, если new_disc > cur_disc

RAISE (поднять цену):
    new_disc = max(cur_disc − step, 0)
    new_disc = ПРАВИЛО_2(cur_disc, new_disc, factual, net_cost)
    ок, если new_disc < cur_disc
Если шаг в заданном направлении невозможен — пробуется противоположное. Если не вышло ни то, ни другое — остаётся HOLD с первопричиной.

К причине добавляется суффикс: — коррекция скидки обязательна (есть остаток N шт).

5.2. «Скидку при подъёме режем не более чем пополам» (_half_discount_guard)
Действует на все пути RAISE (горячий, последние единицы, обязательный шаг):

если factual ≥ net_cost:   new_disc = max(new_disc, cur_disc / 2)
иначе:                     new_disc = new_disc      # исключение: продаём в убыток
factual — фактическая цена продажи (avg_price воронки; если её нет — eff).
Исключение: если фактическая цена ниже себестоимости — товар уже продаётся в минус, поэтому поднимаем без капа (иначе убыток продолжается).
На LOWER правило не распространяется: скидку увеличить можно сколько угодно (в пределах пола/потолка).
6. Мин. цена WB (клампинг)
use_min_price (по умолчанию выкл.): на каждой панели WB известна price_minimum витрины; включается в max(...) всех LOWER-формул — рекомендация не опустится ниже.
Если мин. цена уже ≥ текущей витрины (снижать «некуда»), правило 5.1 разворачивает решение в RAISE (шаг 1 п.п.) — пример M2 в п.8.
При применении (require_min_prices) наличие мин. цены для каждого LOWER обязательно: без неё применение блокируется (ValueError), чтобы не снижать вслепую (WB API всё равно не пропустит цену ниже минимума).
7. Применение и кулдаун
recommendations() — расчёт (только чтение).
apply_recommendations() — расчёт с show_zero = true (обрабатывает и мёртвых), затем:
собираются строки RAISE / LOWER / HALVE;
строки, у которых был applied в течение cooldown_days (3) → skipped_cooldown (кроме HALVE — мёртвые делят скидку на каждом прогоне, пока есть шаг);
update_prices через WB API (задание upload/task): высылается только nmID → новая скидка, базисная цена не меняется;
все применённые пишутся в журнал price_changes (before/after/причина/статус) — страница «Журнал решений».
cooldown_from = today − cooldown_days — товар не трогается повторно, даже если правила снова рекомендуют изменение.
8. Сквозные примеры
Общее окно 14 дн; все примеры — проверены юнит-тестами.

Пример 1. Нормальные остатки — обязательный шаг вниз (test_r10)
price 1000 ₽, скидка 10% → cur_vis 900. stock 20, скорость ~0.36 шт/дн. DOC = 56 (в нормальной зоне), DOC ≥ target_doc (30).

шаг = 1 п.п.
target_vis = price × 0.89 = 890
→ LOWER, target_discount = 11%
→ 900 ₽ → 890 ₽
reason: «нормальные остатки (DOC=56 дн.), целевые 30 дн. — коррекция скидки обязательна (есть остаток 20 шт)»
Пример 2. Докупаемый дефицит без горячего спроса — обязательный шаг вверх (test_r7)
price 1000 ₽, скидка 20% → cur_vis 800, stock 5, DOC = 17.5 < doc_low'… нет: 16.1 ≤ 17.5 < 69 → нормальная зона, DOC < target_doc → RAISE.

снижение скидки на 1 п.п.: 20% → 19%
кап R2: max(19, 20/2=10) = 19 (не сработал)
→ RAISE, target_discount = 19%, 800 → 810 ₽
Пример 3. Недокупаемый дефицит — половинный кап R2 срабатывает (test_r6)
price 1000 ₽, скидка 20% → cur_vis 800, stock 4, «последние единицы», DOC 6.6 < doc_low' 16.1, не горячий, без данных о рейтинге.

raise_pct = max_raise_pct = 15%
target_vis = min(1000, 800 × 1.15 = 920)
new_disc   = (1 − 920/1000) × 100 = 8%
кап R2: factual = eff = cur_vis = 800 ≥ net_cost 300 → new_disc = max(8, 20/2) = 10%
→ RAISE, target_discount = 10%, 800 → 900 ₽
Пример 4. Исключение R2: продаём ниже себестоимости (test C3)
То же, но фактическая цена продажи воронки avg_price = 250 ₽ < net_cost 300 ₽.

кап R2: factual 250 < net_cost 300 → кап НЕ применяется
→ RAISE, target_discount = 8% (без капа), 800 → 920 ₽
Пример 5. Горячий спрос у докупаемого (test_r5)
price 1000 ₽, скидка 20%, stock 5, replenishable, conv 4% ≥ hot 1.5%.

raise_pct = raise_pct_replenishable = 10%
target_vis = min(1000, 800 × 1.10 = 880), new_disc = 12%
кап R2: max(12, 10) = 12 — не сработал
→ RAISE, target_discount = 12%, 800 → 880 ₽
Пример 6. Перезапас с противовесом (test_prefer_raise_softens_overstock_drop)
price 2000 ₽, скидка 10% → cur_vis 1800, stock 1000, DOC ≈ 7000 ≫ doc_high'.

bias = 0.15 → max_drop' = 15 × 0.85 = 12.75%
k = 0.5 (conv 1.0%… в тесте conv = 2/200 = 1.0% ≥ 1.0 → k не режется)
factor = max(1 − 0.1275, (30/7000)^0.5 ≈ 0.065) = 0.8725
target_vis = max(floor≈473, 1800 × 0.8725 = 1570.5, price×0.5=1000, 0) = 1570.5
new_disc = (1 − 1570.5/2000) × 100 = 21.5%  → delta 11.5 п.п. ≥ 1
→ LOWER, target 1570.5 ₽ / 21.5%
Без противовеса (prefer_raise off) было бы max_drop' = 15% → 1800 × 0.85 = 1530 ₽ / 23.5%.

Пример 7. Мёртвый запас (есть остаток, продаж 40 дн нет) (test_r4)
price 1000 ₽, скидка 0% → cur_vis 1000, stock 100, v_proj 0, last_sale 40 ≥ 7.

(max_drop' берём для prefer_raise=False = 15%)
target_vis = max(floor≈473, 1000 × 0.85 = 850, 0) = 850
target_vis = min(850, 1000) = 850;  max(850, 500) = 850
→ LOWER, 850 ₽ / 15%
Пример 8. Мин. цена выше текущей — LOWER невозможен, RAISE обязателен (test_t15)
price 2000 ₽, скидка 10% → cur_vis 1800, min_price = 1900 ₽, перезапас (DOC 14000).

обычная LOWER упирается в min_price ≥ cur_vis → дельта 0 → обязательный шаг (R1):
try_lower: target_vis = max(…, 1900) → min(1900, 1800) = 1800 → не выше (скидка не изменилась)
try_raise: 10% − 1 = 9%, кап R2: max(9, 5) = 9 → ОК
→ RAISE, target_discount = 9%, 1800 → 1820 ₽
reason: «перезапас … · мин. цена WB 1900 ₽ — коррекция скидки обязательна (есть остаток …)»
Пример 9. Мёртвый товар → HALVE (скидка ÷2)
Скидка 50%:

⌊50/2⌋ = 25 → целевая 25%
далее 25 → 12 → 6 → 3 → 1 (dead_min_discount)
при 1: ⌊1/2⌋ = 0 → target = max(min(0,1), 1) = 1 → target ≥ cur → HOLD «скидка уже минимальна»
9. Качество продаж (опция use_quality)
Отключает/включает две вещи при RAISE:

Гейт — блокирует подъём (SKIP skipped_quality), если (только для ненулевых значений):

product_rating < min_rating_for_raise (4.2);
conv_buyout_percent < min_conv_buyout_for_raise (40%);
отмены/заказы > max_cancel_ratio_for_raise (0.2);
возвраты (из детализации) > max_return_rate_for_raise (15%).
Boost — если выполнены ≥ 2 из 4 «сильных» сигналов (strong_rating 4.5, strong_buyout_conv 60%, strong_return_rate ≤ 5%, strong_margin_pct ≥ 30%) — рост × (1 + raise_boost_pct/100) = 1.25.

10. Статусы и действия
action	Смысл	Кулдаун
RAISE	поднять витринную цену (уменьшить скидку)	да
LOWER	опустить витрину (увеличить скидку)	да
HALVE	мёртвый товар: скидка ÷2	нет
HOLD	не трогать (обязательная причина: распродан / оба направления упёрлись)	—
SKIP	вне решения (нет данных/цен/остатков, качество, возвраты, корзины)	—
status: suggested → applied (после успешного WB API), либо skipped_no_stock, skipped_no_ratio, skipped_no_data, skipped_returns, skipped_carts, skipped_quality, skipped_cooldown.

11. Параметры (полная таблица)
Дефолты — PRICING_DEFAULTS в app/services/pricing.py; значения из UI сохраняются в браузере (localStorage) и перекрывают дефолты.

11.1. Окно и данные
Параметр	Дефолт	Роль
window_days	14	Ширина окна анализа (дней), если не задан период
min_days_with_sales	5	Минимум дней с продажами в окне; меньше — «Мало истории»
fallback_window_days	90	Окно для unit-экономики (комиссия, логистика, хранение)
season_adj	true	Учитывать тренд при прогнозе скорости
season_damp	0.5	Ослабление экстраполяции тренда (0..1)
11.2. Зоны DOC и шаги
Параметр	Дефолт	Роль
target_doc	30	Целевой запас, дней продаж. DOC ≥ него в нормальной зоне → LOWER
doc_low	14	DOC ниже → дефицит (повод RAISE)
doc_high	60	DOC выше → перезапас (повод LOWER)
prefer_raise	true	Противовес автоскидкам WB
prefer_raise_bias	0.15	Сдвиг границ DOC и смягчение max_drop
dead_stock_days	7	Дней без продаж → мёртвый запас
11.3. Скидка и деньги
Параметр	Дефолт	Роль
floor_margin_pct	10.0	Запас маржи над безубыточностью (% от цены)
max_discount_pct	50.0	Потолок скидки от базовой цены (любой товар)
max_raise_pct	15.0	Макс. рост витрины за шаг (последние единицы / горячие)
max_drop_pct	15.0	Макс. снижение витрины за шаг
raise_pct_replenishable	10.0	Тот же рост, но для докупаемых (умеренно)
min_delta_pp	1.0	Мин. изменение скидки, п.п. (защита от «флапа»)
dead_min_discount	1.0	Нижняя граница скидки мёртвых (HALVE)
cooldown_days	3	Дней между изменениями цены товара
11.4. Спрос и воронка
Параметр	Дефолт	Роль
hot_conv_pct	1.5	Конверсия ≥ → горячий спрос (можно RAISE в дефиците)
hot_backlog_factor	2.0	«в корзине» ≥ ×orders → горячий спрос
low_conv_pct	0.7	Много в корзинах и conv ниже → SKIP («цена тормозит сделку»)
return_penalty	0.3	Доля возвратов/отмен выше → SKIP (мыльный спрос)
min_rating_reviews	4.0	Порог рейтинга по отзывам (ценный товар)
value_ratio_low	1.0	Ценность/себест. ≤ → ограничений нет (value_scale = 1.0)
value_ratio_high	4.0	Ценность/себест. ≥ → скидку не увеличиваем (value_scale = 0.0)
11.5. Качество (используется при use_quality)
Параметр	Дефолт	Роль
min_rating_for_raise	4.2	Гейт RAISE: рейтинг ниже → нельзя поднимать
min_conv_buyout_for_raise	40.0	Гейт RAISE: плохая выкупаемость → нельзя
max_cancel_ratio_for_raise	0.2	Гейт RAISE: много отмен → нельзя
max_return_rate_for_raise	15.0	Гейт RAISE: много возвратов → нельзя
strong_rating	4.5	Сильный сигнал → boost
strong_buyout_conv	60.0	Сильный сигнал → boost
strong_return_rate	5.0	Сильный сигнал → boost
strong_margin_pct	30.0	Сильный сигнал → boost
raise_boost_pct	25.0	Uplift роста при ≥ 2 сильных сигналах
11.6. Факторы-чекбоксы (объяснение из UI)
Фактор	Выключение
use_inventory	остаток/DOC не влияет на решение
use_sales	скорость и мёртвый запас не влияют
use_orders	воронка (горячий спрос, корзины) не влияет
use_margin	unit-экономика не блокирует (но цена может уйти в убыток)
use_replenishable	докупаемость не различается
use_season	тренд не учитывается
use_reviews	рейтинг по отзывам не ограничивает скидку
use_quality	качество не гейтит и не форсит RAISE (по умолчанию выкл.)
use_returns	защита от «мыльного» спроса отключена (по умолчанию выкл.)
use_min_price	клампинг по мин. цене WB отключён (по умолчанию выкл.)
12. Куда смотреть в коде
merge_settings, PRICING_DEFAULTS — параметры и их склейка.
project_velocity — прогноз скорости (momentum).
recommendations — вся сборка сигналов и метрик.
_decide_dead — HALVE (мёртвый товар).
_decide — главное дерево решений (R3–R10 + зоны).
_mandatory_step — правило 1 (обязательная коррекция при остатке).
_half_discount_guard — правило 2 (половинный кап RAISE).
_rating_discount_scale, _value_discount_scale, _raise_quality_gate, _raise_boost — качество/рейтинг/ценность.
_pushed_items, apply_recommendations — кулдаун, применение, журнал.
Менять поведение «скидки» нужно здесь, а не в UI: интерфейс лишь сохраняет числа, дефолты — в PRICING_DEFAULTS.
### 4.5. Дефицит — `doc < doc_low'`

Зона дефицита — повод поднять цену (уменьшить скидку).

**Горячий спрос** (только при `use_orders`): `claim = (conv ≥ hot_conv_pct) ИЛИ hot_backlog`.
Проверяется качество (только при `use_quality`): не прошли `_raise_quality_gate`
→ `SKIP skipped_quality`.

```
raise_pct = raise_pct_replenishable (10%), если товар докупаемый (repl)
          = max_raise_pct (15%),               если последние единицы
            × boost                            (качество ≥ 2 сильных сигналов → ×1.25)

target_vis = min(price, cur_vis × (1 + raise_pct/100))
new_disc   = (1 − target_vis/price) × 100
new_disc   = ПРАВИЛО_ПОЛОВИННОГО_КАПА(cur_disc, new_disc, factual, net_cost)   # п.5.2
target_vis = price × (1 − new_disc/100)

если cur_disc − new_disc ≥ min_delta_pp → RAISE
иначе → RAISE-обязательный шаг «дефицит и горячий спрос»
```

**Докупаемый дефицит без горячего спроса** («темп важнее»):
теперь это всегда **RAISE-обязательный шаг** (п.5) — 1 п.п. вверх.
(Раньше был HOLD; по правилу «есть остаток — коррекция обязательна» — изменён.)

### 4.6. Перезапас — `doc ≥ doc_high'`

Повод опустить цену (уменьшить скидку = увеличить скидку… проще: сделать витрину ниже).

```
если item_scale ≤ 0.01 (рейтинг 5.0 или ценность ≥ порога):  LOWER-обязательный шаг «ценный товар» (п.5)

k = 0.5
        × 0.8, если use_orders и conv < 1.0
        × 0.8, если use_season и trend < 1.0        # тренд падает — глубже
        × 1.2, если use_season и trend > 1.0        # тренд растёт — мягче
        clamp(0.2..1.2)

factor = max(1 − max_drop'×item_scale/100, (target_doc / doc)^k)
target_vis = max(floor, eff × factor, price × (1 − max_disc_item/100), min_price)
target_vis = min(target_vis, cur_vis)
new_disc   = (1 − target_vis/price) × 100
если new_disc − cur_disc ≥ min_delta_pp → LOWER
иначе → LOWER-обязательный шаг «перезапас» (+ «· мин. цена WB … ₽», если зажало мин. ценой)
```

> Числовой смысл `(target_doc/doc)^k`: при слабом перезапасе (doc недалеко от
> цели) цель экономичная; при огромном doc множитель стремится к нулю и цель
> упирается в `max_drop'`/пол/мин.цену.

### 4.7. Нормальные остатки — `doc_low' ≤ doc < doc_high'`

Все «тихие» товары с остатком **обязательно** двигают скидку на шаг:

```
если doc ≥ target_doc (30) → LOWER-обязательный шаг (1 п.п.)
иначе                       → RAISE-обязательный шаг (1 п.п.)
```

---

## 5. Два жёстких правила пользователя

### 5.1. «Есть остаток → изменение скидки обязательно» (`_mandatory_step`)

HOLD в движке допустим только когда:
- остатка нет (распродан / скрыт / фактор off при stock ≤ 0), или
- цена не может измениться ни в одну сторону (пол ≥ цены, мин.цена ≥ цены,
  потолок скидки достигнут И скидка уже 0/минимальна).

Во всех остальных случаях ставится **минимальный шаг**:

```
step  = max(min_delta_pp, 1.0)   п.п.   (даже если min_delta_pp больше — не меньше 1 п.п.)

LOWER (снизить цену):
    target_vis = max(price × (1 − (cur_disc + step)/100),
                     floor,
                     price × (1 − max_disc_item/100),
                     min_price)
    target_vis = min(target_vis, cur_vis)          # не выше текущей витрины
    new_disc   = (1 − target_vis/price) × 100
    ок, если new_disc > cur_disc

RAISE (поднять цену):
    new_disc = max(cur_disc − step, 0)
    new_disc = ПРАВИЛО_2(cur_disc, new_disc, factual, net_cost)
    ок, если new_disc < cur_disc
```

Если шаг в заданном направлении невозможен — пробуется **противоположное**.
Если не вышло ни то, ни другое — остаётся HOLD с первопричиной.

К причине добавляется суффикс:
` — коррекция скидки обязательна (есть остаток N шт)`.

### 5.2. «Скидку при подъёме режем не более чем пополам» (`_half_discount_guard`)

Действует на **все** пути RAISE (горячий, последние единицы, обязательный шаг):

```
если factual ≥ net_cost:   new_disc = max(new_disc, cur_disc / 2)
иначе:                     new_disc = new_disc      # исключение: продаём в убыток
```

- `factual` — фактическая цена продажи (`avg_price` воронки; если её нет — `eff`).
- Исключение: если фактическая цена **ниже себестоимости** — товар уже продаётся
  в минус, поэтому поднимаем **без капа** (иначе убыток продолжается).
- На LOWER правило не распространяется: скидку увеличить можно сколько угодно
  (в пределах пола/потолка).

---

## 6. Мин. цена WB (клампинг)

- `use_min_price` (по умолчанию выкл.): на каждой панели WB известна
  `price_minimum` витрины; включается в `max(...)` всех LOWER-формул — рекомендация
  не опустится ниже.
- Если мин. цена уже ≥ текущей витрины (снижать «некуда»), правило 5.1 разворачивает
  решение в RAISE (шаг 1 п.п.) — пример M2 в п.8.
- При **применении** (`require_min_prices`) наличие мин. цены для каждого LOWER
  обязательно: без неё применение блокируется (`ValueError`), чтобы не снижать
  вслепую (WB API всё равно не пропустит цену ниже минимума).

---

## 7. Применение и кулдаун

1. `recommendations()` — расчёт (только чтение).
2. `apply_recommendations()` — расчёт с `show_zero = true` (обрабатывает и мёртвых),
   затем:
   - собираются строки `RAISE / LOWER / HALVE`;
   - строки, у которых был `applied` в течение `cooldown_days (3)` → `skipped_cooldown`
     (кроме **HALVE** — мёртвые делят скидку на каждом прогоне, пока есть шаг);
   - `update_prices` через WB API (задание `upload/task`): высылается только
     `nmID → новая скидка`, базисная цена не меняется;
   - все применённые пишутся в журнал `price_changes`
     (before/after/причина/статус) — страница «Журнал решений».
3. `cooldown_from = today − cooldown_days` — товар не трогается повторно, даже если
   правила снова рекомендуют изменение.

---

## 8. Сквозные примеры

Общее окно 14 дн; все примеры — проверены юнит-тестами.

### Пример 1. Нормальные остатки — обязательный шаг вниз (test_r10)

`price 1000 ₽`, скидка `10%` → `cur_vis 900`. `stock 20`, скорость ~0.36 шт/дн.
`DOC = 56` (в нормальной зоне), `DOC ≥ target_doc (30)`.

```
шаг = 1 п.п.
target_vis = price × 0.89 = 890
→ LOWER, target_discount = 11%
→ 900 ₽ → 890 ₽
reason: «нормальные остатки (DOC=56 дн.), целевые 30 дн. — коррекция скидки обязательна (есть остаток 20 шт)»
```

### Пример 2. Докупаемый дефицит без горячего спроса — обязательный шаг вверх (test_r7)

`price 1000 ₽`, скидка `20%` → `cur_vis 800`, `stock 5`, `DOC = 17.5` < `doc_low'`… нет:
16.1 ≤ 17.5 < 69 → нормальная зона, `DOC < target_doc` → RAISE.

```
снижение скидки на 1 п.п.: 20% → 19%
кап R2: max(19, 20/2=10) = 19 (не сработал)
→ RAISE, target_discount = 19%, 800 → 810 ₽
```

### Пример 3. Недокупаемый дефицит — половинный кап R2 срабатывает (test_r6)

`price 1000 ₽`, скидка `20%` → `cur_vis 800`, `stock 4`, «последние единицы»,
`DOC 6.6` < `doc_low' 16.1`, не горячий, без данных о рейтинге.

```
raise_pct = max_raise_pct = 15%
target_vis = min(1000, 800 × 1.15 = 920)
new_disc   = (1 − 920/1000) × 100 = 8%
кап R2: factual = eff = cur_vis = 800 ≥ net_cost 300 → new_disc = max(8, 20/2) = 10%
→ RAISE, target_discount = 10%, 800 → 900 ₽
```

### Пример 4. Исключение R2: продаём ниже себестоимости (test C3)

То же, но фактическая цена продажи воронки `avg_price = 250 ₽` < `net_cost 300 ₽`.

```
кап R2: factual 250 < net_cost 300 → кап НЕ применяется
→ RAISE, target_discount = 8% (без капа), 800 → 920 ₽
```

### Пример 5. Горячий спрос у докупаемого (test_r5)

`price 1000 ₽`, скидка `20%`, `stock 5`, `replenishable`, conv 4% ≥ hot 1.5%.

```
raise_pct = raise_pct_replenishable = 10%
target_vis = min(1000, 800 × 1.10 = 880), new_disc = 12%
кап R2: max(12, 10) = 12 — не сработал
→ RAISE, target_discount = 12%, 800 → 880 ₽
```

### Пример 6. Перезапас с противовесом (test_prefer_raise_softens_overstock_drop)

`price 2000 ₽`, скидка `10%` → `cur_vis 1800`, `stock 1000`, `DOC ≈ 7000` ≫ `doc_high'`.

```
bias = 0.15 → max_drop' = 15 × 0.85 = 12.75%
k = 0.5 (conv 1.0%… в тесте conv = 2/200 = 1.0% ≥ 1.0 → k не режется)
factor = max(1 − 0.1275, (30/7000)^0.5 ≈ 0.065) = 0.8725
target_vis = max(floor≈473, 1800 × 0.8725 = 1570.5, price×0.5=1000, 0) = 1570.5
new_disc = (1 − 1570.5/2000) × 100 = 21.5%  → delta 11.5 п.п. ≥ 1
→ LOWER, target 1570.5 ₽ / 21.5%
```

Без противовеса (prefer_raise off) было бы `max_drop' = 15%` → `1800 × 0.85 = 1530 ₽ / 23.5%`.

### Пример 7. Мёртвый запас (есть остаток, продаж 40 дн нет) (test_r4)

`price 1000 ₽`, скидка `0%` → `cur_vis 1000`, `stock 100`, `v_proj 0`, `last_sale 40` ≥ 7.

```
(max_drop' берём для prefer_raise=False = 15%)
target_vis = max(floor≈473, 1000 × 0.85 = 850, 0) = 850
target_vis = min(850, 1000) = 850;  max(850, 500) = 850
→ LOWER, 850 ₽ / 15%
```

### Пример 8. Мин. цена выше текущей — LOWER невозможен, RAISE обязателен (test_t15)

`price 2000 ₽`, скидка `10%` → `cur_vis 1800`, `min_price = 1900 ₽`, перезапас (DOC 14000).

```
обычная LOWER упирается в min_price ≥ cur_vis → дельта 0 → обязательный шаг (R1):
try_lower: target_vis = max(…, 1900) → min(1900, 1800) = 1800 → не выше (скидка не изменилась)
try_raise: 10% − 1 = 9%, кап R2: max(9, 5) = 9 → ОК
→ RAISE, target_discount = 9%, 1800 → 1820 ₽
reason: «перезапас … · мин. цена WB 1900 ₽ — коррекция скидки обязательна (есть остаток …)»
```

### Пример 9. Мёртвый товар → HALVE (скидка ÷2)

Скидка `50%`:

```
⌊50/2⌋ = 25 → целевая 25%
далее 25 → 12 → 6 → 3 → 1 (dead_min_discount)
при 1: ⌊1/2⌋ = 0 → target = max(min(0,1), 1) = 1 → target ≥ cur → HOLD «скидка уже минимальна»
```

---

## 9. Качество продаж (опция `use_quality`)

Отключает/включает две вещи при RAISE:

**Гейт** — блокирует подъём (`SKIP skipped_quality`), если (только для ненулевых значений):
- `product_rating < min_rating_for_raise (4.2)`;
- `conv_buyout_percent < min_conv_buyout_for_raise (40%)`;
- отмены/заказы > `max_cancel_ratio_for_raise (0.2)`;
- возвраты (из детализации) > `max_return_rate_for_raise (15%)`.

**Boost** — если выполнены ≥ 2 из 4 «сильных» сигналов
(`strong_rating 4.5`, `strong_buyout_conv 60%`, `strong_return_rate ≤ 5%`,
`strong_margin_pct ≥ 30%`) — рост × `(1 + raise_boost_pct/100) = 1.25`.

---

## 10. Статусы и действия

| `action` | Смысл | Кулдаун |
|---|---|---|
| `RAISE` | поднять витринную цену (уменьшить скидку) | да |
| `LOWER` | опустить витрину (увеличить скидку) | да |
| `HALVE` | мёртвый товар: скидка ÷2 | **нет** |
| `HOLD` | не трогать (обязательная причина: распродан / оба направления упёрлись) | — |
| `SKIP` | вне решения (нет данных/цен/остатков, качество, возвраты, корзины) | — |

`status`: `suggested` → `applied` (после успешного WB API), либо
`skipped_no_stock`, `skipped_no_ratio`, `skipped_no_data`, `skipped_returns`,
`skipped_carts`, `skipped_quality`, `skipped_cooldown`.

---

## 11. Параметры (полная таблица)

Дефолты — `PRICING_DEFAULTS` в `app/services/pricing.py`; значения из UI
сохраняются в браузере (`localStorage`) и перекрывают дефолты.

### 11.1. Окно и данные

| Параметр | Дефолт | Роль |
|---|---|---|
| `window_days` | 14 | Ширина окна анализа (дней), если не задан период |
| `min_days_with_sales` | 5 | Минимум дней с продажами в окне; меньше — «Мало истории» |
| `fallback_window_days` | 90 | Окно для unit-экономики (комиссия, логистика, хранение) |
| `season_adj` | true | Учитывать тренд при прогнозе скорости |
| `season_damp` | 0.5 | Ослабление экстраполяции тренда (0..1) |

### 11.2. Зоны DOC и шаги

| Параметр | Дефолт | Роль |
|---|---|---|
| `target_doc` | 30 | Целевой запас, дней продаж. DOC ≥ него в нормальной зоне → LOWER |
| `doc_low` | 14 | DOC ниже → дефицит (повод RAISE) |
| `doc_high` | 60 | DOC выше → перезапас (повод LOWER) |
| `prefer_raise` | true | Противовес автоскидкам WB |
| `prefer_raise_bias` | 0.15 | Сдвиг границ DOC и смягчение `max_drop` |
| `dead_stock_days` | 7 | Дней без продаж → мёртвый запас |

### 11.3. Скидка и деньги

| Параметр | Дефолт | Роль |
|---|---|---|
| `floor_margin_pct` | 10.0 | Запас маржи над безубыточностью (% от цены) |
| `max_discount_pct` | 50.0 | Потолок скидки от базовой цены (любой товар) |
| `max_raise_pct` | 15.0 | Макс. рост витрины за шаг (последние единицы / горячие) |
| `max_drop_pct` | 15.0 | Макс. снижение витрины за шаг |
| `lower_soften` | 4.0 | R18: во сколько раз мягче снижать цену (LOWER) |
| `raise_pct_replenishable` | 10.0 | Тот же рост, но для докупаемых (умеренно) |
| `min_delta_pp` | 1.0 | Мин. изменение скидки, п.п. (защита от «флапа») |
| `dead_min_discount` | 1.0 | Нижняя граница скидки мёртвых (HALVE) |
| `cooldown_days` | 3 | Дней между изменениями цены товара |
| `promo_enabled` | выкл. | Акции WB: добор участия (R11) + потолок по `planDiscount` (R19) |

### 11.4. Спрос и воронка

| Параметр | Дефолт | Роль |
|---|---|---|
| `hot_conv_pct` | 1.5 | Конверсия ≥ → горячий спрос (можно RAISE в дефиците) |
| `hot_backlog_factor` | 2.0 | «в корзине» ≥ `×orders` → горячий спрос |
| `low_conv_pct` | 0.7 | Много в корзинах и conv ниже → SKIP («цена тормозит сделку») |
| `return_penalty` | 0.3 | Доля возвратов/отмен выше → SKIP (мыльный спрос) |
| `min_rating_reviews` | 4.0 | Порог рейтинга по отзывам (ценный товар) |
| `value_ratio_low` | 1.0 | Ценность/себест. ≤ → ограничений нет (`value_scale = 1.0`) |
| `value_ratio_high` | 4.0 | Ценность/себест. ≥ → скидку не увеличиваем (`value_scale = 0.0`) |
| `demand_raise` | true | R12: подтверждённый спрос поднимает цену (за пределами дефицита) |
| `demand_min_buyouts` | 3 | R12: порог сигнала «выкупы» (0 = сигнал выключен) |
| `demand_min_backlog` | 5 | R12: порог сигнала «в корзине» |
| `demand_min_rating` | 4.5 | R12: порог сигнала «рейтинг отзывов» |
| `demand_min_conv_pct` | 1.0 | R12: порог сигнала «конверсия заказов», % |
| `demand_min_signals` | 2 | R12: сколько сигналов из 4 нужно, чтобы поднимать |
| `demand_raise_pct` | 15.0 | R12: рост витрины при подтверждённом спросе, % (обходит половинный кап) |
| `demand_raise_max_doc` | 120.0 | R12: не поднимать, если DOC выше (глубокий перезапас) |
| `velocity_from_funnel` | true | R13: брать скорость из воронки, если строк продаж в окне нет |
| `funnel_velocity_field` | buyouts | R13: поле воронки как прокси скорости (`buyouts`/`orders`) |
| `no_econ_cost_floor` | true | R14: без unit-экономики считать пол по себестоимости (иначе `SKIP`) |
| `stock_value_weight` | true | R15: приоритет по деньгам остатка (`stock × net_cost`), не по штукам |
| `follow_main` | true | R16/T-33: следовать за свежей рекомендацией главной связки (только повышения) |
| `main_influence_pct` | 50.0 | R16/T-33: доля влияния главной (`50` = половина её Δ%) |

### 11.5. Качество (используется при `use_quality`)

| Параметр | Дефолт | Роль |
|---|---|---|
| `min_rating_for_raise` | 4.2 | Гейт RAISE: рейтинг ниже → нельзя поднимать |
| `min_conv_buyout_for_raise` | 40.0 | Гейт RAISE: плохая выкупаемость → нельзя |
| `max_cancel_ratio_for_raise` | 0.2 | Гейт RAISE: много отмен → нельзя |
| `max_return_rate_for_raise` | 15.0 | Гейт RAISE: много возвратов → нельзя |
| `strong_rating` | 4.5 | Сильный сигнал → boost |
| `strong_buyout_conv` | 60.0 | Сильный сигнал → boost |
| `strong_return_rate` | 5.0 | Сильный сигнал → boost |
| `strong_margin_pct` | 30.0 | Сильный сигнал → boost |
| `raise_boost_pct` | 25.0 | Uplift роста при ≥ 2 сильных сигналах |

### 11.6. Факторы-чекбоксы (объяснение из UI)

| Фактор | Выключение |
|---|---|
| `use_inventory` | остаток/DOC не влияет на решение |
| `use_sales` | скорость и мёртвый запас не влияют |
| `use_orders` | воронка (горячий спрос, корзины) не влияет |
| `use_margin` | unit-экономика не блокирует (но цена может уйти в убыток) |
| `use_replenishable` | докупаемость не различается |
| `use_season` | тренд не учитывается |
| `use_reviews` | рейтинг по отзывам не ограничивает скидку |
| `use_quality` | качество не гейтит и не форсит RAISE (по умолчанию выкл.) |
| `use_returns` | защита от «мыльного» спроса отключена (по умолчанию выкл.) |
| `use_min_price` | клампинг по мин. цене WB отключён (по умолчанию выкл.) |

---

## 12. Куда смотреть в коде

- `merge_settings`, `PRICING_DEFAULTS` — параметры и их склейка.
- `project_velocity` — прогноз скорости (momentum).
- `recommendations` — вся сборка сигналов и метрик.
- `_decide_dead` — HALVE (мёртвый товар).
- `_decide` — главное дерево решений (R3–R10 + зоны).
- `_mandatory_step` — правило 1 (обязательная коррекция при остатке).
- `_half_discount_guard` — правило 2 (половинный кап RAISE).
- `_rating_discount_scale`, `_value_discount_scale`, `_raise_quality_gate`, `_raise_boost` — качество/рейтинг/ценность.
- `_demand_hot` — R12: подтверждённый спрос (выкупы/корзина/рейтинг/конверсия).
- `soften_lower` — R18: мягкость снижения цены (`lower_soften`).
- `_promo_pass` — R11: акции WB (info-поля + добор участия `promo_push_pct`).
- `_promo_plan_map`, `promo_plan_apply` — R19/T-41: потолок по рекомендованной
  акцией скидке (`planDiscount` из `wb_promo_nomenclatures`; regular-акции).
- `build_main_map`, `main_influence_map`, `apply_main_influence` — R16/T-33:
  межсвязочное влияние (главная связка → зависимые, только повышения).
- `apply_min_prices` — T-33: минимальные (безубыточные) цены (`action=MIN`).
- `_pushed_items`, `apply_recommendations` — кулдаун, применение, журнал.

Менять поведение «скидки» нужно здесь, а не в UI: интерфейс лишь сохраняет
числа, дефолты — в `PRICING_DEFAULTS`.

---

## 13. R12–R15 (T-32): спрос важнее остатка, скорость из воронки, пол по себестоимости

Эти правила чинят три перекоса прежней логики: хорошие товары дешевели из-за
остатка, живые товары без строк продаж объявлялись «мёртвыми», а товары с
остатком и без unit-экономики уходили в `SKIP`.

### R12. Подтверждённый спрос → RAISE (`_demand_hot`)

- Работает при включённом факторе «Заказы и конверсия» (`use_orders`).
- Считаются 4 сигнала: выкупы ≥ `demand_min_buyouts`, «в корзине» ≥
  `demand_min_backlog`, рейтинг отзывов ≥ `demand_min_rating`, конверсия ≥
  `demand_min_conv_pct`. Нулевой порог отключает сигнал (0/нет данных не считается).
- Если набрано ≥ `demand_min_signals` и `DOC ≤ demand_raise_max_doc` — **RAISE**
  на `demand_raise_pct` (по умолчанию +15% к витрине). Этот путь **обходит**
  «половинный кап» (правило 5.2), иначе рост цены почти незаметен.
- Зона действия — нормальные остатки и перезапас (`doc ≥ doc_low'`). В дефиците
  (`doc < doc_low'`) продолжает работать нижестоящая логика с гейтами качества
  T-14, чтобы «мыльный» спрос (плохой рейтинг, отмены, возвраты) не поднимал цену.
- `prefer_raise`/зоны DOC на R12 не влияют; `use_quality` — тоже (гейты T-14
  остаются только у дефицитной ветки).

### R13. Скорость из воронки при отсутствии строк продаж

- Если за окно нет строк `Sale` (`v_proj ≤ 0`), но в свежем срезе воронки есть
  активность, `v_proj = funnel_velocity_field / дней_в_срезе` (по умолчанию
  `buyouts` к выкупам). Так товар с выкупами не считается «мёртвым запасом».
- `use_sales = off` по-прежнему обнуляет скорость; при отсутствии воронки
  поведение не меняется (п.4.3 «мёртвый запас»).

### R14. Пол по себестоимости без unit-экономики (`no_econ_cost_floor`)

- Если `use_margin`, но unit-экономики нет (`comm_rate`/`floor` пусты) и
  `no_econ_cost_floor = true` (по умолчанию), а `net_cost > 0` — вместо
  `SKIP skipped_no_ratio` берётся `floor = net_cost × (1 + floor_margin_pct/100)`,
  и товар идёт по обычным веткам мёртвого запаса/перезапаса (обычно LOWER).
  `net_cost = 0` → по-прежнему `SKIP`. Выключение флага (`false`) возвращает
  прежнее поведение «пропустить».

### R15. Ценность остатка (`stock_value`)

- В строку добавляется `stock_value = stock × net_cost`. При
  `stock_value_weight = true` это поле используется как ключ приоритета добора
  акций (`_promo_pass`) вместо штук: 100 шт × 1000 ₽ важнее 100 шт × 100 ₽.
  Схема `Stock` не меняется.

### R17. «Остаток» без «в пути» и отдельная колонка «В пути»

- Колонка «Остаток» (`stock`) считается **только по складу** (`quantity`),
  товары «в пути» в неё не входят — и у WB, и у Ozon
  (`_latest_stock` / `_latest_ozon_stock`).
- «В пути» вынесен отдельной колонкой `in_way`
  (`_latest_in_way` / `_latest_ozon_in_way`): у WB это
  `inWayToClient + inWayFromClient`, у Ozon — `promised_amount`.
- `stock_value = stock × net_cost` остаётся **только по складу**.
- Товар с нулевым складом, но идущий «в пути» — **живой**: он не прячется
  как мёртвый (`has_life` включает `in_way > 0`) и показывается как
  HOLD «распродан (остаток 0)» с заполненной колонкой «В пути».
- Колонка есть в обеих вкладках (WB и Ozon) и в обоих Excel-экспортах.

### R18. Мягкость снижения цены (`lower_soften`)

- Настройка `lower_soften` (дефолт **4.0**) делит расчётное **снижение** цены:
  дельта LOWER уменьшается в `lower_soften` раз (4 → снижение в 4 раза мягче,
  1 → без смягчения). **Повышения цены (RAISE) не затрагиваются.**
- WB (`soften_lower`): `target_discount = current_discount + Δ / lower_soften`,
  где `Δ = target_discount − current_discount`; пересчитываются `target_vis`,
  `delta_discount`, `margin_pct_at_target`.
- Ozon (`soften_lower_ozon`): `target_price = price − (price − target) / lower_soften`.
- **Минимальный шаг не смягчается**: если после деления шаг меньше
  `min_delta_pp` (WB) или `min (min_delta_pct %, oz_price_step ₽)` (Ozon),
  берётся минимальный шаг. Так правило «коррекция обязательна (есть остаток)»
  и защита от «флапа» остаются в силе.
- Порядок: смягчение идёт **до** акций WB (добор участия `promo_push_pct`
  встаёт поверх уже смягчённой скидки) и **до** влияния главной связки
  (R16 поднимает цену поверх). Применяется и в рекомендациях, и при «Применить».

### R19. Потолок по рекомендованной акцией скидке (`planDiscount`, T-41)

Автопилот не должен уходить **ниже скидки, которую рекомендует активная акция
WB**. Если автопилот хотел бы 12%, а акция WB просит 7% — он останавливается
на 7%.

- **Источник данных.** `GET /calendar/promotions/nomenclatures?promotionID=&inAction=`
  отдаёт per-product `planPrice` и `planDiscount` (рекомендованная WB скидка).
  **Работает только для `regular`-акций — у `auto` endpoint отвечает 422.**
  Поскольку автоакции составляют ~97% каталога, правило сейчас покрывает лишь
  обычные акции; это задокументированное ограничение API WB, а не пробел.
- **Хранение.** Per-cab таблица `wb_promo_nomenclatures`
  (`models.PromoNomenclature`): `promo_id`, `nm_id`, `in_action`, `price`,
  `plan_price`, `discount`, `plan_discount`, `fetched_at`; unique `(promo_id, nm_id)`.
  Заполняется в `refresh.pull_wb_promotions` **только по активным regular-акциям**
  (обе ветки: участники `inAction=true` + кандидаты `false`), чистится
  `sync.purge_promo_nomenclatures`. Регистрация per-cab — в `WB_ONLY_TABLES`
  (`services/cabinet_migrate.py`).
- **Расчёт.** `pricing._promo_plan_map(db)` → `{nm_id: max(plan_discount)}`
  по **активным** regular-акциям (`adv_type='regular'`, `starts_at ≤ now ≤ ends_at`).
  Если у товара несколько акций — берётся **максимальная (самая глубокая)** скидка.
- **Правило.** `pricing.promo_plan_apply` применяет
  `effective_cap = max(cap, current_discount)` и
  `target_discount = min(target_discount, effective_cap)`; пересчитываются
  `target_vis`, `delta_discount`, `margin_pct_at_target`. Потолок лишь
  предотвращает **углубление** скидки, но **никогда не заставляет повышать цену**:
  товар, уже сидящий глубже рекомендации WB, остаётся как есть.
- **Порядок.** Вызывается в `recommendations()` **после** push (R11),
  смягчения (R18) и главной связки (R16) — то есть это **финальный** потолок:
  ничто из предыдущих правил не может увести цену ниже плана WB.
  («Главная связка» ранее считалась последним словом — R19 становится новым
  финальным ограничением, поскольку требование WB жёстче любого внутреннего правила.)
- **Включение.** Работает только при `promo_enabled = true` (общий флаг
  «учитывать акции WB» — и добор R11, и потолок R19). При выключенном флаге
  `promo_plan_pct` всё равно заполняется как справочная информация.
- **Колонки.** `promo_plan_pct` («WB просит скидку, %») и
  `promo_cap_applied` («Ограничено акцией») — в таблице автопилота и в
  Excel-экспорте (`pricing_export`).

---

## 14. R16/T-33: межсвязочное влияние и минимальные цены

Функциональность работает поверх автопилота и **не заменяет** обычные правила
R1–R15: она лишь подтягивает итоговую строку вверх (влияние) либо отправляет
пол безубыточности (мин. цены).

### 14.1. Главная связка (влияние цен между кабинетами)

- «Главная связка» — настройка пользователя (`users.main_cabinet_id`); выбирается
  в панели «Личные кабинеты» (селект «Главная связка»), API —
  `POST /api/cabinet/main {id|null}`. `null` = не следовать ни за кем.
- Если у активной связки `follow_main = true` (по умолчанию) и задана главная
  (и это не та же связка), сервер считает свежую рекомендацию главной (read-only,
  под её собственной схемой) и строит карту `article → Δ%` по строкам
  **`action = RAISE`** главной: `Δ = (target_vis − current_vis) / current_vis × 100`.
  Настройки главной берутся её собственные (`follow_main` у неё принудительно
  снят — без рекурсии).
- Локальная строка зависимой связки поднимается как минимум на
  `main_influence_pct`% от Δ (по умолчанию 50% = половина). Правила:
  - влияют **только повышения** главной; её снижения игнорируются;
  - цена **не опускается**: если локальный расчёт и так поднимает сильнее
    (горячий спрос) — строка не трогается;
  - пол безубыточности (`floor_price`) соблюдается;
  - выше базовой цены не поднять — если скидка уже 0%,
    строка помечается `status = skipped_no_headroom`;
  - влияние применяется **поверх** `min_delta_pp` (иначе мелкие половинки глохнут).
- Строки, затронутые влиянием, получают `influenced_by_main = true` и
  `main_delta_pct` (Δ главной, %); колонка «Влияние главной, %» в таблице
  автопилота и в экспорте Excel.
- Пустая карта (нет главной / ошибка / нет пересечений) — влияние просто не
  применяется, расчёт зависимой связки не падает.

### 14.2. Минимальные (безубыточные) цены

- Кнопка «Минимальные цены» в шапке автопилота (WB и Ozon):
  API — `POST /api/pricing/apply-min` для WB (тело как у `/pricing/apply`; опционально
  `ui_rows` — видимые строки таблицы, иначе сервер пересчитывает всё);
  для Ozon — `POST /api/pricing-ozon/apply-min`.
- Отправляются **только строки, у которых текущая витринная цена ниже пола**
  (`current_vis < floor_price`); ставятся `price = пол` (WB — целое число после клампа), `discount = 0`.
- Порог WB (`priceLimits.minPrice`) применяется как кламп истинного минимума:
  итог = `max(пол, мин. цена WB)`.
- Пишется журнал `price_changes` с `action = MIN` (в кулдауне автопилота не
  участвует).
- В таблице автопилота (WB) добавлены поля воронки продаж: `Просмотры`, `Добавления в корзину`,
  `Заказы`, `Отмены`, `Просмотр→Корзина, %`, `Корзина→Заказ, %`, `Заказов в день` (данные из
  среза воронки; доступно через «Вид таблицы»/экспорт).