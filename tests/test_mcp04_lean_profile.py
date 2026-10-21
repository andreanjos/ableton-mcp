"""MCP-04 tests: the lean Omnicorp package profile (ABLETON_MCP_LEAN_PROFILE).

The profile is chosen at import time from the environment, so each
profile is exercised in a fresh subprocess running the same observation
snippet, and the test compares the observations:

* default  — no switches, dataset consent stored as denied. Reference
  for the byte-identical pretty-JSON pin and the tool catalog.
* switches — ABLETON_MCP_DISABLE_TELEMETRY + ABLETON_MCP_DISABLE_DATASET.
  The reference for how the dataset tools respond when recording is
  disabled via the existing switches; the lean profile must mirror
  those responses exactly.
* lean     — ABLETON_MCP_LEAN_PROFILE=1. Must show: identical catalog,
  no dataset/telemetry/Supabase module imported on the control path,
  dataset tools answering exactly as in the switches profile, a
  modifying tool issuing exactly its own bridge command (no snapshot
  round trips), compact JSON carrying the identical value, and INFO
  logs free of parameter payloads.

No Ableton, no network: the bridge connection is a fake.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

DATASET_OFF = (
    "Dataset recording is off (telemetry disabled or user has not consented)."
)

SNIPPET = r"""
import asyncio
import hashlib
import inspect
import json
import logging
import os
import sys

sys.path.insert(0, os.environ["REPO_ROOT"])

obs = {}

import MCP_Server.server as server

obs["lean"] = server.LEAN_PROFILE


def side_plane_modules():
    return sorted(
        m for m in sys.modules
        if m == "supabase"
        or m.startswith("MCP_Server.dataset")
        or m.startswith("MCP_Server.telemetry")
    )


obs["side_plane_after_import"] = side_plane_modules()

# ---- tool catalog -------------------------------------------------------
tools = asyncio.run(server.mcp.list_tools())
obs["catalog"] = {t.name: t.model_dump() for t in tools}

# ---- fake bridge connection ---------------------------------------------
def big_session():
    tracks = []
    for i in range(24):
        tracks.append({
            "index": i, "name": "Track %d" % i, "type": "midi",
            "volume": 0.8, "panning": 0.0, "muted": False,
            "devices": [{"name": "Device %d" % j, "type": "instrument",
                         "parameters": [{"name": "P%d" % k, "value": k / 10.0,
                                         "min": 0.0, "max": 1.0}
                                        for k in range(8)]}
                        for j in range(3)],
            "clips": [{"index": c, "name": "Clip %d" % c, "length": 4.0,
                       "notes": [{"pitch": 36 + n, "start_time": n * 0.25,
                                  "duration": 0.25, "velocity": 100,
                                  "mute": False}
                                 for n in range(32)]}
                      for c in range(4)],
        })
    return {"tempo": 120.0, "time_signature": "4/4",
            "track_count": len(tracks), "tracks": tracks}


BIG = big_session()


class FakeConn:
    def __init__(self):
        self.calls = []

    def send_command(self, command_type, params=None, **kwargs):
        self.calls.append(command_type)
        if command_type == "get_session_info":
            return BIG
        if command_type == "create_midi_track":
            return {"name": "1-MIDI", "index": 0}
        if command_type == "get_script_info":
            return {"name": "AbletonMCP", "script_version": "1.7.4",
                    "protocol_version": 1, "capabilities": []}
        return {}


fake = FakeConn()
server.get_ableton_connection = lambda: fake


def call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


# ---- representative Live-facing tool: large result ----------------------
session_out = call(server.get_session_info, None)
obs["session_info_len"] = len(session_out.encode("utf-8"))
obs["session_info_value_sha"] = hashlib.sha256(
    json.dumps(json.loads(session_out), sort_keys=True).encode("utf-8")
).hexdigest()
obs["session_info_is_pretty"] = session_out == json.dumps(BIG, indent=2)
obs["session_info_is_compact"] = (
    session_out == json.dumps(BIG, separators=(",", ":"))
)

# ---- a modifying tool: exactly its own bridge command -------------------
fake.calls.clear()
obs["create_out"] = call(server.create_midi_track, None)
obs["create_calls"] = list(fake.calls)
obs["create_is_coroutine_fn"] = inspect.iscoroutinefunction(
    server.create_midi_track)

