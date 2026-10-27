"""Vidchecker (Telestream, 8.x): run QC checks on exported files from the dashboard, and show their results.

Talks to the Vidchecker server's own SOAP API (the one its web page uses), at for example http://10.0.0.66:51060:

    "vidchecker": {"url": "http://10.0.0.66:51060", "templates": [117, 116, 111, 114]}

`templates` are the ones offered in the dashboard (the "assigned" checks); the server has many more.

The server runs Windows and sees the shared drives under its own letters (W: is \\\\10.0.0.152\\WashDeliverables).
A file on this Mac is turned into the server's path by matching the share it's mounted from with the server's drive
letters, so nothing has to be set up by hand; before a check is started the server is asked whether it can see the
file, so a check is never started on a path it can't open.

Kept apart from the backup engine like project_setup: nothing here can stop backups.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

NS = "http://www.vidcheck.com/services"
API_VERSION = 11
MEDIA = {".mov", ".mxf", ".mp4", ".m4v", ".mts", ".ts", ".mpg", ".mpeg", ".wav", ".mkv", ".avi"}
CACHE_TTL = 300
# The server's drive list is slow (Windows waits on every mapped drive, dead ones too) and rarely changes.
ROOTS_TTL = 3600
SLOW_TIMEOUT = 120
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?/?$")
_MOUNT_RE = re.compile(r"^//(?:[^@/]*@)?([^/]+)/(.+?) on (/Volumes/.+?) \((\w+)")


class VidcheckerError(Exception):
    """Something to show the user."""


def http_post(url: str, headers: dict, body: bytes, timeout: float = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _strip(el: ET.Element) -> ET.Element:
    for e in el.iter():
        if isinstance(e.tag, str) and "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
        for k in list(e.attrib):
            if "}" in k:
                e.attrib[k.split("}", 1)[1]] = e.attrib.pop(k)
    return el


def _text(el: ET.Element | None, name: str, default=None):
    if el is None:
        return default
    c = el.find(name)
    return default if c is None or c.attrib.get("nil") == "true" else (c.text or "")


def mounts(run=subprocess.run) -> dict[str, tuple[str, str]]:
    """{"/Volumes/WashDeliverables": ("10.0.0.152", "WashDeliverables")} for the network shares mounted here."""
    try:
        out = run(["/sbin/mount"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.splitlines():
        m = _MOUNT_RE.match(line)
        if m and m.group(4) in ("smbfs", "afpfs", "nfs"):
            found[m.group(3)] = (m.group(1).lower(), urllib.parse.unquote(m.group(2)))
    return found


class Vidchecker:
    def __init__(self, get_cfg, http=None, mount_table=None, clock=time.monotonic):
        self.get_cfg = get_cfg
        self.http = http or http_post
        self.mount_table = mount_table or mounts
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}
        self._roots_busy = False

    # ---- config
    def cfg(self) -> dict:
        c = self.get_cfg().get("vidchecker") or {}
        return {"url": (c.get("url") or "").rstrip("/") or None, "templates": list(c.get("templates") or [])}

    # ---- SOAP
    def _call(self, service: str, contract: str, action: str, inner: str, timeout: float = 30) -> ET.Element:
        url = self.cfg()["url"]
        if not url:
            raise VidcheckerError("Set the Vidchecker server's address first.")
        body = (f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                f'envelope/" xmlns:vc="{NS}" xmlns:arr="http://schemas.microsoft.com/2003/10/Serialization/Arrays">'
                f"<soap:Body>{inner}</soap:Body></soap:Envelope>").encode("utf-8")
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{NS}/{contract}/{action}"'}
        try:
            status, raw = self.http(f"{url}/{service}", headers, body, timeout)
        except OSError as e:
            raise VidcheckerError(f"Couldn't reach Vidchecker at {url}: {getattr(e, 'reason', None) or e}.") from e
        try:
            root = _strip(ET.fromstring(raw))
        except ET.ParseError as e:
            raise VidcheckerError(f"Vidchecker answered with something unexpected (error {status}).") from e
        fault = root.find(".//Fault")
        if fault is not None:
            raise VidcheckerError(f"Vidchecker said: {(_text(fault, 'faultstring') or 'an error').strip()}")
        resp = root.find(f".//{action}Response")
        if resp is None:
            raise VidcheckerError(f"Vidchecker answered without a result (error {status}).")
        return resp

    def _private(self, action: str, inner: str, contract: str = "IPrivateService", timeout: float = 30) -> ET.Element:
        return self._call(f"{API_VERSION}/PrivateService.svc", contract, action, inner, timeout)

    def _cached(self, key: str, fn, fresh: bool = False, ttl: float = CACHE_TTL, stale_ok: bool = False):
        hit = self._cache.get(key)
        if hit and not fresh and self.clock() - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
        except VidcheckerError:
            if stale_ok and hit:
                return hit[1]
            raise
        self._cache[key] = (self.clock(), val)
        return val

    # ---- what the server has
    def templates(self, fresh: bool = False) -> list[dict]:
        def get():
            r = self._private("ListTemplates", "<vc:ListTemplates/>", contract="IPublicService")
            out = [{"id": int(_text(t, "Id")), "name": (_text(t, "Name") or "").strip()}
                   for t in r.iter("TemplateRef") if (_text(t, "Id") or "").isdigit()]
            return sorted(out, key=lambda t: t["name"].lstrip("*- ").lower())
        return self._cached("templates", get, fresh)

    def roots(self, fresh: bool = False) -> list[dict]:
        """The server's drives: [{"drive": "W:\\\\", "host": "10.0.0.152", "share": "WashDeliverables"}]."""
        def get():
            r = self._call("FileService.svc", "IFileService", "GetRoots", "<vc:GetRoots/>", timeout=SLOW_TIMEOUT)
            out = []
            for f in r.iter("File"):
                info = (_text(f, "Info") or "").strip()
                m = re.match(r"^\\\\([^\\]+)\\(.+?)\\?$", info)
                out.append({"drive": _text(f, "Filename") or "", "host": m.group(1).lower() if m else None,
                            "share": m.group(2) if m else None})
            return out
        return self._cached("roots", get, fresh, ttl=ROOTS_TTL, stale_ok=True)

    def warm_roots(self) -> None:
        """Fetch the drive list in the background, so the QC page never waits for it."""
        hit = self._cache.get("roots")
        if self._roots_busy or not self.cfg()["url"] or (hit and self.clock() - hit[0] < ROOTS_TTL):
            return
        self._roots_busy = True

        def run():
            try:
                self.roots()
            except VidcheckerError:
                pass
            finally:
                self._roots_busy = False
        threading.Thread(target=run, daemon=True).start()

    def server_path(self, mac_path: str | Path) -> str:
        """The path the Vidchecker server opens this file by. Raises VidcheckerError when it can't see the drive."""
        p = Path(mac_path)
        table = self._cached("mounts", self.mount_table)
        mount = max((m for m in table if p == Path(m) or Path(m) in p.parents), key=len, default=None)
        if not mount:
            raise VidcheckerError(f"{p} isn't on a shared drive, so Vidchecker can't open it.")
        host, share = table[mount]
        roots = [r for r in self.roots() if r["share"] and r["share"].lower() == share.lower()]
        if not roots:
            raise VidcheckerError(f"The Vidchecker server doesn't have {Path(mount).name} ({share}) as a drive. "
                                  "Map it on the server, then try again.")
        same_host = [r for r in roots if r["host"] == host]  # the same share can have two letters: always pick one
        root = sorted(same_host or roots, key=lambda r: r["drive"])[-1]
        rel = p.relative_to(mount).parts
        return str(PureWindowsPath(root["drive"], *rel))

    def server_sees(self, server_path: str) -> bool:
        parent = str(PureWindowsPath(server_path).parent)
        try:
            r = self._call("FileService.svc", "IFileService", "ListFiles",
                           f"<vc:ListFiles><vc:Dir><vc:Filename>{escape(parent)}</vc:Filename></vc:Dir></vc:ListFiles>",
                           timeout=SLOW_TIMEOUT)
        except VidcheckerError:
            return False
        names = {(_text(f, "Filename") or "").lower() for f in r.iter("File")}
        return server_path.lower() in names

    # ---- checks
    def start(self, mac_path: str | Path, template_id: int) -> dict:
        """Start a check of one file with one of the assigned templates."""
        c = self.cfg()
        if template_id not in c["templates"]:
            raise VidcheckerError("Choose one of the checks set up for this app.")
        name = next((t["name"] for t in self.templates() if t["id"] == template_id), None)
        if name is None:
            raise VidcheckerError("That check isn't on the Vidchecker server any more.")
        p = Path(mac_path)
        if not p.is_file():
            raise VidcheckerError(f"{p.name} isn't there any more.")
        spath = self.server_path(p)
        if not self.server_sees(spath):
            raise VidcheckerError(f"The Vidchecker server can't see {spath}. Check the drive is mapped on the server.")
        with self._lock:
            self._private("NewTaskInternal", f"<vc:NewTaskInternal><vc:Filename>{escape(spath)}</vc:Filename>"
                                             f"<vc:TemplateId>{int(template_id)}</vc:TemplateId>"
                                             "<vc:type>Manual</vc:type></vc:NewTaskInternal>")
        return {"file": str(p), "server_path": spath, "template": name}

    def tasks(self, limit: int = 40) -> list[dict]:
        """The most recent checks, newest first, with the Mac path where it can be worked out."""
        r = self._private("GetPaged2", (
            f"<vc:GetPaged2><vc:PageSize>{int(limit)}</vc:PageSize><vc:PageNumber>0</vc:PageNumber>"
            "<vc:OrderBy><arr:int>1</arr:int></vc:OrderBy><vc:Direction><vc:OrderDirection>Descending"
            "</vc:OrderDirection></vc:Direction><vc:TaskFilter><vc:Status/><vc:Results>"
            + "".join(f"<vc:TaskCheckResult>{x}</vc:TaskCheckResult>" for x in ("Passed", "Failed", "Warning", "Reject"))
            + "</vc:Results></vc:TaskFilter></vc:GetPaged2>"))
        back = self._mac_paths()
        out = []
        for t in r.iter("Task"):
            spath = _text(t, "Filename") or ""
            out.append({"id": int(_text(t, "Id") or 0), "server_path": spath, "file": back(spath),
                        "name": PureWindowsPath(spath).name, "template": (_text(t, "Template") or "").strip(),
                        "status": _text(t, "Status"), "result": _text(t, "CheckResult"),
                        "percent": int(_text(t, "PercentComplete") or 0), "alerts": int(_text(t, "AlertCount") or 0),
                        "started": _text(t, "StartedDate"), "completed": _text(t, "CompletedDate"),
                        "size": int(_text(t, "FileSize") or 0)})
        out.sort(key=lambda x: x["id"], reverse=True)
        return out

    def _mac_paths(self):
        hit = self._cache.get("roots")
        if not hit:  # not fetched yet: show the list now, the Mac paths appear once the drives are known
            self.warm_roots()
            return lambda s: None
        self.warm_roots()
        table = self._cached("mounts", self.mount_table)
        roots = hit[1]
        by_drive = {}
        for mount, (host, share) in table.items():
            for r in roots:
                if r["share"] and r["share"].lower() == share.lower():
                    by_drive.setdefault(r["drive"].upper(), mount)

        def back(spath: str):
            w = PureWindowsPath(spath)
            m = by_drive.get(w.anchor.upper())
            return str(Path(m, *w.parts[1:])) if m and len(w.parts) > 1 else None
        return back

    def status(self) -> dict:
        """For the dashboard: is it reachable, which checks it offers."""
        c = self.cfg()
        if not c["url"]:
            return {"configured": False, "ok": False, "problem": None, "templates": [], "assigned": []}
        try:
            tpls = self.templates()
        except VidcheckerError as e:
            return {"configured": True, "ok": False, "problem": str(e), "templates": [], "assigned": []}
        names = {t["id"]: t["name"] for t in tpls}
        return {"configured": True, "ok": True, "problem": None, "templates": tpls,
                "assigned": [{"id": i, "name": names[i]} for i in c["templates"] if i in names]}


