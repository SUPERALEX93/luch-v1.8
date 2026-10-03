"""Управление подключёнными устройствами (телефон на Android-приложении).

Серверная часть «моста» между ИИ и телефоном:
  * реестр устройств: у каждого свой client_id и токен, они не связаны с web_token;
  * очередь команд с монотонным seq — телефон забирает и отвечает тем же seq;
  * выбор устройства и сводка для ИИ (маршрутизацию по тексту промта делает сам ИИ).

Телефон лежит в той же локальной сети, что и сервер, поэтому транспорт простой:
HTTP-опрос раз в несколько секунд. Очередь серверная, поэтому позже её можно
перевести на WebSocket, не меняя ничего здесь.
"""
import copy
import json
import logging
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

# ── Реестр устройств ────────────────────────────────────────────────────────
# devices[client_id] = {
#   "id", "name", "kind", "token", "caps", "battery", "model", "os",
#   "lat", "lng", "accuracy", "location_at", "last_seen", "created", "online"
# }
devices = {}
devices_lock = threading.RLock()

# ── Очередь команд для телефонов ────────────────────────────────────────────
# queue[client_id] = [{"seq", "id", "command", "args", "created", "created_ts"}]
queue = {}
# Результаты адресуются парой (client_id, cmd_id). Глобальный cmd_id угадываем,
# поэтому без привязки к клиенту чужое устройство могло бы подсунуть свой вывод.
results = {}          # (client_id, cmd_id) -> {"client_id","status","output","at","at_ts"}
_acked = {}           # client_id -> последний seq, подтверждённый в submit()
_waiters = {}         # (client_id, cmd_id) -> threading.Event ожидающего enqueue()
_seq = [0]

# Порядок блокировок всегда один: devices_lock -> _seq_lock. Обратный порядок
# запрещён (взаимная блокировка), поэтому все функции берут их только так.
_seq_lock = threading.RLock()

# Устройства и их токены должны переживать перезапуск сервера, иначе телефон
# пришлось бы регистрировать заново при каждом запуске.
DEVICES_FILE = Path(__file__).resolve().parent / "devices.json"

MAX_QUEUE = 50        # сколько команд ждать ответа на одном устройстве
RESULT_TTL = 900      # 15 минут: старше — результат считается протухшим
COMMAND_TTL = 180     # 3 минуты: недоставленная команда старше — протухла и выкидывается
RESULTS_MAX = 500     # жёсткий предел таблицы результатов (на случай всплеска)
MAX_OUTPUT = 4000     # предел вывода телефона, который уходит в контекст ИИ

# Что телефон умеет. capabilities приходят от приложения при регистрации.
KNOWN_CAPS = ("location", "notify", "vibrate", "open_url", "ring", "battery",
              "clipboard", "stop_ring", "status", "wake", "torch", "volume",
              "brightness", "open_app", "apps", "dial", "share", "toast",
              "open_settings")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _mark_seen(dev):
    """Отметить связь: ISO-время для файла и веб-интерфейса + epoch для быстрой проверки."""
    dev["last_seen"] = _now()
    dev["last_seen_ts"] = time.time()


def _is_online(dev, offline_after=90):
    """Устройство считается онлайн, если polled недавно.

    Горячий путь (опрос очереди раз в 2-3 с и цикл ожидания в enqueue) не должен
    каждый раз парсить ISO. Если epoch-кэша нет, разбираем ISO один раз и кэшируем.
    """
    ts = dev.get("last_seen_ts")
    if ts is None:
        last = dev.get("last_seen")
        if not last:
            return False
        try:
            ts = datetime.fromisoformat(last).timestamp()
        except (ValueError, TypeError):
            return False
        dev["last_seen_ts"] = ts
    try:
        return (time.time() - float(ts)) < offline_after
    except (TypeError, ValueError):
        return False


