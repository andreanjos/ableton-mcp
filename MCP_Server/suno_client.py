"""MCP-15: a streamable-HTTP MCP client for Suno's official MCP server.

Suno's server lives at https://api.suno.com/mcp (https://suno.com/mcp
is only the landing page). This module speaks the MCP streamable-HTTP
transport directly with the standard library only: initialize /
notifications/initialized, a per-connection ``Mcp-Session-Id`` header,
and tools/call. It is deliberately transport-thin - the bridge logic
lives in :mod:`MCP_Server.suno_bridge`, the OAuth grant that feeds the
bearer token lives in :mod:`MCP_Server.suno_auth`.

Load-bearing transport facts, all verified live on 2026-10-29 (see
hidden_files/ableton-suno-bridge-design-2026-10-29.md):

* **Cloudflare bans default HTTP-client user agents.** The edge in
  front of api.suno.com answers a request carrying urllib's default
  ``Python-urllib/x.y`` user agent with HTTP 403, Cloudflare error
  1010 (``browser_signature_banned``) - the initialize call never
  reaches Suno. ``USER_AGENT`` below is an explicit, non-default
  user agent and every request this module makes carries it. Do not
  remove it and do not let a library substitute its own default.
* The server negotiates one of the protocol versions in
  ``PROTOCOL_VERSIONS`` and may answer with JSON or an event stream.
* Redirects are refused: an authenticated request that is redirected
  is a configuration error, not something to follow silently.
* Tool failures arrive as a result carrying ``isError`` (or a JSON-RPC
  error). They are surfaced as :class:`SunoToolError` /
  :class:`SunoProtocolError` carrying Suno's own ``error`` / ``code``
  fields. Nothing here retries a tool call: a creation may already
  have spent credits, so a retry is the caller's decision, made with
  an idempotency key.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "https://api.suno.com/mcp"

# Explicit, non-default User-Agent - see the module docstring. Suno's
# Cloudflare edge bans default client user agents (error 1010).
USER_AGENT = (
    "AbletonMCP-SunoBridge/1.0 (MCP Client; "
    "github.com/andreanjos/ableton-mcp)"
)

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

# audio-wait blocks server-side for ~50 s per call; a request timeout
# has to clear that window with slack.
DEFAULT_TIMEOUT = 75.0


class SunoError(Exception):
    """Base class for every Suno-side failure this client raises."""


class SunoTransportError(SunoError):
    """The HTTP exchange itself failed (network, redirect, timeout)."""


class SunoProtocolError(SunoError):
    """The server answered, but not with a usable MCP message."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.message = message
        self.code = code


class SunoToolError(SunoError):
    """A tool call was answered with an error payload by Suno.

    Carries Suno's own fields: ``error`` (human text) and ``code``
    (machine string, e.g. ``not_found`` / ``insufficient_credits``).
    """

    def __init__(self, error, *, code=None, tool=None):
        super().__init__(error)
        self.error = error
        self.code = code
        self.tool = tool


