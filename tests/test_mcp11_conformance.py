"""MCP-11: golden compatibility + conformance suite.

This file freezes the fork's whole compatibility surface as explicit,
recorded expectations, ahead of the MCP-12 (pin swap) and MCP-13
(Remote Script swap) migrations that will lean on it. Everything in
here was recorded once from the tree at script version 1.7.10 and is
then FROZEN: no expectation below is re-derived from the code under
test at runtime. Enumerations (the tool manager's registry, the
dispatcher's AST) are only ever compared against the frozen snapshot,
never used as their own expectation - a golden that re-derives itself
pins nothing.

Pinned surfaces:

* the MCP tool catalog - every tool name, its full parameter schema
  (names, types, defaults, required set) and its return annotation,
  as registered at import time on ``MCP_Server.server.mcp``;
* the same catalog served over the wire: an in-process MCP client
  session offering protocol 2024-11-05 (the version Omnicorp's client
  negotiates) must negotiate exactly that version and receive a
  tools/list whose names and input schemas equal the frozen catalog;
* the wire command surface - COMMAND_TABLE (all 42 commands with
  their MCP-10 thread classes, in advertised order) and
  SCRIPT_CAPABILITIES, cross-checked against the command set the
  dispatcher's AST actually implements (the derivation the Omnicorp
  host patcher uses);
* the get_script_info handshake payload, field for field;
* v1 and v2 framing behaviour for representative commands through
  the real script socket handler, including the exact error envelope
  a direct call produces;
* the MCP-02/MCP-06 envelope qualification, as exact frozen
  envelopes: an item that reaches dispatch carries the direct call's
  envelope (``"result": {}`` included on error); an item rejected
  before dispatch (malformed, nested batch, unaddressable, unknown
  name) carries the batch-style envelope - same message, no
  ``"result"`` key;
* the MCP-09 error-classification surface of send_command /
  send_batch: pre-send failures and definitive answers raise plain
  exceptions; anything after the request was fully sent raises
  OutcomeUnknownError; successes return the unwrapped result;
* the per-command timeout budget table (MCP-09);
* the Remote Script installer's behaviour in a scratch directory:
  layout (``AbletonMCP/__init__.py``, the single-file absorbed
  layout), installed / unchanged / updated-with-backup / skipped /
  no-target outcomes, the version lockstep between the installed
  script and EXPECTED_REMOTE_SCRIPT_VERSION, and the capability set
  derivable from the installed file;
* the Omnicorp extension's import surface: the exact names
  ``ableton-extension/server.py`` imports from MCP_Server.server
  (``mcp``, ``get_ableton_connection``, ``main``), import-time tool
  registration in a fresh interpreter, and the extension's
  registration pattern (an external module adding tools onto the
  shared ``mcp`` object).

Known state, recorded deliberately (MCP-11 report): the Omnicorp
extension's script-side installer (install.py / host_patch.py)
patches an installed script through string anchors written against
the pre-MCP-10 dispatcher layout (a literal command list in
_process_command). Against the current fork script it fails closed -
``RuntimeError: Unsupported remote script layout; nothing
installed``, target left byte-identical - because MCP-10 replaced
that literal list with the COMMAND_TABLE-derived set. Making the
extension installable again is MCP-13's job (fork+extension bundle);
this suite pins the safety property that matters until then - the
fork installer never produces a half-patched script - via the
layout and AST-capability pins above, and the lane record carries
the finding.

Regenerating the goldens (only ever as a deliberate, reviewed act -
e.g. after an intentional catalog change): re-run the probes that
produced GOLDEN_JSON below against the new tree, paste the result,
and let the diff be the review surface.
"""

import ast
import inspect
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import MCP_Server.server as server_module  # noqa: E402
from MCP_Server.framing import encode_frame  # noqa: E402
from MCP_Server.remote_script_install import (  # noqa: E402
    EXPECTED_REMOTE_SCRIPT_VERSION,
    install_remote_script,
)
from MCP_Server.server import (  # noqa: E402
    AbletonConnection,
    OutcomeUnknownError,
)
from tests.test_mcp06_bulk_params import make_instance as mcp06_instance  # noqa: E402
from tests.test_mcp09_connection_lifecycle import GapSock  # noqa: E402
from tests.test_remote_script_bridge import (  # noqa: E402
    FakeClient,
    _load_script_module,
)

