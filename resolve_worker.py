"""Calls into DaVinci Resolve made from a separate process, so a slow or stuck Resolve can't freeze this app.

Resolve's scripting library keeps Python's lock while it waits for Resolve to answer, so when the calls are made in
this process every one of them freezes the whole app (dashboard, menu bar, timers) until Resolve replies; a Resolve
that has grown huge took 20 minutes per export and the dashboard stopped answering with it. Here the app only waits
on a socket, with a time limit. A call that runs past it gets the worker process killed and replaced and raises
ResolveNotAnswering; Resolve itself is left alone (the caller decides whether to stop it).

Resolve objects come back as stand-ins (Remote) whose methods are calls made in the worker, so code written for the
in-process objects works unchanged: ``worker.connect(cfg).GetProjectManager().GetCurrentDatabase()``.
"""
from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path

log = logging.getLogger("resolve-cloud-backup")

WORKER_FLAG = "--rcb-resolve-worker"
CALL_LIMIT = 120  # seconds for an ordinary call (folder listing, attributes...); exports set their own


class ResolveNotAnswering(Exception):
    """A call into Resolve ran past its time limit, or the worker making it died. The worker has been replaced."""


class RemoteError(Exception):
    """A call into Resolve raised in the worker."""


class _Ref:
    """A Resolve object held by the worker, as it travels between the processes."""
    __slots__ = ("id",)

    def __init__(self, id_: int):
        self.id = id_

    def __getstate__(self):
        return self.id

    def __setstate__(self, state):
        self.id = state


class Remote:
    """Stand-in for a Resolve object living in the worker. ``remote.Method(args)`` runs ``obj.Method(args)`` there."""
    __slots__ = ("_worker", "_id", "_gen")

    def __init__(self, worker: "Worker", id_: int, gen: int):
        self._worker, self._id, self._gen = worker, id_, gen

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **kw: self._worker.call(self, name, a, kw)

    def __repr__(self):
        return f"<Resolve object {self._id} in worker {self._gen}>"


