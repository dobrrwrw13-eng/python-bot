import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
import pytz
from pathlib import Path
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Firebase
try:
    import firebase_admin
    from firebase_admin import credentials, firestore, initialize_app
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    logging.warning("Firebase не установлен. Установите: pip install firebase-admin")

# =======================
# НАЛАШТУВАННЯ
# =======================
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "students.db")
ANNOUNCEMENT_FILES_DIR = os.path.join(BASE_DIR, "announcement_files")
FIREBASE_CREDENTIALS_PATH = os.path.join(BASE_DIR, "serviceAccountKey.json")

# Email Namecheap конфигурация
EMAIL_CONFIG = {
    'smtp_server': os.getenv('EMAIL_SMTP_SERVER', 'mail.privateemail.com'),
    'smtp_port': int(os.getenv('EMAIL_SMTP_PORT', '587')),
    'email': os.getenv('EMAIL_FROM', ''),
    'password': os.getenv('EMAIL_PASSWORD', ''),
    'use_tls': os.getenv('EMAIL_USE_TLS', 'True').lower() == 'true'
}

# Створюємо папку для файлів оголошень
Path(ANNOUNCEMENT_FILES_DIR).mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# =======================
# БД (SQLite -> students.db)
# =======================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


DB = db_connect()


def db_init() -> None:
    DB.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            fio TEXT NOT NULL,
            class_name TEXT NOT NULL,
            role TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            welcomed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    DB.commit()

    cols = [row["name"] for row in DB.execute("PRAGMA table_info(users)").fetchall()]
    if "tg_id" not in cols:
        DB.execute("ALTER TABLE users ADD COLUMN tg_id INTEGER")
        DB.commit()
        DB.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id)")
        DB.commit()
    if "events_notifications" not in cols:
        DB.execute("ALTER TABLE users ADD COLUMN events_notifications INTEGER NOT NULL DEFAULT 1")
        DB.commit()
    
    # Таблиця для розкладу
    DB.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_name TEXT NOT NULL,
            day_name TEXT NOT NULL,
            lesson_number INTEGER NOT NULL,
            subject TEXT NOT NULL,
            teacher TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            UNIQUE(class_name, day_name, lesson_number)
        )
        """
    )
    DB.commit()
    
    # Таблиця для відправлених сповіщень (щоб не спамити)
    DB.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_phone TEXT NOT NULL,
            class_name TEXT NOT NULL,
            day_name TEXT NOT NULL,
            lesson_number INTEGER NOT NULL,
            sent_date TEXT NOT NULL
        )
        """
    )
    DB.commit()


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D+", "", phone or "")


def db_get_user(phone_norm: str):
    cur = DB.execute("SELECT * FROM users WHERE phone = ?", (phone_norm,))
    row = cur.fetchone()
    return dict(row) if row else None


def db_get_user_by_tg(tg_id: int):
    cur = DB.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def db_bind_tg_to_phone(tg_id: int, phone_norm: str) -> None:
    DB.execute("UPDATE users SET tg_id = ? WHERE phone = ?", (tg_id, phone_norm))
    DB.commit()


