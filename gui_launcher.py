import os
import sys
import json
import time
import threading
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QProcess, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QPlainTextEdit, QComboBox,
    QLineEdit, QDoubleSpinBox, QSpinBox, QHeaderView, QFrame, QMessageBox, QFormLayout,
    QTextEdit, QListWidget, QListWidgetItem, QProgressBar,
)
from PySide6.QtGui import QFont, QColor, QPainter, QPen, QPalette
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

import requests

BASE_DIR = Path(__file__).resolve().parent
MAIN_SCRIPT = str(BASE_DIR / "main.py")
SETTINGS_PATH = BASE_DIR / "settings.json"
OLLAMA = "http://localhost:11434"
WEB = "http://localhost:1337"

try:
    import psutil
except Exception:
    psutil = None
try:
    import settings as settings_mod
    PATHS = settings_mod.PATHS
except Exception:
    PATHS = {}

DARK = """
QWidget { background-color: #1e1e2e; color: #cdd6f4; font-family: 'Segoe UI', Arial; font-size: 13px; }
QPushButton { background-color: #313244; color: #cdd6f4; border: none; border-radius: 8px; padding: 9px 12px; font-weight: 600; }
QPushButton:hover { background-color: #45475a; }
QPushButton:pressed { background-color: #585b70; }
QPushButton:disabled { background-color: #24243a; color: #585b70; }
QPushButton#accent { background-color: #89b4fa; color: #11111b; }
QPushButton#accent:hover { background-color: #74c7ec; }
QPushButton#danger { background-color: #f38ba8; color: #11111b; }
QPushButton#danger:hover { background-color: #eba0ac; }
QPushButton#nav { background-color: transparent; text-align: left; padding: 10px 14px; border-radius: 8px; font-weight: 600; }
QPushButton#nav:checked { background-color: #313244; color: #89b4fa; }
QStackedWidget { background: #1e1e2e; }
QTableWidget { gridline-color: #313244; background: #181825; border: none; border-radius: 8px; }
QHeaderView::section { background: #181825; color: #9399b2; padding: 8px; border: none; }
QTableWidget::item { padding: 6px; }
QPlainTextEdit, QTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #11111b; color: #cdd6f4; border: none; border-radius: 8px; padding: 8px; }
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView { background: #11111b; color: #cdd6f4; selection-background-color: #313244; border: none; }
QLabel { color: #cdd6f4; }
QListWidget { background: #11111b; border: none; border-radius: 10px; padding: 6px; }
QListWidget::item { padding: 6px; border-radius: 6px; }
QListWidget::item:hover { background: #313244; }
QProgressBar { background: #11111b; border: none; border-radius: 6px; text-align: center; color: #cdd6f4; }
QProgressBar::chunk { background: #a6e3a1; border-radius: 6px; }
QScrollBar:vertical { background: #181825; width: 10px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #45475a; border-radius: 5px; }
"""

STATUS_COLOR = {"ok": "#a6e3a1", "error": "#f38ba8", "stopped": "#fab387", "unknown": "#6c7086"}


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def save_settings(data):
    try:
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
        return True
    except Exception as e:
        print(f"Не удалось сохранить настройки: {e}")
        return False


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def find_python():
    """Интерпретатор для запуска main.py: сначала venv проекта, иначе текущий."""
    name = "python.exe" if os.name == "nt" else "python"
    for rel in (BASE_DIR / "venv" / "bin" / name,
                BASE_DIR.parent / "ai_env" / "bin" / name,
                BASE_DIR / ".venv" / "bin" / name):
        if rel.exists():
            return str(rel)
    return sys.executable


