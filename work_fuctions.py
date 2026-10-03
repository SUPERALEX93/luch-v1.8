import math
import requests as http_requests
import os
import shutil
import subprocess
import sys
import threading
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from colorama import Fore, Style
from ddgs import DDGS

import device_control

# --- Связь с основным процессом и вебом ---
tts_callback = None  # Сюда main.py передаст функцию генерации голоса ИИ
web_alerts = []      # Очередь уведомлений для веб-интерфейса
_alerts_lock = threading.Lock()   # web_alerts пишется из нескольких потоков
_WEB_ALERTS_MAX = 100             # держим только свежие уведомления
last_err = None      # последняя ошибка вспомогательных действий (для диагностики)


def _push_alert(description):
    """Потокобезопасно добавить уведомление в web_alerts (не длиннее _WEB_ALERTS_MAX).

    web_alerts остаётся обычным списком: web_server.py читает его через
    .copy()/.clear() и ожидает именно такой тип.
    """
    try:
        with _alerts_lock:
            web_alerts.append({"description": description})
            if len(web_alerts) > _WEB_ALERTS_MAX:
                del web_alerts[: len(web_alerts) - _WEB_ALERTS_MAX]
    except Exception:
        pass


# --- Журнал важных событий (пишется в файл, а не в консоль) ---
LOG_DIR = Path(__file__).resolve().parent / "logs"
_log_lock = threading.Lock()
_LOG_COMPACT_MIN_BYTES = 65536  # ниже этого размера файл не сжимаем


def log_event(name, text, keep=500, alert=None):
    """Записать событие в отдельный журнал logs/<name>.log.

    Нужно для сообщений, которые не должны сыпаться в консоль и мешать меню
    (например, отказы по голосу). Если передан alert, событие дополнительно
    уходит в веб-интерфейс.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _log_lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"{name}.log"
            # Обычная работа — только дописываем строку. Полная перезапись файла
            # (сжатие до последних keep строк) выполняется редко, когда он вырос.
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {text}\n")
            try:
                limit = max(int(keep), 1)
            except (TypeError, ValueError):
                limit = 500
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size > max(limit * 200, _LOG_COMPACT_MIN_BYTES):
                lines = path.read_text(encoding="utf-8").splitlines()
                if len(lines) > limit:
                    path.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
    except Exception:
        pass
    if alert:
        _push_alert(alert)

# --- Модуль геолокации / навигатора ---
# Хранит последние координаты клиента, историю и флаг запроса позиции.
LOCATION_DEFAULT = {"lat": None, "lng": None, "accuracy": None, "timestamp": None, "source": None}
location_data = dict(LOCATION_DEFAULT)   # текущая позиция клиента (обновляется с сайта)
route_data = None                        # точка назначения: {"lat":.., "lng":.., "label":..}
location_history = []                    # история перемещений (список позиций)
location_request_pending = False         # если True — фронт должен прислать свежие координаты
_location_lock = threading.Lock()        # защищает location_data/location_history от гонок

_GEO_UA = "LUCH-AI/1.7 (voice assistant; geolocation tracker)"


def update_location(lat, lng, accuracy=None, source="web"):
    """Сохранить текущую позицию клиента. lat/lng приводятся к float.
    В историю пишется только при сдвиге > 2 м от последней точки (фильтр шума GPS)."""
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return "ERROR: invalid coordinates."
    with _location_lock:
        prev = dict(location_data)   # копия ДО обновления (иначе ссылалась бы на тот же словарь)
        location_data.update({
            "lat": lat,
            "lng": lng,
            "accuracy": float(accuracy) if accuracy is not None else None,
            "timestamp": datetime.now().isoformat(),
            "source": source,
        })
        # Шум: не спамим историю одинаковыми точками
        if prev.get("lat") is not None:
            if haversine(prev["lat"], prev["lng"], lat, lng) > 2.0:
                location_history.append(dict(location_data))
        else:
            location_history.append(dict(location_data))
        # Ограничение истории, чтобы она не росла без предела
        if len(location_history) > 5000:
            del location_history[: len(location_history) - 5000]
    return "LOCATION UPDATED"


def get_location():
    """Вернуть текущую позицию клиента словарём."""
    return {k: location_data[k] for k in location_data}


def reset_location():
    location_data.update(dict(LOCATION_DEFAULT))
    return "LOCATION RESET"


# ---- Геоинструменты для ИИ ----

def haversine(lat1, lng1, lat2, lng2):
    """Расстояние между двумя координатами в метрах (формула гаверсинусов)."""
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


# ---- Кэш и лимит частоты для Nominatim ----
# Nominatim требует не более 1 запроса в секунду, а геокодирование вызывается
# часто (get_my_location/build_route), поэтому успешные ответы кэшируем на 5 минут.
_GEO_CACHE_TTL = 300.0
_geo_cache = {}
_geo_cache_lock = threading.Lock()
_GEO_MIN_INTERVAL = 1.1
_geo_rate_lock = threading.Lock()
_geo_last_request = [0.0]


def _geo_rate_limit():
    """Выдержать паузу, чтобы не превысить лимит Nominatim (<= 1 запрос/с)."""
    with _geo_rate_lock:
        wait = _GEO_MIN_INTERVAL - (time.monotonic() - _geo_last_request[0])
        if wait > 0:
            time.sleep(wait)
        _geo_last_request[0] = time.monotonic()


def _geo_cache_get(key):
    with _geo_cache_lock:
        item = _geo_cache.get(key)
        if not item:
            return None
        ts, value = item
        if time.monotonic() - ts > _GEO_CACHE_TTL:
            _geo_cache.pop(key, None)
            return None
        return value


def _geo_cache_put(key, value):
    with _geo_cache_lock:
        _geo_cache[key] = (time.monotonic(), value)
        if len(_geo_cache) > 500:   # чистим самые старые записи
            for old_key, _ in sorted(_geo_cache.items(), key=lambda kv: kv[1][0])[:100]:
                _geo_cache.pop(old_key, None)


def geocode(query):
    """Геокодирование: текстовый адрес/место → координаты (Nominatim/OSM)."""
    cache_key = ("geocode", str(query).strip().lower())
    cached = _geo_cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        _geo_rate_limit()
        r = http_requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 3, "addressdetails": 0},
            headers={"User-Agent": _GEO_UA}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return {"status": "error", "message": f"Ничего не найдено по запросу '{query}'"}
        results = [
            {"name": d.get("display_name"), "lat": float(d["lat"]), "lng": float(d["lon"]),
             "type": d.get("type") or d.get("class")}
            for d in data[:3]
        ]
        result = {"status": "ok", "query": query, "results": results}
        _geo_cache_put(cache_key, result)
        return result
    except Exception as e:
        return {"status": "error", "message": f"Ошибка геокодирования: {e}"}


def reverse_geocode(lat, lng):
    """Обратное геокодирование: координаты → адрес (Nominatim/OSM)."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Некорректные координаты."}
    cache_key = ("reverse", round(lat, 5), round(lng, 5))
    cached = _geo_cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        _geo_rate_limit()
        r = http_requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "json", "zoom": 18, "addressdetails": 1},
            headers={"User-Agent": _GEO_UA}, timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        # Пытаемся собрать максимально точный адрес (дом, улица, город)
        addr = d.get("address") or {}
        parts = []
        for key in ("house_number", "road", "pedestrian", "suburb", "city", "town", "village", "county", "state", "country"):
            if addr.get(key):
                parts.append(addr[key])
        precise = ", ".join(parts) or (d.get("display_name") or "адрес не определён")
        result = {"status": "ok", "lat": lat, "lng": lng,
                  "address": precise, "house": addr.get("house_number"), "road": addr.get("road")}
        _geo_cache_put(cache_key, result)
        return result
    except Exception as e:
        return {"status": "error", "message": f"Ошибка обратного геокодирования: {e}"}