def register_device(name="Телефон", kind="android", token=None, caps=None,
                    model=None, os_version=None, client_id=None):
    """Зарегистрировать устройство. Возвращает client_id и его токен.

    Повторная регистрация существующего client_id разрешена только тому, кто
    доказал владение текущим токеном (сравнение постоянного времени). Иначе любой
    желающий по угаданному client_id перезаписал бы запись и украл токен.
    """
    # Новому устройству при пустых caps достаётся безопасный минимум; у уже
    # зарегистрированного пустой список означает «ничего не меняем», иначе
    # пропущенные caps затирали бы реальные возможности телефона.
    caps = [c for c in (caps or []) if c in KNOWN_CAPS]
    fallback_caps = caps or ["location", "notify", "vibrate"]
    with devices_lock:
        if client_id and client_id in devices:
            dev = devices[client_id]
            own = str(dev.get("token") or "")
            given = str(token or "")
            # У записи ПК токена нет вовсе; удалённая перерегистрация такой
            # записи (в т.ч. pc) запрещена — иначе её можно было бы переписать.
            if not own or not given or not secrets.compare_digest(
                    own.encode("utf-8"), given.encode("utf-8")):
                raise PermissionError("устройство с таким client_id уже зарегистрировано")
            # Обновляем только явно переданные поля, не понижая устройство.
            if name:
                dev["name"] = name
            if caps:
                dev["caps"] = caps
            if model:
                dev["model"] = model
            if os_version:
                dev["os"] = os_version
            _mark_seen(dev)
        else:
            client_id = client_id or "dev_" + secrets.token_hex(4)
            now = _now()
            dev = {
                "id": client_id, "name": name or "Телефон", "kind": kind or "android",
                "token": token or secrets.token_urlsafe(24),
                "caps": fallback_caps, "model": model, "os": os_version,
                "battery": None, "lat": None, "lng": None, "accuracy": None,
                "location_at": None, "last_seen": now, "last_seen_ts": time.time(),
                "created": now,
            }
            devices[client_id] = dev
        with _seq_lock:
            queue.setdefault(client_id, [])
    save_devices()
    return {"client_id": dev["id"], "token": dev["token"], "name": dev["name"],
            "caps": dev["caps"]}


def check_token(client_id, token):
    with devices_lock:
        dev = devices.get(client_id)
    if not dev:
        return False
    own = str(dev.get("token") or "")
    given = str(token or "")
    # У записи ПК токена нет вовсе, и пустой токен не должен считаться верным:
    # иначе кто угодно подставил бы client_id=pc и забрал бы чужую очередь.
    if not own or not given:
        return False
    # compare_digest не принимает не-ASCII, а телефон может прислать что угодно
    return secrets.compare_digest(own.encode("utf-8"), given.encode("utf-8"))


def save_devices():
    """Записать реестр на диск. Файл с токенами — только владельцу.

    Под блокировкой делаем только глубокую копию, а сериализацию и запись — уже
    без неё: иначе диск и chmod тормозили бы каждый запрос. Ошибку глотаем и
    логируем: падать в обработчике запроса из-за файла нельзя.
    """
    try:
        with devices_lock:
            with _seq_lock:
                payload = {"_seq": _seq[0], "devices": copy.deepcopy(devices)}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        DEVICES_FILE.write_text(text, encoding="utf-8")
        DEVICES_FILE.chmod(0o600)
    except Exception as exc:                      # noqa: BLE001 — файл не должен ронять запрос
        _log.warning("не удалось сохранить %s: %s", DEVICES_FILE, exc)


