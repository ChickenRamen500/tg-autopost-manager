#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика/починка кодировки Python-файлов.

Причина бага Фазы 1: файл, сохранённый в Windows в cp1251 ( кириллица в докстрингах),
не читается интерпретатором внутри Docker (он ждёт UTF-8) ->
SyntaxError: Non-UTF-8 code starting with '\\xd1'.

Использование:
    python scripts/fix_encoding.py            # только проверка (diagnose)
    python scripts/fix_encoding.py --fix      # конвертировать cp1251 -> utf-8 + cookie
"""
import argparse
import pathlib
import re
import sys

CYR = re.compile(r"[\u0400-\u04FF]")


def check_file(p: pathlib.Path, fix: bool) -> str:
    data = p.read_bytes()
    try:
        text = data.decode("utf-8")
        status = "OK (utf-8)"
    except UnicodeDecodeError:
        if not fix:
            return f"BROKEN: не UTF-8 (похоже на cp1251/koi8-r): {p}"
        try:
            text = data.decode("cp1251")
        except UnicodeDecodeError:
            text = data.decode("koi8-r")
        status = "CONVERTED -> utf-8"
    # добавляем PEP 263 cookie, если есть кириллица и нет объявления кодировки
    lines = text.split("\n")
    head = "\n".join(lines[:2])
    if CYR.search(text) and "coding" not in head:
        idx = 1 if lines[0].startswith("#!") else 0
        lines.insert(idx, "# -*- coding: utf-8 -*-")
        text = "\n".join(lines)
        status += " (+cookie)"
    if fix and ("CONVERTED" in status or "+cookie" in status):
        p.write_bytes(text.encode("utf-8"))
    return f"{status}: {p}" if fix or "BROKEN" in status else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="конвертировать в UTF-8")
    args = ap.parse_args()
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = 0
    for p in sorted(root.rglob("*.py")):
        if ".git" in p.parts or "node_modules" in p.parts:
            continue
        msg = check_file(p, args.fix)
        if msg:
            print(msg)
        if msg.startswith("BROKEN"):
            bad += 1
    if bad and not args.fix:
        print(f"\nНайдено битых файлов: {bad}. Запустите: python scripts/fix_encoding.py --fix")
        return 1
    print("Кодировка в порядке." if not bad else "Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
