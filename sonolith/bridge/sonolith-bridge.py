#!/usr/bin/env python3
"""Bridge between the Noctalia Sonolith plugin and Sonolith's local API.

    sonolith-bridge.py daemon --data-dir DIR   long-running, started by the plugin service
    sonolith-bridge.py call METHOD [JSON]      one request through the running daemon

The daemon owns the only connection to Sonolith (JSON-RPC over a Unix socket) and writes one JSON object per line
to stdout: status changes, the session, Sonolith events 1:1, heartbeats and logs. `call` talks to the daemon over a
private control socket, so it never authenticates on its own, and prints one line: {"ok": true, "result": ...} or
{"ok": false, "error": {"name": ..., "reason": ..., ...}}.

Standard library only. The token lives in the Secret Service (secret-tool) or in a 0600 file in the data dir, and
is never printed.
"""

import json
import os
import socket
import sys

APP_ID = "com.github.opecko.sonolith-noctalia"
API_VERSION = "1.0"
MAX_MESSAGE = 1024 * 1024
CALL_TIMEOUT = 30
HEARTBEAT_S = 5

SCOPES = [
    ("player:read", True, "To show the current song in your bar"),
    ("player:control", False, "To play, pause, skip and seek from your bar"),
    ("queue:read", False, "To show what's up next"),
    ("queue:write", False, "To add, move and remove songs in your queue"),
    ("lyrics:read", False, "To show the current lyric line"),
    ("library:read", False, "To browse your library"),
    ("search", False, "To search YouTube Music"),
    ("library:write", False, "To like songs and save them to playlists"),
]
REASONS = {scope: reason for scope, _, reason in SCOPES}

# Topics we subscribe to, by the scope each one needs (None = no scope).
TOPICS = [
    ("player.state", "player:read"),
    ("player.track", "player:read"),
    ("player.position", "player:read"),
    ("player.volume", "player:read"),
    ("queue", "queue:read"),
    ("lyrics", "lyrics:read"),
    ("lyrics.line", "lyrics:read"),
    ("account", None),
]


def ctl_dir():
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "sonolith-noctalia")
    return f"/tmp/sonolith-noctalia-{os.getuid()}"


def ctl_path():
    return os.path.join(ctl_dir(), "ctl.sock")


# ---------------------------------------------------------------- call mode


