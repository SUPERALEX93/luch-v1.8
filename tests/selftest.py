#!/usr/bin/env python3
"""Автономная проверка веб-API ЛУЧ без запуска ассистента.

Поднимает приложение web_server через FastAPI TestClient с заглушкой ядра,
поэтому не требует микрофона, моделей Whisper и GPU.

Запуск:
    cd /home/pwbad/proekte/Python/luch/v1.8
    ../ai_env/bin/python tests/selftest.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

import settings  # noqa: E402
import web_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))
    mark = "OK  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail and not condition else ""))


class StubAI:
    """Минимальная заглушка AIconsole: всё, что нужно веб-слою."""

    def __init__(self) -> None:
        self.ai_api_key = ""
        self.ai_base_url = "http://127.0.0.1:9/v1"
        self.ai_timeout = 5.0
        self.ai_model = "stub"
        self.provider = "ollama"
        self.response = ""
        self.response_ready_event = threading.Event()
        self.pending_voice = False
        self.ignore_response = False
        self.stop_audio_triggered = False
        self.user_prompt = None
        self._seq = 0
        self._history: list = []

    # --- методы, которые вызываются по имени и должны что-то возвращать ---

    def ai_config(self):
        return {"provider": "ollama", "provider_label": "Ollama", "active_model": "stub",
                "summary": "stub", "base_url": "", "model": "stub", "timeout": 5,
                "has_key": False, "key_length": 0, "chat_url": "", "models_url": "",
                "ollama_url": "http://localhost:11434", "ollama_model": "stub",
                "ollama_models": [], "zen_model": "", "zen_has_key": False,
                "zen_key_length": 0, "active_custom_id": "", "custom_providers": []}

    def ai_label(self) -> str:
        return "Ollama"

    def web_settings_info(self):
        # настоящая реализация живёт в main.py; здесь повторяем контракт маскировки
        data = {}
        for key, value in settings.settings.items():
            if key in ("zen_api_key", "ai_api_key", "web_token"):
                data[key] = {"hidden": True, "set": bool(value), "length": len(value or "")}
            elif key == "ai_providers":
                masked = []
                for entry in (value or []):
                    item = dict(entry) if isinstance(entry, dict) else entry
                    if isinstance(item, dict) and "api_key" in item:
                        secret = item.get("api_key") or ""
                        item["api_key"] = {"hidden": True, "set": bool(secret),
                                           "length": len(secret)}
                    masked.append(item)
                data[key] = masked
            else:
                data[key] = value
        return data

    def web_command_info(self):
        return {"groups": [], "commands": {}}

    def web_ai_commands(self, *a, **k):
        return {"commands": [], "seq": self._seq}

    def web_ai_command_history(self, limit: int = 30):
        return {"history": self._history[-limit:], "seq": self._seq}

    def web_clear_ai_command_history(self, *a, **k):
        self._history.clear()
        return {"status": "ok"}

    def ai_command_seq(self) -> int:
        return self._seq

    def ai_commands_since(self, seq: int):
        return {"commands": [], "seq": self._seq}

    def web_run_ai_command(self, name, args=None, confirm=False, timeout=None):
        return {"status": "ok", "command": name, "output": "", "confirmed": confirm}

    def web_run_command(self, name, confirm=False):
        return {"status": "ok", "output": ""}

    def process_command(self, cmd):
        return "OK"

    def web_apply_setting(self, key, value):
        return {"status": "ok", "key": key}

    def format_response(self, text):
        return text

    def _normalize_ai_base_url(self, raw):
        return (raw or "").rstrip("/")

    def list_ai_models(self, base_url=None, api_key=None):
        return {"models": []}

    def test_ai_endpoint(self, base_url=None, api_key=None, model=None, text="Привет!"):
        return {"ok": False, "detail": "stub"}

    def apply_ai_endpoint(self, *a, **k):
        return {"status": "ok"}


client = TestClient(web_server.app)
TOKEN = (settings.settings.get("web_token") or "").strip()
AUTH = {"X-Luch-Token": TOKEN}
NOAUTH: dict[str, str] = {}


def main() -> int:
    print("\n=== ЛУЧ: проверка веб-API ===\n")

    print(f"web_token задан: {bool(TOKEN)} (длина {len(TOKEN)})")
    check("web_token не пустой (обязательная аутентификация включена)", bool(TOKEN),
          "settings.py не сгенерировал токен")

    web_server.ai_instance = StubAI()

    # Заглушка сама «отвечает»: ручки /api/ask и /api/voice ждут response_ready_event,
    # без этого тест висел бы полную минуту на каждом таком запросе.
    _stub = web_server.ai_instance
    _stub.response = "заглушка"
    _keep = threading.Event()

    def _answer_loop() -> None:
        while not _keep.is_set():
            _stub.response_ready_event.set()
            time.sleep(0.05)

    threading.Thread(target=_answer_loop, daemon=True).start()

    print("\n-- Публичные маршруты --")
    r = client.get("/")
    check("GET / доступен без токена", r.status_code == 200, f"код {r.status_code}")
    r = client.get("/api/health")
    check("GET /api/health доступен без токена", r.status_code == 200, f"код {r.status_code}")
    if r.status_code == 200:
        body = r.text
        check("GET /api/health не содержит секретов",
              TOKEN not in body and "api_key" not in body.lower(),
              "в ответе /api/health есть токен или ключ")
    r = client.get("/docs")
    check("GET /docs отключён (нет автодокументации)", r.status_code == 404,
          f"код {r.status_code} — документация открыта")
    r = client.get("/openapi.json")
    check("GET /openapi.json отключён", r.status_code == 404, f"код {r.status_code}")

    print("\n-- Защищённые маршруты БЕЗ токена (ожидаем 401) --")
    for method, path in [
        ("get", "/api/settings"),
        ("get", "/api/ai/config"),
        ("get", "/api/audio"),
        ("get", "/api/alerts"),
        ("get", "/api/alert_audio"),
        ("get", "/api/menu"),
        ("get", "/api/state"),
        ("get", "/api/location"),
        ("get", "/api/location/history"),
        ("get", "/api/navigator/status"),
        ("get", "/api/device/list"),
        ("post", "/api/ask"),
        ("post", "/api/command"),
        ("post", "/api/settings"),
        ("post", "/api/menu/run"),
        ("post", "/api/location"),
        ("post", "/_selftest"),
    ]:
        if method == "get":
            r = client.get(path, headers=NOAUTH)
        else:
            r = client.post(path, headers=NOAUTH, json={})
        check(f"{method.upper()} {path} без токена → 401", r.status_code == 401,
              f"код {r.status_code}")

    print("\n-- Защищённые маршруты С токеном (ожидаем не 401) --")
    for method, path, payload in [
        ("get", "/api/settings", None),
        ("get", "/api/ai/config", None),
        ("get", "/api/menu", None),
        ("get", "/api/state", None),
        ("post", "/api/ask", {"text": "привет"}),
    ]:
        if method == "get":
            r = client.get(path, headers=AUTH)
        else:
            r = client.post(path, headers=AUTH, json=payload)
        check(f"{method.upper()} {path} с токеном → не 401", r.status_code != 401,
              f"код {r.status_code}")

    print("\n-- Устройства: свой токен, веб-токен не нужен --")
    r = client.post("/api/device/register", headers=NOAUTH,
                    json={"name": "Тест", "kind": "android",
                          "caps": ["location", "notify", "vibrate"]})
    check("POST /api/device/register без веб-токена работает (401 нет)",
          r.status_code != 401, f"код {r.status_code}")
    check("POST /api/device/register → 200", r.status_code == 200, f"код {r.status_code}")

    if r.status_code == 200:
        info = r.json()
        cid, dev_token = info.get("client_id"), info.get("token")
        check("регистрация вернула client_id и token", bool(cid) and bool(dev_token))

        # Повторная регистрация СВОИМ токеном должна работать (так делает Android)
        r2 = client.post("/api/device/register", headers=NOAUTH,
                         json={"name": "Тест", "kind": "android", "client_id": cid,
                               "token": dev_token, "caps": ["location", "notify", "vibrate"]})
        check("повторная регистрация со своим токеном → 200", r2.status_code == 200,
              f"код {r2.status_code}")

        # Повторная регистрация БЕЗ токена не должна выдавать чужой токен
        r3 = client.post("/api/device/register", headers=NOAUTH,
                         json={"name": "Взлом", "kind": "android", "client_id": cid})
        leaked = False
        if r3.status_code == 200:
            leaked = bool((r3.json() or {}).get("token"))
        check("повторная регистрация без токена НЕ выдаёт токен устройства",
              r3.status_code == 403 and not leaked,
              f"код {r3.status_code}, token выдан: {leaked}")

        # /api/voice: телефон не знает web_token, он аутентифицируется токеном устройства
        fake_wav = ("t.wav", b"RIFF0000WAVEfmt ", "audio/wav")
        rv = client.post("/api/voice", headers=NOAUTH, files={"file": fake_wav})
        check("POST /api/voice вообще без токена → 401", rv.status_code == 401,
              f"код {rv.status_code} — голос с телефона не защищён")

        rv2 = client.post("/api/voice", headers=NOAUTH,
                          params={"client_id": cid, "token": dev_token},
                          files={"file": fake_wav})
        check("POST /api/voice с токеном устройства → не 401",
              rv2.status_code != 401, f"код {rv2.status_code}")

        # Очередь обязана отдавать reset: телефон сбрасывает по нему свой счётчик
        rq = client.get("/api/device/queue", headers=NOAUTH,
                        params={"client_id": cid, "token": dev_token, "since": 0})
        if rq.status_code == 200:
            check("GET /api/device/queue возвращает поле reset",
                  "reset" in (rq.json() or {}), "поле reset отсутствует")
        else:
            check("GET /api/device/queue с токеном устройства → 200", False,
                  f"код {rq.status_code}")

    r = client.post("/api/device/location", headers=NOAUTH, json={"lat": 1.0, "lng": 2.0})
    check("POST /api/device/location без client_id → 400 (а не 200 с ошибкой)",
          r.status_code == 400, f"код {r.status_code}")

    r = client.post("/api/device/result", headers=NOAUTH)
    check("POST /api/device/result без тела → не 500", r.status_code < 500,
          f"код {r.status_code} (AttributeError на data.cmd_id)")

    print("\n-- Утечка секретов через /api/settings --")
    settings.settings.setdefault("ai_providers", [])
    settings.settings["ai_providers"] = [{
        "id": "test", "name": "test", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "SUPER_SECRET_KEY_123", "model": "m", "timeout": 5,
    }]
    r = client.get("/api/settings", headers=AUTH)
    if r.status_code == 200:
        body = r.text
        check("GET /api/settings не отдаёт ключ провайдера открытым текстом",
              "SUPER_SECRET_KEY_123" not in body,
              "ключ ai_providers[].api_key виден в ответе")
        check("GET /api/settings не отдаёт zen_api_key открытым текстом",
              (settings.settings.get("zen_api_key") or "###") not in body,
              "zen_api_key виден в ответе")
        check("GET /api/settings не отдаёт web_token открытым текстом",
              "web_token" not in body or TOKEN not in body,
              "web_token виден в ответе")
    else:
        check("GET /api/settings с токеном → 200", False, f"код {r.status_code}")

    print("\n-- Итог --")
    print(f"  пройдено: {len(PASSED)}")
    print(f"  провалено: {len(FAILED)}")
    if FAILED:
        print("\n  Проваленные проверки:")
        for name, detail in FAILED:
            print(f"    - {name}" + (f"  ({detail})" if detail else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"\nСтенд упал с исключением: {exc}")
        sys.exit(2)