def db_upsert_user(phone_norm: str, fio: str, class_name: str, role: str = "учень") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = db_get_user(phone_norm)
    if existing:
        DB.execute(
            "UPDATE users SET fio = ?, class_name = ?, role = ? WHERE phone = ?",
            (fio, class_name, role, phone_norm),
        )
    else:
        DB.execute(
            """
            INSERT INTO users (phone, fio, class_name, role, registered_at, welcomed)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (phone_norm, fio, class_name, role, now),
        )
    DB.commit()


def db_is_welcomed(phone_norm: str) -> bool:
    user = db_get_user(phone_norm)
    return bool(user) and int(user["welcomed"]) == 1


def db_set_welcomed(phone_norm: str) -> None:
    DB.execute("UPDATE users SET welcomed = 1 WHERE phone = ?", (phone_norm,))
    DB.commit()


def db_toggle_events_notifications(phone_norm: str) -> None:
    user = db_get_user(phone_norm)
    current = int(user["events_notifications"]) if user else 1
    new_value = 1 - current
    DB.execute("UPDATE users SET events_notifications = ? WHERE phone = ?", (new_value, phone_norm))
    DB.commit()


def db_get_events_notifications(phone_norm: str) -> bool:
    user = db_get_user(phone_norm)
    return bool(user) and int(user["events_notifications"]) == 1


def db_get_user_role(phone_norm: str) -> str:
    """Отримати роль користувача"""
    user = db_get_user(phone_norm)
    return user.get("role", "учень") if user else "учень"


def is_admin(phone_norm: str) -> bool:
    """Перевірити, чи є користувач адміністратором"""
    return db_get_user_role(phone_norm) == "admin"


def db_set_user_role(phone_norm: str, role: str) -> None:
    """Встановити роль користувача"""
    DB.execute("UPDATE users SET role = ? WHERE phone = ?", (role, phone_norm))
    DB.commit()


# =======================
# ФУНКЦІЇ ДЛЯ РОЗКЛАДУ
# =======================
def db_insert_schedule(class_name: str, day_name: str, lesson_number: int, subject: str, teacher: str, start_time: str, end_time: str) -> None:
    DB.execute(
        """
        INSERT OR REPLACE INTO schedule (class_name, day_name, lesson_number, subject, teacher, start_time, end_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (class_name, day_name, lesson_number, subject, teacher, start_time, end_time),
    )
    DB.commit()


def db_get_schedule_for_user_today(phone_norm: str) -> list:
    """Отримати розклад для юзера на сьогодні"""
    user = db_get_user(phone_norm)
    if not user:
        return []
    
    class_name = user["class_name"]
    
    # Отримуємо назву дня тижня (англійська)
    days_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days_ua = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
    
    today_en = days_en[datetime.now().weekday()]
    # Конвертуємо на українську
    day_index = days_en.index(today_en)
    day_ua = days_ua[day_index]
    
    cur = DB.execute(
        "SELECT * FROM schedule WHERE class_name = ? AND day_name = ? ORDER BY lesson_number",
        (class_name, day_ua),
    )
    return [dict(row) for row in cur.fetchall()]


def db_get_upcoming_class(phone_norm: str, minutes_ahead: int = 30) -> dict:
    """Отримати наступне заняття в межах minutes_ahead хвилин (сегодня или завтра)"""
    user = db_get_user(phone_norm)
    if not user:
        logging.debug(f"  ⚠ Користувач {phone_norm} не знайдено")
        return None
    
    class_name = user["class_name"]
    now = datetime.now()
    
    # Дні тижня
    days_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days_ua = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
    
    today_en = days_en[now.weekday()]
    today_index = days_en.index(today_en)
    today_ua = days_ua[today_index]
    
    logging.debug(f"  Перевірка розкладу о {now.strftime('%H:%M:%S')} ({today_ua}), пошук уроків на наступні {minutes_ahead} хвилин")
    
    upcoming_lessons = []
    
    # === ПРОВЕРЯЕМ СЕГОДНЯ ===
    cur = DB.execute(
        "SELECT * FROM schedule WHERE class_name = ? AND day_name = ? ORDER BY lesson_number",
        (class_name, today_ua),
    )
    today_schedule = [dict(row) for row in cur.fetchall()]
    logging.debug(f"  Знайдено {len(today_schedule)} уроків сьогодні для {class_name}")
    
    for lesson in today_schedule:
        try:
            start_parts = lesson["start_time"].split(":")
            start_time = now.replace(hour=int(start_parts[0]), minute=int(start_parts[1]), second=0, microsecond=0)
            
            # Время до начала урока в минутах
            time_diff = (start_time - now).total_seconds() / 60
            
            logging.debug(f"    Урок {lesson['lesson_number']}: {lesson['subject']} о {lesson['start_time']} - за {time_diff:.1f} хв")
            
            # Если урок в будущем (даже если далеко), добавляем в список
            if time_diff >= 0:
                upcoming_lessons.append({
                    'lesson': lesson,
                    'time_diff': time_diff,
                    'day': today_ua
                })
                
                # Если в пределах 30 минут - берем этот
                if time_diff <= minutes_ahead:
                    logging.info(f"    → ЗБІГ СЬОГОДНІ! {lesson['subject']} о {lesson['start_time']} (за {time_diff:.1f} хв)")
                    return lesson
        except (ValueError, KeyError) as e:
            logging.error(f"    ПОМИЛКА розбору уроку {lesson.get('lesson_number', '?')}: {e}")
            continue
    
    # === ЯКЩО СЬОГОДНІ НЕ ЗНАЙДЕНО, ПЕРЕВІРЯЄМО ЗАВТРА ===
    logging.debug(f"  Сьогодні не знайдено уроків у вікні {minutes_ahead} хв, перевіряємо завтра...")
    
    tomorrow_index = (today_index + 1) % 7
    tomorrow_ua = days_ua[tomorrow_index]
    tomorrow_date = now + timedelta(days=1)
    
    cur = DB.execute(
        "SELECT * FROM schedule WHERE class_name = ? AND day_name = ? ORDER BY lesson_number",
        (class_name, tomorrow_ua),
    )
    tomorrow_schedule = [dict(row) for row in cur.fetchall()]
    logging.debug(f"  Знайдено {len(tomorrow_schedule)} уроків завтра ({tomorrow_ua}) для {class_name}")
    
    for lesson in tomorrow_schedule:
        try:
            start_parts = lesson["start_time"].split(":")
            start_time = tomorrow_date.replace(hour=int(start_parts[0]), minute=int(start_parts[1]), second=0, microsecond=0)
            time_diff = (start_time - now).total_seconds() / 60
            
            logging.debug(f"    Завтрашній урок {lesson['lesson_number']}: {lesson['subject']} о {lesson['start_time']} - за {time_diff:.1f} хв")
            
            # Если в пределах 30 минут от сейчас
            if 0 <= time_diff <= minutes_ahead:
                logging.info(f"    → ЗБІГ ЗАВТРА! {lesson['subject']} о {lesson['start_time']} (за {time_diff:.1f} хв)")
                return lesson
                
            upcoming_lessons.append({
                'lesson': lesson,
                'time_diff': time_diff,
                'day': tomorrow_ua
            })
        except (ValueError, KeyError) as e:
            logging.error(f"    ПОМИЛКА розбору завтрашнього уроку: {e}")
    
    logging.debug(f"  ✗ Не знайдено майбутніх уроків у вікні {minutes_ahead} хв")
    return None


def check_notification_already_sent(phone_norm: str, class_name: str, day_name: str, lesson_number: int) -> bool:
    """Перевірити, чи вже було відправлено сповіщення сьогодні"""
    today = datetime.now().strftime("%Y-%m-%d")
    cur = DB.execute(
        "SELECT COUNT(*) as cnt FROM notifications_sent WHERE user_phone = ? AND class_name = ? AND day_name = ? AND lesson_number = ? AND sent_date LIKE ?",
        (phone_norm, class_name, day_name, lesson_number, f"{today}%"),
    )
    row = cur.fetchone()
    return row["cnt"] > 0


def db_record_notification_sent(phone_norm: str, class_name: str, day_name: str, lesson_number: int) -> None:
    """Записати, що сповіщення було відправлено"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    DB.execute(
        "INSERT INTO notifications_sent (user_phone, class_name, day_name, lesson_number, sent_date) VALUES (?, ?, ?, ?, ?)",
        (phone_norm, class_name, day_name, lesson_number, now),
    )
    DB.commit()


# =======================
# FSM состояния
# =======================
class Reg(StatesGroup):
    waiting_for_phone = State()
    confirm_found_fio = State()
    input_fio = State()
    confirm_input_fio = State()
    choose_class = State()
    confirm_class = State()


class Form(StatesGroup):
    waiting_for_class = State()
    waiting_for_day = State()


class Teachers(StatesGroup):
    waiting_for_subject = State()


class Settings(StatesGroup):
    main_menu = State()


class AdminAnnouncement(StatesGroup):
    waiting_for_announcement = State()
    waiting_for_file = State()


# =======================
# ПАРСУВАННЯ РОЗКЛАДУ
# =======================
# =======================
# КЛАВИАТУРЫ
# =======================
def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Предмети"), KeyboardButton(text="Розклад")],
            [KeyboardButton(text="Параметри"), KeyboardButton(text="Події")],
        ],
        resize_keyboard=True,
    )


def kb_share_phone():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Поділитися", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_yes_no():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Так"), KeyboardButton(text="Ні")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_classes():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="10-А"), KeyboardButton(text="10-Б")],
            [KeyboardButton(text="11-А"), KeyboardButton(text="11-Б")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_schedule_classes():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="10-А"), KeyboardButton(text="11-А")],
            [KeyboardButton(text="10-Б"), KeyboardButton(text="11-Б")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def kb_days():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Понеділок"), KeyboardButton(text="Вівторок")],
            [KeyboardButton(text="Середа"), KeyboardButton(text="Четвер")],
            [KeyboardButton(text="П'ятниця")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


# ===== Вчителі: предмети (опора на предмети з розкладу)
# ❌ "Математика" УДАЛЕНА
SUBJECTS = [
    "Алгебра",
    "Геометрія",
    "Фізика та астрономія",
    "Хімія",
    "Українська мова",
    "Українська література",
    "Іноземна мова",
    "Історія України",
    "Всесвітня історія",
    "Зарубіжна література",
    "Біологія та екологія",
    "Географія",
    "Інформатика",
    "Захист України",
    "Правознавство",
    "Громадянська освіта",
    "Фізична культура",
    "Фінансова грамотність",
    "Мистецтво",
    "Практикум усного і писемного мовлення",
    "Практикум з математики",
    "Практичні основи юридичної проф.",
    "Година куратора",
]


def kb_subjects():
    rows = []
    row = []
    for s in SUBJECTS:
        row.append(KeyboardButton(text=s))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# =======================
# ВЧИТЕЛІ (на основі розкладу) + кабінети
# ✅ Алгебра/Геометрія -> ДВЕ УЧИЛКИ
# ❌ "Математика" ключ УДАЛЕН
# =======================
TEACHERS = {
    "Алгебра": [
        {"name": "Погребнюк Н.О.", "cab": "110"},
        {"name": "Білик Ю.П.", "cab": "101"},
    ],
    "Геометрія": [
        {"name": "Погребнюк Н.О.", "cab": "110"},
        {"name": "Білик Ю.П.", "cab": "101"},
    ],

    "Фізика та астрономія": [{"name": "Хомчак В.М.", "cab": "304"}],
    "Хімія": [{"name": "Нечитайло М.М.", "cab": "302"}],
    "Українська мова": [
        {"name": "Слободянюк Л.М.", "cab": "201"},
    ],
    "Українська література": [{"name": "Королюк Г.Ф.", "cab": "306"}],
    "Іноземна мова": [
        {"name": "Журавель О.Д.", "cab": "311"},
    ],
    "Практикум з математики": [
        {"name": "Погребнюк Н.О.", "cab": "110"},
        {"name": "Білик Ю.П.", "cab": "101"},
    ],
    "Практикум усного і писемного мовлення": [{"name": "Шостаківська Г.Г.", "cab": "—"}],
    "Історія України": [{"name": "Харитонова І.В.", "cab": "207"}],
    "Всесвітня історія": [{"name": "Маліновський Ю.Л.", "cab": "307"}],
    "Зарубіжна література": [{"name": "Середюк С.Д.", "cab": "103"}],
    "Біологія та екологія": [{"name": "Новак В.В.", "cab": "302"}],
    "Географія": [{"name": "Косюк Н.А.", "cab": "203"}],
    "Інформатика": [
        {"name": "Зеленюк С.В.", "cab": "—"},
        {"name": "Білик Ю.П.", "cab": "—"},
    ],
    "Захист України": [
        {"name": "Радлієвський В.В.", "cab": "—"},
    ],
    "Громадянська освіта": [{"name": "Лемпій О.В.", "cab": "306"}, {"name": "Гуцол Д.О.", "cab": "306"}],
    "Правознавство": [{"name": "Лемпій О.В.", "cab": "103"}],
    "Фізична культура": [{"name": "Прухніцький Е.А.", "cab": "спортзал"}],
    "Фінансова грамотність": [{"name": "Блідченко Н.Г.", "cab": "107"}],
    "Мистецтво": [{"name": "Гуцол Д.О.", "cab": "102"}],
    "Практичні основи юридичної проф.": [{"name": "Супрун-Ковальчук Т.М.", "cab": "206"}],
    "Година куратора": [{"name": "Куратор класу", "cab": "Ваша аудиторія"}],
}


def format_teachers(subject: str) -> str:
    items = TEACHERS.get(subject, [])
    if not items:
        return "Поки що немає інформації по цьому предмету."
    lines = [f"**{subject}**\n"]
    for t in items:
        lines.append(f"• {t['name']} — каб. {t['cab']}")
    return "\n".join(lines)


# =======================
# РАСПИСАНИЯ ХРАНЯТСЯ В БД
# =======================
# schedule удален - данные загружаются из коллекции schedule в базе данных



# =======================
# ВСПОМОГАТЕЛЬНОЕ
# =======================
def is_valid_fio(text: str) -> bool:
    parts = [p for p in (text or "").split() if p.strip()]
    return len(parts) >= 3


async def show_main_menu(message: types.Message):
    await message.answer("Оберіть одну з опцій:", reply_markup=kb_main())


# =======================
# ФУНКЦИИ ОТПРАВКИ ПИСЕМ
# =======================
async def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Отправить письмо через Namecheap SMTP"""
    try:
        # Создаём сообщение
        message = MIMEMultipart('alternative')
        message['Subject'] = subject
        message['From'] = EMAIL_CONFIG['email']
        message['To'] = to_email
        
        # Добавляем HTML версию письма
        html_part = MIMEText(html_body, 'html', 'utf-8')
        message.attach(html_part)
        
        # Отправляем через SMTP
        with smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port']) as server:
            if EMAIL_CONFIG['use_tls']:
                server.starttls()
            
            server.login(EMAIL_CONFIG['email'], EMAIL_CONFIG['password'])
            server.send_message(message)
        
        logging.info(f"✅ Письмо отправлено на {to_email}")
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке письма на {to_email}: {e}")
        return False


def format_acceptance_email(fio: str, app_id: str) -> str:
    """Форматирует письмо о принятии заявки"""
    return f"""
    <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background: #f9f9f9; border-radius: 10px; }}
                .header {{ text-align: center; margin-bottom: 20px; }}
                .status {{ color: #28a745; font-size: 18px; font-weight: bold; }}
                .details {{ background: white; padding: 15px; border-radius: 5px; margin: 15px 0; }}
                .footer {{ text-align: center; color: #666; font-size: 12px; margin-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>✅ Вітаємо, {fio}!</h1>
                </div>
                
                <p>Привіт, <strong>{fio}</strong>!</p>
                
                <p>Команда нашого коледжу розглянула Вашу заявку і з задоволенням повідомляємо, що вона була прийнята!</p>
                
                <div class="details">
                    <p><strong>Деталі заявки:</strong></p>
                    <p>ID Заявки: <code>{app_id}</code></p>
                    <p class="status">✅ Статус: ПРИЙНЯТА</p>
                </div>
                
                <p>Ми вважаємо, що Ви будете чудовим доповненням до нашої спільноти. Команда коледжу зв'яжеться з Вами в найближчим часом з подальшими деталями та інформацією про наступні кроки.</p>
                
                <p>Дякуємо за Вашу заяву та довіру до нашого коледжу!</p>
                
                <p style="margin-top: 30px; font-style: italic;">З найкращими побажаннями,<br>Команда коледжу</p>
                
                <div class="footer">
                    <p>Це автоматичне повідомлення. Будь ласка, не відповідайте на цей лист.</p>
                </div>
            </div>
        </body>
    </html>
    """


def format_rejection_email(fio: str, app_id: str) -> str:
    """Форматирует письмо об отклонении заявки"""
    return f"""
    <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background: #f9f9f9; border-radius: 10px; }}
                .header {{ text-align: center; margin-bottom: 20px; }}
                .status {{ color: #dc3545; font-size: 18px; font-weight: bold; }}
                .details {{ background: white; padding: 15px; border-radius: 5px; margin: 15px 0; }}
                .footer {{ text-align: center; color: #666; font-size: 12px; margin-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Результати розгляду заявки, {fio}</h1>
                </div>
                
                <p>Привіт, <strong>{fio}</strong>!</p>
                
                <p>Дякуємо за Вашу заявку до нашого коледжу. Команда коледжу ретельно розглянула Вашу заявку та набір документів.</p>
                
                <div class="details">
                    <p><strong>Деталі заявки:</strong></p>
                    <p>ID Заявки: <code>{app_id}</code></p>
                    <p class="status">❌ На жаль, ми не змогли взяти Вашу заявку</p>
                </div>
                
                <p>Ми ціним Вашу зацікавленість нашим коледжем. У цьому конкурсному відборі ми мали можливість прийняти обмежену кількість кандидатів, і нам довелося відхилити деякі дуже сильні заявки. Це не означає, що Ваша заявка не мала якості - це просто була складна конкуренція.</p>
                
                <p>Якщо у Вас є запитання щодо результатів розгляду, ви завжди можете звернутися до нашої команди коледжу.</p>
                
                <p>Ми бажаємо Вам успіхів у Вашій освітній подорожі!</p>
                
                <p style="margin-top: 30px; font-style: italic;">З найкращими побажаннями,<br>Команда коледжу</p>
                
                <div class="footer">
                    <p>Це автоматичне повідомлення. Будь ласка, не відповідайте на цей лист.</p>
                </div>
            </div>
        </body>
    </html>
    """


# =======================
# ФУНКЦИИ ДЛЯ РАБОТЫ С НОВОСТЯМИ
# =======================

async def get_latest_news(limit: int = 3) -> list:
    """Получает последние новости из коллекции news"""
    try:
        if applications_listener is None or applications_listener.db is None:
            return []
        
        # Получаем только опубликованные новости (без order_by чтобы избежать нужности индекса)
        docs = (applications_listener.db.collection('news')
                .where('published', '==', True)
                .stream())
        
        news_list = []
        for doc in docs:
            if doc.exists:
                data = doc.to_dict()
                data['id'] = doc.id
                news_list.append(data)
        
        # Сортируем по дате в Python коде
        news_list.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
        
        # Возвращаем только нужное количество
        return news_list[:limit]
    except Exception as e:
        logging.error(f"Ошибка при получении новостей: {e}")
        return []


def format_news_post(news_data: dict) -> str:
    """Форматирует новость для отправки в Telegram"""
    title = news_data.get('title', 'Новина')
    content = news_data.get('content', '')
    author = news_data.get('authorName', 'Невідомий автор')
    category = news_data.get('category', '')
    
    # Удаляем HTML теги для превью
    import re
    content_clean = re.sub('<[^<]+?>', '', content)
    content_preview = content_clean[:150] + '...' if len(content_clean) > 150 else content_clean
    
    text = f"""
📰 <b>{title}</b>

{content_preview}

<i>Категорія: {category}</i>
👤 Автор: {author}
    """
    return text.strip()


# =======================
# FIREBASE LISTENER ДЛЯ ЗАЯВОК
# =======================
class ApplicationsListener:
    """Слушатель заявок з Firebase"""
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.db = None
        self.unsubscribe = None
        self.tracking_applications = set()
        self.loop = None
        
    def _init_firebase(self):
        """Ініціалізація Firebase"""
        if not FIREBASE_AVAILABLE:
            logging.warning("Firebase не доступний")
            return False
            
        try:
            if not os.path.exists(FIREBASE_CREDENTIALS_PATH):
                logging.warning(f"Файл {FIREBASE_CREDENTIALS_PATH} не знайдений")
                return False
                
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
                initialize_app(cred)
            
            self.db = firestore.client()
            logging.info("Firebase ініціалізовано успішно")
            return True
        except Exception as e:
            logging.error(f"Помилка ініціалізації Firebase: {e}")
            return False
    
    def _on_snapshot(self, collection_snapshot, changes, read_time):
        """Callback при зміні заявок в Firestore"""
        try:
            for change in changes:
                doc = change.document
                app_id = doc.id
                app_data = doc.to_dict()
                
                if app_data is None:
                    continue
                
                status = app_data.get('status', '')
                
                # Якщо це нова заявка зі статусом 'new'
                if (change.type.name in ['ADDED', 'MODIFIED'] and 
                    status == 'new' and 
                    app_id not in self.tracking_applications):
                    
                    self.tracking_applications.add(app_id)
                    logging.info(f"🆕 Нова заявка: {app_id}")
                    
                    # Запускаємо async функцію
                    if self.loop and self.loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            self._send_notification_to_admins(app_id, app_data),
                            self.loop
                        )
        except Exception as e:
            logging.error(f"Помилка в _on_snapshot: {e}")
    
    async def _send_notification_to_admins(self, app_id: str, app_data: dict):
        """Відправити сповіщення про заявку адміністраторам"""
        try:
            # Отримуємо всіх адміністраторів з БД
            cur = DB.execute("SELECT tg_id FROM users WHERE role = 'admin' AND tg_id IS NOT NULL")
            admin_tg_ids = [row[0] for row in cur.fetchall()]
            
            if not admin_tg_ids:
                logging.warning("Немає адміністраторів для отримання сповіщення")
                return
            
            # Форматуємо дані заявки
            message_text = self._format_application(app_id, app_data)
            
            # Відправляємо усім адміністраторам
            for admin_id in admin_tg_ids:
                try:
                    await self.bot.send_message(
                        admin_id,
                        message_text,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="📋 Переглянути заявку",
                                    callback_data=f"view_app_{app_id}"
                                )]
                            ]
                        )
                    )
                    logging.info(f"✅ Сповіщення відправлено адміну {admin_id}")
                except Exception as e:
                    logging.error(f"Помилка при відправці адміну {admin_id}: {e}")
        except Exception as e:
            logging.error(f"Помилка при відправці сповіщень адміністраторам: {e}")
    
    def _format_application(self, app_id: str, app_data: dict) -> str:
        """Форматує дані заявки для повідомлення"""
        timestamp = app_data.get('timestamp', 'Невідомо')
        name = app_data.get('name', 'Невідомо')
        email = app_data.get('email', 'Невідомо')
        phone = app_data.get('phone', 'Невідомо')
        specialty = app_data.get('specialty', 'Невідомо')
        message = app_data.get('message', 'Немає повідомлення')
        status = app_data.get('status', 'new')
        
        return (
            f"🆕 <b>Нова заявка!</b>\n\n"
            f"📋 <b>ID заявки:</b> <code>{app_id}</code>\n\n"
            f"👤 <b>Ім'я:</b> {name}\n"
            f"📧 <b>Електронна пошта:</b> {email}\n"
            f"📱 <b>Телефон:</b> {phone}\n"
            f"🎓 <b>Спеціальність:</b> {specialty}\n\n"
            f"💬 <b>Повідомлення:</b>\n{message}\n\n"
            f"⏰ <b>Час:</b> {timestamp}\n"
            f"✅ <b>Статус:</b> {status}"
        )
    
    def start_listening(self, loop):
        """Запустити слухання заявок"""
        if not self._init_firebase():
            logging.warning("Не вдалось ініціалізувати Firebase")
            return
            
        self.loop = loop
        
        try:
            # Завантажуємо існуючі заявки
            docs = self.db.collection('applications').stream()
            for doc in docs:
                self.tracking_applications.add(doc.id)
            
            logging.info(f"✅ Завантажено {len(self.tracking_applications)} існуючих заявок")
            
            # Запускаємо слухача
            self.unsubscribe = self.db.collection('applications').on_snapshot(
                self._on_snapshot
            )
            logging.info("✅ Слушатель Firestore запущен")
        except Exception as e:
            logging.error(f"Помилка при запуску слухача: {e}")
    
    def stop_listening(self):
        """Зупинити слухання"""
        try:
            if self.unsubscribe:
                self.unsubscribe()
                logging.info("✅ Слушатель Firestore зупинено")
        except Exception as e:
            logging.error(f"Помилка при зупинці слухача: {e}")


