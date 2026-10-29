"""MCP-15: Suno OAuth for the bridge code itself.

The bridge cannot borrow any other client's grant - Suno's user
tools refuse API-key principals outright, and another application's
stored token is not this code's to use. So the fork registers its OWN
OAuth client with Suno and runs the authorization-code flow:

* dynamic client registration (RFC 7591) against Suno's register
  endpoint - no pre-created app, no client secret
  (``token_endpoint_auth_method: none``);
* authorization-code + PKCE S256, with the canonical MCP ``resource``
  (``https://api.suno.com/mcp``) sent on authorize / token / refresh;
* the granted token set is stored through a :class:`TokenStore`:
  the macOS Keychain in real use (the service this project already
  trusts for the Stripe CLI), an injected store in tests.

The one-time interactive sign-in is the ``ableton-mcp-suno-auth``
command (``python -m MCP_Server.suno_auth``). Nothing in this module
runs at MCP-server import time, and no sign-in happens during the
test suite - tests drive the flow against fake endpoints.
"""

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .suno_client import USER_AGENT, SunoAuthError

ISSUER = "https://studio-api-prod.suno.com/api/v2/external/oauth"
AUTHORIZE_URL = ISSUER + "/authorize/"
TOKEN_URL = ISSUER + "/token/"
REGISTER_URL = ISSUER + "/register/"
REVOKE_URL = ISSUER + "/revoke_token/"
RESOURCE = "https://api.suno.com/mcp"
SCOPES = (
    "api:v0 credits:spend download library:read library:search "
    "library:write offline_access profile:read"
)
CLIENT_NAME = "AbletonMCP Suno Bridge"
DEFAULT_REDIRECT_PORT = 8765
KEYCHAIN_SERVICE = "ableton-mcp-suno"
KEYCHAIN_ACCOUNT = "oauth-tokens"

# Treated as expired this many seconds early, so a token is never
# handed out on the edge of its lifetime.
_EXPIRY_SKEW = 60


# ------------------------------------------------------------- stores


class TokenStore:
    """Where the OAuth token set (a JSON-able dict) lives."""

    def load(self):
        raise NotImplementedError

    def save(self, tokens):
        raise NotImplementedError

    def clear(self):
        raise NotImplementedError


class InMemoryTokenStore(TokenStore):
    def __init__(self, tokens=None):
        self._tokens = dict(tokens) if tokens else None

    def load(self):
        return dict(self._tokens) if self._tokens else None

    def save(self, tokens):
        self._tokens = dict(tokens)

    def clear(self):
        self._tokens = None


class FileTokenStore(TokenStore):
    """A JSON file store (tests and non-macOS development).

    The file is written with 0600 permissions; it holds a refresh
    token, so treat it like a credential file.
    """

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, tokens):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(tokens), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self):
        try:
            self.path.unlink()
        except OSError:
            pass


class KeychainTokenStore(TokenStore):
    """The macOS login Keychain, via the ``security`` CLI.

    The whole token set is one generic-password item. The runner is
    injectable so tests never touch a real keychain.
    """

    def __init__(self, runner=None, service=KEYCHAIN_SERVICE,
                 account=KEYCHAIN_ACCOUNT):
        self._runner = runner or self._run_security
        self.service = service
        self.account = account

    @staticmethod
    def _run_security(argv):
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=30
        )
        return proc.returncode, proc.stdout

    def load(self):
        code, out = self._runner([
            "security", "find-generic-password",
            "-a", self.account, "-s", self.service, "-w",
        ])
        if code != 0 or not out.strip():
            return None
        try:
            return json.loads(out.strip())
        except ValueError:
            return None

    def save(self, tokens):
        code, _out = self._runner([
            "security", "add-generic-password",
            "-a", self.account, "-s", self.service,
            "-w", json.dumps(tokens), "-U",
        ])
        if code != 0:
            raise SunoAuthError(
                "Could not store the Suno token in the macOS Keychain"
            )

    def clear(self):
        self._runner([
            "security", "delete-generic-password",
            "-a", self.account, "-s", self.service,
        ])


