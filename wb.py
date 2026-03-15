import logging
import sqlite3
import time
import random
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import ParseMode
from aiogram.utils import executor

# Абсолютный путь к файлу базы данных
db_path = "/home/wb/wb/forbidden_words.db"
# Подключение к базе данных
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Укажите здесь ваш токен от BotFather
TOKEN = '6321570817:AAEahOkgHyAT5BWJb6Gx2C0_fxEr49DBiRI'
subscription_check_enabled = False

# Инициализируем бота и диспетчера
bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# Словарь для хранения статусов пользователей
user_statuses = {}

# Идентификатор владельца (внутренний Telegram ID)
owner_id = 382254550

# Идентификатор канала (куда надо подписаться)
CHANNEL_ID = "-1001605638152"

subscribed_users = set()
ADMIN_IDS = [382254550]
admin_ids = ADMIN_IDS
# Команда /start
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    user_id = message.from_user.id
    if user_id == owner_id:
        await message.reply("Привет, я бот! Ты владелец.")
    elif user_id in user_statuses and user_statuses[user_id] == "admin":
        await message.reply("Привет, я бот! Ты админ.")
    else:
        await message.reply("Привет, я бот!")



def check_sub_channel(chat_member):
    if hasattr(chat_member, 'status') and chat_member.status not in ['left', 'kicked']:
        return True
    else:
        return False
# Command /on_sub (accessible only to admins)
@dp.message_handler(commands=['on_sub'])
async def enable_subscription_check(message: types.Message):
    global subscription_check_enabled
    if message.from_user.id in admin_ids or message.from_user.id == owner_id:
        subscription_check_enabled = True
        await message.reply("Проверка подписки включена.")
    else:
        await message.reply("У вас нет прав на эту команду.")

# Command /off_sub (accessible only to admins)
@dp.message_handler(commands=['off_sub'])
async def disable_subscription_check(message: types.Message):
    global subscription_check_enabled
    if message.from_user.id in admin_ids or message.from_user.id == owner_id:
        subscription_check_enabled = False
        await message.reply("Проверка подписки отключена.")
    else:
        await message.reply("У вас нет прав на эту команду.")

# Command /sub_check (accessible only to admins)
@dp.message_handler(commands=['sub_check'])
async def check_subscription_status(message: types.Message):
    global subscription_check_enabled
    if message.from_user.id in admin_ids or message.from_user.id == owner_id:
        if subscription_check_enabled:
            await message.reply(f"Проверка подписки включена. Количество подписчиков: {len(subscribed_users)}")
        else:
            await message.reply("Проверка подписки отключена.")
    else:
        await message.reply("У вас нет прав на эту команду.")

# Command /sub_check (accessible only to admins)
@dp.message_handler(commands=['sub_check'])
async def check_subscription_status(message: types.Message):
    if message.from_user.id in admin_ids or message.from_user.id == owner_id:
        if subscribed_users:
            await message.reply(f"Проверка подписки включена. Количество подписчиков: {len(subscribed_users)}")
        else:
            await message.reply("Проверка подписки отключена.")
    else:
        await message.reply("У вас нет прав на эту команду.")

