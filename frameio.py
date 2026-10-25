"""Frame.io (V4, Adobe sign-in): make a job's Frame.io project and its folders.

Signing in uses an Adobe Developer Console credential of type "OAuth Web App" (or "OAuth Single Page App", then
without a secret) with the Frame.io API added. Its client ID goes in the config; the client secret and the refresh
token go in the Mac keychain, never in a file. The browser comes back to the dashboard's /frameio/callback.

    "frameio": {"enabled": true, "client_id": "…", "account_id": "…", "workspace_id": "…", "workspace": "Wash",
                "template": "FrameIO", "project": "{job}"}

Kept apart from the backup engine like project_setup: nothing here can stop backups.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IMS = "https://ims-na1.adobelogin.com"
AUTHORIZE_URL = IMS + "/ims/authorize/v2"
TOKEN_URL = IMS + "/ims/token/v3"
API = "https://api.frame.io"
SCOPES = "openid,email,profile,offline_access,additional_info.roles"
KEYCHAIN_SERVICE = "Colour Project Manager Frame.io"
LOGIN_TTL = 15 * 60          # seconds a sign-in link stays valid
FOLDER_GAP = 0.35            # Frame.io allows 3 new folders a second
CACHE_TTL = 60
_SECRET_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{1,4096}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

SIGNED_OUT = "Frame.io isn't connected. Connect it under Templates and locations."


class FrameIOError(Exception):
    """Something to show the user."""


class NotConnected(FrameIOError):
    pass


class Keychain:
    """Secrets in the login keychain, through /usr/bin/security. Values never go on a command line."""

    def __init__(self, service: str = KEYCHAIN_SERVICE, run=subprocess.run):
        self.service = service
        self.run = run
        self._mem: dict[str, str] | None = {} if os.environ.get("RCB_NO_KEYCHAIN") else None  # tests

    def get(self, name: str) -> str | None:
        if self._mem is not None:
            return self._mem.get(name)
        try:
            r = self.run(["/usr/bin/security", "find-generic-password", "-s", self.service, "-a", name, "-w"],
                         capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() or None if r.returncode == 0 else None

    def set(self, name: str, value: str) -> bool:
        if not _SECRET_RE.match(value or "") or not re.match(r"^[A-Za-z0-9_]+$", name):
            raise ValueError("That doesn't look like a valid value.")
        if self._mem is not None:
            self._mem[name] = value
            return True
        cmd = f'add-generic-password -U -s "{self.service}" -a "{name}" -l "{self.service}" -w "{value}"\n'
        try:
            r = self.run(["/usr/bin/security", "-i"], input=cmd, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and self.get(name) == value

    def delete(self, name: str) -> None:
        if self._mem is not None:
            self._mem.pop(name, None)
            return
        try:
            self.run(["/usr/bin/security", "delete-generic-password", "-s", self.service, "-a", name],
                     capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass


def _ssl_context() -> ssl.SSLContext:
    try:  # the built app has no system certificate file; certifi is bundled with it
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http_request(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 20):
    """(status, headers, parsed JSON or None). Network failures raise OSError."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            raw, status, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, dict(e.headers or {})
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        data = None
    return status, hdrs, data


def _error_text(data) -> str:
    if isinstance(data, dict):
        errs = data.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            return str(errs[0].get("detail") or errs[0].get("title") or "")
        return str(data.get("error_description") or data.get("error") or data.get("message") or "")
    return ""