def request_location():
    """Запросить у клиента свежие координаты. Фронт оповестится и пришлёт их."""
    global location_request_pending
    location_request_pending = True
    return {"status": "location_requested",
            "message": "Клиенту отправлен запрос координат. Подожди немного и вызови get_my_location повторно."}


def get_my_location():
    """Текущая позиция клиента (с адресом, если удалось определить)."""
    loc = get_location()
    if loc.get("lat") is None or loc.get("lng") is None:
        return {"status": "error",
                "message": "Текущая позиция не определена. Используй request_location или попроси клиента открыть карту."}
    try:
        rev = reverse_geocode(loc["lat"], loc["lng"])
        address = rev.get("address") if rev.get("status") == "ok" else None
    except Exception:
        address = None
    return {"status": "ok", "lat": loc["lat"], "lng": loc["lng"],
            "accuracy_m": loc.get("accuracy"), "timestamp": loc.get("timestamp"),
            "address": address}


def build_route(destination):
    """Построить маршрут от текущей позиции клиента до цели (OSRM).
    Возвращает расстояние, время, полную геометрию и шаги (маневры)."""
    global route_data
    loc = get_location()
    if loc.get("lat") is None:
        return {"status": "error",
                "message": "Текущая позиция не определена. Используй request_location или попроси клиента открыть карту."}
    g = geocode(destination)
    if g.get("status") != "ok":
        return g
    d = g["results"][0]
    dlat, dlng = d["lat"], d["lng"]
    slat, slng = loc["lat"], loc["lng"]
    route_data = {"lat": dlat, "lng": dlng, "label": destination}
    try:
        r = http_requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{slng},{slat};{dlng},{dlat}",
            params={"overview": "full", "steps": "true", "geometries": "geojson", "alternatives": "false"},
            headers={"User-Agent": _GEO_UA}, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("routes"):
            return {"status": "error", "message": "OSRM не вернул маршрут."}
        route = data["routes"][0]
        geometry = route.get("geometry", {}).get("coordinates", [])
        # OSRM geometry — [[lng,lat],...], переворачиваем в [lat,lng]
        geometry = [[float(c[1]), float(c[0])] for c in geometry]
        steps = route.get("legs", [{}])[0].get("steps", [])
        return {
            "status": "ok",
            "destination": destination,
            "destination_name": d["name"],
            "destination_coords": {"lat": dlat, "lng": dlng},
            "from": {"lat": slat, "lng": slng},
            "distance_km": round(route.get("distance", 0) / 1000, 2),
            "duration_min": round(route.get("duration", 0) / 60, 1),
            "geometry": geometry,
            "steps": steps,
            "note": "Возможен только автомобильный маршрут.",
        }
    except Exception as e:
        return {"status": "error", "message": f"Ошибка построения маршрута: {e}"}


# ---- Полноценный навигатор с голосовыми подсказками ----
navigation_data = {
    "active": False,
    "destination": None,
    "geometry": [],
    "steps": [],
    "next_maneuver": None,
    "last_announced": -1,
    "early_announced": set(),
    "started_at": None,
}
# Текущий запуск навигатора: поток и ЕГО СОБСТВЕННОЕ стоп-событие.
# Отдельное событие на каждый запуск важно: раньше set()+clear() был импульсом,
# который устаревший цикл мог пропустить и продолжал озвучивать манёвры вечно.
_nav_run = {"thread": None, "stop": None}

