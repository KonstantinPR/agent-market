# -*- coding: utf-8 -*-
"""«Размножить имена»: генерация имён ``<артикул>-<n>.JPG``."""

from app.services.names_multiply import names_multiply, to_text


def test_multiply_basic():
    assert names_multiply(["A", "B"], 2) == [
        "A-1.JPG", "A-2.JPG", "B-1.JPG", "B-2.JPG",
    ]


def test_multiply_start_from():
    assert names_multiply(["A"], 2, start_from=5) == ["A-5.JPG", "A-6.JPG"]


def test_multiply_same_article_continues_numbering():
    # Повторный артикул продолжает нумерацию, чтобы имена не совпадали.
    assert names_multiply(["A", "A"], 1) == ["A-1.JPG", "A-2.JPG"]


def test_multiply_zero_returns_empty():
    assert names_multiply(["A"], 0) == []


def test_multiply_skips_blank():
    assert names_multiply(["A", "  ", ""], 1) == ["A-1.JPG"]


def test_multiply_strips_whitespace():
    assert names_multiply([" A "], 1) == ["A-1.JPG"]


def test_multiply_upper_default():
    # Капслок по умолчанию: артикул выводится в верхнем регистре.
    assert names_multiply(["wlp-117", "abc"], 1) == ["WLP-117-1.JPG", "ABC-1.JPG"]


def test_multiply_upper_false_keeps_case():
    assert names_multiply(["wlp-117"], 1, upper=False) == ["wlp-117-1.JPG"]


def test_multiply_upper_numbering_by_upper_art():
    # При капслоке «a» и «A» — один артикул: нумерация сквозная.
    assert names_multiply(["a", "A"], 1) == ["A-1.JPG", "A-2.JPG"]


def test_to_text():
    assert to_text(["a", "b"]) == "a\nb\n"
    assert to_text([]) == ""
