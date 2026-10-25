"""MCP-07: the browser access diet.

Pins, against an instrumented fake browser whose ``children``
property counts every hierarchy descent:

* default browser calls (tree, items-at-path) are byte-identical to
  the pre-MCP-07 results - hand-computed goldens, the MCP-05 way;
* a repeated identical read is served from the script-side read
  cache and a repeated URI lookup from the URI memo, both with zero
  additional hierarchy walks;
* the caches are bounded LRUs (eviction pinned at shrunken bounds)
  and are invalidated by a load - the one browser-affecting mutation
  the script performs - and by the explicit refresh_browser_cache
  command, which reports what it dropped;
* failed reads are never cached;
* get_browser_items_at_path paging is opt-in: slices of the paged
  form concatenate to exactly the unpaged items, with total /
  next_offset bookkeeping;
* a degenerate limit (0 or negative) on either paged read is
  clamped to a page size of 1, so next_offset always advances;
* get_browser_items_at_path builds its read-cache key from the
  coerced paging values (SI-75): an unhashable raw value reaches
  the lenient coercion instead of crashing the cache lookup, and
  raw forms of the same coerced page (5 / "5" / 5 with offset 0)
  share one cache entry, while the unpaged form keeps its own;
* paging values that are non-finite floats fall back to the
  default page instead of failing the read (SI-236):
  int(+-inf) raises OverflowError, which the lenient
  coercion now treats as unusable alongside TypeError and
  ValueError - NaN already fell back - while a finite
  huge value still clamps to the cap;
* the remaining browser cache keys refuse unhashable key
  components by name (SI-237): get_browser_tree keys on the raw
  category_type, get_browser_items_at_path still embeds the
  raw path (SI-75 coerced only its paging values), and the
  get_browser_item URI memo keys on the raw uri, so a list or
  dict crashed each lookup with a raw "unhashable type: ..."
  message; those shapes now refuse as "Invalid <field>: must
  be a string", write no cache entry, and hashable non-string
  scalars (42, None) keep their exact legacy behaviour; the two
  load commands share that same URI memo, so their list/dict
  refusals are pinned directly as well, together with the
  remaining probed hashable controls - a uri of bytes and a
  category_type tuple (SI-238);
* search_browser matches by name or URI, case-insensitively, in
  browser order, pages from one cached walk, and validates its
  query; the tool surface sends clean wire params and an old script
  (no search command) produces the standard Unknown-command Error
  string server-side;
* the per-call dir(browser) dump is gone from the browser logs,
  while its data (available_categories) still reaches results.
"""

import asyncio
import json
import os
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.test_remote_script_bridge import _load_script_module  # noqa: E402

script_module = _load_script_module()


# ── Counting fake browser ────────────────────────────────────────────

class WalkCounter:
    def __init__(self):
        self.walks = 0


class FakeItem:
    """A browser item whose children access is the counted walk."""

    def __init__(self, counter, name, children=(), loadable=False,
                 device=False, uri=None):
        self._counter = counter
        self.name = name
        self._children = list(children)
        self.is_folder = bool(children)
        self.is_loadable = loadable
        self.is_device = device
        self.uri = uri

    @property
    def children(self):
        self._counter.walks += 1
        return self._children


class FakeBrowser:
    """The five standard roots and nothing else: the URI walk reads
    all five unguarded (as Live's browser guarantees), and dir() -
    hence available_categories - is exactly those five names."""

    def __init__(self, instruments, sounds, drums, audio_effects,
                 midi_effects):
        self.instruments = instruments
        self.sounds = sounds
        self.drums = drums
        self.audio_effects = audio_effects
        self.midi_effects = midi_effects


class LoadableBrowser(FakeBrowser):
    """Adds the one browser-directed mutation the script performs."""

    def __init__(self, *args):
        super().__init__(*args)
        self.loaded = []

    def load_item(self, item):
        self.loaded.append(item)


def build_browser(counter, browser_cls=FakeBrowser):
    def item(name, **kw):
        return FakeItem(counter, name, **kw)

    synths = item("Synths", uri="fake:synths", children=[
        item("Analog", loadable=True, device=True, uri="fake:analog"),
        item("Operator", loadable=True, device=True, uri="fake:operator"),
    ])
    keys = item("Keys", uri="fake:keys", children=[
        item("Grand Piano", loadable=True, uri="fake:grand-piano"),
    ])
    instruments = item("Instruments", uri="fake:instruments", children=[
        synths, keys,
        item("Simpler", loadable=True, device=True, uri="fake:simpler"),
    ])
    kits = item("Kits", uri="fake:kits", children=(
        [item("808 Core Kit", loadable=True, uri="fake:808"),
         item("909 Kit", loadable=True, uri="fake:909")]
        + [item("Kit %02d" % n, loadable=True, uri="fake:kit-%02d" % n)
           for n in range(1, 9)]))
    drums = item("Drums", uri="fake:drums", children=[kits])
    sounds = item("Sounds", uri="fake:sounds", children=[])
    audio_effects = item("Audio Effects", uri="fake:audio-effects",
                         children=[])
    midi_effects = item("MIDI Effects", uri="fake:midi-effects",
                        children=[])
    return browser_cls(instruments, sounds, drums, audio_effects,
                       midi_effects)


