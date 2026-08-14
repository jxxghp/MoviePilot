from unittest import TestCase

from app.foundation.collections import ListUtils


class ListUtilsTest(TestCase):
    def test_flatten_keeps_scalar_items_in_mixed_list(self):
        self.assertEqual(ListUtils.flatten([1, [2, 3], 4]), [1, 2, 3, 4])

    def test_flatten_returns_plain_list_unchanged(self):
        source = [1, 2, 3]
        self.assertEqual(ListUtils.flatten(source), source)

    def test_flatten_rejects_non_list_input(self):
        self.assertEqual(ListUtils.flatten("1,2,3"), [])
