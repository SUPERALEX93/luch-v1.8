#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скачивает модель проверки голоса для ЛУЧ.

Веса не хранятся в git — репозиторий весит десятки мегабайт, и держать
в нём модель на 90 МБ неправильно. Поэтому при первом запуске запускай:

    python fetch_models.py

Скрипт кладёт файлы в pretrained_models/spkrec/. Если интернет недоступен
или HuggingFace отвечает отказом, попробует запасное зеркало, а если и оно
не помогло — скажет, что делать дальше. Повторный запуск ничего не ломает:
уже скачанные файлы пропускаются.
"""

import os
import shutil
import sys
import urllib.request
import zipfile

REPO_ID = "microsoft/spkrec-ecapa-voxceleb"
FILES = (
    "hyperparams.yaml",
    "embedding_model.ckpt",
    "classifier.ckpt",
    "label_encoder.ckpt",
    "mean_var_norm_emb.ckpt",
)
# Минимальный размер, при котором файл считается недокачанным (1 КБ).
MIN_SIZE = 1024

# Готовый архив в релизе репозитория — самый надёжный источник, он не зависит
# от чужих сервисов. HuggingFace держим запасным: он регулярно отвечает 401
# на анонимные запросы, и на нём запуск падал.
ARCHIVES = (
    "https://github.com/SUPERALEX93/luch-v1.8/releases/download/v1.8/spkrec.zip",
)

MIRRORS = (
    "https://huggingface.co/{repo}/resolve/main/{name}",
    "https://hf-mirror.com/{repo}/resolve/main/{name}",
)

DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "pretrained_models", "spkrec")

# GitHub и HuggingFace отклоняют запросы без User-Agent, поэтому headers
# обязателен — иначе скачивание падает с 403/401 без внятной причины.
HEADERS = {"User-Agent": "luch-fetch-models/1.0 (+https://github.com/SUPERALEX93/luch-v1.8)"}


def open_url(url, timeout):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=HEADERS), timeout=timeout)


def human(n):
    return f"{n / 1048576:.1f} МБ" if n >= 1048576 else f"{n / 1024:.0f} КБ"


def already_have(name):
    path = os.path.join(DEST, name)
    if os.path.isfile(path) and os.path.getsize(path) >= MIN_SIZE:
        return True
    # Недокачанный хвост от прошлой попытки не нужен.
    if os.path.exists(path):
        os.remove(path)
    return False


def borrow_from_sibling():
    """Модель могла остаться в соседней версии — копируем оттуда."""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    for name in sorted(os.listdir(parent)):
        src = os.path.join(parent, name, "pretrained_models", "spkrec")
        if os.path.isfile(os.path.join(src, "hyperparams.yaml")):
            try:
                os.makedirs(DEST, exist_ok=True)
                for f in FILES:
                    if not already_have(f):
                        shutil.copy2(os.path.join(src, f), os.path.join(DEST, f))
                print(f"Модель скопирована из {src}")
                return True
            except OSError:
                continue
    return False


def fetch(name):
    """Качает один файл, перебирая зеркала. True — файл на месте."""
    tmp = os.path.join(DEST, name + ".part")
    for template in MIRRORS:
        url = template.format(repo=REPO_ID, name=name)
        try:
            print(f"  {name} <- {url.split('/')[2]} ... ", end="", flush=True)
            with open_url(url, 60) as r, \
                    open(tmp, "wb") as out:
                shutil.copyfileobj(r, out)
            if os.path.getsize(tmp) < MIN_SIZE:
                raise OSError("файл подозрительно маленький")
            os.replace(tmp, os.path.join(DEST, name))
            print(f"готово ({human(os.path.getsize(os.path.join(DEST, name)))})")
            return True
        except Exception as e:
            print(f"не вышло ({type(e).__name__})")
            if os.path.exists(tmp):
                os.remove(tmp)
    return False


def missing_files():
    return [f for f in FILES if not already_have(f)]


def fetch_archive():
    """Забирает готовый архив из релиза на GitHub. True — модель на месте."""
    os.makedirs(DEST, exist_ok=True)
    for url in ARCHIVES:
        zip_path = os.path.join(DEST, "_spkrec.zip.part")
        try:
            print(f"Качаю архив с {url.split('/')[2]} ... ", end="", flush=True)
            with open_url(url, 300) as r, \
                    open(zip_path, "wb") as out:
                shutil.copyfileobj(r, out)
            with zipfile.ZipFile(zip_path) as z:
                for name in z.namelist():
                    base = os.path.basename(name)
                    if base in FILES:
                        with z.open(name) as src, \
                                open(os.path.join(DEST, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
            os.remove(zip_path)
            if not missing_files():
                print("готово")
                return True
            print("в архиве не хватает файлов")
        except Exception as e:
            print(f"не вышло ({type(e).__name__})")
            if os.path.exists(zip_path):
                os.remove(zip_path)
    return False


def main():
    os.makedirs(DEST, exist_ok=True)
    missing = missing_files()

    if not missing:
        print(f"Модель уже на месте: {DEST}")
        return 0

    print(f"Модель проверки голоса не найдена, качаю в {DEST}")
    if borrow_from_sibling() and not missing_files():
        print("Готово.")
        return 0

    if fetch_archive() and not missing_files():
        print("Готово. Можно запускать python main.py")
        return 0

    missing = missing_files()
    failed = [f for f in missing if not fetch(f)]

    if failed:
        print()
        print("Не удалось скачать:", ", ".join(failed))
        print()
        print("Что делать:")
        print("  1. Проверь интернет")
        print("  2. Скачай вручную с")
        print(f"     https://huggingface.co/{REPO_ID}/files")
        print(f"     и положи файлы в {DEST}")
        print("  3. Либо скачай архив spkrec.zip из релизов")
        print("     https://github.com/SUPERALEX93/luch-v1.8/releases")
        print("     и распакуй его в pretrained_models/spkrec")
        return 1

    print("\nГотово. Можно запускать python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())