def call_main(method, raw_params):
    def fail(name, reason):
        print(json.dumps({"ok": False, "error": {"name": name, "reason": reason}}), flush=True)

    try:
        params = json.loads(raw_params) if raw_params else {}
    except ValueError as e:
        return fail("InvalidParams", str(e))
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(CALL_TIMEOUT + 5)
        s.connect(ctl_path())
    except OSError as e:
        return fail("BridgeDown", str(e))
    try:
        s.sendall(json.dumps({"method": method, "params": params}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        return fail("BridgeDown", str(e))
    finally:
        s.close()
    line = buf.decode("utf-8", "replace").strip()
    if not line:
        return fail("BridgeDown", "no answer")
    print(line, flush=True)


# ---------------------------------------------------------------- daemon


def daemon_main(data_dir):
    import asyncio
    import fcntl
    import random
    import shutil
    import signal
    import subprocess
    import time

    def now_ms():
        return int(time.time() * 1000)

    def emit(obj):
        try:
            sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            os._exit(0)  # the plugin went away

    def log(level, msg):
        emit({"t": "log", "level": level, "msg": msg})

    # -------------------------------------------------------- single instance

    def private_dir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise SystemExit(f"{path} is not private to this user")

    def take_lock():
        """The newest daemon wins: a restarted plugin service must not end up with two bridges."""
        private_dir(ctl_dir())
        fd = os.open(os.path.join(ctl_dir(), "daemon.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                pid = int(os.pread(fd, 32, 0).decode().strip() or 0)
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if b"sonolith-bridge" in f.read():
                        os.kill(pid, signal.SIGTERM)
            except (ValueError, OSError):
                pass
            for _ in range(30):
                time.sleep(0.1)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    continue
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
        return fd

    # -------------------------------------------------------- token storage

    class TokenStore:
        ATTRS = ["service", "sonolith-api", "app", APP_ID]

        def __init__(self, directory):
            self.file = os.path.join(directory, "token")
            self.tool = shutil.which("secret-tool")

        def _run(self, args, data=None):
            try:
                return subprocess.run([self.tool, *args], input=data, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return None

        def load(self):
            if self.tool:
                r = self._run(["lookup", *self.ATTRS])
                if r and r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
            try:
                with open(self.file) as f:
                    return f.read().strip() or None
            except OSError:
                return None

        def save(self, token):
            if self.tool:
                r = self._run(["store", "--label=Sonolith token (Noctalia widget)", *self.ATTRS], token)
                if r and r.returncode == 0:
                    self._unlink()
                    return
                log("warn", "Secret Service unavailable; keeping the token in a user-only file")
            fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)

        def delete(self):
            if self.tool:
                self._run(["clear", *self.ATTRS])
            self._unlink()

        def _unlink(self):
            try:
                os.unlink(self.file)
            except FileNotFoundError:
                pass

    # -------------------------------------------------------- Sonolith endpoint

    class NotAvailable(Exception):
        pass

    class RpcError(Exception):
        def __init__(self, error):
            self.code = error.get("code")
            self.data = error.get("data") or {}
            self.name = self.data.get("name") or error.get("message") or "Error"
            super().__init__(f"{self.name}: {self.data.get('reason') or error.get('message')}")

        def as_dict(self):
            d = dict(self.data)
            d.setdefault("name", self.name)
            d["code"] = self.code
            return d

    def sonolith_dir():
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return os.path.join(xdg, "sonolith")
        d = f"/tmp/sonolith-{os.getuid()}"
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            return d
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise NotAvailable(f"{d} is not private to this user")
        return d

    def discovery():
        """(socket path, startedAt) or raises NotAvailable."""
        base = sonolith_dir()
        try:
            with open(os.path.join(base, "discovery.json")) as f:
                info = json.load(f)
            return info["path"], info.get("startedAt")
        except (OSError, KeyError, ValueError):
            path = os.path.join(base, "api.sock")
            if os.path.exists(path):
                return path, None
            raise NotAvailable("no endpoint")

    def endpoint_marker():
        try:
            return discovery()
        except NotAvailable:
            return None

    class Connection:
        def __init__(self, reader, writer):
            self.reader, self.writer = reader, writer
            self.next_id = 1
            self.pending = {}
            self.events = asyncio.Queue()
            self.closed = False
            self.task = asyncio.ensure_future(self._read())

        @classmethod
        async def open(cls):
            path, _ = discovery()
            try:
                return cls(*await asyncio.open_unix_connection(path, limit=2 * MAX_MESSAGE))
            except OSError as e:
                raise NotAvailable(str(e)) from e

        async def call(self, method, params=None, timeout=CALL_TIMEOUT):
            if self.closed:
                raise NotAvailable("connection closed")
            msg_id, self.next_id = self.next_id, self.next_id + 1
            msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params:
                msg["params"] = params
            line = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
            if len(line) > MAX_MESSAGE:
                raise RpcError({"code": -32600, "data": {"name": "InvalidRequest", "reason": "messageTooLarge"}})
            fut = asyncio.get_running_loop().create_future()
            self.pending[msg_id] = fut
            try:
                self.writer.write(line)
                await self.writer.drain()
                return await asyncio.wait_for(fut, timeout)
            except (ConnectionError, OSError) as e:
                raise NotAvailable(str(e)) from e
            except asyncio.TimeoutError:
                raise RpcError({"code": -32011, "data": {"name": "Timeout", "reason": "bridgeTimeout"}})
            finally:
                self.pending.pop(msg_id, None)

        async def _read(self):
            try:
                while True:
                    line = await self.reader.readline()
                    if not line:
                        break
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "id" in msg and msg["id"] in self.pending:
                        fut = self.pending[msg["id"]]
                        if fut.done():
                            continue
                        if "error" in msg:
                            fut.set_exception(RpcError(msg["error"] or {}))
                        else:
                            fut.set_result(msg.get("result"))
                    elif "method" in msg:
                        await self.events.put(msg)
            except (ConnectionError, OSError, ValueError, asyncio.LimitOverrunError):
                pass
            finally:
                self.closed = True
                for fut in self.pending.values():
                    if not fut.done():
                        fut.set_exception(NotAvailable("connection closed"))
                await self.events.put(None)

        def close(self):
            if not self.closed:
                self.closed = True
                try:
                    self.writer.close()
                except Exception:
                    pass
            self.task.cancel()
            self.events.put_nowait(None)

    # -------------------------------------------------------- the app icon

    def icon_data_uri():
        """A 64x64 PNG drawn here (two beamed notes on a rounded square), so the bridge stays one file."""
        import base64
        import struct
        import zlib

        size, ss = 64, 3
        bg, fg = (103, 80, 164), (255, 255, 255)

        def inside_rounded(x, y):
            r = 14
            cx = min(max(x, r), size - r)
            cy = min(max(y, r), size - r)
            return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

        def head(x, y, cx, cy):
            dx, dy = x - cx, y - cy
            u = dx * 0.94 + dy * 0.34
            v = -dx * 0.34 + dy * 0.94
            return (u / 7.5) ** 2 + (v / 5.5) ** 2 <= 1

        def note(x, y):
            if head(x, y, 21, 45) or head(x, y, 41, 41):
                return True
            if 26 <= x <= 29.5 and 18 <= y <= 45:
                return True
            if 46 <= x <= 49.5 and 14 <= y <= 41:
                return True
            top = 18 - (x - 26) * (4 / 23.5)
            return 26 <= x <= 49.5 and top <= y <= top + 6

        rows = []
        for y in range(size):
            row = bytearray([0])
            for x in range(size):
                cover = paint = 0
                for sy in range(ss):
                    for sx in range(ss):
                        px, py = x + (sx + 0.5) / ss, y + (sy + 0.5) / ss
                        if inside_rounded(px, py):
                            cover += 1
                            paint += note(px, py)
                n = ss * ss
                if cover == 0:
                    row += bytes((0, 0, 0, 0))
                    continue
                t = paint / cover
                rgb = [round(bg[i] + (fg[i] - bg[i]) * t) for i in range(3)]
                row += bytes((*rgb, round(255 * cover / n)))
            rows.append(bytes(row))

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
        return "data:image/png;base64," + base64.b64encode(png).decode()

    def registration():
        return {
            "appId": APP_ID,
            "name": "Noctalia Sonolith widget",
            "description": "Shows what's playing in your Noctalia bar and controls Sonolith.",
            "publisher": "opecko",
            "homepage": "https://github.com/opecko/sonobar",
            "icon": icon_data_uri(),
            "scopes": [{"scope": s, "required": req, "reason": reason} for s, req, reason in SCOPES],
            "apiVersion": API_VERSION,
        }

    # -------------------------------------------------------- the bridge

    class Bridge:
        def __init__(self):
            self.store = TokenStore(data_dir)
            self.wake = asyncio.Event()
            self.status_key = None
            self.sticky = None  # (state, info) shown until the user acts
            self.session = None  # the authenticated Connection
            self.scopes = []
            self.reg_task = None
            self.reg_conn = None

        def set_status(self, state, **info):
            key = (state, json.dumps(info, sort_keys=True))
            if key == self.status_key:
                return
            self.status_key = key
            emit({"t": "status", "state": state, "info": info})

        def publish_idle(self):
            """No token: what the panel should offer."""
            if self.reg_task:
                return
            if self.sticky and self.sticky[0] in ("blocked", "denied", "expired"):
                self.set_status(self.sticky[0], **self.sticky[1])
            elif endpoint_marker() is None:
                self.set_status("unavailable")
            elif self.sticky:
                self.set_status(self.sticky[0], **self.sticky[1])
            else:
                self.set_status("unregistered")

        def poke(self):
            self.wake.set()

        async def wait(self, seconds=None):
            """Sleeps until poked, the endpoint appears or changes, or `seconds` pass."""
            marker = endpoint_marker()
            end = None if seconds is None else time.monotonic() + seconds
            while not self.wake.is_set():
                if end is not None and time.monotonic() >= end:
                    return
                if endpoint_marker() != marker:
                    return
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass

        async def run(self):
            delay = 1.0
            while True:
                self.wake.clear()
                token = await asyncio.to_thread(self.store.load)
                if not token:
                    self.publish_idle()
                    await self.wait()
                    continue
                if self.reg_task:  # a token from an earlier approval while a new request waits
                    await self.wait()
                    continue
                try:
                    conn = await Connection.open()
                except NotAvailable:
                    self.set_status("unavailable")
                    await self.wait(delay * random.uniform(0.8, 1.2))
                    delay = min(delay * 2, 60)
                    continue
                try:
                    outcome = await self.run_session(conn, token)
                except NotAvailable:
                    outcome = "retry"
                except RpcError as e:
                    log("warn", f"session failed: {e}")
                    outcome = "retry"
                finally:
                    self.session = None
                    conn.close()
                if outcome == "ok":
                    delay = 1.0
                    outcome = "retry"
                if outcome == "stop":
                    await self.wait_for_user()
                elif outcome == "retry":
                    if not self.wake.is_set():
                        self.set_status("unavailable")
                    await self.wait(delay * random.uniform(0.8, 1.2))
                    delay = min(delay * 2, 60)

        async def wait_for_user(self):
            while not self.wake.is_set():
                await self.wake.wait()

        def lose_token(self, state, **info):
            self.store.delete()
            self.sticky = (state, info)
            self.set_status(state, **info)

        async def hello(self, conn, token):
            while True:
                try:
                    return await conn.call("session.hello", {"appId": APP_ID, "token": token, "apiVersion": API_VERSION})
                except RpcError as e:
                    if e.name in ("Unauthorized", "NotRegistered"):
                        self.lose_token("disconnected")
                        return "idle"
                    if e.name == "Blocked":
                        self.lose_token("blocked")
                        return "idle"
                    if e.name == "UnsupportedVersion":
                        self.set_status("error", message="unsupportedVersion", supported=e.data.get("supported"))
                        return "stop"
                    if e.name == "RateLimited":
                        await asyncio.sleep(max(e.data.get("retryAfterMs") or 1000, 100) / 1000)
                        continue
                    if e.name != "RegistrationPending":
                        raise
                    request_id = e.data.get("requestId")
                    self.set_status("pending", kind="identity", expiresAt=e.data.get("expiresAt"))
                    outcome = await self.wait_resolved(conn, request_id)
                    if outcome is None:
                        return "retry"
                    if outcome["status"] != "approved":
                        state = "denied" if outcome["status"] == "denied" else outcome["status"]
                        self.set_status(state, kind="identity", **self.retry_info(outcome))
                        return "stop"

        @staticmethod
        def retry_info(outcome):
            ms = outcome.get("retryAfterMs")
            return {"retryAfterMs": ms, "retryAt": now_ms() + ms} if ms else {}

        async def wait_resolved(self, conn, request_id):
            while True:
                msg = await conn.events.get()
                if msg is None:
                    return None
                if msg.get("method") == "registration.resolved" and msg["params"].get("requestId") == request_id:
                    return msg["params"]

        async def subscribe(self, conn):
            topics = [t for t, scope in TOPICS if scope is None or scope in self.scopes]
            if topics:
                await conn.call("events.subscribe", {"topics": topics, "positionHz": 1})

        async def run_session(self, conn, token):
            result = await self.hello(conn, token)
            if isinstance(result, str):
                return result
            self.sticky = None
            self.scopes = list(result.get("grantedScopes") or [])
            self.session = conn
            emit({
                "t": "hello",
                "grantedScopes": self.scopes,
                "signedIn": result.get("signedIn"),
                "appVersion": result.get("appVersion"),
                "apiVersion": result.get("apiVersion"),
            })
            self.set_status("connected")
            await self.subscribe(conn)
            while True:
                msg = await conn.events.get()
                if msg is None:
                    return "ok"
                method, params = msg.get("method"), msg.get("params") or {}
                if method == "session.revoked":
                    self.session = None
                    self.lose_token("disconnected", reason=params.get("reason"))
                    return "idle"
                if method == "session.scopesChanged":
                    self.scopes = list(params.get("grantedScopes") or [])
                    emit({"t": "scopes", "grantedScopes": self.scopes})
                    try:
                        await self.subscribe(conn)  # new topics only; removed scopes end their topics
                    except RpcError as e:
                        log("warn", f"resubscribe failed: {e}")
                    continue
                emit({"t": "event", "method": method, "params": params})

        # ---------------------------------------------------- control commands

        async def register(self):
            if self.session:
                return {"error": {"name": "AlreadyConnected", "reason": "alreadyConnected"}}
            if self.reg_task:
                return {"result": {"status": "pending"}}
            try:
                conn = await Connection.open()
            except NotAvailable as e:
                self.publish_idle()
                return {"error": {"name": "Unavailable", "reason": "sonolithNotRunning", "detail": str(e)}}
            try:
                res = await conn.call("app.register", registration())
            except (RpcError, NotAvailable) as e:
                conn.close()
                if isinstance(e, NotAvailable):
                    return {"error": {"name": "Unavailable", "reason": "sonolithNotRunning"}}
                if e.name == "Blocked":
                    self.sticky = ("blocked", {})
                elif e.name == "RegistrationDenied":
                    self.sticky = ("denied", self.retry_info(e.data))
                self.publish_idle()
                return {"error": e.as_dict()}
            self.sticky = None
            self.reg_conn = conn
            self.set_status("pending", kind="register", expiresAt=res.get("expiresAt"))
            self.reg_task = asyncio.ensure_future(self.await_registration(conn, res.get("requestId")))
            return {"result": {"status": "pending", "expiresAt": res.get("expiresAt")}}

        async def await_registration(self, conn, request_id):
            try:
                outcome = await self.wait_resolved(conn, request_id)
                if outcome is None:
                    log("warn", "the registration connection closed; the request was cancelled")
                elif outcome["status"] == "approved" and outcome.get("token"):
                    await asyncio.to_thread(self.store.save, outcome["token"])
                elif outcome["status"] in ("denied", "blocked", "expired"):
                    self.sticky = (outcome["status"], self.retry_info(outcome))
            finally:
                conn.close()
                self.reg_conn = None
                self.reg_task = None
                self.status_key = None
                self.poke()

        async def cancel_register(self):
            if self.reg_task:
                self.reg_task.cancel()
                if self.reg_conn:
                    self.reg_conn.close()
            self.sticky = None
            self.poke()
            return {"result": {}}

        async def disconnect(self):
            if self.reg_task:
                await self.cancel_register()
            conn = self.session
            try:
                if conn:
                    await conn.call("app.unregister", timeout=5)
                else:
                    token = await asyncio.to_thread(self.store.load)
                    if token and endpoint_marker():
                        tmp = await Connection.open()
                        try:
                            r = await tmp.call("session.hello", {"appId": APP_ID, "token": token, "apiVersion": API_VERSION}, timeout=5)
                            if r:
                                await tmp.call("app.unregister", timeout=5)
                        finally:
                            tmp.close()
            except (RpcError, NotAvailable) as e:
                log("info", f"unregister: {e}")  # the token goes anyway
            await asyncio.to_thread(self.store.delete)
            if conn:
                conn.close()
            self.session = None
            self.sticky = None
            self.status_key = None
            self.poke()
            return {"result": {}}

        async def request_scopes(self, params):
            if not self.session:
                return {"error": {"name": "NotConnected", "reason": "notConnected"}}
            wanted = [s for s in (params.get("scopes") or []) if s in REASONS and s not in self.scopes]
            if not wanted:
                return {"result": {"status": "granted", "grantedScopes": self.scopes}}
            scopes = [{"scope": s, "required": False, "reason": REASONS[s]} for s in wanted]
            try:
                return {"result": await self.session.call("app.requestScopes", {"scopes": scopes})}
            except RpcError as e:
                return {"error": e.as_dict()}

        async def forward(self, method, params):
            conn = self.session
            if not conn:
                return {"error": {"name": "NotConnected", "reason": "notConnected"}}
            try:
                return {"result": await conn.call(method, params or None)}
            except RpcError as e:
                return {"error": e.as_dict()}
            except NotAvailable:
                return {"error": {"name": "NotConnected", "reason": "connectionLost"}}

        async def handle(self, req):
            method = req.get("method")
            params = req.get("params")
            if not isinstance(params, dict):
                params = {}  # Luau encodes an empty table as []
            if method == "bridge.register":
                return await self.register()
            if method == "bridge.cancelRegister":
                return await self.cancel_register()
            if method == "bridge.disconnect":
                return await self.disconnect()
            if method == "bridge.requestScopes":
                return await self.request_scopes(params)
            if method == "bridge.retry":
                self.sticky = None if not self.session else self.sticky
                self.status_key = None
                self.poke()
                return {"result": {}}
            if not isinstance(method, str) or method.startswith("bridge.") or method in ("session.hello", "app.register", "app.unregister", "app.requestScopes"):
                return {"error": {"name": "MethodNotFound", "reason": "notAllowed"}}
            return await self.forward(method, params)

        async def serve_client(self, reader, writer):
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
                req = json.loads(line)
                out = await self.handle(req if isinstance(req, dict) else {})
            except (ValueError, asyncio.TimeoutError, OSError) as e:
                out = {"error": {"name": "InvalidRequest", "reason": str(e)}}
            except Exception as e:  # never let one bad request take the daemon down
                out = {"error": {"name": "InternalError", "reason": repr(e)}}
            reply = {"ok": "error" not in out}
            reply.update(out)
            try:
                writer.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
                await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()

    async def heartbeat():
        parent = os.getppid()
        while True:
            emit({"t": "hb"})
            if os.getppid() != parent:
                os._exit(0)
            await asyncio.sleep(HEARTBEAT_S)

    async def main():
        bridge = Bridge()
        path = ctl_path()
        try:
            os.unlink(path)  # ours: we hold the lock
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(bridge.serve_client, path, limit=2 * MAX_MESSAGE)
        os.chmod(path, 0o600)
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, stop.set)
        tasks = [asyncio.ensure_future(bridge.run()), asyncio.ensure_future(heartbeat())]
        stopper = asyncio.ensure_future(stop.wait())
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not stopper and t.exception():
                log("error", f"bridge crashed: {t.exception()!r}")
        server.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    take_lock()
    asyncio.run(main())


def main(argv):
    if len(argv) >= 2 and argv[1] == "call":
        if len(argv) < 3:
            sys.exit("usage: sonolith-bridge.py call METHOD [JSON]")
        call_main(argv[2], argv[3] if len(argv) > 3 else "")
    elif len(argv) >= 2 and argv[1] == "daemon":
        data_dir = argv[argv.index("--data-dir") + 1] if "--data-dir" in argv else os.path.expanduser("~/.local/state/sonolith-noctalia")
        daemon_main(data_dir)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
