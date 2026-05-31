# config.example.py - ОБРАЗЕЦ, НЕ СОДЕРЖИТ РЕАЛЬНЫХ ТОКЕНОВ

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")  # Берётся из .env
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
YOUR_USER_ID = int(os.getenv("YOUR_USER_ID", "0"))

# Список чатов
TARGET_CHATS = [
    "https://t.me/he1pers",
    # ... и так далее
]

# Ключевые слова
HELPER_KEYWORDS = ["хелпер", "промоутер", ...]
# ... и так далее
