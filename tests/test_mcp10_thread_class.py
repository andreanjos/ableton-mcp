"""MCP-10: dispatch thread-class audit + browser LRU race hardening.

Pins:

* COMMAND_TABLE is the dispatcher's single source: its names are
  exactly the command set derived from _process_command's AST (the
  MCP-01 derivation), in sorted order, so the derived
  SCRIPT_CAPABILITIES / handshake list is byte-identical to the
  historical hand-maintained one;
* the shared scheduled branch's membership is the pre-MCP-10 literal
  23-name list, derived from the table minus the four bespoke
  "main" commands - the refactor changed no membership;
* observed dispatch agrees with the table for every command: each
  "main" command costs exactly one schedule_message hop through an
  instrumented scheduler, each "handler" command costs none;
* the batch rule: execute_batch is one main-thread task even when
  every item in it is "handler" class - batches are never split;
* the two dead browser branches (get_browser_categories /
  get_browser_items, whose handlers do not exist) keep today's
  behaviour exactly: an immediate error envelope, no scheduling;
* the browser LRU helpers survive the MCP-07 review's race: a
  concurrent clear/evict landing between a lookup (or store) and its
  move_to_end touch can no longer raise KeyError out of
  _browser_cache_get / _browser_cache_put, and cache semantics
  (bounds, LRU order, invalidation) are unchanged;
* script version lockstep at 1.7.10 (script + installer constant).
"""

import ast
import sys
import threading
from collections import OrderedDict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.test_remote_script_bridge import _load_script_module  # noqa: E402

script_module = _load_script_module()
AbletonMCP = script_module.AbletonMCP
BUNDLED_SCRIPT = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")


# ------------------------------------------------------------ harness

class RecordingScheduler:
    """schedule_message stand-in: records the hop, runs the task
    inline as Live's main thread would."""

    def __init__(self):
        self.calls = 0

    def __call__(self, delay, fn):
        self.calls += 1
        fn()


_STUBBED_IMPLS = [
    "_create_midi_track", "_create_audio_track", "_set_track_name",
    "_create_clip", "_create_audio_clip", "_add_notes_to_clip",
    "_clear_notes_from_clip", "_set_clip_name",
    "_set_arrangement_clip_name", "_set_tempo", "_fire_clip",
    "_stop_clip", "_delete_clip", "_start_playback", "_stop_playback",
    "_load_instrument_or_effect", "_load_browser_item",
    "_switch_to_arrangement_view", "_set_current_song_time",
    "_duplicate_session_clip_to_arrangement", "_map_rack_magnitude",
    "_inspect_rack", "_create_locator",
    "_set_device_parameter", "_set_passive_listeners",
    "_get_session_info", "_get_track_info", "_get_browser_item",
    "get_browser_tree", "get_browser_items_at_path", "_search_browser",
    "_get_arrangement_clips", "_get_clip_notes",
    "_get_device_parameters", "_get_session_snapshot",
    "_drain_passive_events",
]


def make_instance(stub=False):
    inst = AbletonMCP.__new__(AbletonMCP)
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    scheduler = RecordingScheduler()
    inst.schedule_message = scheduler
    if stub:
        for attr in _STUBBED_IMPLS:
            setattr(inst, attr, lambda *a, **k: {"stubbed": True})
        inst._apply_parameter_item = lambda *a, **k: {
            "status": "success", "result": {}}
    return inst, scheduler


_PARAMS = {
    "execute_batch": {"commands": [{"type": "get_script_info",
                                    "params": {}}]},
    "set_device_parameters": {
        "track_index": 0, "device_index": 0,
        "parameters": [{"parameter_index": 0, "value": 0.5}]},
    "set_passive_listeners": {"enabled": True},
}


def _dispatcher_commands():
    """The MCP-01 derivation: every string compared against
    command_type in _process_command, from the bundled script's AST."""
    tree = ast.parse(BUNDLED_SCRIPT.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "AbletonMCP")
    method = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_process_command")
    caps = set()
    for node in ast.walk(method):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "command_type"):
            for other in node.comparators:
                values = (other.elts if isinstance(other, (ast.List, ast.Tuple))
                          else [other])
                caps.update(v.value for v in values
                            if isinstance(v, ast.Constant)
                            and isinstance(v.value, str))
    return caps


