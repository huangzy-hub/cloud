#!/usr/bin/env python3
"""Small single-Key authentication service for RK Cloud.

The service intentionally does not proxy file data. Nginx uses /auth as an
auth_request endpoint and streams large requests directly to FileBrowser.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import html
import http.cookies
import http.server
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


COOKIE_NAME = "__Host-cloud_session"
DEFAULT_STATE_DIR = "/etc/cloud-auth"
DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_PORT = 18081
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_FORM_BYTES = 4096
KEY_RE = re.compile(r"^rk1\.([0-9a-f]{16})\.([A-Za-z0-9_-]{32})$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PUBLIC_HOST = os.environ.get("CLOUD_AUTH_PUBLIC_HOST", "").strip().lower()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def utc_now() -> int:
    return int(time.time())


def iso_time(value: int | None) -> str:
    if value is None:
        return "never"
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")


def parse_expiry(value: str | None) -> int | None:
    if value is None or value.lower() == "never":
        return None
    match = re.fullmatch(r"([1-9][0-9]*)([mhdw])", value.lower())
    if not match:
        raise ValueError("expiry must be 'never' or a duration such as 30m, 12h, 7d, 4w")
    amount = int(match.group(1))
    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    return utc_now() + amount * unit


def derive_key(value: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=1 << 15,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )


class State:
    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.keys_path = self.directory / "keys.json"
        self.secret_path = self.directory / "session.key"
        self.lock_path = self.directory / ".lock"

    def initialize(self) -> None:
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        if not self.keys_path.exists():
            self._atomic_write(self.keys_path, {"version": 1, "keys": []})
        if not self.secret_path.exists():
            self._atomic_bytes(self.secret_path, secrets.token_bytes(32))

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with os.fdopen(fd, "r+") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                yield
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            pass

    def load_keys(self) -> dict:
        with self.keys_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("version") != 1 or not isinstance(data.get("keys"), list):
            raise RuntimeError("unsupported or damaged key store")
        return data

    def save_keys(self, data: dict) -> None:
        self._atomic_write(self.keys_path, data)

    def load_secret(self) -> bytes:
        value = self.secret_path.read_bytes()
        if len(value) != 32:
            raise RuntimeError("session secret has an invalid length")
        return value

    def reset_sessions(self) -> None:
        self._atomic_bytes(self.secret_path, secrets.token_bytes(32))

    def _atomic_write(self, path: Path, data: dict) -> None:
        payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._atomic_bytes(path, payload)

    def _atomic_bytes(self, path: Path, payload: bytes) -> None:
        try:
            previous = path.stat()
        except FileNotFoundError:
            previous = None
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.directory)
        try:
            if previous is not None:
                os.fchmod(fd, previous.st_mode & 0o777)
                if os.geteuid() == 0:
                    os.fchown(fd, previous.st_uid, previous.st_gid)
            else:
                os.fchmod(fd, 0o640)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def find_key(store: dict, key_id: str) -> dict | None:
    return next((entry for entry in store["keys"] if entry.get("id") == key_id), None)


def verify_access_key(state: State, supplied: str) -> dict | None:
    match = KEY_RE.fullmatch(supplied.strip())
    if not match:
        return None
    entry = find_key(state.load_keys(), match.group(1))
    if not entry or not entry.get("enabled", False):
        return None
    expires_at = entry.get("expires_at")
    if expires_at is not None and expires_at <= utc_now():
        return None
    try:
        expected = b64u_decode(entry["hash"])
        actual = derive_key(supplied.strip(), b64u_decode(entry["salt"]))
    except (KeyError, ValueError):
        return None
    return entry if hmac.compare_digest(actual, expected) else None


def issue_session(state: State, entry: dict) -> str:
    now = utc_now()
    expiry = now + SESSION_TTL_SECONDS
    if entry.get("expires_at") is not None:
        expiry = min(expiry, int(entry["expires_at"]))
    payload = {
        "kid": entry["id"],
        "ver": int(entry["key_version"]),
        "iat": now,
        "exp": expiry,
        "nonce": b64u(secrets.token_bytes(12)),
    }
    encoded = b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = b64u(hmac.digest(state.load_secret(), encoded.encode("ascii"), "sha256"))
    return f"{encoded}.{signature}"


def verify_session(state: State, token: str) -> dict | None:
    if len(token) > 2048 or token.count(".") != 1:
        return None
    encoded, signature = token.split(".", 1)
    expected = b64u(hmac.digest(state.load_secret(), encoded.encode("ascii"), "sha256"))
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(b64u_decode(encoded))
        key_id = str(payload["kid"])
        key_version = int(payload["ver"])
        expiry = int(payload["exp"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    now = utc_now()
    if expiry <= now or expiry > now + SESSION_TTL_SECONDS + 60:
        return None
    entry = find_key(state.load_keys(), key_id)
    if not entry or not entry.get("enabled", False):
        return None
    if int(entry.get("key_version", 0)) != key_version:
        return None
    if entry.get("expires_at") is not None and int(entry["expires_at"]) <= now:
        return None
    return entry


def session_from_cookie(state: State, raw_cookie: str) -> dict | None:
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(raw_cookie)
        morsel = jar.get(COOKIE_NAME)
        return verify_session(state, morsel.value) if morsel else None
    except (http.cookies.CookieError, ValueError):
        return None


def safe_next(value: str | None) -> str:
    if not value or len(value) > 2048 or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def hostname_from_authority(value: str) -> str:
    try:
        return (urllib.parse.urlsplit("//" + value.strip()).hostname or "").lower()
    except ValueError:
        return ""


def origin_allowed(origin: str, host: str, forwarded_host: str = "") -> bool:
    request_hosts = {
        hostname_from_authority(host),
        hostname_from_authority(forwarded_host),
    }
    request_hosts.discard("")
    if origin.strip().lower() == "null":
        if PUBLIC_HOST:
            return PUBLIC_HOST in request_hosts
        return len(request_hosts) == 1
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    allowed = set(request_hosts)
    if PUBLIC_HOST:
        allowed.add(PUBLIC_HOST)
    return parsed.hostname.lower() in allowed


class FailureLimiter:
    def __init__(self, attempts: int = 5, window: int = 600):
        self.attempts = attempts
        self.window = window
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allowed(self, address: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self.lock:
            recent = [stamp for stamp in self.failures.get(address, []) if now - stamp < self.window]
            self.failures[address] = recent
            if len(recent) < self.attempts:
                return True, 0
            return False, max(1, int(self.window - (now - recent[0])))

    def fail(self, address: str) -> None:
        with self.lock:
            self.failures.setdefault(address, []).append(time.monotonic())

    def success(self, address: str) -> None:
        with self.lock:
            self.failures.pop(address, None)


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 RK Cloud</title><style>
:root{color-scheme:light;--accent:#0067c0;--border:#d8dde5;--text:#1b1b1b;--muted:#5f6368}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;font-family:"Segoe UI Variable","Segoe UI","Microsoft YaHei UI",sans-serif;color:var(--text);background:radial-gradient(circle at 18% 4%,rgba(120,190,240,.28),transparent 34%),linear-gradient(145deg,#eaf3f9,#f4f5f8 52%,#e6edf4)}
.window{width:min(94vw,780px);overflow:hidden;border:1px solid #cbd3da;border-radius:10px;background:rgba(250,252,254,.96);box-shadow:0 24px 70px rgba(20,36,58,.2)}
.titlebar{height:40px;display:flex;align-items:center;padding:0 14px;border-bottom:1px solid #e4e9ee;background:#f2f6fa;font-size:12px;font-weight:600}
.mark{width:18px;height:18px;margin-right:9px;display:grid;grid-template-columns:1fr 1fr;gap:1px}.mark i{background:#1683d8}.controls{margin-left:auto;display:flex;gap:28px;color:#59636b;font-size:14px}
.toolbar{height:50px;display:flex;align-items:center;gap:9px;padding:8px 14px;border-bottom:1px solid #e2e7eb;background:#f8fafc}.back{font-size:20px;color:#5d6871}.address{flex:1;height:33px;display:flex;align-items:center;padding:0 12px;border:1px solid #d5dbe2;border-radius:6px;background:#fff;color:#52606a;font-size:12px}.lock{margin-right:8px;color:#237a42}
.main{display:grid;grid-template-columns:1.05fr .95fr;min-height:390px}.intro{padding:58px 48px;background:linear-gradient(145deg,rgba(235,246,253,.88),rgba(255,255,255,.5));border-right:1px solid #e1e6ea}.disk{position:relative;width:82px;height:53px;margin-bottom:30px;border-radius:6px 6px 9px 9px;background:linear-gradient(#eef2f5 0 51%,#aeb8c1 52% 73%,#75838e 74%);box-shadow:inset 0 0 0 1px #9da8b2,0 8px 16px rgba(29,52,69,.16)}.disk:before{content:"";position:absolute;top:-11px;left:10px;right:10px;height:15px;border:1px solid #adb8c0;border-bottom:0;border-radius:5px 5px 0 0;background:#e5ebef}.disk:after{content:"";position:absolute;right:9px;bottom:9px;width:6px;height:6px;border-radius:50%;background:#39c24f;box-shadow:0 0 5px #42dd59}
.eyebrow{margin:0 0 10px;color:var(--accent);font-size:11px;font-weight:700;letter-spacing:1.2px}.intro h1{margin:0 0 13px;font-size:30px;font-weight:600}.intro p{margin:0;max-width:320px;color:var(--muted);font-size:14px;line-height:1.7}
.panel{align-self:center;padding:42px 44px}.panel h2{margin:0 0 8px;font-size:22px;font-weight:600}.lead{margin:0 0 26px;color:var(--muted);font-size:13px;line-height:1.6}label{display:block;margin-bottom:8px;font-size:13px;font-weight:600}input{width:100%;height:42px;padding:0 12px;border:1px solid #9da8b2;border-radius:5px;background:#fff;color:var(--text);font:inherit;outline:none}input:focus{border-color:#4b96cc;box-shadow:inset 0 -2px var(--accent)}button{width:100%;height:42px;margin-top:14px;border:0;border-radius:5px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}button:hover{background:#075b9f}.error{margin:0 0 16px;padding:10px 12px;border-left:4px solid #c42b1c;border-radius:4px;background:#fdebea;color:#8d2118;font-size:13px}.hint{margin:15px 0 0;text-align:center;color:#76818a;font-size:11px}
@media(max-width:680px){body{padding:12px}.main{display:block}.intro{min-height:0;padding:30px 28px;border:0;border-bottom:1px solid #e1e6ea}.disk{display:none}.intro h1{font-size:24px}.panel{padding:30px 28px}.controls{display:none}}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--text:#f1f3f5;--muted:#b3bbc1;--border:#3b4349}body{background:#1c2227}.window{border-color:#46515a;background:#20262b}.titlebar,.toolbar{border-color:#343b40;background:#252d33}.address,input{border-color:#4a555d;background:#30383e;color:#eef2f5}.main{background:#20262b}.intro{border-color:#3b4349;background:linear-gradient(145deg,#263640,#20262b)}.error{background:#4b1f2a;color:#ffbdc8}.controls{color:#c4cbd0}}
</style></head><body><main class="window"><header class="titlebar"><span class="mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>RK Cloud 文件资源管理器<span class="controls" aria-hidden="true"><span>—</span><span>□</span><span>×</span></span></header><div class="toolbar"><span class="back">←</span><div class="address"><span class="lock">●</span>RK Cloud / 安全登录</div></div><div class="main"><section class="intro"><div class="disk" aria-hidden="true"></div><p class="eyebrow">RK3576 私有存储</p><h1>访问 SSD 云盘</h1><p>登录后直接进入 SSD 文件目录。文件始终保存在你的 RK3576 上，公网服务器仅负责安全转发。</p></section><section class="panel"><h2>输入访问 Key</h2><p class="lead">使用管理员为你创建的有效 Key 继续访问。</p>{error}<form method="post" action="/login"><input type="hidden" name="next" value="{next}"><label for="key">访问 Key</label><input id="key" name="key" type="password" autocomplete="current-password" autofocus required><button type="submit">进入 SSD</button></form><p class="hint">请勿在公共设备上保存或分享 Key</p></section></div></main></body></html>"""


