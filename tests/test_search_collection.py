"""用户场景驱动的智能收集、中断后重拉第一页与页数上限回归。"""

from app.domain.search import (
    SearchCollection,
    SearchResourceEvidence,
    SearchSourceCursor,
    search_page_signature,
)


def evidence(key, *targets):
    return SearchResourceEvidence(key, frozenset(targets))


def accept(source, page, items, *, now=1, exhausted=False):
    return source.accept_page(page=page, evidence=items, targets={"E1", "E4"},
                              exhausted=exhausted, now=now)


def test_closed_source_keeps_shared_cursor_for_opportunistic_candidates():
    collection = SearchCollection({"E1", "E4"}, {"A": SearchSourceCursor(), "B": SearchSourceCursor()})
    source = collection.sources["A"]
    accept(source, 0, [evidence("one", "E1")])
    accept(source, 1, [evidence("four", "E4")])
    assert source.closed == {"E1"}
    assert collection.ready() == set()
    accept(source, 2, [evidence("late", "E1"), evidence("four2", "E4")])
    assert source.found
    assert "E1" in source.closed
    assert source.next_page == 3
    collection.deepen({"E1"})
    assert collection.active_sources() == ["A", "B"]
    assert source.next_page == 3


def test_unknown_identity_cannot_close_collection():
    source = SearchSourceCursor()
    accept(source, 0, [evidence("first", "E1")])
    accept(source, 1, [SearchResourceEvidence("unknown", None)])
    assert not source.closed


def test_continuous_paging_never_goes_back_to_the_first_page():
    source = SearchSourceCursor()
    for page in range(5):
        assert source.page_to_request(page * 10) == page
        accept(source, page, [evidence(str(page))], now=page * 10)


def test_resume_after_interruption_loads_next_page_then_refreshes_first_page():
    source = SearchSourceCursor()
    accept(source, 0, [evidence("pin", "E1"), evidence("old")], now=1)
    accept(source, 1, [evidence("tail")], now=2)
    assert source.page_to_request(901) == 2
    assert source.page_to_request(902) == 2
    accept(source, 2, [evidence("next")], now=902)
    assert source.page_to_request(903) == 0
    closed, seen = set(source.closed), set(source.seen)
    # 置顶与旧资源按去重键只算一份，新资源补入；第一页不作为缺席证据，原页码不变。
    accept(source, 0, [evidence("pin", "E1"), evidence("new", "E4"), evidence("old")], now=903)
    assert (source.closed, source.seen) == (closed, seen)
    assert source.page_to_request(904) == 3


def test_every_gap_longer_than_threshold_still_advances_between_refreshes():
    source = SearchSourceCursor()
    accept(source, 0, [evidence("p0")], now=0)
    now = 0
    requested = []
    for _ in range(6):
        now += 1000
        page = source.page_to_request(now)
        requested.append(page)
        accept(source, page, [evidence(f"p{page}-{now}")], now=now)
    assert requested == [1, 0, 2, 0, 3, 0]


def test_twenty_pages_are_scanned_then_the_cap_stops_the_source():
    """第 20 页后释放收集屏障，但不把预算结束当成真实末页。"""
    collection = SearchCollection({"E1"}, {"A": SearchSourceCursor()})
    source = collection.sources["A"]
    for page in range(20):
        assert collection.active_sources() == ["A"]
        accept(source, page, [evidence(str(page))])
    assert not collection.active_sources()
    assert not source.exhausted
    assert collection.remaining == {"E1"}
    assert collection.ready() == {"E1"}


def test_page_identical_to_previous_page_is_treated_as_empty_last_page():
    source = SearchSourceCursor()
    accept(source, 0, [evidence("one", "E1")])
    accept(source, 1, [evidence("two", "E2")])
    assert accept(source, 2, [evidence("two", "E2")])
    assert source.exhausted
    assert not source.failed
    assert source.next_page == 3


def test_partial_overlap_keeps_paging_and_checkpoint_page_signature():
    source = SearchSourceCursor()
    accept(source, 0, [evidence("pin"), evidence("one", "E1")])
    accept(source, 1, [evidence("pin"), evidence("two", "E4")])
    assert not source.exhausted
    assert source.last_page_signature == search_page_signature(["pin", "two"])
    assert accept(source, 2, [evidence("pin"), evidence("two", "E4")])
    assert source.exhausted
    assert source.last_page_signature is None


def released(key, latest, *targets):
    return SearchResourceEvidence(key, frozenset(targets), (("1", latest),))


def test_episodes_newer_than_any_seen_resource_only_need_first_pages():
    collection = SearchCollection({"1:1003", "1:1006"}, {"A": SearchSourceCursor(), "B": SearchSourceCursor()})
    page = [released("1005", 1005), released("1003", 1003, "1:1003")]
    collection.observe(page)
    collection.sources["A"].accept_page(page=0, evidence=page, targets=collection.remaining, exhausted=False, now=1)
    assert collection.unreleased() == {"1:1006"}
    # B 尚未请求第一页，仍为站点未收录目标查第一页；A 只为站点已收录的 1003 继续翻页。
    assert collection.active_sources() == ["A", "B"]
    collection.sources["B"].accept_page(page=0, evidence=[], targets=collection.remaining, exhausted=False, now=1)
    collection.settle({"1:1003"})
    assert collection.active_sources() == []
    assert collection.ready() == {"1:1006"}
    assert collection.pending == set()
    collection.deepen({"1:1006"})
    assert collection.fallback == set()


def test_deeper_page_with_newer_episode_restores_normal_collection():
    collection = SearchCollection({"1:12"}, {"A": SearchSourceCursor(next_page=1)})
    collection.observe([released("ep10", 10)])
    assert collection.unreleased() == {"1:12"}
    collection.observe([released("ep13", 13)])
    assert collection.unreleased() == set()
    assert collection.active_sources() == ["A"]


def test_full_search_keeps_unseen_episodes_behind_the_page_barrier():
    """完整搜索不能由前页最大集号推断后页没有资源，并提前释放目标。"""
    collection = SearchCollection({"1:3"}, {"A": SearchSourceCursor(), "B": SearchSourceCursor()})
    page = [released("ep1", 1)]
    collection.observe(page)
    for source in collection.sources.values():
        source.accept_page(page=0, evidence=page, targets=collection.remaining, exhausted=False, now=1)
    assert collection.unreleased() == {"1:3"}
    assert collection.ready(full=True) == set()
    assert collection.active_sources(full=True) == ["A", "B"]
    for source in collection.sources.values():
        source.exhausted = True
    assert collection.ready(full=True) == {"1:3"}


def test_without_any_resource_of_the_season_targets_keep_normal_collection():
    collection = SearchCollection({"1:5"}, {"A": SearchSourceCursor(next_page=1)})
    collection.observe([SearchResourceEvidence("other", frozenset())])
    assert collection.unreleased() == set()
    assert collection.active_sources() == ["A"]