class SunoAuthError(SunoError):
    """No usable access token (not signed in, or a 401 that a single
    refresh could not cure)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SunoTransportError(
            "Suno endpoint redirected to %s; redirects are refused on "
            "authenticated requests - verify the endpoint" % (newurl,)
        )


class HttpTransport:
    """The real HTTP exchange, over urllib. Injectable for tests.

    Every method returns ``(status, headers, body_bytes)`` and never
    raises for an HTTP error status - the caller decides what a status
    means. Header names in the returned dict are lower-cased.
    """

    def __init__(self, user_agent=USER_AGENT):
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _open(self, request, timeout):
        try:
            with self._opener.open(request, timeout=timeout) as response:
                headers = {k.lower(): v for k, v in response.headers.items()}
                return response.status, headers, response.read()
        except urllib.error.HTTPError as error:
            headers = (
                {k.lower(): v for k, v in error.headers.items()}
                if error.headers
                else {}
            )
            return error.code, headers, error.read()
        except SunoTransportError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SunoTransportError(
                "Suno request did not complete: %s" % (error,)
            ) from error

    def post(self, url, headers, body, timeout=DEFAULT_TIMEOUT):
        merged = {"User-Agent": self.user_agent}
        merged.update(headers)
        request = urllib.request.Request(
            url, data=body, headers=merged, method="POST"
        )
        return self._open(request, timeout)

    def put(self, url, data, content_type, timeout=DEFAULT_TIMEOUT):
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": content_type,
            },
            method="PUT",
        )
        return self._open(request, timeout)

    def get(self, url, timeout=DEFAULT_TIMEOUT):
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent}, method="GET"
        )
        return self._open(request, timeout)


def _parse_tool_payload(result, tool_name):
    """Reduce an MCP tools/call result to the tool's own payload.

    Suno's tools answer with text content blocks whose text is a JSON
    document (the verified account/usage/library shapes all arrive
    this way); a structuredContent object, when present, wins. A block
    that is not JSON is returned as ``{"text": ...}`` rather than
    being silently dropped.
    """
    if not isinstance(result, dict):
        raise SunoProtocolError("tools/call returned no result object")
    if result.get("isError"):
        _raise_tool_error(result, tool_name)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    texts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    if len(texts) == 1:
        try:
            parsed = json.loads(texts[0])
        except (ValueError, TypeError):
            return {"text": texts[0]}
        if isinstance(parsed, dict):
            if parsed.get("error") and (
                parsed.get("code") or parsed.get("status") == "error"
            ):
                raise SunoToolError(
                    str(parsed["error"]),
                    code=parsed.get("code"),
                    tool=tool_name,
                )
            return parsed
        return {"value": parsed}
    if texts:
        return {"text": "\n".join(texts)}
    return {}


def _raise_tool_error(result, tool_name):
    error_text = None
    code = None
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("error"):
                error_text = str(parsed["error"])
                code = parsed.get("code")
                break
            if error_text is None:
                error_text = text
    raise SunoToolError(
        error_text or "Suno tool %s failed" % (tool_name,),
        code=code,
        tool=tool_name,
    )


class SunoClient:
    """One MCP session against Suno, created lazily on first use.

    ``token_provider`` is a zero-argument callable returning a bearer
    access token (see :mod:`MCP_Server.suno_auth`); ``refresh_token``
    is an optional zero-argument callable returning a fresh token (or
    None) used exactly once when the server answers 401.
    """

    def __init__(
        self,
        token_provider,
        *,
        endpoint=DEFAULT_ENDPOINT,
        transport=None,
        refresh_token=None,
        timeout=DEFAULT_TIMEOUT,
    ):
        split = urllib.parse.urlsplit(endpoint)
        if (
            split.scheme != "https"
            or not split.hostname
            or split.username
            or split.password
            or split.query
            or split.fragment
            or split.port not in (None, 443)
        ):
            raise SunoTransportError(
                "Suno endpoint must be a plain HTTPS URL: %r" % (endpoint,)
            )
        self.endpoint = endpoint
        self._token_provider = token_provider
        self._refresh_token = refresh_token
        self._transport = transport if transport is not None else HttpTransport()
        self._timeout = timeout
        self._session_id = None
        self._protocol = PROTOCOL_VERSIONS[0]
        self._sequence = 0
        self._initialized = False

    # ------------------------------------------------------------ rpc

    def _send(self, method, params, *, notification=False, _retried=False):
        self._sequence += 1
        request_id = None if notification else self._sequence
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        try:
            token = self._token_provider()
        except SunoError:
            raise
        except Exception as error:
            raise SunoAuthError(
                "Could not obtain a Suno access token: %s" % (error,)
            ) from error
        if not token:
            raise SunoAuthError(
                "No Suno access token - run the one-time sign-in "
                "(ableton-mcp-suno-auth) first"
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol,
            "Authorization": "Bearer " + token,
            # Set here, at the client layer, so every transport -
            # including an injected one - carries it: Suno's
            # Cloudflare edge bans default client user agents
            # (error 1010; see the module docstring).
            "User-Agent": USER_AGENT,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        status, resp_headers, resp_body = self._transport.post(
            self.endpoint,
            headers,
            json.dumps(body).encode("utf-8"),
            timeout=self._timeout,
        )
        if status == 401:
            if self._refresh_token is not None and not _retried:
                refreshed = self._refresh_token()
                if refreshed:
                    # The token was refreshed out from under this one
                    # request; replaying the identical request once
                    # with the fresh token is safe (a rejected-401
                    # request was never processed by a tool).
                    return self._send(
                        method, params,
                        notification=notification, _retried=True,
                    )
            raise SunoAuthError(
                "Suno answered 401 - the sign-in is missing or expired "
                "and could not be refreshed; run ableton-mcp-suno-auth"
            )
        session = resp_headers.get("mcp-session-id")
        if session:
            self._session_id = session
        if notification:
            if status in (200, 202, 204):
                return None
            raise SunoProtocolError(
                "Suno did not accept the initialized notification "
                "(HTTP %s)" % (status,)
            )
        if status != 200:
            raise SunoTransportError(
                "Suno MCP returned HTTP %s for %s; the operation was "
                "not replayed" % (status, method)
            )
        return self._read_result(resp_headers, resp_body, request_id)

    @staticmethod
    def _decode_message(payload, request_id):
        try:
            message = json.loads(payload)
        except (ValueError, TypeError):
            raise SunoProtocolError("Suno returned a non-JSON MCP message")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise SunoProtocolError("Suno returned an invalid JSON-RPC message")
        if type(message.get("id")) is not int or message["id"] != request_id:
            return None
        if "error" in message:
            error = message["error"] or {}
            raise SunoProtocolError(
                "Suno returned a JSON-RPC error: %s"
                % (error.get("message", error),),
                code=error.get("code"),
            )
        if not isinstance(message.get("result"), dict):
            raise SunoProtocolError("Suno returned no result object")
        return message["result"]

    def _read_result(self, headers, body, request_id):
        content_type = (headers.get("content-type") or "").split(";")[0].strip()
        if content_type == "text/event-stream":
            data = []
            for raw_line in body.split(b"\n"):
                line = raw_line.rstrip(b"\r")
                if line.startswith(b"data:"):
                    data.append(line[5:].lstrip(b" "))
                elif not line and data:
                    result = self._decode_message(b"\n".join(data), request_id)
                    data = []
                    if result is not None:
                        return result
            raise SunoProtocolError(
                "Suno event stream ended before the matching result"
            )
        result = self._decode_message(body, request_id)
        if result is None:
            raise SunoProtocolError("Suno returned the wrong request ID")
        return result

    def _ensure_session(self):
        if self._initialized:
            return
        result = self._send(
            "initialize",
            {
                "protocolVersion": self._protocol,
                "capabilities": {},
                "clientInfo": {
                    "name": "ableton-mcp-suno-bridge",
                    "version": "1",
                },
            },
        )
        protocol = result.get("protocolVersion")
        capabilities = result.get("capabilities")
        if (
            not isinstance(protocol, str)
            or protocol not in PROTOCOL_VERSIONS
            or not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("tools"), dict)
        ):
            raise SunoProtocolError(
                "Suno did not negotiate a supported tool protocol"
            )
        self._protocol = protocol
        self._send("notifications/initialized", {}, notification=True)
        self._initialized = True

    # ----------------------------------------------------------- tools

    def call_tool(self, name, arguments=None):
        self._ensure_session()
        result = self._send(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        return _parse_tool_payload(result, name)

    def list_tools(self, cursor=None):
        self._ensure_session()
        params = {"cursor": cursor} if cursor else {}
        return self._send("tools/list", params)

    @staticmethod
    def _clean(params):
        return {k: v for k, v in params.items() if v is not None}

    def account_me(self):
        return self.call_tool("account-me", {})

    def account_usage(self):
        return self.call_tool("account-usage", {})

    def audio_create(self, **params):
        return self.call_tool("audio-create", self._clean(params))

    def audio_retrieve(self, clip_id):
        return self.call_tool("audio-retrieve", {"id": clip_id})

    def audio_wait(self, clip_id=None, clip_ids=None, until="complete"):
        params = {"until": until}
        if clip_ids:
            params["ids"] = list(clip_ids)
        else:
            params["id"] = clip_id
        return self.call_tool("audio-wait", params)

    def audio_analysis(self, clip_id):
        return self.call_tool("audio-analysis", {"id": clip_id})

    def audio_download(self, clip_id, format="wav", confirm=False):
        params = {"id": clip_id, "format": format}
        if confirm:
            params["confirm"] = True
        return self.call_tool("audio-download", params)

    def audio_upload_create(self, title=None):
        return self.call_tool("audio-upload-create", self._clean({"title": title}))

    def audio_upload_complete(self, session_id, title=None):
        params = {"id": session_id}
        if title is not None:
            params["title"] = title
        return self.call_tool("audio-upload-complete", params)

    def audio_covers_create(self, source_id, **params):
        payload = {"id": source_id}
        payload.update(params)
        return self.call_tool("audio-covers-create", self._clean(payload))

    def audio_extensions_create(self, source_id, continue_at, **params):
        payload = {"id": source_id, "continue_at": continue_at}
        payload.update(params)
        return self.call_tool("audio-extensions-create", self._clean(payload))

    def audio_full_song(self, clip_id, idempotency_key=None):
        params = {"id": clip_id}
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        return self.call_tool("audio-full-song", params)

    def library_list(self, **params):
        return self.call_tool("library-list", self._clean(params))

    def library_search(self, query, **params):
        payload = {"query": query}
        payload.update(params)
        return self.call_tool("library-search", self._clean(payload))

    # -------------------------------------------------- raw file moves

    def put_bytes(self, url, data, content_type="application/octet-stream"):
        """PUT raw bytes to an upload session's ``upload_url``."""
        status, _headers, _body = self._transport.put(
            url, data, content_type, timeout=self._timeout
        )
        if status not in (200, 201, 204):
            raise SunoTransportError(
                "Upload PUT to Suno storage returned HTTP %s" % (status,)
            )

    def download_to_file(self, url, dest_path):
        """GET a download URL and write the bytes to ``dest_path``."""
        status, _headers, body = self._transport.get(
            url, timeout=self._timeout
        )
        if status != 200:
            raise SunoTransportError(
                "Download from Suno returned HTTP %s" % (status,)
            )
        dest_path.write_bytes(body)
        return dest_path