GOLDEN_JSON = r"""{
 "batch_response": {
  "result": {
   "results": [
    {
     "result": {
      "agent0_capabilities_complete": true,
      "agent0_framing": [
       "v1",
       "v2"
      ],
      "agent0_host_revision": 1,
      "agent0_passive_listeners_negotiated": true,
      "agent0_request_ids": true,
      "capabilities": [
       "add_notes_to_clip",
       "clear_notes_from_clip",
       "create_audio_clip",
       "create_audio_track",
       "create_clip",
       "create_locator",
       "create_midi_track",
       "delete_clip",
       "drain_passive_events",
       "duplicate_session_clip_to_arrangement",
       "execute_batch",
       "fire_clip",
       "get_arrangement_clips",
       "get_browser_categories",
       "get_browser_item",
       "get_browser_items",
       "get_browser_items_at_path",
       "get_browser_tree",
       "get_clip_notes",
       "get_device_parameters",
       "get_script_info",
       "get_session_info",
       "get_session_snapshot",
       "get_track_info",
       "inspect_rack",
       "load_browser_item",
       "load_instrument_or_effect",
       "map_rack_magnitude",
       "refresh_browser_cache",
       "search_browser",
       "set_arrangement_clip_name",
       "set_clip_name",
       "set_current_song_time",
       "set_device_parameter",
       "set_device_parameters",
       "set_passive_listeners",
       "set_tempo",
       "set_track_name",
       "start_playback",
       "stop_clip",
       "stop_playback",
       "switch_to_arrangement_view"
      ],
      "name": "AbletonMCP",
      "passive_listeners": true,
      "passive_listeners_enabled": false,
      "port": 9877,
      "protocol_version": 1,
      "script_version": "1.7.10",
      "snapshot_schema": "ableton_mcp_snapshot_v2"
     },
     "status": "success"
    },
    {
     "message": "Unknown command: no_such_command",
     "result": {},
     "status": "error"
    },
    {
     "message": "Batch items must be objects with 'type' and 'params'",
     "status": "error"
    },
    {
     "message": "Nested execute_batch is not allowed",
     "status": "error"
    },
    {
     "message": "Batch item 'params' must be an object",
     "status": "error"
    }
   ],
   "summary": {
    "failed": 4,
    "skipped": 0,
    "succeeded": 1,
    "total": 5
   }
  },
  "status": "success"
 },
 "bulk_response": {
  "result": {
   "results": [
    {
     "result": {
      "device_index": 0,
      "max": 1.0,
      "min": 0.0,
      "name": "Osc A Level",
      "old_value": 0.05,
      "parameter_index": 0,
      "track_index": 0,
      "value": 0.5
     },
     "status": "success"
    },
    {
     "message": "Parameter index out of range",
     "result": {},
     "status": "error"
    },
    {
     "message": "Parameter not found: No Such Param",
     "status": "error"
    },
    {
     "message": "Parameter item requires 'parameter_index' or 'name'",
     "status": "error"
    },
    {
     "message": "Parameter items must be objects with 'parameter_index' (or 'name') and 'value'",
     "status": "error"
    }
   ],
   "summary": {
    "failed": 4,
    "skipped": 0,
    "succeeded": 1,
    "total": 5
   }
  },
  "status": "success"
 },
 "command_table": [
  [
   "add_notes_to_clip",
   "main"
  ],
  [
   "clear_notes_from_clip",
   "main"
  ],
  [
   "create_audio_clip",
   "main"
  ],
  [
   "create_audio_track",
   "main"
  ],
  [
   "create_clip",
   "main"
  ],
  [
   "create_locator",
   "main"
  ],
  [
   "create_midi_track",
   "main"
  ],
  [
   "delete_clip",
   "main"
  ],
  [
   "drain_passive_events",
   "handler"
  ],
  [
   "duplicate_session_clip_to_arrangement",
   "main"
  ],
  [
   "execute_batch",
   "main"
  ],
  [
   "fire_clip",
   "main"
  ],
  [
   "get_arrangement_clips",
   "handler"
  ],
  [
   "get_browser_categories",
   "handler"
  ],
  [
   "get_browser_item",
   "handler"
  ],
  [
   "get_browser_items",
   "handler"
  ],
  [
   "get_browser_items_at_path",
   "handler"
  ],
  [
   "get_browser_tree",
   "handler"
  ],
  [
   "get_clip_notes",
   "handler"
  ],
  [
   "get_device_parameters",
   "handler"
  ],
  [
   "get_script_info",
   "handler"
  ],
  [
   "get_session_info",
   "handler"
  ],
  [
   "get_session_snapshot",
   "handler"
  ],
  [
   "get_track_info",
   "handler"
  ],
  [
   "inspect_rack",
   "main"
  ],
  [
   "load_browser_item",
   "main"
  ],
  [
   "load_instrument_or_effect",
   "main"
  ],
  [
   "map_rack_magnitude",
   "main"
  ],
  [
   "refresh_browser_cache",
   "handler"
  ],
  [
   "search_browser",
   "handler"
  ],
  [
   "set_arrangement_clip_name",
   "main"
  ],
  [
   "set_clip_name",
   "main"
  ],
  [
   "set_current_song_time",
   "main"
  ],
  [
   "set_device_parameter",
   "main"
  ],
  [
   "set_device_parameters",
   "main"
  ],
  [
   "set_passive_listeners",
   "main"
  ],
  [
   "set_tempo",
   "main"
  ],
  [
   "set_track_name",
   "main"
  ],
  [
   "start_playback",
   "main"
  ],
  [
   "stop_clip",
   "main"
  ],
  [
   "stop_playback",
   "main"
  ],
  [
   "switch_to_arrangement_view",
   "main"
  ]
 ],
 "script_capabilities": [
  "add_notes_to_clip",
  "clear_notes_from_clip",
  "create_audio_clip",
  "create_audio_track",
  "create_clip",
  "create_locator",
  "create_midi_track",
  "delete_clip",
  "drain_passive_events",
  "duplicate_session_clip_to_arrangement",
  "execute_batch",
  "fire_clip",
  "get_arrangement_clips",
  "get_browser_categories",
  "get_browser_item",
  "get_browser_items",
  "get_browser_items_at_path",
  "get_browser_tree",
  "get_clip_notes",
  "get_device_parameters",
  "get_script_info",
  "get_session_info",
  "get_session_snapshot",
  "get_track_info",
  "inspect_rack",
  "load_browser_item",
  "load_instrument_or_effect",
  "map_rack_magnitude",
  "refresh_browser_cache",
  "search_browser",
  "set_arrangement_clip_name",
  "set_clip_name",
  "set_current_song_time",
  "set_device_parameter",
  "set_device_parameters",
  "set_passive_listeners",
  "set_tempo",
  "set_track_name",
  "start_playback",
  "stop_clip",
  "stop_playback",
  "switch_to_arrangement_view"
 ],
 "script_info": {
  "agent0_capabilities_complete": true,
  "agent0_framing": [
   "v1",
   "v2"
  ],
  "agent0_host_revision": 1,
  "agent0_passive_listeners_negotiated": true,
  "agent0_request_ids": true,
  "capabilities": [
   "add_notes_to_clip",
   "clear_notes_from_clip",
   "create_audio_clip",
   "create_audio_track",
   "create_clip",
   "create_locator",
   "create_midi_track",
   "delete_clip",
   "drain_passive_events",
   "duplicate_session_clip_to_arrangement",
   "execute_batch",
   "fire_clip",
   "get_arrangement_clips",
   "get_browser_categories",
   "get_browser_item",
   "get_browser_items",
   "get_browser_items_at_path",
   "get_browser_tree",
   "get_clip_notes",
   "get_device_parameters",
   "get_script_info",
   "get_session_info",
   "get_session_snapshot",
   "get_track_info",
   "inspect_rack",
   "load_browser_item",
   "load_instrument_or_effect",
   "map_rack_magnitude",
   "refresh_browser_cache",
   "search_browser",
   "set_arrangement_clip_name",
   "set_clip_name",
   "set_current_song_time",
   "set_device_parameter",
   "set_device_parameters",
   "set_passive_listeners",
   "set_tempo",
   "set_track_name",
   "start_playback",
   "stop_clip",
   "stop_playback",
   "switch_to_arrangement_view"
  ],
  "name": "AbletonMCP",
  "passive_listeners": true,
  "passive_listeners_enabled": false,
  "port": 9877,
  "protocol_version": 1,
  "script_version": "1.7.10",
  "snapshot_schema": "ableton_mcp_snapshot_v2"
 },
 "tool_catalog": {
  "add_notes_to_clip": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "notes": {
      "items": {
       "additionalProperties": {
        "anyOf": [
         {
          "type": "integer"
         },
         {
          "type": "number"
         },
         {
          "type": "boolean"
         }
        ]
       },
       "type": "object"
      },
      "title": "Notes",
      "type": "array"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index",
     "notes"
    ],
    "title": "add_notes_to_clipArguments",
    "type": "object"
   },
   "return": "str"
  },
  "clear_notes_from_clip": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index"
    ],
    "title": "clear_notes_from_clipArguments",
    "type": "object"
   },
   "return": "str"
  },
  "create_audio_clip": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "path": {
      "title": "Path",
      "type": "string"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index",
     "path"
    ],
    "title": "create_audio_clipArguments",
    "type": "object"
   },
   "return": "str"
  },
  "create_audio_track": {
   "parameters": {
    "properties": {
     "index": {
      "default": -1,
      "title": "Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "create_audio_trackArguments",
    "type": "object"
   },
   "return": "str"
  },
  "create_clip": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "length": {
      "default": 4.0,
      "title": "Length",
      "type": "number"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index"
    ],
    "title": "create_clipArguments",
    "type": "object"
   },
   "return": "str"
  },
  "create_locator": {
   "parameters": {
    "properties": {
     "name": {
      "title": "Name",
      "type": "string"
     },
     "time": {
      "title": "Time",
      "type": "number"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "name",
     "time"
    ],
    "title": "create_locatorArguments",
    "type": "object"
   },
   "return": "str"
  },
  "create_midi_track": {
   "parameters": {
    "properties": {
     "index": {
      "default": -1,
      "title": "Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "create_midi_trackArguments",
    "type": "object"
   },
   "return": "str"
  },
  "delete_clip": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index"
    ],
    "title": "delete_clipArguments",
    "type": "object"
   },
   "return": "str"
  },
  "duplicate_to_arrangement": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "destination_time": {
      "title": "Destination Time",
      "type": "number"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index",
     "destination_time"
    ],
    "title": "duplicate_to_arrangementArguments",
    "type": "object"
   },
   "return": "str"
  },
  "fire_clip": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index"
    ],
    "title": "fire_clipArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_arrangement_clips": {
   "parameters": {
    "properties": {
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index"
    ],
    "title": "get_arrangement_clipsArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_browser_items_at_path": {
   "parameters": {
    "properties": {
     "limit": {
      "anyOf": [
       {
        "type": "integer"
       },
       {
        "type": "null"
       }
      ],
      "default": null,
      "title": "Limit"
     },
     "offset": {
      "anyOf": [
       {
        "type": "integer"
       },
       {
        "type": "null"
       }
      ],
      "default": null,
      "title": "Offset"
     },
     "path": {
      "title": "Path",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "path"
    ],
    "title": "get_browser_items_at_pathArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_browser_tree": {
   "parameters": {
    "properties": {
     "category_type": {
      "default": "all",
      "title": "Category Type",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "get_browser_treeArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_clip_notes": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index"
    ],
    "title": "get_clip_notesArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_device_parameters": {
   "parameters": {
    "properties": {
     "device_index": {
      "title": "Device Index",
      "type": "integer"
     },
     "parameter_indices": {
      "anyOf": [
       {
        "items": {
         "type": "integer"
        },
        "type": "array"
       },
       {
        "type": "null"
       }
      ],
      "default": null,
      "title": "Parameter Indices"
     },
     "parameter_names": {
      "anyOf": [
       {
        "items": {
         "type": "string"
        },
        "type": "array"
       },
       {
        "type": "null"
       }
      ],
      "default": null,
      "title": "Parameter Names"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "device_index"
    ],
    "title": "get_device_parametersArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_remote_script_info": {
   "parameters": {
    "properties": {
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "get_remote_script_infoArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_session_info": {
   "parameters": {
    "properties": {
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "get_session_infoArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_session_snapshot": {
   "parameters": {
    "properties": {
     "include_arrangement_clips": {
      "default": true,
      "title": "Include Arrangement Clips",
      "type": "boolean"
     },
     "include_automation": {
      "anyOf": [
       {
        "type": "boolean"
       },
       {
        "type": "null"
       }
      ],
      "default": null,
      "title": "Include Automation"
     },
     "include_clip_slots": {
      "default": true,
      "title": "Include Clip Slots",
      "type": "boolean"
     },
     "include_cue_points": {
      "default": true,
      "title": "Include Cue Points",
      "type": "boolean"
     },
     "include_devices": {
      "default": true,
      "title": "Include Devices",
      "type": "boolean"
     },
     "include_master_track": {
      "default": true,
      "title": "Include Master Track",
      "type": "boolean"
     },
     "include_notes": {
      "default": true,
      "title": "Include Notes",
      "type": "boolean"
     },
     "include_params": {
      "default": true,
      "title": "Include Params",
      "type": "boolean"
     },
     "include_return_tracks": {
      "default": true,
      "title": "Include Return Tracks",
      "type": "boolean"
     },
     "include_scenes": {
      "default": true,
      "title": "Include Scenes",
      "type": "boolean"
     },
     "track_indices": {
      "anyOf": [
       {
        "items": {
         "type": "integer"
        },
        "type": "array"
       },
       {
        "type": "null"
       }
      ],
      "default": null,
      "title": "Track Indices"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "get_session_snapshotArguments",
    "type": "object"
   },
   "return": "str"
  },
  "get_track_info": {
   "parameters": {
    "properties": {
     "include_clip_slots": {
      "default": true,
      "title": "Include Clip Slots",
      "type": "boolean"
     },
     "include_devices": {
      "default": true,
      "title": "Include Devices",
      "type": "boolean"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index"
    ],
    "title": "get_track_infoArguments",
    "type": "object"
   },
   "return": "str"
  },
  "load_drum_kit": {
   "parameters": {
    "properties": {
     "kit_path": {
      "title": "Kit Path",
      "type": "string"
     },
     "rack_uri": {
      "title": "Rack Uri",
      "type": "string"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "rack_uri",
     "kit_path"
    ],
    "title": "load_drum_kitArguments",
    "type": "object"
   },
   "return": "str"
  },
  "load_instrument_or_effect": {
   "parameters": {
    "properties": {
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "uri": {
      "title": "Uri",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "uri"
    ],
    "title": "load_instrument_or_effectArguments",
    "type": "object"
   },
   "return": "str"
  },
  "prefer_candidate": {
   "parameters": {
    "properties": {
     "candidate_a": {
      "title": "Candidate A",
      "type": "string"
     },
     "candidate_b": {
      "title": "Candidate B",
      "type": "string"
     },
     "reason": {
      "default": "",
      "title": "Reason",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     },
     "winner": {
      "title": "Winner",
      "type": "string"
     }
    },
    "required": [
     "candidate_a",
     "candidate_b",
     "winner"
    ],
    "title": "prefer_candidateArguments",
    "type": "object"
   },
   "return": "str"
  },
  "rate_last_action": {
   "parameters": {
    "properties": {
     "note": {
      "default": "",
      "title": "Note",
      "type": "string"
     },
     "rating": {
      "title": "Rating",
      "type": "string"
     },
     "tags": {
      "default": "",
      "title": "Tags",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "rating"
    ],
    "title": "rate_last_actionArguments",
    "type": "object"
   },
   "return": "str"
  },
  "record_audition": {
   "parameters": {
    "properties": {
     "dwell_ms": {
      "default": 0.0,
      "title": "Dwell Ms",
      "type": "number"
     },
     "kept": {
      "default": false,
      "title": "Kept",
      "type": "boolean"
     },
     "search_query": {
      "default": "",
      "title": "Search Query",
      "type": "string"
     },
     "uri": {
      "title": "Uri",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "uri"
    ],
    "title": "record_auditionArguments",
    "type": "object"
   },
   "return": "str"
  },
  "refresh_browser_cache": {
   "parameters": {
    "properties": {
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "refresh_browser_cacheArguments",
    "type": "object"
   },
   "return": "str"
  },
  "reject_last_action": {
   "parameters": {
    "properties": {
     "reason": {
      "default": "",
      "title": "Reason",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "reject_last_actionArguments",
    "type": "object"
   },
   "return": "str"
  },
  "search_browser": {
   "parameters": {
    "properties": {
     "limit": {
      "default": 50,
      "title": "Limit",
      "type": "integer"
     },
     "offset": {
      "default": 0,
      "title": "Offset",
      "type": "integer"
     },
     "query": {
      "title": "Query",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "query"
    ],
    "title": "search_browserArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_arrangement_clip_name": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "name": {
      "title": "Name",
      "type": "string"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index",
     "name"
    ],
    "title": "set_arrangement_clip_nameArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_arrangement_time": {
   "parameters": {
    "properties": {
     "time": {
      "title": "Time",
      "type": "number"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "time"
    ],
    "title": "set_arrangement_timeArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_clip_name": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "name": {
      "title": "Name",
      "type": "string"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index",
     "name"
    ],
    "title": "set_clip_nameArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_dataset_consent": {
   "parameters": {
    "properties": {
     "consent": {
      "title": "Consent",
      "type": "boolean"
     },
     "user_said": {
      "default": "",
      "title": "User Said",
      "type": "string"
     }
    },
    "required": [
     "consent"
    ],
    "title": "set_dataset_consentArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_device_parameter": {
   "parameters": {
    "properties": {
     "device_index": {
      "title": "Device Index",
      "type": "integer"
     },
     "parameter_index": {
      "title": "Parameter Index",
      "type": "integer"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     },
     "value": {
      "title": "Value",
      "type": "number"
     }
    },
    "required": [
     "track_index",
     "device_index",
     "parameter_index",
     "value"
    ],
    "title": "set_device_parameterArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_device_parameters": {
   "parameters": {
    "properties": {
     "device_index": {
      "title": "Device Index",
      "type": "integer"
     },
     "parameters": {
      "items": {
       "additionalProperties": true,
       "type": "object"
      },
      "title": "Parameters",
      "type": "array"
     },
     "stop_on_error": {
      "default": false,
      "title": "Stop On Error",
      "type": "boolean"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "device_index",
     "parameters"
    ],
    "title": "set_device_parametersArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_passive_listeners": {
   "parameters": {
    "properties": {
     "enabled": {
      "default": true,
      "title": "Enabled",
      "type": "boolean"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "set_passive_listenersArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_tempo": {
   "parameters": {
    "properties": {
     "tempo": {
      "title": "Tempo",
      "type": "number"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "tempo"
    ],
    "title": "set_tempoArguments",
    "type": "object"
   },
   "return": "str"
  },
  "set_track_name": {
   "parameters": {
    "properties": {
     "name": {
      "title": "Name",
      "type": "string"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "name"
    ],
    "title": "set_track_nameArguments",
    "type": "object"
   },
   "return": "str"
  },
  "start_playback": {
   "parameters": {
    "properties": {
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "start_playbackArguments",
    "type": "object"
   },
   "return": "str"
  },
  "stop_clip": {
   "parameters": {
    "properties": {
     "clip_index": {
      "title": "Clip Index",
      "type": "integer"
     },
     "track_index": {
      "title": "Track Index",
      "type": "integer"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "track_index",
     "clip_index"
    ],
    "title": "stop_clipArguments",
    "type": "object"
   },
   "return": "str"
  },
  "stop_playback": {
   "parameters": {
    "properties": {
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "stop_playbackArguments",
    "type": "object"
   },
   "return": "str"
  },
  "submit_intent": {
   "parameters": {
    "properties": {
     "level": {
      "default": 5,
      "title": "Level",
      "type": "integer"
     },
     "text": {
      "title": "Text",
      "type": "string"
     },
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "required": [
     "text"
    ],
    "title": "submit_intentArguments",
    "type": "object"
   },
   "return": "str"
  },
  "switch_to_arrangement_view": {
   "parameters": {
    "properties": {
     "user_prompt": {
      "default": "",
      "title": "User Prompt",
      "type": "string"
     }
    },
    "title": "switch_to_arrangement_viewArguments",
    "type": "object"
   },
   "return": "str"
  }
 }
}"""

