import datetime
import os.path
import json
import re
import requests
import time
import random
import threading
import ctypes
from playwright_stealth import stealth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

SCOPES = ['https://www.googleapis.com/auth/calendar']
TIMEZONE_NAME = 'Asia/Yekaterinburg'
TZ_OBJECT = datetime.timezone(datetime.timedelta(hours=5))
MODEL_NAME = 'qwen2.5-coder:14b'
WORKSPACE_DIR = os.path.abspath("./workspace")
DB_FILE = "chat_history.json"

if not os.path.exists(WORKSPACE_DIR):
    os.makedirs(WORKSPACE_DIR)


def send_windows_notification(title, message):
    """Отправляет всплывающее системное уведомление в Windows."""
    threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
        0, message, title, 0x40 | 0x00)).start()


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
                calendarId='primary', timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])
            for event in events:
                start_raw = event['start'].get(
                    'dateTime', event['start'].get('date'))
                if not start_raw or 'T' not in start_raw:
                    continue

                event_time = datetime.datetime.fromisoformat(
                    start_raw).astimezone(TZ_OBJECT)
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


def get_calendar_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)


def get_upcoming_events():
    try:
        service = get_calendar_service()
        now = datetime.datetime.now(TZ_OBJECT).isoformat()
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=15, singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        if not events:
            return "Предстоящих событий не найдено."

        # Словари для красивого перевода на русский язык
        months_ru = {
            1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
            7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
        }
        days_ru = {
            0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье"
        }

        output = "Расписание из календаря (АКТУАЛЬНЫЕ ТОЧНЫЕ ДАТЫ):\n"
        for event in events:
            start_raw = event['start'].get(
                'dateTime', event['start'].get('date'))

            # Парсим дату в объект datetime
            dt = datetime.datetime.fromisoformat(
                start_raw.split('+')[0].split('Z')[0])

            # Форматируем в абсолютно однозначный текст для ИИ
            date_readable = f"{dt.day} {months_ru[dt.month]} {dt.year} года ({days_ru[dt.weekday()]})"
            time_readable = dt.strftime("%H:%M")

            output += f"- {event['summary']} (Дата: {date_readable}, Время: {time_readable})\n"

        return output
    except Exception as e:
        return f"Не удалось загрузить календарь: {e}"


def create_calendar_event(summary, start_time, duration_minutes=60):
    try:
        service = get_calendar_service()
        start = datetime.datetime.fromisoformat(
            start_time).replace(tzinfo=TZ_OBJECT)
        end = start + datetime.timedelta(minutes=int(duration_minutes))
        event = {
            'summary': summary,
            'start': {'dateTime': start.isoformat(), 'timeZone': TZ_OBJECT},
            'end': {'dateTime': end.isoformat(), 'timeZone': TZ_OBJECT},
        }
        service.events().insert(calendarId='primary', body=event).execute()
        return f"Успешно создано событие: '{summary}' на {start.strftime('%Y-%m-%d %H:%M')}."
    except Exception as e:
        return f"Ошибка при создании события: {e}"


def delete_calendar_event(summary_to_delete):
    try:
        service = get_calendar_service()
        now = datetime.datetime.now(TZ_OBJECT).isoformat()
        events_result = service.events().list(calendarId='primary', timeMin=now,
                                              maxResults=100, singleEvents=True).execute()
        events = events_result.get('items', [])
        deleted_count = 0
        for event in events:
            if summary_to_delete.lower() in event.get('summary', '').lower():
                service.events().delete(calendarId='primary',
                                        eventId=event['id']).execute()
                deleted_count += 1
        if deleted_count > 0:
            return f"Успешно удалено событий '{summary_to_delete}': {deleted_count} шт."
        return f"Событие '{summary_to_delete}' не найдено."
    except Exception as e:
        return f"Ошибка при удаления: {e}"


