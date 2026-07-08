"""
Веб-интерфейс для ИИ-ментора Кот.
Запуск: python app.py
Откроется на http://127.0.0.1:5000

Использует mentor_core.py - тот же движок, что и консольная версия
(ai_mentor_fixed.py -> start_chat_agent), поэтому все инструменты
(календарь, задачи, прогресс, файлы, браузер) работают одинаково.
"""
import threading
import datetime
from flask import Flask, render_template, request, jsonify

import mentor_core as core

app = Flask(__name__)

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

    return jsonify({
        "tasks": tasks,
        "events": events,
        "progress": progress,
        "server_time": datetime.datetime.now(core.TZ_OBJECT).strftime("%H:%M"),
    })


@app.route('/api/history')
def api_history():
    """Отдаёт историю чата (без системного промпта) для отрисовки при загрузке страницы."""
    with _state_lock:
        visible = [m for m in _chat_history if m.get("role") in ("user", "assistant")]
    # Убираем служебный префикс с временной меткой из пользовательских сообщений
    cleaned = []
    for m in visible:
        content = m["content"]
        if m["role"] == "user" and "Пользователь: " in content:
            content = content.split("Пользователь: ", 1)[-1]
        # пропускаем служебные сообщения "Система выполнила команды..."
        if content.startswith("Система выполнила команды"):
            continue
        cleaned.append({"role": m["role"], "content": content})
    return jsonify({"messages": cleaned})


if __name__ == '__main__':
    threading.Thread(target=core.calendar_reminder_worker, daemon=True).start()
    threading.Thread(target=core.task_reminder_worker, daemon=True).start()
    print("\n=== Веб-интерфейс ИИ-ментора Кот запущен: http://127.0.0.1:5000 ===\n")
    app.run(host='127.0.0.1', port=5000, threaded=True, debug=False)