def load_devices():
    """Поднять реестр после перезапуска сервера."""
    try:
        raw = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    # Телефоны помнят последний обработанный seq. Если счётчик не восстановить,
    # новые команды после перезапуска сервера покажутся телефону «уже виденными».
    stored = raw.get("devices") if isinstance(raw.get("devices"), dict) else raw
    try:
        saved_seq = int(raw.get("_seq", 0))
    except (TypeError, ValueError):
        saved_seq = 0
    with devices_lock:
        for cid, dev in stored.items():
            if str(cid).startswith("_"):
                continue
            if isinstance(dev, dict) and dev.get("id"):
                # Ключ карты — истина: list_devices ищет по dev["id"], и без
                # принудительной синхронизации мог бы получить None.
                dev["id"] = cid
                if not isinstance(dev.get("caps"), list):
                    dev["caps"] = []
                # Прогреваем epoch-кэш, чтобы _is_online не парсил ISO на каждом опросе.
                if "last_seen_ts" not in dev:
                    try:
                        dev["last_seen_ts"] = datetime.fromisoformat(
                            dev.get("last_seen") or "").timestamp()
                    except (ValueError, TypeError):
                        dev["last_seen_ts"] = 0.0
                # last_seen с прошлого запуска — телефон считается не на связи
                devices[cid] = dev
        with _seq_lock:
            for cid in devices:
                queue.setdefault(cid, [])
            _seq[0] = max(_seq[0], saved_seq)


def device_info(client_id):
    """Данные устройства без токена — для показа и для ИИ."""
    with devices_lock:
        dev = devices.get(client_id)
        if not dev:
            return None
        info = {
            "id": dev.get("id") or client_id, "name": dev.get("name") or client_id,
            "kind": dev.get("kind"), "caps": list(dev.get("caps") or []),
            "model": dev.get("model"), "os": dev.get("os"),
            "battery": dev.get("battery"), "online": _is_online(dev),
            "last_seen": dev.get("last_seen"), "created": dev.get("created"),
            "lat": dev.get("lat"), "lng": dev.get("lng"),
            "accuracy": dev.get("accuracy"), "location_at": dev.get("location_at"),
        }
        with _seq_lock:
            info["pending"] = len(queue.get(client_id, []))
    return info


def list_devices():
    with devices_lock:
        ids = list(devices)
    return [d for d in (device_info(cid) for cid in ids) if d]


def forget_device(client_id):
    with devices_lock:
        if client_id not in devices:
            return {"status": "error", "message": "устройство не найдено"}
        devices.pop(client_id)
        with _seq_lock:
            queue.pop(client_id, None)
            _acked.pop(client_id, None)
    save_devices()
    return {"status": "ok", "message": "устройство %s забыто" % client_id}


def rename_device(client_id, name):
    with devices_lock:
        dev = devices.get(client_id)
        if not dev:
            return {"status": "error", "message": "устройство не найдено"}
        dev["name"] = (name or "").strip()[:40] or dev.get("name") or client_id
        new_name = dev["name"]
    save_devices()
    return {"status": "ok", "name": new_name}


def touch(client_id, battery=None):
    """Отметить, что устройство только что было на связи."""
    with devices_lock:
        dev = devices.get(client_id)
        if not dev:
            return None
        _mark_seen(dev)
        if battery is not None:
            try:
                dev["battery"] = int(battery)
            except (TypeError, ValueError):
                pass
    return dev


def report_location(client_id, lat, lng, accuracy=None):
    with devices_lock:
        dev = devices.get(client_id)
        if not dev:
            return {"status": "error", "message": "устройство не зарегистрировано"}
        try:
            lat_f, lng_f = float(lat), float(lng)
            acc_f = float(accuracy) if accuracy is not None else None
        except (TypeError, ValueError):
            return {"status": "error", "message": "некорректные координаты"}
        dev["lat"], dev["lng"], dev["accuracy"] = lat_f, lng_f, acc_f
        dev["location_at"] = _now()
        _mark_seen(dev)
        name = dev.get("name") or "android"
    # Телефон — это клиент ЛУЧ, поэтому его координаты должны попасть в общую
    # геолокацию: от неё зависят геокод, маршруты, рядом и история перемещений.
    # Внешний вызов делаем без блокировки и не глотаем ошибку молча.
    try:
        import work_fuctions
        work_fuctions.update_location(lat_f, lng_f, acc_f,
                                      source="phone:" + str(name))
    except (ImportError, AttributeError, TypeError, ValueError, KeyError,
            OSError, RuntimeError) as exc:
        _log.warning("координаты телефона не попали в общую геолокацию: %s", exc)
        return {"status": "ok", "lat": lat_f, "lng": lng_f, "location_saved": False,
                "message": "координаты телефона приняты, но в общую геолокацию "
                           "не записались: %s" % exc}
    return {"status": "ok", "lat": lat_f, "lng": lng_f, "location_saved": True}