def browse_web_page(url):
    """Инструмент: Скрытый браузер для чтения содержимого сайтов."""
    try:
        if not url.startswith('http'):
            url = 'https://' + url
        print(f"\n[Система]: Скрытый браузер открывает страницу: {url}...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                                        "--disable-blink-features=AutomationControlled", "--no-sandbox"])
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080}, locale='ru-RU')
            page = context.new_page()
            stealth(page)
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            time.sleep(2)

            # Забираем чистый текст напрямую из body без использования bs4
            text = page.locator("body").inner_text()
            browser.close()

            clean_text = " ".join(text.split())
            return clean_text[:4000]
    except Exception as e:
        return f"Не удалось прочитать страницу {url}. Ошибка: {e}"


def list_workspace_files():
    """Инструмент: Просмотр списка файлов."""
    try:
        files = os.listdir(WORKSPACE_DIR)
        if not files:
            return "Папка 'workspace' пуста."
        return "Файлы в 'workspace':\n" + "\n".join([f"- {f}" for f in files])
    except Exception as e:
        return f"Ошибка чтения папки: {e}"


def read_workspace_file(filename):
    """Инструмент: Чтение файлов."""
    try:
        safe_path = os.path.abspath(os.path.join(WORKSPACE_DIR, filename))
        if not safe_path.startswith(WORKSPACE_DIR):
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
        safe_path = os.path.abspath(os.path.join(WORKSPACE_DIR, filename))
        if not safe_path.startswith(WORKSPACE_DIR):
            return "Ошибка безопасности: Доступ ограничен."
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Файл '{filename}' успешно сохранен/обновлен в папке workspace."
    except Exception as e:
        return f"Ошибка записи: {e}"


def ask_ollama_chat(messages):
    url = "http://localhost:11434/api/chat"
    # Увеличиваем контекст до 8192 токенов, чтобы модель не забывала системный промпт
    data = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        "options": {
            "num_ctx": 8192
        }
    }
    try:
        response = requests.post(url, json=data)
        return response.json()['message']['content']
    except Exception as e:
        return f"Ошибка подключения к Ollama: {e}"


def load_chat_history(calendar_data):
    """Загружает историю чата и обновляет системный промпт на актуальный."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
                if isinstance(history, list) and len(history) > 0:
                    # Обновляем СТРОГО первый элемент списка свежим календарем
                    history[0] = generate_system_prompt(calendar_data)
                    return history
        except Exception as e:
            print(f"[Система]: Ошибка чтения файла памяти: {e}")

    # Если файла нет или он сломан, создаем новый список с промптом
    return [generate_system_prompt(calendar_data)]


def save_chat_history(history):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def generate_system_prompt(calendar_data):
    current_date = datetime.date.today().isoformat()
    return {
        "role": "system",
        "content": f"""Ты персональный ИИ-ментор Кот. Сегодня: {current_date}. Часовой пояс: {TIMEZONE_NAME}.
Задачи: обучение Python, Golang, AI, Cyber Security, управление Google Календарем и файлами в папке 'workspace'.

Тебе доступна локальная папка 'workspace' для чтения и записи файлов.

ИНСТРУКЦИЯ ПО РАБОТЕ:
Если для выполнения просьбы пользователя тебе нужно использовать инструмент (прочитать/записать файл, посмотреть календарь или сайт), ты должен сгенерировать соответствующий JSON-блок. 

Форматы команд:
{{"action": "visit_url", "url": "полный адрес сайта"}}
{{"action": "create_event", "summary": "Название", "start": "ГГГГ-ММ-ДДTЧЧ:ММ:СС", "duration": 60}}
{{"action": "delete_event", "summary": "Название"}}
{{"action": "list_events"}}
{{"action": "list_files"}}
{{"action": "read_file", "filename": "имя.расширение"}}
{{"action": "write_file", "filename": "имя.расширение", "content": "текст"}}

