"""MCP-15 tests: the Suno bridge (SunoClient, Suno OAuth, bridge ops).

Everything is headless and offline:

* the Suno MCP server is faked at the transport seam - FakeSunoTransport
  speaks the streamable-HTTP MCP protocol (initialize, session header,
  tools/call) and its tool payloads replay the response shapes verified
  live on 2026-10-29 (account-usage's suno_account block, the
  audio-retrieve clip object, library items);
* the Suno OAuth issuer is faked at the HTTP seam (FakeOAuthHttp):
  dynamic registration, authorize URL construction, code exchange and
  refresh - the flow André will run once via ableton-mcp-suno-auth;
* Live is faked the way the rest of the fork's suite fakes it: a
  send_command recording stub (the MCP-04 pattern) behind the real
  _SunoLivePlacer, and a recording placer behind the bridge.

No real Suno call, no generation, no credit spend, no Live session.
"""

import asyncio
import base64
import hashlib
import inspect
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import MCP_Server.server as server_module  # noqa: E402
from MCP_Server.suno_auth import (  # noqa: E402
    FileTokenStore,
    InMemoryTokenStore,
    KeychainTokenStore,
    SunoOAuth,
    make_pkce_pair,
    run_sign_in,
)
from MCP_Server.suno_bridge import (  # noqa: E402
    InsufficientCreditsError,
    SunoBridge,
    SunoBridgeError,
    derive_idempotency_key,
)
from MCP_Server.suno_client import (  # noqa: E402
    USER_AGENT,
    SunoAuthError,
    SunoClient,
    SunoProtocolError,
    SunoToolError,
    SunoTransportError,
)


def call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


# ------------------------------------------------------------ fake MCP


class FakeSunoTransport:
    """A fake streamable-HTTP MCP server for Suno.

    ``tool_handlers`` maps a Suno tool name to a callable taking the
    arguments dict and returning the tool payload (a dict, replaying
    the verified shapes). A handler may return ("error", payload) to
    answer with an isError result.
    """

    def __init__(self, tool_handlers=None):
        self.tool_handlers = tool_handlers or {}
        self.requests = []  # (method, headers, params)
        self.puts = []
        self.gets = []
        self.unauthorized_once = set()  # tool names that 401 once

    def post(self, url, headers, body, timeout=0):
        message = json.loads(body.decode("utf-8"))
        method = message.get("method")
        params = message.get("params") or {}
        self.requests.append((method, dict(headers), params))
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-suno", "version": "0"},
            }
            return self._result(message, result, session="sess-1")
        if method == "notifications/initialized":
            return 202, {}, b""
        if method == "tools/list":
            return self._result(message, {"tools": []})
        if method == "tools/call":
            name = params.get("name")
            if name in self.unauthorized_once:
                self.unauthorized_once.discard(name)
                return 401, {}, b'{"error":"unauthorized"}'
            handler = self.tool_handlers[name]
            payload = handler(params.get("arguments") or {})
            if isinstance(payload, tuple) and payload[0] == "error":
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(payload[1])}
                    ],
                    "isError": True,
                }
            elif isinstance(payload, tuple) and payload[0] == "sse":
                data = json.dumps({
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "content": [
                            {"type": "text",
                             "text": json.dumps(payload[1])}
                        ]
                    },
                })
                return (
                    200,
                    {"content-type": "text/event-stream"},
                    ("data: " + data + "\n\n").encode("utf-8"),
                )
            else:
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(payload)}
                    ]
                }
            return self._result(message, result)
        raise AssertionError("unexpected method %r" % (method,))

    @staticmethod
    def _result(message, result, session=None):
        headers = {"content-type": "application/json"}
        if session:
            headers["mcp-session-id"] = session
        body = json.dumps({
            "jsonrpc": "2.0", "id": message.get("id"), "result": result,
        }).encode("utf-8")
        return 200, headers, body

    def put(self, url, data, content_type, timeout=0):
        self.puts.append((url, data, content_type))
        return 200, {}, b""

    def get(self, url, timeout=0):
        self.gets.append(url)
        return 200, {}, b"FAKE-WAV-BYTES"


USAGE_RICH = {
    "allowed_models": ["v6-mini", "v6", "v6-wild"],
    "features": ["generate.simple", "generate.custom", "audio.download"],
    "metered_features": {},
    "suno_account": {
        "cancel_on": None,
        "credits_remaining": 20000,
        "download_credits_remaining": None,
        "monthly_limit": 10000,
        "monthly_usage": 0,
        "plan": {"name": "Premier Plan", "period": "month"},
        "renews_on": "2026-11-05T06:44:47Z",
    },
}


