## MCP-15: Suno bridge in the fork

Adds six `suno_*` tools that talk to Suno's official MCP server (`https://api.suno.com/mcp`) directly from this code, with the fork's own OAuth grant. Design of record: the fleet's `ableton-suno-bridge-design-2026-10-29.md` (verified live 2026-10-29). Everything is headless-tested: **no real Suno calls, no generations, no credit spend, no OAuth sign-in, and no Live session were used in this build** — the Suno side is tested against fakes replaying the verified tool schemas/response shapes, and the Live side against the suite's usual fakes. Live placement proof rides the MCP-13 Live window.

### What was built

- **`MCP_Server/suno_client.py`** — streamable-HTTP MCP client (stdlib only): protocol negotiation (2025-11-25 / 2025-06-18 / 2025-03-26), session header, no redirects, typed errors carrying Suno's `error`/`code`, no silent retries. It sends an **explicit non-default User-Agent** — Suno's Cloudflare edge bans default client UAs with error 1010 (verified live); the header is set at the client layer and commented as load-bearing.
- **`MCP_Server/suno_auth.py`** — the code's own OAuth: DCR (`token_endpoint_auth_method: none`), authorization-code + PKCE S256, canonical `resource=https://api.suno.com/mcp`, scopes `api:v0 credits:spend download library:read library:search library:write offline_access profile:read`. New console script **`ableton-mcp-suno-auth`** (also `python -m MCP_Server.suno_auth`) runs the one-time sign-in: registers the client, opens the browser, serves the localhost callback, exchanges the code. Refresh token stored via a `TokenStore` abstraction — macOS Keychain (`security` CLI) for real use, in-memory/file stores injected in tests. Refresh on expiry + a single 401-refresh-retry in the client.
- **`MCP_Server/suno_bridge.py` + tools in `server.py`** (pure insertion, 0 deletions):
  - `suno_status` — account + usage; the pre-generation guard surface.
  - `suno_generate` — create + bounded wait-loop to complete (reports `pending` honestly with live clip ids; nothing re-submitted).
  - `suno_generate_to_live` — generate → best-effort analysis (bpm/key; failure never blocks) → WAV download → place on a new audio track named `<title> [suno:<clip_id>]`, verified by read-back, `.suno.json` sidecar next to the download. Live tempo is set only with `match_tempo=True` **and** a measured bpm.
  - `suno_library_to_live` — search/list the user's library and place an existing song (no generation cost).
  - `suno_cover_clip` / `suno_extend_clip` — verify the Live slot holds a clip, upload the clip's file via the PUT flow (upload-create → PUT bytes → upload-complete → poll retrieve), then cover (source lyrics passed explicitly when `keep_vocals`, per Suno's no-carry-over rule) or extend (full-song join, free/idempotent), and place the result back.
- **Guardrails as code, each with tests:** usage check before generation (refuse when credits cannot cover the clip count); `clip_count` always sent explicitly — **Suno defaults an MCP sign-in caller to 2**, so unpinned means double spend; `duration_strict` explicit whenever a duration is given; idempotency key derived per logical creation (stable across retries of the same request); downloads requested **without** `confirm` first — a `requires_credit` answer stops the operation and is returned as a state, never auto-confirmed.

### ⚠️ Golden delta — the MCP-15 catalog migration (deliberate, reviewed surface)

`tests/test_mcp11_conformance.py`: the frozen tool catalog is extended **additively, 41 → 47**, with exactly the six `suno_*` entries above. Verified mechanically against the base commit: every pre-existing catalog entry is byte-identical, nothing was removed, and **all other golden sections (wire command table, handshake, envelopes, classification, installer) are identical** — the bridge adds no Live wire commands. The only other edits in that file are the catalog count pin (41 → 47) and a docstring paragraph recording this delta. `tests/test_mcp04_lean_profile.py` has the same one-line count update (41 → 47); the lean catalog remains identical to the default catalog. `pyproject.toml` gains one console-script line. README gains a Suno bridge section documenting the sign-in and the guardrails.

### One stated deviation

`suno_cover_clip` / `suno_extend_clip` take the clip's `file_path` as an explicit parameter. The design says "take the clip's underlying file from Live", but the frozen wire surface does not expose clip file paths (`get_track_info` clip entries carry name/length/state only), and adding a wire command is a both-sides migration, out of MCP-15's additive scope. The tools still verify against Live that the addressed slot holds a clip, and the v1 limit from the design stands: the file is uploaded as it exists on disk (a warped clip's rendered sound may differ; a rendered bounce is v2).

### Tests

`uv run pytest` — **812 passed** (base 768 + 44 new in `tests/test_mcp15_suno_bridge.py`: client protocol/UA/session/SSE/error/401-refresh, OAuth DCR/PKCE/exchange/refresh/Keychain+file stores, a full sign-in against a local callback server incl. state-mismatch abort, bridge guardrails and flows, placer command order + read-back verification, tool registration/conversion). No existing test changed behaviour; the two count pins moved only because the catalog grew, as called out above.

### After merge (André, one-time)

1. `ableton-mcp-suno-auth` (browser approval on his Suno sign-in; refresh token lands in the macOS Keychain).
2. First real `suno_status` read, then a first generation + placement inside the next Live window (with MCP-13) — the headless suite deliberately proves everything up to those two steps.
