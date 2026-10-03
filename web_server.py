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
from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi import Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from urllib.parse import urlparse
import uvicorn
import settings
import work_fuctions
import device_control

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = BASE_DIR / "index.html"

# Автодокументация FastAPI (Swagger/OpenAPI) выключена: она раскрывает полную
# карту API без токена, а нам нужен только явный контракт ниже.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# CORS без «*»: UI живёт на том же origin, ему кросс-домен не нужен. Список
# источников задаётся окружением LUCH_CORS_ORIGINS через запятую (по умолчанию
# пусто = никаких кросс-доменных запросов). Android — не браузер, CORS игнорирует.
_cors_raw = os.environ.get("LUCH_CORS_ORIGINS", "") or ""
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Luch-Token"],
)

# --- Кэши: версия сборки и готовый app.js (пересчёт только при изменениях) ---
_build_version_lock = threading.Lock()
_build_version_cache = {"mtime": None, "value": None}
_app_js_lock = threading.Lock()
_app_js_cache = {"key": None, "body": None}

BUILD_GRADLE = BASE_DIR / "android" / "app" / "build.gradle.kts"


def app_build_version() -> str:
    """Версия приложения из build.gradle.kts — страница сообщает её мосту.

    Файл читается один раз: значение кэшируется и пересчитывается только при
    изменении mtime, а не на каждом запросе app.js.
    """
    try:
        mtime = BUILD_GRADLE.stat().st_mtime_ns
    except OSError:
        with _build_version_lock:
            _build_version_cache["mtime"] = None
            _build_version_cache["value"] = "?"
        return "?"
    with _build_version_lock:
        if _build_version_cache["mtime"] == mtime and _build_version_cache["value"] is not None:
            return _build_version_cache["value"]
    try:
        txt = BUILD_GRADLE.read_text(encoding="utf-8")
        m = re.search(r'versionName\s*=\s*"([^"]+)"', txt)
        value = m.group(1) if m else "?"
    except Exception:
        value = "?"
    with _build_version_lock:
        _build_version_cache["mtime"] = mtime
        _build_version_cache["value"] = value
    return value


def _render_app_js(full_path, stat_result) -> bytes:
    """app.js с подставленной версией сборки. Кэш по ключу (mtime, size, version)."""
    try:
        mtime, size = stat_result.st_mtime_ns, stat_result.st_size
    except Exception:
        st = os.stat(full_path)
        mtime, size = st.st_mtime_ns, st.st_size
    version = app_build_version()
    key = (mtime, size, version)
    with _app_js_lock:
        if _app_js_cache["key"] == key and _app_js_cache["body"] is not None:
            return _app_js_cache["body"]
    text = Path(full_path).read_text(encoding="utf-8")
    body = text.replace("__BUILD__", version).encode("utf-8")
    with _app_js_lock:
        _app_js_cache["key"] = key
        _app_js_cache["body"] = body
    return body


class NoCacheStatic(StaticFiles):
    """app.js и html всегда свежие: старый app.js в кэше WebView выглядел как
    «ничего не изменилось». Остальным файлам (css, картинки, шрифты) разрешено
    короткое кэширование — Starlette отдаёт их с ETag и Last-Modified."""

    NO_CACHE = "no-store, no-cache, must-revalidate, max-age=0"
    SHORT_CACHE = "public, max-age=300"

    def file_response(self, full_path, stat_result, scope, status_code=200):
        name = str(full_path).lower()
        if name.endswith("app.js") or name.endswith(".html"):
            hdr = {"Cache-Control": self.NO_CACHE, "Pragma": "no-cache"}
            if name.endswith("app.js"):
                try:
                    return Response(_render_app_js(full_path, stat_result),
                                    media_type="application/javascript", headers=hdr)
                except Exception:
                    pass
            resp = super().file_response(full_path, stat_result, scope, status_code)
            resp.headers["Cache-Control"] = self.NO_CACHE
            resp.headers["Pragma"] = "no-cache"
            return resp
        resp = super().file_response(full_path, stat_result, scope, status_code)
        try:
            resp.headers["Cache-Control"] = self.SHORT_CACHE
        except Exception:
            pass
        return resp