def make_client(transport, token="tok-1", refresh=None):
    return SunoClient(lambda: token, transport=transport,
                      refresh_token=refresh)


# ------------------------------------------------------- client tests


def test_client_negotiates_session_and_sends_user_agent():
    transport = FakeSunoTransport({
        "account-usage": lambda args: USAGE_RICH,
    })
    client = make_client(transport)
    usage = client.account_usage()
    assert usage["suno_account"]["credits_remaining"] == 20000
    methods = [m for m, _h, _p in transport.requests]
    assert methods == [
        "initialize", "notifications/initialized", "tools/call",
    ]
    # The Cloudflare rule: an explicit, non-default user agent on
    # every request, and the bearer token alongside it.
    for _m, headers, _p in transport.requests:
        assert headers["User-Agent"] == USER_AGENT
        assert "Python-urllib" not in headers["User-Agent"]
        assert headers["Authorization"] == "Bearer tok-1"
    # The session header issued at initialize is carried afterwards.
    assert "Mcp-Session-Id" not in transport.requests[0][1]
    assert transport.requests[2][1]["Mcp-Session-Id"] == "sess-1"


def test_client_parses_sse_tool_results():
    transport = FakeSunoTransport({
        "account-me": lambda args: ("sse", {
            "display_name": "rac", "handle": "immovableorganist719",
        }),
    })
    client = make_client(transport)
    me = client.account_me()
    assert me["handle"] == "immovableorganist719"


def test_client_tool_error_carries_suno_code():
    transport = FakeSunoTransport({
        "audio-analysis": lambda args: (
            "error", {"error": "audio not found", "code": "not_found"}
        ),
    })
    client = make_client(transport)
    with pytest.raises(SunoToolError) as caught:
        client.audio_analysis("nope")
    assert caught.value.error == "audio not found"
    assert caught.value.code == "not_found"
    assert caught.value.tool == "audio-analysis"


def test_client_jsonrpc_error_is_protocol_error():
    class ErrorTransport(FakeSunoTransport):
        def post(self, url, headers, body, timeout=0):
            message = json.loads(body.decode("utf-8"))
            if message.get("method") == "tools/call":
                return 200, {"content-type": "application/json"}, json.dumps({
                    "jsonrpc": "2.0", "id": message.get("id"),
                    "error": {"code": -32602, "message": "bad params"},
                }).encode("utf-8")
            return super().post(url, headers, body, timeout=timeout)

    client = make_client(ErrorTransport({"x": lambda a: {}}))
    with pytest.raises(SunoProtocolError):
        client.call_tool("x", {})


def test_client_401_refreshes_once_and_retries():
    transport = FakeSunoTransport({
        "account-me": lambda args: {"display_name": "rac"},
    })
    transport.unauthorized_once.add("account-me")
    tokens = iter(["tok-1", "tok-2"])
    current = {"token": "tok-1"}

    def refresh():
        current["token"] = "tok-2"
        return "tok-2"

    client = SunoClient(lambda: current["token"], transport=transport,
                        refresh_token=refresh)
    assert client.account_me()["display_name"] == "rac"
    auths = [
        h["Authorization"] for m, h, _p in transport.requests
        if m == "tools/call"
    ]
    assert auths == ["Bearer tok-1", "Bearer tok-2"]
    assert next(tokens)  # silence the unused iterator intent


def test_client_missing_token_is_auth_error():
    client = SunoClient(lambda: None, transport=FakeSunoTransport())
    with pytest.raises(SunoAuthError):
        client.account_me()


def test_client_put_and_download():
    transport = FakeSunoTransport()
    client = make_client(transport)
    client.put_bytes("https://storage.example/put", b"abc", "audio/wav")
    assert transport.puts == [
        ("https://storage.example/put", b"abc", "audio/wav")
    ]

    class FailTransport(FakeSunoTransport):
        def put(self, url, data, content_type, timeout=0):
            return 403, {}, b"no"

    with pytest.raises(SunoTransportError):
        make_client(FailTransport()).put_bytes(
            "https://storage.example/put", b"abc", "audio/wav"
        )


# --------------------------------------------------------- fake OAuth


class FakeOAuthHttp:
    def __init__(self):
        self.json_posts = []
        self.form_posts = []
        self.token_queue = []

    def post_json(self, url, payload):
        self.json_posts.append((url, payload))
        return 201, {"client_id": "client-123"}

    def post_form(self, url, fields):
        self.form_posts.append((url, fields))
        return 200, self.token_queue.pop(0)