# =======================
# NEWS LISTENER - СЛУШАТЕЛЬ НОВОСТЕЙ
# =======================

class NewsListener:
    """Слушатель для нових новин з Firebase"""
    
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.db = None
        self.unsubscribe = None
        self.tracking_news = set()
        self.loop = None
        
    def _init_firebase(self):
        """Ініціалізація Firebase"""
        if not FIREBASE_AVAILABLE:
            logging.warning("Firebase не доступний")
            return False
            
        try:
            if not os.path.exists(FIREBASE_CREDENTIALS_PATH):
                logging.warning(f"Файл {FIREBASE_CREDENTIALS_PATH} не знайдений")
                return False
                
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
                initialize_app(cred)
            
            self.db = firestore.client()
            logging.info("Firebase ініціалізовано успішно для NewsListener")
            return True
        except Exception as e:
            logging.error(f"Помилка ініціалізації Firebase для NewsListener: {e}")
            return False
    
    def _on_snapshot(self, collection_snapshot, changes, read_time):
        """Callback при зміні новин в Firestore"""
        try:
            for change in changes:
                doc = change.document
                news_id = doc.id
                news_data = doc.to_dict()
                
                if news_data is None:
                    continue
                
                published = news_data.get('published', False)
                
                # Якщо це нова опублікована новина
                if (change.type.name in ['ADDED', 'MODIFIED'] and 
                    published and 
                    news_id not in self.tracking_news):
                    
                    self.tracking_news.add(news_id)
                    logging.info(f"🆕 Нова новина: {news_id}")
                    
                    # Запускаємо async функцію
                    if self.loop and self.loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            self._send_notification_to_all_users(news_id, news_data),
                            self.loop
                        )
        except Exception as e:
            logging.error(f"Помилка в _on_snapshot (News): {e}")
    
    async def _send_notification_to_all_users(self, news_id: str, news_data: dict):
        """Відправити сповіщення про нову новину всім користувачам з включеними сповіщеннями про події"""
        try:
            # Отримуємо всіх користувачів з БД, у яких включені сповіщення про события
            cur = DB.execute("SELECT tg_id FROM users WHERE tg_id IS NOT NULL AND events_notifications = 1")
            user_tg_ids = [row[0] for row in cur.fetchall()]
            
            if not user_tg_ids:
                logging.warning("Немає користувачів з включеними сповіщеннями про новини")
                return
            
            # Форматуємо дані новини
            message_text = self._format_news_notification(news_id, news_data)
            image_url = news_data.get('image', '')
            
            # Відправляємо користувачам з включеними сповіщеннями
            failed_count = 0
            success_count = 0
            
            for user_id in user_tg_ids:
                try:
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="📖 Читати далі",
                                url=f"https://bgpk-liceum.site/news/{news_id}"
                            )]
                        ]
                    )
                    
                    # Якщо є зображення, відправляємо його з підписом
                    if image_url and image_url.strip():
                        try:
                            await self.bot.send_photo(
                                user_id,
                                photo=image_url,
                                caption=message_text,
                                parse_mode="HTML",
                                reply_markup=keyboard
                            )
                            success_count += 1
                        except Exception as e:
                            logging.debug(f"Помилка при відправці фото користувачу {user_id}: {e}")
                            # Спробуємо відправити без фото
                            try:
                                await self.bot.send_message(
                                    user_id,
                                    message_text,
                                    parse_mode="HTML",
                                    reply_markup=keyboard
                                )
                                success_count += 1
                            except:
                                failed_count += 1
                    else:
                        await self.bot.send_message(
                            user_id,
                            message_text,
                            parse_mode="HTML",
                            reply_markup=keyboard
                        )
                        success_count += 1
                        
                except Exception as e:
                    logging.debug(f"Помилка при відправці користувачу {user_id}: {e}")
                    failed_count += 1
            
            logging.info(f"📰 Сповіщення про новину відправлено: {success_count} успішно, {failed_count} помилок")
        except Exception as e:
            logging.error(f"Помилка при відправці сповіщень про новину: {e}")
    
    def _format_news_notification(self, news_id: str, news_data: dict) -> str:
        """Форматує дані новини для сповіщення"""
        title = news_data.get('title', 'Нова новина')
        content = news_data.get('content', '')
        author = news_data.get('authorName', 'Невідомий автор')
        category = news_data.get('category', '')
        
        # Удаляем HTML теги для превью
        import re
        content_clean = re.sub('<[^<]+?>', '', content)
        content_preview = content_clean[:100] + '...' if len(content_clean) > 100 else content_clean
        
        return (
            f"📰 <b>Нова новина!</b>\n\n"
            f"<b>{title}</b>\n\n"
            f"{content_preview}\n\n"
            f"<i>Категорія: {category}</i>"
        )
    
    def start_listening(self, loop):
        """Запустити слухання новин"""
        if not self._init_firebase():
            logging.warning("Не вдалось ініціалізувати Firebase для новин")
            return
            
        self.loop = loop
        
        try:
            # Завантажуємо існуючі новини
            docs = self.db.collection('news').stream()
            for doc in docs:
                news_data = doc.to_dict()
                if news_data and news_data.get('published'):
                    self.tracking_news.add(doc.id)
            
            logging.info(f"✅ Завантажено {len(self.tracking_news)} існуючих новин")
            
            # Запускаємо слухача
            self.unsubscribe = self.db.collection('news').on_snapshot(
                self._on_snapshot
            )
            logging.info("✅ Слушатель новин Firestore запущен")
        except Exception as e:
            logging.error(f"Помилка при запуску слухача новин: {e}")
    
    def stop_listening(self):
        """Зупинити слухання"""
        try:
            if self.unsubscribe:
                self.unsubscribe()
                logging.info("✅ Слушатель новин Firestore зупинено")
        except Exception as e:
            logging.error(f"Помилка при зупинці слухача новин: {e}")


