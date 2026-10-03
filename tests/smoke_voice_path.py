#!/usr/bin/env python3
"""Интеграционная проверка ГОЛОСОВОГО ПУТИ ЛУЧ (без микрофона и моделей).

Прогоняет `listen_user(from_file=True)` на синтетическом WAV с заглушками
распознавания и проверки голоса: триггер-слово, его отсечение, игнорирование
фразы без триггера и запуск конвейера ответа.

Запуск:
    cd /home/pwbad/proekte/Python/luch/v1.8
    ../ai_env/bin/python tests/smoke_voice_path.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

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


def make_wav(path: Path, seconds: float = 2.0, rate: int = 16000) -> None:
    """Синтетический WAV: тон с речеподобной огибающей, лишь бы читался."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    signal = (0.3 * np.sin(2 * np.pi * 180 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)))
    data = (signal * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


# ───────────────────────────── заглушки ─────────────────────────────

class _FakeResponse:
    def json(self):
        return {"models": []}


class _FakeVerifier:
    own_voice = True
    score = 0.99

    @classmethod
    def from_hparams(cls, **kwargs):
        return cls()

    def verify_batch(self, profile, sample):
        import torch
        return (torch.tensor(self.score),
                torch.tensor(type(self).own_voice))


class _FakeTTS:
    def __init__(self, **kwargs):
        self.device = "cpu"

    def tts(self, *a, **k):
        return None


class _Segment:
    def __init__(self, text: str):
        self.text = text


class _FakeWhisper:
    heard = "луч привет"

    def transcribe(self, *a, **k):
        return [_Segment(type(self).heard)], None


def install_stubs(main) -> None:
    import contextlib

    @contextlib.contextmanager
    def _mic(device_index=None, **kwargs):
        yield object()

    main.quiet_native = lambda what="": contextlib.nullcontext()
    main.open_microphone = _mic
    main.SpeakerRecognition = _FakeVerifier
    main.SileroTTS = _FakeTTS
    main.load_whisper = lambda model_name: _FakeWhisper()
    main.tts_device = lambda prefer=None: "cpu"
    main.torch_device = lambda: "cpu"
    main.requests.get = lambda *a, **k: _FakeResponse()
    main.sr.Recognizer.adjust_for_ambient_noise = lambda self, source, duration=1: None


def build_instance(main):
    """Создать AIconsole с заглушками и подменить конвейер ответа."""
    install_stubs(main)
    ai = main.AIconsole()

    calls: list[str] = []

    def _fake_release():
        calls.append("full_relese_response")

    ai.full_relese_response = _fake_release
    ai.whisper_load_model = _FakeWhisper()
    ai.stt_mode = "whisper"
    return ai, calls


def main_test() -> int:
    print("\n=== ЛУЧ: проверка голосового пути ===\n")

    import main as core

    # Ничего не пишем в рабочие файлы проекта.
    tmpdir = Path(tempfile.mkdtemp(prefix="luch_voice_"))
    settings.PATHS["temp_wav_file"] = str(tmpdir / "temp.wav")
    settings.PATHS["voice_profile_path"] = str(tmpdir / "profile.wav")
    make_wav(Path(settings.PATHS["temp_wav_file"]))
    make_wav(Path(settings.PATHS["voice_profile_path"]))

    ai, calls = build_instance(core)

    check("тестовый WAV создан", Path(settings.PATHS["temp_wav_file"]).exists())
    check("триггер-слово задано", bool(ai.trigger_word), f"trigger_word={ai.trigger_word!r}")

    print("\n-- фраза с триггер-словом --")
    _FakeVerifier.own_voice = True
    _FakeWhisper.heard = f"{ai.trigger_word} привет"
    ai.user_prompt = None
    ai.should_exit = False
    ai.listen_user(from_file=True)
    check("конвейер ответа запущен", calls == ["full_relese_response"], f"вызовы: {calls}")
    check("триггер-слово отсечено от запроса", ai.user_prompt == "привет",
          f"user_prompt={ai.user_prompt!r}")
    check("голос признан своим", ai.is_voice_success is True)
    check("событие готовности взведено", ai.response_ready_event.is_set())

    print("\n-- фраза БЕЗ триггер-слова --")
    calls.clear()
    ai.user_prompt = None
    ai.response_ready_event.clear()
    ai.ignore_response = False
    _FakeWhisper.heard = "просто болтовня без обращения"
    ai.listen_user(from_file=True)
    check("без триггера конвейер НЕ запускается", calls == [], f"вызовы: {calls}")
    check("фраза помечена как проигнорированная", ai.ignore_response == "trigger",
          f"ignore_response={ai.ignore_response!r}")

    print("\n-- триггер без текста --")
    calls.clear()
    ai.user_prompt = None
    ai.response_ready_event.clear()
    _FakeWhisper.heard = ai.trigger_word
    ai.listen_user(from_file=True)
    check("один триггер превращается в «Привет!»", ai.user_prompt == "Привет!",
          f"user_prompt={ai.user_prompt!r}")

    print("\n-- пустое распознавание --")
    calls.clear()
    ai.user_prompt = None
    ai.response_ready_event.clear()
    _FakeWhisper.heard = ""
    ai.listen_user(from_file=True)
    check("пустая фраза ничего не ломает", ai.user_prompt is None,
          f"user_prompt={ai.user_prompt!r}")

    print("\n-- чужой голос (файл отказов не трогаем) --")
    calls.clear()
    ai.user_prompt = None
    ai.response_ready_event.clear()
    _FakeWhisper.heard = f"{ai.trigger_word} открой дверь"
    _FakeVerifier.own_voice = False
    # Перехватываем запись в файл отказов, чтобы не мусорить в рабочем проекте.
    import builtins

    real_open = builtins.open
    intercepted: list[str] = []

    class _Sink:
        def write(self, s):
            intercepted.append(s)
            return len(s)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _patched_open(file, *a, **k):
        if "phrases_not_spoken" in str(file):
            return _Sink()
        return real_open(file, *a, **k)

    builtins.open = _patched_open
    try:
        ai.listen_user(from_file=True)
    finally:
        builtins.open = real_open
    check("чужой голос НЕ запускает конвейер", calls == [], f"вызовы: {calls}")
    check("доступ запрещён", ai.ignore_response == "voice", f"ignore_response={ai.ignore_response!r}")
    check("фраза попала в приёмник вместо рабочего файла", intercepted != [],
          "запись в файл отказов не перехвачена")

    print("\n-- нет файла записи --")
    missing = Path(tmpdir) / "нет_такого.wav"
    settings.PATHS["temp_wav_file"] = str(missing)
    ai.response = ""
    ai.response_ready_event.clear()
    ai.listen_user(from_file=True)
    check("отсутствие файла обработано без падения",
          "не найден" in (ai.response or "").lower(), f"response={ai.response!r}")

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
        print(f"\nСтенд упал с исключением: {exc}")
        sys.exit(2)