# Command /offsubinthisgroup (accessible only to admins in the respective group)
@dp.message_handler(commands=['offsubinthisgroup'], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def disable_subscription_check_in_group(message: types.Message):
    if message.from_user.id in admin_ids:
        global subscribed_users
        subscribed_users = set()
        await message.reply("Проверка подписки в данной группе отключена.")
    else:
        await message.reply("У вас нет прав на эту команду в данной группе.")

# Command /onsubinthisgroup (accessible only to admins in the respective group)
@dp.message_handler(commands=['onsubinthisgroup'], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def enable_subscription_check_in_group(message: types.Message):
    if message.from_user.id in admin_ids:
        await message.reply("Проверка подписки в данной группе включена.")
    else:
        await message.reply("У вас нет прав на эту команду в данной группе.")

# Command /checksubinthisgroup (accessible only to admins in the respective group)
@dp.message_handler(commands=['checksubinthisgroup'], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def check_subscription_status_in_group(message: types.Message):
    if message.from_user.id in admin_ids:
        if subscribed_users:
            await message.reply(f"Проверка подписки в данной группе включена. Количество подписчиков: {len(subscribed_users)}")
        else:
            await message.reply("Проверка подписки в данной группе отключена.")
    else:
        await message.reply("У вас нет прав на эту команду в данной группе.")
# Команда /status (все пользователи могут узнать свой статус)
@dp.message_handler(commands=['status'])
async def status(message: types.Message):
    user_id = message.from_user.id
    if user_id == owner_id:
        await message.reply("Ты владелец бота.")
    elif user_id in user_statuses:
        await message.reply(f"Твой статус: {user_statuses[user_id]}")
    else:
        user_statuses[user_id] = "user"
        await message.reply("Твой статус: Юзер")

# Команда /addadmin (доступна только владельцу)
@dp.message_handler(commands=['addadmin'])
async def add_admin(message: types.Message):
    if message.from_user.id == owner_id:
        if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
            user_id = message.reply_to_message.from_user.id
            user_statuses[user_id] = "admin"
            await message.reply(f"Пользователь с id {user_id} теперь админ.")
        else:
            await message.reply("Для добавления админа ответьте на сообщение пользователя.")
    else:
        await message.reply("У тебя нет прав на эту команду.")

# Команда /deladmin (доступна только владельцу)
@dp.message_handler(commands=['deladmin'])
async def del_admin(message: types.Message):
    if message.from_user.id == owner_id:
        if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
            user_id = message.reply_to_message.from_user.id
            if user_statuses.get(user_id) == "admin":
                user_statuses[user_id] = "user"
                await message.reply(f"Пользователь с id {user_id} больше не админ.")
            else:
                await message.reply(f"Пользователь с id {user_id} не является админом.")
        else:
            await message.reply("Для удаления админа ответьте на сообщение пользователя.")
    else:
        await message.reply("У тебя нет прав на эту команду.")

# Команда /add_word (доступна только владельцу и админам)
@dp.message_handler(commands=['add_word'])
async def add_forbidden_word(message: types.Message):
    if message.from_user.id in [owner_id] + [user_id for user_id, status in user_statuses.items() if status == "admin"]:
        args = message.get_args().strip()
        if not args:
            await message.reply("Пожалуйста, укажите слово для добавления в запрещенный список.")
            return

        cursor.execute("INSERT INTO forbidden_words (word) VALUES (?)", (args,))
        conn.commit()

        await message.reply(f"Слово '{args}' добавлено в запрещенный список.")
    else:
        await message.reply("У тебя нет прав на эту команду.")

# Команда /del_word (доступна только владельцу и админам)
@dp.message_handler(commands=['del_word'])
async def delete_forbidden_word(message: types.Message):
    if message.from_user.id in [owner_id] + [user_id for user_id, status in user_statuses.items() if status == "admin"]:
        args = message.get_args().strip()
        if not args:
            await message.reply("Пожалуйста, укажите слово для удаления из запрещенного списка.")
            return

        cursor.execute("DELETE FROM forbidden_words WHERE word=?", (args,))
        conn.commit()

        await message.reply(f"Слово '{args}' удалено из запрещенного списка.")
    else:
        await message.reply("У тебя нет прав на эту команду.")

# Команда /help (доступна всем пользователям)
@dp.message_handler(commands=['help'])
async def show_help(message: types.Message):
    message_ids_to_delete = [message.message_id, reply_message.message_id] 
    user_id = message.from_user.id

    if user_id == owner_id:
        admin_commands_text = (
            "/addadmin - Добавить пользователя в администраторы (только владелец)\n"
            "/deladmin - Удалить пользователя из администраторов (только владелец)\n"
            "/add_word <слово> - Добавить слово в запрещенный список (только владелец и админ)\n"
            "/del_word <слово> - Удалить слово из запрещенного списка (только владелец и админ)\n"
            "/off_sub - Отключить проверку подписки (только владелец и админ)\n"
            "/on_sub - Включить проверку подписки (только владелец и админ)\n"
            "/sub_check - Показать статус проверки подписки (только владелец и админ)\n"
        )
        await message.reply("Привет! Ты владелец бота.\n\n" + admin_commands_text)
    elif user_id in [admin.user.id for admin in await bot.get_chat_administrators(message.chat.id)]:
        admin_commands_text = (
            "/add_word <слово> - Добавить слово в запрещенный список\n"
            "/del_word <слово> - Удалить слово из запрещенного списка\n"
            "/off_sub - Отключить проверку подписки\n"
            "/on_sub - Включить проверку подписки\n"
            "/sub_check - Показать статус проверки подписки\n"
        )
        await message.reply("Привет! Ты админ.\n\n" + admin_commands_text)
    else:
        user_commands_text = (
            "/start - Приветственное сообщение и информация о боте\n"
            "/status - Узнать свой статус\n"
            "/help - Показать все команды\n"
            "/view_words - Посмотреть список запрещенных слов"
        )
        await message.reply("Привет! Ты обычный пользователь.\n\n" + user_commands_text)



# Команда /view_words (доступна всем пользователям)
@dp.message_handler(commands=['view_words'])
async def view_forbidden_words(message: types.Message):
    # Connect to the database and retrieve forbidden words
    cursor.execute("SELECT word FROM forbidden_words")
    forbidden_words = [row[0] for row in cursor.fetchall()]

    if not forbidden_words:
        forbidden_words1 = "Запрещенных слов нет."
        response = await message.reply(forbidden_words1)
        await asyncio.sleep(15)
        await bot.delete_message(message.chat.id, response.message_id)
    else:
        words_list = "\n".join(forbidden_words)
        forbidden_words2 = f"Запрещенные слова:\n{words_list}"
        response = await message.reply(forbidden_words2)
        await asyncio.sleep(15)
        await bot.delete_message(message.chat.id, response.message_id)


@dp.message_handler(content_types=types.ContentType.TEXT)
async def process_text_messages(message: types.Message):
    # Connect to the database and retrieve forbidden words
    cursor.execute("SELECT word FROM forbidden_words")
    forbidden_words = [row[0] for row in cursor.fetchall()]

    # The code for checking subscription and sending welcome messages
    user: User = message.from_user
    chat: types.Chat = message.chat

    if user.is_bot:
        return

    # Skip processing messages from bots and admins for /start command
    if message.from_user.is_bot or message.from_user.id in ADMIN_IDS:
        return

    # Check if the message is from a user in a group or supergroup for /start command
    if message.chat.type in [types.ChatType.GROUP, types.ChatType.SUPERGROUP]:
        user_id = message.from_user.id

    # The code for checking forbidden links and words
    for entity in message.entities:
        if entity.type in ["url", "text_link"]:
            if chat.type in ['group', 'supergroup'] and user.id not in [admin.user.id for admin in await bot.get_chat_administrators(chat.id)]:
                await bot.delete_message(message.chat.id, message.message_id)
                reply_message = "Если Вы хотите отправлять ссылки со своей рекламой, обратитесь к администратору @supx100"
                await bot.send_message(message.chat.id, reply_message)
                await asyncio.sleep(15)
                await bot.delete_message(message.chat.id, reply_message.message_id)

    # Check if the message contains forbidden words
    if chat.type in ['group', 'supergroup'] and user.id not in [admin.user.id for admin in await bot.get_chat_administrators(chat.id)]:
        for word in forbidden_words:
            if word.lower() in message.text.lower():
                await bot.delete_message(message.chat.id, message.message_id)
                reply_message = "Ваше сообщение содержит запрещенное слово. Для просмотра запретных слов используйте команду /view_words"
                reply_message = await bot.send_message(message.chat.id, reply_message)
                async def delete_message():
                    await asyncio.sleep(15)
                    await bot.delete_message(message.chat.id, reply_message.message_id)
                asyncio.create_task(delete_message())
        # Check if the subscription check is enabled
        global subscription_check_enabled
        if subscription_check_enabled:
            if user_id in subscribed_users:
                return

            if check_sub_channel(await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)):
                subscribed_users.add(user_id)

                reply_message = 'Спасибо за подписку! Можете отправлять сообщения! Однако мы были бы рады, чтобы вы проверили закреп, и общались соблюдая правила группы!'
                await bot.send_message(message.chat.id, reply_message)
                async def delete_message():
                    await asyncio.sleep(15)
                    await bot.delete_message(message.chat.id, reply_message.message_id)
                asyncio.create_task(delete_message())

            else:
                message_to_delete = await bot.send_message(message.chat.id, "Если Вы хотите отправлять свои сообщения -- подпишитесь на канал: https://t.me/art_ecom")
                # Delete the user's message (in a group or supergroup) for /start command
                await bot.delete_message(message.chat.id, message.message_id)

                # Define the timer function to delete the bot's message after 30 seconds
                async def delete_message():
                    await asyncio.sleep(15)
                    await bot.delete_message(message.chat.id, message_to_delete.message_id)
                asyncio.create_task(delete_message())
        else:
            # If the subscription check is disabled, simply allow users to send messages
            return    

    cursor.execute("SELECT word FROM forbidden_words")
    forbidden_words = [row[0] for row in cursor.fetchall()]
    # Собираем message_ids для удаления в одном списке
    message_ids_to_delete = [message.message_id, reply_message.message_id]
    # Удаляем сообщения с задержкой
    await delete_messages(message.chat.id, message_ids_to_delete)

# Запуск бота
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    dp.middleware.setup(LoggingMiddleware())

    conn = sqlite3.connect('forbidden_words.db')
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS forbidden_words (
                        id INTEGER PRIMARY KEY,
                        word TEXT
                    )''')
    conn.commit()

    executor.start_polling(dp, skip_updates=True)