# ------------------------------------------------------------ http io


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SunoAuthError(
            "Suno OAuth endpoint redirected; refusing to follow with "
            "credentials in flight"
        )


class OAuthHttp:
    """Form/JSON posts to the OAuth endpoints. Injectable for tests."""

    def __init__(self, user_agent=USER_AGENT):
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _post(self, url, body, content_type, timeout=30):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SunoAuthError(
                "Suno OAuth request did not complete: %s" % (error,)
            ) from error

    def post_form(self, url, fields):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        status, raw = self._post(
            url, body, "application/x-www-form-urlencoded"
        )
        return status, _parse_json_body(raw)

    def post_json(self, url, payload):
        body = json.dumps(payload).encode("utf-8")
        status, raw = self._post(url, body, "application/json")
        return status, _parse_json_body(raw)


def _parse_json_body(raw):
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# -------------------------------------------------------------- flow


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce_pair():
    """(verifier, S256 challenge) for one authorization attempt."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class SunoOAuth:
    """The fork's own OAuth client against Suno's issuer."""

    def __init__(self, store, *, http=None, issuer=ISSUER,
                 redirect_uri="http://127.0.0.1:%d/callback"
                 % DEFAULT_REDIRECT_PORT,
                 clock=time.time):
        self.store = store
        self.http = http if http is not None else OAuthHttp()
        self.issuer = issuer.rstrip("/")
        self.redirect_uri = redirect_uri
        self._clock = clock

    # -- registration -------------------------------------------------

    def register_client(self):
        """Dynamic client registration; stores and returns client_id."""
        status, body = self.http.post_json(
            self.issuer + "/register/",
            {
                "client_name": CLIENT_NAME,
                "redirect_uris": [self.redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": SCOPES,
            },
        )
        client_id = body.get("client_id")
        if status not in (200, 201) or not client_id:
            raise SunoAuthError(
                "Suno client registration failed (HTTP %s): %s"
                % (status, body.get("error", "no client_id returned"))
            )
        tokens = self.store.load() or {}
        tokens["client_id"] = client_id
        self.store.save(tokens)
        return client_id

    def client_id(self):
        tokens = self.store.load() or {}
        if tokens.get("client_id"):
            return tokens["client_id"]
        return self.register_client()

    # -- authorize ------------------------------------------------------

    def authorize_url(self, state, challenge):
        params = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return self.issuer + "/authorize/?" + urllib.parse.urlencode(params)

    # -- token exchange / refresh --------------------------------------

    def _token_request(self, fields):
        status, body = self.http.post_form(self.issuer + "/token/", fields)
        if status != 200 or not body.get("access_token"):
            raise SunoAuthError(
                "Suno token request failed (HTTP %s): %s"
                % (status, body.get("error", "no access_token returned"))
            )
        tokens = self.store.load() or {}
        tokens["access_token"] = body["access_token"]
        if body.get("refresh_token"):
            tokens["refresh_token"] = body["refresh_token"]
        if body.get("token_type"):
            tokens["token_type"] = body["token_type"]
        if body.get("scope"):
            tokens["scope"] = body["scope"]
        expires_in = body.get("expires_in")
        if isinstance(expires_in, (int, float)):
            tokens["expires_at"] = self._clock() + float(expires_in)
        self.store.save(tokens)
        return tokens

    def exchange_code(self, code, verifier):
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id(),
            "code_verifier": verifier,
            "resource": RESOURCE,
        })

    def refresh(self):
        tokens = self.store.load() or {}
        if not tokens.get("refresh_token"):
            raise SunoAuthError(
                "No Suno refresh token stored - run the one-time "
                "sign-in (ableton-mcp-suno-auth) first"
            )
        return self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": self.client_id(),
            "resource": RESOURCE,
        })

    # -- what SunoClient consumes ---------------------------------------

    def access_token(self):
        """A currently-valid access token, refreshing when expired."""
        tokens = self.store.load() or {}
        access = tokens.get("access_token")
        expires_at = tokens.get("expires_at")
        if access and (
            not isinstance(expires_at, (int, float))
            or expires_at - self._clock() > _EXPIRY_SKEW
        ):
            return access
        if tokens.get("refresh_token"):
            return self.refresh()["access_token"]
        raise SunoAuthError(
            "Not signed in to Suno - run the one-time sign-in "
            "(ableton-mcp-suno-auth) first"
        )

    def refresh_access_token(self):
        """SunoClient's 401 hook: refresh once, return the new token."""
        try:
            return self.refresh()["access_token"]
        except SunoAuthError:
            return None