# Словари подсказок вынесены на уровень модуля: _maneuver_text вызывается
# каждые 3 секунды на каждый активный цикл навигации.
_MANEUVER_ACTIONS = {
    "departure": "начали движение",
    "arrival": "вы прибыли в пункт назначения",
    "turn": "поверните",
    "continue": "продолжайте движение",
    "merge": "вливайтесь в поток",
    "fork": "держитесь",
    "end of road": "в конце дороги поверните",
    "roundabout": "двигайтесь по кругу",
    "roundabout turn": "по кругу поверните",
    "new name": "продолжайте движение",
    "on ramp": "выезжайте на съезд",
    "off ramp": "сверните на съезд",
    "use lane": "держите свою полосу",
    "restricted": "двигайтесь по разрешённому маршруту",
    "uturn": "развернитесь",
}
_MANEUVER_DIRS = {
    "left": "налево", "right": "направо",
    "sharp left": "резко налево", "sharp right": "резко направо",
    "slight left": "плавно налево", "slight right": "плавно направо",
    "straight": "прямо", "uturn": "развернитесь",
}


def _maneuver_text(step):
    """Человекопонятная инструкция из шага маршрута OSRM."""
    m = step.get("maneuver", {})
    typ = m.get("type")
    mod = m.get("modifier")
    name = step.get("name") or ""
    if typ == "arrival":
        return _MANEUVER_ACTIONS["arrival"]
    act = _MANEUVER_ACTIONS.get(typ, "продолжайте движение")
    direction = _MANEUVER_DIRS.get(mod, "")
    text = f"{act} {direction}".strip()
    if name and typ not in ("departure",):
        text += f" на {name}"
    return text


def _find_next_maneuver(loc_lat, loc_lng):
    """Какой шаг маршрута сейчас актуален и сколько м до его маневра."""
    steps = navigation_data.get("steps") or []
    for i in range(navigation_data["last_announced"] + 1, len(steps)):
        step = steps[i]
        m = step.get("maneuver", {})
        if m.get("type") in ("departure",):
            continue
        lat = m.get("location", [None, None])
        if not lat or lat[0] is None:
            continue
        mlat, mlng = float(lat[1]), float(lat[0])
        dist = haversine(loc_lat, loc_lng, mlat, mlng)
        return i, step, dist
    return None, None, None


def _nav_loop(stop_evt):
    """Фоновый цикл навигатора: следит за позицией и озвучивает манёвры.

    stop_evt — событие ИМЕННО ЭТОГО запуска: цикл продолжается, пока не взведено
    его собственное событие, поэтому устаревшие циклы гарантированно завершаются.
    """
    while not stop_evt.wait(3):
        loc = get_location()
        if loc.get("lat") is None:
            continue
        steps = navigation_data.get("steps") or []
        if not steps:
            continue
        i, step, dist = _find_next_maneuver(loc["lat"], loc["lng"])
        if step is None:
            continue
        m = step.get("maneuver", {})
        typ = m.get("type", "")

        # Конец маршрута: пункт назначения рядом
        if typ == "arrival" and dist < 80:
            if navigation_data["last_announced"] < i:
                navigation_data["last_announced"] = i
                text = _maneuver_text(step)
                if tts_callback:
                    tts_callback(text, "/tmp/nav_arrival.wav")
                navigation_data["next_maneuver"] = None
            continue

        # Раннее предупреждение за ~300 м (один раз)
        if 90 <= dist <= 300 and i not in navigation_data["early_announced"]:
            navigation_data["early_announced"].add(i)
            text = f"Через {int(round(dist / 10) * 10)} метров {_maneuver_text(step)}"
            navigation_data["next_maneuver"] = {"text": text, "distance_m": int(dist), "index": i}
            if tts_callback and typ not in ("arrival",):
                tts_callback(text, "/tmp/nav_early.wav")

        # Непосредственно манёвр: подходим ближе 70 м
        if dist < 70 and navigation_data["last_announced"] < i:
            navigation_data["last_announced"] = i
            text = _maneuver_text(step)
            navigation_data["next_maneuver"] = {"text": text, "distance_m": int(dist), "index": i}
            if tts_callback and typ not in ("arrival",):
                tts_callback(text, "/tmp/nav_turn.wav")


def start_navigation(destination):
    """Полноценный навигатор: строит маршрут и голосом ведёт по нему."""
    global navigation_data
    route = build_route(destination)
    if route.get("status") != "ok":
        return route
    # Останавливаем предыдущую навигацию: взводим ЕЁ событие и ждём завершения,
    # иначе старый цикл продолжит жить и дублировать голосовые подсказки.
    prev_stop = _nav_run.get("stop")
    prev_thread = _nav_run.get("thread")
    if prev_stop is not None:
        prev_stop.set()
    if prev_thread is not None and prev_thread.is_alive():
        prev_thread.join(timeout=3.5)
    stop_evt = threading.Event()
    _nav_run["stop"] = stop_evt
    navigation_data.update({
        "active": True,
        "destination": destination,
        "destination_coords": route["destination_coords"],
        "geometry": route["geometry"],
        "steps": route["steps"],
        "next_maneuver": None,
        "last_announced": -1,
        "early_announced": set(),
        "started_at": datetime.now().isoformat(),
    })
    t = threading.Thread(target=_nav_loop, args=(stop_evt,), daemon=True)
    _nav_run["thread"] = t
    t.start()
    first = _next_maneuver_summary()
    return {
        "status": "navigation_started",
        "destination": destination,
        "destination_coords": route["destination_coords"],
        "distance_km": route["distance_km"],
        "duration_min": route["duration_min"],
        "geometry": route["geometry"],
        "next_maneuver": first,
        "note": "Навигация активна: ЛУЧ будет голосом подсказывать повороты.",
    }