# ------------------------------------------------------- the table

def test_table_is_the_complete_dispatcher_surface():
    derived = _dispatcher_commands()
    names = [name for name, _cls in script_module.COMMAND_TABLE]
    assert len(derived) == 42
    assert len(names) == len(set(names)) == 42
    assert set(names) == derived
    # Table order is the historical sorted order, so the derived
    # capabilities list is byte-identical to the hand-maintained one.
    assert names == sorted(derived)
    assert script_module.SCRIPT_CAPABILITIES == names
    assert script_module.COMMAND_THREAD_CLASS == dict(
        script_module.COMMAND_TABLE)
    assert set(script_module.COMMAND_THREAD_CLASS.values()) == {
        script_module.THREAD_MAIN, script_module.THREAD_HANDLER}


def test_handshake_capabilities_come_from_the_table():
    inst, _scheduler = make_instance()
    info = inst._get_script_info()
    assert info["capabilities"] == [
        name for name, _cls in script_module.COMMAND_TABLE]
    assert info["capabilities"] == script_module.SCRIPT_CAPABILITIES


# The 23 names of the pre-MCP-10 literal scheduled-branch list, and
# the four bespoke "main" branches, as they stood at fork main
# 735ad64 - the refactor must not have moved a single command.
_PRE_MCP10_SCHEDULED_GROUP = {
    "create_midi_track", "create_audio_track", "set_track_name",
    "create_clip", "create_audio_clip", "add_notes_to_clip",
    "set_clip_name", "set_arrangement_clip_name", "delete_clip",
    "clear_notes_from_clip", "set_tempo", "fire_clip", "stop_clip",
    "start_playback", "stop_playback", "load_browser_item",
    "load_instrument_or_effect", "switch_to_arrangement_view",
    "set_current_song_time", "duplicate_session_clip_to_arrangement",
    "map_rack_magnitude", "inspect_rack", "create_locator",
}
_BESPOKE_MAIN = {
    "execute_batch", "set_device_parameter", "set_device_parameters",
    "set_passive_listeners",
}


def test_scheduled_group_membership_is_unchanged():
    assert script_module._SCHEDULED_GROUP_COMMANDS == (
        _PRE_MCP10_SCHEDULED_GROUP)
    assert script_module._BESPOKE_MAIN_THREAD_COMMANDS == _BESPOKE_MAIN
    main = {name for name, cls in script_module.COMMAND_TABLE
            if cls == script_module.THREAD_MAIN}
    handler = {name for name, cls in script_module.COMMAND_TABLE
               if cls == script_module.THREAD_HANDLER}
    assert main == _PRE_MCP10_SCHEDULED_GROUP | _BESPOKE_MAIN
    assert len(main) == 27
    assert len(handler) == 15
    assert not (main & handler)


# ------------------------------------------- observed dispatch agrees

_MATRIX_COMMANDS = [
    name for name, _cls in script_module.COMMAND_TABLE
    if name not in ("get_browser_categories", "get_browser_items")]


@pytest.mark.parametrize("command", _MATRIX_COMMANDS)
def test_dispatch_scheduling_matches_the_table(command):
    inst, scheduler = make_instance(stub=True)
    response = inst._process_command(
        {"type": command, "params": _PARAMS.get(command, {})})
    expected = (1 if script_module.COMMAND_THREAD_CLASS[command]
                == script_module.THREAD_MAIN else 0)
    assert scheduler.calls == expected, command
    assert response["status"] == "success", (command, response)


@pytest.mark.parametrize("command,impl", [
    ("get_browser_categories", "_get_browser_categories"),
    ("get_browser_items", "_get_browser_items"),
])
def test_dead_browser_branches_keep_todays_behaviour(command, impl):
    # The handlers do not exist; the branches raise AttributeError
    # before any state is touched. That is today's behaviour and the
    # table records the commands as "handler" class - no scheduling,
    # an immediate error envelope naming the missing handler.
    assert not hasattr(AbletonMCP, impl)
    inst, scheduler = make_instance()
    response = inst._process_command({"type": command, "params": {}})
    assert response["status"] == "error"
    assert impl in response["message"]
    assert scheduler.calls == 0


