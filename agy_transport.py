"""Private loopback forwarding and connection evidence. No TLS interception."""
import ctypes
from ctypes import wintypes
import os
import select
import socket
import socketserver
import struct
import threading
from urllib.parse import urlsplit


def caller_name(port):
    iphlp = ctypes.WinDLL("iphlpapi")
    size = wintypes.DWORD()
    table = iphlp.GetExtendedTcpTable
    table(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
    buf = ctypes.create_string_buffer(size.value)
    if table(buf, ctypes.byref(size), False, socket.AF_INET, 5, 0):
        return ""
    count = struct.unpack_from("<I", buf)[0]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    for i in range(count):
        _, addr, local, _, _, pid = struct.unpack_from("<6I", buf, 4 + i * 24)
        if socket.ntohs(local & 65535) != port or addr != 0x0100007f:
            continue
        h = kernel.OpenProcess(0x1000, False, pid)
        if h:
            try:
                length = wintypes.DWORD(32768)
                path = ctypes.create_unicode_buffer(length.value)
                if kernel.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(length)):
                    return os.path.basename(path.value).lower()
            finally:
                kernel.CloseHandle(h)
    return ""


def service(host):
    host = host.lower().rstrip(".")
    if host.endswith(".googleapis.com"):
        if any(word in host.split(".")[0] for word in
               ("cloudcode", "generativelanguage", "aiplatform", "antigravity", "jetski")):
            return "Google 模型服务"
        return "Google API"
    if host == "antigravity.google" or host.endswith(".antigravity.google"):
        return "Antigravity 服务"
    if host.endswith(".google.com") or host == "google.com":
        return "Google 网站"
    return "其他 HTTPS 服务"


class Forwarder(socketserver.BaseRequestHandler):
    def handle(self):
        peer = self.request
        try:
            peer.settimeout(10)
            first = b""
            while b"\r\n" not in first and len(first) < 8192:
                chunk = peer.recv(4096)
                if not chunk:
                    return
                first += chunk
            fields = first.split(b"\r\n", 1)[0].split(b" ")
            if len(fields) != 3:
                return
            with socket.create_connection(self.server.upstream, timeout=8) as remote:
                remote.settimeout(15)
                remote.sendall(first)
                kind = None
                if fields[0] == b"CONNECT":
                    domain = fields[1].rsplit(b":", 1)[0].decode("ascii", "ignore")
                    kind = service(domain)
                while not self.server.stop_event.is_set():
                    ready, _, _ = select.select([peer, remote], [], [], 0.7)
                    for sock in ready:
                        data = sock.recv(65536)
                        if not data:
                            return
                        if sock is remote and kind is not None:
                            if data.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                                self.server.record(self.client_address[1], kind)
                            kind = None
                        (remote if sock is peer else peer).sendall(data)
        except OSError:
            return


class Relay(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, upstream, emit):
        url = urlsplit(upstream)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or not url.port:
            raise ValueError("需要本机 HTTP 代理端口")
        self.upstream = ("127.0.0.1", url.port)
        self.stop_event = threading.Event()
        self.emit = emit
        self.events = set()
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), Forwarder)
        self.url = "http://127.0.0.1:" + str(self.server_address[1])

    def record(self, port, kind):
        try:
            name = caller_name(port)
        except (OSError, ValueError):
            name = ""
        if not name or name.startswith(("python", "powershell")):
            return
        with self.lock:
            if (name, kind) in self.events:
                return
            self.events.add((name, kind))
        self.emit(f"代理连接已建立：{name} → {kind}")

    def model_path_seen(self):
        with self.lock:
            return any(("agy" in name or "language_server" in name or "antigravity" in name)
                       and kind in ("Google 模型服务", "Antigravity 服务")
                       for name, kind in self.events)

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_event.set()
        self.shutdown()
        self.server_close()
        self.thread.join(2)
