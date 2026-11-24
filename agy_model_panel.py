"""通过现有 Antigravity 账号查询和实测 Gemini 模型。"""
from contextlib import contextmanager
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from tkinter import ttk

import psutil

import agy_cli_probe as cli
import agy_routes
from agy_transport import Relay

HOME = Path(__file__).resolve().parent
WORK = HOME / "检测工作区"


def desktop_relay():
    """只复用这个启动器持有、并被桌面客户端明确指定的回环端口。"""
    desktop_ports = set()
    launchers = []
    for proc in psutil.process_iter(["name"]):
        name = (proc.info["name"] or "").lower()
        try:
            if name == "antigravity.exe":
                for arg in proc.cmdline()[1:]:
                    match = re.fullmatch(r"--proxy-server=http://127\.0\.0\.1:(\d+)", arg)
                    if match:
                        desktop_ports.add(int(match[1]))
            elif name in {"python.exe", "pythonw.exe"}:
                if any(Path(a) == HOME / "launcher.pyw" for a in proc.cmdline()[1:]):
                    launchers.append(proc)
        except (psutil.Error, OSError):
            continue
    for proc in launchers:
        try:
            for conn in proc.connections(kind="tcp"):
                if (conn.status == psutil.CONN_LISTEN and conn.laddr.ip == "127.0.0.1"
                        and conn.laddr.port in desktop_ports):
                    return f"http://127.0.0.1:{conn.laddr.port}"
        except (psutil.Error, OSError):
            continue
    return None


@contextmanager
def probe_route(cancel, emit):
    endpoint = desktop_relay()
    if endpoint:
        emit("使用正在运行的 Antigravity 桌面专用代理。")
        yield endpoint
        return
    try:
        last = json.loads((HOME / "上次成功.json").read_text(encoding="utf-8")).get("node")
    except (OSError, ValueError, AttributeError):
        last = None
    choices = agy_routes.candidates(last, emit)
    if not choices:
        raise OSError("no candidate")
    emit(f"本次探测线路：{choices[0].name}")
    with agy_routes.open_route(choices[0], cancel, emit) as endpoint:
        yield endpoint


class ModelListError(Exception):
    pass


def fetch_models(endpoint):
    result = subprocess.run(
        [cli.cli_path(), "models"], cwd=WORK, env=cli.proxy_env(endpoint),
        stdin=subprocess.DEVNULL, capture_output=True, timeout=24,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    output = result.stdout.decode("utf-8-sig", errors="replace")
    if result.returncode:
        detail = cli._failure(output + "\n" + result.stderr.decode("utf-8", errors="replace"))[1]
        raise ModelListError("读取账号模型列表失败。" + detail)
    models = []
    for row in output.splitlines():
        fields = row.strip().split(maxsplit=1)
        if fields and re.fullmatch(r"gemini-[a-z0-9][a-z0-9.-]*", fields[0]) and fields[0] not in models:
            models.append(fields[0])
    if not models:
        raise ModelListError("CLI 没有列出可选择的 Gemini 模型。")
    return models


class ModelPanel(ttk.LabelFrame):
    def __init__(self, parent, emit):
        super().__init__(parent, text="Gemini 模型探测", padding=(8, 5))
        self.note = emit
        self.jobs = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        row = ttk.Frame(self)
        row.pack(fill="x")
        self.model = ttk.Combobox(row, state="readonly", width=30)
        self.model.pack(side="left", fill="x", expand=True)
        self.refresh = ttk.Button(row, text="刷新列表", command=lambda: self.start(None))
        self.refresh.pack(side="left", padx=5)
        self.test = ttk.Button(row, text="探测所选模型", command=lambda: self.start(self.model.get()))
        self.test.pack(side="left")
        self.test.config(state="disabled")
        self.stop = ttk.Button(row, text="取消探测", command=self.cancel.set, state="disabled")
        self.stop.pack(side="left", padx=(5, 0))
        self.after(100, self.pump)

    def start(self, model):
        if self.busy or model == "":
            return
        self.busy = True
        if model is None:
            self.saved_model = self.model.get()
            self.model.set("")
            self.model.config(values=())
        self.cancel.clear()
        self.test.config(state="disabled")
        self.refresh.config(state="disabled")
        self.model.config(state="disabled")
        self.stop.config(state="normal")
        self.note("读取官方 CLI 的模型列表…" if model is None else f"正在探测 {model} …")
        threading.Thread(target=self.worker, args=(model,), daemon=False).start()

    def worker(self, model):
        def emit(text):
            self.jobs.put(("log", text))
        try:
            WORK.mkdir(exist_ok=True)
            with probe_route(self.cancel, emit) as endpoint:
                if self.cancel.is_set():
                    raise InterruptedError
                if model is None:
                    models = fetch_models(endpoint)
                    if not self.cancel.is_set():
                        self.jobs.put(("models", models))
                else:
                    with Relay(endpoint, lambda _: None) as relay:
                        start = time.monotonic()
                        status, detail = cli.probe(relay.url, WORK, self.cancel, lambda _: None, model=model)
                        if status == "ok" and not relay.model_path_seen():
                            detail = "已收到回复，但未确认 CLI 的模型代理连接。"
                        emit(f"{model}（{time.monotonic()-start:.1f} 秒）{detail}")
        except subprocess.TimeoutExpired:
            emit("读取模型列表超时。")
        except InterruptedError:
            pass
        except ModelListError as exc:
            emit(str(exc))
        except Exception as exc:
            emit(f"探测未完成（{type(exc).__name__}），请检查启动器的代理线路或 CLI。")
        finally:
            self.jobs.put(("done", None))

    def pump(self):
        while True:
            try:
                event, value = self.jobs.get_nowait()
            except queue.Empty:
                break
            if event == "models":
                selected = self.saved_model
                self.model.config(values=value)
                default = "gemini-4-carbon-high"
                self.model.set(selected if selected in value else default if default in value else value[0])
                self.note(f"已列出 {len(value)} 个 Gemini 档位。")
            elif event == "log":
                self.note(value)
            elif event == "done":
                self.busy = False
                if self.cancel.is_set():
                    self.note("已取消。")
                self.model.config(state="readonly")
                self.refresh.config(state="normal")
                self.test.config(state="normal" if self.model.get() else "disabled")
                self.stop.config(state="disabled")
        self.after(100, self.pump)