def make_instance(browser):
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst.log_message = lambda message: None
    inst.application = lambda: types.SimpleNamespace(browser=browser)
    track = types.SimpleNamespace(name="Track 1")
    inst._song = types.SimpleNamespace(
        tracks=[track],
        view=types.SimpleNamespace(selected_track=None))
    scheduled = []

    def schedule_message(_delay, fn):
        scheduled.append(fn)
        fn()

    inst.schedule_message = schedule_message
    return inst


def wire(inst, command_type, params=None):
    response = inst._process_command(
        {"type": command_type, "params": params or {}})
    assert response["status"] == "success", response
    return response["result"]


def canonical(value):
    return json.dumps(value, sort_keys=True)


# ── Golden default behaviour ─────────────────────────────────────────

GOLDEN_TREE_INSTRUMENTS = {
    "type": "instruments",
    "categories": [{
        "name": "Instruments",
        "is_folder": True,
        "is_device": False,
        "is_loadable": False,
        "uri": "fake:instruments",
        "children": [
            {"name": "Synths", "is_folder": True, "is_device": False,
             "is_loadable": False, "uri": "fake:synths", "children": [],
             "has_more": True},
            {"name": "Keys", "is_folder": True, "is_device": False,
             "is_loadable": False, "uri": "fake:keys", "children": [],
             "has_more": True},
            {"name": "Simpler", "is_folder": False, "is_device": True,
             "is_loadable": True, "uri": "fake:simpler", "children": []},
        ],
    }],
    "available_categories": ["audio_effects", "drums", "instruments",
                           "midi_effects", "sounds"],
    "total_folders": 3,
}

GOLDEN_ITEMS_INSTRUMENTS = {
    "path": "instruments",
    "name": "Instruments",
    "uri": "fake:instruments",
    "is_folder": True,
    "is_device": False,
    "is_loadable": False,
    "items": [
        {"name": "Synths", "is_folder": True, "is_device": False,
         "is_loadable": False, "uri": "fake:synths"},
        {"name": "Keys", "is_folder": True, "is_device": False,
         "is_loadable": False, "uri": "fake:keys"},
        {"name": "Simpler", "is_folder": False, "is_device": True,
         "is_loadable": True, "uri": "fake:simpler"},
    ],
}


def test_default_tree_matches_golden():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    result = wire(inst, "get_browser_tree", {"category_type": "instruments"})
    assert canonical(result) == canonical(GOLDEN_TREE_INSTRUMENTS)


def test_default_items_at_path_matches_golden():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    result = wire(inst, "get_browser_items_at_path", {"path": "instruments"})
    assert canonical(result) == canonical(GOLDEN_ITEMS_INSTRUMENTS)


# ── Caching: repeat reads do not re-walk ─────────────────────────────

def test_repeated_tree_read_does_not_rewalk():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    first = wire(inst, "get_browser_tree", {"category_type": "instruments"})
    walks_after_first = counter.walks
    assert walks_after_first > 0
    second = wire(inst, "get_browser_tree", {"category_type": "instruments"})
    assert counter.walks == walks_after_first
    assert canonical(second) == canonical(first)


def test_repeated_items_read_does_not_rewalk():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    first = wire(inst, "get_browser_items_at_path", {"path": "drums/Kits"})
    walks_after_first = counter.walks
    assert walks_after_first > 0
    second = wire(inst, "get_browser_items_at_path", {"path": "drums/Kits"})
    assert counter.walks == walks_after_first
    assert canonical(second) == canonical(first)


def test_repeated_uri_lookup_does_not_rewalk():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    first = wire(inst, "get_browser_item", {"uri": "fake:808"})
    assert first["item"]["name"] == "808 Core Kit"
    walks_after_first = counter.walks
    assert walks_after_first > 0
    second = wire(inst, "get_browser_item", {"uri": "fake:808"})
    assert counter.walks == walks_after_first
    assert canonical(second) == canonical(first)


def test_failed_read_is_not_cached():
    counter = WalkCounter()
    browser = build_browser(counter)
    inst = make_instance(browser)
    response = inst._process_command(
        {"type": "get_browser_items_at_path",
         "params": {"path": "instruments/Bogus"}})
    assert response["result"]["error"] == "Path part 'Bogus' not found"
    # The folder appears (library changed outside the script); the
    # error must not have been pinned by the read cache.
    browser.instruments._children.append(
        FakeItem(counter, "Bogus", uri="fake:bogus", children=[]))
    result = wire(inst, "get_browser_items_at_path",
                  {"path": "instruments/Bogus"})
    assert result["name"] == "Bogus"


# ── Cache bounds (LRU eviction at shrunken bounds) ───────────────────

def test_uri_cache_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(script_module, "MAX_BROWSER_URI_CACHE_ENTRIES", 3)
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    for uri in ("fake:analog", "fake:operator", "fake:simpler", "fake:808"):
        wire(inst, "get_browser_item", {"uri": uri})
    uri_cache, _ = inst._browser_cache_state()
    assert len(uri_cache) == 3
    # The oldest entry was evicted: its lookup walks again.
    walks = counter.walks
    wire(inst, "get_browser_item", {"uri": "fake:analog"})
    assert counter.walks > walks
    # The newest entry survived: its lookup is walk-free.
    walks = counter.walks
    wire(inst, "get_browser_item", {"uri": "fake:808"})
    assert counter.walks == walks


