# -*- coding: utf-8 -*-
"""Уборка локальных веток git: удаляет только то, что уже влито в main.

Зачем: после закрытия тикетов ветки `t-<N>-<слаг>` остаются в `git branch`, и
список перестаёт что-либо значить. Скрипт показывает кандидатов и по `--apply`
удаляет их через `git branch -d` (безопасно: git сам откажется удалять
невлятую ветку).

Запуск:
    python scripts/git_cleanup.py                 # только показать план
    python scripts/git_cleanup.py --apply         # удалить влитые
    python scripts/git_cleanup.py --apply --fetch # сначала git fetch --prune

Никогда не трогаются:
- текущая ветка и ветки, занятые в других worktree;
- `main` / `master`;
- stash-подобные ветки (`On <branch>: ...`) и ветки с пометкой «NE TRONAT» /
  «НЕ ТРОНАТЬ» — такой чужим WIP управляет человек, а не автоматика.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

MAIN_CANDIDATES = ("main", "master")
NEVER_TRONAT = ("NE TRONAT", "НЕ ТРОНАТЬ", "НЕ КОММИТИТЬ")


def _git(*args: str) -> str:
    """Вывод git как текст; падение git — исключение (лучше упасть, чем угадать)."""
    res = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return res.stdout


def _is_stash_like(subject: str) -> bool:
    """`On main: T-31 WIP (foreign)` — ветка-слепок stash, не рабочая ветка."""
    return subject.startswith("On ") and ": " in subject


def plan_cleanup(
    branches: list[str],
    merged: set[str],
    current: str,
    worktree_branches: set[str],
    subjects: dict[str, str],
    main_branch: str,
) -> list[str]:
    """Чистая функция (тестируется без git): какие ветки можно удалить."""
    out = []
    for name in branches:
        if name == current or name == main_branch or name in MAIN_CANDIDATES:
            continue
        if name not in merged or name in worktree_branches:
            continue
        subject = subjects.get(name, "")
        if _is_stash_like(subject):
            continue
        if any(mark in subject.upper() for mark in NEVER_TRONAT):
            continue
        out.append(name)
    return sorted(out)


def _main_branch(branches: list[str]) -> str:
    for name in MAIN_CANDIDATES:
        if name in branches:
            return name
    raise SystemExit("Не найдена ветка main/master — уборка отменена.")


def _current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD").strip()


def _worktree_branches() -> set[str]:
    out: set[str] = set()
    for line in _git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("branch refs/heads/"):
            out.add(line[len("branch refs/heads/"):].strip())
    return out


def _subjects() -> dict[str, str]:
    out: dict[str, str] = {}
    raw = _git("for-each-ref", "--format=%(refname:short)%09%(subject)", "refs/heads")
    for line in raw.splitlines():
        name, _, subject = line.partition("\t")
        if name:
            out[name] = subject.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Удалить локальные ветки, влитые в main.")
    parser.add_argument("--apply", action="store_true", help="удалить (без флага — только показать)")
    parser.add_argument("--fetch", action="store_true", help="перед уборкой: git fetch --prune")
    args = parser.parse_args(argv)

    if args.fetch:
        print("git fetch --prune …")
        _git("fetch", "--prune")

    branches = [b.strip() for b in _git("branch", "--format=%(refname:short)").splitlines() if b.strip()]
    main_branch = _main_branch(branches)
    merged = {
        line.strip().lstrip("*+ ").strip()
        for line in _git("branch", "--merged", main_branch, "--format=%(refname:short)").splitlines()
        if line.strip()
    }
    deletable = plan_cleanup(
        branches=branches,
        merged=merged,
        current=_current_branch(),
        worktree_branches=_worktree_branches(),
        subjects=_subjects(),
        main_branch=main_branch,
    )
    leftovers = sorted(set(branches) - set(deletable) - {main_branch})

    if not deletable:
        print(f"Влитых веток для удаления нет. Ветка в работе: {main_branch}.")
    else:
        print(f"Влито в {main_branch}, можно удалить ({len(deletable)}):")
        for name in deletable:
            print(f"  {name}")

    if leftovers:
        print("\nОстаются (не влиты или защищены):")
        for name in leftovers:
            mark = "" if name not in merged else "  [влита, но защищена]"
            print(f"  {name}{mark}")

    if not args.apply:
        print("\nЭто план. Удалить: добавьте --apply")
        return 0

    failed: list[str] = []
    for name in deletable:
        try:
            _git("branch", "-d", name)
            print(f"удалена: {name}")
        except subprocess.CalledProcessError as exc:
            failed.append(name)
            print(f"НЕ удалена: {name} ({exc.stderr.strip()})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
