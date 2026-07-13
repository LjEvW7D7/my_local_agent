"""
Веб-интерфейс для ИИ-ментора Кот.
Запуск: python app.py
Откроется на http://127.0.0.1:5000

Использует mentor_core.py - тот же движок, что и консольная версия
(ai_mentor_fixed.py -> start_chat_agent), поэтому все инструменты
(календарь, задачи, прогресс, файлы, браузер) работают одинаково.
"""
import os
import json
import threading
import datetime
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

import mentor_core as core

app = Flask(__name__)
# Ограничение на размер загружаемого файла (5 МБ) - контекст модели (num_ctx=8192)
# всё равно не вместит больше пары тысяч слов, но это защита от случайной загрузки
# огромного файла, который просто зависнет на сохранении/чтении.
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

# Расширения, которые безопасно читать как текст и сразу показывать модели.
# Остальные (docx, pdf, изображения и т.п.) просто сохраняются в workspace -
# при необходимости модель может попытаться прочитать их через read_file.
TEXT_FILE_EXTENSIONS = {
    '.txt', '.md', '.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.htm', '.css',
    '.json', '.csv', '.tsv', '.java', '.go', '.c', '.cpp', '.h', '.hpp', '.sh',
    '.yaml', '.yml', '.xml', '.log', '.ini', '.cfg', '.rs', '.rb', '.php', '.sql'
}
# Сколько символов содержимого файла вшивать прямо в сообщение модели.
# Системный промпт и так уже большой (цель, прогресс), поэтому лимит скромный,
# чтобы не выесть весь контекст (num_ctx=8192) на одно сообщение.
UPLOAD_PREVIEW_CHARS = 6000

# Общая история чата и лок для потокобезопасности (Flask dev server может
# обрабатывать несколько запросов параллельно, а chat_history - общее состояние)
_state_lock = threading.Lock()
_calendar_data = core.get_upcoming_events()
_chat_history = core.load_chat_history(_calendar_data)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """
    Нестриминговый эндпоинт чата - оставлен как простой фолбэк/для совместимости
    (например, если клиент не умеет читать SSE). Основной путь фронтенда - /api/chat_stream.
    """
    global _chat_history
    payload = request.get_json(silent=True) or {}
    user_input = (payload.get('message') or '').strip()
    if not user_input:
        return jsonify({"error": "Пустое сообщение"}), 400

    with _state_lock:
        try:
            final_response, _chat_history = core.process_message(_chat_history, user_input)
        except Exception as e:
            return jsonify({"error": f"Ошибка агента: {e}"}), 500

    return jsonify({"reply": final_response})


@app.route('/api/chat_stream', methods=['POST'])
def api_chat_stream():
    """
    Стриминговый чат по Server-Sent Events. Отдаёт события по мере готовности:
    status (что сейчас делает агент), delta (кусок финального текста),
    confirmation_required (деструктивное действие ждёт подтверждения), final (весь текст).
    Каждое событие - отдельная строка вида 'data: {...}\\n\\n', как того требует SSE.
    """
    global _chat_history
    payload = request.get_json(silent=True) or {}
    user_input = (payload.get('message') or '').strip()
    if not user_input:
        return jsonify({"error": "Пустое сообщение"}), 400

    def generate():
        # Лок держим на весь стрим - как и раньше для /api/chat, чат обрабатывает
        # запросы последовательно (chat_history - общее изменяемое состояние).
        with _state_lock:
            try:
                for event in core.process_message_stream(_chat_history, user_input):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                err = {"type": "final", "text": f"Ошибка агента: {e}"}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                     headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/confirm_action', methods=['POST'])
