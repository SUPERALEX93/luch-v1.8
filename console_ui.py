"""Интерактивное консольное меню LUCH: навигация стрелками, поиск, скроллинг.

Ключевые моменты реализации:
* клавиши читаются в raw-режиме терминала (termios), без Enter;
* каждый кадр рисуется строго одного размера (фиксированные ширина/высота
  панели и точная длина строк) — иначе rich Live двигает курсор не на ту
  высоту, рамка «уезжает», а текст рвётся;
* ширина текста считается по cell_len (а не len), иначе эмодзи типа 🔎
  ломают выравнивание колонок;
* если терминала нет (пайп, не-tty, Windows) — откат на ввод номера.
"""

import io
import os
import select as _select
import shutil
import sys
import time
from contextlib import contextmanager

from rich.box import ROUNDED
from rich.cells import cell_len
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

try:
    import termios
    import tty
    _RAW_OK = True
except Exception:
    _RAW_OK = False

C_ACCENT = "#f5c2e7"
C_TITLE = "#89b4fa"
C_TEXT = "#cdd6f4"
C_DIM = "#6c7086"
C_OK = "#a6e3a1"
C_WARN = "#f9e2af"
C_GROUP = "#cba6f7"
C_MARK = "❯"
C_CURRENT = "●"

HINT = "↑↓ выбор · Enter — ок · Esc — отмена · буквы — поиск · цифра — быстрый переход"

MIN_PANEL_W = 46
MAX_PANEL_W = 104
ROW_MARK = 4      # "  ❯ " — отступ + маркер + пробел
COL_GAP = 2       # отступ между колонками

MAX_BUFFERED_LINES = 200   # сколько строк фонового вывода держим до показа


class _Buffer:
    """Потокобезопасная замена sys.stdout/sys.stderr на время работы меню.

    Пока открыто меню, любой print() из фонового потока (веб-сервер,
    распознавание речи, таймеры) уходит сюда, а не поверх рамки. Иначе
    rich Live двигает курсор не туда и список «рвётся».
    """

    def __init__(self, limit=MAX_BUFFERED_LINES):
        import threading
        self._lock = threading.Lock()
        self._lines = []
        self._pending = ""
        self._dropped = 0
        self.limit = limit
        self.encoding = "utf-8"
        self.errors = "strict"
        # совместимость с интерфейсом текстового потока: код может читать
        # name/buffer/closed, даже если сейчас никто в репозитории так не делает
        self.name = "<menu>"
        self.buffer = io.BytesIO()
        self._closed = False

    @property
    def closed(self):
        return self._closed

    def close(self):
        self._closed = True

    def _push(self, line):
        if len(self._lines) >= self.limit:
            self._dropped += 1
            return
        self._lines.append(line)

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            # print() пишет текст и перевод строки отдельными вызовами,
            # поэтому недописанный хвост копим, а не рвём строку пополам
            self._pending += text
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                self._push(line)
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("буфер не поддерживает fileno")

    def take(self):
        """Забрать накопленные строки и сбросить буфер."""
        with self._lock:
            if self._pending:
                self._push(self._pending)
                self._pending = ""
            # считаем dropped ДО сброса, иначе уведомление «скрыто ещё N строк»
            # никогда не показывается (прежний баг)
            dropped = self._dropped
            lines, self._lines, self._dropped = self._lines, [], 0
        if dropped:
            lines.append(f"… скрыто ещё {dropped} строк фонового вывода")
        return lines


@contextmanager
def capture_background_output():
    """Глушит посторонний вывод, пока рисуется меню."""
    buf = _Buffer()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = buf
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stdout, sys.stderr = old_out, old_err