def worker_command() -> list[str]:
    """How to start a worker: this same program (the packaged app's executable, or this script)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [sys.executable, str(Path(__file__).resolve()), WORKER_FLAG]


class Worker:
    """The app's side: starts the worker when first needed, sends it calls and enforces the time limits."""

    def __init__(self, command: list[str] | None = None, call_limit: float = CALL_LIMIT,
                 popen=subprocess.Popen, clock=time.monotonic):
        self.command = command or worker_command()
        self.call_limit = call_limit
        self.popen, self.clock = popen, clock
        self.proc = None
        self.conn: Connection | None = None
        self.gen = 0          # bumped each time a worker starts; Remotes from an older one are dead
        self._lock = threading.RLock()
        self._local = threading.local()  # the time limit set by limit(), per thread
        self.busy_since: float | None = None  # clock() when the call in progress started
        self.busy_with: str | None = None

    # ---- lifecycle
    def _start(self) -> None:
        mine, theirs = socket.socketpair()
        try:
            self.proc = self.popen(self.command + [str(theirs.fileno())], pass_fds=(theirs.fileno(),),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        except OSError:
            mine.close()
            raise
        finally:
            theirs.close()
        self.conn = Connection(mine.detach())
        self.gen += 1

    def _kill(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(5)
            except (OSError, subprocess.SubprocessError):
                pass
            self.proc = None

    def close(self) -> None:
        with self._lock:
            self._kill()

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    # ---- time limits
    @contextlib.contextmanager
    def limit(self, seconds: float, what: str):
        """Calls made inside this block (from this thread) get ``seconds`` each instead of call_limit."""
        old = getattr(self._local, "limit", None)
        self._local.limit = (seconds, what)
        try:
            yield
        finally:
            self._local.limit = old

    def busy_for(self) -> float:
        """Seconds the call in progress has been running; 0 when idle."""
        since = self.busy_since
        return 0.0 if since is None else max(0.0, self.clock() - since)

    # ---- calls
    def _request(self, msg: tuple, seconds: float, what: str):
        with self._lock:
            if self.conn is None:
                self._start()
            gen = self.gen
            self.busy_since, self.busy_with = self.clock(), what
            try:
                try:
                    self.conn.send(msg)
                    answered = self.conn.poll(seconds)
                    reply = self.conn.recv() if answered else None
                except (EOFError, OSError) as e:
                    self._kill()
                    raise ResolveNotAnswering(f"the connection to Resolve was lost while {what} ({e})") from None
                if not answered:
                    self._kill()
                    raise ResolveNotAnswering(f"DaVinci Resolve didn't answer within {seconds:.0f} s while {what}")
            finally:
                self.busy_since = self.busy_with = None
            kind, value = reply
            if kind == "err":
                raise RemoteError(value)
            if kind == "noattr":
                raise AttributeError(value)
            return self._decode(value, gen)

    def _decode(self, v, gen: int):
        if isinstance(v, _Ref):
            return Remote(self, v.id, gen)
        if isinstance(v, list):
            return [self._decode(x, gen) for x in v]
        if isinstance(v, dict):
            return {k: self._decode(x, gen) for k, x in v.items()}
        return v

    def _encode(self, v):
        if isinstance(v, Remote):
            if v._gen != self.gen:
                raise ResolveNotAnswering("the connection to Resolve was restarted since this object was fetched")
            return _Ref(v._id)
        if isinstance(v, (list, tuple)):
            return [self._encode(x) for x in v]
        if isinstance(v, dict):
            return {k: self._encode(x) for k, x in v.items()}
        return v

    def connect(self, api: str, lib: str):
        """The Resolve scripting object (a Remote), or None if Resolve isn't reachable."""
        try:
            return self._request(("connect", api, lib), self.call_limit, "connecting")
        except RemoteError as e:
            log.error("Resolve scripting call failed: %s", e)
            return None
        except ResolveNotAnswering as e:
            log.error("%s", e)
            return None

    def call(self, obj: Remote, name: str, args: tuple, kwargs: dict):
        with self._lock:
            if obj._gen != self.gen or self.conn is None:
                raise ResolveNotAnswering("the connection to Resolve was restarted since this object was fetched")
            seconds, what = getattr(self._local, "limit", None) or (self.call_limit, f"calling {name}")
            return self._request(("call", obj._id, name, self._encode(args), self._encode(kwargs)), seconds, what)


# ------------------------------------------------------------------ the worker process
def _serve(conn: Connection) -> int:
    objs: dict[int, object] = {}
    ids: dict[int, int] = {}  # id(obj) -> our id, so the same object keeps one id

    def enc(v):
        if v is None or isinstance(v, (bool, int, float, str, bytes)):
            return v
        if isinstance(v, (list, tuple)):
            return [enc(x) for x in v]
        if isinstance(v, dict):
            return {k: enc(x) for k, x in v.items()}
        n = ids.get(id(v))
        if n is None or objs.get(n) is not v:
            n = len(objs) + 1
            objs[n], ids[id(v)] = v, n
        return _Ref(n)

    def dec(v):
        if isinstance(v, _Ref):
            return objs[v.id]
        if isinstance(v, list):
            return [dec(x) for x in v]
        if isinstance(v, dict):
            return {k: dec(x) for k, x in v.items()}
        return v

    dvr = None
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return 0  # the app closed the connection or went away
        try:
            if msg[0] == "connect":
                _, api, lib = msg
                if dvr is None:
                    os.environ["RESOLVE_SCRIPT_API"], os.environ["RESOLVE_SCRIPT_LIB"] = api, lib
                    modules = os.path.join(api, "Modules")
                    if modules not in sys.path:
                        sys.path.append(modules)
                    import DaVinciResolveScript as dvr  # type: ignore  # noqa: N813
                reply = ("ok", enc(dvr.scriptapp("Resolve")))
            else:
                _, n, name, args, kwargs = msg
                fn = getattr(objs[n], name, None)
                if fn is None:
                    reply = ("noattr", name)
                else:
                    reply = ("ok", enc(fn(*dec(args), **dec(kwargs))))
        except Exception as e:  # noqa: BLE001 - every failure goes back to the app
            reply = ("err", f"{type(e).__name__}: {e}")
        try:
            conn.send(reply)
        except (EOFError, OSError):
            return 0


def main(argv: list[str]) -> int:
    """``<program> --rcb-resolve-worker <fd>``: serve calls on the socket the app passed in."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    return _serve(Connection(int(argv[0])))


if __name__ == "__main__":
    if sys.argv[1:2] == [WORKER_FLAG]:
        # through the module by name, so what is sent is resolve_worker._Ref, which the app can read, and not
        # __main__._Ref
        import resolve_worker
        sys.exit(resolve_worker.main(sys.argv[2:]))
