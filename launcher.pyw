import ctypes
from contextlib import ExitStack
import datetime
import gzip
import http.client
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlsplit

import agy_cli_probe
import agy_desktop
import agy_routes
from agy_transport import Relay
from agy_model_panel import ModelPanel


HOME = Path(__file__).resolve().parent
LAST = HOME / "上次成功.json"
LOG = HOME / "运行记录.txt"
MAX_ATTEMPTS = 3


class CheckFailed(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


class TermsText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found_label = False
        self.country = ""

    def handle_data(self, data):
        text = data.strip()
        if self.country or not text:
            return
        if self.found_label:
            self.country = text[:80]
        elif text.startswith("Country version:"):
            self.found_label = True
            self.country = text[len("Country version:"):].strip()[:80]


def google_check(proxy_url, terms, cancel, on_request=None):
    host, path = ("policies.google.com", "/terms?hl=en") if terms else ("www.google.com", "/generate_204")
    proxy = urlsplit(proxy_url)
    c = http.client.HTTPSConnection(proxy.hostname, proxy.port, timeout=9,
                                    context=ssl.create_default_context())
    c.set_tunnel(host, 443)
    try:
        c.request("GET", path, headers={"Host":host, "Accept-Language":"en", "Accept-Encoding":"gzip"})
        if on_request:
            on_request(host)
        r = c.getresponse()
        expected = 200 if terms else 204
        if r.status != expected:
            state = "network" if r.status in (502, 503, 504) else "error"
            raise CheckFailed(state, f"{host} 返回 HTTP {r.status}，预期 {expected}。")
        if not terms:
            return
        parser = TermsText()
        reader = gzip.GzipFile(fileobj=r) if r.getheader("Content-Encoding") == "gzip" else r
        deadline = time.monotonic() + 15
        while not cancel.is_set() and time.monotonic() < deadline:
            block = reader.read(16384)
            if not block:
                break
            parser.feed(block.decode("utf-8", "replace"))
            if parser.country:
                return parser.country
        if cancel.is_set():
            raise CheckFailed("cancel", "检测已取消。")
        raise CheckFailed("error", "Google 条款页未给出可识别的地区字段，无法完成第二项验证。")
    except ssl.SSLCertVerificationError:
        raise CheckFailed("error", "HTTPS 证书校验失败。请检查系统时间、证书和代理。") from None
    except (OSError, EOFError, http.client.HTTPException) as e:
        raise CheckFailed("network", f"连接 {host} 失败（{type(e).__name__}）。") from None
    finally:
        c.close()


def previous_name():
    try:
        return json.loads(LAST.read_text(encoding="utf-8")).get("node")
    except (OSError, ValueError, AttributeError):
        return None


def remember(name):
    tmp = LAST.with_suffix(".tmp")
    tmp.write_text(json.dumps({"node":name}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LAST)


class Launcher:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.active = False
        self.exiting = False
        self.background = False
        self.exit_prompt = None
        self.keep = False
        self.login_candidate = None
        self.phase = tk.StringVar(value="准备检查")
        root.title("Antigravity 代理检测启动器")
        root.geometry("730x510")
        root.minsize(570,400)
        frame = ttk.Frame(root,padding=16)
        frame.pack(fill="both",expand=True)
        ttk.Label(frame,textvariable=self.phase,font=("Microsoft YaHei UI",14,"bold")).pack(anchor="w")
        ttk.Label(frame,text="最多检查 3 个候选。仅验证 Gemini，真实请求会使用少量额度。",wraplength=590).pack(anchor="w",pady=(5,10))
        self.probe_panel = ModelPanel(frame, self.emit)
        self.probe_panel.pack(fill="x", pady=(0, 8))
        self.text=tk.Text(frame,wrap="word",font=("Microsoft YaHei UI",10),state="disabled",height=12)
        self.text.pack(fill="both",expand=True)
        buttons=ttk.Frame(frame)
        buttons.pack(fill="x",pady=(10,0))
        self.retry=ttk.Button(buttons,text="重新检测",command=self.start)
        self.retry.pack(side="left")
        self.login=ttk.Button(buttons,text="登录官方 CLI",command=lambda:self.start(True))
        self.login.pack(side="left",padx=7)
        self.stop=ttk.Button(buttons,text="取消",command=self.request_cancel)
        self.stop.pack(side="right")
        root.protocol("WM_DELETE_WINDOW",self.close)
        LOG.write_text("",encoding="utf-8")
        root.after(100,self.pump)
        root.after(200,self.start)

    def emit(self,text):
        self.events.put(("log",str(text)))

    def pump(self):
        try:
            while True:
                key,val=self.events.get_nowait()
                if key=="log":
                    line=datetime.datetime.now().strftime("%H:%M:%S")+"  "+val+"\n"
                    self.text.configure(state="normal")
                    self.text.insert("end",line)
                    self.text.see("end")
                    self.text.configure(state="disabled")
                    with LOG.open("a",encoding="utf-8") as out:
                        out.write(line)
                elif key=="phase":
                    self.phase.set(val)
                    self.stop.config(text="后台运行" if self.keep else "取消")
                elif key=="done":
                    self.active=False
                    self.keep=False
                    self.phase.set(val)
                    self.retry.config(state="normal")
                    self.login.config(state="normal" if self.login_candidate else "disabled")
                    self.stop.config(state="disabled")
        except queue.Empty:
            pass
        if self.background and not self.active and not ctypes.windll.user32.IsWindowVisible(self.window_handle()):
            self.exiting = True
            self.probe_panel.cancel.set()
        if self.exiting and not self.active and not self.probe_panel.busy:
            self.root.destroy()
            return
        self.root.after(150,self.pump)

    def window_handle(self):
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        user.GetAncestor.restype = ctypes.c_void_p
        return ctypes.c_void_p(user.GetAncestor(self.root.winfo_id(), 2))

    def keep_in_background(self):
        self.dismiss_prompt()
        self.background = True
        ctypes.windll.user32.ShowWindow(self.window_handle(), 0)

    def dismiss_prompt(self):
        if self.exit_prompt is not None:
            self.exit_prompt.destroy()
            self.exit_prompt = None

    def exit_launcher(self):
        self.dismiss_prompt()
        self.exiting = True
        self.cancel.set()
        self.probe_panel.cancel.set()
        self.root.withdraw()

    def close(self):
        if self.exiting:
            return
        if not self.keep:
            self.exit_launcher()
            return
        if self.exit_prompt is not None:
            self.exit_prompt.lift()
            return
        prompt = self.exit_prompt = tk.Toplevel(self.root)
        prompt.title("关闭启动器")
        prompt.transient(self.root)
        prompt.resizable(False, False)
        frame = ttk.Frame(prompt, padding=14)
        frame.pack()
        ttk.Label(frame, text="当前正在使用专用代理。完全退出会断开这条线路。").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(12, 0))
        ttk.Button(buttons, text="完全退出并断开代理", command=self.exit_launcher).pack(side="left")
        ttk.Button(buttons, text="关闭窗口并保留连接", command=self.keep_in_background).pack(side="left", padx=6)
        ttk.Button(buttons, text="取消", command=self.dismiss_prompt).pack(side="left")
        prompt.protocol("WM_DELETE_WINDOW", self.dismiss_prompt)
        prompt.bind("<Escape>", lambda event: self.dismiss_prompt())
        prompt.geometry(f"+{max(0, self.root.winfo_rootx()+24)}+{max(0, self.root.winfo_rooty()+55)}")
        prompt.grab_set()

    def request_cancel(self):
        if self.keep:
            self.keep_in_background()
            return
        self.cancel.set()
        self.phase.set("正在取消，等待当前连接结束")

    def start(self,login=False):
        if self.active or self.exiting:
            return
        self.cancel.clear()
        self.active=True
        self.retry.config(state="disabled")
        self.login.config(state="disabled")
        self.stop.config(state="normal",text="取消")
        self.phase.set("正在验证")
        threading.Thread(target=self.run,args=(login,),daemon=False).start()

    def run(self,login):
        result="检测结束"
        try:
            if agy_desktop.is_running():
                self.emit("Antigravity 仍在运行。可先做独立检测；重启客户端前请保存工作并正常退出。")
            agy_desktop.app_path()
            agy_cli_probe.cli_path()
            workspace = HOME / "检测工作区"
            workspace.mkdir(exist_ok=True)
            if login:
                self.interactive_login(workspace)
            self.login_candidate=None
            self.test_candidates(workspace)
            result="客户端已退出，辅助代理已清理"
        except CheckFailed as e:
            result={"auth":"需要登录","quota":"模型额度受限","eligibility":"账号资格受限",
                    "region":"服务地区受限","busy":"等待客户端退出","cancel":"已取消"}.get(e.status,"未通过验证")
            self.emit(str(e))
        except FileNotFoundError:
            result="缺少运行组件"
            self.emit("未找到正式版 Antigravity 客户端、官方 CLI 或所需代理程序，请检查安装位置。")
        except Exception as e:
            result="启动失败"
            self.emit(f"检测中断（{type(e).__name__}）。请检查本地代理工具的运行状态。")
        finally:
            self.emit(result)
            self.events.put(("done",result))

    def interactive_login(self,workspace):
        if not self.login_candidate:
            raise CheckFailed("error","请先检测一次。")
        with agy_routes.open_route(self.login_candidate,self.cancel,self.emit) as url:
            env=os.environ.copy()
            env.update(HTTP_PROXY=url,HTTPS_PROXY=url,NO_PROXY="localhost,127.0.0.1,::1",
                       http_proxy=url,https_proxy=url,no_proxy="localhost,127.0.0.1,::1")
            self.emit("请在官方 CLI 窗口自行登录，完成后正常退出 CLI。本窗口随后自动复测。")
            self.keep=True
            self.events.put(("phase","等待你完成官方 CLI 登录"))
            try:
                p=subprocess.Popen([agy_cli_probe.cli_path()],cwd=workspace,env=env,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE)
                while p.poll() is None:
                    if self.cancel.wait(0.2):
                        agy_cli_probe._stop_test(p)
                        raise CheckFailed("cancel","已退出本次 CLI 登录。")
            finally:
                self.keep=False

    def test_candidates(self,workspace):
        options=agy_routes.candidates(previous_name(),self.emit)
        if not options:
            raise CheckFailed("network","没有可验证的本机代理候选，请启动代理客户端后重试。")
        items=options[:MAX_ATTEMPTS]
        region_failures = 0
        for i,candidate in enumerate(items,1):
            if self.cancel.is_set():
                raise CheckFailed("cancel","已取消，未启动客户端。")
            self.emit(f"[{i}/{len(items)}] {candidate.name}")
            with ExitStack() as attempt:
                try:
                    upstream = attempt.enter_context(agy_routes.open_route(candidate,self.cancel,self.emit))
                except InterruptedError:
                    raise CheckFailed("cancel","已取消。") from None
                except OSError as e:
                    self.emit(f"无法初始化候选代理（{type(e).__name__}）。")
                    continue
                with Relay(upstream,self.emit) as relay:
                    try:
                        self.verify_and_launch(candidate,relay,workspace)
                        return
                    except CheckFailed as e:
                        if e.status not in ("network", "region"):
                            raise
                        if e.status == "region":
                            region_failures += 1
                            self.emit("Google 拒绝了本线路的模型请求（地区限制）。")
                        else:
                            self.emit(str(e))
        if self.cancel.is_set():
            raise CheckFailed("cancel","已取消。")
        if region_failures:
            raise CheckFailed("region", f"本轮 {len(items)} 条线路均未通过，其中 {region_failures} 条收到地区限制。")
        raise CheckFailed("network",f"本轮 {len(items)} 个候选均未通过，客户端未启动。")

    def verify_and_launch(self,candidate,relay,workspace):
        self.emit("1/3 正在访问 Google 连通性检测。")
        google_check(relay.url,False,self.cancel)
        self.emit("Google 已返回 204。")
        def show_terms_chain(host):
            upstream = f"http://{relay.upstream[0]}:{relay.upstream[1]}"
            for row in agy_routes.route_evidence(upstream):
                if row["host"] == host and candidate.name in row["chains"]:
                    self.emit(f"实际连接：{host} → {candidate.name}（Mihomo 已确认）。")
                    return
        country=google_check(relay.url,True,self.cancel,show_terms_chain)
        self.emit("2/3 Google 默认展示的条款版本（未指定国家）："+country+"。Gemini 可用性由下一步的真实回复判定。")
        if self.cancel.is_set():
            raise CheckFailed("cancel","已取消。")
        self.emit("3/3 验证官方 CLI 的模型回复和连接路径。")
        status,detail=agy_cli_probe.probe(relay.url,workspace,self.cancel,self.emit)
        if status!="ok":
            if status=="auth":
                self.login_candidate=candidate
            raise CheckFailed(status,detail)
        if not relay.model_path_seen():
            raise CheckFailed("error","CLI 返回正确，但未观察到 CLI 后端经专用端口连接模型服务，暂停启动。")
        self.emit(f"Gemini（{agy_cli_probe.GEMINI_MODEL}）已回复 AGY_OK，已观察到 CLI 的后端代理连接。")
        if self.cancel.is_set():
            raise CheckFailed("cancel","已取消，未启动客户端。")
        if agy_desktop.is_running():
            raise CheckFailed("busy","检测期间客户端已被打开。请保存并退出后重新检测。")
        proc=agy_desktop.launch_app(relay.url)
        self.keep=True
        try:
            try:
                remember(candidate.name)
            except OSError:
                self.emit("本轮验证已通过，但无法保存成功节点。")
            self.events.put(("phase","客户端已启动，正在监测后台连接"))
            self.emit("请在客户端发送消息。客户端正常退出后会释放本次私有代理。")
            agy_desktop.monitor_app(proc,relay.url,self.emit,self.cancel)
            if self.cancel.is_set():
                raise CheckFailed("cancel","按要求断开本次专用代理，正在退出启动器。")
        finally:
            self.keep=False


def main():
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateMutexW.argtypes=(ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p)
    kernel.CreateMutexW.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=(ctypes.c_void_p,)
    ctypes.set_last_error(0)
    lock=kernel.CreateMutexW(None,False,"Local\\AntigravityProxyLauncherV1")
    if not lock or ctypes.get_last_error()==183:
        user = ctypes.windll.user32
        user.FindWindowW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        user.FindWindowW.restype = ctypes.c_void_p
        hwnd = user.FindWindowW("TkTopLevel", "Antigravity 代理检测启动器")
        if hwnd:
            user.ShowWindow(ctypes.c_void_p(hwnd), 9)
            user.SetForegroundWindow(ctypes.c_void_p(hwnd))
        else:
            user.MessageBoxW(None, "启动器窗口暂不可用，请查看任务栏。", "Antigravity 启动器", 64)
        if lock:
            kernel.CloseHandle(lock)
        return
    try:
        root=tk.Tk()
        Launcher(root)
        root.mainloop()
    finally:
        kernel.CloseHandle(lock)


if __name__=="__main__":
    main()