# ── Очередь команд ──────────────────────────────────────────────────────────

def _next_seq():
    with _seq_lock:
        _seq[0] += 1
        return _seq[0]


def _res_key(client_id, cmd_id):
    """Ключ результата: команда привязана к устройству, а не только к cmd_id."""
    return (str(client_id), str(cmd_id))


def _command_age(cmd):
    """Возраст команды в секундах; 0, если время неизвестно."""
    ts = cmd.get("created_ts")
    if ts is None:
        created = cmd.get("created")
        if not created:
            return 0.0
        try:
            ts = datetime.fromisoformat(created).timestamp()
        except (ValueError, TypeError):
            return 0.0
    try:
        return time.time() - float(ts)
    except (TypeError, ValueError):
        return 0.0


def _prune_queue_locked(client_id):
    """Выкинуть протухшие команды. Вызывать ТОЛЬКО под _seq_lock."""
    pend = queue.get(client_id)
    if not pend:
        return
    alive = [c for c in pend if _command_age(c) < COMMAND_TTL]
    if len(alive) != len(pend):
        queue[client_id] = alive


def _drop_command_locked(client_id, cmd_id):
    """Убрать команду из очереди. Вызывать ТОЛЬКО под _seq_lock."""
    pend = queue.get(client_id)
    if not pend:
        return
    queue[client_id] = [c for c in pend if str(c["id"]) != str(cmd_id)]


def _store_result_locked(client_id, cmd_id, status, output, extra=None):
    """Положить результат и вернуть Event ожидающего enqueue (если он есть).

    Вызывать ТОЛЬКО под _seq_lock.
    """
    key = _res_key(client_id, cmd_id)
    entry = {"client_id": client_id, "status": status, "output": output,
             "at": _now(), "at_ts": time.time()}
    if extra:
        entry.update(extra)
    results[key] = entry
    return _waiters.get(key)


def enqueue(client_id, command, args=None, wait=15):
    """Отправить команду телефону. Ждёт ответа до wait секунд."""
    with devices_lock:
        dev = devices.get(client_id)
        if not dev:
            return {"status": "error", "message": "устройство %s не найдено" % client_id}
        dev_name = dev.get("name") or client_id
        dev_caps = list(dev.get("caps") or [])
    if command not in dev_caps:
        return {"status": "error",
                "message": "устройство «%s» не умеет «%s». Умеет: %s"
                           % (dev_name, command, ", ".join(dev_caps) or "ничего")}

    # Event, который submit() выставит сразу после сохранения результата: ожидание
    # перестаёт быть sleep-опросом и не занимает поток лишние 15-30 секунд.
    ev = threading.Event()
    with _seq_lock:
        seq = _next_seq()
        item = {"seq": seq, "id": "c%d" % seq, "command": command,
                "args": args or {}, "created": _now(), "created_ts": time.time()}
        _prune_queue_locked(client_id)
        q = queue.setdefault(client_id, [])
        q.append(item)
        # Переполнение: оставляем вытеснение самого старого, но не молча — его
        # ожидающий enqueue получает явную ошибку «очередь переполнена».
        while len(q) > MAX_QUEUE:
            dropped = q.pop(0)
            d_ev = _store_result_locked(client_id, dropped["id"], "error",
                                        "команда отброшена: очередь переполнена",
                                        {"queue_full": True})
            if d_ev is not None:
                d_ev.set()
        key = _res_key(client_id, item["id"])
        _waiters[key] = ev

    # Счётчик seq обязан пережить перезапуск: телефон помнит, какие команды уже
    # видел, и при откате счётчика новые команды он больше никогда не заберёт.
    save_devices()

    deadline = time.time() + max(0.5, float(wait or 15))
    try:
        while time.time() < deadline:
            got = results.get(key)
            if got:
                return {"status": got.get("status", "ok"), "output": got.get("output", ""),
                        "device": dev_name, "command": command, "waited": True}
            if not _is_online(dev, offline_after=30):
                return {"status": "error", "offline": True,
                        "message": "устройство «%s» не на связи — команда не доставлена" % dev_name}
            ev.wait(0.4)
        return {"status": "error", "timeout": True,
                "message": "устройство «%s» не ответило за %s с" % (dev_name, int(wait or 15))}
    finally:
        # Команда не дождалась ответа (офлайн/таймаут) — убираем её из очереди,
        # иначе телефон выполнит её позже (ring/dial/open_url) и pending останется раздутым.
        with _seq_lock:
            _waiters.pop(key, None)
            _drop_command_locked(client_id, item["id"])