def test_batch_of_handler_items_is_still_one_main_thread_task():
    # Batch rule: a batch is always ONE main-thread task, even when
    # every item in it is "handler" class - never split by item class.
    inst, scheduler = make_instance()
    items = [{"type": "get_script_info", "params": {}} for _ in range(3)]
    response = inst._process_command(
        {"type": "execute_batch", "params": {"commands": items}})
    assert scheduler.calls == 1
    assert response["status"] == "success"
    assert response["result"]["summary"] == {
        "total": 3, "succeeded": 3, "failed": 0, "skipped": 0}


# ------------------------------------------------- LRU race (rider)

class _ClearOnGet(OrderedDict):
    """The entry is cleared (a concurrent refresh/load) between the
    lookup and the LRU touch."""

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.clear()
        return value


class _EvictOnGet(OrderedDict):
    """The entry is evicted between the lookup and the LRU touch."""

    def __getitem__(self, key):
        value = super().__getitem__(key)
        super().__delitem__(key)
        return value


class _ClearOnSet(OrderedDict):
    """The cache is cleared between the store and the LRU touch."""

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.clear()


class _PopItemRaises(OrderedDict):
    """Eviction finds the cache already emptied by another thread."""

    def popitem(self, last=True):
        raise KeyError("already gone")


def test_cache_get_survives_concurrent_clear():
    cache = _ClearOnGet([("a", 1), ("b", 2)])
    # Pre-MCP-10 this raised KeyError from move_to_end.
    assert AbletonMCP._browser_cache_get(cache, "a") == 1
    assert len(cache) == 0  # the clear stands; the hit was already read


def test_cache_get_survives_concurrent_evict():
    cache = _EvictOnGet([("a", 1), ("b", 2)])
    assert AbletonMCP._browser_cache_get(cache, "a") == 1
    assert list(cache.keys()) == ["b"]


def test_cache_put_survives_concurrent_clear():
    cache = _ClearOnSet()
    AbletonMCP._browser_cache_put(cache, "k", "v", 10)  # must not raise
    assert len(cache) == 0  # the concurrent clear wins


def test_cache_put_eviction_survives_concurrent_emptying():
    cache = _PopItemRaises()
    for i in range(3):
        OrderedDict.__setitem__(cache, "k%d" % i, i)
    AbletonMCP._browser_cache_put(cache, "new", 99, 2)  # must not raise


def test_cache_lru_semantics_unchanged():
    cache = OrderedDict()
    get = AbletonMCP._browser_cache_get
    put = AbletonMCP._browser_cache_put
    put(cache, "a", 1, 2)
    put(cache, "b", 2, 2)
    assert get(cache, "a") == 1     # touch: b is now least-recent
    put(cache, "c", 3, 2)           # evicts b, not a
    assert get(cache, "b") is None
    assert get(cache, "a") == 1
    assert get(cache, "c") == 3
    assert get(cache, "missing") is None
    assert len(cache) == 2


def test_browser_caches_survive_concurrent_use():
    inst, _scheduler = make_instance()
    uri_cache, read_cache = inst._browser_cache_state()
    errors = []

    def worker(n):
        try:
            for i in range(300):
                key = "k%d" % ((i + n) % 16)
                AbletonMCP._browser_cache_put(read_cache, key, i, 8)
                AbletonMCP._browser_cache_get(read_cache, key)
                AbletonMCP._browser_cache_put(uri_cache, key, i, 8)
                AbletonMCP._browser_cache_get(uri_cache, key)
                if i % 50 == 0:
                    inst._invalidate_browser_caches()
        except Exception as e:  # noqa: BLE001 - collected and asserted
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,))
               for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    for cache in (uri_cache, read_cache):
        assert len(cache) <= 8
        # Snapshot first: the LRU get reorders the dict, which must
        # not happen while an items() view is being iterated.
        for key, value in list(cache.items()):
            assert value is not None
            assert AbletonMCP._browser_cache_get(cache, key) == value


# ------------------------------------------------------------ lockstep

def test_script_version_lockstep():
    assert script_module.SCRIPT_VERSION == "1.7.10"
    from MCP_Server.remote_script_install import (
        EXPECTED_REMOTE_SCRIPT_VERSION)
    assert EXPECTED_REMOTE_SCRIPT_VERSION == script_module.SCRIPT_VERSION