def make_oauth(store=None, http=None, clock=lambda: 1000.0, **kw):
    return SunoOAuth(
        store if store is not None else InMemoryTokenStore(),
        http=http if http is not None else FakeOAuthHttp(),
        clock=clock,
        **kw,
    )


def test_oauth_registration_stores_client_id():
    store = InMemoryTokenStore()
    http = FakeOAuthHttp()
    oauth = make_oauth(store, http)
    assert oauth.register_client() == "client-123"
    url, payload = http.json_posts[0]
    assert url.endswith("/register/")
    assert payload["token_endpoint_auth_method"] == "none"
    assert payload["grant_types"] == [
        "authorization_code", "refresh_token",
    ]
    assert store.load()["client_id"] == "client-123"


def test_oauth_authorize_url_carries_pkce_resource_and_scopes():
    oauth = make_oauth(InMemoryTokenStore({"client_id": "client-123"}))
    verifier, challenge = make_pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    url = oauth.authorize_url("state-9", challenge)
    query = dict(
        pair.split("=", 1)
        for pair in url.split("?", 1)[1].split("&")
    )
    from urllib.parse import unquote_plus
    assert unquote_plus(query["client_id"]) == "client-123"
    assert unquote_plus(query["resource"]) == "https://api.suno.com/mcp"
    assert unquote_plus(query["code_challenge"]) == challenge
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == "state-9"
    assert "offline_access" in unquote_plus(query["scope"])
    assert "credits:spend" in unquote_plus(query["scope"])


def test_oauth_exchange_code_stores_tokens_with_expiry():
    store = InMemoryTokenStore({"client_id": "client-123"})
    http = FakeOAuthHttp()
    http.token_queue.append({
        "access_token": "acc-1", "refresh_token": "ref-1",
        "token_type": "Bearer", "expires_in": 3600, "scope": "api:v0",
    })
    oauth = make_oauth(store, http, clock=lambda: 5000.0)
    tokens = oauth.exchange_code("code-7", "verifier-7")
    url, fields = http.form_posts[0]
    assert url.endswith("/token/")
    assert fields["grant_type"] == "authorization_code"
    assert fields["code"] == "code-7"
    assert fields["code_verifier"] == "verifier-7"
    assert fields["resource"] == "https://api.suno.com/mcp"
    assert tokens["access_token"] == "acc-1"
    assert tokens["refresh_token"] == "ref-1"
    assert tokens["expires_at"] == 5000.0 + 3600


def test_oauth_access_token_refreshes_when_expired():
    store = InMemoryTokenStore({
        "client_id": "client-123", "access_token": "old",
        "refresh_token": "ref-1", "expires_at": 900.0,
    })
    http = FakeOAuthHttp()
    http.token_queue.append({
        "access_token": "new", "expires_in": 3600,
    })
    oauth = make_oauth(store, http, clock=lambda: 1000.0)
    assert oauth.access_token() == "new"
    _url, fields = http.form_posts[0]
    assert fields["grant_type"] == "refresh_token"
    assert fields["refresh_token"] == "ref-1"
    # A refresh answer without a new refresh token keeps the old one.
    assert store.load()["refresh_token"] == "ref-1"


def test_oauth_access_token_valid_skips_http():
    store = InMemoryTokenStore({
        "client_id": "c", "access_token": "good",
        "refresh_token": "r", "expires_at": 99999.0,
    })
    http = FakeOAuthHttp()
    oauth = make_oauth(store, http, clock=lambda: 1000.0)
    assert oauth.access_token() == "good"
    assert http.form_posts == []


def test_oauth_access_token_without_signin_raises():
    oauth = make_oauth(InMemoryTokenStore())
    with pytest.raises(SunoAuthError):
        oauth.access_token()


def test_keychain_store_roundtrip_with_fake_runner():
    calls = []
    saved = {}

    def runner(argv):
        calls.append(argv)
        if argv[1] == "add-generic-password":
            saved["value"] = argv[argv.index("-w") + 1]
            return 0, ""
        if argv[1] == "find-generic-password":
            if "value" in saved:
                return 0, saved["value"]
            return 44, ""
        return 0, ""

    store = KeychainTokenStore(runner=runner)
    assert store.load() is None
    store.save({"refresh_token": "ref-1"})
    assert store.load() == {"refresh_token": "ref-1"}
    add = next(c for c in calls if c[1] == "add-generic-password")
    assert "-U" in add
    assert "ableton-mcp-suno" in add


def test_file_store_roundtrip(tmp_path):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path)
    assert store.load() is None
    store.save({"refresh_token": "ref-1"})
    assert FileTokenStore(path).load() == {"refresh_token": "ref-1"}
    store.clear()
    assert store.load() is None


