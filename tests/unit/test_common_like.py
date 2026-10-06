"""Расширенный поиск «*» в app/services/common.py."""
import re

from app.services import common
from app.services.common import like_col, like_match, like_re, like_to_regex, like_to_sql


class TestLikeToSql:
    def test_star_becomes_percent(self):
        assert like_to_sql("тику*12") == "%тику%12%"

    def test_empty_query_is_match_all(self):
        assert like_to_sql("") == "%%"
        assert like_to_sql(None) == "%%"

    def test_leading_trailing_stars(self):
        # каждая «*» даёт «%»; «%%abc%%» — то же, что «%abc%»
        assert like_to_sql("*abc*") == "%%abc%%"

    def test_percent_is_literal(self):
        assert like_to_sql("100%") == "%100\\%%"
        assert "\\%" in like_to_sql("100%")

    def test_underscore_is_literal(self):
        assert like_to_sql("а_б") == "%а\\_б%"

    def test_backslash_is_literal(self):
        assert like_to_sql("a\\b") == "%a\\\\b%"

    def test_regular_chars_unescaped(self):
        assert like_to_sql("abc-123") == "%abc-123%"

    def test_trims_query(self):
        assert like_to_sql("  abc  ") == "%abc%"

    def test_escape_char_is_backslash(self):
        assert common.LIKE_ESCAPE == "\\"


class TestLikeToRegex:
    def test_star_becomes_dotstar(self):
        assert like_to_regex("abc*12") == "abc.*12"

    def test_query_is_lowercased(self):
        assert like_to_regex("ABC") == "abc"

    def test_regex_metachars_escaped(self):
        assert like_to_regex("a.b") == "a\\.b"
        assert like_to_regex("a(b)c") == "a\\(b\\)c"
        assert like_to_regex("c++") == "c\\+\\+"

    def test_percent_is_literal(self):
        assert like_to_regex("100%") == "100%"

    def test_underscore_is_literal(self):
        assert like_to_regex("a_b") == "a_b"

    def test_empty_query(self):
        assert like_to_regex("") == ""


class TestLikeRe:
    def test_compiles_and_matches(self):
        r = like_re("Тику*12")
        assert r.search("артикул-1-12-blue")
        assert r.search("новыйАртикул12-1")
        assert not r.search("сова12")

    def test_order_matters(self):
        r = like_re("abc*12*xyz")
        assert r.search("ABC12XYZ")
        assert not r.search("xyz12abc")

    def test_empty_query_is_none(self):
        assert like_re("") is None
        assert like_re(None) is None

    def test_uses_search_semantics(self):
        # как и раньше (art_check.search): подстрока без якорей
        assert like_re("12").search("артикул-12")


class TestLikeMatch:
    def test_user_example(self):
        assert like_match("артикул-1-12-blue", "тику*12")
        assert like_match("новыйАртикул12-1", "тику*12")
        assert not like_match("сова12", "тику*12")

    def test_multiple_stars_ordered(self):
        assert like_match("01597-300-5-GREEN", "01597*GREEN")
        assert not like_match("GREEN-01597", "01597*GREEN")

    def test_star_matches_empty_string(self):
        assert like_match("ac", "a*c")
        assert like_match("abc", "a*c")

    def test_case_insensitive(self):
        assert like_match("TIE-CORE-BLACK", "tie*black")
        assert like_match("tie-core-black", "TIE*BLACK")

    def test_percent_is_literal(self):
        assert like_match("100% хлопок", "100%")
        assert not like_match("1000 символов", "100%")

    def test_underscore_is_literal(self):
        assert like_match("юбка_шорты", "юбка_шорты")
        assert not like_match("юбкаXшорты", "юбка_шорты")

    def test_regex_metachars_are_literal(self):
        assert like_match("a.b", "a.b")
        assert not like_match("aXb", "a.b")

    def test_empty_query_matches_everything(self):
        assert like_match("что угодно", "")
        assert like_match("что угодно", "   ")
        assert like_match("что угодно", None)

    def test_falsy_inputs_do_not_raise(self):
        assert not like_match(None, "abc")
        assert not like_match("", "abc")


class TestLikeCol:
    def test_compiles_to_escaped_ilike(self):
        from sqlalchemy import column
        expr = like_col(column("article"), "100%")
        sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
        # литеральный «%» обязан быть экранирован и явно escape-нут
        assert "ESCAPE" in sql.upper()
        # фактический SQL: lower('%100\%%') ESCAPE '\'
        assert "%100\\%%" in sql

    def test_star_is_percent_wildcard(self):
        from sqlalchemy import column
        expr = like_col(column("article"), "а*б")
        sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
        assert "%а%б%" in sql


class TestNoOldHelpers:
    def test_common_has_no_like_pattern(self):
        # старое имя конфликтует с удалённым sync.like_pattern
        assert not hasattr(common, "like_pattern")

    def test_sync_no_longer_exports_like_pattern(self):
        from app.services import sync
        assert not hasattr(sync, "like_pattern")

    def test_regex_cache_bounded(self):
        src = open("app/static/app.js", encoding="utf-8").read()
        m = re.search(r"if \(_LIKE_RE_CACHE\.size > (\d+)\)", src)
        assert m, "кэш в app.js должен быть ограничен"
        assert int(m.group(1)) == 200