# ---- dataset tools -------------------------------------------------------
obs["dataset"] = {
    "submit_intent": call(server.submit_intent, None, "make it bigger"),
    "rate_last_action": call(server.rate_last_action, None, "better"),
    "prefer_candidate": call(
        server.prefer_candidate, None, "a-id", "b-id", "a"),
    "reject_last_action": call(server.reject_last_action, None, "nope"),
    "record_audition": call(
        server.record_audition, None, "ableton:drums:808", True),
}
obs["side_plane_after_battery"] = side_plane_modules()

# ---- logging diet: a representative send_command -------------------------
records = []


class _Capture(logging.Handler):
    def emit(self, record):
        records.append((record.levelname, record.getMessage()))


logging.getLogger("AbletonMCPServer").addHandler(_Capture())


class FakeSock:
    def __init__(self, response_obj):
        self._data = json.dumps(response_obj).encode("utf-8")
        self._i = 0
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        if self._i >= len(self._data):
            return b""
        chunk = self._data[self._i:self._i + 5]
        self._i += len(chunk)
        return chunk


conn = server.AbletonConnection("127.0.0.1", 9877)
conn.sock = FakeSock({"status": "success", "result": {"ok": True}})
conn.send_command("set_track_name",
                  {"track_index": 0, "name": "ZZZ_MARKER_VALUE"})
obs["info_logs"] = [m for lvl, m in records if lvl == "INFO"]

# ---- lifespan (lean profile only): handshake yes, side plane no ---------
if os.environ.get("RUN_LIFESPAN"):
    async def _cycle():
        async with server.server_lifespan(server.mcp) as ctx:
            return ctx

    cycle = asyncio.run(_cycle())
    info = (cycle or {}).get("script_info") or {}
    obs["lifespan_script_name"] = info.get("name")
    obs["side_plane_after_lifespan"] = side_plane_modules()

# ---- consent tool (switches + lean profiles only; runs last because ----
# ---- recording an answer imports the consent store in both)          ----
if os.environ.get("RUN_CONSENT"):
    obs["consent"] = {
        "decline": call(server.set_dataset_consent, None, False, "no thanks"),
        "grant": call(server.set_dataset_consent, None, True, "yes please"),
    }

