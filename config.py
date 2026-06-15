import os
import shutil
import logging
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

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

# Окно свежести вакансий (часы) — при парсинге
VACANCY_MAX_AGE_HOURS = int(os.getenv("VACANCY_MAX_AGE_HOURS", "36"))
# Окно дедупа: одна и та же кампания с новой датой/репостом не сохраняется повторно
VACANCY_DEDUPE_DAYS = int(os.getenv("VACANCY_DEDUPE_DAYS", "7"))

# Лента «Посмотреть новые»: свежие (ч) и архив (макс. возраст, ч)
FEED_FRESH_HOURS = int(os.getenv("FEED_FRESH_HOURS", "24"))
FEED_ARCHIVE_MAX_HOURS = int(os.getenv("FEED_ARCHIVE_MAX_HOURS", "168"))
# История доставленных вакансий (ч) — «📜 История»
FEED_HISTORY_MAX_HOURS = int(os.getenv("FEED_HISTORY_MAX_HOURS", "720"))

# Premium: заявок на добавление канала в мониторинг (шт. / 24 ч)
CHAT_SUGGEST_DAILY_LIMIT = int(os.getenv("CHAT_SUGGEST_DAILY_LIMIT", "3"))

# Пробный Premium при первой регистрации (дней, 0 = отключить)
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))

# Free: сколько категорий без Premium (1 = одна бесплатно, 2+ только Premium)
FREE_CATEGORY_LIMIT = int(os.getenv("FREE_CATEGORY_LIMIT", "1"))

# За сколько дней до конца Premium/Trial слать напоминание (cron, 0 = отключить)
PREMIUM_RENEWAL_REMIND_DAYS = int(os.getenv("PREMIUM_RENEWAL_REMIND_DAYS", "3"))

# Forum topics в личке (BotFather → Threaded Mode)
FORUM_TOPICS_ENABLED = os.getenv("FORUM_TOPICS_ENABLED", "1").strip().lower() in ("1", "true", "yes")

# Канал @promostaff_agency_job — кросс-пост превью
HUNTER_CHANNEL_ID = os.getenv("HUNTER_CHANNEL_ID", "").strip()
if HUNTER_CHANNEL_ID.lstrip("-").isdigit():
    HUNTER_CHANNEL_ID = int(HUNTER_CHANNEL_ID)
else:
    HUNTER_CHANNEL_ID = None
CHANNEL_CROSSPOST_ENABLED = os.getenv("CHANNEL_CROSSPOST_ENABLED", "0").strip().lower() in ("1", "true", "yes")
BOT_USERNAME = os.getenv("BOT_USERNAME", "PromostaffHunter_bot").strip().lstrip("@")