def test_read_cache_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(script_module, "MAX_BROWSER_READ_CACHE_ENTRIES", 2)
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    for path in ("instruments", "drums", "drums/Kits"):
        wire(inst, "get_browser_items_at_path", {"path": path})
    _, read_cache = inst._browser_cache_state()
    assert len(read_cache) == 2
    walks = counter.walks
    wire(inst, "get_browser_items_at_path", {"path": "instruments"})
    assert counter.walks > walks  # evicted: re-navigates
    walks = counter.walks
    wire(inst, "get_browser_items_at_path", {"path": "drums/Kits"})
    assert counter.walks == walks  # still cached


# ── Invalidation: load and explicit refresh ──────────────────────────

def test_load_invalidates_both_caches():
    counter = WalkCounter()
    browser = build_browser(counter, LoadableBrowser)
    inst = make_instance(browser)
    wire(inst, "get_browser_tree", {"category_type": "instruments"})
    wire(inst, "get_browser_item", {"uri": "fake:808"})
    uri_cache, read_cache = inst._browser_cache_state()
    assert len(read_cache) == 1 and len(uri_cache) == 1

    result = wire(inst, "load_browser_item",
                  {"item_uri": "fake:808", "track_index": 0})
    assert result["item_name"] == "808 Core Kit"
    assert [item.name for item in browser.loaded] == ["808 Core Kit"]
    assert len(uri_cache) == 0 and len(read_cache) == 0

    # And the next reads really do walk again.
    walks = counter.walks
    wire(inst, "get_browser_tree", {"category_type": "instruments"})
    assert counter.walks > walks
    walks = counter.walks
    wire(inst, "get_browser_item", {"uri": "fake:808"})
    assert counter.walks > walks


def test_refresh_browser_cache_reports_and_clears():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    wire(inst, "get_browser_tree", {"category_type": "instruments"})
    wire(inst, "get_browser_item", {"uri": "fake:808"})
    result = wire(inst, "refresh_browser_cache")
    assert result == {"cleared": {"uri_entries": 1, "read_entries": 1}}
    # Refreshing an empty cache reports zeros, not an error.
    assert wire(inst, "refresh_browser_cache") == {
        "cleared": {"uri_entries": 0, "read_entries": 0}}
    walks = counter.walks
    wire(inst, "get_browser_tree", {"category_type": "instruments"})
    assert counter.walks > walks


# ── Paging on get_browser_items_at_path ──────────────────────────────

def test_items_paging_slices_equal_unpaged():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    unpaged = wire(inst, "get_browser_items_at_path", {"path": "drums/Kits"})
    assert "total" not in unpaged and "next_offset" not in unpaged
    assert len(unpaged["items"]) == 10

    stitched, offset, chain = [], 0, []
    while offset is not None:
        page = wire(inst, "get_browser_items_at_path",
                    {"path": "drums/Kits", "limit": 3, "offset": offset})
        assert page["total"] == 10
        # Every legacy key rides along unchanged.
        for key in ("path", "name", "uri", "is_folder", "is_device",
                    "is_loadable"):
            assert page[key] == unpaged[key]
        stitched.extend(page["items"])
        offset = page["next_offset"]
        chain.append(offset)
    assert chain == [3, 6, 9, None]
    assert stitched == unpaged["items"]


def test_items_paging_limit_only_and_offset_only():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    page = wire(inst, "get_browser_items_at_path",
                {"path": "drums/Kits", "limit": 4})
    assert (page["total"], page["next_offset"]) == (10, 4)
    assert len(page["items"]) == 4
    page = wire(inst, "get_browser_items_at_path",
                {"path": "drums/Kits", "offset": 8})
    assert (page["total"], page["next_offset"]) == (10, None)
    assert [i["name"] for i in page["items"]] == ["Kit 07", "Kit 08"]


def test_items_paging_degenerate_limit_advances():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    unpaged = wire(inst, "get_browser_items_at_path", {"path": "drums/Kits"})
    for bad_limit in (0, -5):
        page = wire(inst, "get_browser_items_at_path",
                    {"path": "drums/Kits", "limit": bad_limit, "offset": 0})
        # The effective page size is clamped to 1: one item, and
        # next_offset strictly advances instead of repeating 0.
        assert page["total"] == 10
        assert page["items"] == unpaged["items"][:1]
        assert page["next_offset"] == 1
    # A client blindly following next_offset with limit=0 terminates
    # and stitches exactly the unpaged items, one per page.
    stitched, offset, pages = [], 0, 0
    while offset is not None:
        page = wire(inst, "get_browser_items_at_path",
                    {"path": "drums/Kits", "limit": 0, "offset": offset})
        stitched.extend(page["items"])
        offset = page["next_offset"]
        pages += 1
    assert pages == 10
    assert stitched == unpaged["items"]


# ── Cache-key coercion on get_browser_items_at_path (SI-75) ─────────