GOLDEN = json.loads(GOLDEN_JSON)
FROZEN_TOOLS = GOLDEN["tool_catalog"]
FROZEN_COMMAND_TABLE = [tuple(pair) for pair in GOLDEN["command_table"]]
FROZEN_CAPABILITIES = GOLDEN["script_capabilities"]
FROZEN_SCRIPT_INFO = GOLDEN["script_info"]


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


def _dispatcher_commands(script_path):
    """The host-patch derivation: every string compared against
    command_type in _process_command, from the file's AST - the same
    set Omnicorp's patch_host computes before it will touch a file."""
    tree = ast.parse(Path(script_path).read_text(encoding="utf-8"))
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
                values = (other.elts
                          if isinstance(other, (ast.List, ast.Tuple))
                          else [other])
                caps.update(v.value for v in values
                            if isinstance(v, ast.Constant)
                            and isinstance(v.value, str))
    return caps


def _make_bare_instance(script_module):
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.application = lambda: None

    def schedule(_delay, fn):
        fn()
    inst.schedule_message = schedule
    return inst


# ------------------------------------------------------------ tool catalog

def test_tool_catalog_matches_frozen_snapshot():
    """Every registered tool - name, full parameter schema, return
    annotation - equals the frozen record. A rename, a dropped tool,
    a changed default or a new required parameter fails here."""
    live = {}
    for name, tool in server_module.mcp._tool_manager._tools.items():
        ann = inspect.signature(tool.fn).return_annotation
        live[name] = {
            "parameters": tool.parameters,
            "return": getattr(ann, "__name__", str(ann)),
        }
    assert live == FROZEN_TOOLS