def _next_maneuver_summary():
    """Текущая информация о следующем маневре для статуса/карты."""
    if not navigation_data.get("geometry"):
        return None
    m = navigation_data.get("next_maneuver")
    if m:
        return m
    if navigation_data.get("steps"):
        # первый значимый манёвр маршрута
        for i, s in enumerate(navigation_data["steps"]):
            mm = s.get("maneuver", {})
            if mm.get("type") not in ("departure",):
                loc = mm.get("location", [None, None])
                if loc[0] is not None:
                    return {"text": _maneuver_text(s), "distance_m": None, "index": i}
    return None


def stop_navigation():
    """Остановить голосовую навигацию."""
    stop_evt = _nav_run.get("stop")
    if stop_evt is not None:
        stop_evt.set()
    navigation_data.update({"active": False, "next_maneuver": None})
    return {"status": "navigation_stopped"}


def navigation_status():
    """Текущее состояние навигатора (для веб-карты и ИИ)."""
    loc = get_location()
    current = None
    remaining_m = None
    if navigation_data.get("active") and loc.get("lat") is not None:
        steps = navigation_data.get("steps") or []
        for i in range(navigation_data["last_announced"] + 1, len(steps)):
            m = steps[i].get("maneuver", {})
            lat = m.get("location", [None, None])
            if lat[0] is None:
                continue
            mlat, mlng = float(lat[1]), float(lat[0])
            dist = haversine(loc["lat"], loc["lng"], mlat, mlng)
            remaining_m = int(dist)
            current = {"text": _maneuver_text(steps[i]), "distance_m": int(dist)}
            break
    return {
        "active": navigation_data.get("active", False),
        "destination": navigation_data.get("destination"),
        "destination_coords": navigation_data.get("destination_coords"),
        "geometry": navigation_data.get("geometry", []),
        "next_maneuver": navigation_data.get("next_maneuver") or current,
        "remaining_distance_m": remaining_m,
        "current_location": {"lat": loc.get("lat"), "lng": loc.get("lng")} if loc.get("lat") is not None else None,
    }


def distance_to(lat, lng):
    """Расстояние от текущей позиции клиента до указанной точки (по прямой)."""
    loc = get_location()
    if loc.get("lat") is None:
        return {"status": "error", "message": "Текущая позиция не определена."}
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Некорректные координаты."}
    meters = haversine(loc["lat"], loc["lng"], lat, lng)
    return {"status": "ok",
            "from": {"lat": loc["lat"], "lng": loc["lng"]},
            "to": {"lat": lat, "lng": lng},
            "distance_m": round(meters),
            "distance_km": round(meters / 1000, 2)}


def _overpass_query(query_body, timeout=25):
    """Выполнить запрос к Overpass API, перебирая несколько зеркал."""
    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ]
    last_err = None
    for mirror in mirrors:
        try:
            r = http_requests.post(mirror, data={"data": query_body},
                                   headers={"User-Agent": _GEO_UA}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    # Нельзя raise None: если ни одно зеркало не ответило, поднимаем реальное исключение
    if last_err is not None:
        raise last_err
    raise RuntimeError("Overpass API недоступен")


def nearby(what, radius=2000):
    """Найти места рядом с клиентом по названию (Overpass API/OSM)."""
    loc = get_location()
    if loc.get("lat") is None:
        return {"status": "error", "message": "Текущая позиция не определена."}
    try:
        radius = max(100, min(int(radius), 20000))
    except (TypeError, ValueError):
        radius = 2000
    # Режем слишком длинный запрос и экранируем спецсимволы Overpass QL
    # (обратный слэш, кавычки, переводы строк), чтобы нельзя было сломать запрос.
    what_text = str(what).strip()[:200]
    if not what_text:
        return {"status": "error", "message": "Пустой запрос поиска рядом."}
    escaped = (what_text.replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", " ")
                        .replace("\r", " "))
    q = (
        f'[out:json][timeout:20];'
        f'(node["name"~"{escaped}",i](around:{radius},{loc["lat"]},{loc["lng"]});'
        f'way["name"~"{escaped}",i](around:{radius},{loc["lat"]},{loc["lng"]});'
        f'rel["name"~"{escaped}",i](around:{radius},{loc["lat"]},{loc["lng"]}););'
        f'out center 12;'
    )
    try:
        data = _overpass_query(q)
        elems = data.get("elements", [])
    except Exception:
        # Фолбэк: ищем через Nominatim в квадрате вокруг точки
        try:
            dlat = radius / 111000.0
            dlng = radius / (111000.0 * math.cos(math.radians(loc["lat"])))
            _geo_rate_limit()
            r = http_requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": what_text, "format": "json", "limit": 8, "bounded": 1,
                        "viewbox": f"{loc['lng']-dlng},{loc['lat']-dlat},{loc['lng']+dlng},{loc['lat']+dlat}"},
                headers={"User-Agent": _GEO_UA}, timeout=20,
            )
            r.raise_for_status()
            elems = []
            for item in r.json():
                elems.append({
                    "lat": float(item["lat"]), "lon": float(item["lon"]),
                    "tags": {"name": item.get("display_name", what_text).split(",")[0],
                             "amenity": item.get("type") or "place"},
                    "dist_m": haversine(loc["lat"], loc["lng"], float(item["lat"]), float(item["lon"])),
                })
        except Exception as e:
            return {"status": "error", "message": f"Ошибка поиска рядом: {e}"}

    if not elems:
        return {"status": "error",
                "message": f"Не найдено мест по запросу '{what_text}' в радиусе {radius} м."}
    spots = []
    for el in elems:
        lat = el.get("lat")
        lng = el.get("lon")
        if lat is None and "center" in el:
            lat = el["center"].get("lat"); lng = el["center"].get("lon")
        if lat is None or lng is None:
            continue
        t = el.get("tags", {})
        metric = el.get("dist_m")
        if metric is None:
            metric = haversine(loc["lat"], loc["lng"], lat, lng)
        spots.append({
            "name": t.get("name") or what_text,
            "category": t.get("amenity") or t.get("shop") or t.get("tourism") or t.get("leisure") or "place",
            "distance_m": round(metric),
            "lat": lat, "lng": lng,
        })
    spots = [s for s in spots if s["name"].lower() != what_text.lower()]
    spots = sorted(spots, key=lambda s: s["distance_m"])[:8]
    if not spots:
        return {"status": "error", "message": f"Рядом с тобой нет мест с именем '{what_text}'."}
    return {"status": "ok", "query": what_text, "radius_m": radius,
            "center": {"lat": loc["lat"], "lng": loc["lng"]}, "spots": spots}