def take(client_id, since=0, limit=20):
    """Забрать команды для телефона.

    since — последний seq, который телефон уже обработал. Возвращаемый seq — это
    acked_seq, который растёт ТОЛЬКО в submit(): пока команда не подтверждена,
    сервер не говорит телефону «ты догнал». Незавершённые команды остаются в
    очереди и отдаются заново, поэтому потеря upload'а результата не теряет команду.
    """
    since_i = int(since or 0)
    with _seq_lock:
        _prune_queue_locked(client_id)
        pend = list(queue.get(client_id) or [])
        # Телефон помнит seq больше, чем счётчик сервера, только если сервер потерял
        # состояние (файл удалён или откатился). Тогда фильтр по since отбрасывал бы
        # всё, и команды застряли бы навсегда — сбрасываем фильтр и отдаём очередь.
        reset = since_i > _seq[0]
        # В очереди лежат ровно неподтверждённые команды: отдаём их все (с учётом
        # limit), а не только те, что выше since, иначе команда с меньшим seq
        # застряла бы, если более поздняя уже подтверждена.
        fresh = pend[:int(limit or 20)]
        acked = int(_acked.get(client_id, 0))
    return {"commands": fresh, "seq": acked, "reset": reset}


def submit(client_id, cmd_id, status="ok", output=""):
    """Телефон прислал результат выполненной команды."""
    # Вывод телефона уходит в контекст ИИ, поэтому режем его до разумного предела.
    out = output if isinstance(output, str) else str(output or "")
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + "\n…[вывод обрезан, всего %d символов]" % len(out)
    key = _res_key(client_id, cmd_id)
    with _seq_lock:
        pend = queue.get(client_id) or []
        # Команда обязана принадлежать очереди именно этого устройства: иначе
        # чужой client_id мог бы подсунуть вывод в ожидающий enqueue другого телефона.
        item = next((c for c in pend if str(c["id"]) == str(cmd_id)), None)
        if item is None:
            return {"status": "error",
                    "message": "команда %s не найдена в очереди устройства" % cmd_id}
        _acked[client_id] = max(int(_acked.get(client_id, 0)), int(item.get("seq") or 0))
        ev = _store_result_locked(client_id, cmd_id, status, out)
        _drop_command_locked(client_id, cmd_id)
    if ev is not None:
        ev.set()
    _gc_results()
    return {"status": "ok"}


