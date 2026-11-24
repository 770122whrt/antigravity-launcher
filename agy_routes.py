"""Read-only discovery of installed proxy providers and per-app Mihomo routing."""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit
import winreg


LOCAL = "127.0.0.1"
GROUP = "Antigravity-Route"
_active = {}


@dataclass(frozen=True)
class Candidate:
    name: str
    source: Path | None
    core: Path | None
    endpoint: str = ""
    interface: str = ""


def _request(port, path, token="", timeout=1.0):
    connection = http.client.HTTPConnection(LOCAL, port, timeout=timeout)
    try:
        headers = {"Authorization": "Bearer " + token} if token else {}
        connection.request("GET", path, headers=headers)
        result = connection.getresponse()
        if result.status != 200:
            raise OSError(f"本地代理接口返回 HTTP {result.status}")
        return json.loads(result.read())
    finally:
        connection.close()


def _system_port():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return None
            setting = winreg.QueryValueEx(key, "ProxyServer")[0]
    except OSError:
        return None
    parts = str(setting).split(";")
    server = next((p[6:] for p in parts if p.startswith("https=")),
                  next((p[5:] for p in parts if p.startswith("http=")), parts[0]))
    try:
        url = urlsplit(server if "://" in server else "http://" + server)
        if url.hostname in ("localhost", LOCAL) and url.scheme == "http" and not url.username:
            return url.port
    except ValueError:
        pass
    return None


def _core_path():
    override = os.environ.get("AGY_MIHOMO")
    if override and Path(override).is_file():
        return Path(override)
    candidates = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                                    0, winreg.KEY_READ | view) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                                if "clash verge" in winreg.QueryValueEx(key, "DisplayName")[0].lower():
                                    folder = winreg.QueryValueEx(key, "InstallLocation")[0]
                                    candidates.append(Path(folder) / "verge-mihomo.exe")
                        except OSError:
                            continue
            except OSError:
                pass
    candidates += [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Clash Verge" / "verge-mihomo.exe",
                   ]
    found = shutil.which("mihomo.exe")
    if found:
        candidates.append(Path(found))
    return next((p for p in candidates if p.is_file()), None)


def _sources():
    # The provider core reads the original file. Never load node records into the launcher.
    override = os.environ.get("AGY_PROXY_CONFIG")
    return ([Path(override)] if override else []) + [Path.home() / ".config" / "com.vortex.helper" / "config.yaml",
            Path(os.environ["APPDATA"]) / "io.github.clash-verge-rev.clash-verge-rev" / "clash-verge.yaml"]


def _routing_fields(path):
    fields = {}
    with path.open("rb") as file:
        for line in file:
            if line.startswith((b"mixed-port:", b"external-controller:", b"proxies:")):
                k, val = line.split(b":", 1)
                fields[k.decode()] = True if k == b"proxies" else val.decode().strip().strip("'\u0022")
    return fields


def _current_node(fields, port):
    address = fields.get("external-controller", "")
    match = re.fullmatch(r"(?:127\.0\.0\.1|localhost):(\d+)", address)
    endpoints = [int(match.group(1))] if match else []
    # SakuraCat can bind the helper to a different free port than its saved YAML.
    command = [os.path.join(os.environ["SystemRoot"], "System32", "netstat.exe"), "-ano"]
    result = subprocess.run(command, capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    listeners = []
    for line in result.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "TCP" and parts[2] in ("0.0.0.0:0", "[::]:0"):
            host, _, value = parts[1].rpartition(":")
            if value.isdigit() and host in (LOCAL, "0.0.0.0", "[::]", "[::1]"):
                listeners.append((host, int(value), parts[4]))
    owners = {pid for _, p, pid in listeners if p == port}
    endpoints.extend(p for host, p, pid in listeners if host == LOCAL and p != port and pid in owners)
    for controller in dict.fromkeys(endpoints):
        try:
            config = _request(controller, "/configs")
            if config.get("mode", "").lower() != "global" or config.get("mixed-port") != port:
                continue
            selected = _request(controller, "/proxies/GLOBAL")
            for _ in range(6):
                name = selected.get("now")
                if not name:
                    return selected.get("name")
                selected = _request(controller, "/proxies/" + quote(name, safe=""))
        except (OSError, ValueError, http.client.HTTPException):
            continue
    return None


def _free_port():
    with socket.socket() as s:
        s.bind((LOCAL, 0))
        return s.getsockname()[1]


def _physical_interface():
    # A second core must reach the upstream server through the physical uplink.
    # Otherwise an active TUN can feed it through another proxy a second time.
    script = """
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $physical = @(Get-NetAdapter -Physical -ErrorAction Stop |
        Where-Object Status -eq 'Up' | Select-Object -ExpandProperty ifIndex)
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
        Where-Object { $_.InterfaceIndex -in $physical } |
        Sort-Object { [int]$_.RouteMetric + [int]$_.InterfaceMetric } |
        Select-Object -First 1 -ExpandProperty InterfaceAlias
    """
    command = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(command), "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode:
        return ""
    return result.stdout.decode("utf-8", errors="replace").strip()


def _guard(process):
    """An unnamed Windows job terminates the owned core if the launcher dies."""
    class Limit(ctypes.Structure):
        _fields_ = [(n, t) for n,t in (
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
            ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("max_process", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("sched", wintypes.DWORD))]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Limit), ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            and k.AssignProcessToJobObject(job, int(process._handle))):
        error = ctypes.get_last_error()
        k.CloseHandle(job)
        raise ctypes.WinError(error)
    return k, job


