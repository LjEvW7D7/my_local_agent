import datetime
import os.path
import json
import re
import requests
import time
import threading
import ctypes
import uuid
from playwright.sync_api import sync_playwright

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
TIMEZONE_NAME = 'Asia/Yekaterinburg'
TZ_OBJECT = datetime.timezone(datetime.timedelta(hours=5))
MODEL_NAME = 'qwen2.5-coder:14b'
WORKSPACE_DIR = os.path.abspath("./workspace")
DB_FILE = "chat_history.json"
TASKS_FILE = "tasks.json"
PROGRESS_FILE = "progress.json"
LEARNING_TOPICS = ["Python", "Golang", "AI", "Cyber Security"]
# FIX: 14B-модель, особенно без GPU или при первом ("холодном") запуске,
# может отвечать заметно дольше 2 минут. 120с было слишком мало.
OLLAMA_TIMEOUT = 600  # секунд

if not os.path.exists(WORKSPACE_DIR):
    os.makedirs(WORKSPACE_DIR)

# Защита от гонки потоков при работе с token.json, tasks.json и progress.json
_calendar_lock = threading.Lock()
_tasks_lock = threading.Lock()
_progress_lock = threading.Lock()


def send_windows_notification(title, message):
    """Отправляет всплывающее системное уведомление в Windows."""
    t = threading.Thread(
        target=lambda: ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x00),
        daemon=True  # FIX: не блокирует завершение процесса
    )
    t.start()