def test_run_sign_in_end_to_end_with_local_callback():
    store = InMemoryTokenStore()
    http = FakeOAuthHttp()
    http.token_queue.append({
        "access_token": "acc-1", "refresh_token": "ref-1",
        "expires_in": 3600,
    })
    oauth = make_oauth(
        store, http,
        redirect_uri="http://127.0.0.1:8899/callback",
    )
    opened = {}

    def opener(url):
        opened["url"] = url
        return True

    outcome = {}

    def run():
        try:
            outcome["tokens"] = run_sign_in(oauth, opener=opener)
        except Exception as error:  # surfaced by the assertions below
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    # Wait until the flow has built its authorize URL, then play the
    # provider redirecting the browser back with a code + state.
    for _ in range(200):
        if "url" in opened:
            break
        threading.Event().wait(0.05)
    url = opened["url"]
    state = dict(
        pair.split("=", 1) for pair in url.split("?", 1)[1].split("&")
    )["state"]
    callback = (
        "http://127.0.0.1:8899/callback?code=code-42&state=" + state
    )
    with urllib.request.urlopen(callback, timeout=10) as response:
        assert response.status == 200
    thread.join(timeout=15)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["tokens"]["access_token"] == "acc-1"
    assert store.load()["refresh_token"] == "ref-1"
    assert store.load()["client_id"] == "client-123"


def test_run_sign_in_state_mismatch_aborts():
    store = InMemoryTokenStore({"client_id": "client-123"})
    http = FakeOAuthHttp()
    oauth = make_oauth(
        store, http, redirect_uri="http://127.0.0.1:8900/callback",
    )
    outcome = {}

    def run():
        try:
            run_sign_in(oauth, opener=lambda url: True)
        except Exception as error:
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    threading.Event().wait(0.5)
    with urllib.request.urlopen(
        "http://127.0.0.1:8900/callback?code=x&state=WRONG", timeout=10
    ):
        pass
    thread.join(timeout=15)
    assert isinstance(outcome.get("error"), SunoAuthError)
    assert http.form_posts == []  # no token exchange happened


def test_oauth_token_feeds_client_end_to_end():
    """The fake-stack proof: stored grant -> OAuth token provider ->
    SunoClient -> a Suno tool answer, all offline."""
    store = InMemoryTokenStore({
        "client_id": "c", "access_token": "good",
        "refresh_token": "r", "expires_at": 99999.0,
    })
    oauth = make_oauth(store, FakeOAuthHttp(), clock=lambda: 1.0)
    transport = FakeSunoTransport({
        "account-usage": lambda args: USAGE_RICH,
    })
    client = SunoClient(
        oauth.access_token, transport=transport,
        refresh_token=oauth.refresh_access_token,
    )
    assert client.account_usage() == USAGE_RICH


# -------------------------------------------------------- fake bridge


def clip_payload(clip_id, title="Test Song", lyrics=None):
    return {
        "id": clip_id,
        "title": title,
        "status": "complete",
        "song_url": "https://suno.com/song/" + clip_id,
        "handle": "immovableorganist719",
        "metadata": {
            "description": None,
            "duration": 30.0,
            "lyrics": lyrics,
            "style": "synthwave",
            "type": "gen",
        },
    }