def test_items_paging_unhashable_values_are_coerced():
    # At base the cache key was built from the raw values before
    # coercion, so an unhashable limit/offset crashed the lookup
    # with "unhashable type: 'list'" and the layer's lenient
    # coercion never saw the value. Each shape below runs on a
    # fresh instance (cold cache) and must return the same coerced
    # default page as the "abc" control.
    control_counter = WalkCounter()
    control = wire(make_instance(build_browser(control_counter)),
                   "get_browser_items_at_path",
                   {"path": "instruments", "limit": "abc"})
    assert control["total"] == 3 and control["next_offset"] is None
    assert len(control["items"]) == 3
    for params in ({"path": "instruments", "limit": [1]},
                   {"path": "instruments", "limit": {"x": 1}},
                   {"path": "instruments", "offset": [1]},
                   {"path": "instruments", "offset": {"x": 1}}):
        counter = WalkCounter()
        inst = make_instance(build_browser(counter))
        result = wire(inst, "get_browser_items_at_path", params)
        assert canonical(result) == canonical(control)


def test_items_paging_mixed_raw_forms_share_one_cache_entry():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    forms = [
        {"path": "drums/Kits", "limit": 2},
        {"path": "drums/Kits", "limit": "2"},
        {"path": "drums/Kits", "limit": 2, "offset": 0},
        {"path": "drums/Kits", "limit": "2", "offset": "0"},
        {"path": "drums/Kits", "limit": 2.9, "offset": 0},
    ]
    first = wire(inst, "get_browser_items_at_path", forms[0])
    walks_after_first = counter.walks
    assert walks_after_first > 0
    for params in forms[1:]:
        result = wire(inst, "get_browser_items_at_path", params)
        assert canonical(result) == canonical(first)
    # Every raw form above is the same coerced page (limit 2,
    # offset 0): one cache entry, keyed by the coerced values,
    # and no re-walk after the first read.
    assert counter.walks == walks_after_first
    _, read_cache = inst._browser_cache_state()
    assert list(read_cache.keys()) == [("items", "drums/Kits", 2, 0)]


def test_items_paging_default_forms_share_one_cache_entry():
    # An unhashable value, a non-numeric string and the explicit
    # default all coerce to the same page (limit 100, offset 0).
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    first = wire(inst, "get_browser_items_at_path",
                 {"path": "instruments", "limit": [1]})
    walks_after_first = counter.walks
    for params in ({"path": "instruments", "limit": "abc"},
                   {"path": "instruments", "limit": 100, "offset": 0},
                   {"path": "instruments", "limit": 100}):
        result = wire(inst, "get_browser_items_at_path", params)
        assert canonical(result) == canonical(first)
    assert counter.walks == walks_after_first
    _, read_cache = inst._browser_cache_state()
    assert list(read_cache.keys()) == [("items", "instruments", 100, 0)]


def test_items_unpaged_form_keeps_its_own_cache_entry():
    # Keeping the unpaged form exactly as today is load-bearing:
    # its result has no total/next_offset, so it must never share
    # an entry with the paged read that slices the same children.
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    unpaged = wire(inst, "get_browser_items_at_path",
                   {"path": "instruments"})
    assert "total" not in unpaged and "next_offset" not in unpaged
    walks_after_unpaged = counter.walks
    paged = wire(inst, "get_browser_items_at_path",
                 {"path": "instruments", "limit": 100, "offset": 0})
    assert paged["total"] == 3 and paged["items"] == unpaged["items"]
    assert counter.walks > walks_after_unpaged
    _, read_cache = inst._browser_cache_state()
    assert set(read_cache.keys()) == {
        ("items", "instruments", None, None),
        ("items", "instruments", 100, 0)}
    # The unpaged repeat still hits its own entry, walk-free,
    # with the unpaged shape intact.
    walks = counter.walks
    again = wire(inst, "get_browser_items_at_path",
                 {"path": "instruments"})
    assert counter.walks == walks
    assert canonical(again) == canonical(unpaged)


# ── Non-finite paging values fall back to the default (SI-236) ────

def test_items_paging_nonfinite_values_fall_back_to_default():
    # _coerce_page_int caught (TypeError, ValueError) around int(),
    # but int(+-inf) raises OverflowError, so a non-finite limit or
    # offset failed the whole read with "cannot convert float
    # infinity to integer" instead of serving the documented
    # default page. Each shape below runs on a fresh instance
    # (cold cache) and must return exactly the coerced default
    # page, as the "abc" control does; NaN already fell back at
    # base (int(NaN) raises ValueError) and is pinned as a control.
    control_counter = WalkCounter()
    control = wire(make_instance(build_browser(control_counter)),
                   "get_browser_items_at_path",
                   {"path": "drums/Kits", "limit": "abc"})
    assert control["total"] == 10 and control["next_offset"] is None
    assert len(control["items"]) == 10
    for params in ({"path": "drums/Kits", "limit": float("inf")},
                   {"path": "drums/Kits", "limit": float("-inf")},
                   {"path": "drums/Kits", "offset": float("inf")},
                   {"path": "drums/Kits", "offset": float("-inf")},
                   {"path": "drums/Kits", "limit": float("nan")},
                   {"path": "drums/Kits", "offset": float("nan")}):
        counter = WalkCounter()
        inst = make_instance(build_browser(counter))
        result = wire(inst, "get_browser_items_at_path", params)
        assert canonical(result) == canonical(control)