Ты можешь использовать инструменты последовательно один за другим, пока задача не будет выполнена полностью."""
    }


def start_chat_agent():
    print("\n" + "="*40 +
          f"\n Агент-Браузер на базе {MODEL_NAME} запущен!\n" + "="*40 + "\n")

    calendar_data = get_upcoming_events()
    chat_history = load_chat_history(calendar_data)

    # Запуск фонового потока напоминаний
    threading.Thread(target=calendar_reminder_worker, daemon=True).start()

    while True:
        user_input = input("Вы: ")
        if user_input.lower() in ['выход', 'exit', 'quit']:
            break
        if not user_input.strip():
            continue

        now_local = datetime.datetime.now(TZ_OBJECT)
        time_reminder = f"[Системное уведомление: Время пользователя: {now_local.strftime('%Y-%m-%d %H:%M:%S')}]"
        chat_history.append(
            {"role": "user", "content": f"{time_reminder}\nПользователь: {user_input}"})

        print("Агент думает...")
        ai_response = ask_ollama_chat(chat_history)

        json_blocks = re.findall(r'\{[\s\S]*?\}', ai_response)
        valid_actions = ['create_event', 'delete_event', 'visit_url',
                         'list_files', 'read_file', 'write_file', 'list_events']
        # Проверяем, есть ли хоть один валидный экшен в найденных структурах
        has_real_actions = any(act in "".join(json_blocks)
                               for act in valid_actions)

        if json_blocks and has_real_actions:
            chat_history.append({"role": "assistant", "content": ai_response})
            status_report = ""
            calendar_changed = False

            # 1. Цикл строго выполняет ВСЕ инструменты
            for block in json_blocks:
                try:
                    clean_block = block.strip()
                    # Исправляем проблему с неотэкранированными переносами строк внутри JSON контента
                    clean_block = re.sub(r'\n', '\\n', clean_block)
                    # Если модель забыла отэкранировать табы
                    clean_block = re.sub(r'\t', '\\t', clean_block)

                    event_data = json.loads(clean_block)
                    action = event_data.get('action')

                    if action == 'create_event':
                        status_report += create_calendar_event(event_data.get(
                            'summary', 'Без названия'), event_data.get('start', ''), event_data.get('duration', 60)) + "\n"
                        calendar_changed = True
                    elif action == 'delete_event':
                        status_report += delete_calendar_event(
                            event_data.get('summary', '')) + "\n"
                        calendar_changed = True
                    elif action == 'visit_url':
                        status_report += f"Выгрузка сайта:\n{browse_web_page(event_data.get('url', ''))}\n"
                    elif action == 'list_files':
                        status_report += list_workspace_files() + "\n"
                    elif action == 'read_file':
                        status_report += read_workspace_file(
                            event_data.get('filename', '')) + "\n"
                    elif action == 'write_file':
                        status_report += write_workspace_file(event_data.get(
                            'filename', ''), event_data.get('content', '')) + "\n"
                    elif action == 'list_events':
                        status_report += get_upcoming_events() + "\n"
                except Exception as e:
                    status_report += f"Ошибка инструмента: {e}\n"

            # 2. ПОСЛЕ цикла обновляем промпт, если календарь изменился
            if calendar_changed:
                calendar_data = get_upcoming_events()
                # Правильно: обновляем только нулевой элемент списка!
                chat_history[0] = generate_system_prompt(calendar_data)

            # 3. ПОСЛЕ цикла запрашиваем финальный ответ модели (на уровне блока IF)
            chat_history.append({
                "role": "user",
                "content": f"Система выполнила команды. Результат:\n{status_report}\nСформулируй финальный ответ."
            })
            print("Агент анализирует выгрузку...")
            final_response = ask_ollama_chat(chat_history)
            print(f"\nАгент: {final_response}\n")
            chat_history.append(
                {"role": "assistant", "content": final_response})

        else:
            # Обычный текстовый ответ без вызова JSON
            print(f"\nАгент: {ai_response}\n")
            chat_history.append({"role": "assistant", "content": ai_response})

        save_chat_history(chat_history)


if __name__ == '__main__':
    start_chat_agent()