def analyze_movement():
    """Анализ истории перемещений: трек, длина пути, диапазон времени, текущая точка."""
    # Снимок под замком: update_location может менять и обрезать историю прямо
    # во время обхода, а fixes = location_history был бы лишь псевдонимом списка.
    with _location_lock:
        fixes = [dict(p) for p in location_history]
    if not fixes or fixes[-1].get("lat") is None:
        return {"status": "error",
                "message": "История перемещений пуста. Клиент ещё ни разу не присылал координаты (нужна открытая карта)."}
    total = 0.0
    prev = None
    unique_pts = 0
    prev_bucket = None
    for p in fixes:
        if p.get("lat") is None:
            prev = None; prev_bucket = None; continue
        cur_bucket = (round(p["lat"], 4), round(p["lng"], 4))
        if cur_bucket != prev_bucket:
            unique_pts += 1
            prev_bucket = cur_bucket
        if prev is not None:
            total += haversine(prev["lat"], prev["lng"], p["lat"], p["lng"])
        prev = p
    return {
        "status": "ok",
        "points_count": len([p for p in fixes if p.get("lat") is not None]),
        "unique_spots": unique_pts,
        "track_length_km": round(total / 1000, 2),
        "first_fix": fixes[0].get("timestamp"),
        "last_fix": fixes[-1].get("timestamp"),
        "current": {"lat": location_data["lat"], "lng": location_data["lng"],
                    "accuracy_m": location_data.get("accuracy")},
    }


def navigator(destination):
    """Полноценный навигатор: маршрут + голосовые подсказки поворотов."""
    return start_navigation(destination)


def stop_navigator():
    """Остановить активную голосовую навигацию."""
    return stop_navigation()


def navigator_status():
    """Текущее состояние навигатора (для голосового запроса)."""
    return navigation_status()


# navigator / stop_navigator / navigator_status — публичные алиасы функций
# start_navigation / stop_navigation / navigation_status (оставлены для совместимости).


def web_search(request):
    with DDGS() as ddgs:
        return list(ddgs.text(request, max_results=10))

def terminal(command, timeout=60):
    if not command or not command.strip():
        return ""
    try:
        # shell=True нужен намеренно: пользователь просит произвольные команды.
        # encoding/errors фиксируем явно — при locale-декодировании русский вывод
        # (CPython 3.14) иначе может упасть с UnicodeDecodeError.
        result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"ОШИБКА: команда превысила лимит времени {timeout} секунд."
    except Exception as e:
        return f"ОШИБКА: {e}"
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        combined = (out + "\n" + err).strip()
        return combined or f"ОШИБКА: команда завершилась с кодом {result.returncode}"
    return out or ""

def exit():
    # Имя намеренно совпадает с main.py (commands_ai["exit"]) — не переименовывать.
    print(Fore.GREEN + "EXIT" + Style.RESET_ALL)
    sys.exit(0)

def _current_desktop():
    """Что сообщает окружение о текущем DE: kde, gnome, hyprland, sway..."""
    def detect(raw):
        raw = (raw or "").lower()
        if "kde" in raw or "plasma" in raw:
            return "kde"
        if "hypr" in raw:
            return "hyprland"
        if "sway" in raw:
            return "sway"
        if "gnome" in raw or "unity" in raw:
            return "gnome"
        if "xfce" in raw:
            return "xfce"
        if "cinnamon" in raw:
            return "cinnamon"
        if "lxqt" in raw:
            return "lxqt"
        return ""

    # XDG_CURRENT_DESKTOP важнее: DESKTOP_SESSION иногда остаётся от прежней сессии
    found = detect(os.environ.get("XDG_CURRENT_DESKTOP")) or \
        detect(os.environ.get("DESKTOP_SESSION"))
    if found:
        return found
    if os.environ.get("KDE_FULL_SESSION"):
        return "kde"
    return "other"


def _run_quiet(cmd, timeout=8):
    """Запустить команду и вернуть (returncode, вывод). Ничего не печатает."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, start_new_session=True)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "не найдено"
    except subprocess.TimeoutExpired:
        return 124, "команда не ответила"
    except Exception as exc:
        return 1, str(exc)


def _qdbus_tools():
    """Все qdbus-варианты, которые стоит пробовать (Qt6, Qt5, унаследованный)."""
    return [t for t in ("qdbus6", "qdbus-qt6", "qdbus") if shutil.which(t)]


def _session_is_wayland():
    """True, если сессия Wayland (hyprlock/swaylock без Wayland не работают)."""
    kind = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if kind:
        return kind == "wayland"
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


def _lock_in_background(cmd):
    """Запустить блокировщик, который держит экран до разблокировки.

    hyprlock/swaylock не возвращают управление, поэтому запускаем их отдельной
    сессией и проверяем, что процесс действительно жив: упавший процесс иначе
    выглядел бы как успешная блокировка.
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception:
        return None
    for _ in range(6):
        time.sleep(0.1)
        if proc.poll() is not None:
            return None
    return proc