class _Frame:
    """Перерисовка кадра вручную: поднимаем курсор на точное число строк.

    Свой renderer вместо rich Live — потому что Live двигает курсор по высоте
    предыдущего кадра и «уезжает», если рядом печатает фоновый поток.
    """

    def __init__(self, console, width, height):
        self.file = sys.__stdout__
        self.width = width
        self.height = height
        self.color_system = console.color_system
        self.no_color = console.no_color
        self.is_terminal = console.is_terminal
        self.drawn = False
        self.lines = height
        # переиспользуемые буфер и Console: новый объект на каждый кадр не нужен
        self._buf = io.StringIO()
        self._tmp = None
        self._tmp_size = None

    def _render(self, panel):
        if self._tmp is None or self._tmp_size != (self.width, self.height):
            self._buf = io.StringIO()
            self._tmp = Console(file=self._buf, width=self.width, height=self.height,
                                force_terminal=self.is_terminal,
                                color_system=self.color_system, no_color=self.no_color,
                                highlight=False, soft_wrap=False, markup=False, emoji=True)
            self._tmp_size = (self.width, self.height)
        self._buf.seek(0)
        self._buf.truncate(0)
        self._tmp.print(panel)
        lines = self._buf.getvalue().split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    def draw(self, panel):
        lines = self._render(panel)
        if self.drawn and len(lines) != self.lines:
            # размер кадра менять нельзя — иначе рамка поедет
            lines = lines[:self.lines] + [" " * self.width] * max(0, self.lines - len(lines))
        self.lines = len(lines)
        out = []
        if self.drawn:
            out.append(f"\x1b[{self.lines}A")
        for line in lines:
            out.append("\x1b[2K" + line + "\r\n")
        out.append("\x1b[0J")
        self.file.write("".join(out))
        self.file.flush()
        self.drawn = True

    def clear(self):
        if not self.drawn:
            return
        out = [f"\x1b[{self.lines}A"]
        for _ in range(self.lines):
            out.append("\x1b[2K\r\n")
        self.file.write("".join(out))
        self.file.flush()
        self.drawn = False


class Item:
    """Пункт меню."""

    def __init__(self, value, title, desc="", current=False, custom=False, enabled=True, badge=""):
        self.value = value
        self.title = str(title)
        self.desc = str(desc) if desc else ""
        self.current = current
        self.custom = custom
        self.enabled = enabled
        self.badge = str(badge) if badge else ""


class Section:
    """Заголовок группы пунктов."""

    is_section = True

    def __init__(self, title):
        self.title = str(title)


def items_from(options, current=None, allow_custom=False, custom_label="✎  Ввести вручную"):
    """Собирает список Item из обычных строк (или готовых Item/Section)."""
    out = []
    for opt in options:
        if isinstance(opt, (Item, Section)):
            out.append(opt)
        else:
            out.append(Item(opt, opt, current=(current is not None and str(opt) == str(current))))
    if allow_custom:
        out.append(Item("__custom__", custom_label, "ввести значение с клавиатуры", custom=True))
    return out