app.mount("/static", NoCacheStatic(directory=str(STATIC_DIR), check_dir=False), name="static")

@app.get("/api/health")
def health():
    """Публичная проверка живости: никаких секретов, IP и данных об устройстве."""
    return {"status": "ok", "version": app_build_version()}

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


# =============== Проверка web_token ===============
# Публичны: корень, /static, /api/health и точки, которые аутентифицируются
# токеном УСТРОЙСТВА (client_id + token), а не web_token. Всё остальное под
# /api/* и /_selftest закрыто middleware ниже.
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/device/register",
    "/api/device/queue",
    "/api/device/result",
    "/api/device/ping",
    "/api/device/location",
    # /api/voice сам проверяет доступ: либо токен устройства (телефон не знает
    # web_token), либо токен веб-панели. Поэтому на уровне middleware он открыт,
    # а решение принимается внутри ручки.
    "/api/voice",
}


def _is_public_path(path: str) -> bool:
    if path in ("/", "/api/health"):
        return True
    if path == "/static" or path.startswith("/static/"):
        return True
    # Учитываем возможный слеш в конце (/api/device/ping/), иначе FastAPI-редирект
    # на 307 отсекался бы middleware как «защищённый».
    return path in PUBLIC_API_PATHS or path.rstrip("/") in PUBLIC_API_PATHS


def _require_web_token(token):
    """Проверить токен веб-панели из заголовка X-Luch-Token.

    Пустой web_token — это НЕ «проверки нет», а ошибка конфигурации: без токена
    панель осталась бы открытой всем, поэтому закрываемся (fail closed, 500).
    """
    expected = (settings.settings.get("web_token") or "").strip()
    if not expected:
        raise HTTPException(status_code=500,
                            detail="web_token не задан в настройках — без него веб-панель "
                                   "была бы открыта всем. Задай web_token и перезапусти сервер.")
    if not expected.isascii():
        raise HTTPException(status_code=500,
                            detail="web_token содержит нелатинские символы — такие нельзя передать "
                                   "в HTTP-заголовке. Замени его на латиницу.")
    supplied = "" if token is None else str(token)
    # Сравниваем БАЙТЫ: hmac.compare_digest со str падает TypeError на не-ASCII
    # клиентском токене и превращал бы 401 в 500.
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"),
                                               expected.encode("utf-8")):
        raise HTTPException(status_code=401,
                            detail="Нужен верный web_token (заголовок X-Luch-Token)")


@app.middleware("http")
async def _web_token_middleware(request, call_next):
    """Единая защита /api/* и /_selftest: проверка в одном месте, а не в каждой ручке."""
    path = request.url.path
    # Предполётный CORS-запрос (OPTIONS) не несёт токена — пропускаем его к CORS.
    if request.method == "OPTIONS":
        return await call_next(request)
    guarded = path == "/_selftest" or (path.startswith("/api/") and not _is_public_path(path))
    if guarded:
        try:
            _require_web_token(request.headers.get("X-Luch-Token"))
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


def _log_web_error(where: str, exc: Exception):
    """Настоящую ошибку пишем в журнал, наружу отдаём общее сообщение."""
    try:
        work_fuctions.log_event("web_error", f"{where}: {type(exc).__name__}: {exc}")
    except Exception:
        print(f"[web_error] {where}: {exc}", flush=True)


