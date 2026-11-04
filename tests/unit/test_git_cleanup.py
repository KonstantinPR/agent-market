# -*- coding: utf-8 -*-
"""Правила уборки локальных веток (scripts/git_cleanup.py).

Скрипт удаляет только влитые ветки и не трогает защищённые — текущую, main и
stash-подобный чужой WIP. Проверяется чистая функция plan_cleanup, сам git не
запускается.
"""
from scripts.git_cleanup import plan_cleanup

MAIN = "main"


def _plan(branches, merged, current="main", worktrees=(), subjects=None, main=MAIN):
    return plan_cleanup(
        branches=list(branches),
        merged=set(merged),
        current=current,
        worktree_branches=set(worktrees),
        subjects=subjects or {},
        main_branch=main,
    )


def test_merged_ticket_branch_is_deleted():
    assert _plan(["main", "t-41-old"], {"main", "t-41-old"}) == ["t-41-old"]


def test_unmerged_branch_is_kept():
    assert _plan(["main", "t-99-wip"], {"main"}) == []


def test_current_and_main_are_protected():
    assert _plan(["main", "t-1-a"], {"main", "t-1-a"}, current="t-1-a") == []


def test_branch_checked_out_in_another_worktree_is_protected():
    assert _plan(["main", "t-67"], {"main", "t-67"}, worktrees={"t-67"}) == []


def test_stash_like_branch_is_protected():
    subjects = {"t-34-wip": "On main: T-31 WIP (foreign)"}
    assert _plan(["main", "t-34-wip"], {"main", "t-34-wip"}, subjects=subjects) == []


def test_ne_tronat_marker_is_protected():
    subjects = {"t-34-wip": "WIP: чужая разгрузка — NE TRONAT, NE COMMITAT"}
    assert _plan(["main", "t-34-wip"], {"main", "t-34-wip"}, subjects=subjects) == []


def test_master_repo_uses_master_as_main():
    # main/master защищены всегда, рабочая ветка удаляется как обычно
    assert _plan(["master", "t-1-a"], {"master", "t-1-a"}, current="master", main="master") == ["t-1-a"]


def test_only_merged_and_unprotected_are_returned_sorted():
    branches = ["main", "t-b", "t-a", "t-c", "t-wip"]
    merged = {"main", "t-b", "t-a", "t-c", "t-wip"}
    subjects = {"t-wip": "On main: WIP (foreign)"}
    assert _plan(branches, merged, subjects=subjects) == ["t-a", "t-b", "t-c"]
