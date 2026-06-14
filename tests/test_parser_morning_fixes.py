"""Регрессии по утренней ленте 08.06.2026 и аудиту хелперов 07–09.06."""

from parser import (
    detect_category,
    detect_duplicate_type,
    evaluate_vacancy,
    is_digital_work_spam,
    is_massovka_or_film_extras,
    is_office_staff_spam,
    is_permanent_job_spam,
)


def test_massovka_clip_rejected_not_animator():
    text = (
        "‼️ МОСКВА. МАССОВКА на съемки клипа (9 ИЮНЯ) ‼️\n"
        "💰 Оплата: 500 ₽ за проект (сразу после съемок )\n"
        "ФИО\n"
        "@casting_manager"
    )
    assert is_massovka_or_film_extras(text) is True
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "massovka_film"
    assert detect_category(text) != "animator" or not ok


def test_driver_pickup_with_loader_is_loader():
    text = (
        "На сегодня 13:00\n"
        "1 грузчик( всего два)\n"
        "Метро румянцево ,водитель заберет\n"
        "Ездить по заправкам забирать металл\n"
        "450/4/1800\n"
        "@logist"
    )
    ok, cat, _, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "loader"


def test_digital_helper_spam_rejected():
    text = (
        "📱 **Удаленная подработка: Помощник / Ассистент (со смартфона)**\n"
        "Работа с готовым контентом. Публиковать материалы по шаблону.\n"
        "Оплата 5000 р/мес\n"
        "@remote_boss"
    )
    assert is_digital_work_spam(text) is True
    ok, _, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "digital_work_spam"


def test_permanent_loader_job_rejected():
    text = (
        "На постоянную основу нужен грузчик. В пару к водителю.\n"
        "Оплата 2 раза в месяц, в среднем 70 000 - 85 000 месяц.\n"
        "Метро Румянцево\n"
        "@hr_permanent"
    )
    assert is_permanent_job_spam(text) is True
    ok, _, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason == "permanent_job"


def test_event_helper_with_unload_prefers_helper():
    text = (
        "📍ул. Вильгельма Пика, 16\n"
        "с 8 до 19:00\n"
        "550 р/час\n"
        "Требуются хелперы ( разгрузить, расставить, приготовить площадку к мероприятию)\n"
        "☎️@Fd4Daria"
    )
    ok, cat, _, _ = evaluate_vacancy(text)
    assert ok is True
    assert cat == "helper"