def _gc_results():
    """Удалить протухшие результаты. Вызывается из submit()."""
    now = time.time()
    with _seq_lock:
        for key in list(results):
            entry = results[key]
            ts = entry.get("at_ts")
            if ts is None:
                try:
                    ts = datetime.fromisoformat(entry["at"]).timestamp()
                except (ValueError, TypeError, KeyError):
                    ts = now
            try:
                age = now - float(ts)
            except (TypeError, ValueError):
                age = 0
            if age > RESULT_TTL:
                results.pop(key, None)
        # Жёсткий потолок на случай всплеска: выкидываем самые старые по вставке.
        if len(results) > RESULTS_MAX:
            for key in list(results)[:len(results) - RESULTS_MAX]:
                results.pop(key, None)


# ── Выбор устройства и сводка для ИИ ────────────────────────────────────────
# Раньше здесь жили resolve_target/PHONE_WORDS/PC_WORDS, но маршрутизацию по
# тексту промта делает сам ИИ (см. COMMANDS_HELP), и вызывающих у них нет.

def online_devices(kind=None):
    with devices_lock:
        ids = list(devices)
    out = []
    for cid in ids:
        info = device_info(cid)
        if not info:
            continue
        if kind and info.get("kind") != kind:
            continue
        if info.get("online"):
            out.append(info)
    return out


# ── Команды для ИИ ───────────────────────────────────────────────────────────

def _one_phone():
    """Самый «свежий» онлайн-телефон: если их несколько — берём тот, кто недавно отвечал."""
    on = online_devices("android")
    if not on:
        return None
    return max(on, key=lambda d: str(d.get("last_seen") or ""))


def _status(summary):
    """Короткая сводка по устройствам — попадает в ответ ИИ."""
    listed = list_devices()
    parts = []
    for d in listed:
        bits = ["%s (%s)" % (d.get("name") or d.get("id"),
                             "на связи" if d.get("online") else "не на связи")]
        if d.get("battery") is not None:
            try:
                bits.append("батарея %d%%" % int(d["battery"]))
            except (TypeError, ValueError):
                bits.append("батарея %s" % d["battery"])
        if d.get("lat") is not None:
            bits.append("координаты есть, %s" % d.get("location_at"))
        parts.append(", ".join(bits))
    # register_desktop() всегда кладёт запись «pc», поэтому проверка «нет устройств»
    # была недостижима. Нас интересует отсутствие именно телефонов; перечень
    # устройств (например, одного ПК) при этом сохраняется.
    full = summary + (" | " + " ; ".join(parts) if parts else "")
    if not any(d.get("kind") == "android" for d in listed):
        return {"status": "error",
                "message": "Ни одного телефона не подключено. Поставь Android-приложение LUCH "
                           "и открой его — устройство появится здесь автоматически.",
                "summary": full, "devices": listed}
    return {"status": "ok", "summary": full, "devices": listed}


def _need_phone():
    """Найти онлайн-телефон или объяснить, что делать."""
    dev = _one_phone()
    if not dev:
        return None, ("Телефон не на связи. Проверь, что приложение LUCH открыто и "
                      "подключено к той же сети, что и компьютер.")
    return dev, None


def phone_notify(text="", title="ЛУЧ", target="auto", wait=15):
    """Показать уведомление на телефоне."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "notify", {"title": title, "text": text}, wait)


def phone_vibrate(ms=500, target="auto", wait=15):
    """Завибрировать телефоном."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    try:
        ms = max(50, min(int(ms), 10000))
    except (TypeError, ValueError):
        ms = 500
    return enqueue(dev["id"], "vibrate", {"ms": ms}, wait)


def phone_ring(target="auto", wait=20):
    """Заставить телефон звонить/издавать громкий сигнал (потерянный телефон)."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "ring", {}, wait)


def phone_open_url(url="", target="auto", wait=15):
    """Открыть ссылку на телефоне."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return enqueue(dev["id"], "open_url", {"url": url}, wait)