def test_items_paging_nonfinite_shares_the_default_cache_entry():
    # The fallback is the coerced default page itself, not an
    # error: an inf read keys the read cache by (100, 0) exactly
    # like the explicit-default read, so the two share one entry,
    # the repeat does not re-walk, and nothing is poisoned.
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    first = wire(inst, "get_browser_items_at_path",
                 {"path": "instruments", "limit": float("inf")})
    walks_after_first = counter.walks
    assert walks_after_first > 0
    result = wire(inst, "get_browser_items_at_path",
                  {"path": "instruments", "limit": 100, "offset": 0})
    assert canonical(result) == canonical(first)
    assert counter.walks == walks_after_first
    _, read_cache = inst._browser_cache_state()
    assert list(read_cache.keys()) == [("items", "instruments", 100, 0)]


def test_search_paging_nonfinite_values_fall_back_to_default():
    # Same escape on the search_browser paging coercion: limit and
    # offset alike. The result reports its effective paging, so the
    # default (limit 50, offset 0) is asserted directly as well as
    # by equality with the "abc" control page.
    control_counter = WalkCounter()
    control = wire(make_instance(build_browser(control_counter)),
                   "search_browser", {"query": "kit", "limit": "abc"})
    assert control["limit"] == 50 and control["offset"] == 0
    assert control["total"] == 11 and len(control["items"]) == 11
    for params in ({"query": "kit", "limit": float("inf")},
                   {"query": "kit", "limit": float("-inf")},
                   {"query": "kit", "offset": float("inf")},
                   {"query": "kit", "offset": float("-inf")},
                   {"query": "kit", "limit": float("nan")},
                   {"query": "kit", "offset": float("nan")}):
        counter = WalkCounter()
        inst = make_instance(build_browser(counter))
        result = wire(inst, "search_browser", params)
        assert result["limit"] == 50 and result["offset"] == 0
        assert canonical(result) == canonical(control)


def test_paging_finite_huge_values_still_clamp_not_fall_back():
    # The SI-236 fallback covers non-finite values only: a finite
    # huge limit still clamps to the cap (it does not fall back to
    # the default), and the "1e400" string form - unparseable as
    # an int - still falls back, both unchanged from base.
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    page = wire(inst, "search_browser", {"query": "kit", "limit": 1e308})
    assert page["limit"] == 200 and page["total"] == 11
    assert len(page["items"]) == 11
    page = wire(inst, "search_browser", {"query": "kit", "limit": "1e400"})
    assert page["limit"] == 50 and page["total"] == 11
    items = wire(inst, "get_browser_items_at_path",
                 {"path": "drums/Kits", "limit": 1e308})
    assert items["total"] == 10 and len(items["items"]) == 10
    _, read_cache = inst._browser_cache_state()
    assert ("items", "drums/Kits", 1000, 0) in read_cache


# ── Unhashable browser cache-key components refused (SI-237) ──────

def test_browser_tree_unhashable_category_type_is_refused_by_name():
    # The read-cache key is ("tree", category_type) on the raw
    # value, so a list/dict crashed the lookup with a raw
    # "unhashable type: ..." envelope before the handler ran.
    # Each shape runs on a fresh instance (cold cache): the
    # refusal is named, no cache entry is written, and a valid
    # read on the same instance still succeeds afterwards.
    for bad in ([1], {"a": 1}):
        counter = WalkCounter()
        inst = make_instance(build_browser(counter))
        response = inst._process_command(
            {"type": "get_browser_tree", "params": {"category_type": bad}})
        assert response["status"] == "error", response
        assert response["message"] == (
            "Invalid category_type: must be a string")
        assert response["result"] == {}
        uri_cache, read_cache = inst._browser_cache_state()
        assert len(uri_cache) == 0 and len(read_cache) == 0
        result = wire(inst, "get_browser_tree",
                      {"category_type": "instruments"})
        assert result["type"] == "instruments"


def test_items_at_path_unhashable_path_is_refused_by_name():
    # SI-75 coerced the paging values, but the key still embeds
    # the raw path in both its unpaged and paged forms, so a
    # list/dict path crashed the lookup the same way.
    for params in ({"path": [1]},
                   {"path": {"a": 1}},
                   {"path": [1], "limit": 5},
                   {"path": {"a": 1}, "offset": 0}):
        counter = WalkCounter()
        inst = make_instance(build_browser(counter))
        response = inst._process_command(
            {"type": "get_browser_items_at_path", "params": params})
        assert response["status"] == "error", response
        assert response["message"] == "Invalid path: must be a string"
        assert response["result"] == {}
        uri_cache, read_cache = inst._browser_cache_state()
        assert len(uri_cache) == 0 and len(read_cache) == 0
        result = wire(inst, "get_browser_items_at_path",
                      {"path": "instruments"})
        assert result["name"] == "Instruments"


