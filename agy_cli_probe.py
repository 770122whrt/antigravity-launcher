"""使用已安装的 Google Antigravity CLI 发起独立、极短的真实对话。"""

import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit


GEMINI_MODEL = os.environ.get("AGY_TEST_MODEL") or "gemini-3.8-flash-low"


def cli_path() -> str:
    """官方默认 Windows 安装位置，避免命中同名 IDE 别名。"""
    override = os.environ.get("AGY_CLI")
    return str(Path(override)) if override else str(Path(os.environ["LOCALAPPDATA"]) / "agy" / "bin" / "agy.exe")


def _failure(message):
    # Only fixed messages are shown. Raw diagnostics can include account information.
    text = str(message).lower()
    if re.search(r"no capacity available|capacity exhausted|model.{0,45}overloaded", text):
        return "capacity", "Google 报告此模型当前没有可用容量。"
    if re.search(r"quota|resource[_ ]exhausted|rate.?limit|too many requests|\b429\b|out of credits", text):
        return "quota", "服务报告额度或限流。请待恢复后再试。"
    if re.search(r"invalid_grant|unauthenticated|authentication (?:is )?required|not (?:logged|signed) in|not authenticated|sign.?in required|please (?:log|sign) in|must (?:log|sign) in|no (?:valid |cached )?credentials|credentials (?:are )?(?:missing|expired)|token (?:has )?expired|\b401\b", text):
        return "auth", "CLI 需要登录或重新授权。请在本机交互式 agy 中自行登录。"
    if re.search(r"country|region|geograph|user location|your location", text) and re.search(r"not (?:yet )?(?:available|supported|allowed|eligible)|unsupported|restricted|unavailable|blocked|ineligible", text):
        return "region", "Google 服务明确返回地区限制。请核实账号关联地区及服务可用范围。"
    if re.search(r"not eligible|ineligible|not (?:authorized|enabled) for (?:antigravity|this (?:account|service))|organization.{0,40}(?:disabled|restricted)|administrator.{0,40}(?:disabled|enable|provision)|age (?:requirement|restriction|verification)|not provisioned|license required", text):
        return "eligibility", "服务报告账号资格、年龄、组织授权或许可证限制。"
    if re.search(r"untrusted workspace|trust (?:this|the) (?:workspace|directory|folder)|workspace trust|not (?:a )?trusted (?:workspace|directory)", text):
        return "error", "CLI 要求确认检测目录的信任状态。请在此空目录中交互式启动 agy 并检查提示。"
    if re.search(r"certificate|certification path|tls verification|unknown authority|x509:", text):
        return "error", "CLI 的 TLS 证书验证失败。请检查系统时间和受信任证书。"
    if re.search(r"proxyconnect|connection refused|connection reset|connection aborted|network is unreachable|no route to host|i/o timeout|connect(?:ion)? timed out|dial tcp|no such host|tls handshake timeout|failed to connect|could not resolve host|name resolution", text):
        return "network", "CLI 报告连接、DNS、代理或传输超时错误。"
    if re.search(r"permission denied|permission_denied|\b403\b", text):
        return "error", "服务拒绝访问，未明确区分地区、账号或权限。已停止尝试。"
    if re.search(r"timed? ?out|timeout|deadline exceeded", text):
        return "error", "模型请求未在限定时间内完成，无法确认是节点还是服务响应慢。"
    return "error", "CLI 未返回可验证的模型回复或明确错误类别。已停止尝试。"


def _stop_test(process):
    """Only terminate the probe that this call spawned and its children."""
    if process.poll() is None:
        killer = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(killer), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def proxy_env(proxy_url):
    env = os.environ.copy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "grpc_proxy"):
        env[name] = proxy_url
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


def probe(proxy_url, workspace, cancel, emit, *, model=GEMINI_MODEL):
    """Return (status, safe Chinese detail); the caller owns workspace and proxy lifetime."""
    if cancel.is_set():
        return "cancel", "已取消。"
    endpoint = urlsplit(proxy_url)
    if endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "::1", "localhost") or endpoint.username is not None:
        return "error", "检测请求需要本机 HTTP 代理入口。"
    executable = cli_path()
    if not Path(executable).is_file():
        return "error", "未找到当前用户目录中的官方 Antigravity CLI。"
    if not Path(workspace).is_dir():
        return "error", "找不到启动器专用检测工作目录。"

    env = proxy_env(proxy_url)
    args = [
        executable, "--model", model, "--print", "Without using tools, reply with exactly AGY_OK.",
        "--output-format", "json", "--disable-slash-commands", "--print-timeout", "45s",
        "--log-file", "NUL",
    ]
    emit(f"正在请求 Gemini（{model}），会消耗少量模型额度。")
    try:
        p = subprocess.Popen(
            args, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return "error", f"无法启动官方 CLI（系统错误 {exc.winerror or exc.errno}）。"
    try:
        deadline = time.monotonic() + 65
        while True:
            if cancel.is_set():
                return "cancel", "已取消本次模型检测。"
            if time.monotonic() >= deadline:
                return "error", "CLI 超过总时限仍未完成。已终止本次检测，原因未确定。"
            try:
                stdout, stderr = p.communicate(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            result = json.loads(stdout.decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            result = None
        if isinstance(result, dict):
            if p.returncode == 0 and result.get("status") == "SUCCESS" and str(result.get("response")).strip() == "AGY_OK":
                return "ok", "官方 Antigravity CLI 已收到真实模型回复 AGY_OK。"
            if result.get("status") == "SUCCESS" and p.returncode == 0:
                return "error", "模型有回复，但内容不符合 AGY_OK 检测要求。"
            reason = str(result.get("error") or "")
        else:
            reason = stdout.decode("utf-8", errors="replace")
        return _failure(reason + "\n" + stderr.decode("utf-8", errors="replace"))
    finally:
        try:
            _stop_test(p)
        finally:
            p.stdout.close()
            p.stderr.close()
