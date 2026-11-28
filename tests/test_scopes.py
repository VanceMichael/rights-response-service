"""范围值对象：半开片段区间的并集、交集、扣除与包含判定。"""
import unittest

from app.scopes import (Scope, intersect_ranges, merge_ranges,
                        subtract_ranges)


class RangeArithmeticTests(unittest.TestCase):
    def test_half_open_merge_and_subtract(self):
        self.assertEqual(merge_ranges([(0, 30000), (30000, 60000)]),
                         [(0, 60000)])
        self.assertEqual(subtract_ranges([(0, 60000)], [(30000, 45000)]),
                         [(0, 30000), (45000, 60000)])
        self.assertEqual(intersect_ranges([(0, 60000)], [(45000, 90000)]),
                         [(45000, 60000)])
        self.assertEqual(subtract_ranges([(0, 10000)], [(0, 10000)]), [])

    def test_scope_containment(self):
        big = Scope.create(["east", "south"], ["streaming"], [0, 60000])
        small = Scope.create(["east"], ["streaming"], [10000, 20000])
        self.assertTrue(big.contains(small))
        self.assertFalse(small.contains(big))

    def test_scope_subtract_isolates_other_owner(self):
        # 两权利方在 south 30-60k 重叠：扣除 A 后 B 的独立部分仍可表达
        a = Scope.create(["east", "south"], ["streaming"], [0, 60000])
        b = Scope.create(["south"], ["streaming"], [30000, 60000])
        leftover = a.subtract(b)
        # east 全境 + south 的 0-30k 仍属 A
        self.assertEqual(leftover.regions, frozenset({"east", "south"}))
        self.assertIn((0, 30000), leftover.ranges)

    def test_invalid_range_rejected(self):
        with self.assertRaises(ValueError):
            Scope.create(["east"], ["streaming"], [60000, 60000])
        with self.assertRaises(ValueError):
            Scope.create([], ["streaming"], [0, 1])


if __name__ == "__main__":
    unittest.main()