def _validate_base_url(url: str) -> str:
    """Проверка адреса API перед запросом: только http/https и не служебные цели.

    Локальные и приватные адреса разрешены — пользователь держит LLM на
    127.0.0.1/localhost или в своей LAN. Запрещены адрес метаданных облака и
    весь link-local диапазон 169.254.0.0/16.
    """
    raw = (url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="не указан адрес API (base_url)")
    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").strip().strip("[]").lower()
    except ValueError:
        raise HTTPException(status_code=400, detail="некорректный адрес API (base_url)")
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400,
                            detail="адрес API должен начинаться с http:// или https://")
    if not host:
        raise HTTPException(status_code=400, detail="в адресе API не указан хост")
    if host == "169.254.169.254" or host.startswith("169.254."):
        raise HTTPException(status_code=400,
                            detail="адреса из служебного диапазона 169.254.0.0/16 запрещены")
    return raw


_local_ip_lock = threading.Lock()
_local_ip_cache = {"ip": None, "ts": 0.0}


def get_local_ip():
    """LAN-адрес машины. Кэш ~10 секунд: сокет на каждый запрос не нужен."""
    now = time.time()
    with _local_ip_lock:
        if _local_ip_cache["ip"] and (now - _local_ip_cache["ts"]) < 10.0:
            return _local_ip_cache["ip"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    with _local_ip_lock:
        _local_ip_cache["ip"] = IP
        _local_ip_cache["ts"] = now
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
    ask_started_at = time.time()
    ai_instance.response_ready_event.clear()
    ai_instance.user_prompt = data.text

    is_ready = ai_instance.response_ready_event.wait(timeout=60)
    if not is_ready:
        raise HTTPException(status_code=504, detail="Превышено время ожидания ответа ИИ")

    return {
        "status": "ok",
        "response": ai_instance.format_response(ai_instance.response),
        # тот же принцип, что и в /api/voice: файл ответа, а не мигающий флаг
        "play_audio": _fresh_audio(ask_started_at),
        "commands": ai_instance.ai_commands_since(seq_before),
    }

@app.post("/api/command")
def execute_command(data: CommandModel):
    global ai_instance
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    cmd = (data.cmd or "").strip()
    if not cmd:
        return JSONResponse({"status": "error", "detail": "Пустая команда",
                             "output": "Пустая команда"}, status_code=400)
    try:
        # from_web=True: интерактивные пункты меню (input()/стрелки) из веба
        # запускать нельзя — они заблокировали бы worker-поток FastAPI.
        result = ai_instance.process_command(cmd, from_web=True)
    except ValueError as e:
        return JSONResponse({"status": "error", "detail": str(e), "output": str(e)},
                            status_code=400)
    except Exception as e:
        _log_web_error("/api/command", e)
        msg = "Не удалось выполнить команду"
        return JSONResponse({"status": "error", "detail": msg, "output": msg}, status_code=500)
    # process_command возвращает строку-статус, а не бросает исключение.
    if isinstance(result, str):
        upper = result.strip().upper()
        if upper == "UNKNOWN COMMAND":
            msg = f"Неизвестная команда: {cmd}"
            return JSONResponse({"status": "error", "detail": msg, "output": msg}, status_code=400)
        if upper.startswith("ERROR:"):
            _log_web_error("/api/command", RuntimeError(result))
            msg = "Команда завершилась с ошибкой"
            return JSONResponse({"status": "error", "detail": msg, "output": msg}, status_code=500)
    return {"status": "ok", "output": result}


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
def run_ai_command(data: AICommandRunModel):
    """Выполнить команду ИИ с сайта и вернуть её вывод."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    try:
        return ai_instance.web_run_ai_command(
            data.command, data.args, data.confirm,
            data.timeout or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_web_error("/api/ai/commands/run", e)
        raise HTTPException(status_code=500, detail="не удалось выполнить команду")


@app.post("/api/ai/commands/clear")
def clear_ai_command_history():
    """Очистить журнал выполнения ИИ-команд."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    ai_instance.web_clear_ai_command_history()
    return {"status": "ok"}


@app.post("/api/ai/config")
def set_ai_config(data: AIConfigModel):
    """Сохранить кастомного провайдера (адрес/ключ/модель) и, если надо, переключиться на него."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
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
        _log_web_error("/api/ai/config", e)
        raise HTTPException(status_code=500, detail="не удалось сохранить конфигурацию ИИ")


@app.post("/api/ai/switch")
def switch_ai_provider(data: AIProviderSwitchModel):
    """Переключиться на ollama, zen или сохранённого кастомного провайдера."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    try:
        ai_instance.web_switch_provider(data.provider)
        return {"status": "ok", "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_web_error("/api/ai/switch", e)
        raise HTTPException(status_code=500, detail="не удалось переключить провайдера")


@app.post("/api/ai/zen")
def set_ai_zen_key(data: AIZenKeyModel):
    """Сохранить API-ключ и модель OpenCode Zen."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    try:
        ai_instance.web_set_zen_key(data.api_key, data.model or None)
        return {"status": "ok", "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_web_error("/api/ai/zen", e)
        raise HTTPException(status_code=500, detail="не удалось сохранить ключ Zen")


@app.post("/api/ai/ollama")
def set_ai_ollama_model(data: AIOllamaModelModel):
    """Сохранить модель Ollama с сайта."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    try:
        ai_instance.web_set_ollama_model(data.model)
        return {"status": "ok", "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_web_error("/api/ai/ollama", e)
        raise HTTPException(status_code=500, detail="не удалось сохранить модель Ollama")


@app.delete("/api/ai/provider")
def delete_ai_provider(provider_id: str):
    """Удалить кастомного провайдера из списка."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    try:
        removed = ai_instance.web_delete_custom_provider(provider_id)
        return {"status": "ok", "removed": removed, "config": ai_instance.ai_config()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/ai/models")
def get_ai_models(base_url: str = ""):
    """Список моделей с адреса, используется СОХРАНЁННЫЙ ключ.

    Ключ намеренно НЕ принимается в query-строке: она попадает в логи сервера,
    прокси и историю браузера. Чтобы проверить ещё не сохранённый ключ, есть
    POST-вариант ниже — он принимает ключ в теле запроса.
    """
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    # Адрес приходит от клиента и уходит в requests — проверяем против SSRF.
    if base_url.strip():
        base_url = _validate_base_url(base_url)
    if base_url.strip() and base_url.strip() != ai_instance.ai_base_url:
        # Адрес новый, а ключа в query нет — чужой ключ подставлять нельзя.
        models, err = ai_instance.list_ai_models(base_url=base_url, api_key="")
    else:
        models, err = ai_instance.list_ai_models(base_url=base_url or None)
    return _models_payload(models, err)


@app.post("/api/ai/models")
def post_ai_models(data: AIModelsQuery):
    """Список моделей с явным ключом в ТЕЛЕ запроса (не в URL — его видно в логах)."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    base_url = data.base_url.strip()
    if base_url:
        base_url = _validate_base_url(base_url)
    api_key = (data.api_key or "").strip()
    if not api_key and base_url and base_url != ai_instance.ai_base_url:
        # Новый адрес без ключа — используем сохранённый только для своего адреса.
        api_key = ""
    models, err = ai_instance.list_ai_models(base_url=base_url or None,
                                             api_key=api_key or None)
    return _models_payload(models, err)


def _models_payload(models, err):
    return {"ok": not err, "models": models, "error": err,
            "hint": "" if models else "впиши имя модели вручную"}


@app.post("/api/ai/test")
def test_ai_endpoint(data: AIProbeModel):
    """Проверить соединение коротким запросом."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    # Адрес приходит от клиента и уходит в requests — проверяем против SSRF.
    if data.base_url.strip():
        data.base_url = _validate_base_url(data.base_url)
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
def run_menu_item(data: RunCommandModel):
    """Выполнить пункт меню из браузера. Интерактивные пункты отклоняются."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    try:
        return ai_instance.web_run_command(data.name, confirm=data.confirm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_web_error("/api/menu/run", e)
        raise HTTPException(status_code=500, detail="ошибка выполнения команды меню")


@app.get("/api/settings")
def get_settings_public():
    """Настройки для панели CONFIG. Секреты заменены признаком наличия."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    return {"settings": ai_instance.web_settings_info()}


@app.post("/api/settings")
def set_setting_public(data: SettingModel):
    """Горячее изменение одной настройки (голос, скорость, триггер, микрофон, STT)."""
    if not ai_instance:
        raise HTTPException(status_code=500, detail="AI не инициализирован")
    try:
        value, warning = ai_instance.web_apply_setting(data.key, data.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_web_error("/api/settings", e)
        raise HTTPException(status_code=500, detail="не удалось применить настройку")
    return {"status": "ok", "key": data.key, "value": value, "warning": warning}

# Приватный каталог сервера для временных файлов (не общий /tmp).
_TMP_DIR = BASE_DIR / "logs" / "tmp"
# Лимит тела голосового запроса и потолок текста self-test.
MAX_VOICE_BYTES = 25 * 1024 * 1024
MAX_SELFTEST_CHARS = 4000


def _convert_voice(payload):
    """Сохранить загруженное аудио и привести к 16 кГц моно. Вызывается в пуле потоков.

    ffmpeg пишет в УНИКАЛЬНЫЙ файл (общий temp.wav как цель параллельных запросов
    давал бы мусор), а затем результат атомарно подменяет temp_wav_file, который
    читает основной процесс. Уникальный файл всегда удаляется в finally.
    """
    target_wav = settings.PATHS.get("temp_wav_file", str(BASE_DIR / "temp.wav"))
    fd, temp_raw = tempfile.mkstemp(prefix="luch_voice_", suffix=".webm")
    os.close(fd)
    try:
        _TMP_DIR.mkdir(parents=True, exist_ok=True)
        out_fd, temp_out = tempfile.mkstemp(prefix="luch_voice_", suffix=".wav", dir=str(_TMP_DIR))
        os.close(out_fd)
        try:
            with open(temp_raw, "wb") as f:
                f.write(payload)
            try:
                proc = subprocess.run([
                    "ffmpeg", "-y", "-i", temp_raw,
                    "-ar", "16000", "-ac", "1", temp_out
                ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            except FileNotFoundError:
                return False, "ffmpeg не найден в системе"
            except subprocess.TimeoutExpired:
                return False, "ffmpeg не ответил за 60 секунд"
            if proc.returncode != 0:
                # Декодируем только последнюю строку, а не весь stderr.
                err_bytes = (proc.stderr or b"").strip()
                last = err_bytes.splitlines()[-1].decode("utf-8", "ignore") if err_bytes else "нет вывода"
                return False, f"ffmpeg вернул код {proc.returncode}: {last}"
            if not os.path.exists(temp_out) or os.path.getsize(temp_out) == 0:
                return False, "не удалось создать WAV-файл"
            # Атомарная публикация: основной процесс всегда видит целый файл.
            os.replace(temp_out, target_wav)
            return True, ""
        finally:
            try:
                os.unlink(temp_out)
            except OSError:
                pass
    finally:
        try:
            os.unlink(temp_raw)
        except OSError:
            pass


@app.post("/_selftest")
async def selftest(report: str = Body(...)):
    """Отчёт о проверке связи из приложения. Только для установки.

    Токен требуется middleware (это не /api/*, но путь закрыт явно), а размер
    ограничен, чтобы журнал нельзя было залить произвольным текстом.
    """
    import datetime
    if len(report) > MAX_SELFTEST_CHARS:
        raise HTTPException(status_code=413,
                            detail=f"отчёт слишком длинный (максимум {MAX_SELFTEST_CHARS} символов)")
    print("[SELFTEST]", datetime.datetime.now().strftime("%H:%M:%S"), report, flush=True)
    return {"ok": True}


def _fresh_audio(request_started_at, tolerance=2.0):
    """Есть ли WAV-ответ, соответствующий текущему запросу.

    Сервер пишет озвучку в response.wav и сразу проигрывает её на колонках.
    Флаг ai_speak к моменту ответа в телефон уже погас, поэтому ориентируемся
    на свежесть самого файла.
    """
    path = Path(settings.PATHS["response_path"])
    try:
        if not path.is_file():
            return False
        return path.stat().st_mtime >= request_started_at - tolerance
    except OSError:
        return False


@app.post("/api/voice")
async def ask_ai_voice(file: UploadFile = File(...),
                       client_id: str = "",
                       token: str = "",
                       x_luch_token: str | None = Header(default=None)):
    global ai_instance
    if not ai_instance:
        raise HTTPException(status_code=500, detail="Экземпляр AI не инициализирован")

    # Доступ: либо токен веб-панели (браузер), либо токен УСТРОЙСТВА (телефон).
    # Android не знает web_token — он аутентифицируется своей парой client_id+token,
    # той же, что и остальные /api/device/*. Иначе голос с телефона получал бы 401.
    if not (client_id and token and device_control.check_token(client_id, token)):
        _require_web_token(x_luch_token)

    # Контент-тип: браузер шлёт audio/webm, приложение — octet-stream.
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    allowed = ctype.startswith("audio/") or ctype in ("video/webm", "application/octet-stream")
    if ctype and not allowed:
        raise HTTPException(status_code=415, detail="ожидается аудиофайл (audio/*)")

    # Читаем не больше лимита+1 байт: тело не должно уходить в RAM целиком.
    payload = await file.read(MAX_VOICE_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail="Пустой аудиофайл")
    if len(payload) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"аудиофайл слишком большой (максимум {MAX_VOICE_BYTES // (1024 * 1024)} МБ)")

    # Момент запроса нужен ниже: по нему мы поймём, что аудиофайл ответа
    # принадлежит именно этому запросу, а не прошлому.
    voice_request_at = time.time()

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

    # Раньше здесь стояло «ai_speak или is_voice_success». Оба флага означают
    # не наличие звука, а состояние сервера: ai_speak=True только пока сервер
    # прямо сейчас озвучивает ответ на колонках и сбрасывается сразу после, а
    # is_voice_success — про успешное распознавание голоса. Телефон успевал
    # получить play_audio=false, и ответ не озвучивался вообще.
    # Наличие звука определяем по файлу: он должен быть свежее этого запроса.
    return {
        "status": "ok",
        "user_text": ai_instance.user_prompt or "Голосовой запрос",
        "response": ai_instance.format_response(ai_instance.response),
        "play_audio": _fresh_audio(voice_request_at)
    }

@app.get("/api/audio")
def get_audio():
    path = Path(settings.PATHS["response_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Аудиофайл ещё не готов")
    return FileResponse(path, media_type="audio/wav")

# Замок вокруг чтения+очистки очереди уведомлений: работа с web_alerts идёт из
# разных потоков, а copy()+clear() без замка теряла или дублировала уведомления.
_alerts_lock = threading.Lock()


@app.get("/api/alerts")
def get_alerts():
    # Отдаем уведомления веб-клиенту и сразу очищаем очередь (атомарно на нашей стороне)
    with _alerts_lock:
        try:
            pending = getattr(work_fuctions, "web_alerts", None)
            if pending:
                alerts_to_send = list(pending)
                pending.clear()
                return {"alerts": alerts_to_send}
        except Exception:
            pass
    return {"alerts": []}


# Приватный каталог под BASE_DIR: /tmp/timer_alert.wav писал любой пользователь,
# и подменённый файл уходил бы клиенту. Новый путь — первым, старый /tmp —
# запасной: work_fuctions.timer_alert пока пишет туда литералом, и ломать его
# нельзя. Когда писатель переедет, запасной путь можно будет убрать.
_ALERT_AUDIO_PATHS = (BASE_DIR / "logs" / "timer_alert.wav", Path("/tmp/timer_alert.wav"))


@app.get("/api/alert_audio")
def get_alert_audio():
    # Отдаем сгенерированный аудиофайл таймера
    for candidate in _ALERT_AUDIO_PATHS:
        try:
            # Симлинк — не наш файл: отдавать по нему что угодно нельзя.
            if candidate.is_symlink():
                continue
            if candidate.is_file() and candidate.stat().st_size > 0:
                return FileResponse(str(candidate), media_type="audio/wav")
        except OSError:
            continue
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
    """Остановить uvicorn и дождаться освобождения порта.

    Глобалы чистим ТОЛЬКО после успешного join: если поток ещё жив, состояние
    оставляем видимым, иначе is_running() начнёт врать («остановлено»).
    """
    global server_instance, _server_thread
    srv, thread = server_instance, _server_thread
    if srv is None:
        _server_thread = None
        return True
    srv.should_exit = True
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
    if thread is not None and thread.is_alive():
        return False
    server_instance = None
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
    """Регистрация телефона. Возвращает client_id и токен — их приложение сохраняет.

    Проверку «повторная регистрация существующего client_id требует его токен»
    выполняет device_control.register_device: он кидает PermissionError, который
    здесь превращается в 403 (а не в 500). Токен НИКОГДА не отдаём тому, кто его
    не предъявил.
    """
    try:
        info = device_control.register_device(
            name=data.name, kind=data.kind, token=data.token or None,
            caps=data.caps, model=data.model, os_version=data.os,
            client_id=data.client_id or None)
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="client_id уже зарегистрирован — повторите с верным токеном устройства")
    except Exception as e:
        _log_web_error("/api/device/register", e)
        raise HTTPException(status_code=500, detail="не удалось зарегистрировать устройство")
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
    """Забыть устройство (например, телефон украли).

    Роут административный: закрыт web_token через middleware. Параметр token
    оставлен для совместимости, но не обязателен — иначе веб-панель (она шлёт
    только X-Luch-Token) получала бы двойной отказ.
    """
    if not client_id:
        raise HTTPException(status_code=400, detail="Не указан client_id устройства")
    return device_control.forget_device(client_id)


@app.post("/api/device/rename")
def device_rename(client_id: str = "", token: str = "", name: str = ""):
    """Переименовать устройство (административный роут, закрыт web_token)."""
    if not client_id:
        raise HTTPException(status_code=400, detail="Не указан client_id устройства")
    return device_control.rename_device(client_id, name)


@app.post("/api/device/location")
def device_location(client_id: str = "", token: str = "", data: LocationModel = None):
    """Телефон прислал свои координаты (аутентификация токеном устройства)."""
    if not client_id:
        raise HTTPException(status_code=400, detail="Не указан client_id устройства")
    _device_auth(client_id, token)
    if data is None:
        raise HTTPException(status_code=400, detail="Не переданы координаты")
    return device_control.report_location(client_id, data.lat, data.lng, data.accuracy)


@app.get("/api/device/queue")
def device_queue(client_id: str = "", token: str = "", since: int = 0, battery: int = -1):
    """Телефон забирает команды для выполнения. Должно вызываться каждые 2-3 секунды."""
    dev = _device_auth(client_id, token)
    device_control.touch(client_id, None if battery < 0 else battery)
    pack = device_control.take(client_id, since)
    return {"status": "ok", "commands": pack["commands"], "seq": pack["seq"],
            # reset нужен телефону, чтобы сбросить свой счётчик выполненных
            # команд при откате серверного счётчика (пересоздан devices.json).
            "reset": pack.get("reset", False),
            "device": device_control.device_info(client_id)}


@app.post("/api/device/result")
def device_result(client_id: str = "", token: str = "", data: DeviceResultModel = None):
    """Телефон отчитывается о выполненной команде. Тело обязательно."""
    if not client_id:
        raise HTTPException(status_code=400, detail="Не указан client_id устройства")
    _device_auth(client_id, token)
    if data is None:
        # Раньше тело было необязательным и падало AttributeError → 500.
        raise HTTPException(status_code=400, detail="Не передано тело результата команды")
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