# Глобальний обробник новин
news_listener = None


# Глобальний обробник заявок
applications_listener = None


@dp.callback_query(lambda query: query.data.startswith("view_app_"))
async def view_application_callback(callback_query: types.CallbackQuery):
    """Обробник для перегляду заявки"""
    try:
        app_id = callback_query.data.replace("view_app_", "")
        
        if not FIREBASE_AVAILABLE or applications_listener is None or applications_listener.db is None:
            await callback_query.answer("Firebase недоступний", show_alert=True)
            return
        
        # Отримуємо заявку з Firebase
        doc = applications_listener.db.collection('applications').document(app_id).get()
        
        if not doc.exists:
            await callback_query.answer("Заявка не знайдена", show_alert=True)
            return
        
        app_data = doc.to_dict()
        message_text = applications_listener._format_application(app_id, app_data)
        
        await callback_query.message.edit_text(
            message_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Прийняти", callback_data=f"accept_app_{app_id}"),
                        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject_app_{app_id}")
                    ],
                    [
                        InlineKeyboardButton(text="🗑️ Видалити", callback_data=f"delete_app_{app_id}"),
                        InlineKeyboardButton(text="◀️ Назад", callback_data="close_app")
                    ]
                ]
            )
        )
        await callback_query.answer()
    except Exception as e:
        logging.error(f"Помилка при перегляді заявки: {e}")
        await callback_query.answer(f"Помилка: {e}", show_alert=True)


