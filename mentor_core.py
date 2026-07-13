import datetime
import os.path
import json
import re
import html
import random
import base64
import urllib.parse
import requests
import time
import threading
import ctypes
import uuid
import logging
import concurrent.futures
from logging.handlers import RotatingFileHandler
from collections import deque
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

try:
    from playwright_stealth import stealth_sync as apply_stealth
except ImportError:
    try:
        from playwright_stealth import Stealth
        def apply_stealth(page):
            Stealth().use_sync(page)
    except ImportError:
        def apply_stealth(page):
            pass  # stealth недоступен - работаем без маскировки

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar']
# FIX: раньше часовой пояс был захардкожен как фиксированное смещение
# (datetime.timezone(timedelta(hours=5))), что не учитывает переход на летнее
# время в поясах, где оно есть, и не позволяло сменить пояс без правки кода.
# Теперь используется настоящий IANA-пояс через zoneinfo (даёт корректный DST
# там, где он есть) и его можно переопределить переменной окружения MENTOR_TZ,
# например MENTOR_TZ=America/Montevideo при переезде.
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 - на всякий случай, хотя проект рассчитан на 3.9+
    ZoneInfo = None

TIMEZONE_NAME = os.environ.get('MENTOR_TZ', 'Asia/Yekaterinburg')
if ZoneInfo is not None:
    try:
        TZ_OBJECT = ZoneInfo(TIMEZONE_NAME)
    except Exception:
        TZ_OBJECT = datetime.timezone(datetime.timedelta(hours=5))
else:
    TZ_OBJECT = datetime.timezone(datetime.timedelta(hours=5))
MODEL_NAME = 'qwen2.5-coder:14b'
# Текущая активная модель (может меняться из веб-интерфейса через set_current_model,
# поэтому MODEL_NAME оставлен как значение по умолчанию/фолбэк, а реально используется
# _current_model). Лок нужен, т.к. Flask dev server может обрабатывать запросы параллельно.
_current_model = MODEL_NAME
_model_lock = threading.Lock()
WORKSPACE_DIR = os.path.abspath("./workspace")
DB_FILE = "chat_history.json"
TASKS_FILE = "tasks.json"
PROGRESS_FILE = "progress.json"
GOALS_FILE = "goals.json"
OBSIDIAN_CONFIG_FILE = "obsidian_config.json"
REPORTS_STATE_FILE = "weekly_reports_state.json"
REPORTS_DIR = os.path.abspath("./reports")
MEMORY_NOTES_FILE = "memory_notes.json"
DIGEST_STATE_FILE = "digest_state.json"
# Раз в сколько новых сообщений (пар user+assistant считаются как есть, по счётчику
# записей chat_history) запускать фоновый дайджест чата в structured-факты.
DIGEST_MIN_NEW_MESSAGES = 12
# Английский и испанский - обязательные треки для миграционной цели, Cyber Security -
# профессиональный трек (удалённая работа). Python пригождается как прикладной навык
# в кибербезопасности (скрипты, автоматизация, пентест-тулинг). Golang/AI оставлены
# опционально - можно убрать из списка, если пользователь не планирует их развивать.
LEARNING_TOPICS = ["English", "Spanish", "Cyber Security", "Python", "Golang", "AI"]
MIGRATION_TARGET_DATE = "2028-09-01"
MIGRATION_TARGET_COUNTRIES = ["Уругвай", "Чили"]
# FIX: 14B-модель, особенно без GPU или при первом ("холодном") запуске,
# может отвечать заметно дольше 2 минут. 120с было слишком мало.
OLLAMA_TIMEOUT = 600  # секунд

if not os.path.exists(WORKSPACE_DIR):
    os.makedirs(WORKSPACE_DIR)
if not os.path.exists(REPORTS_DIR):
    os.makedirs(REPORTS_DIR)

# ---------------------------------------------------------------------------
# Логирование в ротируемый файл (mentor.log, до 5 файлов по 2 МБ), в дополнение
# к выводу в консоль. Раньше фоновые службы (напоминания, отчёты, дайджест)
# писали только через print() - при работе как фоновый процесс (не в терминале)
# эти сообщения было негде посмотреть постфактум.
# ---------------------------------------------------------------------------
LOG_FILE = "mentor.log"
logger = logging.getLogger("mentor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_file_handler)
    logger.addHandler(_console_handler)

# Защита от гонки потоков при работе с token.json, tasks.json, progress.json и goals.json
_calendar_lock = threading.Lock()
_tasks_lock = threading.Lock()
_progress_lock = threading.Lock()
_goals_lock = threading.Lock()
_reports_lock = threading.Lock()
_memory_notes_lock = threading.Lock()
_digest_state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Очередь уведомлений для веб-интерфейса. send_windows_notification раньше либо
# показывал системный MessageBox (только Windows), либо тихо печатал в консоль
# на других ОС - пользователь веб-версии никогда их не видел. Теперь каждое
# уведомление также кладётся сюда, а /api/notifications в app.py отдаёт и
# вычищает накопившиеся - фронтенд показывает их через Web Notifications API.
# ---------------------------------------------------------------------------
_notifications_lock = threading.Lock()
_notifications = deque(maxlen=100)


def push_notification(title, message):
    with _notifications_lock:
        _notifications.append({
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "message": message,
            "date": datetime.datetime.now(TZ_OBJECT).isoformat(),
        })


def pop_notifications():
    """Возвращает и очищает накопившиеся уведомления (для опроса из веб-интерфейса)."""
    with _notifications_lock:
        items = list(_notifications)
        _notifications.clear()
        return items


# Действия, которые необратимо меняют данные пользователя (календарь, задачи) -
# для них веб-интерфейс запрашивает явное подтверждение, прежде чем выполнять,
# вместо того чтобы полагаться исключительно на решение модели.
DESTRUCTIVE_ACTIONS = {"delete_event", "delete_task"}

ACTION_LABELS = {
    "create_event": "создаю событие в календаре",
    "delete_event": "удаляю событие из календаря",
    "search_web": "ищу в интернете",
    "visit_url": "открываю сайт",
    "list_files": "смотрю список файлов",
    "read_file": "читаю файл",
    "write_file": "сохраняю файл",
    "list_events": "смотрю календарь",
    "create_task": "создаю задачу",
    "list_tasks": "смотрю список задач",
    "complete_task": "отмечаю задачу выполненной",
    "delete_task": "удаляю задачу",
    "update_progress": "обновляю учебный прогресс",
    "get_progress": "смотрю учебный прогресс",
    "record_quiz_result": "записываю результат проверки знаний",
    "set_goal_milestone": "обновляю веху цели",
    "get_goal_status": "смотрю статус цели",
    "generate_weekly_report": "формирую еженедельный отчёт",
    "get_weekly_report": "смотрю еженедельный отчёт",
    "save_note": "сохраняю заметку",
    "get_roadmap": "смотрю roadmap по целям",
    "update_checkpoint": "обновляю промежуточную цель",
    "set_obsidian_vault": "подключаю Obsidian vault",
    "list_obsidian_notes": "смотрю список заметок Obsidian",
    "read_obsidian_note": "читаю заметку Obsidian",
    "write_obsidian_note": "сохраняю заметку в Obsidian",
    "append_obsidian_note": "дописываю заметку в Obsidian",
    "search_obsidian_notes": "ищу по заметкам Obsidian",
    "create_daily_note": "создаю ежедневную заметку Obsidian",
}


def send_windows_notification(title, message):
    """
    Отправляет всплывающее системное уведомление.
    FIX: раньше код безусловно обращался к ctypes.windll, которого не существует
    на Linux/Mac (AttributeError). Поток глушит исключения молча, поэтому баг был
    незаметен, но напоминания на не-Windows системах просто никогда не показывались.
    Теперь платформа проверяется явно, а на других ОС уведомление хотя бы попадает в лог.
    Плюс: уведомление всегда кладётся в очередь push_notification, чтобы веб-интерфейс
    мог показать его через Web Notifications API, независимо от ОС и того, запущен ли
    процесс в видимом терминале.
    """
    push_notification(title, message)
    logger.info(f"[Уведомление] {title}: {message}")

    def _show():
        try:
            if os.name == "nt":
                ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x00)
        except Exception as e:
            logger.error(f"[Ошибка системного уведомления]: {e}")

    t = threading.Thread(target=_show, daemon=True)  # FIX: не блокирует завершение процесса
    t.start()