def test_krylatskoe_helper_loader_hybrid_accepted():
    text = (
        "Сегодня 19:30\n"
        "м Крылатская \n"
        "Дальше на такси на площадку ( 15 минут езды ) такси закажем \n\n"
        "Нужно  4 хелпера - грузчика \n\n"
        "Демонтаж декор, реквизит, стулья \n"
        "Загрузка машины \n\n"
        "Оплата   4000  - 5000\n"
        "Работы на 8-10 часов \n\n"
        "Возраст 18 + \n\n"
        "Пишите telegram \n"
        "+79262369170"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True, reason
    assert cat == "helper"


def test_bolotnaya_helper_functional_accepted():
    text = (
        "Дата : сегодня \n"
        "Место : болотная площадь - Метро Третьяковская \n"
        "Оплата: 500 рублей в час \n"
        "Минималка 5 часов \n"
        "Функционал: хелперский функционал : принеси, унести, помочь с расстановкой стульев\n"
        "В лс пишем. +79991234567"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True, reason
    assert cat == "helper"


def test_valeria_helper_event_ls_contact():
    text = (
        "Хелперы на мероприятие  парни с 9,10 июня \n"
        "Задачи: разгрузка коробок, помощь на регистрации \n"
        "Ставка 400/час \n"
        "Для записи в личные сообщения Фио, номер телефона. @valeria_hr"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True, reason
    assert cat == "helper"


def test_fried_eggs_montage_helper():
    text = (
        "Парк Митино\n"
        "С 22:00 до 10:00\n"
        "Помощь на монтаже ( принеси, подай , выгрузка машины)\n"
        "Ставка 500 в час.\n"
        "Пишите в ЛС @boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True, reason
    assert cat == "helper"


def test_elena_pomosh_masteram_loader():
    text = (
        "‼️завтра к 9:00‼️\n"
        "🧍🏻 2 человека РФ РБ\n"
        "🚇 Чкаловская\n"
        "💸450/8/3600₽ после смены.\n"
        "Фронт работы: помощь мастерам , работа на лесах\n"
        "📲 писать в лс 89773120997 Елена"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True, reason
    assert cat == "loader"


def test_fd4daria_visiting_cards_promoter():
    text = (
        "Девушки! 14 июня \n"
        "600 р/час 15-20:00\n"
        "РАЗДАЧА ВИЗИТОК (работаем на улице)\n"
        "☎️@Fd4Daria"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True, reason
    assert cat == "promoter"


def test_campaign_duplicate_same_channel(monkeypatch):
    base = (
        "🇷🇺РАЗДАЧА ЛИСТОВОК С РЕЧЕВКОЙ🇷🇺 10-21 ИЮНЯ\n"
        "⚡️Длительный проект⚡️\n"
        "💸2500 руб смена💸\n"
        "ФИО, возраст\n"
        "@promo1"
    )
    variant = (
        "🇷🇺РАЗДАЧА ЛИСТОВОК С РЕЧЕВКОЙ🇷🇺 10-21 ИЮНЯ\n"
        "⚡️Длительный проект⚡️\n"
        "📍ул. Маршала Бирюзова, д. 17\n"
        "💸2500 руб смена💸\n"
        "@promo2"
    )

    def fake_exact(*_a, **_k):
        return False

    def fake_recent(*_a, **_k):
        return [{
            "id": "v1",
            "message_text": base,
            "author_contact": "@promo1",
            "dedupe_key": "k1",
            "source_chat_title": "Promo Channel",
            "category_code": "promoter",
        }]

    monkeypatch.setattr("parser.has_recent_duplicate_vacancy", fake_exact)
    monkeypatch.setattr("parser.get_recent_open_vacancies_for_dedupe", fake_recent)

    dup = detect_duplicate_type(
        variant, "@promo2", "k2", "promoter", "Promo Channel",
    )
    assert dup == "campaign"


def test_office_personal_assistant_not_helper():
    text = (
        "**Требуется личный ****#помощник**** в офис**\n"
        "‼️Поиск срочный ‼️\n"
        "Разбор заявок ✅\n"
        "Выкладка рекламы в соц. сети\n"
        "Помощь с ведением документооборота (по шаблону)\n"
        "1). Настольные игры и зоны отдыха😎\n"
        "2). Гибкий график\n"
        "**Оплата: 80.000-100.00 в месяц на руки **\n"
        "Находимся на м. Университет\n"
        "@office_hr"
    )
    assert is_office_staff_spam(text) is True
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason in ("office_staff_job", "permanent_job")
    assert cat != "helper" or not ok


def test_krasnopresnenskaya_sixteen_helpers_accepted():
    text = (
        "г. Москва, Краснопресненская набережная, дом 12\n"
        "С 9 на 10 июня\n"
        "С 21:00-15:00\n"
        "Нужны 16 хэлперов \n"
        "Помощь на демонтаже, выгрузить мебель, помощь монтажникам, "
        "мелкие поручения принеси/подай, больше монтажных работ, "
        "но хэлперские задачи тоже будут\n"
        "Ставка 500 в час \n"
        "ОПЛАТА 10, край 11 ого ИЮНЯ!!!\n"
        "ПО СМЗ\n"
        "пишите в ЛС\n"
        "@event_crew"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is True, reason
    assert cat == "helper"


def test_bakery_dishwasher_not_loader():
    text = (
        "На постоянную работу в пекарню 🍞 нужна посудомойщица-уборщица,\n"
        "котломой день\n"
        "котломой ночь\n"
        "МОЙКА-УБОРКА:\n"
        "✅6/1 по 14 часов смена (7-21 и 10-24)\n"
        "✅360 руб/час,5040 смена\n"
        "✅От 130 тр в месяц\n"
        "✅Официальное оформление, оплачиваемый отпуск и больничный;\n"
        "✅Нужен полный пакет документов.\n"
        "МОЙЩИК КОТЛОВ:\n"
        "✅6/1 по 14 часов смена 7-21 или 19-7(НОЧЬ СМЕНА)\n"
        "📌м. Китай-город\n"
        "@bakery_hr"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok is False
    assert reason in ("permanent_job", "non_event_labor", "ambiguous_category", "quality_gate:loader")
    assert cat != "loader" or not ok


def test_technician_with_driver_license_plus_is_helper_not_driver():
    text = (
        "В связи с расширением в небольшой прокат концертного оборудования "
        "требуется #**техник** (с возможным ростом до старшего)\n"
        "**Не обязательно с опытом**. Главное — быть молодым, адекватным.\n"
        "Работы много. **Зарплата по рынку. **\n"
        "Если есть водительские права — это большой плюс.\n"
        "📝 Пиши в личные сообщения\n"
        "@rental_boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert detect_category(text) != "driver"
    if ok:
        assert cat == "helper"
    else:
        assert reason != "quality_gate:driver"


def test_electrician_vacancy_classified_as_electrician():
    text = (
        "Требуются :\n"
        "-электромонтажники-мастера\n"
        "-электромонтажники-помощники\n\n"
        "Работа в магазинах в отдельностоящих зданиях.\n"
        "Инструмент, СИЗы, расходники и материал выдаем!\n"
        "Оплата 500 р/ч\n"
        "+79198040280"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok, reason
    assert cat == "electrician"
    assert detect_category(text) == "electrician"


def test_booth_montage_not_helper():
    text = (
        "Нужны монтажники стендов на выставку\n"
        "Сборка конструкций, баннеры, Octanorm\n"
        "4500 руб за смену @stand_hr"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok, reason
    assert cat == "booth"


def test_misc_fallback_when_hiring_and_payment_but_no_role():
    text = (
        "Срочно на объект, нужны 3 человека\n"
        "Работа с 10:00 до 18:00\n"
        "Оплата 3000 руб\n"
        "@shift_boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok, reason
    assert cat == "misc"


def test_helper_montazhnik_on_event_still_helper():
    text = (
        "Нужны 2 хелпера на мероприятие\n"
        "Помощь монтажникам сцен, расстановка\n"
        "500 р/ч @eventboss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok, reason
    assert cat == "helper"


def test_loader_takelazh_keywords():
    text = (
        "Нужны 2 грузчика на такелаж\n"
        "Подъём на этаж, разгрузка фуры\n"
        "4500 руб за смену @loader_hr"
    )
    ok, cat, reason, _ = evaluate_vacancy(text)
    assert ok, reason
    assert cat == "loader"