# ------------------------------------------------------- interactive


class _CallbackHandler(BaseHTTPRequestHandler):
    """Receives the OAuth redirect on localhost; one request only."""

    result = None

    def do_GET(self):  # noqa: N802 (http.server's naming)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        type(self).result = {
            key: values[0] for key, values in params.items()
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Suno sign-in complete</h1>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *args):  # keep the terminal clean
        pass


def run_sign_in(oauth, *, opener=webbrowser.open, timeout=300):
    """Drive one interactive authorization-code + PKCE sign-in.

    Opens the authorize URL in the user's browser, serves the local
    callback, verifies the state, exchanges the code and stores the
    token set. Returns the signed-in token record (sans secrets in
    any printed form - callers print only its non-secret fields).
    """
    verifier, challenge = make_pkce_pair()
    state = _b64url(secrets.token_bytes(24))
    url = oauth.authorize_url(state, challenge)
    split = urllib.parse.urlsplit(oauth.redirect_uri)
    port = split.port or DEFAULT_REDIRECT_PORT
    _CallbackHandler.result = None
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout
    try:
        opener(url)
        server.handle_request()
    finally:
        server.server_close()
    result = _CallbackHandler.result
    if not result:
        raise SunoAuthError(
            "Suno sign-in timed out waiting for the browser callback"
        )
    if result.get("error"):
        raise SunoAuthError(
            "Suno authorization failed: %s" % (result["error"],)
        )
    if result.get("state") != state:
        raise SunoAuthError(
            "Suno authorization state mismatch - sign-in aborted"
        )
    code = result.get("code")
    if not code:
        raise SunoAuthError("Suno authorization returned no code")
    return oauth.exchange_code(code, verifier)


def default_token_store():
    """The real store: the macOS login Keychain."""
    return KeychainTokenStore()


def build_client(store=None):
    """A ready SunoClient wired to the stored grant (lazy import to
    keep this module free of any import cycle)."""
    from .suno_client import SunoClient

    oauth = SunoOAuth(store if store is not None else default_token_store())
    return SunoClient(
        oauth.access_token, refresh_token=oauth.refresh_access_token
    ), oauth


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ableton-mcp-suno-auth",
        description=(
            "One-time Suno sign-in for the AbletonMCP Suno bridge "
            "(MCP-15). Registers the fork's own OAuth client with "
            "Suno, opens your browser for approval, and stores the "
            "refresh token in the macOS Keychain."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_REDIRECT_PORT,
        help="localhost callback port (default %(default)s)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="print the authorize URL instead of opening a browser",
    )
    parser.add_argument(
        "--token-file", default=None,
        help="store tokens in this JSON file instead of the Keychain "
             "(development only; the file holds a refresh token)",
    )
    args = parser.parse_args(argv)

    store = (
        FileTokenStore(args.token_file)
        if args.token_file
        else default_token_store()
    )
    redirect_uri = "http://127.0.0.1:%d/callback" % (args.port,)
    oauth = SunoOAuth(store, redirect_uri=redirect_uri)

    def opener(url):
        if args.no_browser:
            print("Open this URL in your browser to approve Suno access:")
            print(url)
        else:
            print("Opening your browser for Suno sign-in...")
            webbrowser.open(url)
        return True

    tokens = run_sign_in(oauth, opener=opener)
    print(
        "Suno sign-in complete. Refresh token stored (%s); scopes: %s"
        % (
            "file" if args.token_file else "macOS Keychain",
            tokens.get("scope", SCOPES),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