# LLM (deepseek_gateway)
LLM_ENABLED = os.getenv("LLM_ENABLED", "0").strip().lower() in ("1", "true", "yes")
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "").strip()
LLM_INTERNAL_TOKEN = os.getenv("LLM_INTERNAL_TOKEN", "").strip()
LLM_TIMEOUT_SEC = float(os.getenv("LLM_TIMEOUT_SEC", "25"))
LLM_DAILY_LIMIT_PREMIUM = int(os.getenv("LLM_DAILY_LIMIT_PREMIUM", "20"))
# Черновик над полем ввода (sendMessageDraft, Bot API 9.3+) — «живой» LLM
LLM_MESSAGE_DRAFT_ENABLED = os.getenv("LLM_MESSAGE_DRAFT_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)
# Rich-черновик над полем ввода (sendRichMessageDraft + tg-thinking, Bot API 10.1)
LLM_RICH_MESSAGE_DRAFT_ENABLED = os.getenv("LLM_RICH_MESSAGE_DRAFT_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Живое фото в канале (sendLivePhoto, Bot API 10.0): PNG + парный короткий MP4
CHANNEL_LIVE_PHOTO_ENABLED = os.getenv("CHANNEL_LIVE_PHOTO_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Rich Messages для карточек вакансий (sendRichMessage, Bot API 10.1)
RICH_VACANCY_CARDS_ENABLED = os.getenv("RICH_VACANCY_CARDS_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Telegram Stars — отклик и расширенный отклик
STARS_ENABLED = os.getenv("STARS_ENABLED", "0").strip().lower() in ("1", "true", "yes")
STARS_RESPONSE_PRICE = int(os.getenv("STARS_RESPONSE_PRICE", "3"))
STARS_EXTENDED_RESPONSE_PRICE = int(os.getenv("STARS_EXTENDED_RESPONSE_PRICE", "35"))

# Платные отклики после trial (Free): пакет или Stars за штуку
PAID_RESPONSES_ENABLED = os.getenv("PAID_RESPONSES_ENABLED", "1").strip().lower() in ("1", "true", "yes")
RESPONSE_PACK_CREDITS = int(os.getenv("RESPONSE_PACK_CREDITS", "5"))
RESPONSE_PACK_PRICE_RUB = int(os.getenv("RESPONSE_PACK_PRICE_RUB", "99"))
# Trial при первом отклике (не при выборе категорий)
TRIAL_ON_FIRST_RESPONSE = os.getenv("TRIAL_ON_FIRST_RESPONSE", "1").strip().lower() in ("1", "true", "yes")

# Telethon Settings
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
# Bothost / shared volume: $SHARED_DIR → /app/shared/user_session.session
SHARED_DIR = os.getenv("SHARED_DIR", "").strip()
_session_migrate_done = False


def get_shared_dir() -> str | None:
    """Bothost «Общее хранилище» — переживает git-deploy."""
    for path in (SHARED_DIR, "/app/shared"):
        if path and os.path.isdir(path):
            return path
    return None


def get_default_session_name() -> str:
    shared = get_shared_dir()
    if shared:
        return os.path.join(shared, "user_session")
    return "user_session"


def migrate_legacy_session_to_shared() -> str | None:
    """Копирует user_session.session из /app в shared, если в shared ещё нет."""
    shared = get_shared_dir()
    if not shared:
        return None
    target_base = os.path.join(shared, "user_session")
    target_file = f"{target_base}.session"
    if os.path.isfile(target_file) and os.path.getsize(target_file) > 64:
        return target_base

    legacy_candidates = [
        "/app/user_session.session",
        os.path.join(os.getcwd(), "user_session.session"),
        "user_session.session",
    ]
    for legacy in legacy_candidates:
        if not os.path.isfile(legacy) or os.path.getsize(legacy) <= 64:
            continue
        os.makedirs(shared, exist_ok=True)
        shutil.copy2(legacy, target_file)
        journal = f"{legacy}-journal"
        if os.path.isfile(journal):
            shutil.copy2(journal, f"{target_file}-journal")
        logger.info("Telethon session перенесена: %s → %s", legacy, target_file)
        return target_base
    return None


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
    """Путь к сессии Telethon (без .session). Bothost: предпочитаем /app/shared."""
    global _session_migrate_done
    explicit = os.getenv("TELEGRAM_SESSION_NAME", "").strip()
    if explicit:
        return explicit

    if not _session_migrate_done:
        migrate_legacy_session_to_shared()
        _session_migrate_done = True

    for directory in _session_search_dirs():
        picked = _pick_session_from_dir(directory)
        if picked:
            return picked

    return get_default_session_name()


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

# Keywords for vacancy relevance (legacy KEYWORDS; scoring — parser._CATEGORY_KEYWORDS)
HELPER_KEYWORDS = [
    "хелпер", "хелперы", "хэлпер", "хэлперы",
    "хелпер на мероприят", "хэлпер на мероприят",
    "промоутер", "промоутеры", "промо",
    "аниматор", "аниматоры", "анимация",
    "грузчик", "грузчики",
    "разнорабочий", "разнорабочие", "такелажник", "такелаж",
    "погрузка", "разгрузка", "склад", "сортировка", "упаковка", "комплектовка",
    "подсобный рабочий", "подсобный",
    "фасовщик", "сборщик", "упаковщик", "комплектовщик",
    "маляр", "штукатур", "разнорабочие",
    "массовка", "актёр", "актер",
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
    "резюме в лс", "присылайте программу", "присылайте видео", "ищу исполнителя",
    "open call",
    "подписаться на max", "наши каналы в vk", "бот для отправки объявлений",
    "реферальная система", "тестируем сеть", "распознавать образы",
    "на связи владелец чата",
]

# For compatibility
KEYWORDS = HELPER_KEYWORDS + HIRING_VERBS + ONE_TIME_JOB_KEYWORDS + PAYMENT_INDICATORS
EXCLUDE_WORDS = EXCLUDE_CATEGORIES + STOP_PHRASES