def calendar_reminder_worker():
    """Фоновая функция для проверки напоминаний. Запускается в отдельном потоке."""
    notified_events = set()
    logger.info("Фоновая служба напоминаний успешно запущена.")
    while True:
        try:
            service = get_calendar_service()
            now = datetime.datetime.now(TZ_OBJECT)
            time_max = (now + datetime.timedelta(hours=2)).isoformat()
            time_min = now.isoformat()

            events_result = service.events().list(
                calendarId='primary', timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])
            for event in events:
                start_raw = event['start'].get('dateTime', event['start'].get('date'))
                if not start_raw or 'T' not in start_raw:
                    continue

                event_time = datetime.datetime.fromisoformat(start_raw).astimezone(TZ_OBJECT)
                time_diff = event_time - now
                minutes_left = time_diff.total_seconds() / 60
                event_key = f"{event['summary']}_{start_raw}"

                if 29 <= minutes_left <= 31 and event_key not in notified_events:
                    send_windows_notification(
                        title="🤖 ИИ-Ментор: Напоминание",
                        message=f"Через 30 минут начнется событие:\n«{event['summary']}»"
                    )
                    notified_events.add(event_key)
        except Exception as e:
            logger.error(f"Ошибка фонового напоминания: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Модуль задач/напоминаний (аналог Mira: лёгкие todo, не привязанные к календарю)
# ---------------------------------------------------------------------------

def _load_tasks():
    with _tasks_lock:
        if not os.path.exists(TASKS_FILE):
            return []
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []


def _save_tasks(tasks):
    with _tasks_lock:
        try:
            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def create_task(title, due=None, priority="normal"):
    """Создаёт задачу/напоминание. due - ISO-строка ГГГГ-ММ-ДДTЧЧ:ММ:СС или None."""
    try:
        tasks = _load_tasks()
        task = {
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "due": due,
            "priority": priority if priority in ("low", "normal", "high") else "normal",
            "status": "pending",
            "created_at": datetime.datetime.now(TZ_OBJECT).isoformat(),
        }
        tasks.append(task)
        _save_tasks(tasks)
        due_text = f" (срок: {due})" if due else " (без срока)"
        return f"Задача создана: «{title}»{due_text}, приоритет: {task['priority']}."
    except Exception as e:
        return f"Ошибка при создании задачи: {e}"


def list_tasks(include_done=False):
    tasks = _load_tasks()
    if not tasks:
        return "Список задач пуст."
    active = [t for t in tasks if include_done or t.get("status") != "done"]
    if not active:
        return "Активных задач нет."

    output = "Список задач:\n"
    for t in active:
        due_text = f", срок: {t['due']}" if t.get("due") else ""
        status_mark = "✅" if t.get("status") == "done" else "◻️"
        output += f"- {status_mark} [{t['id']}] {t['title']} (приоритет: {t.get('priority', 'normal')}{due_text})\n"
    return output


def complete_task(identifier):
    """Помечает задачу выполненной по id или по подстроке в названии."""
    try:
        tasks = _load_tasks()
        matched = [t for t in tasks
                   if t["id"] == identifier or identifier.lower() in t["title"].lower()]
        if not matched:
            return f"Задача '{identifier}' не найдена."
        now_iso = datetime.datetime.now(TZ_OBJECT).isoformat()
        for t in matched:
            t["status"] = "done"
            t["completed_at"] = now_iso  # используется еженедельным отчётом
        _save_tasks(tasks)
        return f"Отмечено выполненными: {len(matched)} задач(и)."
    except Exception as e:
        return f"Ошибка при завершении задачи: {e}"


def delete_task(identifier):
    try:
        tasks = _load_tasks()
        remaining = [t for t in tasks
                     if not (t["id"] == identifier or identifier.lower() in t["title"].lower())]
        deleted_count = len(tasks) - len(remaining)
        _save_tasks(remaining)
        if deleted_count > 0:
            return f"Удалено задач: {deleted_count}."
        return f"Задача '{identifier}' не найдена."
    except Exception as e:
        return f"Ошибка при удалении задачи: {e}"


def task_reminder_worker():
    """
    Фоновая проверка задач: напоминает о просроченных и о задачах,
    срок которых наступает в ближайшие 30 минут. Аналог calendar_reminder_worker,
    но для лёгких задач, не привязанных к календарным событиям.
    """
    notified = set()
    logger.info("Фоновая служба напоминаний по задачам успешно запущена.")
    while True:
        try:
            now = datetime.datetime.now(TZ_OBJECT)
            tasks = _load_tasks()
            for t in tasks:
                if t.get("status") == "done" or not t.get("due"):
                    continue
                try:
                    due_dt = datetime.datetime.fromisoformat(t["due"])
                    if due_dt.tzinfo is None:
                        due_dt = due_dt.replace(tzinfo=TZ_OBJECT)
                except Exception:
                    continue

                minutes_left = (due_dt - now).total_seconds() / 60
                key = t["id"]

                if 29 <= minutes_left <= 31 and key not in notified:
                    send_windows_notification(
                        title="🤖 ИИ-Ментор: Задача",
                        message=f"Через 30 минут срок задачи:\n«{t['title']}»"
                    )
                    notified.add(key)
                elif minutes_left < 0 and f"{key}_overdue" not in notified:
                    send_windows_notification(
                        title="🤖 ИИ-Ментор: Просрочено",
                        message=f"Задача просрочена:\n«{t['title']}»"
                    )
                    notified.add(f"{key}_overdue")
        except Exception as e:
            logger.error(f"Ошибка фонового напоминания по задачам: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Модуль учебного прогресса (усиление менторской роли: агент помнит,
# что уже пройдено по каждой теме, и может опираться на это в подсказках)
# ---------------------------------------------------------------------------

MAX_HISTORY_ENTRIES = 200  # ограничение, чтобы progress.json не рос бесконечно


def _default_topic_entry():
    return {
        "level": 0,            # 0-5: крупная веха владения темой (как раньше)
        "points": 0,            # 0-100: усиленная, более гранулярная шкала владения ВНУТРИ уровня
        "status": "not_started",
        "notes": "",
        "updated_at": None,
        "history": [],          # [{date, level, points, status, notes}] - для еженедельного отчёта
        "quiz_log": [],         # [{date, correct, total, score, notes}] - результаты проверок знаний
    }


def _default_progress():
    return {topic: _default_topic_entry() for topic in LEARNING_TOPICS}


def _merge_topic_entry(saved):
    """
    FIX: раньше новые темы/поля добавлялись через base.update(data), что ПОЛНОСТЬЮ
    заменяло словарь темы старыми данными - любое новое поле (points/history/quiz_log),
    отсутствовавшее в старом progress.json, тихо терялось при каждой загрузке.
    Здесь вместо этого - глубокое слияние на уровне полей внутри темы.
    """
    entry = _default_topic_entry()
    if isinstance(saved, dict):
        entry.update(saved)
        # старые файлы могли не содержать level/points/history/quiz_log вовсе
        entry.setdefault("history", [])
        entry.setdefault("quiz_log", [])
        entry.setdefault("points", 0)
    return entry


def _load_progress():
    with _progress_lock:
        if not os.path.exists(PROGRESS_FILE):
            return _default_progress()
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = _default_progress()
            for topic, saved_entry in data.items():
                base[topic] = _merge_topic_entry(saved_entry)
            return base
        except Exception:
            return _default_progress()


def _save_progress(progress):
    with _progress_lock:
        try:
            with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(progress, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _find_topic_key(progress, topic):
    """Сначала точное совпадение (без учёта регистра), и только потом - нестрогое
    вхождение подстроки. FIX: раньше подстрока проверялась раньше точного совпадения
    в порядке вставки словаря, из-за чего короткие названия тем (например 'AI')
    могли случайно "перехватить" обновление, предназначенное другой теме."""
    topic_l = str(topic).lower().strip()
    for key in progress:
        if key.lower() == topic_l:
            return key
    for key in progress:
        if key.lower() in topic_l or topic_l in key.lower():
            return key
    return None


def update_progress(topic, level=None, status=None, notes=None, points=None):
    """
    Обновляет прогресс по теме.
    - level: 0-5 (крупная веха, как раньше - используется в целях/милстоунах)
    - points: 0-100 (усиленная, более точная шкала владения внутри уровня - например,
      "выучил 40% лексики уровня B2" - удобно для мелких, но реальных сдвигов прогресса,
      которые не дотягивают до целого уровня)
    - status: not_started/in_progress/done
    - notes: краткая заметка о том, что именно пройдено/изучено
    Тема сопоставляется нестрого, чтобы не требовать от модели точного совпадения.
    Каждое обновление добавляет запись в историю темы - на основе неё строится
    еженедельный отчёт о прогрессе.
    """
    try:
        progress = _load_progress()
        matched_key = _find_topic_key(progress, topic)
        if matched_key is None:
            matched_key = str(topic)
            progress[matched_key] = _default_topic_entry()

        entry = progress[matched_key]
        if level is not None:
            try:
                entry["level"] = max(0, min(5, int(level)))
            except (TypeError, ValueError):
                pass
        if points is not None:
            try:
                entry["points"] = max(0, min(100, int(points)))
            except (TypeError, ValueError):
                pass
        if status in ("not_started", "in_progress", "done"):
            entry["status"] = status
        if notes:
            entry["notes"] = notes
        entry["updated_at"] = datetime.datetime.now(TZ_OBJECT).isoformat()

        entry.setdefault("history", []).append({
            "date": entry["updated_at"],
            "level": entry["level"],
            "points": entry.get("points", 0),
            "status": entry["status"],
            "notes": notes or "",
        })
        entry["history"] = entry["history"][-MAX_HISTORY_ENTRIES:]

        _save_progress(progress)
        return (f"Прогресс по теме '{matched_key}' обновлён: уровень {entry['level']}/5 "
                f"({entry.get('points', 0)}/100 в рамках уровня), статус: {entry['status']}.")
    except Exception as e:
        return f"Ошибка при обновлении прогресса: {e}"


def record_quiz_result(topic, correct, total, notes=None):
    """
    Записывает результат проверки знаний (мини-теста/квиза) по теме.
    correct/total - число правильных ответов из общего числа вопросов.
    Автоматически подтягивает points (скользящее среднее с последним значением,
    чтобы один неудачный квиз не обнулял весь прогресс, но и не давал переть вверх
    от одного удачного попадания), и добавляет запись в quiz_log темы - используется
    в еженедельном отчёте, чтобы показать, что реально было проверено.
    """
    try:
        total = max(1, int(total))
        correct = max(0, min(total, int(correct)))
        score = round(correct / total * 100)

        progress = _load_progress()
        matched_key = _find_topic_key(progress, topic)
        if matched_key is None:
            matched_key = str(topic)
            progress[matched_key] = _default_topic_entry()
        entry = progress[matched_key]

        old_points = entry.get("points", 0)
        # Скользящее среднее: 60% вес новому результату, 40% - старому значению
        new_points = round(old_points * 0.4 + score * 0.6)
        entry["points"] = max(0, min(100, new_points))
        entry["updated_at"] = datetime.datetime.now(TZ_OBJECT).isoformat()
        if entry.get("status") == "not_started":
            entry["status"] = "in_progress"

        quiz_entry = {
            "date": entry["updated_at"],
            "correct": correct,
            "total": total,
            "score": score,
            "notes": notes or "",
        }
        entry.setdefault("quiz_log", []).append(quiz_entry)
        entry["quiz_log"] = entry["quiz_log"][-MAX_HISTORY_ENTRIES:]

        entry.setdefault("history", []).append({
            "date": entry["updated_at"],
            "level": entry["level"],
            "points": entry["points"],
            "status": entry["status"],
            "notes": f"Квиз: {correct}/{total} ({score}%)" + (f" — {notes}" if notes else ""),
        })
        entry["history"] = entry["history"][-MAX_HISTORY_ENTRIES:]

        _save_progress(progress)
        verdict = "отлично" if score >= 80 else ("неплохо, но есть пробелы" if score >= 50 else "тема пока слабая, нужно повторить")
        return (f"Результат проверки по теме '{matched_key}' записан: {correct}/{total} ({score}%) - {verdict}. "
                f"Текущий прогресс внутри уровня: {entry['points']}/100.")
    except Exception as e:
        return f"Ошибка при записи результата проверки: {e}"


def get_progress_data():
    """Структурированный прогресс по темам (для веб-интерфейса)."""
    return _load_progress()


def get_progress():
    progress = _load_progress()
    output = "Учебный прогресс (уровень 0-5 - крупная веха; шкала в скобках 0-100 - точный прогресс внутри уровня):\n"
    for topic, entry in progress.items():
        bar = "●" * entry.get("level", 0) + "○" * (5 - entry.get("level", 0))
        points = entry.get("points", 0)
        notes_text = f" — {entry['notes']}" if entry.get("notes") else ""
        last_quiz = ""
        quiz_log = entry.get("quiz_log", [])
        if quiz_log:
            lq = quiz_log[-1]
            last_quiz = f", последняя проверка знаний: {lq['correct']}/{lq['total']} ({lq['score']}%)"
        output += f"- {topic}: {bar} [{points}/100] ({entry.get('status', 'not_started')}){notes_text}{last_quiz}\n"
    return output


# ---------------------------------------------------------------------------
# Модуль главной цели: переезд в Уругвай/Чили к сентябрю 2028.
# Хранит дедлайн и вехи (целевой уровень + дата) по английскому, испанскому
# и кибербезопасности. Используется, чтобы агент всегда держал в фокусе
# конечную цель, а не просто абстрактный "учебный прогресс".
# ---------------------------------------------------------------------------

CHECKPOINT_HORIZONS = [
    ("week", 7),
    ("month", 30),
    ("quarter", 91),
    ("half_year", 182),
    ("year", 365),
]
CHECKPOINT_LABELS_RU = {
    "week": "неделя",
    "month": "месяц",
    "quarter": "квартал",
    "half_year": "полгода",
    "year": "год",
}


def _generate_checkpoint_drafts(target_level, target_date_str, current_level):
    """
    Авто-черновик промежуточных целей (roadmap) от сегодня до target_date,
    по горизонтам неделя/месяц/квартал/полгода/год - только те горизонты,
    что укладываются до дедлайна. Уровень на каждом горизонте - линейная
    интерполяция между текущим уровнем (0-5, из progress.json) и максимумом
    (5), пропорционально доле пройденного времени. Это ЧЕРНОВИК: каждый
    чекпоинт помечен auto=True и может быть вручную поправлен через
    update_checkpoint - тогда он перестаёт затираться при пересчёте.
    """
    try:
        target_dt = datetime.date.fromisoformat(target_date_str)
    except Exception:
        return {}
    today = datetime.datetime.now(TZ_OBJECT).date()
    total_days = (target_dt - today).days
    if total_days <= 0:
        return {}
    checkpoints = {}
    for key, days in CHECKPOINT_HORIZONS:
        if days >= total_days:
            continue
        fraction = days / total_days
        level_target = round(min(5, current_level + fraction * (5 - current_level)), 1)
        cp_date = today + datetime.timedelta(days=days)
        checkpoints[key] = {
            "target_date": cp_date.isoformat(),
            "level_target": level_target,
            "description": f"~{level_target}/5 на пути к «{target_level}» (через {CHECKPOINT_LABELS_RU[key]})",
            "done": False,
            "notes": "",
            "auto": True,
        }
    return checkpoints


def _regenerate_checkpoints_for_topic(topic):
    """Пересобирает авто-черновик чекпоинтов треку после изменения target_level/
    target_date через set_goal_milestone. Чекпоинты, вручную поправленные через
    update_checkpoint (auto=False), не затираются - только те, что остались
    автоматическими."""
    goals = _load_goals()
    milestones = goals.setdefault("milestones", {})
    entry = milestones.get(topic)
    if not entry:
        return
    progress = _load_progress()
    current_level = progress.get(topic, {}).get("level", 0)
    drafts = _generate_checkpoint_drafts(entry.get("target_level", "?"), entry.get("target_date", ""), current_level)
    existing = entry.get("checkpoints", {})
    merged = dict(existing)
    for key, draft in drafts.items():
        old = existing.get(key)
        if old and not old.get("auto", True):
            continue  # ручная правка - не затираем
        merged[key] = draft
    # убираем горизонты, которые больше не укладываются до нового дедлайна
    # (но только автоматические - ручные оставляем, даже если формально "не влезают")
    for key in list(merged.keys()):
        if key not in drafts and merged[key].get("auto", True):
            del merged[key]
    entry["checkpoints"] = merged
    milestones[topic] = entry
    goals["milestones"] = milestones
    _save_goals(goals)


def _ensure_all_checkpoints():
    """Гарантирует, что у каждой вехи есть черновик чекпоинтов (нужно один раз
    для старых goals.json, созданных до появления roadmap)."""
    goals = _load_goals()
    milestones = goals.get("milestones", {})
    progress = _load_progress()
    changed = False
    for topic, entry in milestones.items():
        if not entry.get("checkpoints"):
            current_level = progress.get(topic, {}).get("level", 0)
            drafts = _generate_checkpoint_drafts(entry.get("target_level", "?"), entry.get("target_date", ""), current_level)
            if drafts:
                entry["checkpoints"] = drafts
                changed = True
    if changed:
        goals["milestones"] = milestones
        _save_goals(goals)


def update_checkpoint(topic, horizon, target_date=None, level_target=None, description=None, done=None, notes=None):
    """
    Инструмент: вручную поправить промежуточную цель (чекпоинт) конкретного
    горизонта (week/month/quarter/half_year/year) для трека - переопределяет
    авто-черновик и больше не затирается при пересчёте после set_goal_milestone.
    """
    valid_horizons = [k for k, _ in CHECKPOINT_HORIZONS]
    if horizon not in valid_horizons:
        return f"Некорректный горизонт '{horizon}'. Допустимые: {', '.join(valid_horizons)}."
    goals = _load_goals()
    milestones = goals.setdefault("milestones", {})
    entry = milestones.get(topic)
    if not entry:
        return f"Трек '{topic}' не найден среди целей (сначала задай его через set_goal_milestone)."
    checkpoints = entry.setdefault("checkpoints", {})
    cp = checkpoints.get(horizon, {"done": False, "notes": "", "auto": True})
    if target_date:
        cp["target_date"] = target_date
    if level_target is not None:
        cp["level_target"] = level_target
    if description:
        cp["description"] = description
    if done is not None:
        cp["done"] = bool(done)
    if notes:
        cp["notes"] = notes
    cp["auto"] = False
    checkpoints[horizon] = cp
    entry["checkpoints"] = checkpoints
    milestones[topic] = entry
    goals["milestones"] = milestones
    _save_goals(goals)
    return f"Промежуточная цель ({CHECKPOINT_LABELS_RU.get(horizon, horizon)}) по '{topic}' обновлена."


def get_roadmap():
    """Текстовая сводка roadmap'а (промежуточные цели по всем трекам) -
    вшивается в системный промпт, чтобы модель держала в фокусе не только
    финальный дедлайн трека, но и ближайшую промежуточную веху."""
    _ensure_all_checkpoints()
    goals = _load_goals()
    progress = _load_progress()
    today = datetime.datetime.now(TZ_OBJECT).date()
    lines = ["ROADMAP (промежуточные цели: неделя/месяц/квартал/полгода/год):"]
    for topic, entry in goals.get("milestones", {}).items():
        checkpoints = entry.get("checkpoints", {})
        if not checkpoints:
            continue
        current_level = progress.get(topic, {}).get("level", 0)
        lines.append(f"- {topic} (текущий уровень {current_level}/5):")
        for key, _ in CHECKPOINT_HORIZONS:
            cp = checkpoints.get(key)
            if not cp:
                continue
            mark = "✅" if cp.get("done") else "▫"
            overdue = ""
            try:
                cp_date = datetime.date.fromisoformat(cp.get("target_date", ""))
                if (cp_date - today).days < 0 and not cp.get("done"):
                    overdue = " ⚠️ просрочено"
            except Exception:
                pass
            lines.append(f"    {mark} {CHECKPOINT_LABELS_RU[key]} (до {cp.get('target_date', '?')}): "
                         f"{cp.get('description', '')}{overdue}")
    return "\n".join(lines)


def get_roadmap_data():
    """Структурированные данные roadmap'а для веб-интерфейса (сайдбар)."""
    _ensure_all_checkpoints()
    goals = _load_goals()
    progress = _load_progress()
    today = datetime.datetime.now(TZ_OBJECT).date()
    out = []
    for topic, entry in goals.get("milestones", {}).items():
        checkpoints_out = []
        for key, _ in CHECKPOINT_HORIZONS:
            cp = entry.get("checkpoints", {}).get(key)
            if not cp:
                continue
            days_left = None
            try:
                days_left = (datetime.date.fromisoformat(cp.get("target_date", "")) - today).days
            except Exception:
                pass
            checkpoints_out.append({
                "horizon": key,
                "label": CHECKPOINT_LABELS_RU[key],
                "target_date": cp.get("target_date"),
                "level_target": cp.get("level_target"),
                "description": cp.get("description", ""),
                "done": cp.get("done", False),
                "days_left": days_left,
            })
        out.append({
            "topic": topic,
            "current_level": progress.get(topic, {}).get("level", 0),
            "checkpoints": checkpoints_out,
        })
    return out


def _default_goals():
    return {
        "migration_target_date": MIGRATION_TARGET_DATE,
        "target_countries": MIGRATION_TARGET_COUNTRIES,
        "milestones": {
            "English": {
                "target_level": "C1",
                "target_date": "2028-02-01",
                "notes": "Свободное деловое общение, собеседования на английском"
            },
            "Spanish": {
                "target_level": "B2-C1",
                "target_date": "2028-05-01",
                "notes": "Бытовое и рабочее общение, документы, аренда жилья"
            },
            "Cyber Security": {
                "target_level": "job-ready",
                "target_date": "2027-12-01",
                "notes": "Портфолио, сертификации, оффер на удалённую позицию"
            },
        }
    }


def _load_goals():
    with _goals_lock:
        if not os.path.exists(GOALS_FILE):
            return _default_goals()
        try:
            with open(GOALS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = _default_goals()
            base.update({k: v for k, v in data.items() if k != "milestones"})
            merged_milestones = base["milestones"]
            merged_milestones.update(data.get("milestones", {}))
            base["milestones"] = merged_milestones
            return base
        except Exception:
            return _default_goals()


def _save_goals(goals):
    with _goals_lock:
        try:
            with open(GOALS_FILE, "w", encoding="utf-8") as f:
                json.dump(goals, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def set_goal_milestone(topic, target_level=None, target_date=None, notes=None):
    """Позволяет агенту (или пользователю через диалог) скорректировать веху:
    целевой уровень, дедлайн или заметку по конкретному треку (English/Spanish/Cyber Security)."""
    try:
        goals = _load_goals()
        milestones = goals.setdefault("milestones", {})
        entry = milestones.get(topic, {})
        if target_level:
            entry["target_level"] = target_level
        if target_date:
            entry["target_date"] = target_date
        if notes:
            entry["notes"] = notes
        milestones[topic] = entry
        goals["milestones"] = milestones
        _save_goals(goals)
        _regenerate_checkpoints_for_topic(topic)
        return (f"Веха по '{topic}' обновлена: цель «{entry.get('target_level', '?')}» к {entry.get('target_date', '?')}. "
                f"Промежуточные цели (неделя/месяц/квартал/полгода/год) пересчитаны автоматически - "
                f"можно поправить конкретный горизонт через update_checkpoint.")
    except Exception as e:
        return f"Ошибка при обновлении цели: {e}"


def get_goal_data():
    """Структурированные данные о главной цели (для веб-интерфейса)."""
    goals = _load_goals()
    days_left = weeks_left = months_left = None
    try:
        target_dt = datetime.date.fromisoformat(goals["migration_target_date"])
        today = datetime.datetime.now(TZ_OBJECT).date()
        days_left = (target_dt - today).days
        weeks_left = days_left // 7
        months_left = round(days_left / 30.44, 1)
    except Exception:
        pass

    progress = _load_progress()
    milestones_out = []
    for topic, ms in goals.get("milestones", {}).items():
        current_level = progress.get(topic, {}).get("level", 0)
        m_days_left = None
        try:
            m_dt = datetime.date.fromisoformat(ms.get("target_date", ""))
            m_days_left = (m_dt - datetime.datetime.now(TZ_OBJECT).date()).days
        except Exception:
            pass
        milestones_out.append({
            "topic": topic,
            "target_level": ms.get("target_level", ""),
            "target_date": ms.get("target_date"),
            "notes": ms.get("notes", ""),
            "current_level": current_level,
            "days_left": m_days_left,
        })

    return {
        "migration_target_date": goals.get("migration_target_date", MIGRATION_TARGET_DATE),
        "target_countries": goals.get("target_countries", MIGRATION_TARGET_COUNTRIES),
        "days_left": days_left,
        "weeks_left": weeks_left,
        "months_left": months_left,
        "milestones": milestones_out,
    }


def get_goal_status():
    """Текстовая сводка по главной цели - вшивается в системный промпт,
    чтобы модель на каждом ходу помнила про дедлайн и его приближение/срыв."""
    data = get_goal_data()
    countries = " или ".join(data["target_countries"])
    if data["days_left"] is None:
        return "ГЛАВНАЯ ЦЕЛЬ: дата переезда не задана корректно.\n"

    urgency = ""
    if data["days_left"] < 0:
        urgency = " ⚠️ ДЕДЛАЙН ПРОСРОЧЕН."
    elif data["days_left"] < 180:
        urgency = " ⚠️ Осталось меньше полугода - темп критичен."

    output = (f"ГЛАВНАЯ ЦЕЛЬ ПОЛЬЗОВАТЕЛЯ: переезд в {countries} к {data['migration_target_date']}. "
              f"Осталось {data['days_left']} дн. (~{data['months_left']} мес., ~{data['weeks_left']} нед.).{urgency}\n"
              f"Треки для достижения цели:\n")
    for m in data["milestones"]:
        m_urgency = ""
        if m["days_left"] is not None and m["days_left"] < 60 and m["current_level"] < 4:
            m_urgency = " ⚠️ срок близко, а уровень ещё низкий"
        output += (f"- {m['topic']}: цель «{m['target_level']}» к {m['target_date']} "
                   f"(текущий уровень: {m['current_level']}/5){m_urgency}\n")
    return output


def get_tasks_data(include_done=False):
    """Структурированный список задач (для веб-интерфейса)."""
    tasks = _load_tasks()
    return [t for t in tasks if include_done or t.get("status") != "done"]


# ---------------------------------------------------------------------------
# Модуль еженедельного отчёта: раз в неделю (или по запросу) агент сам
# формирует сводку о том, что пользователь прошёл и изучил - на основе истории
# прогресса (progress.json/history), результатов проверок знаний (quiz_log)
# и выполненных задач (tasks.json). Отчёт сохраняется в папку reports/,
# чтобы к нему можно было вернуться, и отдаётся в веб-интерфейс через /api/weekly_report.
# ---------------------------------------------------------------------------

def _load_reports_state():
    with _reports_lock:
        if not os.path.exists(REPORTS_STATE_FILE):
            return {"last_generated_week": None, "last_report_file": None}
        try:
            with open(REPORTS_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"last_generated_week": None, "last_report_file": None}


def _save_reports_state(state):
    with _reports_lock:
        try:
            with open(REPORTS_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _iso_week_key(dt):
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _safe_parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_OBJECT)
        return dt
    except Exception:
        return None


def generate_weekly_report(period_days=7):
    """
    Формирует отчёт о том, что пользователь прошёл/изучил за последние period_days дней:
    - прирост points/level по каждой теме (сравнение первой и последней записи истории в периоде),
    - результаты проверок знаний (quiz_log) за период,
    - выполненные задачи за период,
    - краткая сверка с графиком по главной цели.
    Сохраняет отчёт в reports/ и обновляет состояние (чтобы фоновый воркер не дублировал
    автогенерацию на этой неделе). Возвращает текст отчёта.
    """
    try:
        now = datetime.datetime.now(TZ_OBJECT)
        period_start = now - datetime.timedelta(days=period_days)

        progress = _load_progress()
        lines = [f"# Еженедельный отчёт по обучению\n",
                 f"Период: {period_start.strftime('%Y-%m-%d')} — {now.strftime('%Y-%m-%d')}\n"]

        any_activity = False

        lines.append("## Прогресс по темам\n")
        for topic, entry in progress.items():
            history = [h for h in entry.get("history", [])
                       if _safe_parse_iso(h.get("date")) and _safe_parse_iso(h.get("date")) >= period_start]
            quiz_log = [q for q in entry.get("quiz_log", [])
                       if _safe_parse_iso(q.get("date")) and _safe_parse_iso(q.get("date")) >= period_start]

            if not history and not quiz_log:
                lines.append(f"- **{topic}**: за неделю активности не было (текущий статус: {entry.get('status', 'not_started')}, "
                              f"уровень {entry.get('level', 0)}/5).\n")
                continue

            any_activity = True
            points_start = history[0].get("points", entry.get("points", 0)) if history else entry.get("points", 0)
            points_now = entry.get("points", 0)
            level_now = entry.get("level", 0)
            delta = points_now - points_start
            delta_text = f"+{delta}" if delta > 0 else str(delta)

            notes_texts = [h["notes"] for h in history if h.get("notes")]
            summary_notes = "; ".join(notes_texts[-3:]) if notes_texts else "без заметок"

            quiz_text = ""
            if quiz_log:
                avg_score = round(sum(q["score"] for q in quiz_log) / len(quiz_log))
                quiz_text = f" Проверок знаний за неделю: {len(quiz_log)}, средний результат {avg_score}%."

            lines.append(f"- **{topic}**: уровень {level_now}/5, прогресс внутри уровня {points_now}/100 ({delta_text} за неделю). "
                          f"Что пройдено: {summary_notes}.{quiz_text}\n")

        lines.append("\n## Выполненные задачи за неделю\n")
        tasks = _load_tasks()
        done_this_week = [t for t in tasks if t.get("status") == "done"
                          and _safe_parse_iso(t.get("completed_at")) and _safe_parse_iso(t.get("completed_at")) >= period_start]
        if done_this_week:
            any_activity = True
            for t in done_this_week:
                lines.append(f"- ✅ {t['title']}\n")
        else:
            lines.append("- задач, закрытых за этот период, не найдено.\n")

        lines.append("\n## Заметки за неделю\n")
        week_notes = get_memory_notes(since=period_start)
        if week_notes:
            any_activity = True
            for n in week_notes:
                tags_text = f" _(#{', #'.join(n['tags'])})_" if n.get("tags") else ""
                lines.append(f"- {n['text']}{tags_text}\n")
        else:
            lines.append("- заметок за этот период нет.\n")

        lines.append("\n## Сверка с главной целью\n")
        lines.append(get_goal_status())

        if not any_activity:
            lines.append("\n⚠️ За эту неделю заметной учебной активности не зафиксировано. "
                          "Стоит честно обсудить с ментором, что мешает придерживаться темпа.\n")

        report_text = "\n".join(lines)

        week_key = _iso_week_key(now)
        filename = f"weekly_report_{week_key}.md"
        report_path = os.path.join(REPORTS_DIR, filename)
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_text)
        except Exception:
            pass

        _save_reports_state({"last_generated_week": week_key, "last_report_file": filename})
        return report_text
    except Exception as e:
        return f"Ошибка при формировании еженедельного отчёта: {e}"


def get_latest_weekly_report():
    """Возвращает данные последнего сформированного отчёта (для веб-интерфейса), либо None."""
    state = _load_reports_state()
    filename = state.get("last_report_file")
    if not filename:
        return None
    path = os.path.join(REPORTS_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {"filename": filename, "content": f.read(), "week": state.get("last_generated_week")}
    except Exception:
        return None


def weekly_report_worker():
    """
    Фоновая служба: раз в день проверяет, наступила ли новая ISO-неделя с момента
    последнего автосформированного отчёта, и если да - генерирует отчёт сама,
    без участия пользователя, и присылает уведомление. Это и есть "еженедельный
    отчёт, который бот формирует сам" - не нужно просить его каждый раз вручную.
    """
    logger.info("Фоновая служба еженедельных отчётов успешно запущена.")
    while True:
        try:
            now = datetime.datetime.now(TZ_OBJECT)
            current_week = _iso_week_key(now)
            state = _load_reports_state()
            # Генерируем не раньше понедельника недели и только один раз за неделю
            if state.get("last_generated_week") != current_week and now.weekday() == 0:
                generate_weekly_report()
                send_windows_notification(
                    title="🤖 ИИ-Ментор: Еженедельный отчёт",
                    message="Готов новый отчёт о том, что вы прошли и изучили за неделю."
                )
        except Exception as e:
            logger.error(f"Ошибка формирования еженедельного отчёта: {e}")
        time.sleep(6 * 60 * 60)  # проверка раз в 6 часов достаточно


def _load_memory_notes():
    with _memory_notes_lock:
        if not os.path.exists(MEMORY_NOTES_FILE):
            return []
        try:
            with open(MEMORY_NOTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []


def _save_memory_notes(notes):
    with _memory_notes_lock:
        try:
            with open(MEMORY_NOTES_FILE, "w", encoding="utf-8") as f:
                json.dump(notes, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def save_note(text, tags=None):
    """
    Сохраняет свободный факт/заметку, которая не укладывается в схему progress/tasks -
    предпочтения, договорённости, сложности, любой другой контекст, который стоит
    помнить и позже подсветить в еженедельном отчёте.
    """
    text = (text or "").strip()
    if not text:
        return "Ошибка: пустой текст заметки, save_note проигнорирован."
    try:
        notes = _load_memory_notes()
        note = {
            "id": str(uuid.uuid4())[:8],
            "text": text,
            "tags": tags if isinstance(tags, list) else ([tags] if tags else []),
            "date": datetime.datetime.now(TZ_OBJECT).isoformat(),
        }
        notes.append(note)
        _save_memory_notes(notes)
        return f"Заметка сохранена: «{text}»."
    except Exception as e:
        return f"Ошибка при сохранении заметки: {e}"


def get_memory_notes(since=None, limit=None):
    """Возвращает заметки, опционально отфильтрованные по дате (since - datetime) и с лимитом (последние N)."""
    notes = _load_memory_notes()
    if since is not None:
        notes = [n for n in notes if _safe_parse_iso(n.get("date")) and _safe_parse_iso(n.get("date")) >= since]
    if limit:
        notes = notes[-limit:]
    return notes


def _load_digest_state():
    with _digest_state_lock:
        if not os.path.exists(DIGEST_STATE_FILE):
            return {"last_digested_index": 0}
        try:
            with open(DIGEST_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                if "last_digested_index" not in state:
                    state["last_digested_index"] = 0
                return state
        except Exception:
            return {"last_digested_index": 0}


def _save_digest_state(state):
    with _digest_state_lock:
        try:
            with open(DIGEST_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


DIGEST_ACTIONS = ('update_progress', 'record_quiz_result', 'create_task', 'save_note')

DIGEST_SYSTEM_PROMPT = """Ты - фоновый архивариус ИИ-ментора Кот. Тебе НЕ нужно отвечать пользователю -
он этот текст не увидит. Твоя единственная задача: перечитать присланный ниже кусок диалога между
пользователем и ментором и извлечь из него всё, что стоит сохранить в структурированную память, даже
если пользователь сам не просил это явно логировать.

Для каждого значимого факта выпусти ОТДЕЛЬНЫЙ JSON-блок одного из видов:
{"action": "update_progress", "topic": "English/Spanish/Cyber Security/Python/Golang/AI", "level": 0-5 (необязательно), "points": 0-100 (необязательно), "status": "not_started/in_progress/done" (необязательно), "notes": "что именно пройдено"}
{"action": "record_quiz_result", "topic": "тема", "correct": число, "total": число, "notes": "необязательно"}
{"action": "create_task", "title": "текст задачи", "due": "ГГГГ-ММ-ДДTЧЧ:ММ:СС (необязательно)", "priority": "low/normal/high"}
{"action": "save_note", "text": "предпочтение/договорённость/сложность/другой важный контекст", "tags": ["тема"] (необязательно)}

Правила:
- Не отвечай пользователю, не пиши ничего кроме JSON-блоков.
- Если в диалоге не было ничего значимого для памяти - верни пустой ответ (ничего не пиши).
- Не дублируй факты, если один и тот же вывод логично выразить одним блоком.
- notes/text пиши кратко и по делу, как сжатую заметку, а не пересказ диалога."""


def _dispatch_digest_block(event_data):
    """Выполняет один JSON-блок дайджеста через уже существующие функции - без ответа пользователю."""
    action = event_data.get('action')
    if action == 'update_progress':
        return update_progress(
            event_data.get('topic', ''),
            event_data.get('level'),
            event_data.get('status'),
            event_data.get('notes'),
            event_data.get('points')
        )
    elif action == 'record_quiz_result':
        return record_quiz_result(
            event_data.get('topic', ''),
            event_data.get('correct', 0),
            event_data.get('total', 1),
            event_data.get('notes')
        )
    elif action == 'create_task':
        return create_task(
            event_data.get('title', 'Без названия'),
            event_data.get('due'),
            event_data.get('priority', 'normal')
        )
    elif action == 'save_note':
        return save_note(event_data.get('text', ''), event_data.get('tags'))
    return None


def run_chat_digest(force=False, min_new_messages=DIGEST_MIN_NEW_MESSAGES):
    """
    Просматривает кусок chat_history.json с момента последнего дайджеста, отдельным
    "административным" запросом к модели просит выпустить JSON-команды по всему
    значимому, что произошло, и прогоняет их через тот же диспетчер действий, что
    process_message - но без ответа пользователю в конце. Возвращает текстовый отчёт
    о том, что было сделано (для логов/ручного запуска), либо None, если новых
    сообщений было недостаточно (и force=False).
    """
    try:
        if not os.path.exists(DB_FILE):
            return None
        with open(DB_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, list):
            return None

        state = _load_digest_state()
        last_index = state.get("last_digested_index", 0)
        # Индекс 0 - системный промпт, его не трогаем
        last_index = max(last_index, 1)
        new_slice = [m for m in history[last_index:] if m.get("role") in ("user", "assistant")]

        if not new_slice:
            return None
        if not force and len(new_slice) < min_new_messages:
            return None

        transcript_lines = []
        for m in new_slice:
            content = m.get("content", "")
            if content.startswith("Система выполнила команды"):
                continue
            role_label = "Пользователь" if m["role"] == "user" else "Ментор"
            transcript_lines.append(f"{role_label}: {content}")
        transcript = "\n\n".join(transcript_lines)

        if not transcript.strip():
            _save_digest_state({"last_digested_index": len(history)})
            return None

        digest_messages = [
            {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
            {"role": "user", "content": f"Кусок диалога для анализа:\n\n{transcript}"}
        ]
        digest_response = ask_ollama_chat(digest_messages)
        json_blocks = extract_json_blocks(digest_response)

        results = []
        for block in json_blocks:
            try:
                event_data = json.loads(block)
                if event_data.get('action') in DIGEST_ACTIONS:
                    result = _dispatch_digest_block(event_data)
                    if result:
                        results.append(result)
            except Exception as e:
                results.append(f"Ошибка разбора блока дайджеста: {e}")

        _save_digest_state({"last_digested_index": len(history)})

        if results:
            return "Дайджест чата обновил память:\n" + "\n".join(results)
        return "Дайджест чата прошёл, значимых новых фактов не найдено."
    except Exception as e:
        return f"Ошибка дайджеста чата: {e}"


def chat_digest_worker():
    """
    Фоновая служба: периодически проверяет, накопилось ли достаточно новых сообщений
    в chat_history.json с момента последнего дайджеста, и если да - запускает
    run_chat_digest(), чтобы прогресс/задачи/заметки обновлялись даже если модель
    в живом диалоге не догадалась сама вызвать нужный JSON.
    """
    logger.info("Фоновая служба дайджеста чата успешно запущена.")
    while True:
        try:
            run_chat_digest()
        except Exception as e:
            logger.error(f"Ошибка дайджеста чата: {e}")
        time.sleep(30 * 60)  # проверка раз в 30 минут


def get_calendar_service():
    # FIX: лочим доступ к token.json, т.к. вызывается и из основного,
    # и из фонового потока одновременно
    with _calendar_lock:
        creds = None
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        return build('calendar', 'v3', credentials=creds)


def get_upcoming_events_data(max_results=15):
    """Возвращает список событий в структурированном виде (для веб-интерфейса)."""
    try:
        service = get_calendar_service()
        now = datetime.datetime.now(TZ_OBJECT).isoformat()
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=max_results, singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        result = []
        for event in events:
            start_raw = event['start'].get('dateTime', event['start'].get('date'))
            dt = datetime.datetime.fromisoformat(start_raw.split('+')[0].split('Z')[0])
            result.append({
                "summary": event.get('summary', 'Без названия'),
                "start_raw": start_raw,
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M"),
            })
        return result
    except Exception:
        return []


def get_upcoming_events():
    try:
        events = get_upcoming_events_data()
        if not events:
            return "Предстоящих событий не найдено."

        months_ru = {
            "01": "января", "02": "февраля", "03": "марта", "04": "апреля", "05": "мая", "06": "июня",
            "07": "июля", "08": "августа", "09": "сентября", "10": "октября", "11": "ноября", "12": "декабря"
        }
        days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

        output = "Расписание из календаря (АКТУАЛЬНЫЕ ТОЧНЫЕ ДАТЫ):\n"
        for ev in events:
            y, m, d = ev["date"].split("-")
            dt = datetime.date(int(y), int(m), int(d))
            date_readable = f"{int(d)} {months_ru[m]} {y} года ({days_ru[dt.weekday()]})"
            output += f"- {ev['summary']} (Дата: {date_readable}, Время: {ev['time']})\n"

        return output
    except Exception as e:
        return f"Не удалось загрузить календарь: {e}"


def create_calendar_event(summary, start_time, duration_minutes=60):
    try:
        service = get_calendar_service()
        start = datetime.datetime.fromisoformat(start_time).replace(tzinfo=TZ_OBJECT)
        end = start + datetime.timedelta(minutes=int(duration_minutes))
        event = {
            'summary': summary,
            # FIX: timeZone должен быть строкой IANA-имени, а не объектом datetime.timezone -
            # иначе тело события не сериализуется в JSON и запрос падает.
            'start': {'dateTime': start.isoformat(), 'timeZone': TIMEZONE_NAME},
            'end': {'dateTime': end.isoformat(), 'timeZone': TIMEZONE_NAME},
        }
        service.events().insert(calendarId='primary', body=event).execute()
        return f"Успешно создано событие: '{summary}' на {start.strftime('%Y-%m-%d %H:%M')}."
    except Exception as e:
        return f"Ошибка при создании события: {e}"


def delete_calendar_event(summary_to_delete):
    try:
        service = get_calendar_service()
        now = datetime.datetime.now(TZ_OBJECT).isoformat()
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=100, singleEvents=True
        ).execute()
        events = events_result.get('items', [])
        deleted_count = 0
        for event in events:
            if summary_to_delete.lower() in event.get('summary', '').lower():
                service.events().delete(calendarId='primary', eventId=event['id']).execute()
                deleted_count += 1
        if deleted_count > 0:
            return f"Успешно удалено событий '{summary_to_delete}': {deleted_count} шт."
        return f"Событие '{summary_to_delete}' не найдено."
    except Exception as e:
        return f"Ошибка при удаления: {e}"


WEB_SEARCH_TRIGGER_RE = re.compile(
    r'\b('
    r'курс[а-я]*|ссылк[а-я]*|сайт[а-я]*|ресурс[а-я]*|гайд[а-я]*|туториал[а-я]*'
    r'|документаци[а-я]*|найди(?!\s+работ)|найти(?!\s+работ)|поищи|погугли|поиск[а-я]*'
    r'|актуальн[а-я]*|свежи[а-я]*|последн(?:ие|яя|ий|юю)|новост[а-я]*'
    r'|статья|статьи|видео|где (?:скачать|найти|посмотреть)|посоветуй (?:сайт|ресурс|курс)'
    r')\b',
    re.IGNORECASE | re.UNICODE
)


def looks_like_web_search_request(text):
    """
    Эвристика: похоже ли сообщение пользователя на запрос информации из интернета
    (курсы, ссылки, ресурсы, актуальные данные и т.п.).

    FIX: раньше решение "искать в интернете или нет" полностью отдавалось на откуп
    модели через JSON-действие search_web в системном промпте - но локальная модель
    (особенно на 14B и меньше через Ollama) часто просто игнорирует эту инструкцию
    в объёмном системном промпте и отвечает по памяти, выдумывая ссылки. Поэтому для
    явных кейсов поиск теперь запускается принудительно на уровне кода, а не по
    решению модели - это гарантирует реальные ссылки независимо от послушности модели.
    """
    if not text:
        return False
    return bool(WEB_SEARCH_TRIGGER_RE.search(text))


FILE_CREATE_TRIGGER_RE = re.compile(
    r'(сделай|создай|сформируй|составь|распиши|подготовь|сохрани|запиши)[^.!?\n]{0,40}'
    r'(md\s*файл|\.md\b|файл[а-я]*|roadmap|роадмап|дорожн\w*\s+карт\w*|заметк\w*|план[а-я]*)'
    r'|roadmap[^.!?\n]{0,40}(файл|md)'
    r'|\bmd\s*файл\b',
    re.IGNORECASE | re.UNICODE
)


def looks_like_file_creation_request(text):
    """
    Эвристика: просит ли пользователь СОЗДАТЬ/СОХРАНИТЬ файл или документ
    (roadmap, план, заметку и т.п.), а не просто найти информацию в интернете.

    FIX: нужна, чтобы отличать "сделай md файл с roadmap, добавь туда ссылки"
    (главная просьба - файл; ссылки - лишь один из пунктов внутри него) от чистого
    "найди мне ссылки на курсы" (главная просьба - собственно поиск). В обоих
    случаях может сработать принудительный авто-поиск (looks_like_web_search_request),
    но когда вдобавок есть явное намерение создать файл, модели нужно отдельное
    напоминание не забыть про write_file - иначе, увидев неудачные/нерелевантные
    результаты поиска, модель рискует ответить только текстом в чате и не создать
    сам файл (именно так и произошло в реальном логе, из-за которого добавлена эта
    проверка).
    """
    if not text:
        return False
    return bool(FILE_CREATE_TRIGGER_RE.search(text))


BROWSER_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]
# Оставлено для обратной совместимости - код в других местах файла может ссылаться
# на одиночную константу BROWSER_USER_AGENT.
BROWSER_USER_AGENT = BROWSER_USER_AGENTS[0]


def _random_user_agent():
    return random.choice(BROWSER_USER_AGENTS)


# ---------------------------------------------------------------------------
# Детект антибот-страниц/капч поисковых движков.
#
# FIX: раньше любая успешно загрузившаяся страница (в т.ч. страница-заглушка
# DuckDuckGo "If this persists, please email us...") считалась валидным ответом
# и отправлялась модели как "реальные результаты поиска". Модель видела текст
# без единой ссылки, не могла выполнить инструкцию "используй только ссылки
# выше" и в итоге либо галлюцинировала урлы по памяти, либо путалась. Теперь
# такие страницы явно распознаются и никогда не выдаются за результаты поиска.
# ---------------------------------------------------------------------------
_BLOCK_MARKERS = (
    "if this persists, please email us",
    "unusual traffic",
    "detected unusual",
    "captcha",
    "are you a robot",
    "verify you are human",
    "g-recaptcha",
    "подтвердите, что запрос сделан человеком",
    "необычн" "ую активность",  # DDG/Google иногда отдают русский вариант капчи
)


def _looks_blocked(text):
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _BLOCK_MARKERS)


# ---------------------------------------------------------------------------
# Переформулировка запроса перед поиском.
#
# FIX: раньше в search_web целиком уходило сырое сообщение пользователя,
# включая приветствия и разговорные обороты - например «привет, можешь
# скинуть курсы для изучения soc analyst l1...». На таком "не-запросе"
# поисковики (особенно Bing) регулярно возвращают вообще нерелевантные
# результаты - случайные совпадения по отдельным словам - вместо честного
# "ничего не найдено", и это выглядит так, будто поиск сработал, хотя
# результаты бесполезны. Теперь перед поиском фраза сначала переформулируется
# в короткий эффективный запрос: сперва быстрым отдельным вызовом модели,
# а если это не удалось (Ollama недоступна/долго отвечает) - эвристической
# очисткой по регуляркам как фолбэк.
# ---------------------------------------------------------------------------
_QUERY_REWRITE_TIMEOUT = 20  # секунд - короткий таймаут, это вспомогательный шаг

_GREETING_FILLER_RE = re.compile(
    r'^\s*(?:привет|здравствуй(?:те)?|добрый\s+(?:день|вечер|утро)|хай|hello|hi)\s*[,!.]?\s*',
    re.IGNORECASE | re.UNICODE
)
_REQUEST_FILLER_RE = re.compile(
    r'\b(?:можешь|можете|не мог(?:ла|ли)?\s+бы|подскажи(?:те)?|скинь(?:те)?|скинуть|дай(?:те)?|'
    r'найди(?:те)?|посоветуй(?:те)?|пожалуйста|please|плиз)\b',
    re.IGNORECASE | re.UNICODE
)


def _heuristic_clean_query(text):
    """
    Быстрая эвристическая очистка без обращения к модели - убирает приветствия
    и слова-паразиты вроде "можешь", "скинь", "пожалуйста". Используется как
    фолбэк, если LLM-переформулировка (_rewrite_search_query) недоступна.
    """
    cleaned = _GREETING_FILLER_RE.sub('', text)
    cleaned = _REQUEST_FILLER_RE.sub(' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ,.!?')
    return cleaned or text.strip()


def _rewrite_search_query(user_text):
    """
    Отдельным быстрым вызовом модели превращает разговорную фразу пользователя
    в короткий эффективный поисковый запрос (3-7 слов). num_predict сильно
    ограничен и таймаут короткий (_QUERY_REWRITE_TIMEOUT) - это вспомогательный
    шаг, он не должен надолго задерживать основной ответ. При любой ошибке или
    подозрительном результате (пустой ответ, слишком длинный "запрос" - модель
    иногда вместо запроса возвращает пояснение) используется эвристический
    фолбэк _heuristic_clean_query.

    Ссылается на OLLAMA_URL/get_current_model/_strip_think_tags, определённые
    ниже по файлу - это нормально в Python, т.к. имена разрешаются в момент
    вызова функции, а не в момент её определения, и к моменту первого реального
    вызова весь модуль уже полностью загружен.
    """
    prompt = [
        {"role": "system", "content": (
            "Ты превращаешь разговорное сообщение пользователя в короткий эффективный "
            "поисковый запрос для поисковика (3-7 слов). Убери приветствия, вежливые "
            "обороты и лишние слова - оставь только суть: ключевые термины, названия, годы. "
            "Если уместнее английские термины (устоявшиеся названия профессий/технологий) - "
            "используй их. Ответь ТОЛЬКО самим запросом, без кавычек, пояснений и знаков "
            "препинания в конце."
        )},
        {"role": "user", "content": user_text},
    ]
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": get_current_model(),
                "messages": prompt,
                "stream": False,
                "options": {"num_ctx": 2048, "num_predict": 40, "temperature": 0.2},
            },
            timeout=_QUERY_REWRITE_TIMEOUT,
        )
        response.raise_for_status()
        query = _strip_think_tags(response.json()['message']['content'])
        query = query.strip().strip('"«»\'').strip()
        # Защита от пустого ответа или "запроса", который на деле - целое
        # объяснение (модель иногда игнорирует инструкцию отвечать одной фразой).
        if query and 1 <= len(query.split()) <= 12 and len(query) <= 150:
            return query
    except Exception as e:
        logger.info(f"Переформулировка поискового запроса через LLM не удалась: {e}")
    return _heuristic_clean_query(user_text)


def _strip_tags(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s or '')).strip()


def _unwrap_ddg_redirect(href):
    # DuckDuckGo оборачивает внешние ссылки в свой редирект вида
    # /l/?uddg=<url-encoded-target>&rut=... - достаём реальный адрес.
    if 'duckduckgo.com/l/' in href or 'uddg=' in href:
        full = href if href.startswith('http') else ('https:' + href if href.startswith('//') else href)
        parsed = urllib.parse.urlparse(full)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'uddg' in qs:
            return urllib.parse.unquote(qs['uddg'][0])
    return href


# ---------------------------------------------------------------------------
# Мини-кэш результатов поиска (in-memory, TTL). Модель нередко дёргает
# search_web несколько раз подряд с тем же или почти тем же запросом в рамках
# одного диалога (например, после уточняющего вопроса) - кэш экономит время
# ответа и снижает риск блокировки по частоте запросов с одного IP.
# ---------------------------------------------------------------------------
_search_cache = {}
_search_cache_lock = threading.Lock()
_SEARCH_CACHE_TTL = 300  # секунд
_SEARCH_CACHE_MAX_ITEMS = 200


def _search_cache_get(key):
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if not entry:
            return None
        value, ts = entry
        if time.time() - ts > _SEARCH_CACHE_TTL:
            del _search_cache[key]
            return None
        return value


def _search_cache_set(key, value):
    with _search_cache_lock:
        _search_cache[key] = (value, time.time())
        if len(_search_cache) > _SEARCH_CACHE_MAX_ITEMS:
            oldest_key = min(_search_cache, key=lambda k: _search_cache[k][1])
            del _search_cache[oldest_key]


def browse_web_page(url, extract_links=False):
    """
    Инструмент: Скрытый браузер для чтения содержимого сайтов.

    Ретраи при таймауте/сетевой ошибке, ротация User-Agent между попытками,
    явный показ реального адреса после редиректов и (опционально) до 20
    реальных ссылок со страницы - чтобы модель могла продолжать переход по
    сайту настоящими адресами, а не выдуманными.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return "Не указан URL для перехода."
    url = url.strip()
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    logger.info(f"Скрытый браузер открывает страницу: {url}")

    last_error = None
    attempts = 2  # первая попытка + один повтор при таймауте/сетевой ошибке
    for attempt in range(attempts):
        browser = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
                context = browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    locale='ru-RU',
                    user_agent=_random_user_agent(),
                )
                page = context.new_page()
                apply_stealth(page)
                page.goto(url, timeout=25000, wait_until="domcontentloaded")
                # Не ждём networkidle жёстко - многие сайты никогда не "затихают"
                # полностью (аналитика/чаты/реклама постоянно шлют запросы), это
                # просто съедало бы таймаут. Короткое окно ожидания достаточно,
                # чтобы догрузился основной динамический контент.
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                time.sleep(1)

                title = ""
                try:
                    title = page.title()
                except Exception:
                    pass

                text = page.locator("body").inner_text()
                clean_text = " ".join(text.split())

                parts = []
                if title:
                    parts.append(f"Заголовок страницы: {title}")
                # Показываем реальный адрес после всех редиректов - если сайт
                # перенаправил на другой URL, модель должна это видеть явно.
                if page.url and page.url != url:
                    parts.append(f"Фактический адрес после редиректа: {page.url}")
                parts.append(clean_text[:6000] if clean_text else "(страница не содержит текста)")

                if extract_links:
                    try:
                        raw_links = page.eval_on_selector_all(
                            "a[href]",
                            "els => els.slice(0, 200).map(e => ({text: e.innerText.trim(), href: e.href}))"
                        )
                        seen = set()
                        useful_links = []
                        for l in raw_links:
                            href = l.get('href', '')
                            text_ = l.get('text', '')
                            if href.startswith('http') and text_ and href not in seen:
                                seen.add(href)
                                useful_links.append((text_[:80], href))
                            if len(useful_links) >= 20:
                                break
                        if useful_links:
                            links_text = "\n".join(f"- {t}: {h}" for t, h in useful_links)
                            parts.append(f"Ссылки на странице (реальные, можно переходить):\n{links_text}")
                    except Exception:
                        pass

                return "\n\n".join(parts)
        except PlaywrightTimeoutError:
            last_error = "превышено время ожидания загрузки страницы (сайт слишком долго отвечает)"
        except Exception as e:
            last_error = str(e)
        finally:
            # FIX: гарантированно закрываем браузер даже при исключении,
            # иначе процессы chromium утекают в память
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
        if attempt < attempts - 1:
            time.sleep(1.5)

    return (f"Не удалось прочитать страницу {url}. Ошибка: {last_error}. "
            f"Если адрес мог быть неточным или устаревшим - используй search_web, "
            f"чтобы найти актуальную рабочую ссылку, вместо повторной попытки с тем же URL.")


# ---------------------------------------------------------------------------
# Парсеры отдельных поисковых источников. Каждый возвращает список
# {"title", "url", "snippet"} или бросает исключение при сетевой ошибке/HTTP-коде.
# Пустой список (без исключения) означает "источник ответил, но результатов нет" -
# в этом случае имеет смысл сразу пробовать следующий источник без повторных попыток.
# ---------------------------------------------------------------------------

def _fetch_brave(query, max_results):
    """
    Brave Search API - самый надёжный источник из всех: обычный JSON, без
    скрапинга и парсинга чужой HTML-вёрстки, без капч. Активируется только
    если задана переменная окружения BRAVE_API_KEY (бесплатный тир - около
    2000 запросов/месяц на сайте brave.com/search/api). Если ключ не задан -
    источник тихо пропускается, остальные источники работают как обычно.
    """
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        return None
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max_results},
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in (data.get("web", {}) or {}).get("results", [])[:max_results]:
        results.append({
            "title": _strip_tags(item.get("title", "")),
            "url": item.get("url", ""),
            "snippet": _strip_tags(item.get("description", "")),
        })
    return results


def _fetch_ddg_html(session, query, max_results):
    resp = session.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={
            "User-Agent": _random_user_agent(),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )
    resp.raise_for_status()
    if _looks_blocked(resp.text):
        raise RuntimeError("страница-заглушка/капча вместо результатов")
    pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</a>',
        re.S,
    )
    results = []
    for m in pattern.finditer(resp.text):
        href_raw, title_raw, snippet_raw = m.groups()
        real_url = _unwrap_ddg_redirect(html.unescape(href_raw))
        title = _strip_tags(title_raw)
        snippet = _strip_tags(snippet_raw)
        if real_url.startswith('http') and title:
            results.append({"title": title, "url": real_url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _fetch_ddg_lite(session, query, max_results):
    # FIX: раньше lite.duckduckgo.com парсился тем же regex, что и html-версия,
    # хотя вёрстка у lite-эндпоинта другая (таблицы, классы result-link /
    # result-snippet) - из-за этого "фолбэк" на lite фактически никогда не
    # находил ни одного результата. Теперь у него свой парсер.
    resp = session.post(
        "https://lite.duckduckgo.com/lite/",
        data={"q": query},
        headers={
            "User-Agent": _random_user_agent(),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )
    resp.raise_for_status()
    if _looks_blocked(resp.text):
        raise RuntimeError("страница-заглушка/капча вместо результатов")
    pattern = re.compile(
        r'class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'class="result-snippet"[^>]*>(.*?)</td>',
        re.S,
    )
    results = []
    for m in pattern.finditer(resp.text):
        href_raw, title_raw, snippet_raw = m.groups()
        real_url = _unwrap_ddg_redirect(html.unescape(href_raw))
        title = _strip_tags(title_raw)
        snippet = _strip_tags(snippet_raw)
        if real_url.startswith('http') and title:
            results.append({"title": title, "url": real_url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _fetch_bing(session, query, max_results):
    # Независимый от DuckDuckGo источник - если DDG временно блокирует IP
    # (что случается регулярно у "голого" requests-скрапинга), Bing нередко
    # ещё отвечает нормально, и наоборот.
    resp = session.get(
        "https://www.bing.com/search",
        params={"q": query, "setlang": "ru", "count": max_results},
        headers={
            "User-Agent": _random_user_agent(),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        timeout=15,
    )
    resp.raise_for_status()
    if _looks_blocked(resp.text):
        raise RuntimeError("страница-заглушка/капча вместо результатов")

    # FIX: раньше один regex одновременно требовал точный порядок атрибутов
    # (href сразу первым внутри <a ...>) и точную вложенность до сниппета -
    # у Bing разметка нередко отличается (другой порядок атрибутов внутри <a>,
    # доп. обёртки вокруг сниппета), из-за чего regex молча не находил ни
    # одного совпадения и код проваливался в шумный рендер-фолбэк через
    # браузер. Теперь сначала вырезаются блоки отдельных результатов
    # (<li class="b_algo">...</li>), а внутри каждого блока href/заголовок/
    # сниппет ищутся независимо и без привязки к порядку атрибутов.
    block_pattern = re.compile(r'<li class="b_algo"[^>]*>(.*?)</li>', re.S)
    href_title_pattern = re.compile(r'<h2[^>]*>.*?<a[^>]*?\shref="([^"]+)"[^>]*>(.*?)</a>', re.S)
    snippet_pattern = re.compile(r'<p[^>]*>(.*?)</p>', re.S)

    results = []
    for block_match in block_pattern.finditer(resp.text):
        block = block_match.group(1)
        ht_match = href_title_pattern.search(block)
        if not ht_match:
            continue
        href_raw, title_raw = ht_match.groups()
        title = _strip_tags(title_raw)
        real_url = html.unescape(href_raw)
        if not (real_url.startswith('http') and title):
            continue
        snippet_match = snippet_pattern.search(block)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        results.append({"title": title, "url": real_url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _fetch_mojeek(session, query, max_results):
    """
    Mojeek - независимый поисковый индекс (не перепродаёт выдачу Google/Bing),
    без ключа. Ещё один независимый источник для параллельного объединения
    результатов: чем больше независимых индексов, тем меньше шанс, что все
    сразу упрутся в капчу/блокировку одновременно.
    """
    resp = session.get(
        "https://www.mojeek.com/search",
        params={"q": query},
        headers={
            "User-Agent": _random_user_agent(),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        timeout=15,
    )
    resp.raise_for_status()
    if _looks_blocked(resp.text):
        raise RuntimeError("страница-заглушка/капча вместо результатов")
    block_pattern = re.compile(r'<a[^>]+class="[^"]*\btitle\b[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    snippet_pattern = re.compile(r'<p[^>]*class="[^"]*s\b[^"]*"[^>]*>(.*?)</p>', re.S)
    snippets = [_strip_tags(s) for s in snippet_pattern.findall(resp.text)]
    results = []
    for i, m in enumerate(block_pattern.finditer(resp.text)):
        href_raw, title_raw = m.groups()
        title = _strip_tags(title_raw)
        real_url = html.unescape(href_raw)
        if not (real_url.startswith('http') and title):
            continue
        snippet = snippets[i] if i < len(snippets) else ""
        results.append({"title": title, "url": real_url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _normalize_url_for_dedup(url):
    """Нормализует URL для дедупликации между источниками: без протокола,
    www-префикса, хвостового слэша и хвостовых query-параметров - разные
    источники нередко отдают одну и ту же страницу с разным написанием адреса."""
    try:
        p = urllib.parse.urlparse(url)
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/")
        return f"{host}{path}".lower()
    except Exception:
        return (url or "").lower()


def _merge_search_results(named_result_lists, max_results):
    """
    Объединяет результаты НЕСКОЛЬКИХ источников в одну ранжированную выдачу
    вместо того, чтобы просто брать первый источник, который ответил
    (как было раньше). Раунд-робин между источниками (по одному результату
    от каждого источника за круг) вместо "все результаты источника A, потом
    все результаты источника B" - так в топе выдачи оказываются страницы,
    подтверждённые НЕСКОЛЬКИМИ независимыми поисковиками, а не просто первые
    ссылки одного источника. Дедупликация - по нормализованному URL.
    """
    seen = set()
    merged = []
    max_len = max((len(lst) for _, lst in named_result_lists), default=0)
    for idx in range(max_len):
        if len(merged) >= max_results:
            break
        for name, lst in named_result_lists:
            if len(merged) >= max_results:
                break
            if idx >= len(lst):
                continue
            item = lst[idx]
            key = _normalize_url_for_dedup(item.get("url", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append({**item, "_source": name})
    return merged


def _format_results(query, results, source=""):
    label = f" ({source})" if source else ""
    lines = [f"Результаты поиска по запросу «{query}»{label}:"]
    for i, r in enumerate(results, 1):
        src_tag = f" [{r['_source']}]" if r.get("_source") else ""
        line = f"{i}. {r['title']}{src_tag}\n   Ссылка: {r['url']}"
        if r.get("snippet"):
            line += f"\n   {r['snippet']}"
        lines.append(line)
    lines.append(
        "\nИспользуй только эти реальные ссылки (через visit_url), "
        "если нужно прочитать содержимое подробнее - не придумывай другие адреса."
    )
    return "\n".join(lines)


def _decode_bing_redirect(href):
    """
    Bing оборачивает часть внешних ссылок в свой редирект вида
    bing.com/ck/a?...&u=a1<base64-адрес>&ntb=1 - без декодирования такие
    ссылки выглядят как "ссылки на сам Bing" и раньше либо засоряли вывод,
    либо терялись при фильтрации навигации поисковика. Значение параметра
    u - это префикс "a1" + base64(реальный_адрес) (без паддинга).
    """
    if 'bing.com/ck/a' not in href:
        return href
    try:
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        u = qs.get('u', [None])[0]
        if not u:
            return href
        b64 = u[2:] if u.startswith('a1') else u
        b64 += '=' * (-len(b64) % 4)
        decoded = base64.urlsafe_b64decode(b64).decode('utf-8', errors='ignore')
        if decoded.startswith('http'):
            return decoded
    except Exception:
        pass
    return href


def _render_search_results(url, own_domains, max_results):
    """
    Последний фолбэк поиска: открывает поисковую выдачу через Playwright и
    вытаскивает СТРУКТУРИРОВАННО только ссылки на внешние сайты.

    FIX: раньше этот фолбэк вызывал общий browse_web_page(extract_links=True)
    и отдавал модели весь текст страницы целиком, включая навигационное
    "мясо" поисковика ("Перейти к контенту", "ВСЕПОИСКИЗОБРАЖЕНИЯВИДЕОКАРТЫ...",
    ссылки смены языка, "Картинки"/"Видео"/"Покупки" и т.п.) - модель либо
    путалась в этом шуме, либо (как в реальном случае) выдавала пользователю
    список навигационных ссылок самого Bing вместо результатов. Теперь
    вытаскиваются только реальные ссылки-результаты: ссылки на сам поисковик
    (own_domains) отбрасываются, а обёрнутые Bing-редиректы (bing.com/ck/a)
    предварительно декодируются до реального адреса через _decode_bing_redirect.
    """
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                locale='ru-RU',
                user_agent=_random_user_agent(),
            )
            page = context.new_page()
            apply_stealth(page)
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            time.sleep(1)

            body_text = " ".join(page.locator("body").inner_text().split())
            if _looks_blocked(body_text):
                return None, "антибот-страница/капча"

            raw_links = page.eval_on_selector_all(
                "a[href]",
                "els => els.slice(0, 300).map(e => ({text: e.innerText.trim(), href: e.href}))"
            )
            seen = set()
            results = []
            for l in raw_links:
                href = l.get('href', '')
                text_ = l.get('text', '')
                if not href.startswith('http') or not text_:
                    continue
                href = _decode_bing_redirect(href)
                if href in seen:
                    continue
                host = urllib.parse.urlparse(href).netloc.lower()
                if any(d in host for d in own_domains):
                    # Ссылка на сам поисковик (навигация, смена языка, реклама
                    # и т.п.) - не результат поиска, пропускаем.
                    continue
                seen.add(href)
                results.append({"title": text_[:120], "url": href, "snippet": ""})
                if len(results) >= max_results:
                    break
            return results, None
    except PlaywrightTimeoutError:
        return None, "превышено время ожидания загрузки страницы"
    except Exception as e:
        return None, str(e)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def search_web(query, max_results=6):
    """
    Инструмент: Поиск в интернете, без обязательных API-ключей (но с
    опциональной поддержкой Brave Search API, если задан BRAVE_API_KEY).

    Возвращает список реальных найденных результатов (заголовок, точная ссылка,
    краткое описание) - модель не должна угадывать/придумывать URL сайтов "из
    головы" (это и было причиной битых/несуществующих ссылок в ответах), а
    сначала находит реальные адреса через поиск и только потом переходит по
    ним через visit_url.

    Источники запрашиваются ПАРАЛЛЕЛЬНО (не по очереди), затем результаты
    ОБЪЕДИНЯЮТСЯ (_merge_search_results) в одну ранжированную выдачу с
    дедупликацией по URL - вместо старой схемы "первый ответивший источник
    побеждает, остальные не спрашиваем". Так в выдаче оказываются страницы,
    подтверждённые несколькими независимыми поисковиками, поиск не зависит
    от блокировки одного конкретного источника, и весь раунд занимает время
    самого медленного источника, а не сумму времени всех источников по очереди:
      - Brave Search API (если задан BRAVE_API_KEY) - JSON, без скрапинга.
      - DuckDuckGo (html-эндпоинт).
      - Bing.
      - Mojeek - независимый индекс, не перепродающий чужую выдачу.

    Если параллельный раунд не дал ни одного результата (все источники
    заблокированы/недоступны), используются последовательные фолбэки:
      - DuckDuckGo lite-эндпоинт (отдельный парсер под его вёрстку).
      - Рендер DuckDuckGo через Playwright, структурированное извлечение
        только внешних ссылок (помогает, если голый requests режется на
        TLS/JS-фингерпринтинге сайта).
      - Рендер Bing через Playwright, аналогично.

    На каждом шаге страница явно проверяется на признаки капчи/блокировки
    (_looks_blocked) - заблокированная "заглушка" НИКОГДА не возвращается
    модели под видом результатов поиска. Если все источники недоступны,
    модель получает явную инструкцию не придумывать ссылки и честно сказать
    пользователю, что поиск не удался.
    """
    query = (query or "").strip()
    if not query:
        return "Пустой поисковый запрос."

    original_query = query
    query = _rewrite_search_query(query)
    if query != original_query:
        logger.info(f"Поисковый запрос переформулирован: «{original_query}» -> «{query}»")

    cache_key = f"{query.lower()}::{max_results}"
    cached = _search_cache_get(cache_key)
    if cached:
        return cached

    errors = []
    session = requests.Session()

    # Запрашиваем сразу немного больше max_results у каждого источника,
    # чтобы после дедупликации по URL всё равно осталось достаточно
    # результатов для итоговой выдачи.
    per_source_n = max(max_results, min(max_results + 3, 10))

    parallel_fetchers = [
        ("Brave Search API", lambda: _fetch_brave(query, per_source_n)),
        ("DuckDuckGo", lambda: _fetch_ddg_html(session, query, per_source_n)),
        ("Bing", lambda: _fetch_bing(session, query, per_source_n)),
        ("Mojeek", lambda: _fetch_mojeek(session, query, per_source_n)),
    ]

    named_result_lists = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(parallel_fetchers)) as pool:
        future_to_name = {pool.submit(fn): name for name, fn in parallel_fetchers}
        for future in concurrent.futures.as_completed(future_to_name, timeout=20):
            name = future_to_name[future]
            try:
                results = future.result()
                if results:
                    named_result_lists.append((name, results))
                elif results is None:
                    pass  # источник не настроен (например, нет BRAVE_API_KEY) - тихо пропускаем
                else:
                    errors.append(f"{name}: источник ответил, но результатов нет")
            except Exception as e:
                errors.append(f"{name}: {e}")

    if named_result_lists:
        merged = _merge_search_results(named_result_lists, max_results)
        if merged:
            sources_label = " + ".join(name for name, _ in named_result_lists)
            out = _format_results(query, merged, source=f"объединено: {sources_label}")
            _search_cache_set(cache_key, out)
            return out

    # Ни один параллельный источник не дал результатов - последовательные фолбэки.
    for name, fetcher in (("DuckDuckGo (lite)", lambda: _fetch_ddg_lite(session, query, max_results)),):
        for attempt in range(2):
            try:
                results = fetcher()
                if results:
                    out = _format_results(query, results, source=name)
                    _search_cache_set(cache_key, out)
                    return out
                break
            except Exception as e:
                errors.append(f"{name}: {e}")
                time.sleep(0.8 + random.random())

    # Последний фолбэк: полноценный браузер (Playwright), структурированное
    # извлечение ссылок-результатов (_render_search_results) - помогает,
    # если голый requests режется на TLS/JS-фингерпринтинге, но не спасает
    # от настоящей капчи, поэтому страница всё равно проверяется на _looks_blocked.
    for label, url, own_domains in (
        ("DuckDuckGo, браузер", f"https://duckduckgo.com/html/?q={urllib.parse.quote(query)}", ["duckduckgo.com"]),
        ("Bing, браузер", f"https://www.bing.com/search?q={urllib.parse.quote(query)}", ["bing.com"]),
    ):
        results, err = _render_search_results(url, own_domains, max_results)
        if err:
            errors.append(f"{label}: {err}")
            continue
        if results:
            out = _format_results(query, results, source=label)
            _search_cache_set(cache_key, out)
            return out
        errors.append(f"{label}: страница загрузилась, но ни одной внешней ссылки-результата не найдено")

    logger.warning(f"Поиск не удался по всем источникам для запроса '{query}': {errors}")
    return (f"Поиск в интернете не удался по запросу «{query}» - все источники сейчас "
            f"недоступны или заблокированы антибот-защитой ({'; '.join(errors[-3:])}). "
            f"НЕ придумывай ссылки и источники по памяти - честно сообщи пользователю, что "
            f"поиск сейчас не сработал, и предложи повторить попытку позже или "
            f"переформулировать запрос покороче/проще.")



def list_workspace_files():
    """Инструмент: Просмотр списка файлов."""
    try:
        files = os.listdir(WORKSPACE_DIR)
        if not files:
            return "Папка 'workspace' пуста."
        return "Файлы в 'workspace':\n" + "\n".join([f"- {f}" for f in files])
    except Exception as e:
        return f"Ошибка чтения папки: {e}"


def _resolve_safe_path(filename):
    """
    Разрешает путь внутри WORKSPACE_DIR, отбрасывая попытки выйти за его пределы.
    FIX (усиление): раньше пустое имя или имя из одних пробелов/точек проходило
    дальше и падало с менее понятной ошибкой ниже по стеку; null-байт в имени
    (классический трюк обхода проверок пути в некоторых ОС/библиотеках) не
    отбрасывался явно. Теперь оба случая отклоняются здесь, до похода на диск.
    """
    if not filename or not isinstance(filename, str):
        return None
    if "\x00" in filename:
        return None
    cleaned = filename.strip().lstrip("/\\")
    if not cleaned or cleaned in (".", ".."):
        return None
    safe_path = os.path.abspath(os.path.join(WORKSPACE_DIR, cleaned))
    if os.path.commonpath([safe_path, WORKSPACE_DIR]) != WORKSPACE_DIR:
        return None
    return safe_path


def read_workspace_file(filename):
    """Инструмент: Чтение файлов."""
    try:
        safe_path = _resolve_safe_path(filename)
        if safe_path is None:
            return "Ошибка безопасности: Доступ ограничен."
        if not os.path.exists(safe_path):
            return f"Файл '{filename}' не найден."
        with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
            return f"Содержимое '{filename}':\n{f.read(4000)}"
    except Exception as e:
        return f"Ошибка чтения: {e}"


def write_workspace_file(filename, content):
    """Инструмент: Создание и перезапись файлов."""
    try:
        safe_path = _resolve_safe_path(filename)
        if safe_path is None:
            return "Ошибка безопасности: Доступ ограничен."
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Файл '{filename}' успешно сохранен/обновлен в папке workspace."
    except Exception as e:
        return f"Ошибка записи: {e}"


# ---------------------------------------------------------------------------
# Obsidian - чтение/запись заметок напрямую в реальный vault пользователя на
# диске (а не в отдельную папку workspace), чтобы всё, что сохраняет ментор
# (конспекты, планы, заметки по трекам), сразу оказывалось в обычном
# Obsidian-интерфейсе пользователя: с вики-ссылками [[...]], тегами #тег и
# YAML-фронт-маттером, которые Obsidian понимает "из коробки".
# ---------------------------------------------------------------------------
_obsidian_lock = threading.Lock()


def _load_obsidian_config():
    with _obsidian_lock:
        if not os.path.exists(OBSIDIAN_CONFIG_FILE):
            return {"vault_path": None}
        try:
            with open(OBSIDIAN_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"vault_path": None}


def _save_obsidian_config(config):
    with _obsidian_lock:
        try:
            with open(OBSIDIAN_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def get_obsidian_vault_path():
    """Текущий путь к vault'у (или None, если ещё не настроен)."""
    return _load_obsidian_config().get("vault_path")


def set_obsidian_vault_path(path):
    """
    Инструмент/настройка: указать путь к реальному Obsidian vault на диске.
    Проверяет, что папка существует (Obsidian создаёт свою служебную папку
    '.obsidian' внутри vault'а при первом открытии - её наличие не обязательно,
    но сам путь должен существовать и быть директорией).
    """
    if not path or not isinstance(path, str):
        return "Не указан путь к vault."
    cleaned = path.strip().strip('"').strip("'")
    abs_path = os.path.abspath(os.path.expanduser(cleaned))
    if not os.path.isdir(abs_path):
        return (f"Папка '{abs_path}' не найдена или не является директорией. "
                f"Проверь путь и попробуй снова (папка должна уже существовать на диске).")
    _save_obsidian_config({"vault_path": abs_path})
    has_dot_obsidian = os.path.isdir(os.path.join(abs_path, ".obsidian"))
    hint = "" if has_dot_obsidian else " (папка '.obsidian' внутри не найдена - убедись, что это действительно vault, а не случайная папка)"
    return f"Obsidian vault подключён: {abs_path}{hint}"


def _resolve_obsidian_path(rel_path):
    """
    Аналог _resolve_safe_path, но относительно vault'а Obsidian, а не workspace.
    Отбрасывает попытки выйти за пределы vault'а. Возвращает (safe_path, None)
    либо (None, текст_ошибки) - в отличие от workspace-версии, ошибок здесь
    больше одной (vault не настроен / путь некорректен), поэтому текст ошибки
    возвращается сразу, чтобы вызывающий код не дублировал сообщения.
    """
    vault = get_obsidian_vault_path()
    if not vault:
        return None, ("Obsidian vault ещё не подключён. Укажи путь к vault'у через "
                       "действие set_obsidian_vault (или в сайдбаре веб-интерфейса), прежде чем работать с заметками.")
    if not rel_path or not isinstance(rel_path, str) or "\x00" in rel_path:
        return None, "Не указан путь заметки внутри vault."
    cleaned = rel_path.strip().lstrip("/\\")
    if not cleaned or cleaned in (".", ".."):
        return None, "Некорректный путь заметки."
    if not cleaned.lower().endswith(".md"):
        cleaned += ".md"
    safe_path = os.path.abspath(os.path.join(vault, cleaned))
    if os.path.commonpath([safe_path, vault]) != vault:
        return None, "Ошибка безопасности: путь выходит за пределы vault."
    return safe_path, None


def _obsidian_frontmatter(tags=None, extra=None):
    tags = tags or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    lines = ["---", f"created: {datetime.datetime.now(TZ_OBJECT).strftime('%Y-%m-%d %H:%M')}"]
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {t}" for t in tags)
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def list_obsidian_notes(subfolder=None):
    """Инструмент: список заметок (.md) в vault'е или его подпапке."""
    vault = get_obsidian_vault_path()
    if not vault:
        return ("Obsidian vault ещё не подключён. Укажи путь через действие "
                "set_obsidian_vault, прежде чем смотреть список заметок.")
    base = vault
    if subfolder:
        safe_path, err = _resolve_obsidian_path(os.path.join(subfolder, "__dummy__.md"))
        if err:
            return err
        base = os.path.dirname(safe_path)
    if not os.path.isdir(base):
        return f"Папка '{subfolder}' не найдена в vault'е."
    notes = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".md"):
                rel = os.path.relpath(os.path.join(root, f), vault)
                notes.append(rel.replace(os.sep, "/"))
    if not notes:
        return "В vault'е (или указанной подпапке) пока нет заметок."
    notes.sort()
    return "Заметки в vault'е:\n" + "\n".join(f"- {n}" for n in notes[:200])


def read_obsidian_note(note_path):
    """Инструмент: чтение содержимого заметки из Obsidian vault."""
    safe_path, err = _resolve_obsidian_path(note_path)
    if err:
        return err
    if not os.path.exists(safe_path):
        return f"Заметка '{note_path}' не найдена в vault'е."
    try:
        with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
            return f"Содержимое заметки '{note_path}':\n{f.read(6000)}"
    except Exception as e:
        return f"Ошибка чтения заметки: {e}"


def write_obsidian_note(note_path, content, tags=None, overwrite=True):
    """
    Инструмент: создание/полная перезапись заметки в Obsidian vault.
    Добавляет YAML-фронт-маттер (created, tags), если содержимое ещё не
    начинается с '---' (т.е. модель не задала фронт-маттер сама).
    """
    safe_path, err = _resolve_obsidian_path(note_path)
    if err:
        return err
    if os.path.exists(safe_path) and not overwrite:
        return f"Заметка '{note_path}' уже существует, а overwrite=false - используй append_obsidian_note, чтобы дописать в конец."
    try:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        body = content or ""
        if not body.lstrip().startswith("---"):
            body = _obsidian_frontmatter(tags) + body
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(body)
        return f"Заметка '{note_path}' сохранена в Obsidian vault."
    except Exception as e:
        return f"Ошибка записи заметки: {e}"


def append_obsidian_note(note_path, content, create_if_missing=True):
    """Инструмент: дописать текст в конец существующей заметки (или создать новую, если её нет)."""
    safe_path, err = _resolve_obsidian_path(note_path)
    if err:
        return err
    try:
        exists = os.path.exists(safe_path)
        if not exists and not create_if_missing:
            return f"Заметка '{note_path}' не найдена, а create_if_missing=false."
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        prefix = ""
        if not exists:
            prefix = _obsidian_frontmatter()
        with open(safe_path, "a", encoding="utf-8") as f:
            if prefix:
                f.write(prefix)
            f.write(("\n" if exists else "") + content)
        return f"Текст добавлен в заметку '{note_path}'."
    except Exception as e:
        return f"Ошибка дозаписи заметки: {e}"


def search_obsidian_notes(query, max_results=10):
    """
    Инструмент: полнотекстовый поиск по заметкам vault'а (заголовки и
    содержимое) - простой grep без индекса, но этого достаточно для
    личного vault'а разумного размера.
    """
    vault = get_obsidian_vault_path()
    if not vault:
        return ("Obsidian vault ещё не подключён. Укажи путь через действие "
                "set_obsidian_vault, прежде чем искать по заметкам.")
    query = (query or "").strip().lower()
    if not query:
        return "Пустой поисковый запрос по заметкам."
    matches = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.lower().endswith(".md"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, vault).replace(os.sep, "/")
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except Exception:
                continue
            lower = text.lower()
            if query in rel.lower() or query in lower:
                idx = lower.find(query)
                snippet = ""
                if idx >= 0:
                    start = max(0, idx - 60)
                    snippet = text[start:idx + len(query) + 60].replace("\n", " ")
                matches.append((rel, snippet))
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break
    if not matches:
        return f"По запросу «{query}» ничего не найдено в заметках vault'а."
    lines = [f"Найдено в заметках по запросу «{query}»:"]
    for rel, snippet in matches:
        lines.append(f"- {rel}" + (f": …{snippet}…" if snippet else ""))
    return "\n".join(lines)


def create_daily_note(content, folder="Daily"):
    """
    Инструмент: создать (или дописать, если уже существует) ежедневную
    заметку за сегодня в стандартном для Obsidian формате имени YYYY-MM-DD.md.
    """
    today = datetime.datetime.now(TZ_OBJECT).strftime("%Y-%m-%d")
    note_path = f"{folder}/{today}.md" if folder else f"{today}.md"
    safe_path, err = _resolve_obsidian_path(note_path)
    if err:
        return err
    if os.path.exists(safe_path):
        return append_obsidian_note(note_path, content)
    return write_obsidian_note(note_path, content, tags=["daily"])


def get_current_model():
    with _model_lock:
        return _current_model


def set_current_model(model_name):
    """
    Меняет активную модель для последующих запросов к Ollama.
    Не проверяет, что модель реально скачана - Ollama сама вернёт ошибку
    при следующем запросе, если модели нет (это отобразится пользователю
    как обычная "Ошибка подключения к Ollama").
    """
    global _current_model
    if not model_name or not isinstance(model_name, str):
        raise ValueError("Пустое имя модели")
    with _model_lock:
        _current_model = model_name.strip()
    return _current_model


def list_local_models():
    """
    Возвращает список моделей, реально скачанных локально в Ollama
    (GET /api/tags), а не какой-то захардкоженный список - чтобы в меню
    выбора попадали только модели, которые действительно есть у пользователя.
    """
    url = "http://localhost:11434/api/tags"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        models = response.json().get("models", [])
        result = []
        for m in models:
            result.append({
                "name": m.get("name") or m.get("model"),
                "size": m.get("size"),
                "param_size": (m.get("details") or {}).get("parameter_size"),
                "quant": (m.get("details") or {}).get("quantization_level"),
            })
        result.sort(key=lambda x: x["name"] or "")
        return result
    except Exception as e:
        raise RuntimeError(f"Не удалось получить список моделей Ollama: {e}")


def _strip_think_tags(text):
    """
    Убирает <think>...</think> блоки (DeepSeek-R1, Qwen3 и другие reasoning-модели
    вставляют туда рассуждения перед финальным ответом). Без этого:
    - extract_json_blocks() мог бы случайно выполнить JSON, который модель
      просто "прикидывала" в рассуждениях, а не выбрала как финальное действие;
    - пользователь увидел бы в чате сырые внутренние рассуждения модели.
    Незакрытый <think> (например, обрезанный по num_predict ответ) удаляется целиком,
    т.к. это обрывок рассуждений, а не финальный ответ.
    """
    if "<think>" not in text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
    return text.strip()


OLLAMA_URL = "http://localhost:11434/api/chat"
# FIX: временный обрыв соединения (Ollama ещё не успела встать после перезапуска,
# короткая просадка) раньше сразу превращался в текст ошибки, показанный
# пользователю как финальный ответ модели. Несколько быстрых ретраев с паузой
# покрывают самый частый случай "сервер как раз перезапускается", не увеличивая
# заметно время ожидания в реальном сбое (таймаут по-прежнему не ретраится -
# это осознанное решение модели/железа, а не временный сбой соединения).
OLLAMA_CONNECT_RETRIES = 2
OLLAMA_RETRY_DELAY = 2  # секунд


def _build_ollama_payload(messages, stream):
    return {
        "model": get_current_model(),
        "messages": messages,
        "stream": stream,
        # FIX: keep_alive держит модель в памяти между запросами, чтобы не было
        # повторного "холодного" прогрева весов на каждый вопрос
        "keep_alive": "30m",
        "options": {
            "num_ctx": 16384,
            # FIX: ограничиваем длину генерации, иначе модель может генерировать
            # очень долго на слабом железе
            "num_predict": 3000
        }
    }


def ask_ollama_chat(messages):
    model_name = get_current_model()
    last_connection_error = None
    for attempt in range(OLLAMA_CONNECT_RETRIES + 1):
        try:
            response = requests.post(OLLAMA_URL, json=_build_ollama_payload(messages, stream=False),
                                      timeout=OLLAMA_TIMEOUT)
            response.raise_for_status()
            content = response.json()['message']['content']
            return _strip_think_tags(content)
        except requests.exceptions.Timeout:
            return (f"Ошибка подключения к Ollama: превышено время ожидания ({OLLAMA_TIMEOUT}с). "
                    f"Проверьте 'ollama ps' - возможно модель '{model_name}' работает на CPU и отвечает слишком медленно, "
                    f"или сервер Ollama не запущен.")
        except requests.exceptions.ConnectionError as e:
            last_connection_error = e
            if attempt < OLLAMA_CONNECT_RETRIES:
                logger.info(f"Ollama недоступна (попытка {attempt + 1}/{OLLAMA_CONNECT_RETRIES + 1}), "
                            f"повтор через {OLLAMA_RETRY_DELAY}с...")
                time.sleep(OLLAMA_RETRY_DELAY)
                continue
        except Exception as e:
            return f"Ошибка подключения к Ollama: {e}"
    logger.error(f"Ollama недоступна после {OLLAMA_CONNECT_RETRIES + 1} попыток: {last_connection_error}")
    return (f"Ошибка подключения к Ollama: сервер недоступен на localhost:11434 после "
            f"{OLLAMA_CONNECT_RETRIES + 1} попыток. Запущена ли Ollama (`ollama serve`)?")


def ask_ollama_chat_stream(messages):
    """
    Потоковая версия ask_ollama_chat: генератор, отдающий текст по кускам
    (сырым, до вырезания <think>-тегов - это делает вызывающий код над полным
    накопленным текстом, т.к. тег может быть разорван между кусками). Используется
    только для финального ответа пользователю (process_message_stream), а не
    там, где нужно сначала целиком распарсить JSON-действия.
    """
    model_name = get_current_model()
    try:
        with requests.post(OLLAMA_URL, json=_build_ollama_payload(messages, stream=True),
                            timeout=OLLAMA_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break
    except requests.exceptions.Timeout:
        yield (f"\n[Ошибка подключения к Ollama: превышено время ожидания ({OLLAMA_TIMEOUT}с). "
               f"Модель '{model_name}' отвечает слишком медленно, или сервер Ollama не запущен.]")
    except requests.exceptions.ConnectionError:
        yield "\n[Ошибка подключения к Ollama: сервер недоступен на localhost:11434.]"
    except Exception as e:
        yield f"\n[Ошибка подключения к Ollama: {e}]"


def load_chat_history(calendar_data):
    """Загружает историю чата и обновляет системный промпт на актуальный."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
                if isinstance(history, list) and len(history) > 0:
                    history[0] = generate_system_prompt(calendar_data)
                    return history
        except Exception as e:
            logger.error(f"Ошибка чтения файла памяти: {e}")

    return [generate_system_prompt(calendar_data)]


def save_chat_history(history):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def generate_system_prompt(calendar_data):
    current_date = datetime.date.today().isoformat()
    progress_snapshot = get_progress()
    goal_snapshot = get_goal_status()
    roadmap_snapshot = get_roadmap()
    obsidian_vault = get_obsidian_vault_path()
    return {
        "role": "system",
        "content": f"""Ты персональный ИИ-ментор Кот. Сегодня: {current_date}. Часовой пояс: {TIMEZONE_NAME}.

{goal_snapshot}
{roadmap_snapshot}

РАБОТА С ROADMAP (промежуточные цели: неделя/месяц/квартал/полгода/год):
Блок ROADMAP выше - это авто-черновик промежуточных целей для каждого трека, рассчитанный линейно от
текущего уровня до дедлайна. Используй его как ориентир на каждом чек-ине: если пользователь явно
отстаёт от ближайшего чекпоинта (his уровень заметно ниже level_target, а дата уже близко или прошла) -
скажи об этом прямо, без драматизации, и предложи конкретный шаг. Когда пользователь явно договаривается
о другой промежуточной цели ("к концу месяца хочу дойти до B1", "в этом квартале беру паузу по испанскому") -
зафиксируй это через update_checkpoint (укажи topic, horizon: week/month/quarter/half_year/year, и что
изменилось - level_target/description/target_date/done). Когда пользователь меняет финальную цель или
дедлайн трека через set_goal_milestone, черновик пересчитывается автоматически - ручные правки чекпоинтов
при этом сохраняются. Если пользователь прямо спрашивает "покажи roadmap/план по шагам" - вызови get_roadmap
и перескажи своими словами, а не просто продублируй сырые данные.
Твоя главная функция - быть коучем, который каждый день приближает пользователя к этой цели. Ты не абстрактный
преподаватель "всего понемногу", а персональный тренер с чёткой финишной линией и дедлайном. Держи дедлайн и
разрыв между текущим уровнем и целевым в фокусе каждого разговора, не только когда об этом прямо спрашивают.

КАК ВЕСТИ КАЖДЫЙ ИЗ ТРЁХ ТРЕКОВ:

СТАРТОВЫЕ УРОВНИ ПОЛЬЗОВАТЕЛЯ (зафиксированы им самим, ориентируйся на них по умолчанию, если progress.json
говорит другое - доверяй более новым данным из progress.json, эта строка только про точку отсчёта):
   - English: A2 (цель C1) - НЕ ведй себя так, будто человек уже B1+, начинай с грамматики и лексики
     уровня A2->B1, усложняй постепенно по мере реальных успехов.
   - Spanish: A1 (цель B2-C1) - самый ранний этап, нужны база (алфавит произношения не нужен, но базовая
     грамматика: presente, ser/estar, основные конструкции) и словарный запас на бытовые темы.
   - Python: junior+ - можно сразу говорить на техническом уровне (ООП, стандартные библиотеки, немного
     async/тестов), но не считать сеньорские темы (архитектура крупных систем, глубокий perf-тюнинг) базовым
     знанием - вводи их постепенно.
   - Golang: junior - синтаксис и базовые паттерны (горутины/каналы на базовом уровне) можно считать
     пройденными в общих чертах, но не углубляйся сразу в сложные concurrency-паттерны без повторения основ.
   - AI: слабый уровень - начинай с основ (что такое LLM/ML на практическом уровне, как пользоваться
     инструментами), не заходи сразу в матчасть (backprop, архитектуры) без явного запроса.
   - Cyber Security: слабый уровень - несмотря на амбициозную цель (job-ready к концу 2027), сейчас нужно
     начинать с основ (сети, ОС, базовые концепции ИБ, CompTIA Security+ как первый ориентир), а не с
     продвинутых пентест-техник. Явно держи в голове, что дистанция до цели большая - see раздел про
     сверку с графиком ниже.

1) English (текущий уровень: A2, цель: C1).
   - Разговорная практика: предлагай писать/говорить (текстом) на английском на бытовые и рабочие темы, мягко
     исправляй ошибки грамматики и лексики, объясняй правило коротко и по делу. На A2 не усложняй лексику -
     простые предложения, частотные слова, базовые времена (Present Simple/Continuous, Past Simple, going to).
   - Периодически предлагай мини-упражнения уровня A2->B1: перефразировать простое предложение, подобрать
     синоним из частотной лексики, разобрать бытовую идиому, написать короткое сообщение на бытовую тему
     (переписка на работу/собеседования - когда дорастёт хотя бы до B1-B2, не с A2).
   - Держи в фокусе ближайший реалистичный шаг - B1, а не сразу C1: техническую лексику по кибербезопасности
     и деловую переписку вводи постепенно, как только базовая грамматика станет уверенной.
   - Если пользователь просит план - предлагай конкретику: сколько часов в неделю, какие ресурсы под уровень
     A2 (простые тексты/подкасты для начинающих, разговорные тренажёры для новичков, экзамен A2/B1 как
     ближайший ориентир, не сразу IELTS/Cambridge), а не общие слова "смотри фильмы".

2) Spanish (текущий уровень: A1, цель: B2-C1, латиноамериканский вариант - предпочтительно с уклоном в
   риоплатский диалект Уругвая/Аргентины или чилийский испанский, в зависимости от того, что пользователь уточнит).
   - Самый ранний этап - начинай с основ: алфавит/произношение (если нужно), базовая грамматика (ser/estar,
     presente de indicativo, основные вопросы), минимальный бытовой словарь (числа, дни недели, еда, простые
     фразы для знакомства). Не переходи сразу к аренде жилья/банкам/миграционным процедурам лексически сложным
     языком - это цель следующих этапов (B1+), сейчас - фундамент.
   - Так же, как с английским: разговорная практика текстом, исправление ошибок, мини-упражнения,
     периодическая проверка лексики/грамматики, но с поправкой на уровень новичка.
   - Учитывай, что испанский стартует позже английского и обычно прогрессирует медленнее - если испанский
     сильно отстаёт от плана, отметь это и предложи скорректировать нагрузку (например, больше времени на
     испанский за счёт English, если английский уже уверенно опережает график).

3) Cyber Security (текущий уровень: слабый/начальный, цель: удалённая работа к концу 2027 - начало 2028,
   с запасом перед переездом).
   - Веди как карьерный трек, но начинай с основ, а не с продвинутых техник: базовые сети (модель OSI, TCP/IP),
     основы ОС (Linux/Windows), базовые концепции ИБ (CIA-триада, типы атак на уровне понимания, а не
     эксплуатации), CompTIA Security+ как первый реалистичный ориентир для сертификации. Продвинутые темы
     (eJPT/PJPT/OSCP, Blue Team Level 1) вводи только после того, как база закрыта.
   - практические площадки для новичков (TryHackMe - Pre Security / Cyber Security 101 путь, лаборатории для
     начинающих), портфолио начинай собирать рано (даже простые writeups по учебным лабам), резюме и LinkedIn
     под удалённые вакансии готовь заранее, но подготовку к техническим собеседованиям начинай ближе к
     появлению реальной базы знаний.
   - Python (уровень junior+) из общего списка тем - прикладной инструмент именно для этого трека
     (автоматизация, скрипты для пентеста/анализа, работа с API), не самоцель. Используй существующий уровень
     Python как мостик - направляй практику в сторону security-задач, где это уместно, не пересказывая базовый
     синтаксис заново.
   - Учитывай нюанс удалённой работы из Уругвая/Чили: часовые пояса (важно уточнять у пользователя целевой
     рынок вакансий - США, Европа, Латам), контракты как контрактор/B2B, важность английского уровня B2+
     уже на этапе собеседований (раньше, чем C1) - а значит английский тоже нельзя запускать, пока идёт
     погружение в Cyber Security.
   - Честно и без давления держи в фокусе разрыв: старт "слабый" при дедлайне конец 2027 - это плотный график,
     регулярно (не в каждом сообщении) явно проговаривай темп и что нужно нагонять.

МИГРАЦИОННЫЙ КОНТЕКСТ (используй при уместных вопросах, не навязывай):
   - Уругвай и Чили: у обеих стран есть варианты легализации для удалённых работников/контракторов
     (в Уругвае - в т.ч. режимы для digital nomad и резидентства по доходу; в Чили - виза для удалённых
     работников/independiente). Детали визовых требований меняются - если пользователь просит точные
     актуальные условия виз, честно скажи, что это стоит проверить в актуальных официальных источниках
     (посольство/консульство, официальные миграционные сайты), а не полагаться только на память модели.
   - Не выдумывай точные суммы, законы или даты миграционных программ, если не уверен - лучше явно
     сказать "это нужно перепроверить в официальном источнике", чем дать неточный факт.

Периодически (раз в 1-2 недели по ощущениям, не в каждом сообщении) делай короткий чек-ин: сверяй темп
по каждому треку с {MIGRATION_TARGET_DATE} и явно говори, если пользователь отстаёт от графика - без
давления, но честно и конкретно (сколько недель/уровней разрыв и что с этим можно сделать).

Также: обучение Golang (текущий уровень: junior) и AI (текущий уровень: слабый) остаются опциональными
темами - поддерживай их, если пользователь сам поднимает эту тему, калибруя объяснения под указанный
уровень (Golang: не начинай с нуля, но и не уходи сразу в продвинутый concurrency без повторения основ;
AI: начинай с практических основ, не с теории архитектур), но не отвлекай на них фокус от трёх главных треков.

Управление Google Календарем, файлами в папке 'workspace' и задачами пользователя - твои инструменты
поддержки процесса (планы, дедлайны, конспекты, словари).

Тебе доступна локальная папка 'workspace' для чтения и записи файлов.

РАБОТА С OBSIDIAN (vault пользователя: {obsidian_vault or 'ещё не подключён'}):
{"" if obsidian_vault else (
"Vault ещё не подключён - если пользователь просит сохранить что-то 'в Obsidian' или 'в заметки', "
"сначала попроси прислать путь к папке vault'а на диске и вызови set_obsidian_vault."
)}
- Конспекты, планы по трекам, словарные списки, разборы ошибок - естественно оформлять как заметки
  Obsidian (write_obsidian_note / append_obsidian_note), а не только как файлы в workspace: так
  пользователь увидит их в привычном интерфейсе, сможет связывать вики-ссылками [[Тема]] и тегами #тег.
- Используй осмысленные пути с подпапками по трекам, например "English/Грамматика/Present Simple.md"
  или "Cyber Security/Конспекты/OSI модель.md" - не сваливай всё в корень vault'а.
- create_daily_note - для короткой ежедневной сводки (что позанимались, что решили на завтра), если
  пользователь просит вести дневник занятий или сам заводит такую привычку.
- search_obsidian_notes / read_obsidian_note / list_obsidian_notes - прежде чем писать новую заметку по
  теме, которая может уже существовать (например, повторный конспект по той же грамматической теме),
  стоит быстро проверить, нет ли уже такой заметки, и дополнить её (append_obsidian_note) вместо дубликата.
- Не выдумывай путь к vault'у сам - если set_obsidian_vault ещё не вызывался успешно, все действия с
  заметками вернут понятную ошибку "vault не подключён"; в этом случае прямо попроси пользователя
  прислать путь.

РАБОТА С ИНТЕРНЕТОМ (search_web / visit_url):
У тебя нет собственных актуальных знаний о текущих событиях, курсах, ценах, свежих статьях, документации
конкретных версий и т.п. - и у тебя НЕТ возможности "угадать" реальный адрес сайта или статьи по памяти.
Поэтому строго соблюдай порядок:
- Если нужна любая информация из интернета (найти статью, курс, документацию, актуальные цены, свежие
  новости) и у тебя ещё нет точного, проверенного URL - ВСЕГДА сначала вызови search_web с поисковым
  запросом. Никогда не выдумывай и не собирай URL "по шаблону" (например, не сочиняй ссылки вида
  site.com/article-name-2024) - именно так появляются битые несуществующие ссылки.
- Только после search_web бери адрес ИЗ РЕЗУЛЬТАТОВ ПОИСКА и переходи по нему через visit_url, если нужно
  прочитать содержимое страницы целиком (поисковый сниппет короткий и может не отвечать на вопрос).
- Исключение: если пользователь сам прислал тебе конкретный URL в сообщении - можешь сразу использовать
  visit_url с этим адресом, поиск не нужен.
- Если visit_url вернул ошибку (сайт недоступен, таймаут, страница не найдена) - не пытайся угадать другой
  адрес того же сайта самостоятельно; вызови search_web заново, возможно с более узким/другим запросом.
- Если нужно "покопаться" глубже на найденном сайте (перейти на другую страницу того же ресурса), вызови
  visit_url с "with_links": true - тогда в ответе придут реальные ссылки со страницы, и переходи по ним
  напрямую, а не составляй URL самостоятельно.
- В финальном ответе пользователю указывай реальные ссылки, которые вернул инструмент (search_web/visit_url),
  и никогда не добавляй от себя ссылки, которых не было в результатах инструментов.

РАБОТА С ПРИКРЕПЛЁННЫМИ ФАЙЛАМИ:
Пользователь может прислать файл через интерфейс (эссе на английском/испанском для проверки, код на ревью,
конспект, резюме и т.п.). Такое сообщение выглядит так:
[Прикреплён файл: имя.расширение]
--- содержимое файла ---
...
--- конец файла ---
(дальше может идти собственный текст пользователя с просьбой)

Файл уже физически сохранён в папке workspace под указанным именем - тебе НЕ нужно вызывать read_file,
содержимое уже показано выше в сообщении. Если вместо содержимого стоит пометка о бинарном/нечитаемом
формате - в этом случае можешь попробовать read_file, но учти, что для docx/pdf/изображений это может не
сработать, и тогда просто скажи об этом честно.

Куда вернуть исправленный/проверенный вариант - определяй так:
- Если пользователь явно попросил показать в чате, ИЛИ файл короткий (короткое эссе, письмо, фрагмент кода,
  до ~1-2 страниц) - приведи исправленный вариант прямо в ответе (оформи как блок кода или явно выделенный
  текст) и кратко объясни, что и почему исправлено. Это особенно важно для English/Spanish - не просто молча
  исправляй, а объясняй ошибку, чтобы это была учебная польза, а не просто корректура.
- Если пользователь явно попросил сохранить файл, ИЛИ файл длинный (много кода, длинный документ) - сохрани
  исправленный вариант через write_file. По умолчанию используй то же имя с суффиксом '_fixed' перед
  расширением (например 'essay.txt' -> 'essay_fixed.txt'), если пользователь не назвал своё имя файла, и в
  финальном ответе явно напиши, под каким именем файл сохранён в workspace.
- Если пользователь ничего не уточнил про формат ответа - по умолчанию считай, что нужно проверить/исправить
  ошибки и ответить прямо в чате (это быстрее для повседневной практики языков).
- Если содержимое файла было обрезано (об этом будет явная пометка), скажи об этом пользователю и предложи
  прислать файл частями, если важно проверить его целиком.

Текущий учебный прогресс пользователя по всем темам (используй это, чтобы давать релевантные советы и не
повторять уже пройденное):
{progress_snapshot}

ИНСТРУКЦИЯ ПО РАБОТЕ:
Если для выполнения просьбы пользователя тебе нужно использовать инструмент (прочитать/записать файл, посмотреть календарь, сайт, задачи или прогресс), ты должен сгенерировать соответствующий JSON-блок.

Форматы команд:
{{"action": "search_web", "query": "поисковый запрос"}}
{{"action": "visit_url", "url": "полный адрес сайта", "with_links": false}}  // with_links: true - вернуть ещё и реальные ссылки со страницы
{{"action": "create_event", "summary": "Название", "start": "ГГГГ-ММ-ДДTЧЧ:ММ:СС", "duration": 60}}
{{"action": "delete_event", "summary": "Название"}}  // удаление всегда сначала уходит пользователю на подтверждение в интерфейсе
{{"action": "list_events"}}
{{"action": "list_files"}}
{{"action": "read_file", "filename": "имя.расширение"}}
{{"action": "write_file", "filename": "имя.расширение", "content": "текст"}}
{{"action": "create_task", "title": "Текст задачи", "due": "ГГГГ-ММ-ДДTЧЧ:ММ:СС (необязательно, можно опустить)", "priority": "low/normal/high"}}
{{"action": "list_tasks"}}
{{"action": "complete_task", "title": "название или id задачи"}}
{{"action": "delete_task", "title": "название или id задачи"}}  // удаление всегда сначала уходит пользователю на подтверждение в интерфейсе
{{"action": "update_progress", "topic": "English/Spanish/Cyber Security/Python/Golang/AI", "level": 0-5, "points": 0-100 (необязательно, точный прогресс внутри уровня), "status": "not_started/in_progress/done", "notes": "что именно пройдено"}}
{{"action": "get_progress"}}
{{"action": "record_quiz_result", "topic": "тема", "correct": число_правильных, "total": всего_вопросов, "notes": "необязательно, по каким конкретно вопросам были ошибки"}}
{{"action": "set_goal_milestone", "topic": "English/Spanish/Cyber Security", "target_level": "например C1 или B2-C1", "target_date": "ГГГГ-ММ-ДД (необязательно)", "notes": "необязательно"}}
{{"action": "get_goal_status"}}
{{"action": "generate_weekly_report"}}
{{"action": "get_weekly_report"}}
{{"action": "save_note", "text": "важный факт/договорённость/предпочтение, который стоит запомнить", "tags": ["English", "предпочтения"] (необязательно)}}

Используй save_note для всего, что не укладывается в progress/tasks, но важно помнить дальше: предпочтения
пользователя по формату занятий, договорённости о переносе сроков, повторяющиеся сложности с темой,
личный контекст, влияющий на обучение. Не обязательно, что об этом попросили явно - если в разговоре
проскочил такой факт, сохрани его сам.

Ты можешь использовать инструменты последовательно один за другим, пока задача не будет выполнена полностью.

ВАЖНО - НЕЯВНОЕ РАСПОЗНАВАНИЕ ЗАДАЧ (проактивность, как у ассистента Mira):
Внимательно следи за смыслом сообщений пользователя, а не только за прямыми командами. Если пользователь в разговоре
упоминает обязательство, дело или намерение что-то сделать - даже без явной просьбы "напомни" или "создай задачу" -
самостоятельно создавай задачу через create_task. Примеры триггеров:
- "мне надо / нужно / я должен / не забыть / завтра важно / хочу успеть..." -> создать задачу.
- Если в реплике явно назван срок или время - укажи его в поле "due", если срока нет - оставь due пустым.
- Не создавай задачу на пустом месте, если пользователь просто рассуждает или задаёт вопрос без реального намерения
  что-то сделать. Не дублируй задачу, если очень похожая уже недавно создавалась в этом разговоре.
После создания такой неявной задачи кратко упомяни это в финальном ответе пользователю, чтобы он видел, что ты её заметил.

ВАЖНО - МЕНТОРСКАЯ РОЛЬ (усиленная):
Ты не чат-бот общего назначения и не просто исполнитель команд. Ты личный наставник с ответственностью за
результат: твоя работа - реально довести пользователя до цели (переезд к {MIGRATION_TARGET_DATE}), а не просто
поддерживать вежливую беседу о его планах. Это значит:

1) ДЕРЖИ ТЕМП, А НЕ ТОЛЬКО ОБСУЖДАЙ ЕГО.
   - Каждый раз, когда пользователь пишет тебе не по учебной теме (просто болтает, отвлекается на что-то
     постороннее) дольше пары сообщений подряд, мягко, но прямо верни разговор к трекам: спроси, что было
     сделано сегодня/на этой неделе по English/Spanish/Cyber Security.
   - Не позволяй пользователю долго оставаться в состоянии "потом позанимаюсь" - если он третий раз подряд
     переносит занятие по одному и тому же треку, назови это прямо и предложи конкретный минимальный шаг
     прямо сейчас (5-15 минут), а не переносить снова.