class FakeClock:
    def __init__(self, step=30.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


class FakeSuno:
    """Bridge-level fake SunoClient: records every call."""

    def __init__(self, credits=20000):
        self.calls = []
        self.credits = credits
        self.wait_statuses = []       # queue of audio-wait statuses
        self.analysis_payload = {
            "status": "complete", "bpm": 122.0, "key": "A minor",
            "time_signature": "4/4",
            "sections": [{"label": "chorus", "duration_s": 8.0}],
            "terms": ["synth"], "labels": ["electronic"],
        }
        self.analysis_error = None
        self.download_requires_credit = False
        self.download_confirm_calls = []
        self.retrieve_status = "complete"
        self.library_items = [clip_payload("lib-1", "Library Song")]

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def calls_of(self, name):
        return [c for c in self.calls if c[0] == name]

    def account_me(self):
        self._record("account_me")
        return {"display_name": "rac",
                "handle": "immovableorganist719"}

    def account_usage(self):
        self._record("account_usage")
        usage = json.loads(json.dumps(USAGE_RICH))
        usage["suno_account"]["credits_remaining"] = self.credits
        return usage

    def audio_create(self, **params):
        self._record("audio_create", **params)
        count = params.get("clip_count", 1)
        ids = ["gen-%d" % (i + 1) for i in range(count)]
        return {"clip_ids": ids}

    def audio_wait(self, clip_id=None, clip_ids=None, until="complete"):
        self._record("audio_wait", clip_id=clip_id,
                     clip_ids=clip_ids, until=until)
        status = self.wait_statuses.pop(0) if self.wait_statuses \
            else "complete"
        return {"status": status, "id": (clip_ids or [clip_id])[0]}

    def audio_retrieve(self, clip_id):
        self._record("audio_retrieve", clip_id)
        title = "Library Song" if clip_id == "lib-1" else "Test Song"
        payload = clip_payload(clip_id, title)
        payload["status"] = self.retrieve_status
        return payload

    def audio_analysis(self, clip_id):
        self._record("audio_analysis", clip_id)
        if self.analysis_error is not None:
            raise self.analysis_error
        return dict(self.analysis_payload)

    def audio_download(self, clip_id, format="wav", confirm=False):
        self._record("audio_download", clip_id, format=format,
                     confirm=confirm)
        if confirm:
            self.download_confirm_calls.append(clip_id)
        if self.download_requires_credit and not confirm:
            return {"status": "requires_credit",
                    "error": "requires_credit"}
        return {"url": "https://files.example/%s.%s" % (clip_id, format)}

    def download_to_file(self, url, dest_path):
        self._record("download_to_file", url, str(dest_path))
        Path(dest_path).write_bytes(b"FAKE-WAV-BYTES")
        return Path(dest_path)

    def audio_upload_create(self, title=None):
        self._record("audio_upload_create", title=title)
        return {"id": "upl-1",
                "upload_url": "https://storage.example/put/upl-1"}

    def put_bytes(self, url, data, content_type):
        self._record("put_bytes", url, data, content_type)

    def audio_upload_complete(self, session_id, title=None):
        self._record("audio_upload_complete", session_id)
        return {"id": "src-1"}

    def audio_covers_create(self, source_id, **params):
        self._record("audio_covers_create", source_id, **params)
        return {"clip_ids": ["cov-1"]}

    def audio_extensions_create(self, source_id, continue_at, **params):
        self._record("audio_extensions_create", source_id,
                     continue_at, **params)
        return {"clip_ids": ["ext-1"]}

    def audio_full_song(self, clip_id, idempotency_key=None):
        self._record("audio_full_song", clip_id,
                     idempotency_key=idempotency_key)
        return {"id": "full-1"}

    def library_list(self, **params):
        self._record("library_list", **params)
        return {"items": list(self.library_items)}

    def library_search(self, query, **params):
        self._record("library_search", query, **params)
        return {"items": list(self.library_items)}


class FakePlacer:
    def __init__(self, has_clip=True):
        self.placements = []
        self.has_clip = has_clip

    def place_audio_file(self, path, track_name, tempo_bpm=None):
        self.placements.append({
            "path": path, "track_name": track_name,
            "tempo_bpm": tempo_bpm,
        })
        return {
            "track_index": 7, "clip_index": 0,
            "track_name": track_name,
            "clip": {"name": track_name, "length": 16.0},
            "tempo_set": tempo_bpm,
        }

    def get_clip_slot(self, track_index, clip_index):
        if not self.has_clip:
            return {"has_clip": False, "clip": None}
        return {"has_clip": True,
                "clip": {"name": "Source Loop", "length": 8.0}}


def make_bridge(tmp_path, suno=None, placer=None, clock=None):
    return SunoBridge(
        suno if suno is not None else FakeSuno(),
        placer if placer is not None else FakePlacer(),
        tmp_path / "downloads",
        clock=clock if clock is not None else FakeClock(),
        sleeper=lambda seconds: None,
    )


# -------------------------------------------------------- bridge tests


def test_bridge_status_reports_credits(tmp_path):
    bridge = make_bridge(tmp_path)
    status = bridge.status()
    assert status["credits_remaining"] == 20000
    assert status["account"]["handle"] == "immovableorganist719"


def test_generate_pins_clip_count_and_idempotency(tmp_path):
    suno = FakeSuno()
    bridge = make_bridge(tmp_path, suno)
    result = bridge.generate(description="a synthwave loop")
    assert result["status"] == "complete"
    assert result["clip_ids"] == ["gen-1"]
    create = suno.calls_of("audio_create")[0][2]
    # Suno defaults an MCP sign-in to 2 - the pin is the point.
    assert create["clip_count"] == 1
    assert create["description"] == "a synthwave loop"
    key = create["idempotency_key"]
    assert key.startswith("mcp15-create-")
    # A retry of the same logical creation derives the same key...
    bridge.generate(description="a synthwave loop")
    assert suno.calls_of("audio_create")[1][2]["idempotency_key"] == key
    # ...and a different request derives a different one.
    bridge.generate(description="a different loop")
    assert suno.calls_of("audio_create")[2][2]["idempotency_key"] != key
    assert derive_idempotency_key("create", {"a": 1}) == \
        derive_idempotency_key("create", {"a": 1})


def test_generate_clip_count_two_is_explicit_and_three_refused(tmp_path):
    suno = FakeSuno()
    bridge = make_bridge(tmp_path, suno)
    result = bridge.generate(description="two takes", clip_count=2)
    assert result["clip_ids"] == ["gen-1", "gen-2"]
    assert suno.calls_of("audio_create")[0][2]["clip_count"] == 2
    with pytest.raises(SunoBridgeError):
        bridge.generate(description="three", clip_count=3)


def test_generate_refuses_when_credits_cannot_cover(tmp_path):
    suno = FakeSuno(credits=1)
    bridge = make_bridge(tmp_path, suno)
    with pytest.raises(InsufficientCreditsError):
        bridge.generate(description="two takes", clip_count=2)
    assert suno.calls_of("audio_create") == []
    # One clip is still affordable at 1 credit.
    result = bridge.generate(description="one take")
    assert result["status"] == "complete"


def test_generate_mode_mixing_and_empty_input_refused(tmp_path):
    bridge = make_bridge(tmp_path)
    with pytest.raises(SunoBridgeError):
        bridge.generate(description="d", style="s")
    with pytest.raises(SunoBridgeError):
        bridge.generate()


def test_generate_duration_strict_always_explicit(tmp_path):
    suno = FakeSuno()
    bridge = make_bridge(tmp_path, suno)
    bridge.generate(lyrics="la", duration=30)
    create = suno.calls_of("audio_create")[0][2]
    assert create["duration"] == 30
    assert create["duration_strict"] is False
    bridge.generate(lyrics="la")
    assert "duration_strict" not in suno.calls_of("audio_create")[1][2]


def test_generate_wait_loop_until_complete(tmp_path):
    suno = FakeSuno()
    suno.wait_statuses = ["pending", "streaming", "complete"]
    bridge = make_bridge(tmp_path, suno)
    result = bridge.generate(description="slow song")
    assert result["status"] == "complete"
    assert len(suno.calls_of("audio_wait")) == 3


def test_generate_wait_budget_reports_pending_honestly(tmp_path):
    suno = FakeSuno()
    suno.wait_statuses = ["pending"] * 50
    suno.retrieve_status = "streaming"
    bridge = make_bridge(tmp_path, suno, clock=FakeClock(step=400.0))
    result = bridge.generate(description="never done",
                             wait_budget_seconds=600)
    assert result["status"] == "pending"
    assert result["clip_ids"] == ["gen-1"]
    assert "Nothing was re-submitted" in result["note"]
    assert len(suno.calls_of("audio_create")) == 1


def test_generate_to_live_places_and_writes_sidecar(tmp_path):
    suno = FakeSuno()
    placer = FakePlacer()
    bridge = make_bridge(tmp_path, suno, placer)
    result = bridge.generate_to_live(
        lyrics="la la", style="synthwave", title="Neon",
        match_tempo=True,
    )
    assert result["status"] == "placed"
    assert len(placer.placements) == 1
    placement = placer.placements[0]
    assert placement["track_name"] == "Neon [suno:gen-1]"
    # Tempo set only because match_tempo AND a measured bpm.
    assert placement["tempo_bpm"] == 122.0
    assert result["analysis"]["bpm"] == 122.0
    assert result["analysis"]["key"] == "A minor"
    sidecar = Path(result["sidecar"])
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text())
    assert data["suno_clip_id"] == "gen-1"
    assert data["source"] == "generate"
    assert data["track_index"] == 7
    assert data["tempo_matched"] is True
    assert data["prompt"]["style"] == "synthwave"
    assert Path(result["download"]["path"]).read_bytes() == \
        b"FAKE-WAV-BYTES"


