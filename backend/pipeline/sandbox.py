"""
Skill 沙盒執行層（V3）：把 LLM 生成的 Python / Shell 送進 WSL 內的 Docker
容器 `pipeline-sandbox` 執行，隔離 Windows host。

此模組只負責：
  1. 狀態檢查 — WSL / Docker / 容器 是否就緒
  2. 自動復活 — 容器停了試著 start
  3. 路徑翻譯 — Windows `C:\\...` → WSL/容器內 `/mnt/c/...`（同路徑映射）
  4. 執行 + I/O 捕捉 + timeout + 可中止

不負責：
  - 建立容器（`sandbox/setup.sh` 負責，一次性）
  - 決定要不要用沙盒（`executor._execute_skill_tool` 根據 settings 判斷）

使用方式見 `pipeline/executor.py` 的沙盒分支（Stage 3）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── 常數 ───────────────────────────────────────────────────────────
CONTAINER_NAME = "pipeline-sandbox"
SANDBOX_TOOL_TIMEOUT = 60  # 秒；對齊 executor.SKILL_TOOL_TIMEOUT

# Docker CLI 呼叫習慣：優先嘗試 plain `docker`（使用者已 `usermod -aG docker` + 重啟 WSL），
# 失敗再 fallback `sudo docker`。快取結果避免每次都試。
_DOCKER_PREFIX_CACHE: dict = {"prefix": None}
_DOCKER_PREFIX_LOCK = threading.Lock()


# ── 路徑翻譯 ───────────────────────────────────────────────────────
_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def windows_to_wsl_path(path: str) -> str:
    """`C:\\Users\\X\\y` → `/mnt/c/Users/X/y`；
    已是 POSIX 或空字串則原樣回傳。"""
    if not path:
        return path
    m = _DRIVE_PATH_RE.match(path)
    if not m:
        return path
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def translate_code_paths(code: str) -> str:
    """把 LLM 程式碼裡的 Windows 絕對路徑都換成 WSL/`/mnt/c/...` 形式。
    只針對 r-string 與普通字串裡 `X:\\...` / `X:/...` 的字面值做替換，
    避免誤傷變數名或 URL 等正規字串。"""
    def _sub(m: re.Match) -> str:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\\\", "/").replace("\\", "/")
        return f'"/mnt/{drive}/{rest}"'

    # 匹配 "C:\Users\..." 或 "C:/Users/..." 或 r"C:\..." 的字面值
    pattern = re.compile(r'r?["\']([A-Za-z]):[\\/]([^"\']*)["\']')
    return pattern.sub(_sub, code)


# ── wsl 指令呼叫封裝 ───────────────────────────────────────────────
def _run_wsl(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """執行 `wsl.exe <args>`，回傳 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            ["wsl", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except FileNotFoundError:
        return -1, "", "wsl.exe not found — Windows 可能沒裝 WSL"
    except subprocess.TimeoutExpired:
        return -2, "", f"wsl 指令 timeout（>{timeout}s）"
    except Exception as e:
        return -3, "", f"wsl 呼叫例外：{e}"


def _detect_docker_prefix() -> list[str]:
    """決定呼叫 docker 時要不要加 sudo。
    優先試 plain `docker ps`；失敗試 `sudo docker ps`。結果快取。"""
    with _DOCKER_PREFIX_LOCK:
        if _DOCKER_PREFIX_CACHE["prefix"] is not None:
            return _DOCKER_PREFIX_CACHE["prefix"]

        # 試 plain docker（前提：使用者已加入 docker group 且 WSL 重啟過）
        rc, _, _ = _run_wsl(["-e", "docker", "info"], timeout=5.0)
        if rc == 0:
            _DOCKER_PREFIX_CACHE["prefix"] = ["docker"]
        else:
            # fallback 到 sudo（需使用者在 sudoers NOPASSWD 或系統不會互動詢問）
            _DOCKER_PREFIX_CACHE["prefix"] = ["sudo", "docker"]
        return _DOCKER_PREFIX_CACHE["prefix"]


def _invalidate_docker_prefix_cache() -> None:
    """狀態異常時重新探測（例如使用者剛重啟 WSL）。"""
    with _DOCKER_PREFIX_LOCK:
        _DOCKER_PREFIX_CACHE["prefix"] = None


# ── 狀態檢查 ───────────────────────────────────────────────────────
_STATUS_CACHE: dict = {"ts": 0.0, "data": None}
_STATUS_TTL = 5.0


def check_status(force_refresh: bool = False) -> dict:
    """沙盒健康檢查。Return:
        {
          "wsl_ok": bool, "wsl_hint": str,
          "docker_ok": bool, "docker_version": str,
          "container_exists": bool, "container_running": bool,
          "ready": bool,           # wsl + docker + container 都綠
          "reasons": list[str],    # 使用者可讀的問題描述
          "hint": str,             # 建議下一步動作
        }
    結果快取 5 秒避免 polling 炸 WSL。"""
    now = time.time()
    if not force_refresh and _STATUS_CACHE["data"] and (now - _STATUS_CACHE["ts"] < _STATUS_TTL):
        return _STATUS_CACHE["data"]

    reasons: list[str] = []
    wsl_ok = False
    docker_ok = False
    docker_version = ""
    container_exists = False
    container_running = False

    # 1. WSL 可用嗎
    rc, out, err = _run_wsl(["--status"], timeout=5.0)
    if rc == 0:
        wsl_ok = True
    else:
        reasons.append(f"WSL 無法使用：{err.strip() or '未知'}")

    # 2. Docker daemon 可用嗎
    if wsl_ok:
        docker_prefix = _detect_docker_prefix()
        rc, out, _ = _run_wsl(["-e", *docker_prefix, "--version"], timeout=5.0)
        if rc == 0 and out.strip():
            docker_ok = True
            docker_version = out.strip().split("\n", 1)[0][:100]
        else:
            reasons.append("Docker Engine 未安裝或無法使用 — 請執行 sandbox/setup_sandbox.bat")

    # 3. 容器狀態
    if docker_ok:
        docker_prefix = _detect_docker_prefix()
        # 用 `docker ps -a --filter name=... --format {{.Status}}` — 最簡單
        rc, out, _ = _run_wsl(
            ["-e", *docker_prefix, "ps", "-a",
             "--filter", f"name=^{CONTAINER_NAME}$",
             "--format", "{{.Status}}"],
            timeout=5.0,
        )
        status_line = out.strip()
        if rc == 0 and status_line:
            container_exists = True
            container_running = status_line.lower().startswith("up ")
            if not container_running:
                reasons.append(f"容器 {CONTAINER_NAME} 存在但已停止（狀態：{status_line[:60]}）")
        else:
            reasons.append(f"容器 {CONTAINER_NAME} 不存在 — 請執行 sandbox/setup_sandbox.bat")

    ready = wsl_ok and docker_ok and container_running
    hint = ""
    if not wsl_ok:
        hint = "請先安裝 WSL：開管理員 PowerShell 跑 `wsl --install` 並重啟"
    elif not docker_ok:
        hint = "請跑 sandbox/setup_sandbox.bat 安裝 Docker + 建容器"
    elif not container_exists:
        hint = "請跑 sandbox/setup_sandbox.bat 建立容器"
    elif not container_running:
        hint = "容器已停止 — backend 會嘗試自動啟動，或手動 `wsl sudo docker start pipeline-sandbox`"

    data = {
        "wsl_ok": wsl_ok,
        "docker_ok": docker_ok,
        "docker_version": docker_version,
        "container_exists": container_exists,
        "container_running": container_running,
        "ready": ready,
        "reasons": reasons,
        "hint": hint,
    }
    _STATUS_CACHE["ts"] = now
    _STATUS_CACHE["data"] = data
    return data


def ensure_running() -> tuple[bool, str]:
    """容器若沒跑就試著 start。回傳 (ok, reason)。"""
    status = check_status(force_refresh=True)
    if status["ready"]:
        return True, ""
    if status["container_exists"] and not status["container_running"]:
        log.info(f"[sandbox] 容器 {CONTAINER_NAME} 已停止，嘗試 docker start …")
        docker_prefix = _detect_docker_prefix()
        rc, _, err = _run_wsl(["-e", *docker_prefix, "start", CONTAINER_NAME], timeout=15.0)
        if rc == 0:
            # 再查一次狀態
            status = check_status(force_refresh=True)
            if status["ready"]:
                log.info(f"[sandbox] 容器 {CONTAINER_NAME} 已成功啟動")
                return True, ""
            return False, status["hint"] or "啟動後仍不正常"
        return False, f"容器啟動失敗：{err.strip() or '未知'}"
    return False, status["hint"] or "沙盒未就緒"


# ── 執行 ──────────────────────────────────────────────────────────
@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


def _docker_exec_cmd(workdir_wsl: Optional[str], runner: list[str]) -> list[str]:
    """組 `wsl <docker_prefix> exec [-w ...] pipeline-sandbox <runner...>`"""
    docker_prefix = _detect_docker_prefix()
    cmd = ["wsl", "-e", *docker_prefix, "exec"]
    if workdir_wsl:
        cmd += ["-w", workdir_wsl]
    cmd += [CONTAINER_NAME, *runner]
    return cmd


# 專案根目錄下放 LLM 程式碼的暫存區（已被 bind mount 到容器內同路徑）
_TMP_DIR = Path(__file__).resolve().parent.parent.parent / "sandbox" / "_tmp"


def _write_code_tempfile(code: str, suffix: str = ".py") -> str:
    """把 LLM 程式碼寫到 sandbox/_tmp/ 下，回傳 Windows 路徑。
    這個目錄在 bind mount 的範圍內 → 容器用 /mnt/c/... 能讀到同一份檔。"""
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="skill_", dir=str(_TMP_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(code)
    return tmp_path


def _run_subprocess(
    cmd: list[str],
    timeout: float,
    run_id: str,
    register_cb: Optional[Callable],
    unregister_cb: Optional[Callable],
) -> SandboxResult:
    """執行指令，串 I/O 回來。對齊 executor._skill_run_python 的行為：
    - encoding='utf-8', errors='replace'
    - timeout → kill + 返回 timed_out=True
    - register_cb / unregister_cb 讓 executor 可以中止"""
    proc = None
    try:
        # 沙盒執行時 stdin 關掉，避免子指令等輸入卡死
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if register_cb and run_id:
            try:
                register_cb(run_id, proc)
            except Exception as e:
                log.warning(f"[sandbox] register_cb 失敗：{e}")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return SandboxResult(stdout=stdout, stderr=stderr, returncode=proc.returncode)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate(timeout=3.0)
            except Exception:
                pass
            return SandboxResult(
                stdout="",
                stderr=f"[錯誤] 沙盒執行超時（>{timeout}秒）",
                returncode=-1,
                timed_out=True,
            )
        finally:
            if unregister_cb and run_id and proc is not None:
                try:
                    unregister_cb(run_id, proc)
                except Exception:
                    pass
    except Exception as e:
        return SandboxResult(
            stdout="",
            stderr=f"[錯誤] 沙盒呼叫失敗：{type(e).__name__}：{e}",
            returncode=-1,
        )


def run_python(
    code: str,
    cwd: Optional[str] = None,
    timeout: float = SANDBOX_TOOL_TIMEOUT,
    run_id: str = "",
    register_cb: Optional[Callable] = None,
    unregister_cb: Optional[Callable] = None,
    translate_paths: bool = True,
) -> SandboxResult:
    """在沙盒內執行 Python 程式碼。
    - code: Python 原始碼
    - cwd: 絕對 Windows 或 WSL 路徑（會自動翻譯）
    - translate_paths: 是否把 code 中的 Windows 絕對路徑自動轉 WSL 形式（預設開）"""
    final_code = translate_code_paths(code) if translate_paths else code
    tmp_win = _write_code_tempfile(final_code, suffix=".py")
    try:
        script_wsl = windows_to_wsl_path(tmp_win)
        cwd_wsl = windows_to_wsl_path(cwd) if cwd else None
        cmd = _docker_exec_cmd(cwd_wsl, ["python", script_wsl])
        return _run_subprocess(cmd, timeout, run_id, register_cb, unregister_cb)
    finally:
        try:
            os.unlink(tmp_win)
        except Exception:
            pass


def run_shell(
    cmd_str: str,
    cwd: Optional[str] = None,
    timeout: float = SANDBOX_TOOL_TIMEOUT,
    run_id: str = "",
    register_cb: Optional[Callable] = None,
    unregister_cb: Optional[Callable] = None,
) -> SandboxResult:
    """在沙盒內執行 shell 命令（透過 sh -c）。"""
    cwd_wsl = windows_to_wsl_path(cwd) if cwd else None
    cmd = _docker_exec_cmd(cwd_wsl, ["sh", "-c", cmd_str])
    return _run_subprocess(cmd, timeout, run_id, register_cb, unregister_cb)