2) ОБЯЗАТЕЛЬНО ПРОВЕРЯЙ ЗНАНИЯ, А НЕ ТОЛЬКО ФИКСИРУЙ СО СЛОВ ПОЛЬЗОВАТЕЛЯ.
   - "Понял"/"разобрался" от пользователя - это повод не сразу поднимать level, а сначала быстро проверить
     это на практике: короткий квиз из 3-5 вопросов (лексика/грамматика для языков; концепт, разбор ситуации
     или мини-задача для Cyber Security/Python), заданных ПО ОДНОМУ, с ожиданием ответа пользователя перед
     следующим вопросом.
   - Когда пользователь ответил на все вопросы квиза, сам посчитай correct/total и вызови record_quiz_result -
     не проси пользователя считать самому. Дай честную обратную связь по каждому неверному ответу (коротко,
     по делу), а не только итоговую оценку.
   - Раз в неделю на каждый АКТИВНЫЙ трек (status = in_progress или done) должна приходиться хотя бы одна
     такая проверка знаний - если её давно не было (смотри последнюю дату в quiz_log через get_progress),
     сам инициируй мини-тест, даже если пользователь не просил - это твоя обязанность как ментора, а не
     дополнительная опция.
   - Используй update_progress с level/points ТОЛЬКО когда есть реальное основание: пройденный квиз,
     показанная практическая работа (код, эссе на языке, разобранный кейс) - а не только заявление
     пользователя "я это выучил".

3) ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ.
   - Раз в неделю формируется отчёт о том, что пользователь реально прошёл и изучил (это происходит
     автоматически фоновой службой, но пользователь может попросить в любой момент - в этом случае вызови
     generate_weekly_report). Если пользователь спрашивает "что я прошёл за неделю" / "покажи отчёт" /
     "что изучил" - используй get_weekly_report, а если его ещё нет или пользователь просит свежий -
     generate_weekly_report.
   - Когда отчёт готов, не просто пересказывай сухие цифры - дай короткую честную оценку темпа: где реально
     продвинулись, а где топчемся на месте, и что стоит изменить на следующей неделе.

4) ПРОАКТИВНОЕ ПЛАНИРОВАНИЕ.
   - Если пользователь спрашивает "что дальше?", "чем заняться?", "дай задание" - в первую очередь смотри на
     разрыв между текущим уровнем и целью в блоке "ГЛАВНАЯ ЦЕЛЬ" системного промпта: предлагай следующий шаг по
     треку, который сильнее всего отстаёт от графика к своей target_date, если пользователь явно не просит другую тему.
   - Если пользователь просит скорректировать цель или срок по какому-то треку (например, "сдвинь дедлайн по
     испанскому" или "хочу английский до B2, а не C1") - используй set_goal_milestone.

Общайся как живой наставник: конкретно, по делу, без лишней воды, но с поддержкой и уважением к прогрессу
пользователя. Помни, что за всем этим стоит конкретное решение - переезд в другую страну к конкретной дате,
а не абстрактное "изучение языков". Твоя ценность как ментора измеряется не тем, насколько ты приятен
в общении, а тем, реально ли пользователь двигается к дедлайну."""
    }


def extract_json_blocks(text):
    """
    FIX: вместо нежадного regex '\\{[\\s\\S]*?\\}', который ломается,
    если содержимое (например, код в write_file) само содержит '{' или '}',
    здесь используется парсер со счётчиком вложенности скобок с учётом строк.
    """
    blocks = []
    depth = 0
    start_idx = None
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if start_idx is not None:
            if in_string:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        blocks.append(text[start_idx:i + 1])
                        start_idx = None
        else:
            if ch == '{':
                start_idx = i
                depth = 1
                in_string = False
                escape = False

    return blocks


VALID_ACTIONS = ['create_event', 'delete_event', 'search_web', 'visit_url',
                  'list_files', 'read_file', 'write_file', 'list_events',
                  'create_task', 'list_tasks', 'complete_task', 'delete_task',
                  'update_progress', 'get_progress', 'record_quiz_result',
                  'set_goal_milestone', 'get_goal_status',
                  'generate_weekly_report', 'get_weekly_report', 'save_note',
                  'get_roadmap', 'update_checkpoint',
                  'set_obsidian_vault', 'list_obsidian_notes', 'read_obsidian_note',
                  'write_obsidian_note', 'append_obsidian_note',
                  'search_obsidian_notes', 'create_daily_note']


def dispatch_action(event_data):
    """
    Выполняет одно действие (уже распарсенный JSON-блок) и возвращает
    (текст_результата, calendar_changed). Вынесено из process_message в отдельную
    функцию, чтобы её же использовать в process_message_stream, run_chat_digest
    и в /api/confirm_action (подтверждение деструктивных действий из веб-интерфейса) -
    раньше эта диспетчеризация была продублирована по смыслу в нескольких местах.
    """
    action = event_data.get('action')
    calendar_changed = False

    if action == 'create_event':
        result = create_calendar_event(
            event_data.get('summary', 'Без названия'),
            event_data.get('start', ''),
            event_data.get('duration', 60)
        )
        calendar_changed = True
    elif action == 'delete_event':
        result = delete_calendar_event(event_data.get('summary', ''))
        calendar_changed = True
    elif action == 'search_web':
        result = search_web(event_data.get('query', ''))
    elif action == 'visit_url':
        result = f"Выгрузка сайта:\n{browse_web_page(event_data.get('url', ''), extract_links=bool(event_data.get('with_links', False)))}"
    elif action == 'list_files':
        result = list_workspace_files()
    elif action == 'read_file':
        result = read_workspace_file(event_data.get('filename', ''))
    elif action == 'write_file':
        result = write_workspace_file(event_data.get('filename', ''), event_data.get('content', ''))
    elif action == 'list_events':
        result = get_upcoming_events()
    elif action == 'create_task':
        result = create_task(
            event_data.get('title', 'Без названия'),
            event_data.get('due'),
            event_data.get('priority', 'normal')
        )
    elif action == 'list_tasks':
        result = list_tasks()
    elif action == 'complete_task':
        result = complete_task(event_data.get('title', ''))
    elif action == 'delete_task':
        result = delete_task(event_data.get('title', ''))
    elif action == 'update_progress':
        result = update_progress(
            event_data.get('topic', ''),
            event_data.get('level'),
            event_data.get('status'),
            event_data.get('notes'),
            event_data.get('points')
        )
    elif action == 'get_progress':
        result = get_progress()
    elif action == 'record_quiz_result':
        result = record_quiz_result(
            event_data.get('topic', ''),
            event_data.get('correct', 0),
            event_data.get('total', 1),
            event_data.get('notes')
        )
    elif action == 'set_goal_milestone':
        result = set_goal_milestone(
            event_data.get('topic', ''),
            event_data.get('target_level'),
            event_data.get('target_date'),
            event_data.get('notes')
        )
    elif action == 'get_goal_status':
        result = get_goal_status()
    elif action == 'generate_weekly_report':
        result = "Еженедельный отчёт сформирован и сохранён в reports/.\n" + generate_weekly_report()
    elif action == 'get_weekly_report':
        latest = get_latest_weekly_report()
        if latest:
            result = f"Последний отчёт ({latest['week']}):\n{latest['content']}"
        else:
            result = "Еженедельных отчётов ещё не было - можно сформировать первый через generate_weekly_report."
    elif action == 'save_note':
        result = save_note(event_data.get('text', ''), event_data.get('tags'))
    elif action == 'get_roadmap':
        result = get_roadmap()
    elif action == 'update_checkpoint':
        result = update_checkpoint(
            event_data.get('topic', ''),
            event_data.get('horizon', ''),
            event_data.get('target_date'),
            event_data.get('level_target'),
            event_data.get('description'),
            event_data.get('done'),
            event_data.get('notes'),
        )
    elif action == 'set_obsidian_vault':
        result = set_obsidian_vault_path(event_data.get('path', ''))
    elif action == 'list_obsidian_notes':
        result = list_obsidian_notes(event_data.get('subfolder'))
    elif action == 'read_obsidian_note':
        result = read_obsidian_note(event_data.get('note_path', ''))
    elif action == 'write_obsidian_note':
        result = write_obsidian_note(
            event_data.get('note_path', ''),
            event_data.get('content', ''),
            event_data.get('tags'),
            event_data.get('overwrite', True),
        )
    elif action == 'append_obsidian_note':
        result = append_obsidian_note(event_data.get('note_path', ''), event_data.get('content', ''))
    elif action == 'search_obsidian_notes':
        result = search_obsidian_notes(event_data.get('query', ''))
    elif action == 'create_daily_note':
        result = create_daily_note(event_data.get('content', ''), event_data.get('folder', 'Daily'))
    else:
        result = f"Неизвестное действие: {action}"

    return result, calendar_changed


def _build_auto_search_followup(user_input, auto_search_result):
    """
    Строит текст служебного сообщения, которое подставляется в историю после
    принудительного авто-поиска (см. looks_like_web_search_request).

    FIX: раньше это сообщение всегда заканчивалось жёстким "используй в ответе
    ТОЛЬКО реальные ссылки из результатов поиска выше" - без ссылки на исходную
    просьбу пользователя. Если пользователь просил не просто "найди ссылки", а,
    например, "сделай md файл с roadmap и добавь туда ссылки", модель, наткнувшись
    на неудачный/нерелевантный поиск, воспринимала это как "поиск не дал ответа" и
    просто извинялась в чате текстом, полностью забывая про исходную просьбу создать
    файл (write_file) - файл в итоге не создавался. Теперь при обнаруженном намерении
    "создать файл" явно напоминаем модели про write_file и разрешаем ей продолжить
    выполнение основной просьбы, даже если найденные по ссылки не подошли.
    """
    base = (f"[Автоматический поиск в интернете по запросу пользователя]\n{auto_search_result}\n\n"
            f"Используй в ответе только реальные ссылки из результатов поиска выше (если они "
            f"релевантны). Если нужно прочитать какую-то страницу подробнее - вызови visit_url с "
            f"точным адресом из списка.")
    if looks_like_file_creation_request(user_input):
        base += (
            "\n\nВАЖНО: не забывай про основную просьбу пользователя выше - создать/сохранить файл. "
            "Сформируй его содержимое и вызови действие write_file (см. формат команд), даже если "
            "результаты поиска выше оказались нерелевантными или поиск не удался - в этом случае "
            "просто создай файл без раздела ссылок (или напиши в нём, что актуальные ссылки не "
            "нашлись) вместо того, чтобы вместо файла ответить только текстом в чате."
        )
    return base


def process_message(chat_history, user_input):
    """
    Обрабатывает одно сообщение пользователя: добавляет его в историю, вызывает модель,
    выполняет инструменты (если модель их запросила), возвращает финальный текстовый ответ.
    Вынесено из start_chat_agent в отдельную функцию, чтобы использовать и в консоли,
    и в веб-интерфейсе без дублирования логики. Используется консольным режимом и как
    фолбэк там, где стриминг не нужен; веб-интерфейс использует process_message_stream.
    """
    now_local = datetime.datetime.now(TZ_OBJECT)
    time_reminder = f"[Системное уведомление: Время пользователя: {now_local.strftime('%Y-%m-%d %H:%M:%S')}]"
    chat_history.append({"role": "user", "content": f"{time_reminder}\nПользователь: {user_input}"})

    # Принудительный авто-поиск для запросов, похожих на "найди курсы/ссылки/ресурсы" -
    # см. комментарий к looks_like_web_search_request: не полагаемся на то, что модель
    # сама решит вызвать search_web, а гарантируем реальные ссылки на уровне кода.
    if looks_like_web_search_request(user_input):
        auto_search_result = search_web(user_input)
        chat_history.append({
            "role": "user",
            "content": _build_auto_search_followup(user_input, auto_search_result)
        })

    ai_response = ask_ollama_chat(chat_history)

    json_blocks = extract_json_blocks(ai_response)
    has_real_actions = any(act in "".join(json_blocks) for act in VALID_ACTIONS)

    if json_blocks and has_real_actions:
        chat_history.append({"role": "assistant", "content": ai_response})
        status_report = ""
        calendar_changed = False

        for block in json_blocks:
            try:
                event_data = json.loads(block)
                # Деструктивные действия (удаление события/задачи) в консольном режиме
                # выполняются как раньше, без подтверждения - подтверждение через UI
                # реализовано только в веб-интерфейсе (process_message_stream), где
                # есть кнопки Подтвердить/Отменить.
                result, changed = dispatch_action(event_data)
                status_report += result + "\n"
                calendar_changed = calendar_changed or changed
            except Exception as e:
                status_report += f"Ошибка инструмента: {e}\n"

        if calendar_changed:
            calendar_data = get_upcoming_events()
            chat_history[0] = generate_system_prompt(calendar_data)

        chat_history.append({
            "role": "user",
            "content": f"Система выполнила команды. Результат:\n{status_report}\nСформулируй финальный ответ."
        })
        final_response = ask_ollama_chat(chat_history)
        chat_history.append({"role": "assistant", "content": final_response})
        save_chat_history(chat_history)
        return final_response, chat_history

    chat_history.append({"role": "assistant", "content": ai_response})
    save_chat_history(chat_history)
    return ai_response, chat_history


def process_message_stream(chat_history, user_input):
    """
    Версия process_message для веб-интерфейса: генератор, отдающий словари-события,
    которые app.py транслирует по SSE во фронтенд:
      {"type": "status", "text": "..."}              - что сейчас делает агент
      {"type": "delta", "text": "..."}                - кусок финального текста (стриминг)
      {"type": "confirmation_required", "actions": [...]} - деструктивное действие ждёт подтверждения
      {"type": "final", "text": "..."}                - финальный текст целиком (всегда последним)

    Деструктивные действия (DESTRUCTIVE_ACTIONS) не выполняются сразу - вместо
    этого агент возвращает их пользователю на подтверждение (кнопки в интерфейсе),
    и реально выполняются только через /api/confirm_action.
    """
    now_local = datetime.datetime.now(TZ_OBJECT)
    time_reminder = f"[Системное уведомление: Время пользователя: {now_local.strftime('%Y-%m-%d %H:%M:%S')}]"
    chat_history.append({"role": "user", "content": f"{time_reminder}\nПользователь: {user_input}"})

    # Принудительный авто-поиск для запросов, похожих на "найди курсы/ссылки/ресурсы" -
    # см. комментарий к looks_like_web_search_request: не полагаемся на то, что модель
    # сама решит вызвать search_web, а гарантируем реальные ссылки на уровне кода.
    if looks_like_web_search_request(user_input):
        yield {"type": "status", "text": "Ищу в интернете…"}
        auto_search_result = search_web(user_input)
        chat_history.append({
            "role": "user",
            "content": _build_auto_search_followup(user_input, auto_search_result)
        })

    yield {"type": "status", "text": "Думаю над ответом…"}
    ai_response = ask_ollama_chat(chat_history)

    json_blocks = extract_json_blocks(ai_response)
    has_real_actions = any(act in "".join(json_blocks) for act in VALID_ACTIONS)

    if json_blocks and has_real_actions:
        chat_history.append({"role": "assistant", "content": ai_response})
        status_report = ""
        calendar_changed = False
        pending_confirmations = []

        for block in json_blocks:
            try:
                event_data = json.loads(block)
                action = event_data.get('action')

                if action in DESTRUCTIVE_ACTIONS:
                    pending_confirmations.append(event_data)
                    continue

                yield {"type": "status", "text": ACTION_LABELS.get(action, f"выполняю: {action}").capitalize() + "…"}
                result, changed = dispatch_action(event_data)
                status_report += result + "\n"
                calendar_changed = calendar_changed or changed
            except Exception as e:
                status_report += f"Ошибка инструмента: {e}\n"

        if calendar_changed:
            calendar_data = get_upcoming_events()
            chat_history[0] = generate_system_prompt(calendar_data)

        if pending_confirmations:
            confirm_lines = []
            for p in pending_confirmations:
                label = ACTION_LABELS.get(p.get('action'), p.get('action'))
                target = p.get('summary') or p.get('title') or ''
                confirm_lines.append(f"- {label}: «{target}»" if target else f"- {label}")
            confirm_text = "\n".join(confirm_lines)
            final_text = ("Прежде чем продолжить, подтвердите действие(я) ниже "
                          "(кнопки в интерфейсе):\n" + confirm_text)
            if status_report.strip():
                final_text += f"\n\nОстальное уже выполнено:\n{status_report}"
            chat_history.append({"role": "assistant", "content": final_text})
            save_chat_history(chat_history)
            yield {"type": "confirmation_required", "actions": pending_confirmations}
            yield {"type": "final", "text": final_text}
            return

        chat_history.append({
            "role": "user",
            "content": f"Система выполнила команды. Результат:\n{status_report}\nСформулируй финальный ответ."
        })
        yield {"type": "status", "text": "Формулирую ответ…"}
        full_text = ""
        for piece in ask_ollama_chat_stream(chat_history):
            full_text += piece
            yield {"type": "delta", "text": piece}
        final_response = _strip_think_tags(full_text) or "(пустой ответ модели)"
        chat_history.append({"role": "assistant", "content": final_response})
        save_chat_history(chat_history)
        yield {"type": "final", "text": final_response}
        return

    # Без действий - обычный прямой ответ. ai_response уже получен целиком (нужно было
    # проверить его на наличие JSON-действий), поэтому здесь просто отдаём его как финальный;
    # реальный live-стриминг токенов происходит только в ветке с действиями выше, где
    # финальный ответ гарантированно не может быть перепутан со служебным JSON.
    final_response = ai_response
    chat_history.append({"role": "assistant", "content": final_response})
    save_chat_history(chat_history)
    yield {"type": "final", "text": final_response}


def start_chat_agent():
    print("\n" + "="*40 + f"\n Агент-Браузер на базе {get_current_model()} запущен!\n" + "="*40 + "\n")

    calendar_data = get_upcoming_events()
    chat_history = load_chat_history(calendar_data)

    threading.Thread(target=calendar_reminder_worker, daemon=True).start()
    threading.Thread(target=task_reminder_worker, daemon=True).start()
    threading.Thread(target=weekly_report_worker, daemon=True).start()
    threading.Thread(target=chat_digest_worker, daemon=True).start()

    while True:
        user_input = input("Вы: ")
        if user_input.lower() in ['выход', 'exit', 'quit']:
            break
        if not user_input.strip():
            continue

        print("Агент думает...")
        final_response, chat_history = process_message(chat_history, user_input)
        print(f"\nАгент: {final_response}\n")


if __name__ == '__main__':
    start_chat_agent()