def build_login_page(next_path: str, error: str = "") -> bytes:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    page = LOGIN_PAGE.replace("{error}", error_html)
    page = page.replace("{next}", html.escape(next_path, quote=True))
    return page.encode("utf-8")


class AuthHandler(http.server.BaseHTTPRequestHandler):
    server_version = "RKCloudAuth/1"
    sys_version = ""

    @property
    def app(self):
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        logging.info("%s %s", self.client_address[0], fmt % args)

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Real-IP", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded)) if forwarded else self.client_address[0]
        except ValueError:
            return self.client_address[0]

    def common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/healthz":
            self.send_response(200)
            self.common_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        if parsed.path == "/auth":
            entry = session_from_cookie(self.app.state, self.headers.get("Cookie", ""))
            self.send_response(204 if entry else 401)
            self.common_headers()
            self.send_header("Content-Length", "0")
            if entry:
                self.send_header("X-Cloud-User", "cloud-owner")
            self.end_headers()
            return
        if parsed.path == "/logout":
            self.send_response(303)
            self.common_headers()
            self.send_header("Set-Cookie", self.app.clear_cookie())
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if parsed.path == "/login":
            if session_from_cookie(self.app.state, self.headers.get("Cookie", "")):
                self.send_response(303)
                self.common_headers()
                self.send_header("Location", safe_next(urllib.parse.parse_qs(parsed.query).get("next", ["/"])[0]))
                self.end_headers()
                return
            next_path = safe_next(urllib.parse.parse_qs(parsed.query).get("next", ["/"])[0])
            self.render_login(next_path)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/logout":
            self.send_response(303)
            self.common_headers()
            self.send_header("Set-Cookie", self.app.clear_cookie())
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if parsed.path != "/login":
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if origin and not origin_allowed(
            origin,
            self.headers.get("Host", ""),
            self.headers.get("X-Forwarded-Host", ""),
        ):
            logging.warning(
                "rejected cross-origin login origin=%r host=%r forwarded_host=%r",
                origin,
                self.headers.get("Host", ""),
                self.headers.get("X-Forwarded-Host", ""),
            )
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_FORM_BYTES:
            self.send_error(413)
            return
        try:
            fields = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "strict"),
                keep_blank_values=True,
                max_num_fields=8,
            )
        except (UnicodeDecodeError, ValueError):
            self.send_error(400)
            return
        supplied = fields.get("key", [""])[0]
        next_path = safe_next(fields.get("next", ["/"])[0])
        address = self.client_ip()
        allowed, retry_after = self.app.limiter.allowed(address)
        if not allowed:
            self.send_response(429)
            self.common_headers()
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(build_login_page(next_path, "尝试次数过多，请稍后再试。"))
            return
        entry = verify_access_key(self.app.state, supplied)
        supplied = ""
        if not entry:
            self.app.limiter.fail(address)
            time.sleep(0.25)
            self.render_login(next_path, "Key 无效或已过期。", status=401)
            return
        self.app.limiter.success(address)
        token = issue_session(self.app.state, entry)
        self.send_response(303)
        self.common_headers()
        self.send_header("Set-Cookie", self.app.session_cookie(token))
        self.send_header("Location", next_path)
        self.end_headers()

    def render_login(self, next_path: str, error: str = "", status: int = 200) -> None:
        body = build_login_page(next_path, error)
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