def phone_battery(target="auto"):
    """Заряд батареи телефона."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    name = dev.get("name") or dev.get("id") or "телефон"
    bat = dev.get("battery")
    if bat is None:
        return {"status": "ok", "device": name, "battery": None,
                "message": "Телефон «%s» ещё не сообщал заряд батареи." % name}
    try:
        value = int(bat)
    except (TypeError, ValueError):
        value = None
    return {"status": "ok", "device": name, "battery": bat,
            "summary": "Батарея «%s»: %s%s" % (name, bat, "%" if value is not None else "")}


def devices_list():
    """Какие устройства вообще подключены."""
    return _status("Устройства:")


def phone_location():
    """Координаты телефона, если приложение их прислало."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    if dev.get("lat") is None:
        return {"status": "error",
                "message": "Телефон ещё не прислал координаты. Подожди немного или попроси "
                           "открыть карту в приложении."}
    return {"status": "ok", "device": dev["name"], "lat": dev["lat"], "lng": dev["lng"],
            "accuracy": dev.get("accuracy"), "at": dev.get("location_at")}


def register_desktop(name="Этот компьютер"):
    """ПК сам себя объявляет, чтобы ИИ видел оба устройства в одном списке.

    Это единственное место, где создаётся запись «pc» (с пустым токеном), поэтому
    register_device не должен принимать удалённую перерегистрацию tokenless-записи.
    """
    with devices_lock:
        if "pc" in devices:
            dev = devices["pc"]
            dev["name"] = name
            _mark_seen(dev)
        else:
            now = _now()
            devices["pc"] = {
                "id": "pc", "name": name, "kind": "pc", "token": "",
                "caps": ["terminal", "system", "browser", "location"],
                "model": None, "os": "linux", "battery": None,
                "lat": None, "lng": None, "accuracy": None,
                "location_at": None, "last_seen": now, "last_seen_ts": time.time(),
                "created": now,
            }
        with _seq_lock:
            queue.setdefault("pc", [])
    return device_info("pc")


# Описание команд для системного промпта ИИ (формат как у commands_ai_and_args)
COMMANDS_HELP = """
19. devices_list - показать все подключённые устройства (телефоны и компьютер): имя, на связи ли, батарея, координаты.
    Если устройств нет - так и скажи пользователю, что надо поставить Android-приложение.

20. phone_notify - показать уведомление на телефоне. Обязательно передай текст!
    Аргументы: text - что написать, title - заголовок (необязательно), wait - сколько ждать ответа в секундах (необязательно).
    Пример: {"command": "phone_notify", "args": {"text": "Пора выйти из дома", "title": "ЛУЧ"}}

21. phone_vibrate - завибрировать телефоном.
    Аргументы: ms - длительность в миллисекундах (необязательно, по умолчанию 500), wait.
    Пример: {"command": "phone_vibrate", "args": {"ms": 1500}}

22. phone_ring - заставить телефон громко звонить. Используй, если телефон потерян.
    Пример: {"command": "phone_ring", "args": {}}

23. phone_open_url - открыть ссылку на телефоне (погода, карта, маршрут).
    Пример: {"command": "phone_open_url", "args": {"url": "https://yandex.ru/pogoda"}}

24. phone_battery - узнать заряд батареи телефона.
    Пример: {"command": "phone_battery", "args": {}}

25. phone_location - координаты телефона (работает, только если приложение их прислало).
    Пример: {"command": "phone_location", "args": {}}

УПРАВЛЕНИЕ УСТРОЙСТВАМИ ПО ПРОМТУ ПОЛЬЗОВАТЕЛЯ:
У тебя есть компьютер (на нём работает сервер, команды без префикса phone_ выполняются локально)
и телефоны (команды с префиксом phone_ уходят на Android-устройство).
САМ ВЫБИРАЙ УСТРОЙСТВО ПО СЛОВАМ В ПРОМТЕ ПОЛЬЗОВАТЕЛЯ, НЕ СПРАШИВАЯ:
- есть слова "телефон", "андроид", "смартфон", "мобильный" -> это телефон -> используй команды phone_*;
- есть слова "комп", "компьютер", "пк", "десктоп", "ноутбук" -> это компьютер -> используй обычные команды;
- оба слова есть -> можно и то и другое, объясни пользователю что сделал на каждом;
- слов-указателей нет -> выполняй на компьютере, а про телефон спроси.
Перед громкими действиями (ring, вибрация) предупреди пользователя, если он не просил прямо.
"""