def test_generate_to_live_without_match_tempo_sets_no_tempo(tmp_path):
    placer = FakePlacer()
    bridge = make_bridge(tmp_path, FakeSuno(), placer)
    result = bridge.generate_to_live(description="x")
    assert result["status"] == "placed"
    assert placer.placements[0]["tempo_bpm"] is None
    assert result["sidecar_data"]["tempo_matched"] is False


def test_download_credit_gate_stops_unless_allowed(tmp_path):
    suno = FakeSuno()
    suno.download_requires_credit = True
    placer = FakePlacer()
    bridge = make_bridge(tmp_path, suno, placer)
    result = bridge.generate_to_live(description="gated")
    assert result["status"] == "requires_download_credit"
    assert placer.placements == []          # nothing placed
    assert suno.download_confirm_calls == []  # nothing confirmed
    # With the caller's explicit approval the credit is spent and
    # the song lands.
    result = bridge.generate_to_live(
        description="gated", allow_download_credit=True
    )
    assert result["status"] == "placed"
    assert suno.download_confirm_calls == ["gen-1"]


def test_analysis_failure_never_blocks_placement(tmp_path):
    suno = FakeSuno()
    suno.analysis_error = SunoToolError(
        "audio not found", code="not_found", tool="audio-analysis"
    )
    placer = FakePlacer()
    bridge = make_bridge(tmp_path, suno, placer)
    result = bridge.generate_to_live(description="x", match_tempo=True)
    assert result["status"] == "placed"
    assert result["analysis"] is None
    assert placer.placements[0]["tempo_bpm"] is None