@dp.callback_query(lambda query: query.data.startswith("accept_app_"))
async def accept_application_callback(callback_query: types.CallbackQuery):
    """Прийняти заявку"""
    try:
        app_id = callback_query.data.replace("accept_app_", "")
        
        if applications_listener is None or applications_listener.db is None:
            await callback_query.answer("Firebase недоступний", show_alert=True)
            return
        
        # Отримуємо дані заявки
        doc = applications_listener.db.collection('applications').document(app_id).get()
        if not doc.exists:
            await callback_query.answer("Заявка не знайдена", show_alert=True)
            return
        
        app_data = doc.to_dict()
        email = app_data.get('email', '')
        name = app_data.get('name', 'Користувач')
        
        # Оновлюємо статус на "accepted"
        applications_listener.db.collection('applications').document(app_id).update({
            'status': 'accepted',
            'updated_at': datetime.now()
        })
        
        # Відправляємо листа на пошту
        if email:
            email_html = format_acceptance_email(name, app_id)
            await send_email(email, "✅ Ваша заявка прийнята!", email_html)
        
        await callback_query.answer("✅ Заявка прийнята! Лист відправлений.", show_alert=True)
        await callback_query.message.edit_text(
            f"✅ <b>Заявка прийнята</b>\n\n"
            f"ID: <code>{app_id}</code>\n"
            f"Статус успішно змінено на <b>accepted</b>\n"
            f"📧 Лист відправлений на {email}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ Закрити", callback_data="close_app")]]
            )
        )
        logging.info(f"✅ Заявка {app_id} прийнята адміністратором. Лист відправлений на {email}")
    except Exception as e:
        logging.error(f"Помилка при прийнятті заявки: {e}")
        await callback_query.answer(f"Помилка: {e}", show_alert=True)


@dp.callback_query(lambda query: query.data.startswith("reject_app_"))
async def reject_application_callback(callback_query: types.CallbackQuery):
    """Відхилити заявку"""
    try:
        app_id = callback_query.data.replace("reject_app_", "")
        
        if applications_listener is None or applications_listener.db is None:
            await callback_query.answer("Firebase недоступний", show_alert=True)
            return
        
        # Отримуємо дані заявки
        doc = applications_listener.db.collection('applications').document(app_id).get()
        if not doc.exists:
            await callback_query.answer("Заявка не знайдена", show_alert=True)
            return
        
        app_data = doc.to_dict()
        email = app_data.get('email', '')
        name = app_data.get('name', 'Користувач')
        
        # Оновлюємо статус на "rejected"
        applications_listener.db.collection('applications').document(app_id).update({
            'status': 'rejected',
            'updated_at': datetime.now()
        })
        
        # Відправляємо листа на пошту
        if email:
            email_html = format_rejection_email(name, app_id)
            await send_email(email, "❌ Ваша заявка відхилена", email_html)
        
        await callback_query.answer("❌ Заявка відхилена! Лист відправлений.", show_alert=True)
        await callback_query.message.edit_text(
            f"❌ <b>Заявка відхилена</b>\n\n"
            f"ID: <code>{app_id}</code>\n"
            f"Статус успішно змінено на <b>rejected</b>\n"
            f"📧 Лист відправлений на {email}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ Закрити", callback_data="close_app")]]
            )
        )
        logging.info(f"❌ Заявка {app_id} відхилена адміністратором. Лист відправлений на {email}")
    except Exception as e:
        logging.error(f"Помилка при відхиленні заявки: {e}")
        await callback_query.answer(f"Помилка: {e}", show_alert=True)


@dp.callback_query(lambda query: query.data.startswith("delete_app_"))
async def delete_application_callback(callback_query: types.CallbackQuery):
    """Видалити заявку"""
    try:
        app_id = callback_query.data.replace("delete_app_", "")
        
        if applications_listener is None or applications_listener.db is None:
            await callback_query.answer("Firebase недоступний", show_alert=True)
            return
        
        # Видаляємо заявку з Firebase
        applications_listener.db.collection('applications').document(app_id).delete()
        
        # Видаляємо з відстеження
        applications_listener.tracking_applications.discard(app_id)
        
        await callback_query.answer("🗑️ Заявка видалена!", show_alert=True)
        await callback_query.message.edit_text(
            f"🗑️ <b>Заявка видалена</b>\n\n"
            f"ID: <code>{app_id}</code>\n"
            f"Заявка успішно видалена з системи",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ Закрити", callback_data="close_app")]]
            )
        )
        logging.info(f"🗑️ Заявка {app_id} видалена адміністратором")
    except Exception as e:
        logging.error(f"Помилка при видаленні заявки: {e}")
        await callback_query.answer(f"Помилка: {e}", show_alert=True)


@dp.callback_query(lambda query: query.data == "close_app")
async def close_application_callback(callback_query: types.CallbackQuery):
    """Закрити перегляд заявки"""
    await callback_query.message.delete()
    await callback_query.answer()


