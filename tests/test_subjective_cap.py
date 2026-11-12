"""Length rules for subjective / fill-blank answers.

Soft target (30) is prompt-only: an answer may run past it untouched so a
sentence is never cut mid-thought. The hard ceiling (60) is enforced.
"""

import unittest

from util.subjective import cap_answer


class CapAnswerTests(unittest.TestCase):
    def test_short_answer_is_untouched(self):
        self.assertEqual(cap_answer(["通货膨胀"], 60), ["通货膨胀"])

    def test_answer_past_soft_target_but_under_ceiling_is_untouched(self):
        answer = "一" * 45
        self.assertEqual(cap_answer([answer], 60), [answer])

    def test_answer_at_exact_ceiling_is_untouched(self):
        answer = "一" * 60
        self.assertEqual(cap_answer([answer], 60), [answer])

    def test_answer_over_ceiling_is_cut_to_ceiling(self):
        result = cap_answer(["一" * 61], 60)
        self.assertEqual(len(result[0]), 60)

    def test_over_ceiling_prefers_sentence_boundary(self):
        answer = "甲" * 29 + "。" + "乙" * 40
        result = cap_answer([answer], 60)
        self.assertEqual(result[0], "甲" * 29 + "。")
        self.assertLessEqual(len(result[0]), 60)

    def test_over_ceiling_falls_back_to_blunt_cut_when_boundary_too_early(self):
        answer = "甲" * 3 + "。" + "乙" * 70
        result = cap_answer([answer], 60)
        self.assertEqual(len(result[0]), 60)
        self.assertEqual(result[0], answer[:60])

    def test_each_item_is_evaluated_independently(self):
        items = ["短答案", "一" * 45, "一" * 80]
        result = cap_answer(items, 60)
        self.assertEqual(result[0], "短答案")
        self.assertEqual(len(result[1]), 45)
        self.assertLessEqual(len(result[2]), 60)

    def test_fill_blank_items_each_respect_ceiling(self):
        items = ["空白一", "一" * 70]
        result = cap_answer(items, 60)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "空白一")
        self.assertLessEqual(len(result[1]), 60)

    def test_no_ceiling_disables_capping(self):
        answer = "一" * 200
        self.assertEqual(cap_answer([answer], 0), [answer])


if __name__ == "__main__":
    unittest.main()
