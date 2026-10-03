import os
import re
import io
import inspect
import warnings
import tempfile
import contextlib
import shutil
import sys
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
os.environ["LOGURU_LEVEL"] = "ERROR"
os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")
import logging
logging.getLogger().setLevel(logging.ERROR)
for name in ["torch", "torchaudio", "urllib3", "requests", "speechbrain", "silero_tts"]:
    logging.getLogger(name).setLevel(logging.ERROR)
    logging.getLogger(name).propagate = False
import torch
import importlib
import ollama
import json
import time
import subprocess
from art import *
from colorama import Fore, Back, Style
import colorama
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.box import ROUNDED, SIMPLE, DOUBLE
from rich.theme import Theme
import threading
from pathlib import Path
import requests
import speech_recognition as sr
from faster_whisper import WhisperModel,available_models
from silero_tts.silero_tts import SileroTTS
from datetime import datetime
import pygame
import numpy as np
from python_speech_features import mfcc
from scipy.spatial.distance import cosine
import scipy.io.wavfile as wav
import torchaudio.transforms as T
from colorama import Fore, Style, init
import torch
from ollama import chat
import settings
import work_fuctions
import web_server
import console_ui

BASE_DIR = Path(__file__).resolve().parent


@contextlib.contextmanager
def quiet_native(what=""):
    """Спрятать вывод C/C++ библиотек (ALSA, JACK, ctranslate2) в журнал.

    Эти библиотеки пишут прямо в файловый дескриптор 2, мимо sys.stderr,
    поэтому обычный перехват не помогает. После блока дескриптор возвращается,
    так что трейсбэки Python по-прежнему видны в консоли.
    """
    path = work_fuctions.LOG_DIR / "native.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        log = open(path, "a", encoding="utf-8", errors="replace")
    except Exception:
        yield
        return
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    saved = os.dup(2)
    try:
        os.dup2(log.fileno(), 2)
        if what:
            sys.stderr.write(f"--- {what} ---\n")
            sys.stderr.flush()
        yield
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(saved, 2)
        os.close(saved)
        log.close()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > 2000:
                path.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8", errors="replace")
        except Exception:
            pass


@contextlib.contextmanager
def open_microphone(device_index=None, **kwargs):
    """Микрофон без шума ALSA/JACK в консоли.

    Глушим на всё время блока, а не только на создание объекта: сам поток
    записи ALSA открывает уже при recognizer.record()/adjust_for_ambient_noise.
    """
    with quiet_native(f"микрофон {device_index}"):
        with sr.Microphone(device_index=device_index, **kwargs) as source:
            yield source


def list_microphones():
    """Список микрофонов без шума ALSA/JACK в консоли."""
    with quiet_native("список микрофонов"):
        return sr.Microphone.list_microphone_names()


def whisper_device():
    """Видеоускоритель для Whisper: 'cuda' или 'cpu' (ctranslate2 такой формат)."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float32"
    except Exception:
        pass
    return "cpu", "float32"


def torch_device():
    """Строка устройства для torch/speechbrain: там нужен формат 'cuda:0'."""
    try:
        if torch.cuda.is_available():
            return "cuda:0"
    except Exception:
        pass
    return "cpu"


def tts_device(prefer=None):
    """Устройство для SileroTTS: 'cuda' при наличии видеокарты, иначе 'cpu'.

    В настройках tts_device: 'auto' (видеокарта, если есть), 'cuda' (только
    видеокарта, с откатом на cpu) или 'cpu' (всегда процессор).
    prefer перекрывает настройку — нужно при применении нового значения,
    которое ещё не записано в settings.
    """
    want = (prefer or settings.settings.get("tts_device") or "auto").strip().lower()
    if want == "cpu":
        return "cpu"
    try:
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def load_whisper(model_name):
    """Загрузить модель Whisper, спрятав служебный вывод ctranslate2.

    Если видеопамять занята (например, уже запущен другой экземпляр),
    не падаем, а работаем на процессоре.
    """
    device, compute_type = whisper_device()
    with quiet_native(f"whisper {model_name} на {device}"):
        try:
            return WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception:
            if device == "cpu":
                raise
    print(Fore.YELLOW + " ⚠ Не удалось загрузить Whisper на видеокарту — считаю на процессоре"
          + Style.RESET_ALL)
    with quiet_native(f"whisper {model_name} на cpu"):
        return WhisperModel(model_name, device="cpu", compute_type="float32")


colorama.init()
with quiet_native("pygame"):
    pygame.init()
    pygame.mixer.init()

import torchaudio
if not hasattr(torchaudio, 'list_audio_backends'):
    torchaudio.list_audio_backends = lambda: ['soundfile']

from speechbrain.inference.speaker import SpeakerRecognition
from speechbrain.utils.fetching import LocalStrategy

class AIconsole:
    # Как пункт меню себя ведёт. Сайт исполняет только "instant", остальное — предупреждение.
    #   instant     — можно выполнять прямо из веба
    #   interactive — ждёт ввода в терминале, с сервера вызывать нельзя
    #   restart     — перезапускает веб-сервер, ответ не успевает уйти
    #   danger      — необратимое действие, требует подтверждения
    COMMAND_MODES = {
        "chat": "interactive",
        "ai": "interactive",
        "provider": "interactive",
        "ai_model": "interactive",
        "zen_model": "interactive",
        "zen_api": "interactive",
        "ai_provider": "interactive",
        "tts_voice": "interactive",
        "change_speed_ai_voice": "interactive",
        "trigger_word": "interactive",
        "change_voice_profile": "interactive",
        "change_stt_mode": "interactive",
        "whisper_model": "interactive",
        "micro": "interactive",
        "menu": "interactive",
        "clear_context": "instant",
        "settings": "instant",
        "reload_libs": "instant",
        "help": "instant",
        "clear": "instant",
        "restart_server": "restart",
        "exit": "danger",
    }

    def __init__(self):
        self.should_exit = False
        self.stop_audio_triggered = False
        self.ignore_response = False
        self.response_ready_event = threading.Event()
        self.pending_voice = False
        self.memory_path = Path(settings.PATHS["memory_path"])
        self.memory = self.get_memory_from_file()
        self.sound = None
        self.ai_speak = False
        self.stop_speak_flags = settings.stop_speak_flags
        self.listen_while_ai_speak_flag = True
        self.type_work = "chat"
        self.ai_state = "idle"
        self.user_prompt = None
        self.ollama_url = "http://localhost:11434/api/"
        self.ollama_data = {"models": []}
        try:
            self.ollama_response = requests.get(self.ollama_url + "tags", timeout=5)
            data = self.ollama_response.json()
            if isinstance(data, dict):
                self.ollama_data = data
        except Exception as e:
            # Ollama может быть не запущена — это не повод падать на старте
            print(Fore.YELLOW + f"Ollama сейчас недоступна ({e}). "
                                 "Запусти её перед общением с ИИ." + Style.RESET_ALL)
        self.ai_thread = threading.Thread(target=self.generate_ai_response, daemon=True)
        self.models_info = []
        self.default_settings = settings.default_settings
        self.global_context = "ГЛОБАЛЬНЫЙ КОНТЕКСТ: \n"
        self.second_context = ""
        self.response = ""
        self.font_for_welcome = "epic"
        self.font_for_menu = "small"
        self.console = Console(theme=Theme({
            "info": "cyan", "ok": "green", "err": "bold red",
            "user": "bold #89b4fa", "ai": "bold #f5c2e7", "cmd": "bold yellow",
        }))

        self.commands_ai = work_fuctions.commands_ai

        self.commands_ai_and_args = work_fuctions.commands_ai_and_args

        work_fuctions.tts_callback = self.play_timer_tts
        self.second_system_prompt = ""
        self.model = settings.settings["model"]
        self.provider = settings.settings.get("provider", "ollama")
        self.zen_api_key = settings.settings.get("zen_api_key", "")
        self.zen_model = settings.settings.get("zen_model", "gpt-5.4-mini")
        self.zen_url = "https://opencode.ai/zen/v1/chat/completions"
        self.zen_models_url = "https://opencode.ai/zen/v1/models"
        self._load_ai_endpoint_from_settings()
        self._migrate_legacy_ai_provider()
        self._banner()
        self.whisper_model = settings.settings["whispermodel"]
        self.micro_index = settings.settings["micro_index"]
        self.tts_voice = settings.settings["tts_voice"]
        self.trigger_word = settings.settings["trigger_word"]
        self.speed_ai_speak = settings.settings["speed_ai_speak"]
        self.stt_mode = settings.settings["stt_mode"]
        self.profile_wav = settings.PATHS["voice_profile_path"]
        self.system_prompt = self._build_system_prompt()
        self.tts = SileroTTS(
            model_id='v4_ru',
            language='ru',
            speaker=self.tts_voice,
            device=tts_device()
        )
        self.whisper_load_model = None
        if self.stt_mode == "whisper":
            self.whisper_load_model = load_whisper(self.whisper_model) if self.whisper_model != "" else None

        self.user_commands = {
            "chat": [lambda: self._print_prompt(), "Продолжить диалог с ИИ (выход из меню)"],
            "ai": [self.ai_hub, "ИИ: провайдер, модель и API-ключ — всё в одном месте"],
            # алиасы, оставленные для совместимости со старыми голосовыми командами
            "provider": [self.ai_hub, "ИИ: провайдер, модель и API-ключ (то же, что /ai)"],
            "ai_model": [self.ai_hub, "ИИ: провайдер, модель и API-ключ (то же, что /ai)"],
            "zen_model": [self.ai_hub, "ИИ: провайдер, модель и API-ключ (то же, что /ai)"],
            "zen_api": [self.ai_hub, "ИИ: провайдер, модель и API-ключ (то же, что /ai)"],
            "ai_provider": [self.ai_hub, "ИИ: провайдер, модель и API-ключ (то же, что /ai)"],
            "tts_voice": [self.select_tts_voice, "Выбрать голос синтеза речи (TTS)"],
            "change_speed_ai_voice": [self.change_spped_ai_voice, "Изменить скорость речи ИИ"],
            "trigger_word": [self.select_trigger_word, "Слово-триггер для голосового обращения к ИИ"],
            "change_voice_profile": [self.make_voice_profile, "Перезаписать эталонный голос (голосовой пропуск)"],
            "change_stt_mode": [self.change_stt_mode, "Режим распознавания речи: whisper или google"],
            "whisper_model": [self.select_whisper_model, "Выбрать модель Whisper для распознавания речи"],
            "micro": [self.select_micro, "Выбрать микрофон для записи голоса"],
            "clear_context": [self.clear_context, "Очистить контекст диалога (память ИИ)"],
            "settings": [self.show_settings, "Показать текущие настройки"],
            "restart_server": [self.restart_web_server, "Перезапустить веб-сервер и мобильный интерфейс"],
            "reload_libs": [self.reload_libs, "Перезагрузить настройки и модули"],
            "help": [self._show_help, "Показать список всех команд"],
            "clear": [self.clear_console, "Очистить экран консоли"],
            "menu": [self.open_menu, "Открыть это меню"],
            "exit": [work_fuctions.exit, "Выйти из программы"],
        }

        # Логичный порядок пунктов главного меню
        # старые команды ИИ остаются рабочими алиасами /ai, но отдельными
        # пунктами в меню, справке и на сайте не показываются
        self.menu_layout = [
            ("💬 ЧАТ", ["chat", "clear_context"]),
            ("🧠 МОДЕЛЬ И ПРОВАЙДЕР ИИ", ["ai"]),
            ("🎙 ГОЛОС И РАСПОЗНАВАНИЕ РЕЧИ", ["tts_voice", "change_speed_ai_voice", "trigger_word",
                                          "change_voice_profile", "change_stt_mode", "whisper_model", "micro"]),
            ("🔧 СИСТЕМА", ["settings", "restart_server", "reload_libs", "clear", "help"]),
            ("🚪 ВЫХОД", ["exit"]),
        ]

        with quiet_native("модель проверки голоса"):
            self.voice_verifier = SpeakerRecognition.from_hparams(
                source="microsoft/spkrec-ecapa-voxceleb",
                savedir="pretrained_models/spkrec",
                local_strategy=LocalStrategy.COPY,
                run_opts={"device": torch_device()}
            )
        self.profile_tensor = None

        recognizer = sr.Recognizer()

        with open_microphone(self.micro_index, sample_rate=48000, chunk_size=2048) as source:
            print(Fore.YELLOW + "MICROPHONE SETUP (BE SLIENT FOR 2 SECONDS)..." + Style.RESET_ALL)
            recognizer.adjust_for_ambient_noise(source, duration=2)

            if not os.path.exists(self.profile_wav):
                print(Fore.YELLOW + "=== THE VOICE STANDARD WAS NOT FOUND ===" + Style.RESET_ALL)
                print("SAY A LONG PHRASE (5 SECONDS) SO THAT THE AI REMEMBERS YOUR TIMBRE...")
                try:
                    print(Fore.CYAN + "THE RECORDIND HAS STERTED! SPEAK..." + Style.RESET_ALL)
                    enroll_audio = recognizer.record(source, duration=5)
                    with open(self.profile_wav, "wb") as f:
                        f.write(enroll_audio.get_wav_data())
                    print(Fore.GREEN + "YOUR REFERENCE VOICE IS SAVED!\n" + Style.RESET_ALL)
                except Exception as e:
                    print(Fore.RED + f"STANDARD RECORDING ERROR: {e}" + Style.RESET_ALL)
                    return

            self.load_profile_tensor()

    def _build_system_prompt(self):
        """Собрать системный промпт. Вызывается и на старте, и при смене триггер- слова."""
        return ("Тебя зовут" + str(self.trigger_word) + """,Ты работаешь на Arch Linux. ВАЖНОЕ ПРАВИЛО: ОТВЕЧАЙ СРАЗУ И НАПРЯМУЮ. . ВЫДАВАЙ ТОЛЬКО ИТОГОВЫЙ ОТВЕТ!
