from pathlib import Path
import json

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
                    "whispermodel":"", 
                    "micro_index": 1, 
                    "tts_voice": "xenia", 
                    "tts_device": "auto",
                    "trigger_word":"",
                    "speed_ai_speak": 1.0,
                    "stt_mode":"google"
                    }

def get_settings_from_file():
    path = Path(PATHS["settings_path"])
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            if not isinstance(loaded, dict):
                raise ValueError("ожидался объект JSON")
        except Exception as e:
            print(f"Ошибка чтения настроек ({path}): {e}")
            backup = path.with_suffix(".json.broken")
            try:
                path.replace(backup)
                print(f"Повреждённый файл сохранён как {backup.name}, создаю новый")
            except Exception:
                pass
            loaded = {}
        merged = dict(default_settings)
        merged.update(loaded)
        return merged
    else:
        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(default_settings, file, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Ошибка создания настроек: {e}")
        return dict(default_settings)

def save_settings():
    path = Path(PATHS["settings_path"])
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(settings, file, ensure_ascii=False, indent=4)
        tmp.replace(path)
    except Exception as e:
        print(f"Ошибка сохранения настроек: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

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