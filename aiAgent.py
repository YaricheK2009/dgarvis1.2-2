import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

config_path = Path(__file__).resolve().with_name("config.py")
config = None
if config_path.is_file():
    spec = importlib.util.spec_from_file_location("bot_config", config_path)
    if spec is None or spec.loader is None:
        sys.exit(f"ОШИБКА: не удалось загрузить файл настроек: {config_path}")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)


def setting(name):
    value = os.environ.get(name)
    if value is None and config is not None:
        value = getattr(config, name, "")
    return value.strip() if isinstance(value, str) else value


BOT_TOKEN = setting("BOT_TOKEN")
GEMINI_API_KEY = setting("GEMINI_API_KEY")

GEMINI_MODEL = "gemini-3.8-flash"
MAX_VOICE_SIZE = 15 * 1024 * 1024

if not isinstance(BOT_TOKEN, str) or not BOT_TOKEN or BOT_TOKEN.startswith("ВСТАВЬ_"):
    sys.exit("ОШИБКА: задай действующий BOT_TOKEN в переменных окружения или config.py.")
if (
    not isinstance(GEMINI_API_KEY, str)
    or not GEMINI_API_KEY
    or GEMINI_API_KEY.startswith("ВСТАВЬ_")
):
    sys.exit("ОШИБКА: задай действующий GEMINI_API_KEY в переменных окружения или config.py.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)


def request_json(url, payload=None, headers=None, timeout=60):
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8"))
            message = details.get("description") or details.get("error", {}).get("message")
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = None
        raise RuntimeError(message or f"HTTP {error.code}") from None
    except URLError as error:
        raise RuntimeError(f"Ошибка сети: {error.reason}") from None


def telegram_call(method, payload):
    result = request_json(f"{TELEGRAM_API}/{method}", payload, timeout=40)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Ошибка Telegram Bot API"))
    return result["result"]


def send_text(chat_id, text):
    text = text or "Не удалось получить текстовый ответ."
    for start in range(0, len(text), 4000):
        telegram_call(
            "sendMessage",
            {"chat_id": chat_id, "text": text[start:start + 4000]},
        )


def gemini_generate(parts):
    payload = {"contents": [{"role": "user", "parts": parts}]}
    result = request_json(
        GEMINI_API,
        payload,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        timeout=120,
    )
    try:
        return "".join(
            part["text"]
            for part in result["candidates"][0]["content"]["parts"]
            if "text" in part
        ).strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Gemini не вернул текстовый ответ.") from None


def download_voice(file_id):
    file_info = telegram_call("getFile", {"file_id": file_id})
    if file_info.get("file_size", 0) > MAX_VOICE_SIZE:
        raise RuntimeError("Голосовое слишком большое (максимум 15 МБ).")

    file_path = file_info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram не вернул путь к голосовому файлу.")

    request = Request(f"{TELEGRAM_FILE_API}/{file_path}")
    try:
        with urlopen(request, timeout=60) as response:
            audio = response.read(MAX_VOICE_SIZE + 1)
    except (HTTPError, URLError):
        raise RuntimeError("Не удалось скачать голосовое из Telegram.") from None

    if len(audio) > MAX_VOICE_SIZE:
        raise RuntimeError("Голосовое слишком большое (максимум 15 МБ).")
    return audio


def answer_text(text):
    prompt = (
        "Ты — мой личный ИИ-секретарь. Отвечай кратко и дружелюбно.\n\n"
        f"Пользователь: {text}"
    )
    return gemini_generate([{"text": prompt}])


def answer_voice(message):
    voice = message["voice"]
    audio = download_voice(voice["file_id"])
    mime_type = voice.get("mime_type") or "audio/ogg"
    transcription = gemini_generate([
        {
            "text": (
                "Распознай речь в этом аудио на русском языке. "
                "Верни только распознанные слова, без пояснений."
            )
        },
        {
            "inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(audio).decode("ascii"),
            }
        },
    ])
    if not transcription:
        raise RuntimeError("Не удалось распознать речь.")
    return transcription, answer_text(transcription)


def process_message(message):
    chat_id = message["chat"]["id"]
    if "voice" in message:
        send_text(chat_id, "🎤 Получил голосовое, распознаю...")
        transcription, reply = answer_voice(message)
        send_text(chat_id, f"🗣 Вы сказали:\n{transcription}")
        send_text(chat_id, f"🤖 Ответ:\n{reply}")
    elif "text" in message:
        if message["text"].startswith("/start"):
            send_text(chat_id, "Привет! Напиши сообщение или отправь голосовое.")
            return
        send_text(chat_id, "✍️ Думаю...")
        send_text(chat_id, answer_text(message["text"]))


def main():
    print("Бот запущен (Gemini, без сторонних библиотек).")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = telegram_call("getUpdates", params)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                try:
                    process_message(message)
                except Exception as error:
                    print(f"Ошибка обработки сообщения: {error}")
                    try:
                        send_text(message["chat"]["id"], f"Ошибка: {error}")
                    except Exception as send_error:
                        print(f"Не удалось отправить сообщение об ошибке: {send_error}")
        except KeyboardInterrupt:
            print("\nБот остановлен.")
            return
        except Exception as error:
            print(f"Ошибка Telegram API: {error}. Повтор через 5 секунд.")
            time.sleep(5)


if __name__ == "__main__":
    main()