def _trunc(text, width):
    """Единственная реализация обрезки по ширине (в ячейках терминала).

    Обрезает строку ровно до width ячеек (по cell_len), при нехватке места
    добавляет «…». _fit/_pad_text ниже переиспользуют эту логику.
    """
    text = str(text)
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        w = cell_len(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _pad_str(text, width):
    """Дополняет уже обрезанную строку пробелами ровно до width ячеек."""
    return text + " " * max(0, width - cell_len(text))


def _fit(text, width):
    """Обрезает до width ячеек и дополняет пробелами ровно до width."""
    return _pad_str(_trunc(text, width), width)


def _pad_text(text, width):
    """Дополняет готовый rich-текст пробелами до ровно width ячеек."""
    text.append(" " * max(0, width - cell_len(text.plain)))
    return text


def _section_rows(entries):
    """Строки, которые занимают заголовки групп вместе с пустыми разделителями."""
    seen = False
    n = 0
    for e in entries:
        if isinstance(e, Section):
            if seen:
                n += 1        # пустая строка-разделитель перед заголовком
            n += 1            # сам заголовок
            seen = True
        else:
            seen = True
    return n


def _geometry(console, entries, height, term_size=None):
    """Фиксирует размеры рамки на всё время работы меню.

    Возвращает (panel_w, inner, rows_target, item_rows). panel_w/inner/rows_target
    не должны меняться между кадрами, иначе rich Live начнёт «уезжать».
    item_rows здесь — стартовая оценка; в цикле она пересчитывается под
    фактическое число динамических строк (поиск, ввод номера, заголовки групп,
    счётчик скролла, подсказка).
    """
    if term_size:
        term_w, term_h = term_size
    else:
        term_w, term_h = console.width or 80, console.height or 24
    n_sections = _section_rows(entries)
    panel_w = max(MIN_PANEL_W, min(term_w, MAX_PANEL_W))
    inner = panel_w - 4
    screen_rows = max(8, term_h - 6)
    # резерв: строка счётчика скролла (1) + две строки подсказки
    extra = n_sections + 3
    n_items = sum(1 for e in entries if not isinstance(e, Section))
    item_rows = max(3, min(height, n_items, screen_rows - extra))
    rows_target = min(item_rows + extra, screen_rows)
    item_rows = max(3, rows_target - extra)
    return panel_w, inner, rows_target, item_rows


def _overhead_rows(view_items, query, digits, hint, reserve_scroll=True):
    """Сколько строк кадра уйдёт не на пункты (для расчёта item_rows)."""
    n = _section_rows(view_items)
    if query:
        n += 1
    if digits:
        n += 1
    if reserve_scroll:
        n += 1            # строка счётчика «пункт N из M» резервируется всегда
    if hint:
        n += 2            # пустая строка + текст подсказки
    return n


def _utf8_len(b0):
    """Ожидаемая длина UTF-8-последовательности по первому байту."""
    if b0 < 0x80:
        return 1
    if 0xC0 <= b0 <= 0xDF:
        return 2
    if 0xE0 <= b0 <= 0xEF:
        return 3
    if 0xF0 <= b0 <= 0xF7:
        return 4
    return 1              # одиночный/битый байт — декодируем как есть


def _decode_one(buf):
    """Извлекает один полный UTF-8 символ из bytearray buf.

    Возвращает (символ, сколько байт съедено) или (None, 0), если данных
    ещё мало — тогда неполные байты остаются в buf до следующего чтения.
    """
    if not buf:
        return None, 0
    need = _utf8_len(buf[0])
    if len(buf) < need:
        return None, 0
    raw = bytes(buf[:need])
    del buf[:need]
    return raw.decode("utf-8", "replace"), need


def _fill_buf(fd, buf, timeout):
    """Добирает байты из fd в buf. Возвращает 'data' / 'timeout' / 'eof'."""
    try:
        ready, _, _ = _select.select([fd], [], [], timeout)
    except OSError:
        return "eof"
    if not ready:
        return "timeout"
    try:
        data = os.read(fd, 256)
    except OSError:
        return "eof"
    if not data:
        return "eof"
    buf.extend(data)
    return "data"


def _read_char(fd, buf, timeout=0.0):
    """Читает один полноценный символ UTF-8, не блокируясь на неполной последовательности.

    Возвращает символ, "" (данных пока нет) или None (stdin закрыт).
    """
    while True:
        ch, _ = _decode_one(buf)
        if ch is not None:
            return ch
        wait = timeout if not buf else 0.1   # хвост последовательности уже в пути
        state = _fill_buf(fd, buf, wait)
        if state == "eof":
            if buf:
                raw = bytes(buf)
                del buf[:]
                return raw.decode("utf-8", "replace")
            return None
        if state == "timeout":
            if buf and len(buf) >= 4:        # заведомо битая последовательность
                raw = bytes(buf)
                del buf[:]
                return raw.decode("utf-8", "replace")
            return ""


def _read_key(fd, buf):
    """Читает одну клавишу, возвращает (имя, символ)."""
    ch = _read_char(fd, buf)
    if ch is None:
        return "exit", ""
    if ch == "":
        return "none", ""

    if ch == "\x1b":
        seq = ""
        while True:
            nxt = _read_char(fd, buf, timeout=0.12)
            if nxt is None or nxt == "":
                break
            seq += nxt
            if nxt.isalpha() or nxt == "~":
                break
        if seq in ("", "[", "O"):
            return "esc", ""
        if seq in ("[A", "OA"):
            return "up", seq
        if seq in ("[B", "OB"):
            return "down", seq
        if seq in ("[C", "OC"):
            return "right", seq
        if seq in ("[D", "OD"):
            return "left", seq
        if seq in ("[H", "OH", "[1~", "[7~"):
            return "home", seq
        if seq in ("[F", "OF", "[4~", "[8~"):
            return "end", seq
        if seq == "[5~":
            return "pageup", seq
        if seq == "[6~":
            return "pagedown", seq
        if seq == "[3~":
            return "delete", seq
        return "unknown", seq

    if ch in ("\r", "\n"):
        return "enter", ch
    if ch == "\x03":
        return "cancel", ch
    if ch == "\x04":
        return "exit", ch
    if ch in ("\x7f", "\b"):
        return "backspace", ch
    if ch == "\t":
        return "tab", ch
    if ord(ch) < 32:
        return "unknown", ch
    return "char", ch


def _visible(total, cursor, height):
    """Диапазон видимых пунктов так, чтобы курсор был в поле зрения."""
    if total <= height:
        return list(range(total)), 0
    half = height // 2
    start = max(0, min(cursor - half, total - height))
    return list(range(start, start + height)), start


def _build_rows(view_items, cursor, first_shown, query, digits, total, shown_count,
                inner, hint, badge_cap=26, title_cap=28):
    """Собирает строки тела панели. Каждая строка — ровно inner ячеек."""
    titles = [e.title for e in view_items if not isinstance(e, Section)]
    title_w = max([10] + [min(cell_len(t), title_cap) for t in titles]) if titles else 10
    badges = []
    for e in view_items:
        if isinstance(e, Section):
            continue
        b = e.badge or (C_CURRENT if e.current else "")
        if e.custom and not b:
            b = "вручную"
        badges.append(cell_len(_trunc(b, badge_cap)))
    badge_w = max(badges) if badges else 0
    desc_w = inner - ROW_MARK - title_w - COL_GAP - badge_w - COL_GAP
    if desc_w < 6:
        desc_w = 0

    rows = []

    if query:
        t = Text()
        t.append("  ", style=C_DIM)
        t.append("🔎 поиск: ", style=C_WARN)
        t.append(query, style=f"bold {C_WARN}")
        t.append("▏", style=C_WARN)
        rows.append(_pad_text(t, inner))
    if digits:
        t = Text()
        t.append("  ", style=C_DIM)
        t.append("⌨ номер: ", style=C_TITLE)
        t.append(digits, style=f"bold {C_TITLE}")
        rows.append(_pad_text(t, inner))

    pos = first_shown - 1
    first_item = True
    for entry in view_items:
        if isinstance(entry, Section):
            if not first_item:
                rows.append(Text(""))
            t = Text()
            t.append("  " + _trunc(entry.title.upper(), inner - 2), style=f"bold {C_GROUP}")
            rows.append(t)
            first_item = False
            continue
        first_item = False
        pos += 1
        selected = pos == cursor
        right = entry.badge or (C_CURRENT if entry.current else "")
        if entry.custom and not right:
            right = "вручную"
        right = _fit(right, badge_cap)
        if selected:
            marker_style, title_style, desc_style = f"bold {C_ACCENT}", f"bold {C_TITLE}", C_TEXT
        else:
            marker_style = C_DIM
            title_style = C_TEXT if entry.enabled else C_DIM
            desc_style = C_DIM
        t = Text()
        t.append("  ", style="")
        t.append(C_MARK if selected else " ", style=marker_style)
        t.append(" ", style="")
        t.append(_fit(entry.title, title_w), style=title_style)
        if desc_w > 6:
            t.append(" " * COL_GAP, style="")
            t.append(_fit(entry.desc, desc_w), style=desc_style)
        if badge_w:
            t.append(" " * COL_GAP, style="")
            t.append(right, style=C_OK if right.strip() else "")
        rows.append(_pad_text(t, inner))

    if total > shown_count and total > 0:
        t = Text()
        t.append("   " + f"пункт {cursor + 1} из {total}", style=C_DIM)
        t.append(f"   ▲ {cursor} выше   ▼ {max(0, total - cursor - 1)} ниже", style=C_DIM)
        rows.append(_pad_text(t, inner))

    if hint:
        rows.append(Text(""))
        t = Text()
        t.append("  " + _trunc(hint, inner - 2), style=C_DIM)
        rows.append(t)

    return rows


def _frame(title, subtitle, rows, panel_w, rows_target, inner, keep_tail=0):
    """Собирает панель строго фиксированного размера: rows_target строк × panel_w.

    keep_tail — сколько последних строк нельзя терять при переполнении
    (счётчик скролла + подсказка), чтобы они не исчезали в тесном терминале.
    """
    if len(rows) > rows_target:
        keep = rows[-keep_tail:] if keep_tail else []
        rows = rows[:max(0, rows_target - len(keep))] + keep
    body = Text()
    for i in range(rows_target):
        if i:
            body.append("\n")
        if i < len(rows):
            body.append_text(rows[i])
        else:
            body.append(" " * inner)
    return Panel(
        body,
        title=_trunc(" " + str(title) + " ", panel_w - 6),
        title_align="left",
        subtitle=_trunc(" " + str(subtitle) + " ", panel_w - 6) if subtitle else None,
        subtitle_align="left",
        border_style=C_ACCENT,
        box=ROUNDED,
        padding=(0, 1),
        width=panel_w,
    )


def _fallback_select(console, title, entries, subtitle=""):
    """Режим без raw-терминала: обычный ввод номера."""
    lines = Text()
    n = 0
    for entry in entries:
        if isinstance(entry, Section):
            lines.append(f"\n  {entry.title.upper()}\n", style=f"bold {C_GROUP}")
            continue
        n += 1
        cur = f"  {C_CURRENT}" if entry.current else ""
        badge = f"  ({entry.badge})" if entry.badge else ""
        lines.append(f"  {n}. ", style=f"bold {C_TITLE}")
        lines.append(_trunc(entry.title + badge + cur, 62), style=C_TEXT)
        lines.append("\n")
    lines.append("\n  0 — отмена\n", style=C_DIM)
    console.print(Panel(lines, title=f" {title} ", title_align="left", border_style=C_ACCENT,
                        box=ROUNDED, padding=(0, 1)))
    try:
        raw = input("  Выбери номер (0 — отмена) › ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw.isdigit():
        return None
    picks = [e for e in entries if not isinstance(e, Section)]
    idx = int(raw)
    if 1 <= idx <= len(picks):
        return picks[idx - 1]
    return None


def select(title, entries, subtitle="", hint="", height=16, console=None):
    """Показывает меню и возвращает выбранный Item (или None при отмене).

    entries — список Item/Section в нужном порядке.
    """
    console = console or Console()
    entries = list(entries)

    try:
        interactive = _RAW_OK and sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        return _fallback_select(console, title, entries, subtitle)

    try:
        fd = sys.stdin.fileno()
    except Exception:
        return _fallback_select(console, title, entries, subtitle)

    # геометрию считаем от реального размера терминала, а не от размера Console
    _term0 = shutil.get_terminal_size()
    panel_w, inner, rows_target, item_rows = _geometry(console, entries, height,
                                                       (_term0.columns, _term0.lines))
    state = {"query": "", "digits": "", "digits_at": 0.0, "cursor": 0}

    def active_items():
        q = state["query"].lower().strip()
        res = []
        for e in entries:
            if isinstance(e, Section):
                continue
            if q and q not in e.title.lower() and q not in e.desc.lower() and q not in str(e.value).lower():
                continue
            res.append(e)
        return res

    def rebuild_view():
        """Возвращает (записи для отрисовки, позиции активных пунктов)."""
        actives = active_items()
        if not actives:
            return [Item(None, "ничего не найдено", enabled=False)], [0]
        keep = {id(a) for a in actives}
        view = []
        for e in entries:
            if isinstance(e, Section):
                if view and not isinstance(view[-1], Section):
                    view.append(e)
            elif id(e) in keep:
                view.append(e)
        return view, list(range(len(actives)))

    def move(delta):
        total = len(active_items())
        if total:
            state["cursor"] = max(0, min(state["cursor"] + delta, total - 1))

    def jump(number):
        total = len(active_items())
        if 1 <= number <= total:
            state["cursor"] = number - 1

    chosen = None

    old_attrs = None
    try:
        old_attrs = termios.tcgetattr(fd)
        tty.setraw(fd)
    except Exception:
        return _fallback_select(console, title, entries, subtitle)

    # Пока открыто меню, фоновые потоки не должны печатать поверх рамки
    with capture_background_output() as pending:
        screen = _Frame(console, panel_w, rows_target + 2)
        try:
            sys.__stdout__.write("\x1b[?25l")
            sys.__stdout__.flush()
            kbuf = bytearray()                 # недобранные байты UTF-8 между чтениями
            term_size = shutil.get_terminal_size()
            while True:
                # при изменении размера терминала пересчитываем геометрию
                new_size = shutil.get_terminal_size()
                if (new_size.columns, new_size.lines) != (term_size.columns, term_size.lines):
                    term_size = new_size
                    try:
                        screen.clear()
                    except Exception:
                        pass
                    panel_w, inner, rows_target, item_rows = _geometry(console, entries, height,
                                                                       (term_size.columns, term_size.lines))
                    screen.width = panel_w
                    screen.height = rows_target + 2

                if state["digits"] and time.time() - state["digits_at"] > 0.8:
                    state["digits"] = ""
                view, sel = rebuild_view()
                total = len(sel)
                if state["cursor"] >= total:
                    state["cursor"] = max(0, total - 1)
                # динамический расчёт строк под пункты: вычитаем фактические
                # строки поиска/номера/заголовков/счётчика/подсказки, иначе
                # _frame обрезал бы хвост вместе со счётчиком и подсказкой
                overhead = _overhead_rows(view, state["query"], state["digits"], hint)
                item_rows = max(1, rows_target - overhead)
                idxs, start = _visible(total, state["cursor"], item_rows)
                shown = []
                n = -1
                for e in view:
                    if isinstance(e, Section):
                        shown.append(e)
                    else:
                        n += 1
                        if n in idxs:
                            shown.append(e)
                rows = _build_rows(shown, state["cursor"], start, state["query"], state["digits"],
                                   total, len(idxs), inner, hint)
                # счётчик скролла + подсказка не должны пропадать в тесном терминале
                screen.draw(_frame(title, subtitle, rows, panel_w, rows_target, inner,
                                   keep_tail=3 if hint else 1))

                # ждём ввод с таймаутом — кадр обновляется и без нажатия клавиши
                # (истечение буфера цифр) и перерисовывается после ресайза
                try:
                    ready, _, _ = _select.select([fd], [], [], 0.2)
                except OSError:
                    ready = True
                if not ready:
                    continue
                key, sym = _read_key(fd, kbuf)
                if key == "none":
                    continue

                if key in ("enter", "tab"):
                    actives = active_items()
                    if actives:
                        chosen = actives[state["cursor"]]
                    break
                if key in ("esc", "cancel", "exit"):
                    if key == "esc" and (state["query"] or state["digits"]):
                        state["query"] = ""
                        state["digits"] = ""
                        continue
                    chosen = None
                    break
                if key == "up":
                    move(-1)
                elif key == "down":
                    move(1)
                elif key == "pageup":
                    move(-item_rows)
                elif key == "pagedown":
                    move(item_rows)
                elif key == "home":
                    state["cursor"] = 0
                elif key == "end":
                    state["cursor"] = max(0, len(active_items()) - 1)
                elif key == "char":
                    if sym.isdigit():
                        state["digits"] += sym
                        state["digits_at"] = time.time()
                        jump(int(state["digits"]))
                    elif sym in ("q", "Q", "й", "Й") and state["query"]:
                        # q/й отменяют меню ТОЛЬКО когда поиск уже начат (запрос
                        # непустой); при пустом запросе они печатаются как есть,
                        # иначе этими буквами нельзя было бы начать поиск.
                        chosen = None
                        break
                    elif sym == " ":
                        state["query"] = ""
                    else:
                        state["query"] += sym
                        state["cursor"] = 0
                elif key == "backspace":
                    if state["query"]:
                        state["query"] = state["query"][:-1]
                    elif state["digits"]:
                        state["digits"] = state["digits"][:-1]
                        jump(int(state["digits"]) if state["digits"] else 1)
                elif key == "delete":
                    state["query"] = ""
                    state["digits"] = ""
        except Exception as e:
            chosen = None
            try:
                sys.__stdout__.write(f"\n  Ошибка отображения меню: {e}\n")
                sys.__stdout__.flush()
            except Exception:
                pass
        finally:
            # очищаем кадр целиком, чтобы снизу не осталось мусора
            try:
                screen.clear()
            except Exception:
                pass
            try:
                sys.__stdout__.write("\x1b[?25h")
                sys.__stdout__.flush()
            except Exception:
                pass
            if old_attrs is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
                except Exception:
                    pass

        # Фоновый вывод показываем после меню, а не поверх него
        lines = [l for l in pending.take() if l.strip()]
        if lines:
            out = sys.__stdout__
            out.write("\n  ┄┄ события во время меню ┄┄\n")
            for line in lines:
                out.write("  " + line.rstrip() + "\n")
            out.flush()

    if chosen is not None:
        line = Text()
        line.append("  ")
        line.append(C_MARK, style=f"bold {C_ACCENT}")
        line.append(" ")
        line.append(_trunc(chosen.title, 60), style=f"bold {C_TITLE}")
        if chosen.badge:
            line.append(f"  ({chosen.badge})", style=C_OK)
        line.append("  выбрано", style=C_DIM)
        console.print(line)
    return chosen
