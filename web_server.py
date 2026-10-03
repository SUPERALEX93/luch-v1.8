import os
import json
import hmac
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import re
from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Body
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi import Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
import uvicorn
import settings
import work_fuctions
import device_control

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = BASE_DIR / "index.html"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def app_build_version() -> str:
    """Версия приложения из build.gradle.kts — страница сообщает её мосту."""
    try:
        txt = (BASE_DIR / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
        m = re.search(r'versionName\s*=\s*"([^"]+)"', txt)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


class NoCacheStatic(StaticFiles):
    """Страница всегда свежая: старый app.js в кэше WebView выглядел как
    «ничего не изменилось»."""

    NO_CACHE = "no-store, no-cache, must-revalidate, max-age=0"

    def file_response(self, full_path, stat_result, scope, status_code=200):
        hdr = {"Cache-Control": self.NO_CACHE, "Pragma": "no-cache"}
        if str(full_path).endswith("app.js"):
            try:
                text = Path(full_path).read_text(encoding="utf-8")
                text = text.replace("__BUILD__", app_build_version())
                return Response(text, media_type="application/javascript", headers=hdr)
            except Exception:
                pass
        resp = super().file_response(full_path, stat_result, scope, status_code)
        resp.headers["Cache-Control"] = self.NO_CACHE
        resp.headers["Pragma"] = "no-cache"
        return resp


app.mount("/static", NoCacheStatic(directory=str(STATIC_DIR), check_dir=False), name="static")

ai_instance = None
server_instance = None
_server_thread = None
_server_port = 1337

class QueryModel(BaseModel):
    text: str

class CommandModel(BaseModel):
    cmd: str   # полная команда, начинающаяся с '/'

class LocationModel(BaseModel):
    lat: float
    lng: float
    accuracy: float | None = None
    source: str = "web"

class RouteModel(BaseModel):
    destination: str   # описание цели маршрута (адрес/фраза)

class AIConfigModel(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float | None = None
    activate: bool = True
    name: str = ""
    provider_id: str = ""
    keep_key: bool = True

class AIProviderSwitchModel(BaseModel):
    provider: str = ""

class AIZenKeyModel(BaseModel):
    api_key: str = ""
    model: str = ""

class AIOllamaModelModel(BaseModel):
    model: str = ""

class AICommandRunModel(BaseModel):
    command: str = ""
    args: dict = {}
    confirm: bool = False
    timeout: int = 0

class AIModelsQuery(BaseModel):
    base_url: str = ""
    api_key: str = ""

class AIProbeModel(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""

class SettingModel(BaseModel):
    key: str
    value: str

class RunCommandModel(BaseModel):
    name: str
    confirm: bool = False


def _require_web_token(token: str | None):
    """Защита изменяющих запросов, если в настройках задан web_token.

    Пустой токен = проверки нет (обратная совместимость).
    """
    expected = (settings.settings.get("web_token") or "").strip()
    if not expected:
        return
    if not expected.isascii():
        raise HTTPException(status_code=500,
                            detail="web_token содержит нелатинские символы — такие нельзя передать "
                                   "в HTTP-заголовке. Замени его на латиницу.")
    if not token or not hmac.compare_digest(str(token), expected):
        raise HTTPException(status_code=401, detail="Нужен верный web_token (заголовок X-Luch-Token)")


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

@app.get("/")
def index():
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="Файл index.html не найден")
    resp = FileResponse(INDEX_FILE)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.post("/api/ask")
def ask_ai(data: QueryModel):
    global ai_instance
    if not ai_instance:
        raise HTTPException(status_code=500, detail="Экземпляр AI не инициализирован")

    seq_before = ai_instance.ai_command_seq()
    ai_instance.response_ready_event.clear()
    ai_instance.user_prompt = data.text

    is_ready = ai_instance.response_ready_event.wait(timeout=60)
    if not is_ready:
        raise HTTPException(status_code=504, detail="Превышено время ожидания ответа ИИ")

    return {
        "status": "ok",
        "response": ai_instance.format_response(ai_instance.response),
        "play_audio": getattr(ai_instance, 'ai_speak', False),
        "commands": ai_instance.ai_commands_since(seq_before),
    }

@app.post("/api/command")
def execute_command(data: CommandModel, x_luch_token: str | None = Header(default=None)):
    global ai_instance
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        result = ai_instance.process_command(data.cmd)
        return {"status": "ok", "output": result}
    except Exception as e:
        return {"status": "error", "output": str(e)}


# =============== Настройка провайдера ИИ ===============

@app.get("/api/ai/config")
def get_ai_config():
    """Текущий провайдер/адрес/модель. API-ключ наружу не отдаётся."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    return ai_instance.ai_config()


@app.get("/api/ai/commands")
def get_ai_commands():
    """Список команд ИИ со схемой аргументов и журнал последних запусков."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    return {"commands": ai_instance.web_ai_commands(),
            "history": ai_instance.web_ai_command_history()}


@app.post("/api/ai/commands/run")
def run_ai_command(data: AICommandRunModel, x_luch_token: str | None = Header(default=None)):
    """Выполнить команду ИИ с сайта и вернуть её вывод."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        return ai_instance.web_run_ai_command(
            data.command, data.args, data.confirm,
            data.timeout or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"не удалось выполнить: {e}")


@app.post("/api/ai/commands/clear")
def clear_ai_command_history(x_luch_token: str | None = Header(default=None)):
    """Очистить журнал выполнения ИИ-команд."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    ai_instance.web_clear_ai_command_history()
    return {"status": "ok"}


@app.post("/api/ai/config")
def set_ai_config(data: AIConfigModel, x_luch_token: str | None = Header(default=None)):
    """Сохранить кастомного провайдера (адрес/ключ/модель) и, если надо, переключиться на него."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    if not data.base_url.strip():
        raise HTTPException(status_code=400, detail="не указан адрес API (base_url)")
    try:
        if data.provider_id.strip() or data.name.strip():
            ai_instance.web_save_custom_provider(
                data.name, data.base_url, api_key=data.api_key, model=data.model,
                timeout=data.timeout, keep_key=data.keep_key and not data.api_key.strip(),
                provider_id=data.provider_id.strip() or None, activate=data.activate)
        else:
            ai_instance.apply_ai_endpoint(
                data.base_url, api_key=data.api_key, model=data.model,
                timeout=data.timeout, activate=data.activate,
                keep_key=not data.api_key.strip())
        return {"status": "ok", "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"не удалось сохранить: {e}")


@app.post("/api/ai/switch")
def switch_ai_provider(data: AIProviderSwitchModel, x_luch_token: str | None = Header(default=None)):
    """Переключиться на ollama, zen или сохранённого кастомного провайдера."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        ai_instance.web_switch_provider(data.provider)
        return {"status": "ok", "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"не удалось переключить: {e}")


@app.post("/api/ai/zen")
def set_ai_zen_key(data: AIZenKeyModel, x_luch_token: str | None = Header(default=None)):
    """Сохранить API-ключ и модель OpenCode Zen."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        ai_instance.web_set_zen_key(data.api_key, data.model or None)
        return {"status": "ok", "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"не удалось сохранить: {e}")


@app.post("/api/ai/ollama")
def set_ai_ollama_model(data: AIOllamaModelModel, x_luch_token: str | None = Header(default=None)):
    """Сохранить модель Ollama с сайта."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        ai_instance.web_set_ollama_model(data.model)
        return {"status": "ok", "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"не удалось сохранить: {e}")


@app.delete("/api/ai/provider")
def delete_ai_provider(provider_id: str, x_luch_token: str | None = Header(default=None)):
    """Удалить кастомного провайдера из списка."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        removed = ai_instance.web_delete_custom_provider(provider_id)
        return {"status": "ok", "removed": removed, "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/ai/models")
def get_ai_models(base_url: str = "", api_key: str = ""):
    """Список моделей с адреса. Не у всех серверов есть /models — тогда впиши имя вручную."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    # пустая api_key = «оставь сохранённую», но только если адрес не меняли
    if not api_key.strip() and not base_url.strip():
        models, err = ai_instance.list_ai_models()
    elif not api_key.strip():
        models, err = ai_instance.list_ai_models(base_url=base_url, api_key=ai_instance.ai_api_key
                                                if base_url.strip() == ai_instance.ai_base_url else "")
    else:
        models, err = ai_instance.list_ai_models(base_url=base_url, api_key=api_key)
    return {"ok": not err, "models": models, "error": err,
            "hint": "" if models else "впиши имя модели вручную"}


@app.post("/api/ai/test")
def test_ai_endpoint(data: AIProbeModel, x_luch_token: str | None = Header(default=None)):
    """Проверить соединение коротким запросом."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    same_address = (ai_instance._normalize_ai_base_url(data.base_url) == ai_instance.ai_base_url
                    if data.base_url.strip() else True)
    key = data.api_key.strip() or (ai_instance.ai_api_key if same_address else "")
    ok, message = ai_instance.test_ai_endpoint(data.base_url, key, data.model)
    return {"ok": ok, "message": message}


# =============== Меню и настройки с сайта ===============

@app.get("/api/menu")
def get_menu():
    """Структура меню и список команд ИИ для отрисовки на сайте."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    return ai_instance.web_command_info()


@app.post("/api/menu/run")
def run_menu_item(data: RunCommandModel, x_luch_token: str | None = Header(default=None)):
    """Выполнить пункт меню из браузера. Интерактивные пункты отклоняются."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        return ai_instance.web_run_command(data.name, confirm=data.confirm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ошибка выполнения: {e}")


@app.get("/api/settings")
def get_settings_public():
    """Настройки для панели CONFIG. Секреты заменены признаком наличия."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    return {"settings": ai_instance.web_settings_info()}


@app.post("/api/settings")
def set_setting_public(data: SettingModel, x_luch_token: str | None = Header(default=None)):
    """Горячее изменение одной настройки (голос, скорость, триггер, микрофон, STT)."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    _require_web_token(x_luch_token)
    try:
        value, warning = ai_instance.web_apply_setting(data.key, data.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"не удалось применить: {e}")
    return {"status": "ok", "key": data.key, "value": value, "warning": warning}

def _convert_voice(payload):
    """Сохранить загруженное аудио и привести к 16 кГц моно. Вызывается в пуле потоков."""
    target_wav = settings.PATHS.get("temp_wav_file", str(BASE_DIR / "temp.wav"))
    fd, temp_raw = tempfile.mkstemp(prefix="luch_voice_", suffix=".webm")
    os.close(fd)
    try:
        with open(temp_raw, "wb") as f:
            f.write(payload)
        try:
            proc = subprocess.run([
                "ffmpeg", "-y", "-i", temp_raw,
                "-ar", "16000", "-ac", "1", target_wav
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        except FileNotFoundError:
            return False, "ffmpeg не найден в системе"
        except subprocess.TimeoutExpired:
            return False, "ffmpeg не ответил за 60 секунд"
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            return False, f"ffmpeg вернул код {proc.returncode}: {tail[-1] if tail else 'нет вывода'}"
        if not os.path.exists(target_wav) or os.path.getsize(target_wav) == 0:
            return False, "не удалось создать WAV-файл"
        return True, ""
    finally:
        try:
            os.unlink(temp_raw)
        except OSError:
            pass


@app.post("/_selftest")
async def selftest(report: str = Body(...)):
    """Отчёт о проверке связи из приложения. Только для установки."""
    import datetime
    print("[SELFTEST]", datetime.datetime.now().strftime("%H:%M:%S"), report, flush=True)
    return {"ok": True}


@app.post("/api/voice")
async def ask_ai_voice(file: UploadFile = File(...)):
    global ai_instance
    if not ai_instance:
        raise HTTPException(status_code=500, detail="Экземпляр AI не инициализирован")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Пустой аудиофайл")

    ok, err = await run_in_threadpool(_convert_voice, payload)
    if not ok:
        raise HTTPException(status_code=503, detail=err)

    ai_instance.response_ready_event.clear()
    ai_instance.pending_voice = True

    is_ready = await run_in_threadpool(ai_instance.response_ready_event.wait, 60)
    if not is_ready:
        raise HTTPException(status_code=504, detail="Превышено время ожидания ответа ИИ")

    # Проверка стоп-слова
    if getattr(ai_instance, 'stop_audio_triggered', False):
        ai_instance.stop_audio_triggered = False
        return {
            "status": "stop_audio",
            "user_text": "",
            "response": "",
            "play_audio": False
        }

    # Игнорирование (чужой голос или нет триггера). Раньше здесь возвращалась
    # пустота, и приложение не показывала ничего — пользователь не понимал,
    # что его отвергли. Теперь причина доходит до экрана.
    if getattr(ai_instance, 'ignore_response', False):
        reason = ai_instance.ignore_response
        ai_instance.ignore_response = False
        why = ""
        if reason == "voice":
            why = ("Голос не распознан: проверка владельца не пройдена. "
                   "Перезапишите эталон голоса или скажите громче.")
        elif reason == "trigger":
            why = "Не услышал слово-триггер «%s»." % getattr(ai_instance, "trigger_word", "луч")
        return {
            "status": "ignored",
            "reason": reason if isinstance(reason, str) else "trigger",
            "user_text": getattr(ai_instance, "user_prompt", "") or "",
            "response": why,
            "play_audio": False
        }

    return {
        "status": "ok",
        "user_text": ai_instance.user_prompt or "Голосовой запрос",
        "response": ai_instance.format_response(ai_instance.response),
        "play_audio": getattr(ai_instance, 'ai_speak', False) or getattr(ai_instance, 'is_voice_success', False)
    }

@app.get("/api/audio")
def get_audio():
    path = Path(settings.PATHS["response_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Аудиофайл ещё не готов")
    return FileResponse(path, media_type="audio/wav")

@app.get("/api/alerts")
def get_alerts():
    # Отдаем уведомления веб-клиенту и сразу очищаем очередь
    if hasattr(work_fuctions, "web_alerts") and len(work_fuctions.web_alerts) > 0:
        alerts_to_send = work_fuctions.web_alerts.copy()
        work_fuctions.web_alerts.clear()
        return {"alerts": alerts_to_send}
    return {"alerts": []}

@app.get("/api/alert_audio")
def get_alert_audio():
    # Отдаем сгенерированный аудиофайл таймера
    file_path = "/tmp/timer_alert.wav"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/wav")
    raise HTTPException(status_code=404, detail="Audio not found")

@app.post("/api/location")
def post_location(data: LocationModel):
    """Принять текущие координаты клиента (телефон/браузер)."""
    result = work_fuctions.update_location(data.lat, data.lng, data.accuracy, data.source)
    return {"status": "ok", "message": result}

@app.get("/api/location")
def get_location():
    """Вернуть текущую позицию клиента."""
    return work_fuctions.get_location()

@app.get("/api/location/history")
def get_location_history():
    """Вернуть историю перемещений клиента."""
    return {"history": work_fuctions.location_history}

@app.get("/api/location/request")
def location_request_status():
    """Есть ли активный запрос координат со стороны ИИ."""
    return {"pending": getattr(work_fuctions, "location_request_pending", False)}

@app.post("/api/location/request/resolve")
def location_request_resolve():
    """Сбросить запрос координат (клиент прислал позицию)."""
    work_fuctions.location_request_pending = False
    return {"status": "ok"}

@app.get("/api/navigator/status")
def navigator_status():
    """Состояние навигатора: активен ли, маршрут, текущий маневр."""
    status = work_fuctions.navigation_status()
    status["location"] = work_fuctions.get_location()
    status["ready"] = work_fuctions.location_data.get("lat") is not None
    return status

@app.post("/api/navigator/start")
def navigator_start(data: RouteModel):
    """Запустить голосовую навигацию до цели."""
    result = work_fuctions.start_navigation(data.destination)
    return {"status": "ok", "data": result}

@app.post("/api/navigator/stop")
def navigator_stop():
    """Остановить голосовую навигацию."""
    result = work_fuctions.stop_navigation()
    return {"status": "ok", "data": result}

@app.get("/api/state")
def get_ai_state():
    """Текущее состояние AI для подсветки 3D-ядра.
    idle | listening | thinking | speaking | command | error"""
    if not ai_instance:
        return {"state": "idle", "provider": "", "model": "", "label": ""}
    provider = getattr(ai_instance, "provider", "") or ""
    if provider in ("openai", "custom", "api", "compatible"):
        model = getattr(ai_instance, "ai_model", "")
    elif provider == "zen":
        model = getattr(ai_instance, "zen_model", "")
    else:
        model = getattr(ai_instance, "model", "")
    try:
        label = ai_instance.ai_label()
    except Exception:
        label = provider
    return {"state": getattr(ai_instance, "ai_state", "idle"),
            "provider": provider,
            "model": model,
            "label": label}

def is_running():
    srv = server_instance
    return bool(srv is not None and getattr(srv, "started", False) and not srv.should_exit)


def stop_server(timeout=8.0):
    """Остановить uvicorn и дождаться освобождения порта."""
    global server_instance, _server_thread
    srv, thread = server_instance, _server_thread
    server_instance = None
    if srv is None:
        return True
    srv.should_exit = True
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
    if thread is not None and thread.is_alive():
        return False
    _server_thread = None
    return True


def _wait_port_free(port, timeout=5.0):
    """Дождаться, пока порт перестанет слушаться."""
    import socket as _socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.1)
    return False


# =============== Управление подключёнными устройствами (телефон) ===============
# Токен устройства выдаётся при регистрации и не связан с web_token:
# телефон не должен уметь менять настройки сервера и ключи провайдера.


class DeviceRegisterModel(BaseModel):
    name: str = "Телефон"
    kind: str = "android"
    token: str = ""
    client_id: str = ""
    caps: list = []
    model: str = ""
    os: str = ""


class DeviceResultModel(BaseModel):
    cmd_id: str = ""
    status: str = "ok"
    output: str = ""


class DeviceRenameModel(BaseModel):
    name: str = ""


def _device_auth(client_id, token):
    if not device_control.check_token(client_id, token):
        raise HTTPException(status_code=401, detail="Неверный токен устройства")
    return device_control.devices.get(client_id)


@app.get("/api/device/ping")
def device_ping(client_id: str = "", token: str = "", battery: int = -1):
    """Проверить, что сервер виден, и заодно отметить устройство онлайн."""
    if not device_control.check_token(client_id, token):
        return {"status": "unauthorized", "server": "LUCH", "now": time.time()}
    dev = device_control.touch(client_id, None if battery < 0 else battery)
    return {"status": "ok", "server": "LUCH", "now": time.time(),
            "device": device_control.device_info(client_id)}


@app.post("/api/device/register")
def device_register(data: DeviceRegisterModel):
    """Регистрация телефона. Возвращает client_id и токен — их приложение сохраняет."""
    info = device_control.register_device(
        name=data.name, kind=data.kind, token=data.token or None,
        caps=data.caps, model=data.model, os_version=data.os,
        client_id=data.client_id or None)
    return {"status": "ok", "client_id": info["client_id"], "token": info["token"],
            "name": info["name"], "caps": info["caps"], "server": "LUCH"}


@app.get("/api/device/list")
def device_list():
    """Список устройств без токенов — для панели и для ИИ."""
    device_control.register_desktop()
    return {"devices": device_control.list_devices(),
            "server_ip": get_local_ip(), "port": _server_port}


@app.post("/api/device/forget")
def device_forget(client_id: str = "", token: str = ""):
    """Забыть устройство (например, телефон украли)."""
    _device_auth(client_id, token)
    return device_control.forget_device(client_id)


@app.post("/api/device/rename")
def device_rename(client_id: str = "", token: str = "", name: str = ""):
    """Переименовать устройство."""
    _device_auth(client_id, token)
    return device_control.rename_device(client_id, name)


@app.post("/api/device/location")
def device_location(client_id: str = "", token: str = "", data: LocationModel = None,
                    x_luch_token: str | None = Header(default=None)):
    """Телефон прислал свои координаты. Токен устройства ИЛИ web_token."""
    if not device_control.check_token(client_id, token):
        if not client_id:
            _require_web_token(x_luch_token)
        else:
            _device_auth(client_id, token)
    return device_control.report_location(client_id, data.lat, data.lng, data.accuracy)


@app.get("/api/device/queue")
def device_queue(client_id: str = "", token: str = "", since: int = 0, battery: int = -1):
    """Телефон забирает команды для выполнения. Должно вызываться каждые 2-3 секунды."""
    dev = _device_auth(client_id, token)
    device_control.touch(client_id, None if battery < 0 else battery)
    pack = device_control.take(client_id, since)
    return {"status": "ok", "commands": pack["commands"], "seq": pack["seq"],
            "device": device_control.device_info(client_id)}


@app.post("/api/device/result")
def device_result(client_id: str = "", token: str = "", data: DeviceResultModel = None):
    """Телефон отчитывается о выполненной команде."""
    _device_auth(client_id, token)
    return device_control.submit(client_id, data.cmd_id, data.status, data.output)


@app.post("/api/device/ping")
def device_ping_post(client_id: str = "", token: str = "", battery: int = -1):
    """То же, что GET /api/device/ping, но POST — так проще из приложения."""
    return device_ping(client_id, token, battery)


_CERT_DIR = Path(__file__).parent
CERT_FILE = _CERT_DIR / "luch-cert.pem"
KEY_FILE = _CERT_DIR / "luch-key.pem"
CERT_META = _CERT_DIR / "luch-cert.json"
CERT_DAYS = 825
_tls_server = None


def _interface_ips():
    """Все IPv4-адреса всех интерфейсов: сертификат должен подходить любому,
    кто заходит с телефона, ноутбука или по VPN, а не только текущему IP."""
    ips = set()
    try:
        import fcntl
        import struct
        for _, name in socket.if_nameindex():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = struct.pack("256s", name.encode()[:15])
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])
                if not addr.startswith("127."):
                    ips.add(addr)
            except OSError:
                pass
            finally:
                s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if not info[4][0].startswith("127."):
                ips.add(info[4][0])
    except Exception:
        pass
    return sorted(ips)


def _cert_names():
    host = socket.gethostname()
    names = ["localhost", host, host.split(".")[0] + ".local"]
    ips = ["127.0.0.1"] + _interface_ips()
    return names, sorted(set(ips))


def ensure_tls_cert():
    """Самоподписанный сертификат со всеми локальными адресами.
    Создаётся сам и пересоздаётся, только если адреса изменились или срок вышел,
    чтобы не приходилось вручную перегенерировать его при смене IP в сети."""
    names, ips = _cert_names()
    san = ["DNS:" + n for n in names] + ["IP:" + i for i in ips]
    if CERT_FILE.exists() and KEY_FILE.exists() and CERT_META.exists():
        try:
            meta = json.loads(CERT_META.read_text(encoding="utf-8"))
            if meta.get("san") == san and float(meta.get("not_after", 0)) > time.time():
                return True
        except Exception:
            pass
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
             "-days", str(CERT_DAYS), "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
             "-subj", "/CN=ЛУЧ",
             # CA:TRUE обязателен, иначе телефон не поставит сертификат в доверенные
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
             "-addext", "subjectAltName=" + ",".join(san)],
            check=True, capture_output=True, timeout=60)
        os.chmod(KEY_FILE, 0o600)
        not_after = 0.0
        out = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", str(CERT_FILE)],
                             capture_output=True, text=True, timeout=30).stdout
        # notAfter=Sep 30 08:12:00 2027 GMT
        if "notAfter=" in out:
            import datetime as _dt
            raw = out.split("notAfter=", 1)[1].strip()
            for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
                try:
                    not_after = _dt.datetime.strptime(raw, fmt).replace(
                        tzinfo=_dt.timezone.utc).timestamp()
                    break
                except ValueError:
                    continue
        CERT_META.write_text(json.dumps({"san": san, "not_after": not_after},
                                        ensure_ascii=False), encoding="utf-8")
        print(f" 🔑 Сертификат выпущен для: {', '.join(san)}")
        return True
    except Exception as e:
        print(f" ⚠ Не удалось выпустить сертификат: {e}. HTTPS недоступен")
        return False


def _start_tls_server(tls_port):
    """Второй веб-сервер поверх того же приложения, но с шифрованием."""
    global _tls_server
    cfg = uvicorn.Config(app, host="0.0.0.0", port=tls_port, log_level="error",
                         ssl_certfile=str(CERT_FILE), ssl_keyfile=str(KEY_FILE))
    srv = uvicorn.Server(cfg)
    _tls_server = srv
    threading.Thread(target=srv.run, daemon=True, name="luch-tls").start()
    for _ in range(30):
        if getattr(srv, "started", False):
            break
        time.sleep(0.1)
    if not getattr(srv, "started", False):
        print(f" ⚠ Защищённый сервер на порту {tls_port} не поднялся")


def start_server_in_thread(ai_obj, port=1337):
    global ai_instance, server_instance, _server_thread, _server_port
    ai_instance = ai_obj
    _server_thread = threading.current_thread()
    _server_port = port
    ip = get_local_ip()

    if not _wait_port_free(port, 1.0):
        print(f" ⚠ Порт {port} занят — веб-интерфейс может не запуститься")

    # Микрофон в приложении работает только по HTTPS: браузер и WebView не дают
    # доступ к микрофону на http://, кроме localhost. Поэтому рядом с обычным
    # HTTP поднимается ещё и HTTPS — на соседнем порту, чтобы старые http://
    # ссылки и закладки продолжали работать.
    tls_port = int(os.environ.get("LUCH_HTTPS_PORT", "") or (port + 1))
    tls_on = os.environ.get("LUCH_HTTPS", "1") != "0"
    if tls_on:
        if ensure_tls_cert():
            if not _wait_port_free(tls_port, 0.5):
                print(f" ⚠ Порт {tls_port} занят — защищённый адрес не запустится")
            else:
                _start_tls_server(tls_port)
        else:
            tls_on = False

    print(f" 🌐 Веб-интерфейс: http://{ip}:{port}")
    if tls_on:
        print(f" 🔒 Защищённый адрес (для микрофона): https://{ip}:{tls_port}")

    # Компьютер сам объявляет себя, чтобы ИИ видел ПК и телефоны в одном списке
    device_control.register_desktop()

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_instance = server
    try:
        server.run()
    except OSError as e:
        print(f" ✖ Веб-сервер не запустился: {e}")
    finally:
        if server_instance is server:
            server_instance = None
        _server_thread = None