def validate_config(inc) -> tuple[dict, dict]:
    errs: dict = {}
    if not isinstance(inc, dict):
        return {}, {"vidchecker": "Invalid Vidchecker settings."}
    clean: dict = {}
    if "url" in inc:
        u = inc["url"]
        if u is None or (isinstance(u, str) and not u.strip()):
            clean["url"] = None
        elif not isinstance(u, str) or not _URL_RE.match(u.strip()):
            errs["vidchecker.url"] = "Use the server's address, for example http://10.0.0.66:51060"
        else:
            clean["url"] = u.strip().rstrip("/")
    if "templates" in inc:
        t = inc["templates"]
        if not isinstance(t, list) or len(t) > 50 or not all(isinstance(x, int) and not isinstance(x, bool) for x in t):
            errs["vidchecker.templates"] = "Choose the checks to offer."
        else:
            clean["templates"] = list(dict.fromkeys(t))
    for k in inc:
        if k not in ("url", "templates"):
            errs[f"vidchecker.{k}"] = "Unknown setting."
    return clean, errs


def media_files(root: Path, limit: int = 400) -> list[dict]:
    """Exported media under a job's folder, newest first."""
    out = []
    try:
        for p in root.rglob("*"):
            if p.suffix.lower() in MEDIA and not p.name.startswith(".") and p.is_file():
                st = p.stat()
                out.append({"path": str(p), "rel": str(p.relative_to(root)), "size": st.st_size, "mtime": st.st_mtime})
                if len(out) >= limit * 3:
                    break
    except OSError:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]
