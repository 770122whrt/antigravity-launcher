"""为解压后的启动器创建独立 Python 环境和当前用户桌面入口。"""
import base64
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def create_shortcut(pythonw, desktop=None):
    env = os.environ.copy()
    env["AGY_SETUP_ROOT"] = str(ROOT)
    env["AGY_SETUP_PYTHON"] = str(pythonw)
    env["AGY_SETUP_DESKTOP"] = str(desktop) if desktop is not None else ""
    script = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$desktop = $env:AGY_SETUP_DESKTOP
if (!$desktop) { $desktop = [Environment]::GetFolderPath('Desktop') }
$linkPath = Join-Path $desktop 'Antigravity 代理检测启动器.lnk'
$scriptPath = Join-Path $env:AGY_SETUP_ROOT 'launcher.pyw'
$arguments = '"' + $scriptPath + '"'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
if ((Test-Path -LiteralPath $linkPath) -and
    ($link.TargetPath -ne $env:AGY_SETUP_PYTHON -or $link.Arguments -ne $arguments)) {
    throw '桌面已有指向其他安装目录的同名快捷方式。请先重命名旧快捷方式，再运行安装。'
}
$link.TargetPath = $env:AGY_SETUP_PYTHON
$link.Arguments = $arguments
$link.WorkingDirectory = $env:AGY_SETUP_ROOT
$link.Description = '验证代理和 Gemini 回复后启动 Antigravity'
$link.IconLocation = $env:AGY_SETUP_PYTHON + ',0'
$link.Save()
$verified = $shell.CreateShortcut($linkPath)
if ($verified.TargetPath -ne $env:AGY_SETUP_PYTHON -or $verified.Arguments -ne $arguments) {
    throw '快捷方式回读校验失败。'
}
'''
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                   env=env, check=True, timeout=25)


def main():
    if os.name != "nt" or not (3, 10) <= sys.version_info[:2] <= (3, 12):
        raise RuntimeError("本包请使用 Windows 上的 Python 3.10、3.11 或 3.12。")
    import tkinter
    window = tkinter.Tk()
    window.withdraw()
    window.destroy()
    environment = ROOT / ".venv"
    python = environment / "Scripts/python.exe"
    pythonw = environment / "Scripts/pythonw.exe"
    if not python.is_file():
        print("正在包目录下创建独立 Python 环境…", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    print("正在安装固定版本的 psutil 依赖，需要联网…", flush=True)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "--retries", "1", "--timeout", "20", "-r", str(ROOT / "requirements.txt")],
                   check=True, timeout=180)
    subprocess.run([str(python), "-c", "import tkinter, psutil"], check=True, timeout=15)
    if not pythonw.is_file():
        raise FileNotFoundError("独立环境缺少 pythonw.exe。请检查 Python 安装。")
    create_shortcut(pythonw)
    print("安装完成。请保留此文件夹，从桌面打开 Antigravity 代理检测启动器。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"安装未完成：{exc}", file=sys.stderr)
        sys.exit(1)
