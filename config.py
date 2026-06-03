import os
from dotenv import load_dotenv
load_dotenv()

# Bot Settings
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUR_USER_ID = int(os.getenv("YOUR_USER_ID", "0"))
# Ссылка на оплату подписки (Telegram Stars invoice URL, ЮKassa, T-Bank — по желанию)
SUBSCRIPTION_PAY_URL = os.getenv("SUBSCRIPTION_PAY_URL", "").strip()
SUBSCRIPTION_SUPPORT = os.getenv("SUBSCRIPTION_SUPPORT", "@promostaff_support").strip()
SUBSCRIPTION_PRICE_RUB = os.getenv("SUBSCRIPTION_PRICE_RUB", "299").strip()
SUBSCRIPTION_CARD_HINT = os.getenv("SUBSCRIPTION_CARD_HINT", "").strip()

# Окно свежести вакансий (часы) — вместо «только сегодня» по МСК
VACANCY_MAX_AGE_HOURS = int(os.getenv("VACANCY_MAX_AGE_HOURS", "36"))

# Пробный Premium при первой регистрации (дней, 0 = отключить)
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))

# Telethon Settings
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
# Bothost / shared volume: $SHARED_DIR → /app/shared/user_session.session
SHARED_DIR = os.getenv("SHARED_DIR", "").strip()


def _resolve_telegram_session_name() -> str:
    """Путь к сессии Telethon (без .session). Выбирает первый существующий файл."""
    explicit = os.getenv("TELEGRAM_SESSION_NAME", "").strip()
    if explicit:
        return explicit

    candidates = []
    if SHARED_DIR:
        candidates.append(os.path.join(SHARED_DIR, "user_session"))
    if os.path.isdir("/app/shared"):
        candidates.append("/app/shared/user_session")
    candidates.append("user_session")  # /app/user_session.session при cwd=/app

    for base in candidates:
        if os.path.isfile(f"{base}.session"):
            return base

    # Bothost иногда сохраняет upload как tmpXXXX.session — берём единственный .session в shared
    for shared_root in filter(None, [SHARED_DIR or None, "/app/shared" if os.path.isdir("/app/shared") else None]):
        try:
            session_files = sorted(
                f for f in os.listdir(shared_root)
                if f.endswith(".session") and not f.endswith(".session-journal")
            )
        except OSError:
            continue
        if len(session_files) == 1:
            fname = session_files[0]
            return os.path.join(shared_root, fname[: -len(".session")])

    if SHARED_DIR:
        return os.path.join(SHARED_DIR, "user_session")
    if os.path.isdir("/app/shared"):
        return "/app/shared/user_session"
    return "user_session"


TELEGRAM_SESSION_NAME = _resolve_telegram_session_name()

# List of chats for monitoring
TARGET_CHATS = [
    # Существующие группы
    "https://t.me/he1pers",
    "https://t.me/eventbaran",
    "https://t.me/eventori",
    "https://t.me/EventFamily",
    "https://t.me/rabotakastingi",
    "https://t.me/mskeventjob",
    "https://t.me/myeventhunter",
    "https://t.me/EVENT_Assist_chat",
    "https://t.me/gruzchiki_rabota_podrabotka",
    
    # Новые группы
    "https://t.me/HelpersTeamRa",
    "https://t.me/worldeventjob",
    "https://t.me/HelpersTeamRaGruz",
    "https://t.me/horoshievakansii",
    "https://t.me/helpvkino",
    "https://t.me/GRUZCHIKI_ZAKAZY_89099816950",
    "https://t.me/stoodent_moskva",
    "https://t.me/WorkExpo",
    "https://t.me/promo_moskva",
    "https://t.me/creonvacancy",
    "https://t.me/goodjobmsk",
    "https://t.me/rabota_promo_event",
    "https://t.me/lis_person",
    "https://t.me/workermoscowstudents",
    "https://t.me/steward_MB",
    "https://t.me/CENSORED_prod",
    "https://t.me/modelshostes",
    "https://t.me/moscowworkvsem",
    "https://t.me/meetplanet",
    "https://t.me/Flexit_RabotaMSK",
    "https://t.me/Mos_Prom",
    "https://t.me/WorkEventMoscow",
    "https://t.me/libertytime1",
    "https://t.me/nemelochi",
    "https://t.me/majorevents",
    
    # Канал "Работа №1 Москва" (это канал, а не группа, может потребоваться другой подход)
    # "https://t.me/rabota1_msk",  # Работа №1 Москва
]

# Keywords for helper search
HELPER_KEYWORDS = [
    "хелпер", "хелперы", "хэлпер", "хэлперы", "helper", "helpers",
    "промоутер", "промоутеры", "промо", "promo",
    "аниматор", "аниматоры", "анимация",
    "грузчик", "грузчики",
    "разнорабочий", "разнорабочие", "такелажник", "такелаж",
    "погрузка", "разгрузка", "склад", "сортировка", "упаковка", "комплектовка",
    "подсобный рабочий", "подсобный",
    "фасовщик", "сборщик", "упаковщик", "комплектовщик",
    "маляр", "штукатур", "разнорабочие",
    "массовка", "актёр", "актер"  # можно добавить, если хотите видеть и такие
]

HIRING_VERBS = [
    "ищу", "ищем", "нужен", "нужна", "нужны", "требуется", "требуются",
    "найм", "вакансия", "подработка", "замена", "срочно",
]

ONE_TIME_JOB_KEYWORDS = [
    "на сегодня", "на завтра", "раздача", "помощь на площадке",
    "руб/час", "р/час", "₽/час", "смена",
]

PAYMENT_INDICATORS = [
    "оплата", "ставка", "бюджет", "гонорар", "зп", "зарплата", "руб", "₽",
]

EXCLUDE_CATEGORIES = [
    "музыкант", "саксофонист", "вокалист", "певец", "диджей", "актер", "актриса", "модель",
    "фотограф", "видеограф", "ведущий", "аренда", "кастинг", "вебинар",
]

STOP_PHRASES = [
    "агентство полного цикла", "продаем", "на себя", "ищу работу", "резюме",
]

# For compatibility
KEYWORDS = HELPER_KEYWORDS + HIRING_VERBS + ONE_TIME_JOB_KEYWORDS + PAYMENT_INDICATORS
EXCLUDE_WORDS = EXCLUDE_CATEGORIES + STOP_PHRASES