class AuthApplication:
    def __init__(self, state: State):
        self.state = state
        self.limiter = FailureLimiter()

    @staticmethod
    def session_cookie(token: str) -> str:
        return f"{COOKIE_NAME}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax"

    @staticmethod
    def clear_cookie() -> str:
        return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"


def generate_access_key(key_id: str) -> str:
    return f"rk1.{key_id}.{b64u(secrets.token_bytes(24))}"


def add_key(state: State, name: str, expiry: int | None) -> str:
    if not NAME_RE.fullmatch(name):
        raise ValueError("name must contain only letters, digits, dot, underscore, and hyphen")
    with state.locked():
        store = state.load_keys()
        if any(entry["name"] == name for entry in store["keys"]):
            raise ValueError(f"key name already exists: {name}")
        key_id = secrets.token_hex(8)
        access_key = generate_access_key(key_id)
        salt = secrets.token_bytes(16)
        store["keys"].append({
            "id": key_id,
            "name": name,
            "enabled": True,
            "created_at": utc_now(),
            "expires_at": expiry,
            "key_version": 1,
            "salt": b64u(salt),
            "hash": b64u(derive_key(access_key, salt)),
        })
        state.save_keys(store)
    return access_key


def rotate_key(state: State, name: str, expiry_text: str | None) -> str:
    with state.locked():
        store = state.load_keys()
        entry = next((item for item in store["keys"] if item["name"] == name), None)
        if not entry:
            raise ValueError(f"unknown key: {name}")
        access_key = generate_access_key(entry["id"])
        salt = secrets.token_bytes(16)
        entry["salt"] = b64u(salt)
        entry["hash"] = b64u(derive_key(access_key, salt))
        entry["enabled"] = True
        entry["key_version"] = int(entry.get("key_version", 0)) + 1
        entry["rotated_at"] = utc_now()
        if expiry_text is not None:
            entry["expires_at"] = parse_expiry(expiry_text)
        state.save_keys(store)
    return access_key


