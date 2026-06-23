# Golden-набор карточек (роль + адрес)

Эталон для проверки **классификации и локации**, не «видит ли бот канал вообще».

## Файлы

| Файл | Содержание |
|------|------------|
| `vacancy_cards_2026_06_23.yaml` | 21 кейс (редактировать здесь) |
| `vacancy_cards_2026_06_23.json` | копия для pytest (генерируется из YAML) |

## Поля кейса

- `expected_ingest` — сохранять в БД (`true` / `false`)
- `expected_primary_category` — код категории (`promoter`, `loader`, …) или `reject`
- `reject_reason` — если не ingest: `spam`, `permanent`, `resume`, `casting`, …
- `expected_address` — одна строка для 📍 на карточке
- `bot_wrong` — что показал бот (для регрессии)
- `notes` — почему так

## Как использовать

1. Таблица для человека: [`docs/GOLDEN_VACANCY_CARDS_2026_06_23.md`](../../docs/GOLDEN_VACANCY_CARDS_2026_06_23.md)
2. Правки ожиданий — в **YAML**, затем пересобрать JSON (команда ниже).
3. `python -m pytest tests/test_golden_vacancy_cards.py -q` — baseline **21/21** по роли (не ухудшать).
4. Строгий прогон по каждому кейсу: `set GOLDEN_STRICT=1` (Windows) / `GOLDEN_STRICT=1 pytest ...`
5. Новые «не та роль / кривой адрес» — добавлять в YAML.

Пересборка JSON после правки YAML:

```bash
python -c "import yaml,json,pathlib; p=pathlib.Path('tests/golden/vacancy_cards_2026_06_23.yaml'); d=yaml.safe_load(p.read_text(encoding='utf-8')); pathlib.Path('tests/golden/vacancy_cards_2026_06_23.json').write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')"
```

См. также [`docs/HUNTER_ARCHITECTURE_MIGRATION.md`](../../docs/HUNTER_ARCHITECTURE_MIGRATION.md).