def test_tool_catalog_count_and_return_contract():
    assert len(FROZEN_TOOLS) == 41
    assert {entry["return"] for entry in FROZEN_TOOLS.values()} == {"str"}


def test_import_time_registration_in_fresh_interpreter():
    """The catalog exists because the module was imported - no main(),
    no lifespan, no Live. The extension depends on exactly this: it
    imports the module and registers onto the populated registry."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import json, MCP_Server.server as s;"
         "print(json.dumps(sorted(s.mcp._tool_manager._tools)))"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == sorted(
        FROZEN_TOOLS)


# ------------------------------------------------- extension import surface

def test_extension_import_surface():
    """The exact import the Omnicorp extension performs first."""
    from MCP_Server.server import get_ableton_connection, main, mcp

    assert mcp is server_module.mcp
    assert callable(main)
    assert callable(get_ableton_connection)


def test_extension_registration_pattern(tmp_path):
    """A module in the extension's shape - import the three names,
    decorate functions onto the shared mcp, call send_command -
    registers its tools alongside the frozen base catalog."""
    fixture = tmp_path / "fixture_extension.py"
    fixture.write_text(
        '"""Shaped like ableton-extension/server.py."""\n'
        "from MCP_Server.server import mcp, get_ableton_connection, main\n"
        "\n"
        "\n"
        "@mcp.tool()\n"
        "def conformance_probe_track(track_index: int) -> dict:\n"
        '    """Probe tool in the extension registration pattern."""\n'
        "    return get_ableton_connection().send_command(\n"
        "        'delete_track', {'track_index': track_index})\n",
        encoding="utf-8")
    probe = (
        "import json, sys;"
        "sys.path.insert(0, {fixture_dir!r});"
        "import MCP_Server.server as s;"
        "import fixture_extension;"
        "print(json.dumps(sorted(s.mcp._tool_manager._tools)))"
    ).format(fixture_dir=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    names = json.loads(proc.stdout.strip().splitlines()[-1])
    assert names == sorted(list(FROZEN_TOOLS) + ["conformance_probe_track"])


# ------------------------------------------------------- MCP wire protocol

def test_mcp_session_negotiates_2024_11_05_and_serves_frozen_catalog(
        monkeypatch):
    """A real client session over the SDK's in-memory transport.

    The client is made to offer 2024-11-05 (what Omnicorp's client
    negotiates); the server must accept and echo exactly that
    version, and tools/list must carry the frozen names and input
    schemas. The server lifespan runs for real - with no Live
    listening it logs its failed connect attempts and serves anyway,
    which is the headless behaviour being pinned too.
    """
    import anyio
    import mcp.types as mcp_types
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_connected_server_and_client_session

    captured = {}
    original_initialize = ClientSession.initialize

    async def capturing_initialize(self):
        result = await original_initialize(self)
        captured["result"] = result
        return result

    monkeypatch.setattr(ClientSession, "initialize", capturing_initialize)
    monkeypatch.setattr(mcp_types, "LATEST_PROTOCOL_VERSION", "2024-11-05")

    async def run():
        async with create_connected_server_and_client_session(
                server_module.mcp) as session:
            listed = await session.list_tools()
            return {tool.name: tool.inputSchema for tool in listed.tools}

    wire = anyio.run(run)
    assert captured["result"].protocolVersion == "2024-11-05"
    assert wire == {
        name: entry["parameters"] for name, entry in FROZEN_TOOLS.items()}


# ------------------------------------------------------- wire command table

def test_command_table_matches_frozen(script_module):
    assert list(script_module.COMMAND_TABLE) == FROZEN_COMMAND_TABLE
    assert len(FROZEN_COMMAND_TABLE) == 42
    assert script_module.SCRIPT_CAPABILITIES == FROZEN_CAPABILITIES
    assert dict(script_module.COMMAND_TABLE) == {
        name: cls for name, cls in FROZEN_COMMAND_TABLE}


def test_dispatcher_ast_set_equals_frozen_table(script_module):
    """The implemented command set (AST derivation, as the host
    patcher computes it) is exactly the frozen table's name set."""
    bundled = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
               / "AbletonMCP_init.py")
    assert _dispatcher_commands(bundled) == {
        name for name, _cls in FROZEN_COMMAND_TABLE}


