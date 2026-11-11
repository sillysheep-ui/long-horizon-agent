import unittest

from utils.text import (
    check_str,
    get_chars,
    get_cjk_chars,
    is_cjk,
    to_bool,
    truncate_text,
)


class TextUtilsTest(unittest.TestCase):
    def test_to_bool_accepts_case_insensitive_true_strings(self):
        for value in ("1", "true", "TRUE", "TrUe", "yes", "YES", "Yes"):
            with self.subTest(value=value):
                self.assertIs(to_bool(value), True)

    def test_to_bool_rejects_other_strings_without_stripping(self):
        for value in ("", "0", "false", "no", "on", "2", " true", "yes ", " "):
            with self.subTest(value=value):
                self.assertIs(to_bool(value), False)

    def test_to_bool_uses_truthiness_for_non_strings(self):
        for value, expected in (
            (None, False), (False, False), (0, False), ([], False), ({}, False),
            (True, True), (1, True), (-1, True), ([0], True), ({"a": 0}, True),
        ):
            with self.subTest(value=value):
                self.assertIs(to_bool(value), expected)

    def test_check_str_returns_stripped_string(self):
        for value, expected in (
            ("hello", "hello"),
            (" \t你好 world\n", "你好 world"),
            ("a  b", "a  b"),
            ("", ""),
            (" \t\n", ""),
        ):
            with self.subTest(value=value):
                result = check_str(value)
                self.assertIsInstance(result, str)
                self.assertEqual(result, expected)

    def test_check_str_rejects_non_strings(self):
        for value in (None, True, 0, 123, [], ["hello"], {}, b"hello"):
            with self.subTest(value=value):
                self.assertIs(check_str(value), False)

    def test_get_chars_preserves_letters_digits_and_chinese_in_order(self):
        self.assertEqual(get_chars(" A中1，b文2! A_\n🙂éあ한９"), "A中1b文2A")

    def test_get_chars_handles_empty_and_excluded_characters(self):
        for value in ("", " \t\n,。!?_🙂éあ한９"):
            with self.subTest(value=value):
                self.assertEqual(get_chars(value), "")

    def test_get_chars_includes_full_configured_chinese_range(self):
        self.assertEqual(get_chars("\u4dff\u4e00\u9fff\ua000"), "\u4e00\u9fff")

    def test_get_cjk_chars_preserves_only_chinese_in_order(self):
        self.assertEqual(get_cjk_chars("A中1，文!中 \n🙂あ한"), "中文中")

    def test_get_cjk_chars_handles_empty_and_excluded_characters(self):
        for value in ("", "abc123 \t。🙂あ한"):
            with self.subTest(value=value):
                self.assertEqual(get_cjk_chars(value), "")

    def test_get_cjk_chars_respects_configured_range_boundaries(self):
        # 覆盖基本区完整范围 U+4E00–U+9FFF：U+4DFF 与 U+A000 在范围外
        self.assertEqual(get_cjk_chars("\u4dff\u4e00\u9fa5\u9fa6\u9fff\ua000"), "\u4e00\u9fa5\u9fa6\u9fff")

    def test_is_cjk_detects_any_matching_character(self):
        for value in ("中文", "abc中123", "一", "龥"):
            with self.subTest(value=value):
                self.assertIs(is_cjk(value), True)

    def test_is_cjk_rejects_text_without_matching_characters(self):
        for value in ("", "abc123", " \t\n。🙂", "あ한", "\u4dff\ua000"):
            with self.subTest(value=value):
                self.assertIs(is_cjk(value), False)

    def test_truncate_text_preserves_text_at_or_below_limit(self):
        for value, limit in (("", 0), ("", 5), ("abc", 5), ("abcde", 5)):
            with self.subTest(value=value, limit=limit):
                self.assertEqual(truncate_text(value, limit), value)

    def test_truncate_text_without_whitespace(self):
        for limit in (4, 5):
            with self.subTest(limit=limit):
                self.assertEqual(
                    truncate_text("abcdefghij", limit),
                    "ab\n\n... [内容已截断，共省略 6 字符] ...\n\nij",
                )

    def test_truncate_text_uses_last_head_and_first_tail_whitespace(self):
        self.assertEqual(
            truncate_text("aa bb cc dd ee ff gg hh", 16),
            "aa bb\n\n... [内容已截断，共省略 13 字符] ...\n\ngg hh",
        )

    def test_truncate_text_handles_tabs_newlines_and_tail_leading_spaces(self):
        self.assertEqual(
            truncate_text("ab\tcdMIDDLEef\n \tgh", 12),
            "ab\n\n... [内容已截断，共省略 14 字符] ...\n\ngh",
        )

    def test_truncate_text_counts_unicode_characters(self):
        self.assertEqual(
            truncate_text("甲乙丙丁🙂戊己庚辛壬", 4),
            "甲乙\n\n... [内容已截断，共省略 6 字符] ...\n\n辛壬",
        )

    def test_truncate_text_default_limit(self):
        self.assertEqual(truncate_text("a" * 5000), "a" * 5000)
        self.assertEqual(
            truncate_text("a" * 5001),
            "a" * 2500 + "\n\n... [内容已截断，共省略 1 字符] ...\n\n" + "a" * 2500,
        )


if __name__ == "__main__":
    unittest.main()
