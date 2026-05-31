import asyncio
import re
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from db import init_db
from parser import get_new_messages, get_last_debug_report
from config import BOT_TOKEN, YOUR_USER_ID

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
periodic_task = None  # Глобальная переменная для хранения задачи

def escape_markdown(text: str) -> str:
    """Экранирует спецсимволы Markdown"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def format_order_message(order: dict) -> str:
    """Формирует безопасное сообщение с MarkdownV2"""
    chat_title = escape_markdown(order['chat_title'])
    message_text = escape_markdown(order['message_text'][:500])
    message_link = order['message_link']
    
    return (
        f"🔔 *{chat_title}*\n\n"
        f"{message_text}\n\n"
        f"📎 [Перейти к сообщению]({message_link})"
    )

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "👋 Бот запущен!\n\n"
        "Команды:\n"
        "/check_now - Проверить новые заказы\n"
        "/status - Статус бота\n"
        "/debug_last - Отчёт последнего прогона парсера"
    )

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    status_text = "📊 Статус бота:\n"
    status_text += f"✅ Polling активен\n"
    status_text += f"🔄 Периодическая проверка: {'запущена' if periodic_task and not periodic_task.done() else 'остановлена'}\n"
    status_text += f"⏱️ Интервал проверки: 5 минут"
    
    await message.answer(status_text)

@dp.message(Command("check_now"))
async def check_now_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    status_msg = await message.answer("🔍 Начинаю проверку...")
    
    try:
        orders = await get_new_messages()
        
        if not orders:
            await status_msg.edit_text("✅ Новых заказов не найдено.")
            return
        
        sent_count = 0
        for order in orders:
            try:
                text = format_order_message(order)
                await bot.send_message(
                    YOUR_USER_ID, 
                    text, 
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True
                )
                sent_count += 1
                await asyncio.sleep(0.5)  # Задержка между отправкой сообщений
                
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения: {e}")
                # Fallback - без форматирования
                plain_text = f"🔔 {order['chat_title']}\n\n{order['message_text'][:500]}\n\n📎 {order['message_link']}"
                try:
                    await bot.send_message(YOUR_USER_ID, plain_text)
                    sent_count += 1
                except Exception as e2:
                    logger.error(f"Не удалось отправить даже plain текст: {e2}")
        
        await status_msg.edit_text(f"✅ Проверка завершена. Найдено и отправлено {sent_count} из {len(orders)} заказов.")
        
    except Exception as e:
        logger.error(f"Ошибка в check_now_cmd: {e}")
        await status_msg.edit_text(f"❌ Ошибка при проверке: {str(e)[:100]}")


@dp.message(Command("debug_last"))
async def debug_last_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    await message.answer(get_last_debug_report())

async def periodic_check():
    """Автоматическая проверка каждые 5 минут"""
    logger.info("🔄 Запущена периодическая проверка")
    
    while True:
        try:
            logger.info("🔍 Выполняю периодическую проверку...")
            orders = await get_new_messages()
            
            if orders:
                logger.info(f"📬 Найдено {len(orders)} новых заказов")
                for order in orders:
                    try:
                        text = format_order_message(order)
                        await bot.send_message(
                            YOUR_USER_ID, 
                            text, 
                            parse_mode="MarkdownV2",
                            disable_web_page_preview=True
                        )
                        await asyncio.sleep(0.5)
                        
                    except Exception as e:
                        logger.error(f"Ошибка отправки: {e}")
                        plain_text = f"🔔 {order['chat_title']}\n\n{order['message_text'][:500]}\n\n📎 {order['message_link']}"
                        await bot.send_message(YOUR_USER_ID, plain_text)
            else:
                logger.info("✅ Нет новых заказов")
                
        except Exception as e:
            logger.error(f"Ошибка в periodic_check: {e}", exc_info=True)
        
        # Ждём 5 минут
        await asyncio.sleep(300)

async def on_startup():
    """Действия при запуске бота"""
    logger.info("🚀 Запуск бота...")
    
    # Инициализируем БД
    init_db()
    logger.info("📁 База данных инициализирована")
    
    # Запускаем периодическую проверку
    global periodic_task
    periodic_task = asyncio.create_task(periodic_check())
    
    # Отправляем уведомление о запуске
    try:
        await bot.send_message(
            YOUR_USER_ID, 
            "🟢 Бот запущен и начал мониторинг заказов.\n"
            "Интервал проверки: 5 минут\n"
            "Команды: /check_now, /status"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление о запуске: {e}")

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("🛑 Остановка бота...")
    
    # Отменяем периодическую задачу
    global periodic_task
    if periodic_task and not periodic_task.done():
        periodic_task.cancel()
        try:
            await periodic_task
        except asyncio.CancelledError:
            logger.info("✅ Периодическая задача отменена")
    
    # Закрываем сессию бота
    await bot.session.close()
    
    # Отправляем уведомление о остановке (если возможно)
    try:
        await bot.send_message(YOUR_USER_ID, "🔴 Бот остановлен.")
    except:
        pass
    
    logger.info("👋 Бот остановлен")

async def main():
    """Главная функция запуска"""
    try:
        # Выполняем действия при запуске
        await on_startup()
        
        # Запускаем polling
        logger.info("📡 Запуск polling...")
        await dp.start_polling(bot)
        
    except KeyboardInterrupt:
        logger.info("⚠️ Получен сигнал остановки")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
    finally:
        # Выполняем действия при остановке
        await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
