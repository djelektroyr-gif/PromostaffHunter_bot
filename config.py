import os
from dotenv import load_dotenv
load_dotenv()

# PostgreSQL (прод Bothost): если задан — вместо SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

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


def _session_search_dirs() -> list[str]:
    """Каталоги, где ищем *.session (Bothost: /app и /app/shared)."""
    dirs = []
    for path in (SHARED_DIR, "/app/shared", "/app", os.getcwd()):
        if path and os.path.isdir(path) and path not in dirs:
            dirs.append(path)
    return dirs


def _pick_session_from_dir(directory: str) -> str | None:
    try:
        session_files = sorted(
            f for f in os.listdir(directory)
            if f.endswith(".session") and not f.endswith(".session-journal")
        )
    except OSError:
        return None
    if not session_files:
        return None
    if "user_session.session" in session_files:
        return os.path.join(directory, "user_session")
    fname = session_files[0]
    return os.path.join(directory, fname[: -len(".session")])


def get_telegram_session_name() -> str:
    """Путь к сессии Telethon (без .session). Имя файла может быть любым — tmp*.session тоже."""
    explicit = os.getenv("TELEGRAM_SESSION_NAME", "").strip()
    if explicit:
        return explicit

    for directory in _session_search_dirs():
        picked = _pick_session_from_dir(directory)
        if picked:
            return picked

    return "user_session"


def describe_session_search() -> str:
    """Диагностика для логов — куда смотрели и что нашли."""
    lines = [f"cwd={os.getcwd()}"]
    if SHARED_DIR:
        lines.append(f"SHARED_DIR={SHARED_DIR}")
    for directory in _session_search_dirs():
        try:
            sessions = sorted(
                f for f in os.listdir(directory)
                if f.endswith(".session") and not f.endswith(".session-journal")
            )
            lines.append(f"{directory}: {sessions or '(нет .session)'}")
        except OSError as e:
            lines.append(f"{directory}: ошибка ({e})")
    return "; ".join(lines)


# Обратная совместимость (не использовать для Telethon — только runtime get_telegram_session_name)
TELEGRAM_SESSION_NAME = "user_session"


def get_database_path() -> str:
    """SQLite на Bothost — лучше в /app/shared, чтобы не терять подписчиков при git-deploy."""
    explicit = os.getenv("DATABASE_PATH", "").strip()
    if explicit:
        return explicit
    shared_candidates = [p for p in (SHARED_DIR, "/app/shared") if p and os.path.isdir(p)]
    for directory in shared_candidates:
        shared_db = os.path.join(directory, "bot_database.db")
        if os.path.isfile(shared_db):
            return shared_db
    if shared_candidates:
        return os.path.join(shared_candidates[0], "bot_database.db")
    return "bot_database.db"

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