class FrameIO:
    """One per service; safe to call from the dashboard's threads."""

    def __init__(self, get_cfg, state_dir: Path, keychain: Keychain | None = None, http=None,
                 clock=time.time, sleep=time.sleep):
        self.get_cfg = get_cfg
        self.state_file = Path(state_dir) / "frameio.json"
        self.keychain = keychain or Keychain()
        self.http = http or http_request
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self._access: tuple[str, float] | None = None   # (token, expires at)
        self._pending: dict[str, dict] = {}             # sign-in state -> {verifier, redirect, at}
        self._cache: dict[tuple, tuple[float, list]] = {}

    # ---- config and status
    def cfg(self) -> dict:
        c = (self.get_cfg().get("project_setup") or {}).get("frameio") or {}
        return {"enabled": bool(c.get("enabled")), "client_id": c.get("client_id"), "account_id": c.get("account_id"),
                "workspace_id": c.get("workspace_id"), "workspace": c.get("workspace"),
                "template": c.get("template") or "FrameIO", "project": c.get("project") or "{job}"}

    def _info(self) -> dict:
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _set_info(self, d: dict | None) -> None:
        try:
            if d is None:
                self.state_file.unlink(missing_ok=True)
            else:
                tmp = self.state_file.with_name(".frameio.json.tmp")
                tmp.write_text(json.dumps(d), encoding="utf-8")
                os.replace(tmp, self.state_file)
        except OSError:
            pass

    def connected(self) -> bool:
        return bool(self.keychain.get("refresh_token"))

    def status(self) -> dict:
        c = self.cfg()
        if not (c["enabled"] or c["client_id"]):  # not being set up: don't touch the keychain at all
            return {"client_id": False, "secret": False, "connected": False, "user": None, "since": None}
        info = self._info()
        connected = self.connected()
        return {"client_id": bool(c["client_id"]), "secret": bool(self.keychain.get("client_secret")),
                "connected": connected, "user": info.get("user") if connected else None,
                "since": info.get("connected_at") if connected else None}

    def save_secret(self, secret: str) -> None:
        secret = (secret or "").strip()
        if not secret:
            self.keychain.delete("client_secret")
            return
        if not _SECRET_RE.match(secret):
            raise FrameIOError("That doesn't look like a client secret. Copy it again from the Adobe Developer Console.")
        if not self.keychain.set("client_secret", secret):
            raise FrameIOError("Couldn't save it in the keychain.")

    # ---- sign-in
    def login_url(self, redirect_uri: str) -> str:
        c = self.cfg()
        if not c["client_id"]:
            raise FrameIOError("Enter and save the Client ID first.")
        now = self.clock()
        state = secrets.token_urlsafe(24)
        q = {"client_id": c["client_id"], "redirect_uri": redirect_uri, "scope": SCOPES, "response_type": "code",
             "state": state}
        verifier = None
        if not self.keychain.get("client_secret"):  # a Single Page App credential: PKCE instead of a secret
            verifier = secrets.token_urlsafe(48)
            q["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            q["code_challenge_method"] = "S256"
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if now - v["at"] < LOGIN_TTL}
            self._pending[state] = {"verifier": verifier, "redirect": redirect_uri, "at": now}
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)

    def finish_login(self, code: str, state: str) -> str:
        """Swap the code from the browser for tokens. Returns who signed in."""
        with self._lock:
            p = self._pending.pop(state or "", None)
        if not p or self.clock() - p["at"] > LOGIN_TTL:
            raise FrameIOError("That sign-in link has expired or was already used. Press Connect Frame.io again.")
        if not code:
            raise FrameIOError("Adobe didn't send a sign-in code.")
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect"]}
        if p["verifier"]:
            form["code_verifier"] = p["verifier"]
        self._token_request(form, signing_in=True)
        try:
            me = self.request("GET", "/v4/me").get("data") or {}
            user = " ".join(x for x in (me.get("name"), f"({me['email']})" if me.get("email") else None) if x) or None
        except FrameIOError:
            user = None
        self._set_info({"user": user, "connected_at": int(self.clock())})
        return user or "your Adobe ID"

    def disconnect(self) -> None:
        with self._lock:
            self._access = None
            self._cache.clear()
            self.keychain.delete("refresh_token")
            self._set_info(None)

    def _token_request(self, form: dict, signing_in: bool = False) -> None:
        c = self.cfg()
        if not c["client_id"]:
            raise NotConnected("Enter the Frame.io Client ID under Templates and locations.")
        form = {**form, "client_id": c["client_id"]}
        secret = self.keychain.get("client_secret")
        if secret:
            form["client_secret"] = secret
        try:
            status, _h, data = self.http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded",
                                                             "Accept": "application/json"},
                                         urllib.parse.urlencode(form).encode())
        except OSError as e:
            raise FrameIOError(f"Couldn't reach Adobe to sign in: {getattr(e, 'reason', None) or e}.") from e
        if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            why = _error_text(data) or f"error {status}"
            if signing_in:
                raise FrameIOError(f"Adobe refused the sign-in: {why}.")
            if status in (400, 401, 403):  # the refresh token has expired or been revoked
                self.disconnect()
                raise NotConnected("Frame.io has signed out. Connect it again under Templates and locations.")
            raise FrameIOError(f"Adobe sign-in failed: {why}.")
        self._access = (data["access_token"], self.clock() + float(data.get("expires_in") or 3600))
        if data.get("refresh_token"):
            if not self.keychain.set("refresh_token", data["refresh_token"]):
                raise FrameIOError("Signed in, but couldn't save it in the keychain.")
        elif signing_in:
            raise FrameIOError("Adobe didn't allow staying signed in. Check the credential has the offline_access scope.")

    def _token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._access and self._access[1] - 60 > self.clock():
                return self._access[0]
            refresh = self.keychain.get("refresh_token")
            if not refresh:
                raise NotConnected(SIGNED_OUT)
            self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
            return self._access[0]  # type: ignore[index]

    # ---- the API
    def request(self, method: str, path: str, body=None, query: dict | None = None) -> dict:
        url = path if path.startswith("https://") else API + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        retried_auth = refresh = False
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self._token(force=refresh)}", "Accept": "application/json"}
            refresh = False
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                status, hdrs, out = self.http(method, url, headers, data)
            except OSError as e:
                raise FrameIOError(f"Couldn't reach Frame.io: {getattr(e, 'reason', None) or e}.") from e
            if status == 401 and not retried_auth:  # the access token may have just been revoked: refresh once
                retried_auth = refresh = True
                continue
            if status == 429 and attempt < 3:
                try:
                    wait = float({k.lower(): v for k, v in hdrs.items()}.get("retry-after") or 2)
                except ValueError:
                    wait = 2
                self.sleep(min(max(wait, 0.5), 15))
                continue
            if 200 <= status < 300:
                return out if isinstance(out, dict) else {}
            why = _error_text(out) or f"error {status}"
            if status == 401:
                raise FrameIOError(f"Frame.io didn't accept the sign-in ({why}). Use the same Adobe ID as in Frame.io, "
                                   "or connect again.")
            if status == 403:
                raise FrameIOError(f"Frame.io says this account isn't allowed to do that ({why}).")
            raise FrameIOError(f"Frame.io said: {why}.")
        raise FrameIOError("Frame.io is busy. Try again in a minute.")

    def _all(self, path: str) -> list:
        out, url, pages = [], path, 0
        while url and pages < 50:
            d = self.request("GET", url, query={"page_size": 100})
            out += d.get("data") or []
            url = (d.get("links") or {}).get("next")
            pages += 1
        return out

    def workspaces(self) -> list[dict]:
        """Every workspace the signed-in user can see: [{account_id, account, workspace_id, workspace}]."""
        out = []
        for a in self._all("/v4/accounts"):
            for w in self._all(f"/v4/accounts/{a['id']}/workspaces"):
                out.append({"account_id": a["id"], "account": a.get("display_name") or a["id"],
                            "workspace_id": w["id"], "workspace": w.get("name") or w["id"]})
        return out

    def projects(self, account_id: str, workspace_id: str, fresh: bool = False) -> list[dict]:
        key = (account_id, workspace_id)
        hit = self._cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        val = self._all(f"/v4/accounts/{account_id}/workspaces/{workspace_id}/projects")
        self._cache[key] = (time.monotonic(), val)
        return val

    def find_project(self, name: str, fresh: bool = False) -> dict | None:
        c = self.cfg()
        return next((p for p in self.projects(c["account_id"], c["workspace_id"], fresh) if p.get("name") == name), None)

    def make_project(self, name: str, folders: list[str]) -> dict:
        """Make the project and its folders (parents first). On failure, deletes the project again and raises."""
        c = self.cfg()
        acc, ws = c["account_id"], c["workspace_id"]
        if self.find_project(name, fresh=True):
            raise FrameIOError(f"A Frame.io project called {name} already exists.")
        p = self.request("POST", f"/v4/accounts/{acc}/workspaces/{ws}/projects", {"data": {"name": name}}).get("data") or {}
        self._cache.pop((acc, ws), None)
        if not p.get("id") or not p.get("root_folder_id"):
            raise FrameIOError("Frame.io didn't say which project it made.")
        try:
            ids = {"": p["root_folder_id"]}
            for rel in folders:
                parent, _, leaf = rel.rpartition("/")
                self.sleep(FOLDER_GAP)
                f = self.request("POST", f"/v4/accounts/{acc}/folders/{ids[parent]}/folders",
                                 {"data": {"name": leaf}}).get("data") or {}
                if not f.get("id"):
                    raise FrameIOError(f"Frame.io didn't make the folder {rel}.")
                ids[rel] = f["id"]
        except FrameIOError as e:
            try:
                self.request("DELETE", f"/v4/accounts/{acc}/projects/{p['id']}")
            except FrameIOError:
                raise FrameIOError(f"{e} The half-made project {name} couldn't be removed, so delete it in "
                                   "Frame.io.") from e
            raise
        return {"id": p["id"], "url": p.get("view_url"), "folders": len(folders)}

    def delete_project(self, project_id: str) -> None:
        c = self.cfg()
        self.request("DELETE", f"/v4/accounts/{c['account_id']}/projects/{project_id}")
        self._cache.clear()
