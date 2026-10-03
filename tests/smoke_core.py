#!/usr/bin/env python3
"""Headless-проверка ЯДРА ЛУЧ без микрофона, GPU и загрузки моделей.

Подменяет железо и тяжёлые модели заглушками и реально создаёт `AIconsole`,
после чего проверяет ключевые ветки логики. Это проверка работоспособности,
а не только синтаксиса: если в `__init__` или в проверяемых методах осталась
ошибка, тест упадёт.

Запуск:
    cd /home/pwbad/proekte/Python/luch/v1.8
    ../ai_env/bin/python tests/smoke_core.py
"""

from __future__ import annotations

import contextlib
import re
import tempfile
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

import main  # noqa: E402
import settings  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))
    print(f"  [{'OK  ' if condition else 'FAIL'}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


# ───────────────────────── заглушки железа и моделей ─────────────────────────

class _FakeResponse:
    def json(self):
        return {"models": []}


class _FakeVerifier:
    """Заглушка SpeakerRecognition: проверка голоса всегда «свой»."""

    @classmethod
    def from_hparams(cls, **kwargs):
        return cls()

    def verify_batch(self, profile, sample):
        import torch
        return torch.tensor(0.99), torch.tensor(True)


class _FakeTTS:
    def __init__(self, **kwargs):
        self.device = kwargs.get("device", "cpu")

    def tts(self, *a, **k):
        return None


class _FakeWhisper:
    def transcribe(self, *a, **k):
        return [], None


@contextlib.contextmanager
def _fake_microphone(device_index=None, **kwargs):
    yield object()


def prepare_profile() -> str:
    """Готовит синтетический эталон голоса во временном файле.

    Без микрофона записать эталон нечем, а `AIconsole` без него честно
    оставляет проверку голоса выключенной. Чтобы проверка была честной,
    а не зависела от того, что файл случайно остался от прошлого запуска,
    кладём рядом сгенерированный WAV и уводим путь в temp.
    """
    import numpy as np
    import soundfile as wav

    path = Path(tempfile.gettempdir()) / "luch-smoke-voice-profile.wav"
    # main.py читает эталон как int16 и делит на 32768 — пишем именно PCM_16.
    tone = (np.sin(np.arange(16000 * 3) * 0.01) * 3000).astype("int16")
    wav.write(str(path), tone, 16000, subtype="PCM_16")
    return str(path)


def install_stubs() -> None:
    main.quiet_native = lambda what="": contextlib.nullcontext()
    main.open_microphone = _fake_microphone
    main.SpeakerRecognition = _FakeVerifier
    main.SileroTTS = _FakeTTS
    main.load_whisper = lambda model_name: _FakeWhisper()
    main.tts_device = lambda prefer=None: "cpu"
    main.torch_device = lambda: "cpu"
    main.requests.get = lambda *a, **k: _FakeResponse()
    # Микрофон не трогаем: настройка шумового порога не нужна.
    main.sr.Recognizer.adjust_for_ambient_noise = lambda self, source, duration=1: None


def main_test() -> int:
    print("\n=== ЛУЧ: headless-проверка ядра ===\n")
    install_stubs()

    print("-- создание AIconsole --")
    profile_path = prepare_profile()
    main.settings.PATHS["voice_profile_path"] = profile_path
    t0 = time.time()
    try:
        ai = main.AIconsole()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("AIconsole создаётся без железа и моделей", False, repr(exc))
        return 1
    check("AIconsole создаётся без железа и моделей", True)
    print(f"     (за {time.time() - t0:.2f} с)\n")

    print("-- состояние после инициализации --")
    check("profile_tensor загружен", ai.profile_tensor is not None)
    check("voice_verifier доступен", ai.voice_verifier is not None)
    # Локальная модель грузится только при stt_mode="whisper". В режиме google
    # (он же по умолчанию) её отсутствие — это норма, а не поломка.
    if ai.stt_mode == "whisper":
        check("whisper-модель подставлена", ai.whisper_load_model is not None)
    else:
        check("whisper не нужен в режиме google", ai.whisper_load_model is None)
    check("tts создан", ai.tts is not None)
    check("menu_layout заполнен", bool(ai.menu_layout))
    check("user_commands заполнен", bool(ai.user_commands))
    check("колбэк таймеров привязан",
          main.work_fuctions.tts_callback is not None,
          "work_fuctions.tts_callback не выставлен")
    check("_exit_event создан", hasattr(ai, "_exit_event"))
    check("_state_lock создан", hasattr(ai, "_state_lock"))
    check("механизма подтверждения нет", not hasattr(ai, "_confirm_lock")
          and not hasattr(ai, "pending_confirm")
          and not hasattr(ai, "_handle_pending_confirm"))

    print("\n-- должно быть удалено --")
    check("_strip_ansi удалён", not hasattr(ai, "_strip_ansi"))
    check("мёртвых атрибутов нет",
          not any(hasattr(ai, a) for a in ("sound", "listen_while_ai_speak_flag",
                                           "default_settings")))

    print("\n-- гейт опасных команд --")
    check("terminal опасна", ai._is_dangerous("terminal") is True)
    check("lock_pc опасна", ai._is_dangerous("lock_pc") is True)
    check("web_search не опасна", ai._is_dangerous("web_search") is False)
    check("неизвестная команда не опасна", ai._is_dangerous("нет_такой") is False)

    print("\n-- process_command: веб против консоли --")
    # interactive-команда из веба должна быть отклонена, а не заблокировать поток
    started = time.time()
    res_web = ai.process_command("/ai", from_web=True)
    elapsed = time.time() - started
    check("интерактивная команда из веба отклоняется",
          isinstance(res_web, str) and "недоступна из веба" in res_web, f"вернулось {res_web!r}")
    check("отклонение мгновенное (поток не заблокирован)", elapsed < 2.0,
          f"заняло {elapsed:.1f} с — похоже на input()")

    check("неизвестная команда распознана",
          ai.process_command("/нетакой", from_web=False) == "UNKNOWN COMMAND")

    # /exit и /restart_server заведены именно для веба — гейт их не должен трогать
    check("instant-команда разрешена из веба",
          main.AIconsole.COMMAND_MODES.get("help") == "instant")
    for cmd_name in ("exit", "restart_server"):
        mode = main.AIconsole.COMMAND_MODES.get(cmd_name)
        check(f"/{cmd_name} (режим {mode}) не блокируется вебом", mode != "interactive",
              "режим interactive — команда не сможет работать с сайта")

    # /exit не должен ронять поток, но обязан выставить should_exit
    ai.should_exit = False
    ai._exit_event.clear()
    res_exit = ai.process_command("/exit", from_web=True)
    check("/exit из веба не бросает SystemExit", res_exit == "EXITING", f"{res_exit!r}")
    check("/exit выставляет should_exit", ai.should_exit is True)
    check("should_exit взводит событие", ai._exit_event.is_set())

    print("\n-- маскировка секретов --")
    info = ai.web_settings_info()
    check("zen_api_key замаскирован",
          isinstance(info.get("zen_api_key"), dict) and info["zen_api_key"].get("hidden") is True)
    check("web_token замаскирован",
          isinstance(info.get("web_token"), dict) and info["web_token"].get("hidden") is True)
    providers = info.get("ai_providers")
    check("ai_providers не отдан открытым текстом", isinstance(providers, list))
    if isinstance(providers, list) and providers:
        first = providers[0]
        check("вложенный api_key замаскирован",
              isinstance(first.get("api_key"), dict) and first["api_key"].get("hidden") is True,
              f"api_key = {first.get('api_key')!r}")
    check("приватные поля провайдера не утекли",
          "SUPER_SECRET" not in str(info) and bool(settings.settings.get("zen_api_key", "")) is False
          or settings.settings.get("zen_api_key", "") not in str(info),
          "ключ виден в web_settings_info()")

    print("\n-- разбор документации команд --")
    docs = ai._ai_command_docs()
    phone_docs = [d for d in docs if str(d.get("name", "")).startswith("phone_")]
    check("описания phone_* распознаны (регресс регэкспа 0a.)",
          len(phone_docs) >= 10, f"найдено {len(phone_docs)}")
    check("у команд есть непустые описания",
          all(d.get("desc") for d in docs[:5]),
          "часть описаний пуста")

    print("\n-- системный промпт --")
    prompt = ai._build_system_prompt()
    check("системный промпт не пуст", bool(prompt and prompt.strip()))
    check("промпт содержит триггер-слово", str(ai.trigger_word) in prompt)

    print("\n-- подтверждение опасной команды --")
    # Ничего реально не выполняем и не озвучиваем: подменяем исполнителя и речь.
    executed: list[tuple] = []
    ai._invoke_ai_command = lambda name, kwargs, limit: (
        executed.append((name, kwargs)) or ("ok", "выполнено", None, 0.01))
    ai.speak_text = lambda text: None

    # Подтверждения опасных команд больше нет: команда уходит сразу.
    check("опасная команда выполняется без confirm",
          ai.web_run_ai_command("terminal", {"command": "true"})["status"] == "ok",
          "веб всё ещё требует подтверждения")
    check("выполнена именно та команда",
          bool(executed) and executed[0][0] == "terminal", f"{executed}")
    # Пункт меню берём заведомо безопасный: у пункта в режиме «опасно»
    # web_run_command сам запускает выход из программы, в тесте это лишнее.
    safe = next((n for n, m in ai.COMMAND_MODES.items()
                 if m == "instant" and n in ai.user_commands
                 and ai.user_commands[n][0] is not None), None)
    check("веб-пункт меню выполняется без confirm",
          safe is not None and ai.web_run_command(safe)["status"] == "ok",
          "меню всё ещё требует подтверждения")
    check("confirm_dangerous больше не в настройках",
          "confirm_dangerous" not in main.settings.default_settings)

    # Голосовой путь: команда тоже уходит сразу, без ожидания ответа.
    executed.clear()
    rc = ai.parse_ai_command('command {"command": "terminal", "args": {"command": "true"}}')
    check("из голоса опасная команда выполняется сразу",
          len(executed) == 1 and executed[0][0] == "terminal", f"выполнено: {executed}")
    check("голосовая команда не уходит в ожидание подтверждения",
          rc == 0 and not hasattr(ai, "pending_confirm"), f"вернулось {rc!r}")

    print("\n-- валидация аргументов ИИ-команд --")
    # terminal(command, timeout=60) — command обязателен
    try:
        ai._bind_ai_args("terminal", {})
        check("пустые аргументы при обязательном параметре → ошибка", False,
              "ValueError не поднят: команда упала бы TypeError внутри")
    except ValueError as exc:
        check("пустые аргументы при обязательном параметре → ошибка",
              "нужны аргументы" in str(exc), f"{exc}")

    try:
        ai._bind_ai_args("terminal", {"command": "ls", "таймаут": 5})
        check("частично незнакомые аргументы → ошибка", False,
              "раскладка по порядку подставила бы мусор в чужой параметр")
    except ValueError as exc:
        check("частично незнакомые аргументы → ошибка", "нет аргументов" in str(exc), f"{exc}")

    # обратная совместимость: старый позиционный стиль (ни одного знакомого имени)
    try:
        positional = ai._bind_ai_args("terminal", {"команда": "ls"})
        check("старый позиционный стиль ещё работает", positional == {"command": "ls"},
              f"{positional}")
    except ValueError as exc:
        check("старый позиционный стиль ещё работает", False, f"{exc}")

    try:
        bound = ai._bind_ai_args("terminal", {"command": "ls"})
        check("верные имена аргументов принимаются", bound == {"command": "ls"}, f"{bound}")
    except ValueError as exc:
        check("верные имена аргументов принимаются", False, f"{exc}")

    sig = {a["name"]: a for a in ai._ai_command_signature("phone_vibrate")}
    if "ms" in sig:
        check("тип числового аргумента не выдаётся за str",
              sig["ms"]["type"] == "int", f"type={sig['ms']['type']!r}")
    else:
        check("phone_vibrate найден в реестре", False, f"аргументы: {list(sig)}")

    # parse_ai_command обязан пережить неверные аргументы и записать ошибку
    history_before = len(ai.web_ai_command_history(limit=1000))
    rc = ai.parse_ai_command('command {"command": "terminal", "args": {}}')
    history_after = len(ai.web_ai_command_history(limit=1000))
    check("parse_ai_command переживает неверные аргументы", rc == 0, f"вернулось {rc!r}")
    check("ошибка аргументов попала в журнал", history_after > history_before,
          f"было {history_before}, стало {history_after}")

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
        sys.exit(main_test())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"\nТест упал с исключением: {exc}")
        sys.exit(2)