def calendar_reminder_worker():
    """Фоновая функция для проверки напоминаний. Запускается в отдельном потоке."""
    notified_events = set()
    print("[Система]: Фоновая служба напоминаний успешно запущена.")
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
            print(f"\n[Ошибка фонового напоминания]: {e}")
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
        for t in matched:
            t["status"] = "done"
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
    print("[Система]: Фоновая служба напоминаний по задачам успешно запущена.")
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
            print(f"\n[Ошибка фонового напоминания по задачам]: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Модуль учебного прогресса (усиление менторской роли: агент помнит,
# что уже пройдено по каждой теме, и может опираться на это в подсказках)
# ---------------------------------------------------------------------------

def _default_progress():
    return {topic: {"level": 0, "status": "not_started", "notes": "", "updated_at": None}
            for topic in LEARNING_TOPICS}


def _load_progress():
    with _progress_lock:
        if not os.path.exists(PROGRESS_FILE):
            return _default_progress()
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Гарантируем, что все базовые темы присутствуют, даже если файл старый
            base = _default_progress()
            base.update(data)
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


def update_progress(topic, level=None, status=None, notes=None):
    """
    Обновляет прогресс по теме. level: 0-5 (условная шкала владения материалом),
    status: not_started/in_progress/done, notes: краткая заметка о том, что пройдено.
    Тема сопоставляется нестрого (по вхождению подстроки), чтобы не требовать
    от модели точного совпадения регистра/написания.
    """
    try:
        progress = _load_progress()
        matched_key = None
        for key in progress:
            if key.lower() == str(topic).lower() or key.lower() in str(topic).lower() or str(topic).lower() in key.lower():
                matched_key = key
                break
        if matched_key is None:
            matched_key = topic
            progress[matched_key] = {"level": 0, "status": "not_started", "notes": "", "updated_at": None}

        entry = progress[matched_key]
        if level is not None:
            try:
                entry["level"] = max(0, min(5, int(level)))
            except (TypeError, ValueError):
                pass
        if status in ("not_started", "in_progress", "done"):
            entry["status"] = status
        if notes:
            entry["notes"] = notes
        entry["updated_at"] = datetime.datetime.now(TZ_OBJECT).isoformat()

        _save_progress(progress)
        return f"Прогресс по теме '{matched_key}' обновлён: уровень {entry['level']}/5, статус: {entry['status']}."
    except Exception as e:
        return f"Ошибка при обновлении прогресса: {e}"


def get_progress_data():
    """Структурированный прогресс по темам (для веб-интерфейса)."""
    return _load_progress()


def get_progress():
    progress = _load_progress()
    output = "Учебный прогресс:\n"
    for topic, entry in progress.items():
        bar = "●" * entry.get("level", 0) + "○" * (5 - entry.get("level", 0))
        notes_text = f" — {entry['notes']}" if entry.get("notes") else ""
        output += f"- {topic}: {bar} ({entry.get('status', 'not_started')}){notes_text}\n"
    return output


def get_tasks_data(include_done=False):
    """Структурированный список задач (для веб-интерфейса)."""
    tasks = _load_tasks()
    return [t for t in tasks if include_done or t.get("status") != "done"]


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


def browse_web_page(url):
    """Инструмент: Скрытый браузер для чтения содержимого сайтов."""
    if not url.startswith('http'):
        url = 'https://' + url
    print(f"\n[Система]: Скрытый браузер открывает страницу: {url}...")
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = browser.new_context(viewport={'width': 1920, 'height': 1080}, locale='ru-RU')
            page = context.new_page()
            apply_stealth(page)
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            time.sleep(2)

            text = page.locator("body").inner_text()
            clean_text = " ".join(text.split())
            return clean_text[:4000]
    except Exception as e:
        return f"Не удалось прочитать страницу {url}. Ошибка: {e}"
    finally:
        # FIX: гарантированно закрываем браузер даже при исключении,
        # иначе процессы chromium утекают в память
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


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
    """Разрешает путь внутри WORKSPACE_DIR, отбрасывая попытки выйти за его пределы."""
    safe_path = os.path.abspath(os.path.join(WORKSPACE_DIR, filename))
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


def ask_ollama_chat(messages):
    url = "http://localhost:11434/api/chat"
    data = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        # FIX: keep_alive держит модель в памяти между запросами, чтобы не было
        # повторного "холодного" прогрева весов на каждый вопрос
        "keep_alive": "30m",
        "options": {
            "num_ctx": 8192,
            # FIX: ограничиваем длину генерации, иначе модель может генерировать
            # очень долго на слабом железе
            "num_predict": 800
        }
    }
    try:
        response = requests.post(url, json=data, timeout=OLLAMA_TIMEOUT)
        response.raise_for_status()
        return response.json()['message']['content']
    except requests.exceptions.Timeout:
        return (f"Ошибка подключения к Ollama: превышено время ожидания ({OLLAMA_TIMEOUT}с). "
                f"Проверьте 'ollama ps' - возможно модель работает на CPU и отвечает слишком медленно, "
                f"или сервер Ollama не запущен.")
    except requests.exceptions.ConnectionError:
        return "Ошибка подключения к Ollama: сервер недоступен на localhost:11434. Запущена ли Ollama (`ollama serve`)?"
    except Exception as e:
        return f"Ошибка подключения к Ollama: {e}"


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
            print(f"[Система]: Ошибка чтения файла памяти: {e}")

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
    return {
        "role": "system",
        "content": f"""Ты персональный ИИ-ментор Кот. Сегодня: {current_date}. Часовой пояс: {TIMEZONE_NAME}.
Задачи: обучение Python, Golang, AI, Cyber Security, управление Google Календарем, файлами в папке 'workspace' и задачами пользователя.

Тебе доступна локальная папка 'workspace' для чтения и записи файлов.

Текущий учебный прогресс пользователя (используй это, чтобы давать релевантные советы и не повторять уже пройденное):
{progress_snapshot}

ИНСТРУКЦИЯ ПО РАБОТЕ:
Если для выполнения просьбы пользователя тебе нужно использовать инструмент (прочитать/записать файл, посмотреть календарь, сайт, задачи или прогресс), ты должен сгенерировать соответствующий JSON-блок.

Форматы команд:
{{"action": "visit_url", "url": "полный адрес сайта"}}
{{"action": "create_event", "summary": "Название", "start": "ГГГГ-ММ-ДДTЧЧ:ММ:СС", "duration": 60}}
{{"action": "delete_event", "summary": "Название"}}
{{"action": "list_events"}}
{{"action": "list_files"}}
{{"action": "read_file", "filename": "имя.расширение"}}
{{"action": "write_file", "filename": "имя.расширение", "content": "текст"}}
{{"action": "create_task", "title": "Текст задачи", "due": "ГГГГ-ММ-ДДTЧЧ:ММ:СС (необязательно, можно опустить)", "priority": "low/normal/high"}}
{{"action": "list_tasks"}}
{{"action": "complete_task", "title": "название или id задачи"}}
{{"action": "delete_task", "title": "название или id задачи"}}
{{"action": "update_progress", "topic": "Python/Golang/AI/Cyber Security", "level": 0-5, "status": "not_started/in_progress/done", "notes": "что именно пройдено"}}
{{"action": "get_progress"}}

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

ВАЖНО - МЕНТОРСКАЯ РОЛЬ:
Ты не просто исполнитель команд, а наставник, который ведёт пользователя по темам Python, Golang, AI, Cyber Security.
- Когда пользователь успешно разобрался в теме, решил задачу или явно говорит "понял", "разобрался", "получилось" -
  обнови progress через update_progress (подними level, при необходимости смени status на in_progress/done, кратко
  опиши в notes что именно освоено).
- Если пользователь спрашивает "что дальше?", "чем заняться?", "дай задание" - опирайся на текущий прогресс из
  системного промпта: предлагай следующий шаг по теме с наименьшим level или той, что явно в фокусе последних сообщений.
- Периодически (не в каждом сообщении, а когда это уместно и не навязчиво) можешь предложить короткий вопрос
  на закрепление материала по теме, которую пользователь недавно обсуждал.
- Общайся как живой наставник: конкретно, по делу, без лишней воды, но с поддержкой и уважением к прогрессу пользователя."""
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


def process_message(chat_history, user_input):
    """
    Обрабатывает одно сообщение пользователя: добавляет его в историю, вызывает модель,
    выполняет инструменты (если модель их запросила), возвращает финальный текстовый ответ.
    Вынесено из start_chat_agent в отдельную функцию, чтобы использовать и в консоли,
    и в веб-интерфейсе без дублирования логики.
    """
    now_local = datetime.datetime.now(TZ_OBJECT)
    time_reminder = f"[Системное уведомление: Время пользователя: {now_local.strftime('%Y-%m-%d %H:%M:%S')}]"
    chat_history.append({"role": "user", "content": f"{time_reminder}\nПользователь: {user_input}"})

    ai_response = ask_ollama_chat(chat_history)

    json_blocks = extract_json_blocks(ai_response)
    valid_actions = ['create_event', 'delete_event', 'visit_url',
                      'list_files', 'read_file', 'write_file', 'list_events',
                      'create_task', 'list_tasks', 'complete_task', 'delete_task',
                      'update_progress', 'get_progress']
    has_real_actions = any(act in "".join(json_blocks) for act in valid_actions)

    if json_blocks and has_real_actions:
        chat_history.append({"role": "assistant", "content": ai_response})
        status_report = ""
        calendar_changed = False

        for block in json_blocks:
            try:
                event_data = json.loads(block)
                action = event_data.get('action')

                if action == 'create_event':
                    status_report += create_calendar_event(
                        event_data.get('summary', 'Без названия'),
                        event_data.get('start', ''),
                        event_data.get('duration', 60)
                    ) + "\n"
                    calendar_changed = True
                elif action == 'delete_event':
                    status_report += delete_calendar_event(event_data.get('summary', '')) + "\n"
                    calendar_changed = True
                elif action == 'visit_url':
                    status_report += f"Выгрузка сайта:\n{browse_web_page(event_data.get('url', ''))}\n"
                elif action == 'list_files':
                    status_report += list_workspace_files() + "\n"
                elif action == 'read_file':
                    status_report += read_workspace_file(event_data.get('filename', '')) + "\n"
                elif action == 'write_file':
                    status_report += write_workspace_file(
                        event_data.get('filename', ''), event_data.get('content', '')
                    ) + "\n"
                elif action == 'list_events':
                    status_report += get_upcoming_events() + "\n"
                elif action == 'create_task':
                    status_report += create_task(
                        event_data.get('title', 'Без названия'),
                        event_data.get('due'),
                        event_data.get('priority', 'normal')
                    ) + "\n"
                elif action == 'list_tasks':
                    status_report += list_tasks() + "\n"
                elif action == 'complete_task':
                    status_report += complete_task(event_data.get('title', '')) + "\n"
                elif action == 'delete_task':
                    status_report += delete_task(event_data.get('title', '')) + "\n"
                elif action == 'update_progress':
                    status_report += update_progress(
                        event_data.get('topic', ''),
                        event_data.get('level'),
                        event_data.get('status'),
                        event_data.get('notes')
                    ) + "\n"
                elif action == 'get_progress':
                    status_report += get_progress() + "\n"
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


def start_chat_agent():
    print("\n" + "="*40 + f"\n Агент-Браузер на базе {MODEL_NAME} запущен!\n" + "="*40 + "\n")

    calendar_data = get_upcoming_events()
    chat_history = load_chat_history(calendar_data)

    threading.Thread(target=calendar_reminder_worker, daemon=True).start()
    threading.Thread(target=task_reminder_worker, daemon=True).start()

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