# Поднимаем сохранённые устройства при импорте модуля.
load_devices()


# --------------------------------------------------------------------------
# Управление телефоном по командам ИИ.
# Всё, что не требует прав администратора: звонок не совершается,
# только открывается звонилка с номером.
# --------------------------------------------------------------------------

def phone_status(target="auto", wait=30):
    """Состояние телефона: модель, Android, заряд, заряжается ли."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "status", {}, wait=wait)


def phone_wake(target="auto", wait=15):
    """Разбудить экран телефона и открыть приложение."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "wake", {}, wait)


def phone_torch(on=True, target="auto", wait=15):
    """Включить или выключить фонарик телефона."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    flag = bool(on) and str(on).lower() not in ("off", "false", "0", "выключить", "нет")
    return enqueue(dev["id"], "torch", {"on": flag}, wait)


def phone_volume(action="up", level=None, stream="music", target="auto", wait=15):
    """Громкость телефона: up (громче), down (тише), set (уровень 0-100), mute (выключить)."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    args = {"action": str(action or "up"), "stream": str(stream or "music")}
    if level is not None:
        try:
            args["level"] = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return {"status": "error", "message": "уровень должен быть числом 0-100"}
    return enqueue(dev["id"], "volume", args, wait)


def phone_brightness(level=50, target="auto", wait=15):
    """Яркость экрана телефона, 0-100."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    try:
        val = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return {"status": "error", "message": "яркость должна быть числом 0-100"}
    return enqueue(dev["id"], "brightness", {"level": val}, wait)


def phone_open_app(name="", target="auto", wait=20):
    """Запустить приложение на телефоне по названию (например: Камера, Телефон, Настройки)."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    if not str(name or "").strip():
        return {"status": "error", "message": "не указано имя приложения"}
    return enqueue(dev["id"], "open_app", {"name": str(name).strip()}, wait)


def phone_apps(filter="", target="auto", wait=20):
    """Список приложений на телефоне; filter — подстрока для поиска по названию."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "apps", {"filter": str(filter or "")}, wait)


def phone_clipboard(text="", get=False, target="auto", wait=15):
    """Положить текст в буфер обмена (get=true — прочитать его)."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "clipboard",
                   {"text": str(text or ""), "get": bool(get)}, wait)


def phone_dial(number="", target="auto", wait=15):
    """Открыть номер в звонилке телефона. Звонок не совершается."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    num = "".join(c for c in str(number or "") if c.isdigit() or c == "+")
    if not num:
        return {"status": "error", "message": "не передан номер"}
    return enqueue(dev["id"], "dial", {"number": num}, wait)


def phone_share(text="", title="", target="auto", wait=20):
    """Предложить отправить текст с телефона (мессенджеры, почта)."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    if not str(text or "").strip():
        return {"status": "error", "message": "не передан текст"}
    return enqueue(dev["id"], "share", {"text": str(text), "title": str(title or "")}, wait)


def phone_toast(text="", target="auto", wait=15):
    """Показать короткое всплывающее сообщение на экране телефона."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    if not str(text or "").strip():
        return {"status": "error", "message": "пустой текст"}
    return enqueue(dev["id"], "toast", {"text": str(text)}, wait)


def phone_open_settings(what="", target="auto", wait=15):
    """Открыть настройки телефона: wifi, bluetooth, apps, display, sound, home, storage."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "open_settings", {"what": str(what or "")}, wait)


def phone_stop_ring(target="auto", wait=15):
    """Прекратить, чтобы телефон звонил (после phone_ring)."""
    dev, err = _need_phone()
    if err:
        return {"status": "error", "message": err}
    return enqueue(dev["id"], "stop_ring", {}, wait)