def test_browser_item_unhashable_uri_is_refused_by_name():
    # The URI memo (_find_browser_item_by_uri) keys on the raw
    # uri, so a list/dict uri crashed the memo lookup with the
    # same raw TypeError envelope.
    for bad in ([1], {"a": 1}):
        counter = WalkCounter()
        inst = make_instance(build_browser(counter))
        response = inst._process_command(
            {"type": "get_browser_item", "params": {"uri": bad}})
        assert response["status"] == "error", response
        assert response["message"] == "Invalid uri: must be a string"
        assert response["result"] == {}
        uri_cache, read_cache = inst._browser_cache_state()
        assert len(uri_cache) == 0 and len(read_cache) == 0
        result = wire(inst, "get_browser_item", {"uri": "fake:808"})
        assert result["item"]["name"] == "808 Core Kit"


def test_browser_cache_key_hashable_scalars_are_unchanged():
    # The SI-237 refusal covers unhashable values only: hashable
    # non-string scalars keep their exact base behaviour - a
    # category_type of 42 succeeds softly with no categories, a
    # path of 42 still fails downstream in the handler (never
    # reaching a cache write), and a uri of 42 or None still
    # resolves to a plain not-found result.
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    result = wire(inst, "get_browser_tree", {"category_type": 42})
    assert result["type"] == 42 and result["categories"] == []
    result = wire(inst, "get_browser_tree", {"category_type": None})
    assert result["type"] is None and result["categories"] == []

    for bad_path, message in (
            (42, "'int' object has no attribute 'split'"),
            (None, "'NoneType' object has no attribute 'split'")):
        inst = make_instance(build_browser(WalkCounter()))
        response = inst._process_command(
            {"type": "get_browser_items_at_path",
             "params": {"path": bad_path}})
        assert response["status"] == "error", response
        assert response["message"] == message
        _, read_cache = inst._browser_cache_state()
        assert len(read_cache) == 0

    inst = make_instance(build_browser(WalkCounter()))
    result = wire(inst, "get_browser_item", {"uri": 42})
    assert result["found"] is False and result["uri"] == 42
    result = wire(inst, "get_browser_item", {"uri": None})
    assert result["found"] is False and result["uri"] is None


# ── SI-237 load routes + omitted scalar controls (SI-238) ─────────

def test_load_browser_item_unhashable_uri_is_refused_by_name():
    # load_browser_item reaches the URI memo through
    # _load_browser_item -> _find_browser_item_by_uri, the same
    # memo site SI-237 guarded for get_browser_item, so a list or
    # dict item_uri crashed at base with the same raw
    # "unhashable type: ..." envelope. Each shape runs on a fresh
    # instance with a loadable browser (cold caches): the refusal
    # is named, no cache entry is written, nothing is loaded or
    # walked, and a valid load on the same instance still
    # succeeds afterwards.
    for bad in ([1], {"a": 1}):
        counter = WalkCounter()
        browser = build_browser(counter, LoadableBrowser)
        inst = make_instance(browser)
        response = inst._process_command(
            {"type": "load_browser_item",
             "params": {"item_uri": bad, "track_index": 0}})
        assert response["status"] == "error", response
        assert response["message"] == "Invalid uri: must be a string"
        assert response["result"] == {}
        uri_cache, read_cache = inst._browser_cache_state()
        assert len(uri_cache) == 0 and len(read_cache) == 0
        assert browser.loaded == [] and counter.walks == 0
        result = wire(inst, "load_browser_item",
                      {"item_uri": "fake:808", "track_index": 0})
        assert result["loaded"] is True
        assert result["item_name"] == "808 Core Kit"
        assert [item.name for item in browser.loaded] == ["808 Core Kit"]


def test_load_instrument_or_effect_unhashable_uri_is_refused_by_name():
    # load_instrument_or_effect delegates to _load_browser_item,
    # so its uri param reaches the same memo site through the
    # same path; it is pinned directly rather than resting on
    # the delegation, for the same reason as load_browser_item
    # above.
    for bad in ([1], {"a": 1}):
        counter = WalkCounter()
        browser = build_browser(counter, LoadableBrowser)
        inst = make_instance(browser)
        response = inst._process_command(
            {"type": "load_instrument_or_effect",
             "params": {"uri": bad, "track_index": 0}})
        assert response["status"] == "error", response
        assert response["message"] == "Invalid uri: must be a string"
        assert response["result"] == {}
        uri_cache, read_cache = inst._browser_cache_state()
        assert len(uri_cache) == 0 and len(read_cache) == 0
        assert browser.loaded == [] and counter.walks == 0
        result = wire(inst, "load_instrument_or_effect",
                      {"uri": "fake:808", "track_index": 0})
        assert result["loaded"] is True
        assert result["item_name"] == "808 Core Kit"
        assert [item.name for item in browser.loaded] == ["808 Core Kit"]