# ------------------------------------------------------------- handshake

def test_get_script_info_matches_frozen(script_module):
    inst = _make_bare_instance(script_module)
    assert inst._get_script_info() == FROZEN_SCRIPT_INFO


def test_get_script_info_envelope_via_dispatcher(script_module):
    inst = _make_bare_instance(script_module)
    response = inst._process_command(
        {"type": "get_script_info", "params": {}})
    assert response == {"status": "success", "result": FROZEN_SCRIPT_INFO}


# ------------------------------------------------- framing + envelope shapes

def _serve_real(script_module, chunks):
    """Run one client session through the real _handle_client with the
    real _process_command (the bridge harness, unmodified dispatch)."""
    inst = _make_bare_instance(script_module)
    inst.running = True
    client = FakeClient(chunks)
    inst._handle_client(client)
    return client.sent


def test_v1_success_envelope(script_module):
    request = json.dumps(
        {"type": "get_script_info", "params": {}}).encode("utf-8")
    sent = _serve_real(script_module, [request])
    assert len(sent) == 1
    assert json.loads(sent[0].decode("utf-8")) == {
        "status": "success", "result": FROZEN_SCRIPT_INFO}


def test_v1_error_envelope_keeps_result_key(script_module):
    """A dispatched command's error envelope carries "result": {} -
    the dispatcher initialises it before routing and leaves it in
    place. This is the half of the envelope contract the batch/bulk
    qualification mirrors."""
    request = json.dumps(
        {"type": "nope_cmd", "params": {}}).encode("utf-8")
    sent = _serve_real(script_module, [request])
    assert len(sent) == 1
    assert json.loads(sent[0].decode("utf-8")) == {
        "status": "error",
        "message": "Unknown command: nope_cmd",
        "result": {},
    }