# =======================
# АДМІНІСТРАТОР: ОГОЛОШЕННЯ
# =======================
@dp.message(Command("admin"))
async def admin_command(message: types.Message, state: FSMContext):
    """Команда для адміністратора: відправка оголошення"""
    tg_id = message.from_user.id
    user = db_get_user_by_tg(tg_id)
    
    if not user:
        await message.answer("Ви не зареєстровані. Спочатку пройдіть реєстрацію.")
        return
    
    phone_norm = user["phone"]
    if not is_admin(phone_norm):
        await message.answer("У вас нет доступа к этой команде. Только администраторы могут отправлять объявления.")
        return
    
    await message.answer(
        "📢 Введіть текст оголошення, яке буде відправлено всім користувачам бота:\n\n"
        "(Ви можете використовувати форматування Markdown: **жирний**, *курсив*, `код`)",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Скасувати")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    await state.set_state(AdminAnnouncement.waiting_for_announcement)


@dp.message(AdminAnnouncement.waiting_for_announcement)
async def process_announcement(message: types.Message, state: FSMContext):
    """Обробник введення оголошення"""
    if message.text == "Скасувати":
        await state.clear()
        await message.answer("Відправка оголошення скасована.", reply_markup=ReplyKeyboardRemove())
        return
    
    if not message.text:
        await message.answer("Будь ласка, введіть текст оголошення.")
        return
    
    announcement_text = message.text.strip()
    if not announcement_text:
        await message.answer("Будь ласка, введіть текст оголошення.")
        return
    
    await state.update_data(announcement_text=announcement_text)
    await message.answer(
        "📎 Тепер виберіть:\n"
        "• Відправте файл (фото, документ тощо) для додання до оголошення\n"
        "• Або натисніть 'Далі' для відправлення без файлу",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Далі")], [KeyboardButton(text="Скасувати")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    await state.set_state(AdminAnnouncement.waiting_for_file)


@dp.message(AdminAnnouncement.waiting_for_file)
async def handle_announcement_file(message: types.Message, state: FSMContext):
    """Обробник для отримання файлу або перевірки команди 'Далі'"""
    data = await state.get_data()
    announcement_text = data.get("announcement_text", "")
    file_path = data.get("file_path")  # Отримуємо збережений файл
    
    # Перевіряємо команди
    if message.text == "Скасувати":
        # Видаляємо файл якщо він існує
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logging.info(f"Файл оголошення видалено: {file_path}")
            except Exception as e:
                logging.error(f"Помилка при видаленні файлу: {e}")
        await state.clear()
        await message.answer("Відправка оголошення скасована.", reply_markup=ReplyKeyboardRemove())
        return
    
    if message.text == "Далі":
        # Відправляємо оголошення з файлом або без
        await send_announcement_to_all(announcement_text, file_path, message.from_user.id)
        await state.clear()
        return
    
    # Обробляємо файли
    file_id = None
    file_name = None
    
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or f"document_{message.document.file_unique_id}"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = f"photo_{message.photo[-1].file_unique_id}.jpg"
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or f"video_{message.video.file_unique_id}.mp4"
    elif message.audio:
        file_id = message.audio.file_id
        file_name = message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
    else:
        await message.answer("Неправильний тип файлу. Будь ласка, відправте документ, фото, відео або аудіо.")
        return
    
    try:
        # Завантажуємо файл
        file = await bot.get_file(file_id)
        file_path = os.path.join(ANNOUNCEMENT_FILES_DIR, file_name)
        await bot.download_file(file.file_path, file_path)
        logging.info(f"Файл оголошення збережено: {file_path}")
        
        await message.answer(
            "✅ Файл отримано!\n\n"
            "Натисніть 'Далі' для відправлення оголошення або відправте ще один файл",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Далі")], [KeyboardButton(text="Скасувати")]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        
        await state.update_data(file_path=file_path, file_id=file_id)
    except Exception as e:
        logging.error(f"Помилка при завантаженні файлу: {e}")
        await message.answer(f"❌ Помилка при завантаженні файлу: {e}")


async def send_announcement_to_all(announcement_text: str, file_path: str, admin_tg_id: int):
    """Відправити оголошення всім користувачам"""
    try:
        from aiogram.types import FSInputFile
        
        cur = DB.execute("SELECT * FROM users WHERE tg_id IS NOT NULL")
        users = [dict(row) for row in cur.fetchall()]
        
        success_count = 0
        error_count = 0
        
        # Створюємо інлайн кнопку для зв'язку з адміністратором
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📞 Зв'язатися", url=f"tg://user?id={admin_tg_id}")]
            ]
        )
        
        for user in users:
            try:
                if file_path and os.path.exists(file_path):
                    # Відправляємо з файлом
                    input_file = FSInputFile(file_path)
                    
                    if file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                        await bot.send_photo(
                            user["tg_id"],
                            input_file,
                            caption=f"📢 **ОГОЛОШЕННЯ:**\n\n{announcement_text}",
                            parse_mode="Markdown",
                            reply_markup=inline_kb
                        )
                    elif file_path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv')):
                        await bot.send_video(
                            user["tg_id"],
                            input_file,
                            caption=f"📢 **ОГОЛОШЕННЯ:**\n\n{announcement_text}",
                            parse_mode="Markdown",
                            reply_markup=inline_kb
                        )
                    elif file_path.lower().endswith(('.mp3', '.wav', '.m4a', '.flac')):
                        await bot.send_audio(
                            user["tg_id"],
                            input_file,
                            caption=f"📢 **ОГОЛОШЕННЯ:**\n\n{announcement_text}",
                            parse_mode="Markdown",
                            reply_markup=inline_kb
                        )
                    else:
                        await bot.send_document(
                            user["tg_id"],
                            input_file,
                            caption=f"📢 **ОГОЛОШЕННЯ:**\n\n{announcement_text}",
                            parse_mode="Markdown",
                            reply_markup=inline_kb
                        )
                else:
                    # Відправляємо просто текстом
                    await bot.send_message(
                        user["tg_id"],
                        f"📢 **ОГОЛОШЕННЯ:**\n\n{announcement_text}",
                        parse_mode="Markdown",
                        reply_markup=inline_kb
                    )
                success_count += 1
            except Exception as e:
                logging.error(f"Помилка при відправці оголошення користувачу {user['tg_id']}: {e}")
                error_count += 1
        
        # Відправляємо звіт адміністратору
        try:
            await bot.send_message(
                admin_tg_id,
                f"✅ Оголошення відправлено!\n\n"
                f"Успішно: {success_count}\n"
                f"Помилок: {error_count}",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logging.error(f"Помилка при відправці звіту адміністратору: {e}")
        
        logging.info(f"Оголошення адміністратора відправлено {success_count} користувачам, {error_count} помилок")
        
        # Видаляємо файл після відправлення
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logging.info(f"Файл оголошення видалено: {file_path}")
            except Exception as e:
                logging.error(f"Помилка при видаленні файлу: {e}")
    
    except Exception as e:
        logging.error(f"Помилка при відправці оголошень: {e}")
        try:
            await bot.send_message(
                admin_tg_id,
                f"❌ Помилка при відправці оголошень: {e}",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e2:
            logging.error(f"Помилка при відправці повідомлення про помилку: {e2}")


@dp.callback_query(lambda query: query.data == "announcement_received")
async def handle_announcement_received(callback_query: types.CallbackQuery):
    """Обробник кнопки 'Отримано'"""
    await callback_query.answer("Дякуємо за увагу!", show_alert=False)
    await callback_query.message.edit_reply_markup(reply_markup=None)


# =======================
# ФОНОВИЙ ТАСК ДЛЯ СПОВІЩЕНЬ
# =======================
async def check_and_notify_upcoming_classes():
    """Фоновий таск який перевіряє розклад та відправляє сповіщення"""
    # Переды уведомления: проверяем в пределах этих минут
    check_windows = [5, 10, 20, 30]
    
    while True:
        try:
            current_time = datetime.now()
            logging.info(f"[BACKGROUND TASK] Checking notifications at {current_time.strftime('%H:%M:%S')} ({current_time.strftime('%A')})")
            
            # Отримуємо всіх активних юзерів
            cur = DB.execute("SELECT * FROM users WHERE tg_id IS NOT NULL")
            users = [dict(row) for row in cur.fetchall()]
            
            logging.debug(f"Found {len(users)} users with Telegram ID")
            
            for user in users:
                tg_id = user["tg_id"]
                phone_norm = user["phone"]
                class_name = user.get("class_name", "unknown")
                
                logging.debug(f"Checking user {phone_norm} (Class: {class_name}, TG: {tg_id})")
                
                # Перевіряємо чи юзер включив сповіщення
                if not db_get_events_notifications(phone_norm):
                    logging.debug(f"User {phone_norm} ({user['fio']}) has notifications disabled")
                    continue
                
                # Проверяем в разных временных окнах
                for minutes_window in check_windows:
                    upcoming = db_get_upcoming_class(phone_norm, minutes_ahead=minutes_window)
                    
                    if upcoming:
                        logging.info(f"→ Upcoming lesson for {class_name}: {upcoming['subject']} at {upcoming['start_time']} (within {minutes_window} min)")
                        
                        # Перевіряємо чи вже було відправлено сповіщення
                        already_sent = check_notification_already_sent(phone_norm, class_name, upcoming["day_name"], upcoming["lesson_number"])
                        
                        if not already_sent:
                            try:
                                # Формуємо повідомлення
                                message_text = (
                                    f"🔔 Уведомлення про нове заняття!\n\n"
                                    f"Предмет: {upcoming['subject']}\n"
                                    f"Вчитель: {upcoming['teacher']}\n"
                                    f"Час: {upcoming['start_time']} - {upcoming['end_time']}\n\n"
                                    f"Поспішай на заняття! 📚"
                                )
                                
                                await bot.send_message(tg_id, message_text)
                                logging.info(f"✓ SENT: Notification to {tg_id} ({user['fio']}) for {upcoming['subject']} at {upcoming['start_time']}")
                                
                                # Записуємо що сповіщення було відправлено
                                db_record_notification_sent(phone_norm, class_name, upcoming["day_name"], upcoming["lesson_number"])
                                
                                # Выходим из цикла по временным окнам (уведомление отправлено)
                                break
                            except Exception as e:
                                logging.error(f"✗ ERROR sending to {tg_id} ({user.get('fio', 'unknown')}): {e}")
                        else:
                            logging.debug(f"⟳ Already notified: {phone_norm} for {upcoming['subject']} ({upcoming['lesson_number']} on {upcoming['day_name']})")
                            # Выходим из цикла (уведомление уже отправлено)
                            break
                    else:
                        logging.debug(f"  ✗ No lessons in {minutes_window} min window")
            
            logging.debug(f"[BACKGROUND TASK] Check completed, waiting 60 seconds...\n")
            # Чекаємо 1 хвилину перед наступною перевіркою
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"✗ CRITICAL ERROR in background task: {e}")
            await asyncio.sleep(60)



@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()

    tg_id = message.from_user.id
    user = db_get_user_by_tg(tg_id)

    if user:
        await message.answer(
            "Ви вже зареєстровані ✅\n\n"
            f"ПІБ: {user['fio']}\n"
            f"Клас: {user['class_name']}"
        )
        await show_main_menu(message)
        return

    await message.answer(
        "Привіт учню. Готовий до нових можливостей? Тоді приєднуйся до нас!\n\n"
        "Щоб почати користування сервісом, треба поділитися номером телефону. "
        "Натисни кнопку «Поділитися» знизу, щоб зробити це.",
        reply_markup=kb_share_phone(),
    )
    await state.set_state(Reg.waiting_for_phone)


# =======================
# ОСНОВНОЙ ХЕНДЛЕР
# =======================
@dp.message()
async def handle_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()

    # --------------------
    # ВЧИТЕЛІ
    # --------------------
    if current_state == Teachers.waiting_for_subject.state:
        subject = message.text
        if subject not in SUBJECTS:
            await message.answer("Оберіть предмет кнопкою нижче 👇", reply_markup=kb_subjects())
            return

        await message.answer(format_teachers(subject), parse_mode="Markdown")
        await state.clear()
        await show_main_menu(message)
        return

    # --------------------
    # РЕЄСТРАЦІЯ
    # --------------------
    if current_state == Reg.waiting_for_phone.state:
        if not message.contact or not message.contact.phone_number:
            await message.answer("Будь ласка, натисніть кнопку «Поділитися», щоб надіслати номер телефону.")
            return

        await message.answer("Дякую ✅", reply_markup=ReplyKeyboardRemove())

        tg_id = message.from_user.id
        phone_norm = normalize_phone(message.contact.phone_number)
        await state.update_data(phone=phone_norm, tg_id=tg_id)

        user_by_phone = db_get_user(phone_norm)
        if user_by_phone:
            await state.update_data(found_fio=user_by_phone["fio"])
            await message.answer(f"Ваш ПІБ: {user_by_phone['fio']}?", reply_markup=kb_yes_no())
            await state.set_state(Reg.confirm_found_fio)
            return

        await message.answer(
            "Вас не було знайдено.\n\n"
            "Напишіть, будь ласка, свій ПІБ.\n\n"
            "Приклад: Іванов Іван Іванович"
        )
        await state.set_state(Reg.input_fio)
        return

    if current_state == Reg.confirm_found_fio.state:
        if message.text not in ("Так", "Ні"):
            await message.answer("Будь ласка, натисніть «Так» або «Ні».", reply_markup=kb_yes_no())
            return

        data = await state.get_data()
        phone_norm = data["phone"]
        tg_id = data["tg_id"]

        if message.text == "Так":
            db_bind_tg_to_phone(tg_id, phone_norm)
            if not db_is_welcomed(phone_norm):
                await message.answer("Вітаю з реєстрацією. Гарного користування!")
                db_set_welcomed(phone_norm)
            await state.clear()
            await show_main_menu(message)
            return

        await message.answer("Введіть вірний ПІБ:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(Reg.input_fio)
        return

    if current_state == Reg.input_fio.state:
        fio = (message.text or "").strip()
        if not is_valid_fio(fio):
            await message.answer("Будь ласка, введіть ПІБ у форматі: Прізвище Ім'я По батькові.\nПриклад: Іванов Іван Іванович")
            return

        await state.update_data(fio=fio)
        await message.answer(f"Ваше ПІБ «{fio}» вірно?", reply_markup=kb_yes_no())
        await state.set_state(Reg.confirm_input_fio)
        return

    if current_state == Reg.confirm_input_fio.state:
        if message.text not in ("Так", "Ні"):
            await message.answer("Будь ласка, натисніть «Так» або «Ні».", reply_markup=kb_yes_no())
            return

        if message.text == "Ні":
            await message.answer("Введіть вірний ПІБ:", reply_markup=ReplyKeyboardRemove())
            await state.set_state(Reg.input_fio)
            return

        await message.answer("Будь ласка, виберіть клас, де Ви навчаєтесь.", reply_markup=kb_classes())
        await state.set_state(Reg.choose_class)
        return

    if current_state == Reg.choose_class.state:
        if message.text not in ("10-А", "10-Б", "11-А", "11-Б"):
            await message.answer("Будь ласка, оберіть клас кнопкою нижче.", reply_markup=kb_classes())
            return

        class_name = message.text
        await state.update_data(class_name=class_name)
        await message.answer(f"Ви обрали «{class_name}». Все вірно?", reply_markup=kb_yes_no())
        await state.set_state(Reg.confirm_class)
        return

    if current_state == Reg.confirm_class.state:
        if message.text not in ("Так", "Ні"):
            await message.answer("Будь ласка, натисніть «Так» або «Ні».", reply_markup=kb_yes_no())
            return

        if message.text == "Ні":
            await message.answer("Будь ласка, виберіть клас ще раз.", reply_markup=kb_classes())
            await state.set_state(Reg.choose_class)
            return

        data = await state.get_data()
        tg_id = data["tg_id"]
        phone_norm = data["phone"]
        fio = data["fio"]
        class_name = data["class_name"]

        db_upsert_user(phone_norm, fio, class_name, role="учень")
        db_bind_tg_to_phone(tg_id, phone_norm)

        if not db_is_welcomed(phone_norm):
            await message.answer("Вітаю з реєстрацією. Гарного користування!")
            db_set_welcomed(phone_norm)

        await state.clear()
        await show_main_menu(message)
        return

    # --------------------
    # РОЗКЛАД
    # --------------------
    if current_state == Form.waiting_for_class.state:
        if message.text in ("10-А", "10-Б", "11-А", "11-Б"):
            await state.update_data(class_name=message.text)
            await message.answer("На який день потрібен розклад?", reply_markup=kb_days())
            await state.set_state(Form.waiting_for_day)
            return

        if message.text == "Назад":
            await state.clear()
            await show_main_menu(message)
            return

        await message.answer("Будь ласка, оберіть клас: 10-А, 11-А, 10-Б або 11-Б.")
        return

    if current_state == Form.waiting_for_day.state:
        if message.text == "Назад":
            await state.set_state(Form.waiting_for_class)
            await message.answer("Оберіть клас:", reply_markup=kb_schedule_classes())
            return

        if message.text in ("Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця"):
            data = await state.get_data()
            class_name = data.get("class_name")
            day = message.text

            # Отримуємо розклад з БД
            cur = DB.execute(
                "SELECT * FROM schedule WHERE class_name = ? AND day_name = ? ORDER BY lesson_number",
                (class_name, day),
            )
            lessons = [dict(row) for row in cur.fetchall()]
            
            if not lessons:
                await message.answer("Розклад на цей день поки що не додано.")
            else:
                # Форматуємо красиво
                text_lines = [f"**Розклад для класу {class_name} на {day}:**\n"]
                for lesson in lessons:
                    text_lines.append(
                        f"{lesson['lesson_number']}. {lesson['subject']}\n"
                        f"   {lesson['teacher']}  {lesson['start_time']}-{lesson['end_time']}\n"
                    )
                text = "\n".join(text_lines)
                await message.answer(text, parse_mode="Markdown")

            await state.clear()
            await show_main_menu(message)
            return

        await message.answer("Будь ласка, оберіть день: Понеділок, Вівторок, Середа, Четвер або П'ятниця.")
        return

    # --------------------
    # СКАСУВАННЯ (Універсальна обробка)
    # --------------------
    if message.text == "Скасувати":
        await state.clear()
        await message.answer("Дія скасована.", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(message)
        return

    # --------------------
    # ГОЛОВНЕ МЕНЮ
    # --------------------
    if message.text == "Предмети":
        await state.set_state(Teachers.waiting_for_subject)
        await message.answer("Оберіть предмет:", reply_markup=kb_subjects())
        return

    if message.text == "Розклад":
        await state.set_state(Form.waiting_for_class)
        await message.answer("Оберіть клас:", reply_markup=kb_schedule_classes())
        return

    if message.text == "Параметри":
        await state.set_state(Settings.main_menu)
        tg_id = message.from_user.id
        user = db_get_user_by_tg(tg_id)
        if user:
            phone_norm = user["phone"]
            notifications_enabled = db_get_events_notifications(phone_norm)
            status = "✅ Включені" if notifications_enabled else "❌ Вимкнені"
            # Спочатку прибираємо основну клавіатуру
            await message.answer("⏳ Завантаження параметрів...", reply_markup=ReplyKeyboardRemove())
            # Потім відправляємо параметри з інлайн кнопками
            await message.answer(
                f"⚙️ Параметри:\n\n"
                f"Уведомлення про події: {status}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🔔 Уведомлення про події", callback_data="toggle_notifications")],
                        [InlineKeyboardButton(text="ℹ️ Про бота", callback_data="about_bot")],
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")]
                    ]
                )
            )
        return

    if message.text == "Події":
        tg_id = message.from_user.id
        user = db_get_user_by_tg(tg_id)
        notifications_enabled = True
        if user:
            phone_norm = user["phone"]
            notifications_enabled = db_get_events_notifications(phone_norm)
        
        # Получаем последние 3 новости
        news_list = await get_latest_news(3)
        
        if not news_list:
            await message.answer("На жаль, новин немає 📭")
            return
        
        # Отправляем каждую новость
        for news in news_list:
            news_text = format_news_post(news)
            news_id = news.get('id', '')
            image_url = news.get('image', '')
            
            # Создаем кнопку "Читати далі"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="📖 Читати далі",
                        url=f"https://bgpk-liceum.site/news/{news_id}"
                    )]
                ]
            )
            
            # Если есть изображение, отправляем его с подписью
            if image_url and image_url.strip():
                try:
                    await message.answer_photo(
                        photo=image_url,
                        caption=news_text,
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logging.warning(f"Не удалось загрузить фото новости: {e}")
                    await message.answer(news_text, parse_mode="HTML", reply_markup=keyboard)
            else:
                # Если нет изображения, просто отправляем текст
                await message.answer(news_text, parse_mode="HTML", reply_markup=keyboard)
            
            # Небольшая пауза между сообщениями
            await asyncio.sleep(0.5)
        
        return

    await message.answer("Я не знаю, що з цим робити 😕")

    if current_state == Settings.main_menu.state:
        if message.text == "Назад":
            await state.clear()
            await show_main_menu(message)
            return
        
        if message.text == "Уведомлення про події":
            tg_id = message.from_user.id
            user = db_get_user_by_tg(tg_id)
            if user:
                phone_norm = user["phone"]
                db_toggle_events_notifications(phone_norm)
                notifications_enabled = db_get_events_notifications(phone_norm)
                status = "✅ Включені" if notifications_enabled else "❌ Вимкнені"
                await message.answer(f"Уведомлення про події тепер {status}")
                await state.clear()
                await show_main_menu(message)
            return

        await message.answer("Оберіть опцію нижче")
        return


@dp.callback_query(lambda query: query.data == "toggle_notifications")
async def toggle_notifications_callback(callback_query: types.CallbackQuery, state: FSMContext):
    """Обробник кнопки включення/виключення уведомлень"""
    tg_id = callback_query.from_user.id
    user = db_get_user_by_tg(tg_id)
    
    if user:
        phone_norm = user["phone"]
        db_toggle_events_notifications(phone_norm)
        notifications_enabled = db_get_events_notifications(phone_norm)
        status = "✅ Включені" if notifications_enabled else "❌ Вимкнені"
        
        await callback_query.answer(f"Уведомлення тепер {status}", show_alert=True)
        await callback_query.message.edit_text(
            f"⚙️ Параметри:\n\n"
            f"Уведомлення про події: {status}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔔 Уведомлення про події", callback_data="toggle_notifications")],
                    [InlineKeyboardButton(text="ℹ️ Про бота", callback_data="about_bot")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")]
                ]
            )
        )