Используй команды чтобы выполнить данное указание если ты считаешь что это нужно, либо ответь просто текстом.Вот как нужно отвечать:
1. Если нужно выполнить команду, то ответ ДОЛЖЕН НАЧИНАТЬСЯ С command(ОБЯЗАТЕЛЬНО) и быть в формате JSON,ОТВЕТ НЕ ДОЛЖЕН СОДЕРЖАТЬ НИЧЕГО ЛИШНЕГО КРОМЕ СЛОВА COMMAND В НАЧАЛЕ И САМОЙ КОМАНДЫ,к некоторым командам НЕ нужны аргументы(пример):
command {"command": "название_команды", "args": {"аргумент1": "значение1", "аргумент2": "значение2"}}. 
2. Если ты хочешь просто ответить текстом, то ответ должен быть просто текстом(если запрос user'a не элементарный то желательно используй web_search).
        """ + "Вот список доступных команд и их аргуметов: " + self.commands_ai_and_args + "\nЕСЛИ ТЫ ЧТО-ТО НЕ ПОМНИШЬ/НЕ ЗНАЕШЬ/НЕ РАСПОЛАГАЕШЬ ТАКОЙ ИНФОРМАЦИЕЙ ТО СНАЧАЛА ОБРАТИСЬ К ВНУТРЕННЕЙ ПАМЯТИ ЧЕРЕЗ ФУНКЦИИ.НЕ ОТВЕЧАЙ ПОЛЬЗОВАТЕЛЮ 'Я не знаю','У меня нет данных' И ТД.")

    def _banner(self):
        """Премиум-приветственный баннер с названием ЛУЧ."""
        try:
            ascii_luch = str(text2art("LUCH", font="banner3"))
        except Exception:
            ascii_luch = "L U C H"
        versions = []
        from rich import box as _box
        lines = list(ascii_luch.split("\n"))
        # Градиент зелёный -> cyan -> magenta по строкам ASCII-арта
        colors = ["bold bright_green", "bold green", "bold cyan", "bold bright_cyan", "bold magenta"]
        logo = Text()
        for i, ln in enumerate(lines):
            c = colors[i % len(colors)]
            if ln.strip():
                logo.append(ln + "\n", style=c)
        logo.append("\n", style="bold")
        logo.append(" ✦ АССИСТЕНТ «ЛУЧ» ✦\n", style="bold #f5c2e7")
        self.console.print(Panel(
            logo,
            title="⚡ LUCH TERMINAL", title_align="left",
            border_style="#f5c2e7", box=DOUBLE, padding=(1, 2),
        ))
        cfg = f"[bold green]{getattr(self, 'trigger_word', None) or 'луч'}[/bold green]"
        prov = f"{self.ai_label()} · {self.ai_active_model() or 'модель не выбрана'}"
        self._panel("⚙ БЫСТРЫЙ СТАРТ",
                    f"  [user]/help[/user] или [user]/menu[/user] — команды\n"
                    f"  [ok]Провайдер:[/ok] {prov}   [ok]Слово-триггер:[/ok] {cfg}\n"
                    f"  Скажи «{getattr(self, 'trigger_word', None) or 'луч'} + вопрос» голосом или введи текст",
                    style="info", box=SIMPLE)

    def _panel(self, title, text, style="ai", box=None, right=False):
        """Красивый вывод сообщения в Rich-панели."""
        box = box or ROUNDED
        t = Text.from_markup(str(text))
        self.console.print(Panel(
            t,
            title=title, title_align="right" if right else "left",
            border_style={"ai": "#f5c2e7", "cmd": "yellow", "user": "#89b4fa",
                          "info": "cyan", "err": "red", "ok": "green"}.get(style, style),
            box=box, padding=(0, 1),
        ))

    def _print_prompt(self, empty=True):
        """Аккуратный промпт ввода пользователя."""
        if empty:
            print()
        self.console.print("  [bold #89b4fa]➜[/bold #89b4fa] ", end="")

    def _choice_menu(self, title, options, prompt=None, current=None, allow_custom=False):
        """Меню выбора стрелками. Возвращает выбранный console_ui.Item или None."""
        entries = console_ui.items_from(options, current=current, allow_custom=allow_custom)
        return console_ui.select(title, entries, subtitle=prompt or "", hint=console_ui.HINT,
                                 height=min(18, len(entries) + 3), console=self.console)

    def _show_help(self):
        lines = Text()
        shown = set()
        for group_title, keys in self.menu_layout:
            lines.append(f"\n  {group_title}\n", style="bold #cba6f7")
            for key in keys:
                if key in shown or key not in self.user_commands:
                    continue
                shown.add(key)
                lines.append(f"    /{key:<22}", style="bold #89b4fa")
                lines.append(f"{self.user_commands[key][1]}\n", style="#6c7086")
        rest = [k for k in self.user_commands
                if k not in shown and k not in self.HIDDEN_AI_ALIASES]
        if rest:
            lines.append("\n  ПРОЧЕЕ\n", style="bold #cba6f7")
            for key in rest:
                lines.append(f"    /{key:<22}", style="bold #89b4fa")
                lines.append(f"{self.user_commands[key][1]}\n", style="#6c7086")
        lines.append("\n  Подсказка: /menu — меню со стрелками, /help — эта справка\n", style="dim")
        self.console.print(Panel(lines, title=" ℹ СПРАВКА ", title_align="left",
                                 border_style="#cba6f7", box=ROUNDED, padding=(0, 1)))

    def restart_web_server(self):
        if getattr(self, "server_thread", None) and self.server_thread.is_alive():
            if not web_server.stop_server():
                self.console.print(" ⚠ Веб-сервер не остановился, порт может быть занят\n",
                                   style="yellow")
        importlib.reload(web_server)
        self.server_thread = threading.Thread(
            target=web_server.start_server_in_thread,
            args=(self,),
            daemon=True
        )
        self.server_thread.start()
    
    def reload_libs(self):
        try:
            importlib.reload(settings)
            importlib.reload(work_fuctions)
            self.stop_speak_flags = settings.stop_speak_flags
            self.commands_ai = work_fuctions.commands_ai
            self.commands_ai_and_args = work_fuctions.commands_ai_and_args
            self.model = settings.settings["model"]
            self.provider = settings.settings.get("provider", "ollama")
            self.zen_api_key = settings.settings.get("zen_api_key", "")
            self.zen_model = settings.settings.get("zen_model", "gpt-5.4-mini")
            self.zen_url = "https://opencode.ai/zen/v1/chat/completions"
            self.zen_models_url = "https://opencode.ai/zen/v1/models"
            self._load_ai_endpoint_from_settings()
            self._migrate_legacy_ai_provider()
            self.whisper_model = settings.settings["whispermodel"]
            self.micro_index = settings.settings["micro_index"]
            self.tts_voice = settings.settings["tts_voice"]
            self.trigger_word = settings.settings["trigger_word"]
            self.speed_ai_speak = settings.settings["speed_ai_speak"]
            self.stt_mode = settings.settings["stt_mode"]
            self.profile_wav = settings.PATHS["voice_profile_path"]
            self.system_prompt = self._build_system_prompt()
        except Exception as e:
            print("error reload libs")

    # ================= OpenAI-совместимый провайдер ИИ =================

    def _load_ai_endpoint_from_settings(self):
        """Прочитать адрес/ключ/модель универсального API из настроек."""
        self.ai_base_url = (settings.settings.get("ai_base_url", "") or "").strip().rstrip("/")
        self.ai_api_key = (settings.settings.get("ai_api_key", "") or "").strip()
        self.ai_model = (settings.settings.get("ai_model", "") or "").strip()
        try:
            self.ai_timeout = float(settings.settings.get("ai_timeout", 120) or 120)
        except (TypeError, ValueError):
            self.ai_timeout = 120.0

    # ── Единый реестр провайдеров ИИ ──
    # Встроенные: ollama и zen. Кастомные хранятся в settings["ai_providers"].
    # старые команды ИИ остаются рабочими алиасами /ai, но отдельными
    # пунктами в меню, справке и на сайте не показываются
    HIDDEN_AI_ALIASES = ("provider", "ai_model", "zen_model", "zen_api", "ai_provider")

    BUILTIN_PROVIDERS = ("ollama", "zen")
    CUSTOM_PROVIDERS = "openai"

    def _migrate_legacy_ai_provider(self):
        '''Перенести старые плоские поля ai_* в реестр ai_providers (один раз).'''
        if settings.settings.get("ai_providers") or settings.settings.get("ai_active"):
            return
        if (settings.settings.get("provider") or "").strip().lower() not in ("openai", "custom", "api"):
            return
        base = (settings.settings.get("ai_base_url") or "").strip()
        if not base:
            return
        self.save_custom_provider("", base, settings.settings.get("ai_api_key") or "",
                                  settings.settings.get("ai_model") or "",
                                  settings.settings.get("ai_timeout") or 120)
        entry = self.custom_providers()[-1]
        settings.settings["ai_providers"] = self.custom_providers()
        settings.settings["ai_active"] = entry["id"]
        settings.save_settings()
        print(Fore.GREEN + "кастомный провайдер перенесён в список: %s"
              % (entry.get("name") or entry["base_url"]) + Style.RESET_ALL)

    def custom_providers(self):
        """Сохранённые кастомные провайдеры (список словарей)."""
        raw = settings.settings.get("ai_providers") or []
        out = []
        for entry in raw:
            if isinstance(entry, dict) and (entry.get("base_url") or "").strip():
                out.append(entry)
        return out

    def active_custom_id(self):
        return (settings.settings.get("ai_active") or "").strip()

    def active_custom(self):
        """Активный кастомный провайдер или None."""
        want = self.active_custom_id()
        for entry in self.custom_providers():
            if entry.get("id") == want:
                return entry
        return None

    def ai_label(self):
        """Человеческое имя активного провайдера — для меню, подсказок и спиннера."""
        kind = (getattr(self, "provider", "") or "ollama").strip().lower()
        if kind == "zen":
            return "OpenCode Zen"
        if kind == "ollama":
            return "Ollama"
        entry = self.active_custom()
        if entry:
            return entry.get("name") or entry.get("base_url")
        return getattr(self, "ai_base_url", "") or "кастомный API"

    def ai_active_model(self):
        """Модель активного провайдера."""
        kind = (getattr(self, "provider", "") or "ollama").strip().lower()
        if kind == "zen":
            return getattr(self, "zen_model", "")
        if kind == "ollama":
            return getattr(self, "model", "")
        return getattr(self, "ai_model", "")

    def ai_summary(self):
        """Строка «провайдер · модель» для статус-бара и заголовков."""
        return "%s · %s" % (self.ai_label(), self.ai_active_model() or "модель не выбрана")

    @staticmethod
    def _slug(text):
        """Короткий идентификатор провайдера из названия или адреса."""
        base = re.sub(r"^https?://", "", (text or "").strip()).strip("/")
        base = re.sub(r"[^A-Za-z0-9а-яА-Я]+", "-", base).strip("-").lower()
        return (base or "provider")[:40]

    def save_custom_provider(self, name, base_url, api_key, model, timeout, keep_id=None):
        """Создаёт или обновляет кастомного провайдера. Возвращает его запись."""
        entry = {
            "id": keep_id or self._slug(name or base_url),
            "name": (name or "").strip() or self._slug(base_url),
            "base_url": self._normalize_ai_base_url(base_url),
            "api_key": (api_key or "").strip(),
            "model": (model or "").strip(),
            "timeout": float(timeout or 120),
        }
        entries = self.custom_providers()
        for i, e in enumerate(entries):
            if e.get("id") == entry["id"]:
                entries[i] = entry
                break
        else:
            entries.append(entry)
        settings.settings["ai_providers"] = entries
        return entry

    def activate_custom_provider(self, entry, persist=True):
        """Делает кастомного провайдера активным и подставляет его адрес/ключ/модель."""
        entry = self.save_custom_provider(
            entry.get("name"), entry.get("base_url"), entry.get("api_key"),
            entry.get("model"), entry.get("timeout"), keep_id=entry.get("id"),
        )
        self.provider = self.CUSTOM_PROVIDERS
        self.ai_base_url = entry["base_url"]
        self.ai_api_key = entry["api_key"]
        self.ai_model = entry["model"]
        self.ai_timeout = entry["timeout"]
        if persist:
            settings.settings["ai_active"] = entry["id"]
            settings.settings["provider"] = self.CUSTOM_PROVIDERS
            settings.settings["ai_base_url"] = entry["base_url"]
            settings.settings["ai_api_key"] = entry["api_key"]
            settings.settings["ai_model"] = entry["model"]
            settings.settings["ai_timeout"] = entry["timeout"]
            settings.save_settings()
        self.user_prompt = None
        return entry

    def set_active_provider(self, kind):
        """Переключает на встроенного провайдера (ollama / zen)."""
        kind = (kind or "").strip().lower()
        if kind not in self.BUILTIN_PROVIDERS:
            raise ValueError("неизвестный встроенный провайдер: %s" % kind)
        self.provider = kind
        settings.settings["provider"] = kind
        settings.save_settings()
        self.user_prompt = None
        return kind

    @staticmethod
    def _normalize_ai_base_url(raw):
        """Принимает и 'http://host:8000/v1', и полный '.../v1/chat/completions'."""
        url = (raw or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
                break
        return url

    def _ai_chat_url(self):
        base = getattr(self, "ai_base_url", "") or ""
        return base + "/chat/completions" if base else ""

    def _ai_models_url(self):
        base = getattr(self, "ai_base_url", "") or ""
        return base + "/models" if base else ""

    def _ai_headers(self):
        headers = {"Content-Type": "application/json"}
        key = getattr(self, "ai_api_key", "") or ""
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def _ai_http_error(self, exc, url="", model=""):
        """Человекочитаемое описание сетевой/HTTP-ошибки ИИ."""
        response = getattr(exc, "response", None)
        if response is not None:
            code = getattr(response, "status_code", "?")
            hints = {
                400: " — сервер не принял запрос, проверь модель и формат",
                401: " — нужен верный API-ключ",
                403: " — доступ запрещён, проверь ключ",
                404: " — неверный адрес эндпоинта, проверь base_url",
                422: " — сервер не понял параметры запроса",
                429: " — превышен лимит запросов, попробуй позже",
                500: " — внутренняя ошибка на стороне API",
                502: " — прокси не смог связаться с моделью",
                503: " — сервис временно недоступен",
            }
            body = ""
            try:
                body = (response.text or "")[:300].strip()
            except Exception:
                body = ""
            text = "HTTP %s%s" % (code, hints.get(code, ""))
            if body:
                text += ": " + body
            return text
        message = str(exc)
        low = message.lower()
        if "connection" in low or "max retries" in low or "resolve" in low:
            return "нет связи с " + (url or "адресом API")
        if "timeout" in low or "timed out" in low:
            return "таймаут ответа (больше %s c)" % self.ai_timeout
        if "no model" in low or "model not found" in low:
            return "сервер не знает модель '%s'" % model
        return message

    @staticmethod
    def _ai_extract_text(data):
        """Достать текст из обычного (не потокового) ответа OpenAI-API."""
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if content is None:
            content = first.get("text")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        return content if isinstance(content, str) else ""

    def _ai_request(self, messages, stream=True, max_tokens=None, temperature=None):
        """Единый вызов OpenAI-совместимого /chat/completions. Возвращает (текст, ошибка)."""
        url = self._ai_chat_url()
        if not url:
            return "", "не задан адрес API (base_url)"
        model = (self.ai_model or "").strip()
        if not model:
            return "", "не указана модель (model)"
        payload = {"model": model, "messages": messages, "stream": bool(stream)}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if temperature is not None:
            payload["temperature"] = temperature
        timeout = (15, float(self.ai_timeout or 120))
        try:
            with requests.post(url, json=payload, headers=self._ai_headers(),
                               stream=bool(stream), timeout=timeout) as r:
                r.raise_for_status()
                if not stream:
                    return self._ai_extract_text(r.json()), ""
                return self._ai_read_stream(r), ""
        except Exception as e:
            return "", self._ai_http_error(e, url, model)

    def _ai_read_stream(self, response):
        """Разбор потока: понимает SSE (`data: `) и голый JSON построчно."""
        text = ""
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            chunk_text = line.strip()
            if chunk_text == "[DONE]":
                break
            if chunk_text.lower().startswith("data:"):
                chunk_text = chunk_text[5:].strip()
            if not chunk_text:
                continue
            try:
                chunk = json.loads(chunk_text)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(chunk, dict):
                continue
            piece = ""
            choices = chunk.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                first = choices[0]
                delta = first.get("delta")
                message = first.get("message")
                for source in (delta if isinstance(delta, dict) else None,
                               message if isinstance(message, dict) else None,
                               first):
                    if not isinstance(source, dict):
                        continue
                    piece = source.get("content")
                    if piece is None:
                        piece = source.get("text")
                    if piece:
                        break
            if not piece:
                # сервер проигнорировал stream и вернул обычный ответ целиком
                message = chunk.get("message")
                if isinstance(message, dict):
                    piece = message.get("content") or message.get("response") or ""
            if isinstance(piece, list):
                piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
            if piece:
                text += str(piece)
        return text

    def list_ai_models(self, base_url=None, api_key=None):
        """Список моделей с {base}/models. Возвращает (ids, ошибка)."""
        saved = (self.ai_base_url, self.ai_api_key)
        if base_url is not None:
            self.ai_base_url = self._normalize_ai_base_url(base_url)
        if api_key is not None:
            self.ai_api_key = str(api_key).strip()
        url = self._ai_models_url()
        try:
            if not url:
                return [], "не задан адрес API (base_url)"
            r = requests.get(url, headers=self._ai_headers(), timeout=15)
            r.raise_for_status()
            data = r.json()
            raw = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
            if not isinstance(raw, list):
                return [], "сервер не вернул список моделей (нет поля data)"
            ids = []
            for item in raw:
                name = item.get("id") if isinstance(item, dict) else item
                if name:
                    ids.append(str(name))
            return sorted(set(ids)), ""
        except Exception as e:
            return [], self._ai_http_error(e, url, self.ai_model)
        finally:
            self.ai_base_url, self.ai_api_key = saved

    def test_ai_endpoint(self, base_url=None, api_key=None, model=None, text="Привет!"):
        """Быстрая проверка соединения. Возвращает (ok, сообщение)."""
        saved = (self.ai_base_url, self.ai_api_key, self.ai_model)
        if base_url is not None:
            self.ai_base_url = self._normalize_ai_base_url(base_url)
        if api_key is not None:
            self.ai_api_key = str(api_key).strip()
        if model is not None:
            self.ai_model = str(model).strip()
        try:
            if not self._ai_chat_url():
                return False, "не задан адрес API (base_url)"
            if not self.ai_model:
                return False, "не указана модель (model)"
            answer, err = self._ai_request(
                [{"role": "user", "content": text}], stream=False, max_tokens=32
            )
            if err:
                return False, err
            return True, (answer.strip() or "(пустой ответ — соединение работает)")
        finally:
            self.ai_base_url, self.ai_api_key, self.ai_model = saved

    def apply_ai_endpoint(self, base_url, api_key=None, model="", timeout=None,
                          activate=True, keep_key=False, persist=True):
        """Применить и (по умолчанию) сохранить адрес/ключ/модель провайдера."""
        base_url = self._normalize_ai_base_url(base_url)
        if not base_url:
            raise ValueError("пустой адрес API (base_url)")
        if not base_url.lower().startswith(("http://", "https://")):
            raise ValueError("адрес должен начинаться с http:// или https://")
        model = (model or "").strip()
        if not model:
            raise ValueError("пустое имя модели (model)")

        key = (api_key or "").strip()
        if not key and keep_key:
            key = self.ai_api_key
        if activate:
            self.provider = "openai"
        self.ai_base_url = base_url
        self.ai_api_key = key
        self.ai_model = model
        if timeout:
            try:
                self.ai_timeout = float(timeout)
            except (TypeError, ValueError):
                raise ValueError("таймаут должен быть числом")
        if persist:
            settings.settings["ai_base_url"] = base_url
            settings.settings["ai_api_key"] = key
            settings.settings["ai_model"] = model
            settings.settings["ai_timeout"] = self.ai_timeout
            settings.settings["provider"] = self.provider
            settings.save_settings()
        return self.ai_config()

    def ai_config(self):
        """Текущая конфигурация провайдера для веба. Ключи наружу не отдаются."""
        return {
            "provider": getattr(self, "provider", "ollama") or "ollama",
            "provider_label": self.ai_label(),
            "active_model": self.ai_active_model(),
            "summary": self.ai_summary(),
            "base_url": getattr(self, "ai_base_url", ""),
            "model": getattr(self, "ai_model", ""),
            "timeout": getattr(self, "ai_timeout", 120),
            "has_key": bool(getattr(self, "ai_api_key", "")),
            "key_length": len(getattr(self, "ai_api_key", "") or ""),
            "chat_url": self._ai_chat_url(),
            "models_url": self._ai_models_url(),
            "ollama_url": (getattr(self, "ollama_url", "") or "").rstrip("/"),
            "ollama_model": getattr(self, "model", ""),
            "ollama_models": [m.get("name", m) for m in
                              ((getattr(self, "ollama_data", None) or {}).get("models") or [])
                              if isinstance(m, dict)],
            "zen_model": getattr(self, "zen_model", ""),
            "zen_has_key": bool(getattr(self, "zen_api_key", "")),
            "zen_key_length": len(getattr(self, "zen_api_key", "") or ""),
            "active_custom_id": self.active_custom_id(),
            "custom_providers": [
                {"id": e.get("id"), "name": e.get("name"),
                 "base_url": e.get("base_url"), "model": e.get("model"),
                 "timeout": e.get("timeout"),
                 "has_key": bool(e.get("api_key")),
                 "key_length": len(e.get("api_key") or "")}
                for e in self.custom_providers()
            ],
        }

    def web_switch_provider(self, target):
        """Переключение провайдера с сайта: 'ollama', 'zen' или id кастомного."""
        target = (target or "").strip()
        if not target:
            raise ValueError("не указан провайдер")
        if target.lower() in self.BUILTIN_PROVIDERS:
            return self.set_active_provider(target.lower())
        entry = next((e for e in self.custom_providers() if e.get("id") == target), None)
        if not entry:
            raise ValueError("провайдер «%s» не найден в списке" % target)
        self.activate_custom_provider(entry)
        return self.provider

    def web_save_custom_provider(self, name, base_url, api_key=None, model="", timeout=None,
                                 keep_key=True, provider_id=None, activate=True):
        """Создание/обновление кастомного провайдера с сайта."""
        entry = next((e for e in self.custom_providers() if e.get("id") == provider_id), None) or {}
        key = (api_key or "").strip()
        if not key and keep_key:
            key = entry.get("api_key", "")
        saved = self.save_custom_provider(
            name or entry.get("name") or base_url, base_url,
            key, model or entry.get("model", ""),
            timeout or entry.get("timeout", self.ai_timeout),
            keep_id=provider_id or entry.get("id"),
        )
        if activate:
            self.activate_custom_provider(saved)
        return saved

    def web_set_zen_key(self, api_key, model=None):
        """Смена ключа Zen с сайта. Пустой ключ = оставить текущий."""
        key = (api_key or "").strip()
        if key:
            self.zen_api_key = key
            settings.settings["zen_api_key"] = key
            settings.save_settings()
        elif not self.zen_api_key:
            raise ValueError("нужен API-ключ Zen")
        if model:
            self.zen_model = str(model).strip()
            settings.settings["zen_model"] = self.zen_model
            settings.save_settings()
        if self.provider == "zen":
            self.user_prompt = None
        return True

    def web_set_ollama_model(self, model):
        """Смена модели Ollama с сайта."""
        name = (model or "").strip()
        if not name:
            raise ValueError("пустое имя модели")
        self.model = name
        self.provider = "ollama"
        settings.settings["model"] = name
        settings.settings["provider"] = "ollama"
        settings.save_settings()
        self.user_prompt = None
        return name

    def web_delete_custom_provider(self, provider_id):
        """Удаляет кастомного провайдера; если он был активен — переключает на ollama."""
        pid = (provider_id or "").strip()
        entries = self.custom_providers()
        left = [e for e in entries if e.get("id") != pid]
        if len(left) == len(entries):
            raise ValueError("провайдер «%s» не найден" % (pid or "—"))
        settings.settings["ai_providers"] = left
        if self.active_custom_id() == pid:
            settings.settings["ai_active"] = ""
            self.set_active_provider("ollama")
        settings.save_settings()
        return pid

    def _generate_openai(self):
        """Обращение к произвольному OpenAI-совместимому эндпоинту."""
        if not self.ai_base_url:
            self.response = ("Провайдер «openai» выбран, но не задан адрес API. "
                             "Открой CONFIG на сайте и укажи base_url и модель.")
            print(Fore.YELLOW + "AI API: не задан base_url" + Style.RESET_ALL)
            return
        if not self.ai_model:
            self.response = "Не указана модель для OpenAI-совместимого API. Задай её в CONFIG на сайте."
            print(Fore.YELLOW + "AI API: не указана модель" + Style.RESET_ALL)
            return
        messages = [
            {"role": "system",
             "content": self.system_prompt + self.create_second_system_prompt()},
            {"role": "user", "content": self.create_prompt()},
        ]
        answer, err = self._ai_request(messages, stream=True)
        if err:
            print(Fore.RED + f"AI API error: {err}" + Style.RESET_ALL)
            answer = (f"Ошибка обращения к ИИ ({err}). Проверь адрес, ключ и модель "
                      f"'{self.ai_model}' в CONFIG на сайте.")
        self.response = answer

    # ================= Управление с сайта =================

    @staticmethod
    def _first_json_object(text):
        """Первая сбалансированная {...} подстрока, начиная с первого '{'."""
        start = text.find("{")
        if start < 0:
            return ""
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return ""

    def _ai_command_docs(self):
        """Разбор commands_ai_and_args в структуру: имя, иконка, описание, пример вызова."""
        docs = {}
        current = None
        for raw in (self.commands_ai_and_args or "").splitlines():
            line = raw.strip()
            m = re.match(r"^\d+\.\s*([A-Za-z_][A-Za-z_0-9]*)\s*-\s*(.+)$", line)
            if m:
                current = m.group(1)
                docs[current] = {"desc": m.group(2).strip(), "example": ""}
                continue
            if not current:
                continue
            if line.startswith("Пример"):
                example = self._first_json_object(line)
                if example and not docs[current]["example"]:
                    docs[current]["example"] = example
                if example:
                    tail = line[line.find(example) + len(example):]
                else:
                    tail = line.split(":", 1)[-1] if ":" in line else ""
                if tail and not tail.startswith("Пример"):
                    docs[current]["desc"] = (docs[current]["desc"] + " " + tail.strip(" .;,")).strip()
            elif line:
                docs[current]["desc"] = (docs[current]["desc"] + " " + line).strip()
        out = []
        for name, spec in (self.commands_ai or {}).items():
            d = docs.get(name, {})
            out.append({"name": name,
                        "icon": (spec or {}).get("icon", ""),
                        "desc": d.get("desc", ""),
                        "example": d.get("example", "")})
        return out

    def web_command_info(self):
        """Меню и команды ИИ в виде, пригодном для отрисовки на сайте."""
        groups, seen = [], set()
        for title, keys in self.menu_layout:
            items = []
            for key in keys:
                if key not in self.user_commands or key in seen:
                    continue
                seen.add(key)
                items.append({"name": key, "desc": self.user_commands[key][1],
                              "mode": self.COMMAND_MODES.get(key, "instant")})
            if items:
                groups.append({"title": title, "items": items})
        rest = [{"name": k, "desc": v[1], "mode": self.COMMAND_MODES.get(k, "instant")}
                for k, v in self.user_commands.items()
                if k not in seen and k not in self.HIDDEN_AI_ALIASES]
        if rest:
            groups.append({"title": "ПРОЧЕЕ", "items": rest})

        interactive_on_site = sorted(k for k in self.COMMAND_MODES
                                     if self.COMMAND_MODES[k] == "interactive"
                                     and k in ("ai", "tts_voice", "tts_device", "change_speed_ai_voice",
                                               "trigger_word", "change_stt_mode",
                                               "whisper_model", "micro"))
        return {
            "groups": groups,
            "interactive_on_site": interactive_on_site,
            "ai_summary": self.ai_summary(),
            "ai_commands": self.web_ai_commands(),
            "ai_command_history": self.web_ai_command_history(),
            "ai_commands_help": self.commands_ai_and_args,
        }

    # ── ИИ-команды: список, схема аргументов и выполнение с сайта ──
    # Команды, которые реально могут навредить: произвольный shell и блокировка экрана
    AI_CONFIRM_COMMANDS = ("terminal", "lock_pc")

    AI_COMMAND_TIMEOUT = 90  # секунд на одну команду с сайта

    def _ai_command_signature(self, name):
        """Аргументы функции команды ИИ: имя, обязательность, значение по умолчанию."""
        func = (self.commands_ai.get(name) or {}).get("func")
        if func is None:
            return []
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            return []
        out = []
        for pname, param in sig.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            required = param.default is param.empty
            out.append({
                "name": pname,
                "required": required,
                "default": None if required else param.default,
                "type": type(param.default).__name__ if not required else "str",
            })
        return out

    def web_ai_commands(self):
        """Полный список команд ИИ со схемой аргументов — для панели на сайте."""
        out = []
        for doc in self._ai_command_docs():
            name = doc["name"]
            out.append({
                "name": name,
                "icon": doc.get("icon", ""),
                "desc": doc.get("desc", ""),
                "example": doc.get("example", ""),
                "args": self._ai_command_signature(name),
                "danger": name in self.AI_CONFIRM_COMMANDS,
            })
        return out

    def web_ai_command_history(self, limit=30):
        """Последние запуски ИИ-команд, чтобы вывод не пропадал после перезагрузки."""
        history = getattr(self, "_ai_cmd_history", None)
        if not history:
            return []
        return list(history)[-int(limit or 30):]

    def web_clear_ai_command_history(self):
        self._ai_cmd_history = []
        self._ai_cmd_seq = 0
        return True

    def _push_ai_cmd_history(self, entry):
        history = getattr(self, "_ai_cmd_history", None)
        if history is None:
            history = []
            self._ai_cmd_history = history
        seq = getattr(self, "_ai_cmd_seq", 0) + 1
        self._ai_cmd_seq = seq
        entry = dict(entry, seq=seq, source=entry.get("source", "site"))
        history.append(entry)
        while len(history) > 50:
            history.pop(0)
        return entry

    def ai_command_seq(self):
        """Текущий номер последней выполненной команды — чтобы отследить новые."""
        return getattr(self, "_ai_cmd_seq", 0)

    def ai_commands_since(self, seq):
        """Команды, выполненные после отметки seq (для показа в ответе /api/ask)."""
        seq = int(seq or 0)
        return [e for e in (getattr(self, "_ai_cmd_history", None) or []) if e.get("seq", 0) > seq]

    def _bind_ai_args(self, name, args):
        """Привязать аргументы агента к настоящей сигнатуре: по именам, иначе по порядку.

        Не перечисленные необязательные аргументы не передаём вовсе, чтобы сработали
        значения по умолчанию из самой функции.
        """
        spec = self._ai_command_signature(name)
        names = [a["name"] for a in spec]
        args = dict(args or {})
        if not names:
            return {}
        if all(k in names for k in args):
            return dict(args)                      # имена верные — порядок не важен
        # имена не совпали (старый позиционный стиль) — раскладываем по порядку сигнатуры
        values = list(args.values())
        return dict(zip(names, values))

    def _invoke_ai_command(self, name, kwargs, limit):
        """Вызвать функцию команды ИИ, захватив stdout. -> (value, output, error, elapsed)."""
        real_stdout, buf = sys.stdout, io.StringIO()

        class _Tee(io.StringIO):
            def write(self, s):
                try:
                    real_stdout.write(s)
                except Exception:
                    pass
                return buf.write(s)

        value, error = None, None
        sys.stdout = _Tee()
        started = time.time()
        try:
            holder = {}

            def _target():
                try:
                    holder["value"] = self.commands_ai[name]["func"](**kwargs)
                except BaseException as exc:            # noqa: BLE001 — ловим и SystemExit
                    holder["error"] = exc

            thread = threading.Thread(target=_target, daemon=True)
            thread.start()
            thread.join(limit)
            if thread.is_alive():
                raise TimeoutError("команда не ответила за %d с" % limit)
            if holder.get("error") is not None:
                raise holder["error"]
            value = holder.get("value")
        except BaseException as e:                        # noqa: BLE001
            error = e
        finally:
            sys.stdout = real_stdout
            elapsed = time.time() - started
        return value, self._ANSI.sub("", buf.getvalue()).strip(), error, elapsed

    def _record_ai_command(self, name, kwargs, value, output, error, elapsed, source):
        """Одна строка в журнал выполнения — общая для сайта и агента."""
        icon = (self.commands_ai.get(name) or {}).get("icon", "")
        if error is not None:
            status = "error"
            text = "%s: %s" % (type(error).__name__, error)
            output = (output + "\n" if output else "") + "ОШИБКА: " + text
        else:
            status = "ok"
            if not output and value is not None:
                output = str(value)
        entry = self._push_ai_cmd_history({
            "time": time.strftime("%H:%M:%S"), "command": name, "args": kwargs,
            "icon": icon, "status": status, "output": output,
            "elapsed": round(elapsed, 2), "source": source,
        })
        return entry

    def _prepare_ai_args(self, name, args):
        """Проверить имена и обязательность аргументов перед вызовом."""
        spec = self._ai_command_signature(name)
        allowed = {a["name"] for a in spec}
        required = {a["name"] for a in spec if a["required"]}
        args = dict(args or {})

        unknown = [k for k in args if k not in allowed]
        if unknown:
            raise ValueError("у команды %s нет аргументов: %s (есть: %s)"
                             % (name, ", ".join(sorted(unknown)) or "-",
                                ", ".join(sorted(allowed)) or "ни одного"))
        missing = [k for k in required if k not in args or args[k] in (None, "")]
        if missing:
            raise ValueError("не хватает обязательных аргументов: %s" % ", ".join(sorted(missing)))

        for a in spec:
            if a["name"] in args:
                want, val = a["type"], args[a["name"]]
                if want == "int":
                    try:
                        args[a["name"]] = int(val)
                    except (TypeError, ValueError):
                        raise ValueError("аргумент %s должен быть целым числом" % a["name"])
                elif want == "float":
                    try:
                        args[a["name"]] = float(val)
                    except (TypeError, ValueError):
                        raise ValueError("аргумент %s должен быть числом" % a["name"])
                else:
                    args[a["name"]] = str(val)
            elif not a["required"]:
                # показываем в ответе то, что реально ушло в функцию
                args[a["name"]] = a["default"]
        return args

    def web_run_ai_command(self, name, args=None, confirm=False, timeout=None):
        """Выполнить команду ИИ с сайта. Возвращает {status, output, result, ...}."""
        name = (name or "").strip()
        if name not in (self.commands_ai or {}):
            known = ", ".join(sorted(self.commands_ai or {}))
            raise ValueError("неизвестная команда ИИ «%s». Доступные: %s" % (name, known))

        if name in self.AI_CONFIRM_COMMANDS and not confirm:
            return {"status": "confirm_required", "command": name, "args": args or {},
                    "output": "Команда «%s» требует подтверждения на сайте." % name}

        kwargs = self._prepare_ai_args(name, args)
        limit = int(timeout or self.AI_COMMAND_TIMEOUT)
        icon = (self.commands_ai[name] or {}).get("icon", "")
        print(Fore.CYAN + "САЙТ → " + icon + " " + name + " "
              + json.dumps(kwargs, ensure_ascii=False) + Style.RESET_ALL)

        self.ai_state = "command"
        try:
            value, output, error, elapsed = self._invoke_ai_command(name, kwargs, limit)
        finally:
            self.ai_state = "speaking" if getattr(self, "ai_speak", False) else "idle"

        entry = self._record_ai_command(name, kwargs, value, output, error, elapsed, "site")

        # ассистент должен знать, что команда выполнена с сайта
        note = "\nПОЛЬЗОВАТЕЛЬ ВЫПОЛНИЛ С САЙТА КОМАНДУ: %s   АРГУМЕНТЫ: %s   РЕЗУЛЬТАТ: %s" % (
            name, json.dumps(kwargs, ensure_ascii=False), entry["output"][:400])
        self.second_context += note
        self.global_context += note

        return {"status": entry["status"], "command": name, "args": kwargs, "icon": icon,
                "output": entry["output"],
                "result": None if error is not None else value,
                "elapsed": entry["elapsed"], "seq": entry["seq"]}

    def web_run_command(self, name, confirm=False):
        """Выполнить пункт меню из веба. Бросает ValueError с понятным текстом."""
        name = (name or "").strip().lstrip("/")
        if name not in self.user_commands:
            raise ValueError("неизвестная команда /%s" % name)
        mode = self.COMMAND_MODES.get(name, "instant")

        if mode == "interactive":
            raise ValueError("пункт /%s задаётся в CONFIG — открой поле «%s»"
                             % (name, name))
        if mode == "danger" and not confirm:
            return {"status": "confirm_required",
                    "output": "Команда /%s требует подтверждения." % name}
        if mode == "restart":
            return {"status": "restart",
                    "output": "Команда /%s перезапускает LUCH. Подтверди в терминале." % name}

        func = self.user_commands[name][0]
        if func is None:
            return {"status": "confirm_required",
                    "output": "Пункт /%s ждёт ввода в терминале." % name}
        return {"status": "ok", "output": self._run_and_capture(name) or "OK"}

    @staticmethod
    def _strip_ansi(text):
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", text or "")


        if name not in self.user_commands:
            raise ValueError("неизвестная команда /%s" % name)
        mode = self.COMMAND_MODES.get(name, "instant")

        if mode == "danger" and not confirm:
            return {"status": "confirm_required",
                    "output": "Команда /%s требует подтверждения." % name}
        if mode == "interactive":
            raise ValueError("пункт /%s ждёт ввода в терминале — задай его через панель CONFIG "
                             "на сайте или в консоли" % name)
        if mode == "restart":
            def _later_restart():
                time.sleep(1.2)
                try:
                    self.restart_web_server()
                except Exception as e:
                    print(Fore.RED + f"RESTART ERROR: {e}" + Style.RESET_ALL)
            threading.Thread(target=_later_restart, daemon=True).start()
            return {"status": "ok",
                    "output": "Веб-сервер будет перезапущен через пару секунд."}
        if mode == "danger":
            def _later_exit():
                time.sleep(1.2)
                try:
                    work_fuctions.exit()
                except Exception:
                    os._exit(0)
            threading.Thread(target=_later_exit, daemon=True).start()
            return {"status": "ok", "output": "Завершение работы LUCH…"}

        buf = self._run_and_capture(name)
        if buf == "EXITING":
            return {"status": "ok", "output": "Завершение работы LUCH…"}
        if not buf:
            return {"status": "ok", "output": "OK"}
        return {"status": "ok", "output": buf}

    # Команды печатают результат прямо в консоль. Чтобы он же был виден на сайте,
    # перехватываем stdout и возвращаем его в ответе.
    _ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

    def _run_and_capture(self, name):
        """Выполняет команду, возвращает её вывод без ANSI-последовательностей."""
        real = sys.stdout
        buf = io.StringIO()

        class _Tee(io.StringIO):
            def write(self, s):
                try:
                    real.write(s)
                except Exception:
                    pass
                return buf.write(s)

        sys.stdout = _Tee()
        try:
            result = self.user_commands[name][0]()
        except SystemExit:
            sys.stdout = real
            return "EXITING"
        except Exception as e:
            sys.stdout = real
            return "ERROR: %s" % e
        finally:
            sys.stdout = real
        if result == "EXITING":
            return "EXITING"
        text = self._ANSI.sub("", buf.getvalue()).strip()
        if text:
            return text
        return "" if result is None else str(result)

    # Настройки, которые можно поменять с сайта без перезапуска программы
    HOT_SETTINGS = {
        "tts_voice": str,
        "tts_device": str,
        "tts_devices": ["auto", "cuda", "cpu"],
        "trigger_word": str,
        "speed_ai_speak": float,
        "micro_index": int,
        "stt_mode": str,
        "stt_modes": ["whisper", "google"],
    }

    def web_apply_setting(self, key, value):
        """Горячее изменение одной настройки. Возвращает (значение, предупреждение)."""
        if key not in self.HOT_SETTINGS:
            raise ValueError("настройка '%s' не меняется из браузера" % key)
        cast = self.HOT_SETTINGS[key]
        if key == "stt_mode":
            if str(value) not in self.HOT_SETTINGS["stt_modes"]:
                raise ValueError("stt_mode бывает только whisper или google")
            value = str(value)
        elif cast is int:
            value = int(value)
        elif cast is float:
            value = float(value)
            if not 0.5 <= value <= 3.0:
                raise ValueError("скорость речи должна быть от 0.5 до 3.0")
        else:
            value = str(value).strip()

        if key == "tts_voice" and not value:
            raise ValueError("имя голоса не может быть пустым")
        if key == "speed_ai_speak" and not 0.5 <= value <= 3.0:
            raise ValueError("скорость речи должна быть от 0.5 до 3.0")
        if key == "tts_device":
            value = str(value).strip().lower()
            if value not in self.HOT_SETTINGS["tts_devices"]:
                raise ValueError("устройство TTS: auto, cuda или cpu")

        warning = ""
        if key == "tts_voice" and value != self.tts_voice:
            self.tts_voice = value
            try:
                device = tts_device()
                with quiet_native("смена голоса"):
                    self.tts = SileroTTS(model_id="v4_ru", language="ru",
                                         speaker=value, device=device)
                warning = "голос пересоздан на %s" % device
            except Exception as e:
                warning = "сохранено, но голос не пересоздан: %s" % e
        elif key == "speed_ai_speak":
            self.speed_ai_speak = value
        elif key == "trigger_word":
            self.trigger_word = value
            self.system_prompt = self._build_system_prompt()
        elif key == "micro_index":
            self.micro_index = value
            warning = "микрофон применится при следующем запуске"
        elif key == "stt_mode":
            self.stt_mode = value
            if value == "whisper":
                if self.whisper_model and self.whisper_load_model is None:
                    try:
                        with quiet_native("загрузка whisper"):
                            self.whisper_load_model = load_whisper(self.whisper_model)
                    except Exception as e:
                        warning = "Whisper не загрузился: %s" % e
            else:
                self.whisper_load_model = None
                warning = "переключено на Google, Whisper выгружен"
        elif key == "tts_device":
            # переносим модель на другое устройство, но только если оно реально меняется
            wanted = tts_device(value)
            current = getattr(self.tts, "device", "")
            if wanted == current:
                warning = "TTS уже работает на %s" % wanted
            else:
                try:
                    with quiet_native("перенос TTS на %s" % wanted):
                        self.tts = SileroTTS(model_id="v4_ru", language="ru",
                                             speaker=self.tts_voice, device=wanted)
                    warning = "TTS перенесён с %s на %s" % (current or "?", wanted)
                except Exception as e:
                    warning = "устройство сохранено, но TTS не перенесён: %s" % e

        settings.settings[key] = value
        settings.save_settings()
        return value, warning

    def web_settings_info(self):
        """Настройки для панели CONFIG. Секреты наружу не отдаются."""
        data = {}
        for key, value in settings.settings.items():
            if key in ("zen_api_key", "ai_api_key", "web_token"):
                data[key] = {"hidden": True, "set": bool(value), "length": len(value or "")}
            else:
                data[key] = value
        return data

    def change_stt_mode(self):
        list_stt_modes = ["whisper", "google"]
        sel = self._choice_menu("🎙 РЕЖИМ РАСПОЗНАВАНИЯ РЕЧИ (STT)", list_stt_modes, current=self.stt_mode)
        if sel is None:
            return
        self.stt_mode = sel.value
        settings.settings["stt_mode"] = self.stt_mode
        settings.save_settings()
        print(Fore.CYAN + "STT MODE CHANGED SUCCESSFULLY" + Style.RESET_ALL)

    def clear_context(self):
        self.global_context = ""
        print(Fore.CYAN + "CONTEXT CLEARED SUCCESSFULLY" + self.trigger_word + Style.RESET_ALL)

    def get_memory_from_file(self):
        try:
            if not self.memory_path.exists():
                self.memory_path.touch()
            with open(self.memory_path, "r", encoding="utf-8") as file:
                return file.read()
        except Exception as e:
            print("Error reading memory file: " + str(e))
            return "Error reading memory file: " + str(e)

    def change_spped_ai_voice(self):
        while True:
            print(Fore.CYAN + "СКОРОСТЬ РЕЧИ (0.5 — 3.0, Enter — отмена): " + Style.RESET_ALL, end="")
            try:
                raw = input().strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not raw:
                return
            try:
                value = float(raw)
            except ValueError:
                print(Fore.RED + "Нужно число, например 1.2" + Style.RESET_ALL)
                continue
            if not 0.5 <= value <= 3.0:
                print(Fore.RED + "Скорость должна быть от 0.5 до 3.0" + Style.RESET_ALL)
                continue
            self.speed_ai_speak = value
            settings.settings["speed_ai_speak"] = value
            settings.save_settings()
            print(Fore.GREEN + f"Скорость речи: {value}" + Style.RESET_ALL)
            return

    def _parse_command_json(self, payload):
        """Достать JSON-объект команды из ответа модели.

        Модель часто добавляет точку в конце или лишний текст после команды
        ('...args": {}}.'), поэтому целиком на json.loads не полагаемся.
        """
        start = payload.find("{")
        if start < 0:
            raise ValueError("в команде нет JSON-объекта")
        obj, _ = json.JSONDecoder().raw_decode(payload[start:])
        if not isinstance(obj, dict):
            raise ValueError("ожидался JSON-объект")
        if "command" not in obj:
            raise ValueError("в JSON нет поля 'command'")
        if not isinstance(obj.get("args", {}), dict):
            raise ValueError("поле 'args' должно быть объектом")
        obj.setdefault("args", {})
        return obj

    def format_response(self, text):
        if not text.startswith("command "):
            text = text.replace('**', ' ').replace('```', ' ').replace('*',' ')
        else:
            orig = text
            try:
                parsed = self._parse_command_json(text[len("command "):])
                type_command = parsed["command"]
                command = parsed["command"] + " " + str(parsed["args"])
                text = self.commands_ai[type_command]["icon"] + " " + command
            except Exception:
                return orig
        return text

    def make_voice_profile(self):
        recognizer = sr.Recognizer()
        with open_microphone(self.micro_index) as source:
            print(Fore.YELLOW + "MICROPHONE SETUP (BE SLIENT FOR 2 SECONDS)..." + Style.RESET_ALL)
            recognizer.adjust_for_ambient_noise(source, duration=2)

            print(Fore.YELLOW + "=== THE VOICE STANDARD WAS NOT FOUND ===" + Style.RESET_ALL)
            print("SAY A LONG PHRASE (5 SECONDS) SO THAT THE AI REMEMBERS YOUR TIMBRE...")
            try:
                print(Fore.CYAN + "THE RECORDIND HAS STERTED! SPEAK..." + Style.RESET_ALL)
                enroll_audio = recognizer.record(source, duration=5)
                with open(self.profile_wav, "wb") as f:
                    f.write(enroll_audio.get_wav_data())
                print(Fore.GREEN + "YOUR REFERENCE VOICE IS SAVED!\n" + Style.RESET_ALL)
            except Exception as e:
                print(Fore.RED + f"STANDARD RECORDING ERROR: {e}" + Style.RESET_ALL)
                return

            self.load_profile_tensor()

    def select_trigger_word(self):
        try:
            print(Fore.CYAN + "INPUT THE TRIGGER WORD: >> " + Style.RESET_ALL, end="")
            self.trigger_word = input()
            settings.settings["trigger_word"] = self.trigger_word
            settings.save_settings()
            print(Fore.CYAN + "TRIGGER WORD SET TO: " + self.trigger_word + Style.RESET_ALL)
        except Exception as e:
            print(Fore.RED + "ERROR SETTING THE TRIGGER WORD: " + str(e) + Style.RESET_ALL)

    def get_audio_tensor(self, audio_data):
        raw_data = audio_data.get_raw_data()
        audio_np = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(audio_np).unsqueeze(0)

        fs = audio_data.sample_rate
        if fs != 16000:
            resampler = T.Resample(orig_freq=fs, new_freq=16000)
            tensor = resampler(tensor)

        return tensor

    def load_profile_tensor(self):
        fs, prof_audio = wav.read(self.profile_wav)
        prof_audio = prof_audio.astype(np.float32) / 32768.0
        tensor = torch.from_numpy(prof_audio).unsqueeze(0)

        if fs != 16000:
            resampler = T.Resample(orig_freq=fs, new_freq=16000)
            tensor = resampler(tensor)

        self.profile_tensor = tensor

    def show_settings(self):
        lines = Text()
        secret_keys = ("zen_api_key", "ai_api_key", "web_token")
        for key in settings.settings:
            if key in secret_keys:
                val = settings.settings[key]
                val_txt = ('*' * len(val)) if isinstance(val, str) and val else "(пусто)"
                lines.append(f"  {key}  ", style="bold #89b4fa")
                lines.append(f"{val_txt}\n", style="#cdd6f4")
            else:
                lines.append(f"  {key}  ", style="bold #89b4fa")
                lines.append(f"{settings.settings[key]}\n", style="#cdd6f4")
        lines.append("  provider_active  ", style="bold #89b4fa")
        lines.append(f"{self.ai_label()}\n", style="#cdd6f4")
        lines.append("  model_active  ", style="bold #89b4fa")
        lines.append(f"{self.ai_active_model() or 'не выбрана'}\n", style="#cdd6f4")
        lines.append("  tts_device_active  ", style="bold #89b4fa")
        lines.append(f"{getattr(self.tts, 'device', '?')}\n", style="#cdd6f4")
        if self.provider == self.CUSTOM_PROVIDERS:
            lines.append("  custom_providers  ", style="bold #89b4fa")
            names = ", ".join(e.get("name") or e.get("base_url", "")
                              for e in self.custom_providers()) or "нет"
            lines.append(f"{names}\n", style="#cdd6f4")
        self.console.print(Panel(lines, title="⚙ НАСТРОЙКИ", border_style="cyan", box=ROUNDED, padding=(0, 1)))

    def create_second_system_prompt(self):
        self.second_system_prompt = "Вот вывод предыдущих команд и указание пользователя: " + self.second_context + """\nЕсли ты считаешь что ты выполнил указание то ответь просто текстом на поставленный user'ом вопрос.
Если ты не считаешь что ты выполнил указание/недовыполнил указание то закончи его выполнение командой,если произошла ошибка то ответь про ошибку текстом.
1. Если нужно выполнить команду, то ответ должен начинаться с command и быть в формате JSON,ОТВЕТ НЕ ДОЛЖЕН СОДЕРЖАТЬ НИЧЕГО ЛИШНЕГО КРОМЕ СЛОВА COMMAND В НАЧАЛЕ И САМОЙ КОМАНДЫ,к некоторым командам НЕ нужны аргументы(пример):
command {"command": "название_команды", "args": {"аргумент1": "значение1", "аргумент2": "значение2"}}. 
2. Если ты хочешь просто ответить текстом, то ответ должен быть просто текстом(пример): Вместо этого тут должен быть твой ответ на запрос user'а.
Вот список доступных команд и их аргуметов: """ + self.commands_ai_and_args
        return self.second_system_prompt

    def parse_ai_command(self, full_command):
        if full_command.startswith("command "):
            command = None
            try:
                command = self._parse_command_json(full_command[len("command "):])
                name = command["command"]
                if name not in self.commands_ai:
                    known = ", ".join(sorted(self.commands_ai))
                    raise ValueError(f"неизвестная команда '{name}'. Доступные: {known}")
                kwargs = self._bind_ai_args(name, command["args"])
                self.ai_state = "command"
                try:
                    value, output, error, elapsed = self._invoke_ai_command(
                        name, kwargs, self.AI_COMMAND_TIMEOUT)
                finally:
                    self.ai_state = "speaking" if self.ai_speak else "idle"

                # журнал — общий с сайтом, чтобы команда и вывод были видны в вебе
                self._record_ai_command(name, kwargs, value, output, error, elapsed, "ai")
                if error is not None:
                    res = "ОШИБКА: %s: %s" % (type(error).__name__, error)
                    self._last_cmd = (None, res)      # так консоль рисует это как ошибку
                else:
                    res = output or ("" if value is None else str(value))
                    self._last_cmd = (command, res)
                note = "\nAI executed command:   " + name + "   With args: " + str(command["args"]) + " and result: " + res
                self.second_context += note
                self.global_context += note
                return 0
            except Exception as e:
                self._last_cmd = (None, f"ОШИБКА: {e}")
                # команда не выполнилась, но пользователь должен видеть это и на сайте
                name = (command or {}).get("command")
                if name:
                    self._record_ai_command(name, (command or {}).get("args") or {},
                                            None, "", e, 0.0, "ai")
                self.second_context += "\nAI: " + full_command + "   BUT THERE WAS AN ERROR IN PARSING OR EXECUTING THE COMMAND: " + str(e)
                self.global_context += "\nAI: " + full_command + "   BUT THERE WAS AN ERROR IN PARSING OR EXECUTING THE COMMAND: " + str(e)
                return 0
        else:
            self.global_context += "\nAI: " + full_command
            return 1

    def _render_ai_response(self, raw, formatted=None):
        if raw.startswith("command "):
            formatted = str(self.format_response(raw))
            res = ""
            has_err = False
            if getattr(self, "_last_cmd", None):
                if self._last_cmd[0] is not None:
                    res = self._last_cmd[1]
                else:
                    res = self._last_cmd[1]
                    has_err = True
                self._last_cmd = None
            body = Text()
            body.append(" " + formatted + "\n", style="bold yellow")
            res_str = "" if res is None else str(res)
            if res_str.strip() and not has_err:
                body.append("\n  ─ Результат: ─\n", style="bold #a6e3a1")
                # подсветка терминал-вывода зелёным, обрезка длинного
                shown = res_str if len(res_str) <= 1500 else res_str[:1500] + "\n…(обрезано)"
                body.append(shown, style="bright_green")
            elif has_err and res_str.strip():
                body.append("\n  ─ Ошибка: ─\n", style="bold red")
                body.append(res_str, style="red")
            else:
                body.append("\n  (вывод пустой — команда могла ничего не вернуть)", style="dim")
            self.console.print(Panel(
                body,
                title=f"🤖 {str(self.trigger_word or 'AI').upper()} · Команда выполнена", title_align="left",
                border_style="yellow", box=ROUNDED, padding=(0, 1),
            ))
        else:
            self._panel(f"🤖 {str(self.trigger_word or 'AI').upper()}", raw, style="ai")

    def clear_console(self):
        subprocess.run("clear", shell=True)
        self._banner()

    def select_micro(self):
        mic_list = list_microphones()
        entries = [console_ui.Item(i, name, current=(i == self.micro_index))
                   for i, name in enumerate(mic_list)]
        sel = console_ui.select("🎙 ДОСТУПНЫЕ МИКРОФОНЫ", entries, hint=console_ui.HINT, console=self.console)
        if sel is None:
            return
        self.micro_index = sel.value
        settings.settings["micro_index"] = self.micro_index
        settings.save_settings()
        print(Fore.GREEN + "MICROPHONE SWITCHED TO   " + str(self.micro_index) + "   SUCCESSFULLY" + Style.RESET_ALL)
        print(Fore.BLUE + "PLEASE RESTART THE PROGRAM TO APPLY THE CHANGES" + Style.RESET_ALL)

    def select_tts_voice(self):
        voices = self.tts.get_available_speakers()
        sel = self._choice_menu("🎙 ГОЛОСА TTS", voices, current=self.tts_voice)
        if sel is None:
            return
        self.tts_voice = sel.value
        settings.settings["tts_voice"] = self.tts_voice
        settings.save_settings()
        print(Fore.GREEN + "TTS VOICE SWITCHED TO   " + str(self.tts_voice) + "   SUCCESSFULLY" + Style.RESET_ALL)

    def select_whisper_model(self):
        if self.stt_mode != "google":
            self.whisper_models = available_models()
            sel = self._choice_menu("🎙 МОДЕЛИ WHISPER", self.whisper_models, current=self.whisper_model)
            if sel is None:
                return
            self.whisper_model = sel.value
            if self.whisper_model in self.whisper_models:
                self.whisper_load_model = load_whisper(self.whisper_model)
                settings.settings["whispermodel"] = self.whisper_model
                settings.save_settings()
                print(Fore.GREEN + "WHISPER MODEL SWITCHED TO   " + str(self.whisper_model) + "   SUCCESSFULLY" + Style.RESET_ALL)
            else:
                print(Fore.YELLOW + "UNKNOWN MODEL NAME" + Style.RESET_ALL)

    # ─────────────── единый центр настройки ИИ ───────────────

    def ai_hub(self):
        """Один пункт вместо /provider, /ai_model, /zen_model, /zen_api, /ai_provider.

        Меню верхнего уровня: переключение провайдера. Для выбранного провайдера —
        единый подменю с моделью, ключом и адресом.
        """
        while True:
            entries = [console_ui.Section("ВСТРОЕННЫЕ")]
            for kind, desc in (("ollama", "локальные модели, localhost:11434"),
                               ("zen", "OpenCode Zen, облачные модели")):
                label = self.ai_label() if self.provider == kind else None
                mark = "  ← сейчас" if self.provider == kind else ""
                entries.append(console_ui.Item(
                    kind, ("Ollama" if kind == "ollama" else "OpenCode Zen") + mark, desc))

            customs = self.custom_providers()
            if customs:
                entries.append(console_ui.Section("КАСТОМНЫЕ (OpenAI-совместимые)"))
                for e in customs:
                    is_cur = (self.provider == self.CUSTOM_PROVIDERS
                              and e.get("id") == self.active_custom_id())
                    mark = "  ← сейчас" if is_cur else ""
                    entries.append(console_ui.Item(
                        "custom:" + e.get("id", ""),
                        (e.get("name") or e.get("base_url")) + mark,
                        "%s · %s%s" % (e.get("base_url", ""), e.get("model") or "модель не выбрана",
                                       " · ключ задан" if e.get("api_key") else "")))

            entries.append(console_ui.Section("ДЕЙСТВИЯ"))
            entries.append(console_ui.Item("__setup__", "Настроить кастомного провайдера",
                                           "адрес, ключ, модель — сохранить в список", badge="+"))
            entries.append(console_ui.Item("__current__", "Настроить текущего провайдера",
                                           "модель, ключ, таймаут"))
            entries.append(console_ui.Item("__back__", "Готово", "вернуться в меню LUCH"))

            sel = console_ui.select(
                "🧠 ПРОВАЙДЕР И МОДЕЛЬ ИИ", entries,
                subtitle="сейчас: %s" % self.ai_summary(),
                hint=console_ui.HINT, height=20, console=self.console,
            )
            if sel is None or sel.value == "__back__":
                return

            try:
                if sel.value in ("ollama", "zen"):
                    self.set_active_provider(sel.value)
                    print(Fore.GREEN + "ПРОВАЙДЕР: %s" % self.ai_label() + Style.RESET_ALL)
                    if not self.ai_active_model():
                        self._ai_ask_model(sel.value)
                elif sel.value.startswith("custom:"):
                    entry = next((e for e in self.custom_providers()
                                  if e.get("id") == sel.value.split(":", 1)[1]), None)
                    if not entry:
                        print(Fore.RED + "Провайдер не найден в списке" + Style.RESET_ALL)
                        continue
                    self.activate_custom_provider(entry)
                    print(Fore.GREEN + "ПРОВАЙДЕР: %s" % self.ai_label() + Style.RESET_ALL)
                elif sel.value == "__setup__":
                    if not self._ai_setup_custom():
                        return
                elif sel.value == "__current__":
                    self._ai_configure_current()
            except Exception as e:
                print(Fore.RED + "Ошибка: %s" % e + Style.RESET_ALL)

    def _ai_ask_model(self, kind):
        """Выбор модели для встроенного провайдера (ollama / zen)."""
        if kind == "ollama":
            try:
                self.models_info = ollama.list()
            except Exception as e:
                print(Fore.RED + "Не удалось получить список моделей Ollama: %s" % e + Style.RESET_ALL)
                self.models_info = []
            names = [m["name"] for m in self.models_info if isinstance(m, dict) and m.get("name")]
            if not names:
                names = [m["name"] for m in self.ollama_data.get("models", [])
                         if isinstance(m, dict) and m.get("name")]
            if not names:
                print(Fore.YELLOW + "В Ollama нет загруженных моделей. Скачай модель командой /pull"
                      + Style.RESET_ALL)
                return
            self.models = names
            sel = self._choice_menu("🧠 МОДЕЛИ OLLAMA", self.models, current=self.model,
                                    allow_custom=True)
            if sel is None:
                return
            model = self._ai_manual_or_value(sel, "имя модели Ollama")
            if not model:
                return
            self.model = model
            settings.settings["model"] = model
            settings.settings["provider"] = "ollama"
            self.provider = "ollama"
            settings.save_settings()
            print(Fore.GREEN + "МОДЕЛЬ OLLAMA: %s" % model + Style.RESET_ALL)
            self.user_prompt = None
        else:
            self._ai_setup_zen()

    @staticmethod
    def _ai_manual_or_value(sel, prompt):
        """Если в меню выбрано «ввести вручную» — спрашиваем строку."""
        if getattr(sel, "custom", False):
            print(Fore.CYAN + "  %s (пусто — отмена): " % prompt + Style.RESET_ALL, end="")
            return str(input()).strip()
        return str(sel.value).strip()

    def _ai_setup_zen(self):
        """Ключ и модель OpenCode Zen."""
        if not self.zen_api_key:
            print(Fore.CYAN + "API-КЛЮЧ OPENCODE ZEN: >> " + Style.RESET_ALL, end="")
            key = str(input()).strip()
            if not key:
                print(Fore.YELLOW + "Ключ не задан — узнать модель не получится" + Style.RESET_ALL)
            else:
                self.zen_api_key = key
                settings.settings["zen_api_key"] = key
                settings.save_settings()
                print(Fore.GREEN + "КЛЮЧ ZEN СОХРАНЁН" + Style.RESET_ALL)
        models = []
        try:
            headers = {"Authorization": f"Bearer {self.zen_api_key}"} if self.zen_api_key else {}
            r = requests.get(self.zen_models_url, headers=headers, timeout=15)
            data = r.json()
            models = ([m.get("id", m) for m in data] if isinstance(data, list)
                      else ([m.get("id") for m in data.get("data", [])] if "data" in data else []))
        except Exception as e:
            print(Fore.YELLOW + "Список моделей Zen недоступен (%s)" % e + Style.RESET_ALL)
        model = ""
        if models:
            sel = self._choice_menu("🧠 МОДЕЛИ OPENCODE ZEN", models, current=self.zen_model,
                                    allow_custom=True,
                                    prompt="можно выбрать стрелками или вписать имя вручную")
            if sel is None:
                return
            model = self._ai_manual_or_value(sel, "имя модели Zen")
        else:
            print(Fore.CYAN + "  ИМЯ МОДЕЛИ ZEN (пусто — отмена): >> " + Style.RESET_ALL, end="")
            model = str(input()).strip()
        if not model:
            print(Fore.YELLOW + "Модель не выбрана" + Style.RESET_ALL)
            return
        self.zen_model = model
        self.provider = "zen"
        settings.settings["zen_model"] = model
        settings.settings["provider"] = "zen"
        settings.save_settings()
        print(Fore.GREEN + "ПРОВАЙДЕР: OpenCode Zen · %s" % model + Style.RESET_ALL)
        self.user_prompt = None

    def _ai_setup_custom(self):
        """Создание нового кастомного провайдера. False — пользователь отказался."""
        name = self._ask("название (Enter — из адреса)", "")
        if name is None:
            return False
        raw = self._ask("адрес API — http://host:port/v1 или полный .../chat/completions (0 — отмена)", "")
        if raw is None or not raw.strip() or raw.strip().lower() in ("0", "q", "quit", "отмена", "выход"):
            print(Fore.YELLOW + "отмена" + Style.RESET_ALL)
            return False
        base = self._normalize_ai_base_url(raw)
        if not base.lower().startswith(("http://", "https://")):
            print(Fore.RED + "адрес должен начинаться с http:// или https://" + Style.RESET_ALL)
            return False

        print(Fore.CYAN + "API-ключ (Enter — без ключа): >> " + Style.RESET_ALL, end="")
        key = str(input()).strip()

        model = (self._ask("модель (Enter — список с сервера)", "") or "").strip()
        if not model:
            models, err = self.list_ai_models(base_url=base, api_key=key)
            if not models:
                print(Fore.RED + "модель не указана, сервер её не перечислил: %s" % (err or "") + Style.RESET_ALL)
                return False
            sel = self._choice_menu("🧠 МОДЕЛИ СЕРВЕРА", models, allow_custom=True)
            if sel is None:
                print(Fore.YELLOW + "отмена" + Style.RESET_ALL)
                return False
            model = self._ai_manual_or_value(sel, "имя модели")
        if not model:
            return False

        timeout = self._ask("таймаут в секундах", "120") or "120"
        entry = self.save_custom_provider(name, base, key, model, timeout)

        print("")
        print(Fore.CYAN + "проверяю соединение…" + Style.RESET_ALL)
        ok, msg = self.test_ai_endpoint(base_url=base, api_key=key, model=model)
        if ok:
            print(Fore.GREEN + "OK — %s" % str(msg)[:120] + Style.RESET_ALL)
        else:
            print(Fore.RED + "сервер не ответил: %s" % msg + Style.RESET_ALL)
            print(Fore.YELLOW + "провайдер всё равно сохранён" + Style.RESET_ALL)

        self.activate_custom_provider(entry)
        print(Fore.GREEN + "ДОБАВЛЕН И АКТИВЕН: %s" % self.ai_label() + Style.RESET_ALL)
        print("адрес запроса: %s" % self._ai_chat_url())
        return True

    def _ai_configure_current(self):
        """Модель / ключ / таймаут для уже активного провайдера."""
        kind = (self.provider or "ollama").strip().lower()
        if kind == "ollama":
            self._ai_ask_model("ollama")
            return
        if kind == "zen":
            self._ai_setup_zen()
            return
        entry = self.active_custom() or {
            "id": self._slug(self.ai_base_url), "name": self.ai_label(),
            "base_url": self.ai_base_url, "api_key": self.ai_api_key,
            "model": self.ai_model, "timeout": self.ai_timeout,
        }
        print(Fore.CYAN + "настраиваю: %s" % (entry.get("name") or entry.get("base_url")) + Style.RESET_ALL)
        base = self._ask("адрес API (Enter — текущий, 0 — отмена)", entry.get("base_url", ""))
        if base is None or base.strip().lower() in ("0", "q", "quit", "отмена", "выход"):
            print(Fore.YELLOW + "отмена — настройки не менялись" + Style.RESET_ALL)
            return
        if base.strip() == "-":
            base = entry.get("base_url", "")
        else:
            base = self._normalize_ai_base_url(base)
            if base and not base.lower().startswith(("http://", "https://")):
                print(Fore.RED + "адрес должен начинаться с http:// или https://" + Style.RESET_ALL)
                return

        key_arg = entry.get("api_key", "") or ""
        if entry.get("api_key"):
            print(Fore.CYAN + "API-ключ ('-' оставить, Enter — убрать): >> " + Style.RESET_ALL, end="")
            try:
                key = str(input()).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            key_arg = entry.get("api_key", "") if key == "-" else key
        else:
            print(Fore.CYAN + "API-ключ (Enter — без ключа): >> " + Style.RESET_ALL, end="")
            try:
                key_arg = str(input()).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return

        model = (self._ask("модель (Enter — текущая, или список с сервера)", entry.get("model", "")) or "").strip()
        if not model:
            models, err = self.list_ai_models(base_url=base, api_key=key_arg or entry.get("api_key"))
            if not models:
                print(Fore.RED + "модель не указана, сервер её не перечислил: %s" % (err or "") + Style.RESET_ALL)
                return
            sel = self._choice_menu("🧠 МОДЕЛИ СЕРВЕРА", models, allow_custom=True)
            if sel is None:
                return
            model = self._ai_manual_or_value(sel, "имя модели")
        if not model:
            return

        timeout = self._ask("таймаут в секундах (Enter — %s)" % entry.get("timeout", 120),
                            str(entry.get("timeout", 120)))
        if timeout is None:
            return

        new_entry = self.save_custom_provider(
            entry.get("name") or self._slug(base), base or entry.get("base_url"),
            key_arg, model, timeout, keep_id=entry.get("id"))
        self.activate_custom_provider(new_entry)
        print(Fore.GREEN + "ПРОВАЙДЕР: %s · %s" % (self.ai_label(), model) + Style.RESET_ALL)
        print("адрес запроса: %s" % self._ai_chat_url())

    def _ask(self, prompt, default=""):
        """Вопрос в консоли. Пустая строка берёт default, None — отмена (Ctrl-C)."""
        suffix = " [%s]" % default if default else ""
        print(Fore.CYAN + prompt + suffix + ": >> " + Style.RESET_ALL, end="")
        try:
            answer = str(input()).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return None
        return answer if answer else default

    def select_zen_model(self):
        """Выбор модели из списка моделей OpenCode Zen (через эндпоинт /models)."""
        models = []
        try:
            headers = {}
            if self.zen_api_key:
                headers["Authorization"] = f"Bearer {self.zen_api_key}"
            r = requests.get(self.zen_models_url, headers=headers, timeout=15)
            data = r.json()
            models = [m.get("id", m) for m in data] if isinstance(data, list) else \
                     ([m.get("id") for m in data.get("data", [])] if "data" in data else [])
        except Exception as e:
            print(Fore.YELLOW + f"Не удалось получить список моделей Zen ({e})." + Style.RESET_ALL)
            models = []
        if models:
            sel = self._choice_menu("🧠 МОДЕЛИ OPENCODE ZEN", models, current=self.zen_model,
                                    allow_custom=True, prompt="можно выбрать стрелками или вписать имя вручную")
            if sel is None:
                return
            if sel.custom:
                inpraw = input(Fore.CYAN + "  Впиши имя модели (пусто — отмена): " + Style.RESET_ALL).strip()
                if not inpraw:
                    return
                self.zen_model = inpraw
            else:
                self.zen_model = sel.value
        else:
            name_in = input(Fore.CYAN + "INPUT ZEN MODEL NAME (e.g. gpt-5.4-mini, gpt-4.1-mini): >> " + Style.RESET_ALL).strip()
            if not name_in:
                return
            self.zen_model = name_in
        self.provider = "zen"
        settings.settings["zen_model"] = self.zen_model
        settings.settings["provider"] = "zen"
        settings.save_settings()
        self._panel("☑ МОДЕЛЬ ZEN", f"Установлена модель: [bold]{self.zen_model}[/bold]", style="ok")
        self.user_prompt = None

    def create_prompt(self):
        if self.second_context != "USER:  " + self.user_prompt:
            return self.global_context + "\n\n\n" + self.create_second_system_prompt() + "\nОТВЕЧАЙ НА ТЕКУЩИЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ ИЛИ ДЕЛАЙ ТО ЧТО СКАЗАЛ ПОЛЬЗОВАТЕЛЬ В ТЕКУЩЕМ ЗАПРОСЕ"
        else:
            return self.global_context + "\n\n\n" + "Вот запрос пользователя:  " + self.user_prompt + "\n" + "Вот системное указание для тебя: " + self.system_prompt + "\nОТВЕЧАЙ НА ТЕКУЩИЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ ИЛИ ДЕЛАЙ ТО ЧТО СКАЗАЛ ПОЛЬЗОВАТЕЛЬ В ТЕКУЩЕМ ЗАПРОСЕ"

    def _menu_entries(self):
        """Собирает пункты главного меню по группам (логичный порядок)."""
        entries = []
        for group_title, keys in self.menu_layout:
            entries.append(console_ui.Section(group_title))
            for key in keys:
                if key not in self.user_commands:
                    continue
                entries.append(console_ui.Item(key, f"/{key}", self.user_commands[key][1]))
        return entries

    def _state_badge(self, key):
        """Текущее значение настройки для подсказки в меню."""
        try:
            if key in ("ai", "provider", "ai_model", "zen_model", "zen_api", "ai_provider"):
                return self.ai_summary()
            if key == "tts_voice":
                return str(self.tts_voice)
            if key == "change_speed_ai_voice":
                return "x" + str(self.speed_ai_speak)
            if key == "trigger_word":
                return str(self.trigger_word)
            if key == "change_stt_mode":
                return str(self.stt_mode)
            if key == "whisper_model":
                return str(self.whisper_model or "не выбрана")
            if key == "micro":
                return f"индекс {self.micro_index}"
        except Exception:
            pass
        return None

    def open_menu(self):
        entries = self._menu_entries()
        subtitle = f"ИИ: {self.ai_summary()}"
        hint = console_ui.HINT + "  ·  /menu — снова"
        for e in entries:
            if not isinstance(e, console_ui.Section):
                e.badge = self._state_badge(e.value) or ""
        sel = console_ui.select("⚙ МЕНЮ LUCH", entries, subtitle=subtitle, hint=hint,
                                height=22, console=self.console)
        if sel is None:
            print(Fore.YELLOW + "  Меню закрыто" + Style.RESET_ALL)
            return
        if sel.value == "chat":
            print(Fore.GREEN + "  Продолжаю диалог с текущей моделью" + Style.RESET_ALL)
            return
        try:
            result = self.process_command("/" + sel.value)
            if result not in ("OK", "EXITING"):
                print(Fore.RED + f"Ошибка выполнения пункта меню: {result}" + Style.RESET_ALL)
        except Exception as e:
            print(Fore.RED + f"Ошибка выполнения пункта меню: {e}" + Style.RESET_ALL)

    def generate_ai_response(self):
        self.response = ""
        provider = (self.provider or "ollama").strip().lower()
        if provider == "zen":
            self._generate_zen()
        elif provider in ("openai", "custom", "api", "compatible"):
            self._generate_openai()
        else:
            self._generate_ollama()

    def _generate_ollama(self):
        # Формируем payload точно так же, как вы делали
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": self.create_prompt()}],
            "stream": True,
            "options": {
                "think": False,
                "num_predict": 512
            }
        }
        
        # Отправляем POST‑запрос на эндпоинт /api/chat
        response_text = ""
        try:
            with requests.post(self.ollama_url + "chat", json=payload, stream=True, timeout=120) as r:
                r.raise_for_status()  # если статус не 200 — выбросит исключение
                for line in r.iter_lines(decode_unicode=True):
                    if line:   # пропускаем пустые строки (kseep-alive)
                        try:
                            chunk = json.loads(line)
                            # извлекаем содержимое из потока
                            if 'message' in chunk and 'content' in chunk['message']:
                                response_text += chunk['message']['content']
                        except json.JSONDecodeError:
                            # если вдруг пришёл невалидный JSON — игнорируем
                            continue
        except Exception as e:
            print(Fore.RED + f"Ollama error: {e}" + Style.RESET_ALL)
            response_text = (f"Не удалось получить ответ от Ollama: {e}. "
                             f"Проверь, что Ollama запущена (localhost:11434) и модель '{self.model}' загружена.")
        self.response = response_text

    def _generate_zen(self):
        """Обращение к OpenCode Zen (OpenAI-совместимый эндпоинт /chat/completions)."""
        if not self.zen_api_key:
            self.response = "Не задан API-ключ OpenCode Zen. Задай его через команду /zen_api или в settings.json."
            print(Fore.RED + "OpenCode Zen: не задан API-ключ" + Style.RESET_ALL)
            return
        payload = {
            "model": self.zen_model,
            "messages": [{"role": "system", "content": self.system_prompt + self.create_second_system_prompt()},
                         {"role": "user", "content": self.create_prompt()}],
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.zen_api_key}",
            "Content-Type": "application/json",
        }
        response_text = ""
        try:
            with requests.post(self.zen_url, json=payload, headers=headers, stream=True,
                               timeout=(15, 300)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                        if 'choices' in chunk and chunk['choices']:
                            delta = chunk['choices'][0].get('delta', {})
                            piece = delta.get('content', '')
                            if piece:
                                response_text += piece
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(Fore.RED + f"OpenCode Zen error: {e}" + Style.RESET_ALL)
            response_text = f"Ошибка OpenCode Zen: {e}. Проверь API-ключ и модель '{self.zen_model}'."
        self.response = response_text

    def animation_thinking(self):
        # подпись берётся из реально активного провайдера, а не по умолчанию Ollama
        text = f" {self.ai_label()} думает..."
        try:
            from rich.live import Live
            from rich.spinner import Spinner
            with Live(Spinner("dots", text=text), refresh_per_second=12, transient=True) as live:
                while self.ai_thread.is_alive():
                    time.sleep(0.05)
        except Exception:
            while self.ai_thread.is_alive():
                for i in range(5):
                    if not self.ai_thread.is_alive():
                        break
                    print(" " * 50, end="\r")
                    print(Fore.MAGENTA + text + Style.RESET_ALL, end="\r")
                    time.sleep(0.08)
            print(" " * 50, end="\r")

    def speed_up_audio(self, path):
        """Применить скорость речи через ffmpeg. Возвращает True, если файл ускорен.
        Ничего не ломает и не падает, если ffmpeg недоступен или скорость вне диапазона."""
        try:
            speed = float(self.speed_ai_speak)
        except (TypeError, ValueError):
            return False
        if abs(speed - 1.0) < 0.01:
            return False

        src = str(path)
        filters = []
        # ffmpeg atempo умеет только 0.5–2.0, поэтому длинную цепочку строим по частям
        while speed > 2.0:
            filters.append("atempo=2.0")
            speed /= 2.0
        if filters:
            filters.append(f"atempo={speed:.6f}")
        elif speed < 0.5:
            filters.append(f"atempo={speed:.6f}")
        else:
            filters.append(f"atempo={speed:.6f}")

        # Временный файл обязан лежать рядом с исходным: /tmp и домашний каталог
        # часто на разных ф.sys, и os.replace() между ними падает с EXDEV.
        folder = os.path.dirname(os.path.abspath(src)) or "."
        try:
            fd, fast_path = tempfile.mkstemp(prefix=".luch_fast_", suffix=".wav", dir=folder)
        except OSError:
            fd, fast_path = tempfile.mkstemp(prefix="luch_fast_", suffix=".wav")
        os.close(fd)
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "quiet", "-i", src,
                 "-filter:a", ",".join(filters), fast_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
            if proc.returncode != 0 or os.path.getsize(fast_path) == 0:
                return False
            try:
                os.replace(fast_path, src)
            except OSError:
                # запасной путь, если каталоги всё же на разных ф.sys
                shutil.copyfile(fast_path, src)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            work_fuctions.log_event("voice_errors", f"не удалось применить скорость речи: {e}")
            print(Fore.YELLOW + f"Не удалось применить скорость речи: {e}" + Style.RESET_ALL)
            return False
        finally:
            try:
                os.unlink(fast_path)
            except OSError:
                pass

    def speak_text(self, text):
        """Озвучить текст с учётом скорости. Ошибки не пробрасываются наружу."""
        path = settings.PATHS["response_path"]
        try:
            self.tts.tts(text, path)
        except Exception as e:
            print(Fore.RED + f"Ошибка синтеза речи: {e}" + Style.RESET_ALL)
            return False
        self.speed_up_audio(path)
        try:
            self.speak_thread = threading.Thread(target=self.play_response, daemon=True)
            self.speak_thread.start()
        except Exception as e:
            print(Fore.RED + f"Не удалось запустить озвучку: {e}" + Style.RESET_ALL)
            return False
        self.ai_speak = True
        self.ai_state = "speaking"
        return True

    def play_response(self):
        self.ai_state = "speaking"
        try:
            sound = pygame.mixer.Sound(settings.PATHS["response_path"])
            sound_duration = int(sound.get_length() * 1000)
            sound.play()
            pygame.time.wait(sound_duration)
        except Exception as e:
            print(Fore.RED + f"Ошибка воспроизведения: {e}" + Style.RESET_ALL)
        if self.ai_speak == True:
            self.ai_speak = False
        self.ai_state = "idle"

    def play_timer_tts(self, text, path):
        """Эта функция вызывается из work_fuctions, когда срабатывает таймер"""
        try:
            self.tts.tts(text, path)
            self.speed_up_audio(path)
            sound = pygame.mixer.Sound(path)
            sound.play()
        except Exception as e:
            print(f"Timer TTS error: {e}")

    def full_relese_response(self):
        self.second_context = "USER:  " + self.user_prompt
        self.global_context += "\nUSER:  " + self.user_prompt
        if "заблокируй компьютер" in self.user_prompt or "заблокируй пк" in self.user_prompt or "заблокируй комп" in self.user_prompt:
            work_fuctions.lock_pc()
            self.user_prompt = None
            pass
        if len(self.global_context) > 1000:
            us_metka = self.global_context.find("USER:")
            self.global_context = self.global_context[us_metka:]
        self.ai_thread = threading.Thread(target=self.generate_ai_response, daemon=True)
        self.ai_state = "thinking"
        self.ai_thread.start()
        self.animation_thinking()
        if self.ai_thread and self.ai_thread.is_alive():
            self.ai_thread.join()
        i2 = self.parse_ai_command(self.response)
        self._render_ai_response(self.response)
        if i2 == 1:
            self.speak_text(self.response)
        rounds = 0
        while i2 == 0 and rounds < 20:
            rounds += 1
            self.ai_thread = threading.Thread(target=self.generate_ai_response, daemon=True)
            self.ai_state = "thinking"
            self.ai_thread.start()
            self.animation_thinking()
            self.ai_thread.join()
            i2 = self.parse_ai_command(self.response)
            self._render_ai_response(self.response)
            if i2 == 1:
                self.speak_text(self.response)
        if i2 == 0:
            print(Fore.YELLOW + "ИИ слишком долго просит команды — останавливаюсь." + Style.RESET_ALL)
        if not self.ai_speak:
            self.ai_state = "idle"
        self.user_prompt = None
        self.response_ready_event.set()
        self._print_prompt()

    def listen_user(self, from_file=False):
        recognizer = sr.Recognizer()

        def process_audio(audio):
            self.ai_state = "listening"
            text = ""
            self.is_voice_success = False

            if not from_file:
                with open(settings.PATHS["temp_wav_file"], "wb") as f:
                    f.write(audio.get_wav_data())

            temp_tensor = self.get_audio_tensor(audio)
            score, prediction = self.voice_verifier.verify_batch(self.profile_tensor, temp_tensor)

            if self.stt_mode == "whisper":
                try:
                    segments, info = self.whisper_load_model.transcribe(
                        settings.PATHS["temp_wav_file"],
                        language="ru"
                    )
                    text = " ".join([segment.text for segment in segments]).strip()
                except Exception as e:
                    print(e)
                    text = ""
            elif self.stt_mode == "google":
                try:
                    with sr.AudioFile(settings.PATHS["temp_wav_file"]) as audio_file:
                        audio_data = recognizer.record(audio_file)
                        text = recognizer.recognize_google(audio_data, language="ru-RU")
                except Exception as e:
                    print(e)
                    text = ""

            if prediction.item() is True:
                if not self.ai_speak:
                    if text and self.user_prompt is None:
                        has_trigger = text[0:len(self.trigger_word)].lower() == self.trigger_word.lower()

                        if self.ai_thread.is_alive() == False and has_trigger:
                            self.type_work = "voice"
                            print(Fore.MAGENTA + f"[VOICE MATCHED]: {text}" + Style.RESET_ALL)

                            # Отрезаем триггер-слово из текста перед отправкой в ИИ
                            clean_prompt = text[len(self.trigger_word):].strip(" ,.!?-")
                            self.user_prompt = clean_prompt if clean_prompt else "Привет!"

                            self.full_relese_response()
                            self.type_work = "chat"
                            self.is_voice_success = True
                        elif not has_trigger:
                            print(Fore.YELLOW + f"[TRIGGER MISSING]: Фраза '{text}' проигнорирована (нет триггер-слова)." + Style.RESET_ALL)
                            self.response = f"Сообщение проигнорировано: отсутствует триггер-слово '{self.trigger_word}'."
                            self.ai_state = "idle"
                            self.ignore_response = "trigger"
                else:
                    if text:
                        for frase in self.stop_speak_flags:
                            if frase in text.lower():
                                self.ai_speak = False
                                pygame.mixer.stop()
                                self.stop_audio_triggered = True
                                self.response = ""
                                return
            else:
                score_value = float(score.item())
                work_fuctions.log_event(
                    "access_denied",
                    f"Чужой голос! score={score_value:.4f} | текст: {text!r}",
                    alert="🔒 Отказано: чужой голос",
                )
                with open(BASE_DIR / "phrases_not_spoken_in_my_voice.txt", "a", encoding="utf-8") as f:
                    f.write(text + "\n")
                self.response = "Доступ запрещен: ваш голос не совпадает с владельцем."
                self.ai_state = "idle"
                self.ignore_response = "voice"

        if from_file:
            wav_path = settings.PATHS["temp_wav_file"]
            if not os.path.exists(wav_path):
                self.response = "Файл записи не найден"
                self.response_ready_event.set()
                return
            try:
                with sr.AudioFile(wav_path) as source:
                    audio = recognizer.record(source)
                process_audio(audio)
            except Exception as e:
                print(Fore.RED + f"Ошибка чтения файла: {e}" + Style.RESET_ALL)
                self.response = f"Ошибка обработки: {e}"
            finally:
                self.response_ready_event.set()
        else:
            # Обычное прослушивание микрофона ПК в бесконечном цикле
            recognizer.pause_threshold = 1.5
            with open_microphone(self.micro_index, sample_rate=48000, chunk_size=2048) as source:
                recognizer.adjust_for_ambient_noise(source, duration=2)
                while True:
                    audio = recognizer.listen(source)
                    process_audio(audio)

    def process_command(self, cmd):
        cmd = (cmd or "").strip()
        if cmd.startswith("/") and cmd[1:] in self.user_commands:
            try:
                self.user_commands[cmd[1:]][0]()
            except SystemExit:
                # /exit из веба или голоса: не роняем поток, а просим главный цикл завершиться
                self.should_exit = True
                return "EXITING"
            except Exception as e:
                return f"ERROR: {e}"
            return "OK"
        return "UNKNOWN COMMAND"

    def wait_input(self):
        while not self.should_exit:
            if self.user_prompt is None:
                try:
                    self.user_prompt = input()
                except (EOFError, KeyboardInterrupt):
                    # нет TTY или ввод закрыт — не оставляем мёртвый поток с трассировкой
                    self.should_exit = True
                    return

    def chat(self):
        if self.whisper_model is None or self.whisper_model == "":
            print("\n" + Fore.YELLOW + "PLEASE SELECT WHISPER MODEL")
            self.select_whisper_model()

        self.whisper_thread = threading.Thread(target=self.listen_user, daemon=True)
        if self.whisper_thread.is_alive() == False:
            self.whisper_thread.start()

        self._print_prompt(empty=False)

        self.input_thread = threading.Thread(target=self.wait_input, daemon=True)
        if self.input_thread.is_alive() == False:
            self.input_thread.start()

        while not self.should_exit:
            time.sleep(0.001)

            # Обработка записи с веб-сервера через listen_user(from_file=True)
            if self.pending_voice:
                self.pending_voice = False
                self.listen_user(from_file=True)

            # Обработка текстовых команд и ввода с клавиатуры
            if self.user_prompt is not None and self.user_prompt != "":
                self.second_system_prompt = ""
                self.second_context = ""
                if self.user_prompt.strip()[0] == "/":
                    result = self.process_command(self.user_prompt)
                    if result == "UNKNOWN COMMAND":
                        print(Fore.YELLOW + "UNKNOWN COMMAND" + Style.RESET_ALL)
                    elif result not in ("OK", "EXITING"):
                        print(Fore.RED + str(result) + Style.RESET_ALL)
                    self.user_prompt = None
                    if not self.should_exit:
                        self._print_prompt(empty=False)
                else:
                    if not self.ai_active_model():
                        print("\n" + Fore.YELLOW + "МОДЕЛЬ НЕ ВЫБРАНА — открываю /ai")
                        self.ai_hub()
                    elif self.provider == "zen" and not self.zen_api_key:
                        print("\n" + Fore.YELLOW + "НЕТ API-КЛЮЧА ZEN — открываю /ai")
                        self.ai_hub()
                    elif self.ai_thread.is_alive() == False and self.type_work == "chat":
                        self.type_work = "chat"
                        self.full_relese_response()

        # Аккуратное завершение: гасим звук, останавливаем веб-сервер
        try:
            pygame.mixer.stop()
        except Exception:
            pass
        try:
            web_server.stop_server()
        except Exception:
            pass
        print(Fore.GREEN + "Завершение работы." + Style.RESET_ALL)


if __name__ == "__main__":
    AI = AIconsole()
    AI.server_thread = threading.Thread(
        target=web_server.start_server_in_thread,
        args=(AI,),
        daemon=True
    )
    AI.server_thread.start()
    try:
        AI.chat()
    except KeyboardInterrupt:
        pass