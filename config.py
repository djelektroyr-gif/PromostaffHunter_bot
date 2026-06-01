import os
from dotenv import load_dotenv
load_dotenv()

# Bot Settings
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUR_USER_ID = int(os.getenv("YOUR_USER_ID", "0"))

# Telethon Settings
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")

# List of chats for monitoring
TARGET_CHATS = [
    # Существующие группы
    "https://t.me/he1pers",
    "https://t.me/eventbaran",
    "https://t.me/eventori",
    "https://t.me/meropriyatiyachat",
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
    "аниматор", "аниматоры", "анимация", "грузчик", "грузчики",
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