# --------------------------------------------------------------------------
# Sparkline (без рамок)
# --------------------------------------------------------------------------
class SparkLine(QWidget):
    def __init__(self, color, maxlen=80):
        super().__init__()
        self.color = QColor(color)
        self.maxlen = maxlen
        self.values = []
        self.setMinimumHeight(54)
        self.setMinimumWidth(200)

    def push(self, v):
        self.values.append(0.0 if v is None else float(v))
        if len(self.values) > self.maxlen:
            self.values.pop(0)
        self.update()

    def clear(self):
        self.values = []
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.setBrush(QColor("#11111b"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r, 8, 8)
        w, h = self.width(), self.height()
        # базовая линия
        p.setPen(QPen(QColor("#313244"), 1))
        p.drawLine(4, h - 4, w - 4, h - 4)
        if len(self.values) >= 2:
            mx = max(max(self.values), 1.0)
            step = w / (self.maxlen - 1)
            pad = 6
            p.setPen(QPen(self.color, 2))
            pts = [((i * step), h - (v / mx) * (h - 2 * pad) - pad) for i, v in enumerate(self.values)]
            for i in range(1, len(pts)):
                p.drawLine(*pts[i - 1], *pts[i])
        p.end()


# --------------------------------------------------------------------------
# Строка здоровья (точка + текст, без рамок)
# --------------------------------------------------------------------------
class HealthRow(QWidget):
    def __init__(self, name):
        super().__init__()
        self.setFixedHeight(40)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        self.dot = QLabel()
        self.dot.setFixedSize(12, 12)
        self.dot.setStyleSheet("background:#6c7086; border-radius:6px;")
        self.name = QLabel(name)
        self.name.setFont(QFont("Arial", 10, QFont.Bold))
        self.metric_lbl = QLabel("")
        self.metric_lbl.setStyleSheet("color:#9399b2;")
        self.metric_lbl.setAlignment(Qt.AlignRight)
        lay.addWidget(self.dot)
        lay.addWidget(self.name)
        lay.addStretch()
        lay.addWidget(self.metric_lbl)

    def set(self, status, metric=""):
        self.dot.setStyleSheet(f"background:{STATUS_COLOR.get(status,'#6c7086')}; border-radius:6px;")
        self.metric_lbl.setText(metric)


# --------------------------------------------------------------------------
# Worker статуса
# --------------------------------------------------------------------------
class StatusWorker(QThread):
    updated = Signal(dict)

    def __init__(self):
        super().__init__()
        self._stop = False
        self.proc = None

    def set_process(self, proc):
        self.proc = proc

    def run(self):
        while not self._stop:
            try:
                self.updated.emit(self.collect())
            except Exception as e:            # QThread не должен падать из-за одного сбоя опроса
                self.updated.emit({"error": str(e)})
            for _ in range(20):              # сон прерываемый: отключение не ждёт 2 секунды
                if self._stop:
                    break
                self.msleep(100)

    def collect(self):
        out = {}
        try:
            r = requests.get(OLLAMA + "/api/version", timeout=2)
            ver = r.json().get("version", "?") if r.status_code == 200 else "?"
            rm = requests.get(OLLAMA + "/api/tags", timeout=2)
            models = rm.json().get("models", []) if rm.status_code == 200 else []
            names = [m.get("name", "?") for m in models if isinstance(m, dict)]
            out["ollama"] = {"status": "ok", "version": ver, "models": names, "size": len(names)}
        except Exception as e:
            out["ollama"] = {"status": "error", "detail": str(e), "version": "?", "size": 0}

        if self.proc is not None and self.proc.state() == QProcess.Running:
            pid = self.proc.processId()
            cpu = mem = uptime = None
            if psutil:
                try:
                    pr = psutil.Process(pid)
                    cpu = pr.cpu_percent(interval=None)
                    mem = pr.memory_info().rss / (1024 * 1024)
                    started = pr.create_time()
                    uptime = max(0, time.time() - started) if started else None
                except Exception:
                    pass
            out["core"] = {"status": "ok", "pid": pid, "cpu": cpu, "mem": mem, "uptime": uptime}
        else:
            out["core"] = {"status": "stopped"}

        try:
            r = requests.get(WEB + "/", timeout=2)
            out["web"] = {"status": "ok" if r.status_code == 200 else "error"}
        except Exception:
            out["web"] = {"status": "error", "detail": ""}

        try:
            s = load_settings()
        except Exception:
            s = {}
        out["config"] = {
            "model": s.get("model", ""), "tts_voice": s.get("tts_voice", ""),
            "speed": s.get("speed_ai_speak", 1.0), "trigger": s.get("trigger_word", ""),
            "stt_mode": s.get("stt_mode", ""), "whisper": s.get("whispermodel", ""),
        }

        # один try на файл: между exists() и stat() файл мог исчезнуть
        try:
            fp = BASE_DIR / PATHS.get("voice_profile_path", "my_voice_profile.wav")
            size = fp.stat().st_size
            out["voice_profile"] = {"exists": True, "size_kb": round(size / 1024, 1)}
        except OSError:
            out["voice_profile"] = {"exists": False, "size_kb": 0}

        try:
            mp = BASE_DIR / PATHS.get("memory_path", "memory.txt")
            with open(mp, "r", encoding="utf-8", errors="ignore") as fh:
                lines = sum(1 for _ in fh)
            out["memory"] = {"exists": True, "lines": lines}
        except OSError:
            out["memory"] = {"exists": False, "lines": 0}
        return out

    def stop(self):
        self._stop = True


# --------------------------------------------------------------------------
# Главное окно — пульт управления
# --------------------------------------------------------------------------
class LuchGUI(QMainWindow):
    log_msg = Signal(str)          # вызовы из рабочих потоков — только через сигналы
    chat_html = Signal(str)
    refresh_models = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LUCH AI — Пульт управления")
        self.resize(1100, 720)

        self.player = QMediaPlayer()
        self.audio_out = QAudioOutput()
        self.player.setAudioOutput(self.audio_out)

        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(lambda: self._log(self.process.readAllStandardOutput().data().decode(errors="ignore").rstrip("\n")))
        self.process.readyReadStandardError.connect(lambda: self._log(self.process.readAllStandardError().data().decode(errors="ignore").rstrip("\n")))
        self.process.finished.connect(self._on_finished)

        self.worker = StatusWorker()
        self.worker.set_process(self.process)
        self.worker.updated.connect(self._on_status)
        self.worker.start()

        self.alert_timer = QTimer(self)
        self.alert_timer.timeout.connect(self.poll_alerts)
        self.alert_timer.start(3000)

        self._build_ui()

        # Qt-безопасные мосты: потоки не трогают виджеты напрямую
        # (подключаем после _build_ui — виджеты должны уже существовать)
        self.log_msg.connect(self._log)
        self.chat_html.connect(self.chat_out.append)
        self.refresh_models.connect(self.refresh_ollama)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        hbox = QHBoxLayout(root)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.addWidget(self._sidebar())
        hbox.addWidget(self._center())
        hbox.addWidget(self._rail())
        hbox.setStretch(0, 0)
        hbox.setStretch(1, 1)
        hbox.setStretch(2, 0)

    def _section(self, title):
        """Контейнер-секция без жёстких рамок: скруглённая тёмная панель + заголовок."""
        frame = QFrame()
        frame.setStyleSheet("background-color:#181825; border-radius:10px;")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        if title:
            t = QLabel(title)
            t.setStyleSheet("color:#6c7086; font-size:11px; letter-spacing:1px; font-weight:700;")
            lay.addWidget(t)
        return frame, lay

    # ---- Левая панель: навигация + здоровье ----
    def _sidebar(self):
        w = QWidget()
        w.setFixedWidth(280)
        w.setStyleSheet("background-color:#181825;")
        l = QVBoxLayout(w)
        l.setContentsMargins(14, 16, 14, 16)

        title = QLabel("LUCH AI")
        title.setFont(QFont("Arial", 20, QFont.Bold))
        sub = QLabel("Пульт управления")
        sub.setStyleSheet("color:#9399b2;")
        l.addWidget(title)
        l.addWidget(sub)

        self.btn_start = QPushButton("▶ Запустить")
        self.btn_start.setObjectName("accent")
        self.btn_stop = QPushButton("■ Остановить")
        self.btn_stop.setObjectName("danger")
        self.btn_start.clicked.connect(self.start_luch)
        self.btn_stop.clicked.connect(self.stop_luch)
        self.btn_stop.setEnabled(False)
        l.addWidget(self.btn_start)
        l.addWidget(self.btn_stop)

        l.addSpacing(10)
        hl = QLabel("СОСТОЯНИЕ СИСТЕМЫ")
        hl.setStyleSheet("color:#6c7086; font-size:11px; letter-spacing:1px;")
        l.addWidget(hl)
        self.hr = {}
        for key, name in [("ollama", "Ollama"), ("core", "Ядро LUCH"), ("web", "Web Server"),
                          ("voice", "Голос. профиль"), ("memory", "Память"), ("model", "Модель ИИ"),
                          ("tts", "TTS"), ("stt", "STT")]:
            row = HealthRow(name)
            self.hr[key] = row
            l.addWidget(row)
        l.addStretch()

        foot = QLabel("v1.7 • Control Center")
        foot.setStyleSheet("color:#6c7086; font-size:10px;")
        l.addWidget(foot)
        return w

    # ---- Центр: экраны ----
    def _center(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(14, 14, 14, 14)

        nav = QHBoxLayout()
        self.nav_buttons = {}
        for key, label in [("console", "Пульт"), ("log", "Лог"), ("settings", "Настройки"), ("ollama", "Ollama")]:
            b = QPushButton(label)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _, k=key: self._show_page(k))
            self.nav_buttons[key] = b
            nav.addWidget(b)
        nav.addStretch()
        self.btn_web = QPushButton("🌐 Web UI")
        self.btn_web.clicked.connect(lambda: webbrowser.open(WEB + "/"))
        self.btn_reload = QPushButton("⚙ Перезагрузить")
        self.btn_reload.clicked.connect(self.reload_in_luch)
        nav.addWidget(self.btn_web)
        nav.addWidget(self.btn_reload)
        l.addLayout(nav)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._console_page())
        self.stack.addWidget(self._log_page())
        self.stack.addWidget(self._settings_page())
        self.stack.addWidget(self._ollama_page())
        l.addWidget(self.stack, 1)

        self.status_bar = QLabel("Готов")
        self.status_bar.setStyleSheet("color:#9399b2; padding:6px 4px; font-size:12px;")
        l.addWidget(self.status_bar)

        self._show_page("console")
        return w

    def _show_page(self, key):
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        idx = {"console": 0, "log": 1, "settings": 2, "ollama": 3}[key]
        self.stack.setCurrentIndex(idx)

    def _console_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(12)
        head = QLabel("Диалог с ИИ")
        head.setStyleSheet("font-size:15px; font-weight:700; color:#cdd6f4;")
        l.addWidget(head)
        # быстрые действия
        q = QHBoxLayout()
        q.setSpacing(8)
        for label, cmd in [("🔒 Блокировка ПК", "command {\"command\": \"lock_pc\", \"args\": {}}"),
                           ("⏰ Таймер 5м", "поставь таймер на 5 минут напомнить проверить плиту"),
                           ("🔍 Веб-поиск", "найди в интернете последние новости ИИ")]:
            b = QPushButton(label)
            b.setMinimumHeight(36)
            b.clicked.connect(lambda _, c=cmd: self.send_chat(c))
            q.addWidget(b)
        l.addLayout(q)
        # чат в контейнере
        chat_frame = QFrame()
        chat_frame.setStyleSheet("background-color:#181825; border-radius:10px;")
        cl = QVBoxLayout(chat_frame)
        cl.setContentsMargins(10, 10, 10, 10)
        self.chat_out = QTextEdit()
        self.chat_out.setReadOnly(True)
        cl.addWidget(self.chat_out, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.chat_in = QLineEdit()
        self.chat_in.setPlaceholderText("Запрос к ИИ и Enter...")
        self.chat_in.returnPressed.connect(self.send_chat)
        b_send = QPushButton("➤ Отправить")
        b_send.setObjectName("accent")
        b_send.setMinimumHeight(36)
        b_send.clicked.connect(self.send_chat)
        b_audio = QPushButton("🔊 Озвучить")
        b_audio.setMinimumHeight(36)
        b_audio.clicked.connect(self.play_last_response)
        row.addWidget(self.chat_in)
        row.addWidget(b_send)
        row.addWidget(b_audio)
        cl.addLayout(row)
        l.addWidget(chat_frame, 1)
        return w

    def _log_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        l.addWidget(self.log)
        return w

    def _settings_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        s = load_settings()
        form = QFormLayout()
        self.cb_model = QComboBox()
        self.cb_model.addItems([s.get("model", "")])
        self.cb_model.setCurrentText(s.get("model", ""))
        self.cb_stt = QComboBox()
        self.cb_stt.addItems(["google", "whisper"])
        self.cb_stt.setCurrentText(s.get("stt_mode", "google"))
        self.le_whisper = QLineEdit(s.get("whispermodel", ""))
        self.le_trigger = QLineEdit(s.get("trigger_word", ""))
        self.le_tts = QLineEdit(s.get("tts_voice", ""))
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.5, 3.0)
        self.sp_speed.setSingleStep(0.1)
        self.sp_speed.setValue(_as_float(s.get("speed_ai_speak", 1.0), 1.0))
        self.sb_micro = QSpinBox()
        self.sb_micro.setRange(0, 20)
        self.sb_micro.setValue(_as_int(s.get("micro_index", 1), 1))
        form.addRow("Модель ИИ:", self.cb_model)
        form.addRow("STT режим:", self.cb_stt)
        form.addRow("Whisper модель:", self.le_whisper)
        form.addRow("Триггер-слово:", self.le_trigger)
        form.addRow("Голос TTS:", self.le_tts)
        form.addRow("Скорость речи:", self.sp_speed)
        form.addRow("Индекс микрофона:", self.sb_micro)
        l.addLayout(form)
        self.mic_info = QLabel("")
        self.mic_info.setStyleSheet("color:#9399b2;")
        l.addWidget(self.mic_info)
        b_mic = QPushButton("Считать микрофоны")
        b_mic.clicked.connect(self.list_mics)
        l.addWidget(b_mic)
        b_apply = QPushButton("💾 Сохранить")
        b_apply.setObjectName("accent")
        b_apply.clicked.connect(self.apply_settings)
        l.addWidget(b_apply)
        l.addStretch()
        return w

    def _ollama_page(self):
        w = QWidget()
        l = QVBoxLayout(w)
        self.ollama_table = QTableWidget(0, 3)
        self.ollama_table.setHorizontalHeaderLabels(["Модель", "ГБ", "Изменена"])
        self.ollama_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        l.addWidget(self.ollama_table)
        row = QHBoxLayout()
        self.le_pull = QLineEdit()
        self.le_pull.setPlaceholderText("имя модели, напр. llama3.2")
        b_pull = QPushButton("⬇ Загрузить")
        b_pull.setObjectName("accent")
        b_pull.clicked.connect(self.pull_model)
        b_ref = QPushButton("⟳")
        b_ref.clicked.connect(self.refresh_ollama)
        row.addWidget(self.le_pull)
        row.addWidget(b_pull)
        row.addWidget(b_ref)
        l.addLayout(row)
        self.refresh_ollama()
        return w

    # ---- Правая панель: телеметрия + алерты ----
    def _rail(self):
        w = QWidget()
        w.setFixedWidth(300)
        w.setStyleSheet("background-color:#181825;")
        l = QVBoxLayout(w)
        l.setContentsMargins(14, 16, 14, 16)
        l.setSpacing(14)

        tel, tell = self._section("ТЕЛЕМЕТРИЯ")
        tell.addWidget(QLabel("CPU %"))
        self.cpu_chart = SparkLine("#f38ba8")
        tell.addWidget(self.cpu_chart)
        self.cpu_bar = QProgressBar()
        self.cpu_bar.setFormat("%v%")
        tell.addWidget(self.cpu_bar)
        tell.addWidget(QLabel("RAM (МБ)"))
        self.mem_chart = SparkLine("#89b4fa")
        tell.addWidget(self.mem_chart)
        self.mem_bar = QProgressBar()
        self.mem_bar.setMaximum(4096)
        self.mem_bar.setFormat("%v МБ")
        tell.addWidget(self.mem_bar)
        l.addWidget(tel)

        alr, alrl = self._section("ТАЙМЕРЫ / АЛЕРТЫ")
        self.alert_list = QListWidget()
        alrl.addWidget(self.alert_list, 1)
        b = QPushButton("🔊 Проиграть алерт")
        b.clicked.connect(self.play_alert_audio)
        alrl.addWidget(b)
        l.addWidget(alr, 1)
        return w

    # ---------------- Actions ----------------
    def start_luch(self):
        if self.process.state() == QProcess.Running:
            return
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        python = find_python()
        self.process.start(python, [MAIN_SCRIPT])
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._log(f">>> Запуск main.py ({python})")

    def stop_luch(self):
        if self.process.state() == QProcess.Running:
            self.process.terminate()
            QTimer.singleShot(5000, lambda: self.process.kill() if self.process.state() == QProcess.Running else None)
            self._log(">>> Остановка LUCH...")

    def reload_in_luch(self):
        try:
            r = requests.post(WEB + "/api/command", json={"cmd": "/reload_libs"}, timeout=5)
            self._log(f">>> reload_libs: {r.json()}")
        except Exception as e:
            self._log(f">>> Ошибка reload_libs: {e}")

    def list_mics(self):
        try:
            import speech_recognition as sr
            from pathlib import Path as _Path
            import os as _os
            import sys as _sys
            # ALSA/JACK пишут в stderr мимо Python — сворачиваем в logs/native.log
            log_path = _Path(__file__).resolve().parent / "logs" / "native.log"
            log = open(log_path, "a", encoding="utf-8", errors="replace")
            saved = _os.dup(2)
            try:
                _os.dup2(log.fileno(), 2)
                names = sr.Microphone.list_microphone_names()
            finally:
                _os.dup2(saved, 2)
                _os.close(saved)
                log.close()
            cur = load_settings().get("micro_index", 1)
            self.mic_info.setText("Микрофоны:\n" + "\n".join(f"[{i}] {n}" + ("  <-- выбран" if i == cur else "") for i, n in enumerate(names)))
        except Exception as e:
            self.mic_info.setText(f"Ошибка: {e}")

    def apply_settings(self):
        s = load_settings()
        s.update({
            "model": self.cb_model.currentText().strip(),
            "stt_mode": self.cb_stt.currentText(),
            "whispermodel": self.le_whisper.text().strip(),
            "trigger_word": self.le_trigger.text().strip(),
            "tts_voice": self.le_tts.text().strip(),
            "speed_ai_speak": self.sp_speed.value(),
            "micro_index": self.sb_micro.value(),
        })
        save_settings(s)
        self._log(">>> Настройки сохранены")

    def refresh_ollama(self):
        try:
            r = requests.get(OLLAMA + "/api/tags", timeout=3)
            models = r.json().get("models", [])
            self.ollama_table.setRowCount(len(models))
            for i, m in enumerate(models):
                self.ollama_table.setItem(i, 0, QTableWidgetItem(m.get("name", "?")))
                self.ollama_table.setItem(i, 1, QTableWidgetItem(f"{m.get('size', 0) / (1024 ** 3):.2f}"))
                self.ollama_table.setItem(i, 2, QTableWidgetItem(m.get("modified_at", "")[:19].replace("T", " ")))
            names = [m.get("name", "") for m in models]
            cur = self.cb_model.currentText()
            self.cb_model.clear()
            self.cb_model.addItems(names)
            if cur:
                self.cb_model.setCurrentText(cur)
        except Exception as e:
            self._log(f">>> Ollama недоступен: {e}")

    def pull_model(self):
        name = self.le_pull.text().strip()
        if not name:
            return
        self._log(f">>> Загрузка {name} ...")
        threading.Thread(target=self._pull_worker, args=(name,), daemon=True).start()

    def _pull_worker(self, name):
        try:
            r = requests.post(OLLAMA + "/api/pull", json={"name": name, "stream": False}, timeout=600)
            self.log_msg.emit(f">>> {name}: {r.status_code}")
        except Exception as e:
            self.log_msg.emit(f">>> Ошибка загрузки {name}: {e}")
        self.refresh_models.emit()

    def send_chat(self, text=None):
        text = (text or self.chat_in.text()).strip()
        if not text:
            return
        self.chat_in.clear()
        self.chat_html.emit(f"<b style='color:#89b4fa'>ВЫ:</b> {text}")
        threading.Thread(target=self._chat_worker, args=(text,), daemon=True).start()

    def _chat_worker(self, text):
        try:
            r = requests.post(WEB + "/api/ask", json={"text": text}, timeout=60)
            data = r.json()
            self.chat_html.emit(f"<b style='color:#a6e3a1'>LUCH:</b> {data.get('response','')}")
            if data.get("play_audio"):
                self.chat_html.emit("<i style='color:#6c7086'>(озвучка на ПК)</i>")
        except Exception as e:
            self.chat_html.emit(f"<span style='color:#f38ba8'>Ошибка: {e}</span>")

    def play_last_response(self):
        try:
            r = requests.get(WEB + "/api/audio", timeout=5)
            if r.status_code == 200:
                fp = "/tmp/luch_gui_resp.wav"
                open(fp, "wb").write(r.content)
                self.player.setSource(QUrl.fromLocalFile(fp))
                self.player.play()
        except Exception as e:
            self._log(f">>> Ошибка воспроизведения: {e}")

    def poll_alerts(self):
        try:
            r = requests.get(WEB + "/api/alerts", timeout=3)
            for a in r.json().get("alerts", []):
                self.alert_list.addItem(QListWidgetItem(f"⏰ {time.strftime('%H:%M:%S')} — {a.get('description','')}"))
        except Exception:
            pass

    def play_alert_audio(self):
        try:
            r = requests.get(WEB + "/api/alert_audio", timeout=5)
            if r.status_code == 200:
                fp = "/tmp/luch_gui_alert.wav"
                open(fp, "wb").write(r.content)
                self.player.setSource(QUrl.fromLocalFile(fp))
                self.player.play()
        except Exception as e:
            self._log(f">>> Ошибка алерта: {e}")

    # ---------------- Статус ----------------
    def _on_status(self, d):
        o = d.get("ollama", {}) or {}
        cfg = d.get("config", {}) or {}
        if o.get("status") == "ok":
            self.hr["ollama"].set("ok", f"v{o.get('version')} • {o.get('size')} мод.")
            self.hr["model"].set("ok" if cfg.get("model") else "unknown", cfg.get("model") or "не выбрана")
        else:
            self.hr["ollama"].set("error", "нет связи")
            self.hr["model"].set("unknown", "—")
        core = d.get("core", {}) or {}
        if core.get("status") == "ok":
            sub = f"PID {core.get('pid', '?')}"
            cpu, mem, uptime = core.get("cpu"), core.get("mem"), core.get("uptime")
            if cpu is not None and mem is not None:
                sub += f" • CPU {cpu:.0f}%"
                if uptime is not None:
                    sub += f" • {int(uptime) // 60}м"
                self.cpu_chart.push(cpu)
                self.mem_chart.push(mem)
                self.cpu_bar.setValue(max(0, min(int(cpu), 100)))
                self.mem_bar.setValue(max(0, min(int(mem), 4096)))
            self.hr["core"].set("ok", sub)
        else:
            self.hr["core"].set("stopped", "остановлен")
            self.cpu_chart.clear()
            self.mem_chart.clear()
            self.cpu_bar.setValue(0)
            self.mem_bar.setValue(0)
        web = d.get("web", {}) or {}
        self.hr["web"].set("ok" if web.get("status") == "ok" else "error", "" if web.get("status") == "ok" else "нет связи")
        vp = d.get("voice_profile", {}) or {}
        self.hr["voice"].set("ok" if vp.get("exists") else "error", f"{vp.get('size_kb', 0)} КБ")
        mem = d.get("memory", {}) or {}
        self.hr["memory"].set("ok" if mem.get("exists") else "error", f"{mem.get('lines', 0)} строк")
        self.hr["tts"].set("ok", f"{cfg.get('tts_voice')} x{cfg.get('speed')}")
        self.hr["stt"].set("ok", f"{cfg.get('stt_mode')} • whisper: {cfg.get('whisper') or '—'}")

        overall = "OK" if (core.get("status") == "ok" and o.get("status") == "ok" and web.get("status") == "ok") else "ВНИМАНИЕ"
        self.status_bar.setText(f"Обновлено {time.strftime('%H:%M:%S')}  •  Состояние: {overall}  •  Ollama: {o.get('size', 0)} мод.  •  Ядро: {core.get('status', 'нет данных')}")

    def _on_finished(self, code, status):
        self._log(f">>> LUCH завершён (код {code})")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _log(self, msg):
        if hasattr(self, "log"):
            self.log.appendPlainText(msg)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def closeEvent(self, event):
        self.worker.stop()
        self.alert_timer.stop()
        if self.process.state() == QProcess.Running:
            self.process.terminate()
            if not self.process.waitForFinished(3000):
                self.process.kill()
        self.worker.wait(3000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK)
    win = LuchGUI()
    win.show()
    sys.exit(app.exec())