@dp.callback_query(lambda query: query.data == "about_bot")
async def about_bot_callback(callback_query: types.CallbackQuery):
    """Обробник кнопки 'Про бота'"""
    await callback_query.answer()
    await callback_query.message.edit_text(
        "ℹ️ **ПРО БОТА:**\n\n"
        "Цей бот допомагає студентам отримувати:\n"
        "📚 Розклад занять\n"
        "👨‍🏫 Інформацію про вчителів\n"
        "🔔 Уведомлення про найближчі уроки\n"
        "📢 Оголошення від адміністрації\n\n"
        "**Версія:** 1.0\n"
        "**Розробник:** BGPK Bot",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")]
            ]
        )
    )


@dp.callback_query(lambda query: query.data == "settings_back")
async def settings_back_callback(callback_query: types.CallbackQuery, state: FSMContext):
    """Обробник кнопки 'Назад' у параметрах"""
    await callback_query.answer()
    await callback_query.message.delete()
    await state.clear()
    
    # Відправляємо меню з клавіатурою
    await callback_query.bot.send_message(
        callback_query.from_user.id,
        "Оберіть одну з опцій:",
        reply_markup=kb_main()
    )


# =======================
# MAIN
# =======================
async def main():
    global applications_listener, news_listener
    
    db_init()
    
    # Ініціалізуємо слушача заявок з Firebase
    if FIREBASE_AVAILABLE:
        applications_listener = ApplicationsListener(bot)
        loop = asyncio.get_event_loop()
        applications_listener.start_listening(loop)
        logging.info("✅ Слушатель заявок запущен")
        
        # Ініціалізуємо слушача новин з Firebase
        news_listener = NewsListener(bot)
        news_listener.start_listening(loop)
        logging.info("✅ Слушатель новин запущен")
    else:
        logging.warning("Firebase недоступний. Слушатель заявок і новин не запущен")
    
    # Запускаємо фоновий таск для сповіщень
    asyncio.create_task(check_and_notify_upcoming_classes())
    
    try:
        await dp.start_polling(bot)
    finally:
        # Зупиняємо слушачів при завершенні
        if applications_listener:
            applications_listener.stop_listening()
        if news_listener:
            news_listener.stop_listening()


if __name__ == "__main__":
    if not API_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN не встановлено в .env файлі!")
        exit(1)
    asyncio.run(main())
