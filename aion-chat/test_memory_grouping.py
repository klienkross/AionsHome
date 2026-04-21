"""测试 memory._split_into_groups 的时间戳聚合行为"""

from memory import _split_into_groups, _subdivide_long, _fixed_size_split


def mk(msgs_ts: list[float]) -> list[dict]:
    return [{"id": i, "created_at": ts, "role": "user", "content": "x"} for i, ts in enumerate(msgs_ts)]


def total_len(groups):
    return sum(len(g) for g in groups)


def test_empty():
    assert _split_into_groups([]) == []


def test_single():
    msgs = mk([1000.0])
    assert _split_into_groups(msgs) == [msgs]


def test_short_fast_path():
    msgs = mk([1000.0 + i * 60 for i in range(15)])
    assert _split_into_groups(msgs) == [msgs]


def test_single_session_long_run_subdivide():
    # 50 条，30 秒间隔，全部一段 → Step 3 走 B1 兜底（gap 完全均匀）
    msgs = mk([1000.0 + i * 30 for i in range(50)])
    groups = _split_into_groups(msgs)
    assert all(len(g) <= 20 for g in groups)
    assert len(groups) >= 3
    assert total_len(groups) == 50


def test_multi_day_dense_each_day():
    # 3 天，每天 12 条 1 分钟间隔；天间隔 12 小时 → 3 段，每段 12
    day = 86400
    msgs = []
    for d in range(3):
        base = d * day
        for i in range(12):
            msgs.append(base + i * 60)
    groups = _split_into_groups(mk(msgs))
    assert len(groups) == 3
    assert all(len(g) == 12 for g in groups)


def test_multi_day_sparse_collapses_but_lossless():
    # 3 天每天 5 条 → 单次贪心合并后段数 ∈ {1, 2}，但总条数必须是 15
    day = 86400
    msgs = []
    for d in range(3):
        base = d * day
        for i in range(5):
            msgs.append(base + i * 60)
    groups = _split_into_groups(mk(msgs))
    assert total_len(groups) == 15
    assert len(groups) in (1, 2)


def test_short_segment_merges_to_nearer_neighbor():
    # [12 条] -- 大 gap -- [3 条] -- 大 gap -- [15 条]
    # 让中间 3 条距左侧 gap 更小（左侧 1.5h，右侧 3h）→ 期望并入左侧
    seg1 = [i * 60.0 for i in range(12)]                       # 0..660
    seg2_start = seg1[-1] + 1.5 * 3600                          # +1.5h
    seg2 = [seg2_start + i * 60.0 for i in range(3)]
    seg3_start = seg2[-1] + 3 * 3600                            # +3h
    seg3 = [seg3_start + i * 60.0 for i in range(15)]
    msgs = mk(seg1 + seg2 + seg3)
    groups = _split_into_groups(msgs)
    assert total_len(groups) == 30
    assert len(groups) == 2
    assert len(groups[0]) == 15  # 12 + 3
    assert len(groups[1]) == 15


def test_long_segment_split_at_secondary_gap():
    # 30 条总段（Step 1 单段，因为最大 gap < 1h），中间一个 50 分钟 gap → B2 切成 [15, 15]
    left = [i * 60.0 for i in range(15)]                        # 0..840 step 60
    right_start = left[-1] + 50 * 60                            # +50min
    right = [right_start + i * 60.0 for i in range(15)]
    msgs = mk(left + right)
    groups = _split_into_groups(msgs)
    assert len(groups) == 2
    assert len(groups[0]) == 15
    assert len(groups[1]) == 15


def test_long_segment_uniform_falls_back_to_b1():
    # 50 条 1 分钟均匀间隔 → Step 1 单段，Step 3 找不到显著 gap → B1
    msgs = mk([i * 60.0 for i in range(50)])
    groups = _split_into_groups(msgs)
    assert all(len(g) <= 20 for g in groups)
    assert total_len(groups) == 50
    # B1: 50 = 20 + 20 + 10
    assert len(groups) == 3


def test_boundary_short_first_segment_merges_to_next():
    # 首段 4 条，后面 16 条；间隔 2h → Step 1 切成 [4, 16]，首段边界只能并入下一段
    seg1 = [i * 60.0 for i in range(4)]
    seg2_start = seg1[-1] + 2 * 3600
    seg2 = [seg2_start + i * 60.0 for i in range(16)]
    msgs = mk(seg1 + seg2)
    groups = _split_into_groups(msgs)
    # 合并后变 1 段 20 条；20 不超过 target_max，不再细分
    assert len(groups) == 1
    assert len(groups[0]) == 20