def test_library_to_live_by_query_spends_no_generation(tmp_path):
    suno = FakeSuno()
    placer = FakePlacer()
    bridge = make_bridge(tmp_path, suno, placer)
    result = bridge.library_to_live(query="library")
    assert result["status"] == "placed"
    assert result["clip_id"] == "lib-1"
    assert suno.calls_of("audio_create") == []
    assert suno.calls_of("library_search")
    assert placer.placements[0]["track_name"] == \
        "Library Song [suno:lib-1]"


def test_library_to_live_no_match_refused(tmp_path):
    suno = FakeSuno()
    suno.library_items = []
    bridge = make_bridge(tmp_path, suno)
    with pytest.raises(SunoBridgeError):
        bridge.library_to_live(query="nothing")


def test_cover_clip_upload_flow_and_vocal_lyrics_rule(tmp_path):
    suno = FakeSuno()
    # The uploaded clip carries lyrics in its metadata.
    original_retrieve = suno.audio_retrieve

    def retrieve(clip_id):
        suno._record("audio_retrieve", clip_id)
        return clip_payload(clip_id, lyrics="source words")

    suno.audio_retrieve = retrieve
    bridge = make_bridge(tmp_path, suno)
    source_file = tmp_path / "loop.wav"
    source_file.write_bytes(b"LOOP-BYTES")
    result = bridge.cover_clip(
        track_index=0, clip_index=0, file_path=str(source_file),
        style="bossa nova", keep_vocals=True,
    )
    assert result["status"] == "placed"
    names = [c[0] for c in suno.calls]
    # The PUT flow ran in order, on the clip's own bytes.
    assert names.index("audio_upload_create") < \
        names.index("put_bytes") < \
        names.index("audio_upload_complete") < \
        names.index("audio_covers_create")
    put = suno.calls_of("put_bytes")[0]
    assert put[1][1] == b"LOOP-BYTES"
    assert put[1][2] == "audio/wav"
    cover = suno.calls_of("audio_covers_create")[0]
    assert cover[1][0] == "src-1"
    # Vocals survive because the source lyrics were passed
    # explicitly - Suno does not carry them over on its own.
    assert cover[2]["lyrics"] == "source words"
    assert cover[2]["style"] == "bossa nova"
    assert original_retrieve is not None


def test_cover_clip_without_vocal_rule_passes_no_lyrics(tmp_path):
    suno = FakeSuno()
    bridge = make_bridge(tmp_path, suno)
    source_file = tmp_path / "loop.wav"
    source_file.write_bytes(b"LOOP-BYTES")
    result = bridge.cover_clip(
        track_index=0, clip_index=0, file_path=str(source_file),
        style="techno",
    )
    assert result["status"] == "placed"
    cover = suno.calls_of("audio_covers_create")[0]
    assert cover[2]["lyrics"] is None


def test_cover_clip_empty_slot_refused_before_upload(tmp_path):
    suno = FakeSuno()
    bridge = make_bridge(tmp_path, suno, FakePlacer(has_clip=False))
    source_file = tmp_path / "loop.wav"
    source_file.write_bytes(b"LOOP-BYTES")
    with pytest.raises(SunoBridgeError):
        bridge.cover_clip(track_index=0, clip_index=0,
                          file_path=str(source_file))
    assert suno.calls_of("audio_upload_create") == []


def test_extend_clip_joins_full_song_and_places(tmp_path):
    suno = FakeSuno()
    bridge = make_bridge(tmp_path, suno)
    source_file = tmp_path / "loop.wav"
    source_file.write_bytes(b"LOOP-BYTES")
    result = bridge.extend_clip(
        track_index=1, clip_index=2, file_path=str(source_file),
        continue_at=12.5,
    )
    assert result["status"] == "placed"
    assert result["clip_id"] == "full-1"
    extend = suno.calls_of("audio_extensions_create")[0]
    assert extend[1][0] == "src-1"
    assert extend[1][1] == 12.5
    joined = suno.calls_of("audio_full_song")
    assert joined and joined[0][1][0] == "ext-1"