def api_confirm_action():
    """
    Подтверждение (или отмена) деструктивного действия (удаление события/задачи),
    которое process_message_stream отложил вместо немедленного выполнения.
    Тело запроса: {"action": {...исходный JSON-блок действия...}, "confirmed": true/false}
    При confirmed=true выполняет действие напрямую (без повторного похода к модели за
    решением - подтверждение уже получено от пользователя) и, если действие затронуло
    календарь, обновляет системный промпт с актуальным расписанием.
    """
    global _chat_history
    payload = request.get_json(silent=True) or {}
    action_data = payload.get('action')
    confirmed = bool(payload.get('confirmed'))

    if not isinstance(action_data, dict) or not action_data.get('action'):
        return jsonify({"error": "Некорректные данные действия"}), 400

    if not confirmed:
        return jsonify({"result": "Действие отменено пользователем.", "executed": False})

    with _state_lock:
        try:
            result, calendar_changed = core.dispatch_action(action_data)
            if calendar_changed:
                calendar_data = core.get_upcoming_events()
                _chat_history[0] = core.generate_system_prompt(calendar_data)
            _chat_history.append({
                "role": "assistant",
                "content": f"[Пользователь подтвердил действие через интерфейс]\n{result}"
            })
            core.save_chat_history(_chat_history)
        except Exception as e:
            return jsonify({"error": f"Не удалось выполнить действие: {e}"}), 500

    return jsonify({"result": result, "executed": True})


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """
    Принимает файл (multipart/form-data, поле 'file'), сохраняет его в папку
    workspace (ту же, что использует core.write_workspace_file/read_workspace_file,
    чтобы модель могла работать с ним обычными инструментами) и, если формат
    текстовый, возвращает превью содержимого - фронтенд вшивает его прямо
    в сообщение чата, чтобы не тратить лишний ход на read_file.
    """
    if 'file' not in request.files:
        return jsonify({"error": "Файл не найден в запросе"}), 400
    uploaded = request.files['file']
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Пустое имя файла"}), 400

    filename = secure_filename(uploaded.filename)
    if not filename:
        return jsonify({"error": "Недопустимое имя файла"}), 400

    # Не затираем случайно существующий файл с тем же именем в workspace -
    # добавляем числовой суффикс при совпадении.
    base, ext = os.path.splitext(filename)
    dest_path = os.path.join(core.WORKSPACE_DIR, filename)
    counter = 1
    while os.path.exists(dest_path):
        filename = f"{base}_{counter}{ext}"
        dest_path = os.path.join(core.WORKSPACE_DIR, filename)
        counter += 1

    try:
        uploaded.save(dest_path)
    except Exception as e:
        return jsonify({"error": f"Не удалось сохранить файл: {e}"}), 500

    preview = None
    truncated = False
    ext_lower = os.path.splitext(filename)[1].lower()
    if ext_lower in TEXT_FILE_EXTENSIONS or ext_lower == '':
        try:
            with open(dest_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read(UPLOAD_PREVIEW_CHARS + 1)
            truncated = len(content) > UPLOAD_PREVIEW_CHARS
            preview = content[:UPLOAD_PREVIEW_CHARS]
        except Exception:
            preview = None

    return jsonify({"filename": filename, "preview": preview, "truncated": truncated})


@app.route('/api/models')
def api_models():
    """Список локальных моделей Ollama + текущая активная - для меню выбора в сайдбаре."""
    try:
        models = core.list_local_models()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"models": models, "current": core.get_current_model()})


@app.route('/api/models/select', methods=['POST'])
def api_models_select():
    """Переключает модель, которую использует ask_ollama_chat, на лету (без рестарта)."""
    payload = request.get_json(silent=True) or {}
    model_name = (payload.get('model') or '').strip()
    if not model_name:
        return jsonify({"error": "Не указано имя модели"}), 400
    with _state_lock:
        try:
            current = core.set_current_model(model_name)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    return jsonify({"current": current})


