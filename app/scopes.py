"""范围值对象：地区/媒介 + 毫秒半开片段区间 [start, end)。

内部以“范围项” (region, media, (start, end)) 的集合作为权威表示，
避免把地区/媒介并集与片段做错误的笛卡尔合并。片段区间为半开区间
（见 docs/contracts.md）。范围只基于显式列出的地区与媒介，不推导未列地区。
"""
from __future__ import annotations

from dataclasses import dataclass


def _check_range(range_ms: list | tuple) -> tuple[int, int]:
    if not isinstance(range_ms, (list, tuple)) or len(range_ms) != 2:
        raise ValueError("range_ms 必须是 [起点毫秒, 终点毫秒]")
    start, end = range_ms
    if not (isinstance(start, int) and isinstance(end, int)):
        raise ValueError("片段端点必须为整数毫秒")
    if start < 0 or end <= start:
        raise ValueError("片段区间须满足 0 <= start < end")
    return start, end


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相邻的半开区间。"""
    items = sorted(_check_range(r) for r in ranges)
    merged: list[tuple[int, int]] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def intersect_ranges(
    left: list[tuple[int, int]], right: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for ls, le in left:
        for rs, re in right:
            s, e = max(ls, rs), min(le, re)
            if s < e:
                out.append((s, e))
    return merge_ranges(out)


def subtract_ranges(
    base: list[tuple[int, int]], removed: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从 base 中逐段扣除 removed，返回剩余半开区间。"""
    remaining = [_check_range(r) for r in base]
    for rs, re in merge_ranges(removed):
        nxt: list[tuple[int, int]] = []
        for bs, be in remaining:
            if re <= bs or rs >= be:
                nxt.append((bs, be))
                continue
            if rs > bs:
                nxt.append((bs, min(rs, be)))
            if re < be:
                nxt.append((max(re, bs), be))
        remaining = nxt
    return merge_ranges(remaining)


# 范围项：(地区, 媒介, (起点, 终点))
Component = tuple[str, str, tuple[int, int]]


@dataclass(frozen=True)
class Scope:
    """一项限制的适用范围；内部以去重后的范围项集合表达。"""

    components: frozenset[Component]

    @classmethod
    def create(
        cls,
        regions: list[str],
        media: list[str],
        range_ms: list[int] | tuple[int, int],
    ) -> "Scope":
        if not regions:
            raise ValueError("至少需要一个地区")
        if not media:
            raise ValueError("至少需要一种媒介")
        rng = _check_range(range_ms)
        comps = frozenset(
            (region, medium, rng) for region in regions for medium in media)
        return cls(comps)

    @classmethod
    def from_components(cls, comps) -> "Scope":
        normalized = set()
        for region, medium, rng in comps:
            normalized.add((region, medium, _check_range(rng)))
        if not normalized:
            raise ValueError("范围不能为空")
        return cls(frozenset(normalized))

    @property
    def regions(self) -> frozenset[str]:
        return frozenset(c[0] for c in self.components)

    @property
    def media(self) -> frozenset[str]:
        return frozenset(c[1] for c in self.components)

    @property
    def ranges(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted({c[2] for c in self.components}))

    def is_empty(self) -> bool:
        return not self.components

    def to_dict(self) -> dict:
        # 显式范围项，保证多 (地区,媒介) 不同片段时可无损往返
        return {
            "regions": sorted(self.regions),
            "media": sorted(self.media),
            "ranges_ms": [list(r) for r in self.ranges],
            "components": [
                {"region": r, "media": m, "range_ms": list(rng)}
                for (r, m, rng) in sorted(self.components)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Scope":
        if data.get("components"):
            return cls.from_components(
                (c["region"], c["media"], tuple(c["range_ms"]))
                for c in data["components"])
        # 兼容旧形态：regions × media 共享同一组区间
        comps = [
            (region, medium, tuple(rng))
            for region in data["regions"]
            for medium in data["media"]
            for rng in data["ranges_ms"]
        ]
        return cls.from_components(comps)

    def contains(self, other: "Scope") -> bool:
        """other 的每个范围项都能在本范围中找到完全覆盖项。"""
        for o_region, o_medium, (os_, oe) in other.components:
            if not any(
                s_region == o_region and s_medium == o_medium
                and ss <= os_ and se >= oe
                for s_region, s_medium, (ss, se) in self.components
            ):
                return False
        return True

    def intersect(self, other: "Scope") -> "Scope | None":
        out: set[Component] = set()
        for s_region, s_medium, (ss, se) in self.components:
            for o_region, o_medium, (os_, oe) in other.components:
                if s_region != o_region or s_medium != o_medium:
                    continue
                overlap = intersect_ranges([(ss, se)], [(os_, oe)])
                for rng in overlap:
                    out.add((s_region, s_medium, rng))
        return Scope(frozenset(out)) if out else None

    def subtract(self, other: "Scope") -> "Scope | None":
        """逐 (地区, 媒介) 维度扣除重叠片段，保留其余范围项。"""
        out: set[Component] = set()
        for s_region, s_medium, (ss, se) in self.components:
            removed = [
                (os_, oe)
                for o_region, o_medium, (os_, oe) in other.components
                if o_region == s_region and o_medium == s_medium
            ]
            for rng in subtract_ranges([(ss, se)], removed):
                out.add((s_region, s_medium, rng))
        return Scope(frozenset(out)) if out else None


def union_scopes(scopes: list[Scope | None]) -> Scope | None:
    """合并若干范围：范围项取并集并合并同 (地区,媒介) 的相邻片段。"""
    real = [s for s in scopes if s and not s.is_empty()]
    if not real:
        return None
    grouped: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for scope in real:
        for region, medium, rng in scope.components:
            grouped.setdefault((region, medium), []).append(rng)
    comps: set[Component] = set()
    for (region, medium), ranges in grouped.items():
        for rng in merge_ranges(ranges):
            comps.add((region, medium, rng))
    return Scope(frozenset(comps))
