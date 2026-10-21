<div align="center">

# Ableton MCP

**Connect Ableton Live to Claude AI**

Prompt-assisted music production, end-to-end track creation, and Live session and arrangement manipulation — driven by AI.

[![PyPI Version](https://img.shields.io/pypi/v/ableton-mcp?color=blue)](https://pypi.org/project/ableton-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/JK4hNKGprW)

[**Setup Video**](https://youtu.be/iJWJqyVuPS8) · [**Discord**](https://discord.gg/JK4hNKGprW) · [**Issues**](https://github.com/ahujasid/ableton-mcp/issues)

</div>

---

## Quickstart

Three steps: install `uv`, point your MCP client at the server, install the Ableton Remote Script.

**1. Install uv**

```bash
# macOS
brew install uv
```

Otherwise, install from [uv's official website](https://docs.astral.sh/uv/getting-started/installation/).

> **Warning:** Do not proceed before installing uv.

**2. Add the MCP server to your client**

<details open>
<summary><b>Claude Desktop</b> — Settings → Developer → Edit Config</summary>

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": [
                "ableton-mcp"
            ]
        }
    }
}
```
</details>

<details>
<summary><b>Cursor</b> — Settings → MCP</summary>

Paste this as a command:

```
uvx ableton-mcp
```
</details>

> **Warning:** Only run one instance of the MCP server (either on Cursor or Claude Desktop), not both.

**3. Install the Ableton Remote Script**

```bash
uvx --from ableton-mcp ableton-mcp-install-script
uvx --from ableton-mcp ableton-mcp-install-script --list-targets   # preview target folders first
```

**4. Connect**

1. Launch Ableton Live
2. Go to **Settings/Preferences → Link, Tempo & MIDI**
3. In the **Control Surface** dropdown, select **AbletonMCP**
4. Set **Input** and **Output** to **None**

That's it — ask Claude to build something.

---

## Table of Contents

- [Quickstart](#quickstart)
- [Features](#features)
- [Components](#components)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Claude for Desktop Integration](#claude-for-desktop-integration)
  - [Cursor Integration](#cursor-integration)
  - [Installing the Ableton Remote Script](#installing-the-ableton-remote-script)
- [Usage](#usage)
  - [Starting the Connection](#starting-the-connection)
  - [Using with Claude](#using-with-claude)
  - [Capabilities](#capabilities)
  - [Example Commands](#example-commands)
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)
- [Limitations & Security Considerations](#limitations--security-considerations)
- [Telemetry](#telemetry)
- [Join the Community](#join-the-community)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)

---

## Features

| | |
|---|---|
| **Two-way communication** | Connect Claude AI to Ableton Live through a socket-based server |
| **Track manipulation** | Create, modify, and manipulate MIDI and audio tracks |
| **Instrument and effect selection** | Claude can access and load the right instruments, effects and sounds from Ableton's library |
| **Clip creation** | Create and edit MIDI clips with notes |
| **Arrangement view composition** | Build full songs autonomously in Arrangement View, including sections like intro, buildup, drop, breakdown, and outro |
| **Session control** | Start and stop playback, fire clips, and control transport across Session View and Arrangement View |
| **Anonymous telemetry** | Usage tracking to help improve the tool (can be disabled) |

## Components

The system consists of two main components:

1. **Ableton Remote Script** (`Ableton_Remote_Script/__init__.py`) — a MIDI Remote Script for Ableton Live that creates a socket server to receive and execute commands
2. **MCP Server** (`server.py`) — a Python server that implements the Model Context Protocol and connects to the Ableton Remote Script

---

## Installation

### Prerequisites

- **Ableton Live** 10 or newer
- **Python** 3.8 or newer
- **uv** package manager

If you're on Mac, please install uv as:

```
brew install uv
```

Otherwise, install from [uv's official website](https://docs.astral.sh/uv/getting-started/installation/)

> **Warning:** Do not proceed before installing uv.

### Claude for Desktop Integration

[Follow along with the setup instructions video](https://youtu.be/iJWJqyVuPS8)

Go to **Claude → Settings → Developer → Edit Config → `claude_desktop_config.json`** to include the following:

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": [
                "ableton-mcp"
            ]
        }
    }
}
```

### Cursor Integration

Run ableton-mcp without installing it permanently through uvx. Go to **Cursor Settings → MCP** and paste this as a command:

```
uvx ableton-mcp
```

> **Warning:** Only run one instance of the MCP server (either on Cursor or Claude Desktop), not both.

### Claude Code Integration

In the terminal, run:

```
claude mcp add AbletonMCP uvx ableton-mcp
```

### Installing the Ableton Remote Script

[Follow along with the setup instructions video](https://youtu.be/iJWJqyVuPS8)

Install the Remote Script with:

```bash
uvx --from ableton-mcp ableton-mcp-install-script
uvx --from ableton-mcp ableton-mcp-install-script --list-targets   # preview target folders first
```

> If you installed the package with `pip` or `pipx`, the command is on your PATH directly — just run `ableton-mcp-install-script`.

This copies the matching Remote Script into your Ableton **User Library**'s `Remote Scripts` folder — the location Live (10.1.13+) scans for third-party control surface scripts. The installer reads the User Library location from Live's `Library.cfg`, falling back to the default (`~/Music/Ableton/User Library` on macOS, `Documents\Ableton\User Library` on Windows). If a different version of the script is already there, the existing file is backed up to `__init__.py.bak` before being replaced.

If your User Library lives somewhere non-standard and isn't detected, point the installer at it directly:

```bash
uvx --from ableton-mcp ableton-mcp-install-script --target "/path/to/User Library/Remote Scripts"
```

> The legacy `Preferences/User Remote Scripts` folder (used for instant-mapping configs, not Python control surfaces) is no longer targeted by default; pass `--legacy` if you need it for an old Live version.

Then **restart Ableton** (or re-select the AbletonMCP control surface) so Live loads it. Re-run the command after upgrading the package — the server logs a warning when the loaded script version doesn't match what it expects.

> **Note:** The server does **not** install the script on startup. Writing into Ableton's preferences directory is an explicit action, not a side effect of launching a server.

**First-time Ableton setup:**

1. Run `uvx --from ableton-mcp ableton-mcp-install-script`
2. Launch Ableton Live
3. Go to **Settings/Preferences → Link, Tempo & MIDI**
4. In the **Control Surface** dropdown, select **AbletonMCP**
5. Set **Input** and **Output** to **None**

<details>
<summary><b>Manual fallback locations (User Library → Remote Scripts)</b></summary>

- **macOS:** `~/Music/Ableton/User Library/Remote Scripts/AbletonMCP/`
- **Windows:** `C:\Users\[Username]\Documents\Ableton\User Library\Remote Scripts\AbletonMCP\`

If you've moved your User Library, use its actual location (shown in Live under **Preferences → Library → Location of User Library**), and create the `Remote Scripts` folder inside it if it doesn't exist yet.
</details>

The MCP server and Remote Script share a version handshake (`get_remote_script_info`). If they diverge, newer tools degrade gracefully until Live is restarted.

---

## Usage

### Starting the Connection

1. Ensure the Ableton Remote Script is loaded in Ableton Live
2. Make sure the MCP server is configured in Claude Desktop or Cursor
3. The connection should be established automatically when you interact with Claude

### Using with Claude

Once the config file has been set on Claude, and the remote script is running in Ableton, you will see a hammer icon with tools for the Ableton MCP.

### Capabilities

- Get session and track information
- Create and modify MIDI and audio tracks
- Create full song arrangements from start to finish in Arrangement View
- Create, edit, and trigger clips
- Control playback
- Load instruments and effects from Ableton's browser
- Add notes to MIDI clips
- Change tempo and other session parameters

### Example Commands

Here are some examples of what you can ask Claude to do:

| Prompt | Demo |
|---|---|
| *"Create an 80s synthwave track"* | [Watch](https://youtu.be/VH9g66e42XA) |
| *"Create a Metro Boomin style hip-hop beat"* | |
| *"Create a full arrangement with an intro, buildup, drop, breakdown, and outro"* | |
| *"Create a new MIDI track with a synth bass instrument"* | |
| *"Add reverb to my drums"* | |
| *"Create a 4-bar MIDI clip with a simple melody"* | |
| *"Get information about the current Ableton session"* | |
| *"Load a 808 drum rack into the selected track"* | |
| *"Add a jazz chord progression to the clip in track 1"* | |
| *"Set the tempo to 120 BPM"* | |
| *"Play the clip in track 2"* | |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **Connection issues** | Make sure the Ableton Remote Script is loaded, and the MCP server is configured on Claude |
| **Timeout errors** | Try simplifying your requests or breaking them into smaller steps |
| **Have you tried turning it off and on again?** | If you're still having connection errors, try restarting both Claude and Ableton Live |

## Technical Details

### Communication Protocol

The system uses a simple JSON-based protocol over TCP sockets:

- **Commands** are sent as JSON objects with a `type` and optional `params`
- **Responses** are JSON objects with a `status` and `result` or `message`

### Per-command timing (opt-in)

A command that carries an integer `request_id` (0 to 9007199254740991), or
`"timing": true`, gets two additions on its response envelope: the echoed
`request_id` and an `agent0_timing` block with `dispatch_ms` (total time in
the Remote Script dispatcher), `queue_ms` (wait for the Live main thread;
`null` for commands that run directly) and `execution_ms` (time on the main
thread; `null` likewise). Commands that do not opt in are answered exactly
as before - no extra keys, no timing overhead. The handshake
(`get_script_info`) advertises the complete dispatcher-derived capability
list and the `agent0_request_ids` / `agent0_capabilities_complete` markers.
From Python, `AbletonConnection.send_command(type, params, request_id=n)`
attaches an id, verifies the echo, and exposes the timing block as
`connection.last_timing`.

### Batch execution (execute_batch)

`execute_batch` runs an ordered list of commands in one socket round
trip and one main-thread scheduling - the fix for composite work that
otherwise costs one exchange (and one main-thread queue wait) per
command. The request params are `{"commands": [{"type": ...,
"params": {...}}, ...], "stop_on_error": false}`. The result is
`{"results": [...], "summary": {"total": n, "succeeded": n, "failed":
n, "skipped": n}}`, where each entry of `results` is exactly the
response envelope a direct call of that command would have produced.
With `"stop_on_error": true`, items after the first failure are not
run and are reported as `{"status": "skipped", ...}` - never silently
dropped. A batch inside a batch is rejected as a per-item error.

Batching is a sequencing primitive, **not a transaction**: items
execute in order and a failed item does not roll back the items
before it. From Python, `AbletonConnection.send_batch([{"type": ...,
"params": {...}}, ...])` performs the exchange and returns the batch
result; per-item failures are reported in `results`, not raised.
Requires Remote Script 1.7.3+ (the handshake capabilities advertise
`execute_batch`). The timing opt-in above applies to the batch as a
whole and, per item, to each item's own envelope.

### Framing v2 (length-prefixed, negotiated)

Protocol v1 sends each request and response as one raw JSON object and
finds the message boundary by re-parsing the accumulated buffer after
every chunk - quadratic in the message size (~61x the one-shot parse
cost at 1 MiB, ~133x at 2 MiB, measured on both endpoints; see the
benchmark below). Framing v2 sends the same JSON object as one
length-prefixed frame - a 4-byte big-endian payload length plus the
UTF-8 payload, at most the same 16 MiB (exactly 16 MiB included) -
decoded incrementally, with a
native `request_id` in every payload for exact correlation (the
MCP-01 echo/timing mechanism, not a second one).

Framing is negotiated, never assumed. The Remote Script (1.7.4+)
advertises `agent0_framing: ["v1", "v2"]` in the `get_script_info`
handshake; the MCP server switches a connection to framed exchanges
only after that advertisement, and the script decides each message's
framing at its boundary and answers in kind - so a session opens with
the v1 handshake and frames everything after it, while a v1 client
sees byte-identical v1 behaviour. A frame announcing more than
16 MiB, or a malformed payload, is answered with a framed error and a
closed connection, never a hang.
From Python nothing changes: `send_command` / `send_batch` frame
transparently once negotiated, and `last_timing` is populated on
every v2 exchange.

### Projected state reads

`get_session_snapshot` and `get_track_info` accept opt-in projection
parameters: the caller names the sections it actually needs, and the
Remote Script does not serialise -- or even compute -- the rest. Every
parameter is optional and defaults to the pre-projection behaviour, so
an unprojected call returns exactly the result it always has, and a v1
client that never sends the new params is unaffected.

Snapshot parameters (all optional):

- `track_indices`: restrict the `tracks` list to these song indices
  (invalid entries are ignored, song order is preserved).
- `include_scenes`, `include_return_tracks`, `include_master_track`,
  `include_cue_points`: set one to `false` to omit that top-level
  section from the result entirely.
- `include_clip_slots`, `include_devices`, `include_arrangement_clips`:
  set one to `false` to omit that per-track section.
- `include_automation`: the automated-parameter scan walks every
  parameter of every device and feeds only the `automated_parameters` /
  `automation_enabled` keys on each device. It runs when device
  parameters are included (`include_params`, as before); set
  `include_automation` explicitly to override either way. When the
  scan does not run, both keys are omitted.

Track info parameters: `include_clip_slots` and `include_devices`, with
the same omit-when-false semantics. The `get_session_snapshot` and
`get_track_info` MCP tools expose the same parameters and forward only
the values a caller actually set, so a default tool call sends exactly
the params it sent before. Measured with the benchmark below (real
serializer, fake Live set): a full 128-track snapshot is about 2.5 MB;
asking for one track brings back about 31 KB at any set size. Requires
Remote Script 1.7.5+ for the projection to take effect; older scripts
ignore the new params and return the full result, as before.

### Bulk device parameters

Reading and tuning one device used to cost 1 + N round trips: a full
`get_device_parameters` read, then one main-thread-scheduled
`set_device_parameter` call per parameter. Two opt-in forms collapse
that (Remote Script 1.7.6+):

- **Subset reads.** `get_device_parameters` accepts optional
  `parameter_indices` and/or `parameter_names`. When either is given,
  every `parameters` list in the result (the device's own, and any
  devices nested in its chains) keeps only the entries whose index or
  name was requested - the union when both are given - with the
  per-entry shape unchanged. With neither given the read is exactly
  the pre-subset one; older scripts ignore the new params and return
  the full read. The `get_device_parameters` MCP tool exposes the same
  selectors and forwards only the ones a caller set.
- **Bulk set.** `set_device_parameters` takes `track_index`,
  `device_index` and a `parameters` list of
  `{"parameter_index": i, "value": v}` items - an item may carry
  `"name"` instead of `"parameter_index"` to address a parameter by
  name (`parameter_index` wins when both are given) - and applies the
  whole list inside ONE main-thread scheduling. Each item goes
  through the single set itself, so clamping, value conversion and
  errors are the single set's, item for item, and each item that
  reaches the set reports the envelope a direct
  `set_device_parameter` call would have produced (old/new values
  included). An item that never reaches the set (malformed,
  unaddressable, unknown name) reports a batch-style per-item error
  instead: the same message a direct call would produce, without
  the dispatched call's `"result": {}` key. One bad item does not block the
  others; an optional `stop_on_error` reports the remaining items as
  skipped instead. The result is `{"results": [...], "summary":
  {"total", "succeeded", "failed", "skipped"}}`, mirroring
  `execute_batch`.

`set_device_parameters` is **not a transaction**: items apply in
order and a failed item does not roll back the items before it.

Measured with the benchmark below (real dispatcher, fake song): the
8-parameter tune drops from 9 wire requests and 4,527 result bytes to
2 wire requests and 2,936 result bytes.

### Browser access diet

Every browser read used to re-walk the hierarchy inside Live: the
tree and items-at-path commands had no caching at all, and the one
memo that existed - URI to item, used by every load - was an
unbounded plain dict that was never invalidated, so it grew for the
script's whole lifetime and could keep pointing at an item the
library no longer had. Finding an item by name meant fetching whole
subtrees and filtering them client-side. Both browser commands also
logged the full `dir(browser)` attribute list on every call.

The caches now live in the Remote Script, because that is where the
walks happen: the MCP server only ever sees serialized results (it
cannot hold item objects), and direct-wire clients skip the server
entirely.

- **URI memo** (`_uri_cache`): an LRU bounded at 512 entries.
- **Read cache** (`_browser_read_cache`): an LRU bounded at 128
  entries holding serialized results - `get_browser_tree` per
  category, `get_browser_items_at_path` per (path, limit, offset),
  and `search_browser` match lists per query. A repeated identical
  read is served without touching the browser; failed reads are
  never cached. `get_browser_item` by path is unchanged apart from
  the memo (it navigates a single chain; it never walked the tree).

Invalidation is explicit, because the script cannot observe library
changes made outside it: a successful `load_browser_item` (or
`load_instrument_or_effect`, which shares the path) clears both
caches - loading is the one browser-directed mutation the script
performs - and the new `refresh_browser_cache` command clears both
on demand, returning how many entries each held
(`{"cleared": {"uri_entries": n, "read_entries": m}}`). Call it after
installing a pack, editing the user library, or rescanning.

`search_browser` (command and tool) searches the browser in the
script: `query` is a case-insensitive substring matched against each
item's name and URI; the result is
`{"query", "total", "offset", "limit", "next_offset", "items"}` with
`limit` defaulting to 50 (cap 200), and each item carries its
`path` in `get_browser_items_at_path` form (e.g.
`instruments/Kits/808 Core Kit`). One walk computes the full match
list, which is kept in the read cache, so paging and repeat searches
cost no further walks. `get_browser_items_at_path` also pages now:
pass `limit` and/or `offset` and only that slice of the children is
serialized, with `total` and `next_offset` added to the result;
with neither, the call and its params are exactly the pre-paging ones.

The browser-path logging diet removes only the two per-call
`dir(browser)` dumps; the attribute list itself is unchanged - it
still feeds `available_categories` and root resolution in both
commands.

Measured by the benchmark's B4 scenario (real dispatcher over a real
socket, counting fake browser, hierarchy walks = `children`
accesses): a browse pass (tree + three items reads + one URI lookup)
costs 5 wire requests and 127 hierarchy walks cold, and the same 5
requests with **0 walks** warm. Finding the items named "kit"
client-side (tree + seven folder reads, filtered locally) costs 8
requests and 118 walks; `search_browser` answers it in 3 requests
(three pages of 10) and 41 walks - the single walk that computes the
match list.

Requires Remote Script 1.7.7+ for the new commands. An older script
ignores the paging parameters on `get_browser_items_at_path` and
answers the two new commands with the standard
`Unknown command: ...` error, which the tools surface as their
usual Error strings.

### Passive listeners (negotiated)

The Remote Script's passive listeners - the song/track/clip census
whose events `drain_passive_events` returns - used to be registered
at script startup and stay registered forever, whether or not any
consumer ever drained an event. Since 1.7.8 they are registered
only when a consumer enables them:

- The `set_passive_listeners` command (and the MCP tool of the same
  name) toggles registration: `{"enabled": true}` registers the
  census, `{"enabled": false}` removes every listener, and omitting
  `enabled` just reports the current state. The handshake advertises
  the negotiation (`agent0_passive_listeners_negotiated`) and the
  live state (`passive_listeners_enabled`).
- The dataset poller - the in-repo consumer - enables the listeners
  while it runs (dataset consent on) and disables them when it
  stops, so the dataset event flow is unchanged. A session that
  never enables events registers no listeners at all.
- Clip listeners are tracked per slot and removed when their clip
  goes away: previously, listeners bound when a slot's clip changed
  were recorded nowhere and accumulated on every toggle.
- Continuous value events (`volume_changed`, `panning_changed`,
  `tempo_changed`) coalesce latest-value-wins per source instead of
  enqueueing every intermediate value; discrete events never
  coalesce. The event queue is bounded at 500 events, drops the
  oldest on overflow, and reports the drop count in the next
  `drain_passive_events` result as `"dropped"`.

Requires Remote Script 1.7.8+. An older script's listeners are
always registered and cannot be toggled; it answers
`set_passive_listeners` with the standard `Unknown command: ...`
error, which the tool surfaces as its usual Error string.

### Connection lifecycle and timeouts

Every command exchange runs under a per-command socket budget,
applied before the request is sent: 65 s for `create_audio_clip`,
the sum of the per-item budgets for `execute_batch` (with a 15 s
floor), 15 s for other state-modifying commands, 10 s for reads. Both receive paths (v1 and
framing v2) honour the budget - before MCP-09 the v1 receive path
silently overrode it with a hard 15 s, so long commands failed at
15 s whatever budget they carried.

Failures are classified by when they happen:

- **Before the request is fully sent** (connect refused, a send
  error): a plain exception, as before - the command was not
  applied.
- **After the request was fully sent but no complete response
  arrived** (timeout, connection loss mid-response, an unparseable
  response): `AbletonConnection.send_command` / `send_batch` raise
  `OutcomeUnknownError` (`MCP_Server.server`). The command may have
  been applied in Live - do not blindly retry; reconcile against
  the session state first. For a batch the classification is at
  batch granularity: the batch may have partially applied and the
  per-item outcomes are unknowable.
- **A definitive answer** (the script's `status: "error"` envelope,
  a request-id echo mismatch): a plain exception carrying the
  script's message, as before.

Every failure path closes the socket and resets the per-connection
state (including the framing-v2 adoption), so the next command
reconnects and re-negotiates from v1 instead of inheriting a
half-open connection.

### Benchmark

`ableton-mcp-benchmark` (or `uv run python -m benchmarks.protocol_benchmark`)
runs a headless protocol benchmark - no Live required: the repeated-parse
framing cost on both endpoints against fake sockets, round-trip counts for
representative composite workflows against a fake TCP peer, a synthetic
snapshot serialisation model, full vs projected snapshot sizes from
the real serializer, and a browser browse-then-find scenario counting
wire requests and hierarchy walks, plus a passive-listener census /
event-queue scenario and a timeout-budget scenario replaying
simulated response gaps against the per-command budgets. Add
`--json` for machine-readable output.

## Limitations & Security Considerations

- Creating complex musical arrangements might need to be broken down into smaller steps
- The tool is designed to work with Ableton's default devices and browser items
- Always save your work before extensive experimentation

---

## Telemetry

There are two tiers, and they have different defaults.

**Anonymous telemetry — on by default.** A random install ID, a per-run session ID, which tools ran, whether they succeeded, and how long they took. This is what counts active users and catches broken tools. It contains none of your content: no prompts, no MIDI, no track or clip names, no device settings.

**Dataset recording — off by default, opt-in.** Everything with your work in it: prompts, MIDI notes, track and clip names, and device settings, which may be published as part of an open dataset used to train music-production models. Nothing here is collected unless you explicitly turn it on.

To see exactly what data each tier collects, see the [Terms & Data Use](TERMS.md).

### Opting in to dataset recording

Either set the environment variable before starting the server:

```bash
export ABLETON_MCP_ENABLE_DATASET=true
```

…or answer the question when your client asks it. On your first tool call you're asked once — as a dialog if your client supports MCP elicitation, otherwise as a message in the chat — and answering yes turns it on from that point. Your answer is stored in `~/.ableton-mcp/consent.json`. Declining, or never answering, records nothing. If you dismiss the dialog without choosing, that isn't treated as an answer and you may be asked again in a later session.

### Opting out of anonymous telemetry

Set one of these before starting the MCP server:

```bash
export ABLETON_MCP_DISABLE_TELEMETRY=true
```

Or use any of these alternatives:

- `DISABLE_TELEMETRY=true`
- `MCP_DISABLE_TELEMETRY=true`

This also disables dataset recording. `ABLETON_MCP_DISABLE_DATASET=true` turns off dataset recording only, and overrides any stored grant.

For Claude Desktop, add the environment variable to your config:

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": ["ableton-mcp"],
            "env": {
                "ABLETON_MCP_DISABLE_TELEMETRY": "true"
            }
        }
    }
}
```

### Lean profile

```bash
export ABLETON_MCP_LEAN_PROFILE=true
```

The lean profile is for deployments that drive Live purely as a control path and never use the dataset side plane. It goes further than the disable switches above: with it set, the server

- does not import the dataset/telemetry modules at startup, and nothing on the tool-call control path imports them — the tool decorators are pass-throughs, no passive poller can start, and no Supabase client is constructed; the one exception is `set_dataset_consent`, which imports `dataset.consent` to persist the user's answer — that loads the modules without activating any recording behaviour;
- keeps the dataset tools registered, and they answer exactly as they do when recording is disabled;
- returns tool results as compact JSON instead of pretty-printed JSON (the same JSON value, without `indent=2`);
- logs command names and byte counts at INFO instead of full command parameters.

Leave it unset for the default profile, which is unchanged.

---

## Join the Community

Give feedback, get inspired, and build on top of the MCP: [**Discord**](https://discord.gg/JK4hNKGprW)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Disclaimer

This is a third-party integration and not made by Ableton. Made by [Siddharth](https://x.com/sidahuj).

---

<div align="center">

**If Ableton MCP is useful to you, consider starring the repo**

</div>
