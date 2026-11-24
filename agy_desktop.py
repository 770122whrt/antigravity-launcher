"""发现本机 Antigravity，以进程级代理启动并观察客户端与语言服务。"""

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
import winreg

import psutil


_EXE = "Antigravity.exe"


def _registered_paths():
    keypath = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            parent = winreg.OpenKey(hive, keypath)
        except OSError:
            continue
        with parent:
            for index in range(winreg.QueryInfoKey(parent)[0]):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as app:
                        name = winreg.QueryValueEx(app, "DisplayName")[0]
                        if not (name == "Antigravity" or name.startswith("Antigravity ")):
                            continue
                        icon = winreg.QueryValueEx(app, "DisplayIcon")[0]
                        path = Path(os.path.expandvars(icon.rsplit(",", 1)[0].strip('"')))
                        if path.name.lower() == _EXE.lower():
                            yield path
                except OSError:
                    continue


def app_path() -> str:
    override = os.environ.get('AGY_DESKTOP')
    if override:
        if Path(override).is_file():
            return str(Path(override))
        raise FileNotFoundError('AGY_DESKTOP 指定的 Antigravity.exe 不存在。')
    for path in _registered_paths():
        if path.is_file():
            return str(path)
    for directory in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        if directory:
            for relative in ("Programs/Antigravity", "Antigravity"):
                path = Path(directory, relative, _EXE)
                if path.is_file():
                    return str(path)
    raise FileNotFoundError("找不到已安装的 Google Antigravity 桌面程序。")


def _client_processes(executable):
    backend = executable.parent / "resources" / "bin" / "language_server.exe"
    paths = {executable: "桌面", backend: "后台语言服务"}
    names = {path.name.lower() for path in paths}
    wrapper = Path(os.environ["APPDATA"]) / "Antigravity" / "bin" / "agy-node.cmd"
    wrapper_path = re.compile(re.escape(str(wrapper)) + r'(?=$|[\s"])', re.IGNORECASE)
    result = {}
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() not in names:
            continue
        try:
            role = paths.get(Path(proc.exe()))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if role == "桌面" and _is_node_wrapper_child(proc, wrapper_path):
            continue
        if role:
            result[proc.pid] = role
    return result


@lru_cache(maxsize=128)
def _is_node_wrapper_child(proc, wrapper_path):
    # Antigravity 2.32.1 的 agy-node.cmd 以 ELECTRON_RUN_AS_NODE=1 运行同一 exe。
    # psutil.Process 以 PID 和启动时间标识进程。缓存角色避免每次采样都重读父进程。
    try:
        parent = proc.parent()
        if parent is None or parent.name().lower() != "cmd.exe":
            return False
        return bool(wrapper_path.search(" ".join(parent.cmdline()[1:])))
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def is_running() -> bool:
    return bool(_client_processes(Path(app_path())))


def _proxy_port(proxy_url):
    address = urlsplit(proxy_url)
    if (address.scheme != "http" or address.hostname != "127.0.0.1"
            or address.username is not None or address.password is not None
            or address.path not in ("", "/") or address.query or address.fragment
            or not address.port):
        raise ValueError("客户端需要仅监听本机的 HTTP 代理。")
    return address.port


def launch_app(proxy_url) -> subprocess.Popen:
    _proxy_port(proxy_url)
    executable = Path(app_path())
    if _client_processes(executable):
        raise RuntimeError("Antigravity 或其后台仍在运行。请保存工作并通过客户端菜单退出，再启动。")
    proxy_vars = {"http_proxy", "https_proxy", "all_proxy", "grpc_proxy", "no_proxy", "no_grpc_proxy"}
    env = {k: v for k, v in os.environ.items() if k.lower() not in proxy_vars}
    env.update(HTTP_PROXY=proxy_url, HTTPS_PROXY=proxy_url, ALL_PROXY=proxy_url,
               grpc_proxy=proxy_url, NO_PROXY="localhost,127.0.0.1,::1",
               no_grpc_proxy="localhost,127.0.0.1,::1")
    return subprocess.Popen(
        [str(executable), "--proxy-server=" + proxy_url,
         "--proxy-bypass-list=localhost;127.0.0.1;[::1]", "--disable-quic"],
        env=env, cwd=executable.parent, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def monitor_app(proc, proxy_url, emit, stop=None) -> None:
    """监护应用至自然退出；不终止桌面或语言服务，只报告可观测到的 TCP。"""
    port = _proxy_port(proxy_url)
    executable = Path(app_path())
    reported = set()
    absent_since = None
    while stop is None or not stop.is_set():
        roles = _client_processes(executable)
        if proc.poll() is None:
            roles[proc.pid] = "桌面"
        if not roles:
            if absent_since is None:
                absent_since = time.monotonic()
            elif time.monotonic() - absent_since >= 2:
                return
        else:
            absent_since = None
            try:
                rows = psutil.net_connections(kind="tcp")
            except (psutil.AccessDenied, OSError):
                rows = ()
                if "monitor_unavailable" not in reported:
                    emit("Windows 未允许读取 TCP 表。客户端仍受监护，进程路由证据不完整。")
                    reported.add("monitor_unavailable")
            for row in rows:
                if row.pid not in roles or row.status != psutil.CONN_ESTABLISHED or not row.raddr:
                    continue
                address = ipaddress.ip_address(row.raddr.ip.split("%")[0])
                if address.version == 6 and address.ipv4_mapped:
                    address = address.ipv4_mapped
                if address.is_loopback:
                    if row.raddr.port != port:
                        continue
                    observation = "已建立到本次专用代理的 TCP 连接"
                else:
                    observation = "存在非回环 TCP 连接，未经过本次本地代理端口；系统 TUN 可能另行承载"
                key = roles[row.pid], observation
                if key not in reported:
                    emit(f"{key[0]}：{observation}。")
                    reported.add(key)
        time.sleep(0.7)
