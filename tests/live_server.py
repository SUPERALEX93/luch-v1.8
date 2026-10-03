#!/usr/bin/env python3
"""Живая проверка веб-сервера ЛУЧ: настоящий uvicorn, настоящий HTTP.

В отличие от `selftest.py` (TestClient внутри процесса) этот стенд поднимает
РЕАЛЬНЫЙ сервер на свободном порту 13999 и ходит по нему по сети, проверяя
запуск, middleware, отдачу статики с подстановкой версии сборки и остановку.

Ядро подменяется заглушкой — микрофон, Whisper и GPU не нужны. TLS отключён
(LUCH_HTTPS=0), сервер живёт несколько секунд и не трогает порт 1337, поэтому
уже запущенному ассистенту он не мешает.

Запуск:
    cd /home/pwbad/proekte/Python/luch/v1.8
    ../ai_env/bin/python tests/live_server.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)
os.environ["LUCH_HTTPS"] = "0"          # TLS не нужен: проверяем HTTP-слой
os.environ["LUCH_CORS_ORIGINS"] = ""

import requests  # noqa: E402

PORT = 13999
BASE = f"http://127.0.0.1:{PORT}"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))
    print(f"  [{'OK  ' if condition else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


class StubAI:
    """Минимум, нужный веб-слою."""

    def __init__(self) -> None:
        self.ai_api_key = ""
        self.ai_base_url = ""
        self.ai_model = "stub"
        self.provider = "ollama"
        self.response = ""
        self.response_ready_event = threading.Event()
        self.pending_voice = False
        self.ignore_response = False
        self.stop_audio_triggered = False
        self.user_prompt = None

    def ai_config(self):
        return {"provider": "ollama", "provider_label": "Ollama (заглушка)",
                "active_model": "stub", "summary": "stub", "base_url": "", "model": "stub",
                "timeout": 5, "has_key": False, "key_length": 0, "chat_url": "",
                "models_url": "", "ollama_url": "", "ollama_model": "stub",
                "ollama_models": [], "zen_model": "", "zen_has_key": False,
                "zen_key_length": 0, "active_custom_id": "", "custom_providers": []}

    def ai_label(self) -> str:
        return "Ollama (заглушка)"

    def web_settings_info(self):
        return {"trigger_word": "луч", "web_token": {"hidden": True, "set": True, "length": 32}}

    def web_command_info(self):
        return {"groups": []}

    def _normalize_ai_base_url(self, raw):
        return (raw or "").rstrip("/")

    def list_ai_models(self, base_url=None, api_key=None):
        return [], ""

    def __getattr__(self, name):
        # Всё остальное, что может дёрнуть веб-слой, — безопасная заглушка.
        def _stub(*args, **kwargs):
            return {}
        return _stub


def wait_until_up(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE}/api/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.25)
    return False


def main() -> int:
    print("\n=== ЛУЧ: живая проверка веб-сервера ===\n")

    import settings
    import device_control
    import web_server

    # Живой стенд не должен трогать рабочий реестр устройств.
    device_control.DEVICES_FILE = Path(tempfile.mkdtemp(prefix="luch_live_")) / "devices.json"
    with device_control.devices_lock:
        device_control.devices.clear()
        device_control.queue.clear()
        device_control._acked.clear()
        device_control.results.clear()

    token = (settings.settings.get("web_token") or "").strip()
    check("web_token задан", bool(token))

    web_server.ai_instance = StubAI()

    print("-- запуск реального сервера --")
    thread = threading.Thread(target=web_server.start_server_in_thread,
                              args=(web_server.ai_instance, PORT), daemon=True)
    thread.start()
    up = wait_until_up()
    check(f"сервер поднялся на {BASE}", up, "не ответил за 20 секунд")
    if not up:
        return 1

    auth = {"X-Luch-Token": token}

    print("\n-- реальные HTTP-запросы --")
    r = requests.get(f"{BASE}/", timeout=5)
    check("GET / отдаёт страницу", r.status_code == 200 and "html" in r.text.lower(),
          f"код {r.status_code}")

    r = requests.get(f"{BASE}/api/health", timeout=5)
    check("GET /api/health отвечает без токена", r.status_code == 200, f"код {r.status_code}")
    check("/api/health не содержит токен",
          token not in r.text, "токен попал в /api/health")

    r = requests.get(f"{BASE}/api/settings", timeout=5)
    check("GET /api/settings без токена → 401", r.status_code == 401, f"код {r.status_code}")

    r = requests.get(f"{BASE}/api/settings", headers=auth, timeout=5)
    check("GET /api/settings с токеном → 200", r.status_code == 200, f"код {r.status_code}")

    r = requests.get(f"{BASE}/api/settings", headers={"X-Luch-Token": "wrong-token"}, timeout=5)
    check("неверный токен → 401 (не 500)", r.status_code == 401, f"код {r.status_code}")

    # Не-ASCII токен: HTTP-заголовки латино-1, и байты >127 не должны ронять сервер
    # в 500 — hmac.compare_digest со str падал бы TypeError.
    r = requests.get(f"{BASE}/api/settings", headers={"X-Luch-Token": "caf\xe9"}, timeout=5)
    check("не-ASCII токен → 401 (не 500)", r.status_code == 401,
          f"код {r.status_code} — сервер упал на не-ASCII токене")

    r = requests.get(f"{BASE}/docs", timeout=5)
    check("/docs закрыт", r.status_code == 404, f"код {r.status_code}")

    print("\n-- статика и подстановка версии сборки --")
    r = requests.get(f"{BASE}/static/app.js", timeout=10)
    check("GET /static/app.js отдаётся", r.status_code == 200, f"код {r.status_code}")
    check("__BUILD__ подставлен версией приложения", "__BUILD__" not in r.text,
          "в app.js остался плейсхолдер __BUILD__")
    check("app.js не кэшируется намертво",
          "no-store" in (r.headers.get("Cache-Control") or ""),
          f"Cache-Control: {r.headers.get('Cache-Control')}")

    r = requests.get(f"{BASE}/static/styles.css", timeout=10)
    check("GET /static/styles.css отдаётся", r.status_code == 200, f"код {r.status_code}")
    check("styles.css кэшируется (не no-store)",
          "no-store" not in (r.headers.get("Cache-Control") or ""),
          f"Cache-Control: {r.headers.get('Cache-Control')}")

    print("\n-- устройство регистрируется по своему токену --")
    r = requests.post(f"{BASE}/api/device/register", timeout=5,
                      json={"name": "Живой тест", "kind": "android",
                            "caps": ["location", "notify", "vibrate"]})
    check("POST /api/device/register → 200", r.status_code == 200, f"код {r.status_code}")
    dev = (r.json() if r.status_code == 200 else {}) or {}
    check("регистрация вернула client_id и token",
          bool(dev.get("client_id")) and bool(dev.get("token")))

    if dev.get("client_id"):
        r = requests.get(f"{BASE}/api/device/queue", timeout=5,
                         params={"client_id": dev["client_id"], "token": dev["token"], "since": 0})
        check("GET /api/device/queue по токену устройства → 200",
              r.status_code == 200, f"код {r.status_code}")
        check("в очереди есть поле reset",
              "reset" in ((r.json() if r.status_code == 200 else {}) or {}))

    print("\n-- остановка сервера --")
    web_server.stop_server()
    time.sleep(1.0)
    alive = False
    try:
        requests.get(f"{BASE}/api/health", timeout=1.0)
        alive = True
    except requests.RequestException:
        alive = False
    check("сервер остановлен и порт освобождён", not alive, "сервер всё ещё отвечает")

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
