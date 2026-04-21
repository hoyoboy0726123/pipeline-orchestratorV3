# Skill Sandbox (V3)

WSL + Docker Engine 沙盒：讓 LLM 生成的 Python / Shell 程式碼隔離執行，不直接碰 Windows host。

## 為什麼要有這個？

V2 的 skill 節點把 LLM 生的 code 直接以 `subprocess.Popen` 在 Windows host 跑，對完全沒經過審核的程式碼只隔著一個 venv。V3 把這層改成送進容器，LLM 亂刪檔、存取敏感資料都被擋在沙盒內。

沒有 Docker Desktop、沒有付費授權 — 只靠 Windows 內建的 WSL2 + 在 WSL 內裝 Docker Engine（開源免費）。

## 架構

```
Windows host
  └─ FastAPI backend (8001)
        └─ wsl docker exec pipeline-sandbox python /tmp/xxx.py
              │
              └→ WSL2 Ubuntu
                   └─ Docker Engine
                        └─ pipeline-sandbox 容器（長駐）
                             - Python 3.13
                             - 預裝 pandas / openpyxl / numpy / requests / ...
                             - Bind mounts（三條，路徑全部 1:1 映射，容器內外同路徑）：
                               • 專案本體：C:\...\pipeline-orchestratorV3
                                        → /mnt/c/.../pipeline-orchestratorV3
                               • Agent Skills：C:\Users\<you>\.agents
                                        → /mnt/c/Users/<you>/.agents
                                        (且容器內 ~/.agents 也指向同一地點)
```

## 一次性安裝

1. 雙擊 `setup_sandbox.bat`
2. 如果提示沒 WSL，按提示在管理員 PowerShell 執行 `wsl --install` 並重啟，然後再跑一次 `setup_sandbox.bat`
3. 完成後可看到 `✓ 沙盒就緒！`

檔案清單：
- `Dockerfile` — 沙盒映像檔定義（Python 3.13 slim + 常用資料套件）
- `requirements.txt` — 預裝套件清單（對齊 backend/skill_packages.txt 可跨平台的部分）
- `setup.sh` — 在 WSL 內實際安裝 Docker、build image、啟動容器
- `setup_sandbox.bat` — Windows 入口，把專案路徑轉成 WSL 格式再呼叫 setup.sh
- `README.md` — 這份文件

## 每日啟動

**不用手動做任何事**。V3 backend 啟動時會：
1. 檢查 WSL 能跑 `wsl docker ps`
2. 檢查 `pipeline-sandbox` 容器是否在跑，若沒在跑會試著 `docker start`
3. 前端 Settings 頁會顯示沙盒狀態（綠燈=就緒、紅燈=有問題）

如果沙盒壞掉或關閉，skill 會 fallback 到舊的 host subprocess 模式（有 warning log）。

## 手動管理

偶爾會想手動操作容器：

```bash
# 進 WSL
wsl

# 看容器狀態
sudo docker ps -a | grep pipeline-sandbox

# 手動啟動/停止/重啟
sudo docker start pipeline-sandbox
sudo docker stop pipeline-sandbox
sudo docker restart pipeline-sandbox

# 進容器看看
sudo docker exec -it pipeline-sandbox bash

# 加新 Python 套件（臨時）
sudo docker exec pipeline-sandbox pip install <package>

# 修改 requirements.txt 後永久加：重 build image
sudo docker build -t pipeline-sandbox:latest /mnt/c/Users/<you>/pipeline-orchestratorV3/sandbox
sudo docker rm -f pipeline-sandbox
# 然後再跑 setup_sandbox.bat 讓容器重新起來
```

## 限制

- **computer_use 節點完全不走沙盒** — pyautogui / pynput / mss 需要 host 桌面權限，永遠在 Windows 原生跑
- **Script 節點預設也不走沙盒** — 使用者自己寫的腳本通常需要訪問他們 Windows 上的特定環境
- 只有 **Skill 節點** 走沙盒（LLM 產生的不信任程式碼）
- 路徑翻譯：LLM 程式碼若用 Windows 絕對路徑 `C:\Users\...`，backend 會自動轉成 Linux `/mnt/c/Users/...`（映射同一份檔案）
- Agent Skills：`~/.agents/skills/<skill_name>/` 在容器內也能用 `Path.home() / ".agents" / "skills" / ...` 讀到（`/root/.agents` 指向 host 的 `.agents`）

## 如果 build 撞到 SIGBUS / OOM

Docker build 壓縮 numpy / matplotlib / opencv 等大套件時，如果 WSL 的記憶體或磁碟不夠會直接 daemon crash：

**先檢查磁碟**：C: 殘餘要至少 **8 GB**（Docker image + swap.vhdx 會佔空間）。低於這量會各種莫名其妙失敗。
```powershell
Get-PSDrive C
```

**記憶體不足**：手動建 `C:\Users\<你>\.wslconfig`：
```ini
[wsl2]
memory=8GB
swap=16GB
```
然後 `wsl --shutdown`，下次啟動 WSL 吃新設定。

Dockerfile 已經把 pip install 拆成多層（Tier 1 核心 / Tier 2 HTTP / Tier 3a matplotlib / Tier 3b opencv），單層 commit 壓力較小。預設 WSL 記憶體通常夠用，不用先動 `.wslconfig`。

## 疑難排解

**Build 卡在 `pip install` 然後 Docker daemon crash（SIGBUS / bus error）**
→ WSL 記憶體不夠。先套上面的 `.wslconfig`、`wsl --shutdown`、清空舊快取：
```
wsl sudo docker builder prune -af
wsl sudo docker rm -f pipeline-sandbox
```
然後再跑一次 `setup_sandbox.bat`。

Dockerfile 本來就把 pip install 拆成多個 RUN 分層安裝，每層獨立 OOM-safe。若仍然爆記憶體，把 Tier 3（matplotlib / opencv）那段 RUN 改分成兩行。


**`setup_sandbox.bat` 卡在 sudo 密碼**
→ WSL Ubuntu 的密碼。忘了的話在 WSL 內 `sudo passwd $USER` 重設（需輸入目前密碼）。完全忘了可用 admin PowerShell `wsl --user root passwd <your_username>`。

**Docker 安裝後 `docker ps` 說權限不足**
→ WSL 重啟：`wsl --shutdown` 然後重開 WSL。若仍然不行，手動 `sudo docker ps` 可繞過。

**容器啟動失敗說 port 已使用**
→ 容器不需要對外開 port（純內部 exec），若真撞到可能是舊容器殘留：`sudo docker rm -f pipeline-sandbox`。

**Build 很久**
→ 第一次 build 會從 Docker Hub 拉 python:3.13-slim 基底映像（~150MB）再 pip install，首次約 3-5 分鐘，之後 cache 會快很多。
