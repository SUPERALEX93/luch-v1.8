"""Управление подключёнными устройствами (телефон на Android-приложении).

Серверная часть «моста» между ИИ и телефоном:
  * реестр устройств: у каждого свой client_id и токен, они не связаны с web_token;
  * очередь команд с монотонным seq — телефон забирает и отвечает тем же seq;
  * маршрутизация «телефон или ПК» по тексту промта пользователя.

Телефон лежит в той же локальной сети, что и сервер, поэтому транспорт простой:
HTTP-опрос раз в несколько секунд. Очередь серверная, поэтому позже её можно
перевести на WebSocket, не меняя ничего здесь.
"""
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Реестр устройств ────────────────────────────────────────────────────────
# devices[client_id] = {
#   "id", "name", "kind", "token", "caps", "battery", "model", "os",
#   "lat", "lng", "accuracy", "location_at", "last_seen", "created", "online"
# }
devices = {}
devices_lock = threading.RLock()

# ── Очередь команд для телефонов ────────────────────────────────────────────
# queue[client_id] = [{"seq", "id", "command", "args", "created"}]
queue = {}
results = {}          # cmd_id -> {"client_id", "status", "output", "at"}
_seq = [0]

# Устройства и их токены должны переживать перезапуск сервера, иначе телефон
# пришлось бы регистрировать заново при каждом запуске.
DEVICES_FILE = Path(__file__).resolve().parent / "devices.json"
_seq_lock = threading.RLock()

MAX_QUEUE = 50        # сколько команд ждать ответа на одном устройстве
RESULT_TTL = 900     # 15 минут, потом результат считается протухшим

# Что телефон умеет. capabilities приходят от приложения при регистрации.
KNOWN_CAPS = ("location", "notify", "vibrate", "open_url", "ring", "battery",
              "clipboard", "stop_ring", "status", "wake", "torch", "volume",
              "brightness", "open_app", "apps", "dial", "share", "toast",
              "open_settings")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _is_online(dev, offline_after=90):
    """Устройство считается онлайн, если polled недавно."""
    last = dev.get("last_seen")
    if not last:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(last)).total_seconds() < offline_after
    except (ValueError, TypeError):
        return False


def register_device(name="Телефон", kind="android", token=None, caps=None,
                    model=None, os_version=None, client_id=None):
    """Зарегистрировать устройство. Возвращает client_id и его токен."""
    caps = [c for c in (caps or []) if c in KNOWN_CAPS] or ["location", "notify", "vibrate"]
    with devices_lock:
        if client_id and client_id in devices:
            dev = devices[client_id]
            dev.update({"name": name or dev["name"], "caps": caps,
                        "model": model, "os": os_version, "last_seen": _now()})
        else:
            client_id = client_id or "dev_" + secrets.token_hex(4)
            dev = {
                "id": client_id, "name": name or "Телефон", "kind": kind or "android",
                "token": token or secrets.token_urlsafe(24),
                "caps": caps, "model": model, "os": os_version,
                "battery": None, "lat": None, "lng": None, "accuracy": None,
                "location_at": None, "last_seen": _now(), "created": _now(),
            }
            devices[client_id] = dev
        queue.setdefault(client_id, [])
    save_devices()
    return {"client_id": dev["id"], "token": dev["token"], "name": dev["name"],
            "caps": dev["caps"]}


def check_token(client_id, token):
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
    """Записать реестр на диск. Файл с токенами — только владельцу."""
    try:
        import json
        with devices_lock:
            payload = {"_seq": _seq[0], "devices": devices}
            DEVICES_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        DEVICES_FILE.chmod(0o600)
    except OSError:
        pass


def load_devices():
    """Поднять реестр после перезапуска сервера."""
    import json
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
                # last_seen с прошлого запуска — телефон считается не на связи
                devices[cid] = dev
                queue.setdefault(cid, [])
        _seq[0] = max(_seq[0], saved_seq)


def device_info(client_id):
    """Данные устройства без токена — для показа и для ИИ."""
    dev = devices.get(client_id)
    if not dev:
        return None
    return {
        "id": dev["id"], "name": dev["name"], "kind": dev["kind"],
        "caps": dev["caps"], "model": dev.get("model"), "os": dev.get("os"),
        "battery": dev.get("battery"), "online": _is_online(dev),
        "last_seen": dev.get("last_seen"), "created": dev.get("created"),
        "lat": dev["lat"], "lng": dev["lng"], "accuracy": dev.get("accuracy"),
        "location_at": dev.get("location_at"),
        "pending": len(queue.get(client_id, [])),
    }


