"""用户场景驱动的智能收集、中断后重拉第一页与页数上限回归。"""

from app.domain.search import SearchCollection, SearchResourceEvidence, SearchSourceCursor


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
    assert "late" in source.resources
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
    assert source.resources == {"pin", "old", "tail", "next", "new"}
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


def test_one_hundred_pages_are_scanned_then_the_cap_stops_the_source():
    collection = SearchCollection({"E1"}, {"A": SearchSourceCursor()})
    source = collection.sources["A"]
    for page in range(100):
        assert collection.active_sources() == ["A"]
        accept(source, page, [evidence(str(page))])
    assert not collection.active_sources()
    assert not source.exhausted
    assert collection.remaining == {"E1"}


def test_page_identical_to_previous_page_is_treated_as_empty_last_page():
    source = SearchSourceCursor()
    accept(source, 0, [evidence("one", "E1")])
    accept(source, 1, [evidence("two", "E2")])
    assert accept(source, 2, [evidence("two", "E2")])
    assert source.exhausted
    assert not source.failed
    assert source.next_page == 3


def test_partial_overlap_keeps_paging_and_checkpoint_resource_ids():
    source = SearchSourceCursor()
    accept(source, 0, [evidence("pin"), evidence("one", "E1")])
    accept(source, 1, [evidence("pin"), evidence("two", "E4")])
    assert not source.exhausted
    assert source.last_page_ids == ("pin", "two")
    assert accept(source, 2, [evidence("pin"), evidence("two", "E4")])
    assert source.exhausted
    assert source.last_page_ids == ()
