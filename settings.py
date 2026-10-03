from pathlib import Path
import json
import os
import secrets

BASE_DIR = Path(__file__).resolve().parent

PATHS ={
    "settings_path" : str(BASE_DIR / "settings.json"),
    "voice_profile_path" : str(BASE_DIR / "my_voice_profile.wav"),
    "memory_path" : str(BASE_DIR / "memory.txt"),
    "response_path" : str(BASE_DIR / "response.wav"),
    "temp_wav_file" : str(BASE_DIR / "temp.wav")
    }

default_settings = {"model": "", 
                    "provider": "ollama",
                    "zen_api_key": "",
                    "zen_model": "gpt-5.4-mini",
                    "ai_base_url": "",
                    "ai_api_key": "",
                    "ai_model": "",
                    "ai_timeout": 120,
                    "ai_providers": [],
                    "ai_active": "",
                    "web_token": "",
                    "confirm_dangerous": True,
                    "whispermodel":"", 
                    "micro_index": 1, 
                    "tts_voice": "xenia", 
                    "tts_device": "auto",
                    "trigger_word":"луч",
                    "speed_ai_speak": 1.0,
                    "stt_mode":"google"
                    }

# True, если при последнем get_settings_from_file() был сгенерирован новый web_token.
# Нужно, чтобы вызывающий код мог один раз показать токен пользователю.
web_token_generated = False


def _write_settings_file(path, data):
    """Атомарно записать настройки и закрыть файл правами 0o600.

    Токен веб-доступа лежит в settings.json, поэтому временный файл тоже сразу
    получает права владельца — иначе он на миг был бы доступен всем.
    """
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_settings_from_file():
    global web_token_generated
    web_token_generated = False
    path = Path(PATHS["settings_path"])
    if path.is_file():
        loaded = {}
        try:
            with open(path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            if not isinstance(loaded, dict):
                raise ValueError("ожидался объект JSON")
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            # Файл действительно повреждён (не парсится или это не объект JSON) —
            # только тогда откладываем его как .broken. OSError/PermissionError сюда
            # не попадают: при временной недоступности файл трогать нельзя, иначе
            # здоровые настройки теряются из-за разового сбоя или гонки чтения.
            print(f"Ошибка чтения настроек ({path}): {e}")
            backup = path.with_suffix(".json.broken")
            try:
                path.replace(backup)
                print(f"Повреждённый файл сохранён как {backup.name}, создаю новый")
            except Exception:
                pass
            loaded = {}
        except OSError as e:
            # Временная ошибка доступа/чтения: файл НЕ переименовываем, просто
            # возвращаем значения по умолчанию.
            print(f"Настройки временно недоступны ({path}): {e}")
            return dict(default_settings)
        merged = dict(default_settings)
        merged.update(loaded)
    else:
        merged = dict(default_settings)

    # Пароль веб-API обязан существовать: пустой токен = открытый доступ к API.
    # Генерируем и сразу сохраняем (модульного settings здесь ещё нет),
    # вызывающий код узнаёт о генерации по web_token_generated.
    if not merged.get("web_token"):
        merged["web_token"] = secrets.token_urlsafe(24)
        web_token_generated = True
        try:
            _write_settings_file(path, merged)
        except OSError as e:
            print(f"Ошибка сохранения web_token: {e}")
    return merged


def save_settings():
    path = Path(PATHS["settings_path"])
    tmp = path.with_suffix(".json.tmp")
    try:
        _write_settings_file(path, settings)
        return True
    except Exception as e:
        print(f"Ошибка сохранения настроек: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


settings = get_settings_from_file()

stop_speak_flags = [
            "стоп", "стоп мне неприятно", "стоп хватит", "остановись", "остановить", "останови", "останови речь", "останови ответ", "останови озвучку", "останови разговор",
            "замолчи", "замолчи пожалуйста", "замолкни", "молчи", "помолчи", "тихо", "тише", "можно тише", "будь тише", "заткнись",
            "заткнись пожалуйста", "заткнись уже", "закрой рот", "хватит", "хватит говорить", "хватит болтать", "хватит уже", "достаточно", "довольно", "всё хватит",
            "все хватит", "перестань", "перестань говорить", "перестань болтать", "перестань отвечать", "прекрати", "прекрати говорить", "прекрати ответ", "прекрати озвучку", "прекрати болтать",
            "конец", "закончи", "заканчивай", "заверши", "отмена", "отбой", "не надо", "не продолжай", "не отвечай", "не говори",
            "не нужно", "достаточно спасибо", "всё", "все", "стоп ответ", "остановка", "пауза", "сделай паузу", "поставь на паузу", "замри",
            "хорош уже", "хорош", "угомонись", "успокойся", "тихо тихо", "замолчи уже", "хватит уже говорить", "прекрати уже", "всё понятно", "все понятно",
            "я понял", "я понял спасибо", "понятно", "ясно", "ясно спасибо", "спасибо хватит", "спасибо достаточно", "можешь замолчать", "можешь помолчать", "можешь остановиться",
            "можешь прекратить", "можно остановиться", "можно прекратить", "остановись пожалуйста", "прекрати пожалуйста", "замолчи на секунду", "помолчи немного", "помолчи секунду", "тише пожалуйста", "затихни",
            "прервись", "прерви ответ", "прерви озвучку", "выключи голос", "выключи озвучку", "отключи голос", "отключи озвучку","ебало офни"
        ]