def test_upload_missing_file_refused(tmp_path):
    bridge = make_bridge(tmp_path)
    with pytest.raises(SunoBridgeError):
        bridge.upload_file(str(tmp_path / "nope.wav"))


# -------------------------------------------------------- server tools


class StubBridge:
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append(("status", {}))
        return {"credits_remaining": 42}

    def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        return {"status": "complete", "clip_ids": ["gen-1"]}


@pytest.fixture
def stub_bridge(monkeypatch):
    stub = StubBridge()
    monkeypatch.setattr(server_module, "_suno_bridge", stub)
    return stub


def test_suno_tools_registered_additively():
    names = set(server_module.mcp._tool_manager._tools)
    assert {
        "suno_status", "suno_generate", "suno_generate_to_live",
        "suno_library_to_live", "suno_cover_clip", "suno_extend_clip",
    } <= names


def _tool_json(out):
    """Tool output in the default profile is the JSON document plus
    the dataset layer's consent footer - decode the document."""
    value, _end = json.JSONDecoder().raw_decode(out)
    return value


def test_suno_status_tool_returns_bridge_json(stub_bridge):
    out = call(server_module.suno_status, None)
    assert _tool_json(out) == {"credits_remaining": 42}
    assert stub_bridge.calls == [("status", {})]


def test_suno_generate_tool_converts_empty_strings(stub_bridge):
    out = call(server_module.suno_generate, None)
    payload = _tool_json(out)
    assert payload["status"] == "complete"
    _name, kwargs = stub_bridge.calls[0]
    assert kwargs["description"] is None
    assert kwargs["lyrics"] is None
    assert kwargs["duration"] is None
    assert kwargs["clip_count"] == 1


# ------------------------------------------------------------- placer


class FakeConn:
    """send_command stub in the MCP-04 pattern."""

    def __init__(self, placed=True):
        self.calls = []
        self.placed = placed

    def send_command(self, command_type, params=None, **kwargs):
        self.calls.append((command_type, params))
        if command_type == "get_session_info":
            return {"tempo": 120.0, "track_count": 3,
                    "tracks": [{}, {}, {}]}
        if command_type == "create_audio_track":
            return {"name": "4-Audio"}  # note: no index, on purpose
        if command_type == "create_audio_clip":
            return {"name": "Neon", "length": 16.0,
                    "is_audio_clip": True}
        if command_type == "get_track_info":
            clip = {"name": "Neon", "length": 16.0} if self.placed \
                else None
            return {"clip_slots": [
                {"index": 0, "has_clip": self.placed, "clip": clip},
                {"index": 1, "has_clip": False, "clip": None},
            ]}
        return {}


def test_placer_places_verifies_and_sets_tempo():
    conn = FakeConn()
    placer = server_module._SunoLivePlacer(lambda: conn)
    result = placer.place_audio_file(
        "/tmp/Neon.wav", "Neon [suno:gen-1]", tempo_bpm=122.0
    )
    assert result["track_index"] == 3  # fell back to the track count
    assert result["clip"]["name"] == "Neon"
    assert result["tempo_set"] == 122.0
    order = [name for name, _p in conn.calls]
    assert order == [
        "get_session_info", "create_audio_track", "set_track_name",
        "create_audio_clip", "set_tempo", "get_track_info",
    ]
    create = dict(conn.calls)["create_audio_clip"]
    assert create == {"track_index": 3, "clip_index": 0,
                      "path": "/tmp/Neon.wav"}


def test_placer_without_tempo_sends_no_set_tempo():
    conn = FakeConn()
    placer = server_module._SunoLivePlacer(lambda: conn)
    placer.place_audio_file("/tmp/x.wav", "X [suno:a]")
    assert "set_tempo" not in [name for name, _p in conn.calls]


def test_placer_failed_readback_raises():
    conn = FakeConn(placed=False)
    placer = server_module._SunoLivePlacer(lambda: conn)
    with pytest.raises(Exception, match="verification failed"):
        placer.place_audio_file("/tmp/x.wav", "X [suno:a]")


def test_placer_get_clip_slot():
    conn = FakeConn()
    placer = server_module._SunoLivePlacer(lambda: conn)
    slot = placer.get_clip_slot(0, 0)
    assert slot["has_clip"] is True
    assert slot["clip"]["name"] == "Neon"
    assert placer.get_clip_slot(0, 9) is None
