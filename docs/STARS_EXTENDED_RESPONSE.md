# Расширенный отклик (Telegram Stars)

> **Сборка:** `tool-v4`  
> **Premium в рублях** — без изменений, см. [`SUBSCRIPTION.md`](SUBSCRIPTION.md).

## Что это

Разовая микро-услуга за **Telegram Stars** на экране отклика к вакансии:

1. LLM-черновик текста (если включён `LLM_ENABLED` и у пользователя Premium).
2. Префикс в тексте для заказчика: «⭐ Приоритетный отклик через Promostaff Hunter».
3. Флаг `responses.star_boost = true` для аналитики.

Обычный отклик (шаблон анкеты + deeplink) остаётся **бесплатным**.

## Цена

Env `STARS_EXTENDED_RESPONSE_PRICE` — количество Stars (по умолчанию **35**).  
Включение: `STARS_ENABLED=1` + Payments → Stars в BotFather.

## UX

1. Пользователь нажимает «Откликнуться» на вакансии.
2. Видит черновик и кнопки:
   - «✨ Улучшить текст» — только Premium (`LLM_ENABLED=1`).
   - «⭐ Расширенный отклик» — invoice Stars (если `STARS_ENABLED=1`).
3. После оплаты — готовый текст в теме «📨 Отклики» + кнопка «Открыть чат и отправить».

Повторная покупка той же вакансии блокируется (`has_star_purchase_for_vacancy`).

## Техника

| Элемент | Значение |
|---------|----------|
| Payload invoice | `ext_resp:{vacancy_id}` |
| Валюта | `XTR` |
| Таблица | `star_purchases` |
| Handler | `successful_payment` → `complete_star_purchase` |

## Env (Bothost)

```env
STARS_ENABLED=1
STARS_EXTENDED_RESPONSE_PRICE=35
LLM_ENABLED=1
LLM_GATEWAY_URL=…
LLM_INTERNAL_TOKEN=…
```

## Не входит

- Подписка Premium за Stars.
- Буст вакансии для заказчика.
- Вывод Stars на расчётный счёт.