def _pid_active(pid):
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        # ERROR_INVALID_PARAMETER means the PID has gone; access denial is ambiguous.
        return ctypes.get_last_error() != 87
    try:
        return k.WaitForSingleObject(handle, 0) != 0
    finally:
        k.CloseHandle(handle)


def _runtime_root():
    root = Path(tempfile.gettempdir()) / "AntigravityLauncher-Mihomo"
    root.mkdir(exist_ok=True)
    if root.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("代理工作目录指向重解析点，已停止")
    root = root.resolve()
    for previous in root.iterdir():
        try:
            match = re.fullmatch(r"run-(\d+)-[a-z0-9_]+", previous.name)
            if not match or previous.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            if not previous.is_dir() or previous.resolve().parent != root or _pid_active(int(match[1])):
                continue
            files = list(previous.iterdir())
            if any(f.name not in {"owner.json", "route.json", "cache.db"} or
                   f.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or
                   not f.is_file() for f in files):
                continue
            marker = previous / "owner.json"
            if not marker.is_file() or marker.stat().st_size > 256:
                continue
            record = json.loads(marker.read_text(encoding="ascii"))
            if record.get("kind") != "agy-private-mihomo-1" or record.get("owner") != int(match[1]):
                continue
            core_pid = record.get("core")
            if core_pid is None and (previous / "cache.db").exists():
                continue
            if core_pid is not None and (not isinstance(core_pid, int) or not 0 < core_pid < 2**32 or _pid_active(core_pid)):
                continue
            # Keep the ownership record until all other known files are gone.
            for file in files:
                if file != marker:
                    file.unlink()
            marker.unlink()
            previous.rmdir()
        except (OSError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    return root


@contextlib.contextmanager
def _instance(core, source, node=None, cancel=None, interface=""):
    control = _free_port()
    entrance = _free_port()
    while entrance == control:
        entrance = _free_port()
    secret = secrets.token_urlsafe(32)
    group = {"name": GROUP, "type": "select", "use": ["existing"]}
    if node:
        # Go regexp. Quote the literal name without relying on Python-specific escapes.
        group["filter"] = "^" + re.sub(r"([.\\+*?\[\]^$(){}|])", r"\\\1", node) + "$"
    cfg = {"external-controller": f"{LOCAL}:{control}", "secret": secret, "log-level": "silent",
           "mode": "rule", "allow-lan": False, "tun": {"enable": False}, "find-process-mode": "always",
           "profile": {"store-selected": False}, "proxy-groups": [group],
           "proxy-providers": {"existing": {"type": "file", "path": str(source),
                                "health-check": {"enable": False},
                                "override": {"skip-cert-verify": False}}},
           "rules": ["MATCH," + GROUP]}
    if node:
        if interface:
            cfg["interface-name"] = interface
        cfg["listeners"] = [{"name": "agy", "type": "http", "listen": LOCAL,
                             "port": entrance, "proxy": GROUP}]
    with tempfile.TemporaryDirectory(dir=_runtime_root(), prefix=f"run-{os.getpid()}-") as folder:
        metadata = {"kind": "agy-private-mihomo-1", "owner": os.getpid()}
        marker = Path(folder) / "owner.json"
        marker.write_text(json.dumps(metadata), encoding="ascii")
        config_file = Path(folder) / "route.json"
        config_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["SAFE_PATHS"] = str(source)
        p = subprocess.Popen([str(core), "-d", folder, "-f", str(config_file)], cwd=folder, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        guard = None
        try:
            guard = _guard(p)
            metadata["core"] = p.pid
            marker.write_text(json.dumps(metadata), encoding="ascii")
            deadline = time.monotonic() + 9
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("已取消")
                if p.poll() is not None:
                    raise OSError("独立代理启动失败，未能加载现有节点配置")
                try:
                    status = _request(control, "/proxies/" + GROUP, secret)
                    if (not node and status.get("all")) or (node and status.get("all") == [node] and status.get("now") == node):
                        break
                except (OSError, ValueError, http.client.HTTPException):
                    pass
                time.sleep(.08)
            else:
                raise OSError("独立代理未能确认指定节点，已停止")
            config_file.unlink()  # Already loaded, including the short-lived controller credential.
            yield control, secret, f"http://{LOCAL}:{entrance}", status
        finally:
            if guard:
                guard[0].CloseHandle(guard[1])
            elif p.poll() is None:
                p.kill()
            p.wait(timeout=4)


def candidates(last_name, emit):
    port = _system_port()
    core = _core_path()
    for source in _sources() if core and port else []:
        try:
            if not source.is_file():
                continue
            fields = _routing_fields(source)
            if fields.get("mixed-port") != str(port) or not fields.get("proxies"):
                continue
            current = _current_node(fields, port)
            with _instance(core, source) as (controller, token, _, _):
                provider = _request(controller, "/providers/proxies/existing", token, timeout=3)
                names = [p["name"] for p in provider.get("proxies", [])
                         if p.get("name") and p.get("type", "").lower() not in
                         {"direct", "reject", "rejectdrop", "pass", "compatible", "selector"}]
            if not names:
                continue
            ordered = list(dict.fromkeys(n for n in [last_name, current, *names] if n in names))
            emit(f"已读取 {len(ordered)} 个已有节点；将使用仅供启动器使用的本地代理。")
            interface = _physical_interface()
            if interface:
                emit(f"独立代理通过 {interface} 连接节点，避免系统 TUN 二次转发。")
            else:
                emit("未识别到物理默认出口，将使用系统路由进行网络验证。")
            return [Candidate(n, source, core, interface=interface) for n in ordered]
        except (OSError, ValueError, http.client.HTTPException, subprocess.TimeoutExpired) as exc:
            code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            emit(f"独立代理读取失败（{type(exc).__name__}{' ' + str(code) if code else ''}），继续检查可用入口。")
    if port:
        emit("当前仅支持系统代理已选线路；无法独立切换其他节点。")
        return [Candidate("现有系统代理（共享入口）", None, None, f"http://{LOCAL}:{port}")]
    raise OSError("没有找到已启用的本地 HTTP 系统代理。请先在代理客户端启用系统代理。")


@contextlib.contextmanager
def open_route(candidate, cancel, emit):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("已取消")
    if candidate.endpoint:
        if _system_port() != urlsplit(candidate.endpoint).port:
            raise OSError("系统代理入口已改变，请重新检测")
        yield candidate.endpoint
        return
    with _instance(candidate.core, candidate.source, candidate.name, cancel, candidate.interface) as (control, secret, url, _):
        _active[url] = control, secret
        try:
            yield url
        finally:
            _active.pop(url, None)


def route_evidence(url):
    """Only connection destination, client process name/source port and selected chain."""
    current = _active.get(url)
    if not current:
        return []
    try:
        data = _request(current[0], "/connections", current[1])
    except (OSError, ValueError, http.client.HTTPException):
        return []
    rows = []
    for row in data.get("connections") or []:
        m = row.get("metadata") or {}
        rows.append({"host": m.get("host", ""), "process": m.get("process", ""),
                     "sourcePort": m.get("sourcePort"), "chains": row.get("chains") or []})
    return rows