def test_v2_framed_round_trip_matches_v1_envelope(script_module):
    payload = json.dumps(
        {"type": "get_script_info", "params": {}}).encode("utf-8")
    sent = _serve_real(script_module, [encode_frame(payload)])
    assert len(sent) == 1
    frame = sent[0]
    length = int.from_bytes(frame[:4], "big")
    assert length == len(frame) - 4
    assert json.loads(frame[4:].decode("utf-8")) == {
        "status": "success", "result": FROZEN_SCRIPT_INFO}


# ------------------------------------------- batch / bulk envelope contract

def test_execute_batch_envelopes_match_frozen(script_module):
    """The MCP-02 qualification, frozen: dispatched items return the
    direct envelope (error items keep "result": {}); items rejected
    before dispatch - non-object item, nested batch, non-object
    params - return the batch-style envelope without "result"."""
    inst = _make_bare_instance(script_module)
    response = inst._process_command({
        "type": "execute_batch",
        "params": {"commands": [
            {"type": "get_script_info", "params": {}},
            {"type": "no_such_command", "params": {}},
            "garbage",
            {"type": "execute_batch", "params": {"commands": []}},
            {"type": "set_tempo", "params": "nope"},
        ]},
    })
    assert response == GOLDEN["batch_response"]
    results = response["result"]["results"]
    assert "result" in results[1]        # dispatched error: key present
    assert all("result" not in item for item in results[2:])