@app.route('/api/state')
def api_state():
    """Данные для боковой панели: задачи, ближайшие события, учебный прогресс."""
    try:
        tasks = core.get_tasks_data()
    except Exception:
        tasks = []
    try:
        events = core.get_upcoming_events_data(max_results=6)
    except Exception:
        events = []
    try:
        progress = core.get_progress_data()
    except Exception:
        progress = {}
    try:
        goal = core.get_goal_data()
    except Exception:
        goal = {}
    try:
        roadmap = core.get_roadmap_data()
    except Exception:
        roadmap = []
    try:
        obsidian_vault = core.get_obsidian_vault_path()
    except Exception:
        obsidian_vault = None

    return jsonify({
        "tasks": tasks,
        "events": events,
        "progress": progress,
        "goal": goal,
        "roadmap": roadmap,
        "obsidian_vault": obsidian_vault,
        "server_time": datetime.datetime.now(core.TZ_OBJECT).strftime("%H:%M"),
    })


@app.route('/api/weekly_report')
def api_weekly_report():
    """Отдаёт последний сформированный еженедельный отчёт (если он уже есть)."""
    try:
        report = core.get_latest_weekly_report()
    except Exception as e:
        return jsonify({"error": f"Не удалось получить отчёт: {e}"}), 500
    return jsonify({"report": report})


@app.route('/api/weekly_report/generate', methods=['POST'])
def api_weekly_report_generate():
    """Формирует свежий еженедельный отчёт по запросу пользователя (кнопка в сайдбаре)."""
    with _state_lock:
        try:
            content = core.generate_weekly_report()
        except Exception as e:
            return jsonify({"error": f"Не удалось сформировать отчёт: {e}"}), 500
    report = core.get_latest_weekly_report()
    return jsonify({"report": report or {"content": content}})


@app.route('/api/memory_notes')
def api_memory_notes():
    """Последние заметки из memory_notes.json (для отладки/сайдбара, если понадобится)."""
    try:
        notes = core.get_memory_notes(limit=50)
    except Exception as e:
        return jsonify({"error": f"Не удалось получить заметки: {e}"}), 500
    return jsonify({"notes": notes})


@app.route('/api/digest/run', methods=['POST'])
def api_digest_run():
    """Ручной запуск дайджеста чата (не дожидаясь фоновой службы) - удобно для отладки."""
    with _state_lock:
        try:
            result = core.run_chat_digest(force=True)
        except Exception as e:
            return jsonify({"error": f"Не удалось выполнить дайджест: {e}"}), 500
    return jsonify({"result": result or "Новых сообщений для дайджеста не найдено."})


@app.route('/api/history')
def api_history():
    """
    Отдаёт историю чата (без системного промпта) для отрисовки при загрузке страницы.
    Каждое сообщение теперь несёт "id" - это его индекс в исходном _chat_history
    (а не в отфильтрованном списке), чтобы /api/history/delete могло удалить именно
    этот элемент, не путаясь в пропущенных служебных записях.
    """
    with _state_lock:
        indexed = [(i, m) for i, m in enumerate(_chat_history) if m.get("role") in ("user", "assistant")]
    cleaned = []
    for i, m in indexed:
        content = m["content"]
        if m["role"] == "user" and "Пользователь: " in content:
            content = content.split("Пользователь: ", 1)[-1]
        # пропускаем служебные сообщения "Система выполнила команды..."
        if content.startswith("Система выполнила команды"):
            continue
        cleaned.append({"id": i, "role": m["role"], "content": content})
    return jsonify({"messages": cleaned})


@app.route('/api/history/clear', methods=['POST'])
def api_history_clear():
    """Полностью очищает историю чата, оставляя только свежий системный промпт."""
    global _chat_history
    with _state_lock:
        calendar_data = core.get_upcoming_events()
        _chat_history = [core.generate_system_prompt(calendar_data)]
        core.save_chat_history(_chat_history)
    return jsonify({"ok": True})


