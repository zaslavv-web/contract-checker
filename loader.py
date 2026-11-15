"""Сбор TXT-документов из загруженных файлов, ZIP-архивов и папок."""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import PurePosixPath

from extractor import decode

MAX_FILES = 500
MAX_TOTAL_BYTES = 50 * 1024 * 1024  # защита от «zip-бомб»


def _is_junk(path: str) -> bool:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return any(p.startswith(".") or p == "__MACOSX" for p in parts)


def collect_files(uploads: list[tuple[str, bytes]]) -> tuple[dict[str, str], list[str]]:
    """uploads: (имя или относительный путь, содержимое).

    Возвращает (имя -> текст, заметки для пользователя). TXT берутся как есть,
    из ZIP извлекаются все TXT (включая вложенные папки), остальное пропускается
    с пояснением. Одинаковые по содержимому файлы учитываются один раз.
    """
    files: dict[str, str] = {}
    notes: list[str] = []
    seen: dict[str, str] = {}  # sha1 содержимого -> имя
    total = 0

    def add(name: str, raw: bytes) -> None:
        nonlocal total
        if len(files) >= MAX_FILES:
            notes.append(f"{name}: пропущен — превышен лимит {MAX_FILES} файлов.")
            return
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            notes.append(f"{name}: пропущен — превышен общий объём {MAX_TOTAL_BYTES // 2**20} МБ.")
            return
        digest = hashlib.sha1(raw).hexdigest()
        if digest in seen:
            notes.append(f"{name}: совпадает по содержимому с {seen[digest]}, учтён один раз.")
            return
        short = PurePosixPath(name.replace("\\", "/")).name
        key = short if short not in files else name
        n = 2
        while key in files:
            key = f"{short} ({n})"
            n += 1
        seen[digest] = key
        files[key] = decode(raw)

    for name, raw in uploads:
        lower = name.lower()
        if _is_junk(name):
            continue
        if lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    members = [m for m in zf.infolist() if not m.is_dir()]
                    txt = [m for m in members if m.filename.lower().endswith(".txt") and not _is_junk(m.filename)]
                    if not txt:
                        notes.append(f"{name}: в архиве нет TXT-файлов.")
                    other = len([m for m in members if not _is_junk(m.filename)]) - len(txt)
                    if other:
                        notes.append(f"{name}: пропущено файлов не в формате TXT — {other}.")
                    for m in txt:
                        if m.file_size > MAX_TOTAL_BYTES:
                            notes.append(f"{name}/{m.filename}: пропущен — слишком большой.")
                            continue
                        add(f"{name}/{_member_name(m)}", zf.read(m))
            except zipfile.BadZipFile:
                notes.append(f"{name}: не удалось открыть как ZIP-архив.")
            except RuntimeError as exc:  # зашифрованный архив
                notes.append(f"{name}: архив не прочитан ({exc}).")
        elif lower.endswith(".txt"):
            add(name, raw)
        else:
            notes.append(f"{name}: пропущен — поддерживаются только TXT и ZIP.")
    return files, notes


def _member_name(m: zipfile.ZipInfo) -> str:
    """Имена в ZIP без флага UTF-8 хранятся в CP866 (архиваторы Windows)."""
    name = m.filename
    if not m.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("cp866")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name