def test_bulk_set_envelopes_match_frozen(script_module):
    """The MCP-06 qualification, frozen: an item that reaches the set
    returns the direct envelope exactly (error items keep
    "result": {}); an item that never reaches it - unknown name, no
    address, malformed - returns the batch-style envelope without
    "result"."""
    inst = mcp06_instance(script_module)
    response = inst._process_command({
        "type": "set_device_parameters",
        "params": {"track_index": 0, "device_index": 0, "parameters": [
            {"parameter_index": 0, "value": 0.5},
            {"parameter_index": 99, "value": 0.5},
            {"name": "No Such Param", "value": 0.5},
            {"value": 0.5},
            "garbage",
        ]},
    })
    assert response == GOLDEN["bulk_response"]
    results = response["result"]["results"]
    assert results[1]["result"] == {}    # reached the set: key present
    assert all("result" not in item for item in results[2:])


# ------------------------------------------------- MCP-09 error classification

def _conn(sock=None):
    conn = AbletonConnection("127.0.0.1", 9877)
    if sock is not None:
        conn.sock = sock
    return conn


def test_classification_connect_refused_is_plain():
    """Pre-send failure: plain ConnectionError, never
    OutcomeUnknownError - the command was never sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    conn = AbletonConnection("127.0.0.1", port)
    with pytest.raises(ConnectionError) as excinfo:
        conn.send_command("get_session_info")
    assert type(excinfo.value) is ConnectionError
    assert str(excinfo.value) == "Not connected to Ableton"


def test_classification_success_returns_unwrapped_result():
    payload = json.dumps(
        {"status": "success", "result": {"tempo": 120.0}}).encode()
    conn = _conn(GapSock(payload))
    assert conn.send_command("get_session_info") == {"tempo": 120.0}


def test_classification_error_envelope_is_plain_exception():
    """A definitive answer from the script: plain Exception - the
    command provably did NOT apply. The script's message travels
    inside the generic handler's "Communication error with Ableton: "
    wrap; that composed string is the standing surface (byte-verified
    unchanged by the MCP-09 review)."""
    payload = json.dumps(
        {"status": "error", "message": "Unknown command: nope"}).encode()
    conn = _conn(GapSock(payload))
    with pytest.raises(Exception) as excinfo:
        conn.send_command("nope")
    assert type(excinfo.value) is Exception
    assert str(excinfo.value) == (
        "Communication error with Ableton: Unknown command: nope")


def test_classification_eof_after_send_is_outcome_unknown():
    sock = GapSock(b"")
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert excinfo.value.command_type == "get_session_info"
    assert sock.closed


def test_classification_timeout_after_send_is_outcome_unknown():
    payload = json.dumps(
        {"status": "success", "result": {}}).encode()
    conn = _conn(GapSock(payload, gap=999.0))  # budget is 10 s for a read
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")


def test_classification_unparseable_response_is_outcome_unknown():
    """A complete response that cannot be decoded or parsed - the
    case MCP-09's first review caught escaping this classification
    on the v2 path (UnicodeDecodeError) - is outcome-unknown: the
    command may have applied and its result is unreadable."""
    conn = _conn(GapSock(b"\xff\xfe not utf-8, not json"))
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")


def test_classification_send_batch_contract():
    """send_batch validates before any socket use, returns the batch
    result unwrapped, and raises the batch exchange's own failures
    the way send_command does."""
    conn = _conn(GapSock(b""))
    with pytest.raises(ValueError):
        conn.send_batch("not-a-list")

    batch_result = {"results": [], "summary": {
        "total": 0, "succeeded": 0, "failed": 0, "skipped": 0}}
    payload = json.dumps(
        {"status": "success", "result": batch_result}).encode()
    conn = _conn(GapSock(payload))
    assert conn.send_batch([]) == batch_result


# ------------------------------------------------------- timeout budget table

def test_timeout_budget_table_frozen():
    """The MCP-09 budgets the swaps inherit: long-running table,
    modifying 15 s, reads 10 s, batch sums floored at 15 s."""
    conn = _conn()
    assert conn._command_timeout("create_audio_clip") == 65.0
    assert conn._command_timeout("set_tempo") == 15.0
    assert conn._command_timeout("get_session_info") == 10.0
    assert conn._command_timeout("execute_batch", {"commands": []}) == 15.0
    assert conn._command_timeout(
        "execute_batch",
        {"commands": [{"type": "create_audio_clip"}]}) == 65.0
    assert conn._command_timeout(
        "execute_batch",
        {"commands": [{"type": "set_tempo"},
                      {"type": "get_session_info"}]}) == 25.0


# ----------------------------------------------------------------- installer

def _script_version_of(path):
    match = re.search(
        r'^SCRIPT_VERSION = "([^"]+)"',
        Path(path).read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"no SCRIPT_VERSION in {path}"
    return match.group(1)


def test_installer_version_lockstep():
    repo_script = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
    bundled = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
               / "AbletonMCP_init.py")
    assert _script_version_of(repo_script) == EXPECTED_REMOTE_SCRIPT_VERSION
    assert _script_version_of(bundled) == EXPECTED_REMOTE_SCRIPT_VERSION
    assert EXPECTED_REMOTE_SCRIPT_VERSION == FROZEN_SCRIPT_INFO[
        "script_version"]


def test_installer_fresh_install_layout_and_result(tmp_path):
    results = install_remote_script(tmp_path)
    dest = tmp_path / "AbletonMCP" / "__init__.py"
    assert results == [{
        "path": str(dest),
        "status": "installed",
        "detail": f"script_version={EXPECTED_REMOTE_SCRIPT_VERSION}",
        "script_version": EXPECTED_REMOTE_SCRIPT_VERSION,
        "backup": None,
    }]
    # The absorbed single-file layout: the whole script is __init__.py.
    assert [p.name for p in (tmp_path / "AbletonMCP").iterdir()] == [
        "__init__.py"]
    bundled = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
               / "AbletonMCP_init.py")
    assert dest.read_bytes() == bundled.read_bytes()


def test_installer_reinstall_is_unchanged(tmp_path):
    install_remote_script(tmp_path)
    results = install_remote_script(tmp_path)
    assert results[0]["status"] == "unchanged"
    assert results[0]["backup"] is None


def test_installer_update_backs_up_previous_file(tmp_path):
    install_remote_script(tmp_path)
    dest = tmp_path / "AbletonMCP" / "__init__.py"
    dest.write_bytes(b"# user edit\n")
    results = install_remote_script(tmp_path)
    backup = dest.with_suffix(".py.bak")
    assert results == [{
        "path": str(dest),
        "status": "updated",
        "detail": (
            f"script_version={EXPECTED_REMOTE_SCRIPT_VERSION}"
            f"; previous version backed up to {backup.name}"),
        "script_version": EXPECTED_REMOTE_SCRIPT_VERSION,
        "backup": str(backup),
    }]
    assert backup.read_bytes() == b"# user edit\n"
    bundled = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
               / "AbletonMCP_init.py")
    assert dest.read_bytes() == bundled.read_bytes()


def test_installer_skip_env_and_force(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_SKIP_SCRIPT_INSTALL", "1")
    assert install_remote_script(tmp_path) == [{
        "path": None,
        "status": "skipped",
        "detail": "ABLETON_MCP_SKIP_SCRIPT_INSTALL set",
    }]
    assert not (tmp_path / "AbletonMCP").exists()
    results = install_remote_script(tmp_path, force=True)
    assert results[0]["status"] == "installed"


def test_installer_no_targets_is_an_error_result(tmp_path, monkeypatch):
    import MCP_Server.remote_script_install as install_module

    monkeypatch.setattr(
        install_module, "discover_user_library_remote_script_dirs",
        lambda: [])
    results = install_remote_script()
    assert results == [{
        "path": None,
        "status": "error",
        "detail": (
            "No Ableton User Library found. Create the 'Remote Scripts' "
            "folder in your User Library (default: ~/Music/Ableton/User "
            "Library on macOS, %USERPROFILE%\\Documents\\Ableton\\User "
            "Library on Windows) or pass --target <dir>."),
    }]


def test_installed_script_advertises_frozen_capabilities(tmp_path):
    """The file the installer ships derives - by the host patcher's
    own AST method - exactly the frozen capability set, so what gets
    installed is what the handshake will advertise."""
    install_remote_script(tmp_path)
    dest = tmp_path / "AbletonMCP" / "__init__.py"
    assert _dispatcher_commands(dest) == set(FROZEN_CAPABILITIES)