def list_devices():
    return [device_info(dev["id"]) for dev in list(devices.values())]


def forget_device(client_id):
    with devices_lock:
        if client_id not in devices:
            return {"status": "error", "message": "устройство не найдено"}
        devices.pop(client_id)
        queue.pop(client_id, None)
    save_devices()
    return {"status": "ok", "message": "устройство %s забыто" % client_id}


def rename_device(client_id, name):
    dev = devices.get(client_id)
    if not dev:
        return {"status": "error", "message": "устройство не найдено"}
    dev["name"] = (name or "").strip()[:40] or dev["name"]
    save_devices()
    return {"status": "ok", "name": dev["name"]}


def touch(client_id, battery=None):
    """Отметить, что устройство только что было на связи."""
    dev = devices.get(client_id)
    if not dev:
        return None
    dev["last_seen"] = _now()
    if battery is not None:
        try:
            dev["battery"] = int(battery)
        except (TypeError, ValueError):
            pass
    return dev


def report_location(client_id, lat, lng, accuracy=None):
    dev = devices.get(client_id)
    if not dev:
        return {"status": "error", "message": "устройство не зарегистрировано"}
    try:
        dev["lat"], dev["lng"] = float(lat), float(lng)
        dev["accuracy"] = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        return {"status": "error", "message": "некорректные координаты"}
    dev["location_at"] = _now()
    dev["last_seen"] = _now()
    # Телефон — это клиент ЛУЧ, поэтому его координаты должны попасть в общую
    # геолокацию: от неё зависят геокод, маршруты, рядом и история перемещений.
    try:
        import work_fuctions
        work_fuctions.update_location(dev["lat"], dev["lng"], dev["accuracy"],
                                      source="phone:" + str(dev.get("name") or "android"))
    except Exception:
        pass
    return {"status": "ok", "lat": dev["lat"], "lng": dev["lng"]}


# ── Очередь команд ──────────────────────────────────────────────────────────

def _next_seq():
    with _seq_lock:
        _seq[0] += 1
        return _seq[0]


def enqueue(client_id, command, args=None, wait=15):
    """Отправить команду телефону. Ждёт ответа до wait секунд."""
    dev = devices.get(client_id)
    if not dev:
        return {"status": "error", "message": "устройство %s не найдено" % client_id}
    if command not in dev.get("caps", []):
        return {"status": "error",
                "message": "устройство «%s» не умеет «%s». Умеет: %s"
                           % (dev["name"], command, ", ".join(dev["caps"]) or "ничего")}
    with _seq_lock:
        item = {"seq": _next_seq(), "id": "c%d" % _seq[0], "command": command,
                "args": args or {}, "created": _now()}
        queue.setdefault(client_id, []).append(item)
        while len(queue[client_id]) > MAX_QUEUE:
            queue[client_id].pop(0)

    # Счётчик seq обязан пережить перезапуск: телефон помнит, какие команды уже
    # видел, и при откате счётчика новые команды он больше никогда не заберёт.
    save_devices()

    deadline = time.time() + max(0.5, float(wait or 15))
    while time.time() < deadline:
        got = results.get(item["id"])
        if got:
            return {"status": got.get("status", "ok"), "output": got.get("output", ""),
                    "device": dev["name"], "command": command, "waited": True}
        if not _is_online(dev, offline_after=30):
            return {"status": "error", "offline": True,
                    "message": "устройство «%s» не на связи — команда не доставлена" % dev["name"]}
        time.sleep(0.4)
    return {"status": "error", "timeout": True,
            "message": "устройство «%s» не ответило за %s с" % (dev["name"], int(wait or 15))}


def take(client_id, since=0, limit=20):
    """Забрать команды для телефона.

    since — последний seq, который телефон уже обработал. Всё, что не дождалось
    ответа (сеть моргнула, процесс убили), возвращается заново: пока команда
    висит в очереди, она важнее любого since, иначе она застрянет навсегда.
    """
    pend = queue.get(client_id) or []
    since_i = int(since or 0)
    # Телефон помнит seq больше, чем счётчик сервера, только если сервер потерял
    # состояние (файл удалён или откатился). Тогда фильтр по since отбрасывал бы
    # всё, и команды застряли бы навсегда — сбрасываем фильтр и отдаём очередь.
    reset = since_i > _seq[0]
    fresh = [c for c in pend if reset or c["seq"] > since_i][:int(limit or 20)]
    top = max([0 if reset else since_i] + [c["seq"] for c in pend])
    return {"commands": fresh, "seq": top, "reset": reset}