def test_browser_cache_key_uri_bytes_are_unchanged():
    # The SI-237 refusal covers unhashable values only: a uri of
    # bytes is hashable and keeps its exact base behaviour on
    # every route that reaches the memo - get_browser_item
    # resolves it to a plain not-found result (a miss is never
    # cached), and both load routes fail downstream with the
    # handler's own not-found message, loading nothing.
    counter = WalkCounter()
    browser = build_browser(counter, LoadableBrowser)
    inst = make_instance(browser)
    result = wire(inst, "get_browser_item", {"uri": b"fake:808"})
    assert result == {"uri": b"fake:808", "path": None, "found": False}
    uri_cache, read_cache = inst._browser_cache_state()
    assert len(uri_cache) == 0 and len(read_cache) == 0

    for command, params in (
            ("load_browser_item",
             {"item_uri": b"fake:808", "track_index": 0}),
            ("load_instrument_or_effect",
             {"uri": b"fake:808", "track_index": 0})):
        counter = WalkCounter()
        browser = build_browser(counter, LoadableBrowser)
        inst = make_instance(browser)
        response = inst._process_command(
            {"type": command, "params": params})
        assert response["status"] == "error", response
        assert response["message"] == (
            "Browser item with URI 'b'fake:808'' not found")
        assert response["result"] == {}
        uri_cache, read_cache = inst._browser_cache_state()
        assert len(uri_cache) == 0 and len(read_cache) == 0
        assert browser.loaded == []


def test_browser_cache_key_category_type_tuple_is_unchanged():
    # A category_type tuple is hashable, so it passes the
    # SI-237 guard untouched and keeps its exact base behaviour:
    # a soft success with no categories, keyed in the read cache
    # by the raw tuple, so a repeat read is served from that
    # entry with no further walk and the identical result.
    for value in ((1, 2), ("a", "b")):
        counter = WalkCounter()
        inst = make_instance(build_browser(counter))
        result = wire(inst, "get_browser_tree",
                      {"category_type": value})
        assert result["type"] == value and result["categories"] == []
        assert result["available_categories"] == [
            "audio_effects", "drums", "instruments", "midi_effects",
            "sounds"]
        assert result["total_folders"] == 0
        uri_cache, read_cache = inst._browser_cache_state()
        assert len(uri_cache) == 0
        assert list(read_cache.keys()) == [("tree", value)]
        walks_after_first = counter.walks
        repeat = wire(inst, "get_browser_tree",
                      {"category_type": value})
        assert canonical(repeat) == canonical(result)
        assert counter.walks == walks_after_first


# ── search_browser ───────────────────────────────────────────────────

def test_search_matches_by_name_in_browser_order():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    result = wire(inst, "search_browser", {"query": "kit"})
    assert result["query"] == "kit"
    assert result["total"] == 11
    assert result["offset"] == 0 and result["limit"] == 50
    assert result["next_offset"] is None
    names = [entry["name"] for entry in result["items"]]
    assert names == (["Kits", "808 Core Kit", "909 Kit"]
                     + ["Kit %02d" % n for n in range(1, 9)])
    assert result["items"][0] == {
        "name": "Kits", "is_folder": True, "is_device": False,
        "is_loadable": False, "uri": "fake:kits", "path": "drums/Kits"}
    assert result["items"][1]["path"] == "drums/Kits/808 Core Kit"


def test_search_is_case_insensitive_and_matches_uris():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    assert wire(inst, "search_browser", {"query": "KIT"})["total"] == 11
    # URI-only match: the name "909 Kit" does not contain the query.
    result = wire(inst, "search_browser", {"query": "fake:909"})
    assert result["total"] == 1
    assert result["items"][0]["name"] == "909 Kit"
    assert result["items"][0]["path"] == "drums/Kits/909 Kit"


def test_search_paging_slices_equal_full_result():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    full = wire(inst, "search_browser", {"query": "kit", "limit": 200})
    stitched, offset, chain = [], 0, []
    while offset is not None:
        page = wire(inst, "search_browser",
                    {"query": "kit", "limit": 4, "offset": offset})
        assert page["total"] == full["total"] == 11
        stitched.extend(page["items"])
        offset = page["next_offset"]
        chain.append(offset)
    assert chain == [4, 8, None]
    assert stitched == full["items"]


def test_search_degenerate_limit_advances():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    full = wire(inst, "search_browser", {"query": "kit", "limit": 200})
    for bad_limit in (0, -5):
        page = wire(inst, "search_browser",
                    {"query": "kit", "limit": bad_limit, "offset": 0})
        # The effective page size is clamped to 1 and reported as
        # such; next_offset strictly advances instead of repeating 0.
        assert page["limit"] == 1
        assert page["total"] == 11
        assert page["items"] == full["items"][:1]
        assert page["next_offset"] == 1
    # A client blindly following next_offset with limit=0 terminates
    # and stitches exactly the full result, one per page.
    stitched, offset, pages = [], 0, 0
    while offset is not None:
        page = wire(inst, "search_browser",
                    {"query": "kit", "limit": 0, "offset": offset})
        stitched.extend(page["items"])
        offset = page["next_offset"]
        pages += 1
    assert pages == 11
    assert stitched == full["items"]


def test_search_pages_and_repeats_do_not_rewalk():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    wire(inst, "search_browser", {"query": "kit", "limit": 4, "offset": 0})
    walks = counter.walks
    assert walks > 0
    # Later pages and repeats slice the cached match list.
    wire(inst, "search_browser", {"query": "kit", "limit": 4, "offset": 4})
    wire(inst, "search_browser", {"query": "KIT", "limit": 4, "offset": 8})
    wire(inst, "search_browser", {"query": "kit"})
    assert counter.walks == walks