def set_enabled(state: State, name: str, enabled: bool) -> None:
    with state.locked():
        store = state.load_keys()
        entry = next((item for item in store["keys"] if item["name"] == name), None)
        if not entry:
            raise ValueError(f"unknown key: {name}")
        entry["enabled"] = enabled
        entry["key_version"] = int(entry.get("key_version", 0)) + 1
        state.save_keys(store)


def command_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudkey", description="Manage RK Cloud access keys")
    parser.add_argument("--state-dir", default=os.environ.get("CLOUD_AUTH_STATE_DIR", DEFAULT_STATE_DIR))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--expires", default="never")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("name")
    rotate.add_argument("--expires", default=None)
    for action in ("revoke", "enable"):
        item = sub.add_parser(action)
        item.add_argument("name")
    sub.add_parser("list")
    sub.add_parser("sessions-reset")
    serve = sub.add_parser("serve")
    serve.add_argument("--listen", default=DEFAULT_LISTEN)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    state = State(args.state_dir)
    try:
        if args.command == "init":
            state.initialize()
            print(f"initialized {state.directory}")
        elif args.command == "add":
            state.initialize()
            value = add_key(state, args.name, parse_expiry(args.expires))
            print("Save this Key now; it will not be shown again:")
            print(value)
        elif args.command == "rotate":
            value = rotate_key(state, args.name, args.expires)
            print("Save this replacement Key now; the previous Key and sessions are invalid:")
            print(value)
        elif args.command == "revoke":
            set_enabled(state, args.name, False)
            print(f"revoked {args.name}")
        elif args.command == "enable":
            set_enabled(state, args.name, True)
            print(f"enabled {args.name}")
        elif args.command == "list":
            store = state.load_keys()
            print(f"{'NAME':<24} {'ENABLED':<8} {'VERSION':<8} {'EXPIRES (UTC)'}")
            for entry in store["keys"]:
                print(f"{entry['name']:<24} {str(entry['enabled']):<8} {entry['key_version']:<8} {iso_time(entry.get('expires_at'))}")
        elif args.command == "sessions-reset":
            with state.locked():
                state.reset_sessions()
            print("all browser sessions invalidated")
        elif args.command == "serve":
            state.initialize()
            server = http.server.ThreadingHTTPServer((args.listen, args.port), AuthHandler)
            server.app = AuthApplication(state)  # type: ignore[attr-defined]
            server.daemon_threads = True
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            logging.info("cloud auth listening on %s:%s", args.listen, args.port)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"cloudkey: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(command_main())