def submit(client_id, cmd_id, status="ok", output=""):
    """Телефон прислал результат выполненной команды."""
    results[str(cmd_id)] = {"client_id": client_id, "status": status,
                            "output": output, "at": _now()}
    with _seq_lock:
        pend = queue.get(client_id) or []
        queue[client_id] = [c for c in pend if str(c["id"]) != str(cmd_id)]
    # чистим протухшие результаты
    if len(results) > 200:
        for cid in list(results)[:-200]:
            results.pop(cid, None)
    return {"status": "ok"}


def _gc_results():
    """Вызывается периодически: удалить старые результаты."""
    now = time.time()
    for cid in list(results):
        entry = results[cid]
        try:
            age = now - datetime.fromisoformat(entry["at"]).timestamp()
        except (ValueError, TypeError, KeyError):
            age = 0
        if age > RESULT_TTL:
            results.pop(cid, None)


# ── Маршрутизация «телефон или ПК» по промту ────────────────────────────────

PHONE_WORDS = ("телефон", "телефона", "телефоне", "телефону", "смартфон", "андроид",
               "android", "мобил", "мобильн", "на телефоне", "мою телефон", "мой телефон")
PC_WORDS = ("комп", "компьютер", "компьютера", "пк", "pc", "десктоп", "ноутбук",
            "ноут", "laptop", "десктопа", "на компе", "на пк")


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е"))


def online_devices(kind=None):
    out = []
    for cid, dev in devices.items():
        if kind and dev.get("kind") != kind:
            continue
        if _is_online(dev):
            out.append(device_info(cid))
    return out


def resolve_target(target, prompt=""):
    """Определить, к чему относится просьба: телефон, ПК или оба.

    target: 'auto' | 'phone' | 'pc' | 'both' — что сказал сам ИИ.
    prompt: текст промта пользователя — по нему гадаем, если ИИ не уточнил.
    """
    t = _norm(target)
    p = _norm(prompt)

    if t in ("phone", "android", "phone_only"):
        return "phone"
    if t in ("pc", "computer", "desktop", "pc_only"):
        return "pc"
    if t == "both" or t == "все":
        return "both"

    phone_hit = any(w in p for w in PHONE_WORDS)
    pc_hit = any(w in p for w in PC_WORDS)
    if phone_hit and not pc_hit:
        return "phone"
    if pc_hit and not phone_hit:
        return "pc"
    if phone_hit and pc_hit:
        return "both"

    # промт неоднозначен: если онлайн ровно одно устройство — оно и есть
    on = online_devices()
    if len(on) == 1:
        return "phone" if on[0]["kind"] == "android" else "pc"
    return "pc"          # по умолчанию — компьютер, где работает сервер


# ── Команды для ИИ ───────────────────────────────────────────────────────────

def _one_phone():
    """Самый «свежий» онлайн-телефон: если их несколько — берём тот, кто недавно отвечал."""
    on = online_devices("android")
    if not on:
        return None
    return max(on, key=lambda d: str(d.get("last_seen") or ""))


def _status(summary):
    """Короткая сводка по устройствам — попадает в ответ ИИ."""
    if not devices:
        return {"status": "error",
                "message": "Ни одного телефона не подключено. Поставь Android-приложение LUCH "
                           "и открой его — устройство появится здесь автоматически."}
    parts = []
    for d in list_devices():
        bits = ["%s (%s)" % (d["name"], "на связи" if d["online"] else "не на связи")]
        if d.get("battery") is not None:
            bits.append("батарея %d%%" % d["battery"])
        if d.get("lat") is not None:
            bits.append("координаты есть, %s" % d.get("location_at"))
        parts.append(", ".join(bits))
    return {"status": "ok", "summary": summary + " | " + " ; ".join(parts),
            "devices": list_devices()}


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
    return _status("Батарея «%s»: %s" % (dev["name"], dev.get("battery")))


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
    """ПК сам себя объявляет, чтобы ИИ видел оба устройства в одном списке."""
    with devices_lock:
        if "pc" in devices:
            dev = devices["pc"]
            dev.update({"name": name, "last_seen": _now()})
        else:
            devices["pc"] = {
                "id": "pc", "name": name, "kind": "pc", "token": "",
                "caps": ["terminal", "system", "browser", "location"],
                "model": None, "os": "linux", "battery": None,
                "lat": None, "lng": None, "accuracy": None,
                "location_at": None, "last_seen": _now(), "created": _now(),
            }
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