print("OBS:" + json.dumps(obs))
"""


def _run_profile(tmp_root: Path, name: str, env_extra: dict,
                 seed_consent: dict | None = None) -> dict:
    state_dir = tmp_root / (name + "-state")
    state_dir.mkdir(parents=True)
    if seed_consent is not None:
        (state_dir / "consent.json").write_text(json.dumps(seed_consent))
    home = tmp_root / (name + "-home")
    home.mkdir()

    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("ABLETON_MCP_")
        and k not in ("DO_NOT_TRACK", "DISABLE_TELEMETRY",
                      "MCP_DISABLE_TELEMETRY")
    }
    env.update({
        "REPO_ROOT": str(REPO_ROOT),
        "ABLETON_MCP_STATE_DIR": str(state_dir),
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.update(env_extra)

    proc = subprocess.run(
        [sys.executable, "-c", SNIPPET],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.startswith("OBS:")]
    assert lines, proc.stdout + proc.stderr
    return json.loads(lines[-1][len("OBS:"):])


@pytest.fixture(scope="module")
def default_profile(tmp_path_factory):
    return _run_profile(
        tmp_path_factory.mktemp("mcp04"), "default", {},
        seed_consent={"state": "denied", "consent_version": 1},
    )


@pytest.fixture(scope="module")
def switches_profile(tmp_path_factory):
    return _run_profile(
        tmp_path_factory.mktemp("mcp04"), "switches",
        {"ABLETON_MCP_DISABLE_TELEMETRY": "1",
         "ABLETON_MCP_DISABLE_DATASET": "1",
         "RUN_CONSENT": "1"},
    )


@pytest.fixture(scope="module")
def lean_profile(tmp_path_factory):
    return _run_profile(
        tmp_path_factory.mktemp("mcp04"), "lean",
        {"ABLETON_MCP_LEAN_PROFILE": "1",
         "RUN_CONSENT": "1",
         "RUN_LIFESPAN": "1"},
    )


# ---------------------------------------------------------- default profile

def test_default_profile_is_not_lean_and_wires_side_plane(default_profile):
    assert default_profile["lean"] is False
    # The default profile still imports the side plane at module load:
    # the lean profile's absence of wiring is not a default change.
    assert "MCP_Server.dataset.recorder" in default_profile[
        "side_plane_after_import"]
    assert "MCP_Server.telemetry" in default_profile[
        "side_plane_after_import"]


def test_default_profile_result_is_byte_identical_pretty_json(default_profile):
    # The explicit pin: a representative tool result is exactly the
    # historical json.dumps(..., indent=2) output.
    assert default_profile["session_info_is_pretty"] is True
    assert default_profile["session_info_is_compact"] is False


def test_default_profile_keeps_full_params_info_logging(default_profile):
    # Control for the lean logging diet: by default the send line still
    # carries the parameter payload.
    assert any("ZZZ_MARKER_VALUE" in m for m in default_profile["info_logs"])


def test_default_profile_wraps_tools(default_profile):
    # The trajectory decorator turns sync tool bodies into coroutines in
    # the default profile; the lean contrast is asserted below.
    assert default_profile["create_is_coroutine_fn"] is True


# ------------------------------------------------------------- lean profile

def test_lean_catalog_identical_to_default(default_profile, lean_profile):
    assert len(lean_profile["catalog"]) == 37
    # Names, descriptions and parameter schemas all identical.
    assert lean_profile["catalog"] == default_profile["catalog"]


def test_lean_imports_no_side_plane_modules(lean_profile):
    assert lean_profile["lean"] is True
    assert lean_profile["side_plane_after_import"] == []
    # Still true after the catalog, tool battery and dataset tools ran:
    # nothing on the control path imports the recorder/telemetry stack.
    assert lean_profile["side_plane_after_battery"] == []


def test_lean_lifespan_handshakes_without_side_plane(lean_profile):
    # Startup still performs the script handshake...
    assert lean_profile["lifespan_script_name"] == "AbletonMCP"
    # ...but the dataset/poller startup steps never ran.
    assert lean_profile["side_plane_after_lifespan"] == []


def test_lean_dataset_tools_match_disabled_switches(switches_profile,
                                                     lean_profile):
    assert lean_profile["dataset"] == switches_profile["dataset"]
    for text in lean_profile["dataset"].values():
        assert text == DATASET_OFF


def test_lean_consent_tool_matches_disabled_switches(switches_profile,
                                                     lean_profile):
    # Including a grant: under lean it is recorded but recording stays
    # unavailable, exactly as with the disable switches set.
    assert lean_profile["consent"] == switches_profile["consent"]
    assert lean_profile["consent"]["decline"].startswith(
        "Recorded: dataset contribution declined.")
    assert "recording is unavailable" in lean_profile["consent"]["grant"]


def test_lean_modifying_tool_issues_exactly_its_bridge_command(lean_profile):
    # create_midi_track is in the trajectory decorator's MODIFYING_TOOLS:
    # with recording on it would add pre/post snapshot round trips.
    # Under lean the tool is the plain function and the only bridge
    # command is the create itself.
    assert lean_profile["create_calls"] == ["create_midi_track"]
    assert lean_profile["create_out"] == "Created new MIDI track: 1-MIDI"
    assert lean_profile["create_is_coroutine_fn"] is False


def test_lean_compact_json_same_value_smaller(default_profile, lean_profile):
    assert lean_profile["session_info_is_compact"] is True
    assert lean_profile["session_info_is_pretty"] is False
    # Same JSON value as the default profile's pretty output...
    assert (lean_profile["session_info_value_sha"]
            == default_profile["session_info_value_sha"])
    # ...at a fraction of the bytes (measured ~0.41x on this payload;
    # 0.6 leaves margin while still pinning the diet).
    assert (lean_profile["session_info_len"]
            <= 0.6 * default_profile["session_info_len"])


def test_lean_info_logs_carry_no_param_payload(lean_profile):
    info = lean_profile["info_logs"]
    assert any("set_track_name" in m for m in info)
    assert not any("ZZZ_MARKER_VALUE" in m for m in info)