@app.route('/api/history/delete', methods=['POST'])
def api_history_delete():
    """
    Удаляет одно сообщение по id (индексу в _chat_history, см. /api/history).
    Системный промпт (индекс 0) удалить нельзя.
    """
    global _chat_history
    payload = request.get_json(silent=True) or {}
    msg_id = payload.get('id')
    if not isinstance(msg_id, int) or msg_id <= 0:
        return jsonify({"error": "Некорректный id сообщения"}), 400

    with _state_lock:
        if msg_id >= len(_chat_history):
            return jsonify({"error": "Сообщение не найдено"}), 404
        del _chat_history[msg_id]
        core.save_chat_history(_chat_history)
    return jsonify({"ok": True})


@app.route('/api/notifications')
def api_notifications():
    """
    Отдаёт и очищает накопившиеся уведомления (напоминания о календаре/задачах,
    готовый еженедельный отчёт и т.п.), чтобы фронтенд показал их через
    Web Notifications API - независимо от ОС и того, виден ли терминал процесса.
    """
    try:
        items = core.pop_notifications()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"notifications": items})


@app.route('/api/goals/update', methods=['POST'])
def api_goals_update():
    """
    Позволяет отредактировать веху цели (целевой уровень/дедлайн/заметку) прямо
    из сайдбара, не прося об этом модель в чате. Тело: {"topic", "target_level",
    "target_date", "notes"} - все поля кроме topic необязательны.
    """
    payload = request.get_json(silent=True) or {}
    topic = (payload.get('topic') or '').strip()
    if not topic:
        return jsonify({"error": "Не указана тема"}), 400
    with _state_lock:
        try:
            result = core.set_goal_milestone(
                topic,
                payload.get('target_level'),
                payload.get('target_date'),
                payload.get('notes'),
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"result": result, "goal": core.get_goal_data()})


@app.route('/api/roadmap/checkpoint', methods=['POST'])
def api_roadmap_checkpoint():
    """
    Ручная правка промежуточной цели (чекпоинта) прямо из сайдбара. Тело:
    {"topic", "horizon", "target_date"?, "level_target"?, "description"?,
    "done"?, "notes"?} - horizon один из week/month/quarter/half_year/year.
    """
    payload = request.get_json(silent=True) or {}
    topic = (payload.get('topic') or '').strip()
    horizon = (payload.get('horizon') or '').strip()
    if not topic or not horizon:
        return jsonify({"error": "Не указаны topic и/или horizon"}), 400
    with _state_lock:
        try:
            result = core.update_checkpoint(
                topic, horizon,
                payload.get('target_date'),
                payload.get('level_target'),
                payload.get('description'),
                payload.get('done'),
                payload.get('notes'),
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"result": result, "roadmap": core.get_roadmap_data()})


@app.route('/api/obsidian/vault', methods=['GET'])
def api_obsidian_vault_get():
    """Текущий путь к подключённому Obsidian vault (или null, если не настроен)."""
    try:
        return jsonify({"vault_path": core.get_obsidian_vault_path()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/obsidian/vault', methods=['POST'])
def api_obsidian_vault_set():
    """Подключает Obsidian vault по пути на диске. Тело: {"path": "..."}"""
    payload = request.get_json(silent=True) or {}
    path = (payload.get('path') or '').strip()
    if not path:
        return jsonify({"error": "Не указан путь к vault"}), 400
    with _state_lock:
        try:
            result = core.set_obsidian_vault_path(path)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"result": result, "vault_path": core.get_obsidian_vault_path()})


if __name__ == '__main__':
    threading.Thread(target=core.calendar_reminder_worker, daemon=True).start()
    threading.Thread(target=core.task_reminder_worker, daemon=True).start()
    threading.Thread(target=core.weekly_report_worker, daemon=True).start()
    threading.Thread(target=core.chat_digest_worker, daemon=True).start()
    core.logger.info("Веб-интерфейс ИИ-ментора Кот запущен: http://127.0.0.1:5000")
    app.run(host='127.0.0.1', port=5000, threaded=True, debug=False)