def _lock_kde_plasma():
    """Блокировка экрана в KDE Plasma.

    В Plasma 6 классический org.kde.ScreenSaver живёт только если установлен
    kscreenlocker, поэтому сначала пробуем совместимый freedesktop-интерфейс,
    который регистрирует сам Plasma.
    """
    for tool in _qdbus_tools():
        for service, path in (("org.kde.ScreenSaver", "/ScreenSaver"),
                              ("org.kde.screensaver", "/org/kde/screensaver"),
                              ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver")):
            code, out = _run_quiet([tool, service, path], timeout=5)
            # если объекта/сервиса нет — qdbus пишет об этом и выходит с != 0
            if code != 0:
                continue
            code, out = _run_quiet([tool, service, path, "Lock"], timeout=5)
            if code == 0:
                return f"{tool} {service} Lock"
    return None


def lock_pc():
    """Заблокировать экран. Поддерживает KDE Plasma, GNOME, Hyprland, Sway, XFCE.

    Раньше вызывался только hyprlock и без проверки результата, поэтому на Plasma
    команда молча падала, а агент всё равно отвечал «SUCCESS».
    """
    desktop = _current_desktop()
    wayland = _session_is_wayland()
    errors = []
    desktop_name = {"kde": "KDE Plasma", "gnome": "GNOME", "hyprland": "Hyprland",
                    "sway": "Sway", "xfce": "XFCE", "cinnamon": "Cinnamon",
                    "lxqt": "LXQt"}.get(desktop, desktop)

    def ok(how):
        print(Fore.GREEN + "Экран заблокирован (" + how + ")" + Style.RESET_ALL)
        log_event("lock_pc", "экран заблокирован: " + how)
        return "ЭКРАН ЗАБЛОКИРОВАН: " + how

    def try_cmd(cmd, label):
        """Вернуть сообщение об успехе или пустую строку — тип всегда str."""
        if not shutil.which(cmd[0]):
            return ""
        code, out = _run_quiet(cmd, timeout=8)
        if code == 0:
            return ok(label)
        errors.append(f"{label}: код {code}" + (f" ({out[:120]})" if out else ""))
        return ""

    def try_background(cmd, label):
        # hyprlock/swaylock держат экран заблокированным до ввода пароля,
        # запускаем их отдельной сессией, чтобы не подвесить ассистента
        if not shutil.which(cmd[0]):
            return False
        if _lock_in_background(cmd) is not None:
            return ok(label)
        errors.append(f"{label}: процесс сразу завершился (нужна Wayland-сессия)")
        return False

    # 1. Способ, родной для текущего окружения
    if desktop == "kde":
        result = _lock_kde_plasma()
        if result:
            return ok(result)
        errors.append("Plasma D-Bus: сервис блокировки недоступен")
    elif desktop == "gnome":
        msg = try_cmd(["gnome-screensaver-command", "-l"], "gnome-screensaver-command")
        if msg:
            return msg
    elif desktop == "hyprland":
        if try_background(["hyprlock"], "hyprlock"):
            return "ЭКРАН ЗАБЛОКИРОВАН: hyprlock"
    elif desktop == "sway":
        if try_background(["swaylock"], "swaylock"):
            return "ЭКРАН ЗАБЛОКИРОВАН: swaylock"
    elif desktop == "xfce":
        msg = try_cmd(["xflock4"], "xflock4")
        if msg:
            return msg

    # 2. Универсальные способы
    msg = try_cmd(["loginctl", "lock-session"], "loginctl lock-session")
    if msg:
        return msg
    if desktop != "kde":
        for tool in _qdbus_tools():
            msg = try_cmd([tool, "org.freedesktop.ScreenSaver",
                           "/org/freedesktop/ScreenSaver", "Lock"], f"{tool} ScreenSaver")
            if msg:
                return msg
    msg = try_cmd(["xdg-screensaver", "lock"], "xdg-screensaver")
    if msg:
        return msg

    # 3. Блокировщики compositor'а — только на Wayland, иначе они падают
    if wayland:
        if try_background(["hyprlock"], "hyprlock"):
            return "ЭКРАН ЗАБЛОКИРОВАН: hyprlock"
        if try_background(["swaylock"], "swaylock"):
            return "ЭКРАН ЗАБЛОКИРОВАН: swaylock"

    print(Fore.RED + "Заблокировать экран не удалось" + Style.RESET_ALL)
    detail = "; ".join(errors) if errors else "нет подходящей команды"
    log_event("lock_pc", "не удалось заблокировать экран: " + detail)
    return ("НЕ УДАЛОСЬ ЗАБЛОКИРОВАТЬ ЭКРАН. Окружение: %s (%s). Проблемы: %s"
            % (desktop_name, "wayland" if wayland else "x11", detail))


def print_text(text):
    """Напечатать текст через wtype. Возвращает результат, а не безусловный успех."""
    global last_err
    try:
        proc = subprocess.run(['wtype', text], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15)
    except FileNotFoundError:
        last_err = "wtype не найден"
        return "ОШИБКА: утилита wtype не найдена (установите wtype)."
    except subprocess.TimeoutExpired:
        last_err = "wtype не ответил"
        return "ОШИБКА: печать текста превысила лимит времени 15 секунд."
    except Exception as e:
        last_err = str(e)
        return f"ОШИБКА: {e}"
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        last_err = detail or f"код {proc.returncode}"
        return ("ОШИБКА: не удалось напечатать текст"
                + (f" ({detail[:200]})" if detail else f" (код {proc.returncode})"))
    last_err = None
    return "TEXT PRINTED SUCCESSFULLY"

# ---- ТАЙМЕР ----
# Реестр активных таймеров: позволяет отменить/заменить таймер с тем же описанием
# и не копить потоки, которые никто не отслеживает.
_TIMER_RE = re.compile(r'^\+?(\d+)([smh])$')
_active_timers = {}
_timers_lock = threading.Lock()


def _register_timer(description, timer):
    """Заменить прежний таймер с тем же описанием и запомнить новый."""
    with _timers_lock:
        old = _active_timers.get(description)
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        _active_timers[description] = timer


def timer_alert(description):
    print(Fore.RED + f"\n[АЛАРМ!] Сработал таймер: {description}" + Style.RESET_ALL)
    with _timers_lock:
        _active_timers.pop(description, None)

    try:
        subprocess.run(["notify-send", "-u", "critical", "-t", "10000", "LUCH AI ⏰", description])
    except Exception:
        pass
    
    # 2. Генерируем голос ИИ и воспроизводим на ПК локально
    audio_path = "/tmp/timer_alert.wav"
    if tts_callback:
        tts_callback(description, audio_path)
        
    # 3. Передаем сигнал для веб-интерфейса
    _push_alert(description)

def set_timer(timer_type, time_val, description):
    now = datetime.now()
    delay_seconds = 0
    
    if timer_type == "absolute":
        try:
            target_time = datetime.strptime(time_val, "%H:%M").time()
            target_dt = datetime.combine(now.date(), target_time)
            if target_dt < now:
                target_dt += timedelta(days=1)
            delay_seconds = (target_dt - now).total_seconds()
        except ValueError:
            return "ERROR: invalid absolute time format."
    elif timer_type == "relative":
        match = _TIMER_RE.match(time_val.strip().lower())
        if match:
            val = int(match.group(1))
            unit = match.group(2)
            if unit == 's': delay_seconds = val
            elif unit == 'm': delay_seconds = val * 60
            elif unit == 'h': delay_seconds = val * 3600
        else:
            return "ERROR: invalid relative time format."
    else:
        return "ERROR: invalid timer_type."

    t = threading.Timer(delay_seconds, timer_alert, args=[description])
    t.daemon = True
    _register_timer(description, t)
    t.start()
    
    return f"TIMER SET SUCCESSFULLY FOR {time_val}"

commands_ai = {
    # "dangerous": True — команду ИИ нужно подтвердить у пользователя (гейт в main.py).
    "terminal": {"func": terminal, "icon": "🖥️", "dangerous": True},
    "web_search": {"func": web_search, "icon": "🌐"},
    "lock_pc": {"func": lock_pc, "icon": "🔒", "dangerous": True},
    "print_text": {"func": print_text, "icon": "✍️"},
    "set_timer": {"func": set_timer, "icon": "⏰"},
    "navigator": {"func": navigator, "icon": "🗺️"},
    "stop_navigator": {"func": stop_navigator, "icon": "⏹️"},
    "navigator_status": {"func": navigator_status, "icon": "🧭"},
    "get_my_location": {"func": get_my_location, "icon": "📍"},
    "request_location": {"func": request_location, "icon": "📡"},
    "geocode": {"func": geocode, "icon": "🔎"},
    "reverse_geocode": {"func": reverse_geocode, "icon": "🧭"},
    "build_route": {"func": build_route, "icon": "🛣️"},
    "distance_to": {"func": distance_to, "icon": "📏"},
    "nearby": {"func": nearby, "icon": "📌"},
    "analyze_movement": {"func": analyze_movement, "icon": "📊"},
    "devices_list": {"func": device_control.devices_list, "icon": "📱"},
    "phone_notify": {"func": device_control.phone_notify, "icon": "🔔"},
    "phone_vibrate": {"func": device_control.phone_vibrate, "icon": "📳"},
    "phone_ring": {"func": device_control.phone_ring, "icon": "🔊"},
    "phone_open_url": {"func": device_control.phone_open_url, "icon": "🔗"},
    "phone_battery": {"func": device_control.phone_battery, "icon": "🔋"},
    "phone_location": {"func": device_control.phone_location, "icon": "📍"},
    # управление телефоном
    "phone_status": {"func": device_control.phone_status, "icon": "📱"},
    "phone_wake": {"func": device_control.phone_wake, "icon": "😴"},
    "phone_torch": {"func": device_control.phone_torch, "icon": "🔦"},
    "phone_volume": {"func": device_control.phone_volume, "icon": "🔉"},
    "phone_brightness": {"func": device_control.phone_brightness, "icon": "☀️"},
    "phone_open_app": {"func": device_control.phone_open_app, "icon": "🚀"},
    "phone_apps": {"func": device_control.phone_apps, "icon": "📋"},
    "phone_clipboard": {"func": device_control.phone_clipboard, "icon": "📋"},
    "phone_dial": {"func": device_control.phone_dial, "icon": "📞"},
    "phone_share": {"func": device_control.phone_share, "icon": "📤"},
    "phone_toast": {"func": device_control.phone_toast, "icon": "💬"},
    "phone_open_settings": {"func": device_control.phone_open_settings, "icon": "⚙️"},
    "phone_stop_ring": {"func": device_control.phone_stop_ring, "icon": "🤫"},
}

commands_ai_and_args = """
    УПРАВЛЕНИЕ ТЕЛЕФОНОМ. Приложение LUCH должно быть открыто, иначе команды не выполнятся.
    0. phone_status - состояние телефона: модель, Android, заряд, заряжается ли.
    Пример: {"command": "phone_status", "args": {}}.

    0a. phone_wake - разбудить экран телефона и открыть приложение.
    Пример: {"command": "phone_wake", "args": {}}.

    0b. phone_torch - фонарик. "on": true включить, false выключить.
    Пример: {"command": "phone_torch", "args": {"on": true}}.

    0c. phone_volume - громкость. "action": up громче, down тише, mute выключить,
    set с уровнем "level" 0-100. "stream": music, alarm, ring, system.
    Пример 1: {"command": "phone_volume", "args": {"action": "down"}}.
    Пример 2: {"command": "phone_volume", "args": {"action": "set", "level": 30}}.

    0d. phone_brightness - яркость экрана 0-100.
    Пример: {"command": "phone_brightness", "args": {"level": 100}}.

    0e. phone_open_app - запустить приложение по названию: Камера, Телефон, Настройки.
    Если не помнишь название, сначала phone_apps с фильтром.
    Пример: {"command": "phone_open_app", "args": {"name": "Камера"}}.

    0f. phone_apps - список приложений на телефоне, "filter" — часть названия.
    Пример: {"command": "phone_apps", "args": {"filter": "кар"}}.

    0g. phone_clipboard - положить текст в буфер обмена; "get": true прочитать его.
    Android может не дать доступ, если приложение свёрнуто.
    Пример 1: {"command": "phone_clipboard", "args": {"text": "текст"}}.
    Пример 2: {"command": "phone_clipboard", "args": {"get": true}}.

    0h. phone_dial - открыть номер в звонилке. Звонок НЕ совершается.
    Пример: {"command": "phone_dial", "args": {"number": "+79161234567"}}.

    0i. phone_share - предложить отправить текст с телефона.
    Пример: {"command": "phone_share", "args": {"text": "Я выезжаю", "title": "Сообщение"}}.

    0j. phone_toast - короткое всплывающее сообщение на экране.
    Пример: {"command": "phone_toast", "args": {"text": "Не трогай плиту"}}.

    0k. phone_open_settings - открыть настройки: wifi, bluetooth, apps, display, sound,
    home, storage. Без параметра открывает общие настройки.
    Пример: {"command": "phone_open_settings", "args": {"what": "wifi"}}.

    0l. phone_stop_ring - прекратить сигнал, который телефон издаёт по phone_ring.
    Пример: {"command": "phone_stop_ring", "args": {}}.

    1. terminal - выполнить команду в терминале.
    Пример: {"command": "terminal", "args": {"command": "ls"}}. 
    
    2. web_search - использовать веб поиск по запросу.
    Пример: {"command": "web_search", "args": {"request": "что такое ии"}}. 
    
    3. lock_pc - заблокировать экран компьютера. Аргументы не нужны. Поддерживает
    KDE Plasma, GNOME, Hyprland, Sway, XFCE (определяет окружение сама).
    Возвращает "ЭКРАН ЗАБЛОКИРОВАН: <чем>" или "НЕ УДАЛОСЬ ЗАБЛОКИРОВАТЬ ЭКРАН".
    Если вернулась ошибка - сообщи пользователю причину, не утверждай, что
    компьютер заблокирован.
    Пример: {"command": "lock_pc", "args": {}}. 
    
    4. print_text - сэмитировать печать текста с клавиатуры.
    Пример: {"command": "print_text", "args": {"text":"тут текст"}}. 
    
    5. set_timer - установить таймер. Обязательно укажи описание!
    Пример 1: {"command": "set_timer", "args": {"timer_type": "relative", "time_val": "10m", "description": "Пора выключать плиту"}}. Доступны 's', 'm', 'h'.
    Пример 2: {"command": "set_timer", "args": {"timer_type": "absolute", "time_val": "18:30", "description": "Начинается важный созвон"}}. Указывается в формате HH:MM.

    ГЕОЛОКАЦИЯ И НАВИГАЦИЯ (клиент может присылать свои координаты с телефона/браузера):
    «текущая позиция» — это последние координаты, которые прислал клиент. Если они не определены, сначала вызови request_location.

    6. request_location - запросить у клиента свежие координаты (нужно, если текущая позиция пуста). Затем подожди и повтори get_my_location.
    Пример: {"command": "request_location", "args": {}}.

    7. get_my_location - узнать текущую позицию клиента (координаты, адрес, точность).
    Пример: {"command": "get_my_location", "args": {}}.

    8. geocode - преобразовать текстовый адрес или название места в координаты.
    Пример: {"command": "geocode", "args": {"query": "Москва, Красная площадь"}}.

    9. reverse_geocode - узнать адрес по координатам.
    Пример: {"command": "reverse_geocode", "args": {"lat": 55.7558, "lng": 37.6173}}.

    10. build_route - построить маршрут от текущей позиции клиента до цели (автомобильный, через OSRM). Вернёт расстояние, время, геометрию и список маневров.
    Пример: {"command": "build_route", "args": {"destination": "Казань, Кремль"}}.

    11. navigator - включить полноценный навигатор до цели: ЛУЧ голосом подсказывает повороты по мере движения (дистанции, маневры).
    Пример: {"command": "navigator", "args": {"destination": "Казань, Кремль"}}.
    Вернёт: расстояние, время и первый маневр; дальше ведёт голосом в фоне.

    12. stop_navigator - остановить активную голосовую навигацию. Аргументы не нужны.
    Пример: {"command": "stop_navigator", "args": {}}.

    13. navigator_status - узнать текущее состояние навигатора (активен ли, следующий маневр, остаток пути).
    Пример: {"command": "navigator_status", "args": {}}.

    14. distance_to - расстояние по прямой от текущей позиции клиента до точки.
    Пример: {"command": "distance_to", "args": {"lat": 55.7558, "lng": 37.6173}}.

    15. nearby - найти места (заведения, магазины и т.п.) рядом с клиентом по названию. Аргумент "radius" — радиус в метрах (по умолчанию 2000).
    Пример: {"command": "nearby", "args": {"what": "кофейня", "radius": 3000}}.

    16. analyze_movement - проанализировать историю перемещений клиента: сколько точек и уникальных мест, длина трека, временной диапазон.
    Пример: {"command": "analyze_movement", "args": {}}.
""" + device_control.COMMANDS_HELP