def test_search_cache_is_invalidated_by_load():
    counter = WalkCounter()
    browser = build_browser(counter, LoadableBrowser)
    inst = make_instance(browser)
    wire(inst, "search_browser", {"query": "kit"})
    walks = counter.walks
    wire(inst, "load_browser_item",
         {"item_uri": "fake:808", "track_index": 0})
    wire(inst, "search_browser", {"query": "kit"})
    assert counter.walks > walks


def test_search_requires_a_nonempty_query():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    for params in ({}, {"query": ""}, {"query": "   "}, {"query": 5}):
        response = inst._process_command(
            {"type": "search_browser", "params": params})
        assert response["status"] == "error", params
        assert "requires a non-empty 'query'" in response["message"]


def test_search_works_inside_execute_batch():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    result = wire(inst, "execute_batch", {"commands": [
        {"type": "search_browser", "params": {"query": "piano"}},
        {"type": "refresh_browser_cache", "params": {}},
    ]})
    assert result["summary"] == {
        "total": 2, "succeeded": 2, "failed": 0, "skipped": 0}
    search_item = result["results"][0]
    assert search_item["status"] == "success"
    assert search_item["result"]["total"] == 1
    assert search_item["result"]["items"][0]["name"] == "Grand Piano"
    assert search_item["result"]["items"][0]["path"] == (
        "instruments/Keys/Grand Piano")


def test_new_commands_advertised_at_bumped_version():
    assert script_module.SCRIPT_VERSION == "1.7.10"
    for name in ("search_browser", "refresh_browser_cache"):
        assert name in script_module.SCRIPT_CAPABILITIES
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    info = inst._get_script_info()
    assert "search_browser" in info["capabilities"]
    assert "refresh_browser_cache" in info["capabilities"]


# ── Logging diet ─────────────────────────────────────────────────────

def test_no_dir_dump_in_browser_logs():
    counter = WalkCounter()
    inst = make_instance(build_browser(counter))
    logs = []
    inst.log_message = logs.append
    tree = wire(inst, "get_browser_tree", {"category_type": "all"})
    wire(inst, "get_browser_items_at_path", {"path": "instruments"})
    assert not any("Available browser attributes" in m for m in logs)
    # The dir() data still flows into the result contract.
    assert tree["available_categories"] == [
        "audio_effects", "drums", "instruments", "midi_effects",
        "sounds"]


# ── Tool surface (subprocess, lean profile) ──────────────────────────

SNIPPET = r"""
import asyncio, json, os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
from MCP_Server import server

calls = []

class FakeConn:
    def send_command(self, command_type, params=None):
        calls.append([command_type, params])
        return {"ok": True}

server.get_ableton_connection = lambda: FakeConn()
out = {}
async def main():
    tools = await server.mcp.list_tools()
    out["schemas"] = {t.name: t.inputSchema for t in tools}
asyncio.run(main())

server.get_browser_items_at_path(None, "drums/Kits")
out["default_items"] = calls[-1]
server.get_browser_items_at_path(None, "drums/Kits", limit=5, offset=10)
out["paged_items"] = calls[-1]
server.search_browser(None, "kit")
out["search"] = calls[-1]
server.refresh_browser_cache(None)
out["refresh"] = calls[-1]

class OldScriptConn:
    def send_command(self, command_type, params=None):
        raise Exception("Unknown command: " + command_type)

server.get_ableton_connection = lambda: OldScriptConn()
out["old_search"] = server.search_browser(None, "kit")
out["old_refresh"] = server.refresh_browser_cache(None)
print(json.dumps(out))
"""


def test_tool_surface_and_old_script_behaviour():
    env = dict(os.environ, REPO_ROOT=str(REPO_ROOT),
               ABLETON_MCP_LEAN_PROFILE="1")
    proc = subprocess.run(
        [sys.executable, "-c", SNIPPET], capture_output=True, text=True,
        env=env, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    schemas = out["schemas"]
    # The default items call sends exactly the pre-paging params.
    assert out["default_items"] == [
        "get_browser_items_at_path", {"path": "drums/Kits"}]
    assert out["paged_items"] == [
        "get_browser_items_at_path",
        {"path": "drums/Kits", "limit": 5, "offset": 10}]
    items_props = schemas["get_browser_items_at_path"]["properties"]
    assert items_props["limit"]["default"] is None
    assert items_props["offset"]["default"] is None

    assert out["search"] == [
        "search_browser", {"query": "kit", "limit": 50, "offset": 0}]
    search_schema = schemas["search_browser"]
    assert search_schema["properties"]["limit"]["default"] == 50
    assert search_schema["properties"]["offset"]["default"] == 0
    assert "query" in search_schema["required"]

    assert out["refresh"] == ["refresh_browser_cache", {}]

    # A script without the new commands answers Unknown command, and
    # the tools surface it as the standard Error string - no raise.
    assert out["old_search"] == (
        "Error searching browser: Unknown command: search_browser")
    assert out["old_refresh"] == (
        "Error refreshing browser cache: "
        "Unknown command: refresh_browser_cache")
