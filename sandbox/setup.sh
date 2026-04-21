#!/usr/bin/env bash
# Pipeline Orchestrator V3 — WSL 內的沙盒安裝腳本
#
# 由 setup_sandbox.bat 從 Windows 呼叫進來，在 WSL Ubuntu 內執行。
# 做三件事：
#   1. 如果沒有 Docker Engine 就裝
#   2. build 沙盒映像檔（如果尚未存在）
#   3. 啟動長駐容器 pipeline-sandbox（bind mount 專案根目錄）
#
# 之後 backend 會透過 `wsl docker exec pipeline-sandbox ...` 執行 skill 程式碼。
set -euo pipefail

# ── 參數：專案根目錄（WSL 格式路徑，例如 /mnt/c/Users/Foo/pipeline-orchestratorV3）
PROJECT_DIR="${1:-}"
if [[ -z "$PROJECT_DIR" ]]; then
    echo "用法：$0 <project_dir_in_wsl>"
    echo "範例：$0 /mnt/c/Users/GU605_PR_MZ/pipeline-orchestratorV3"
    exit 1
fi
if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "✗ 找不到專案目錄：$PROJECT_DIR"
    exit 1
fi

CONTAINER="pipeline-sandbox"
IMAGE="pipeline-sandbox:latest"

echo "══════════════════════════════════════════════════════"
echo "Pipeline Orchestrator V3 — 沙盒安裝"
echo "══════════════════════════════════════════════════════"
echo "專案目錄：$PROJECT_DIR"
echo ""

# ── Docker CLI 前綴偵測：優先跑 plain docker；失敗才用 sudo
# 已加入 docker group 的使用者（usermod -aG docker）重啟 WSL 後就免 sudo
if docker info &>/dev/null; then
    DOCKER="docker"
    echo "✓ docker 免 sudo 可用"
else
    DOCKER="sudo docker"
    echo "ℹ docker 需要 sudo（尚未加入 docker group 或 WSL 還沒重啟）"
fi
echo ""

# ── 1. 確認 / 安裝 Docker Engine
if ! command -v docker &>/dev/null; then
    echo "==> Docker 未安裝，開始自動安裝（~2-3 分鐘）..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    echo "✓ Docker 已安裝"
    echo "  ⚠ 已把目前使用者加進 docker group，WSL 重啟後免 sudo 可用 docker"
else
    echo "✓ Docker 已存在：$(docker --version)"
fi

# ── 2. 啟動 Docker daemon（WSL 內 systemd 未預設啟動時需手動）
if ! sudo service docker status &>/dev/null; then
    echo "==> 啟動 Docker daemon..."
    sudo service docker start
fi

# ── 3. Build 沙盒映像檔（若尚未存在 or Dockerfile 有變動就 rebuild）
if [[ "$($DOCKER images -q $IMAGE 2>/dev/null)" == "" ]]; then
    echo "==> Build 沙盒映像檔 $IMAGE（首次約 3-5 分鐘，之後會 cache）..."
    $DOCKER build -t "$IMAGE" "$PROJECT_DIR/sandbox"
    echo "✓ 映像檔已建立"
else
    echo "✓ 映像檔已存在：$IMAGE"
    echo "  （如果修改了 Dockerfile / requirements.txt，跑 '$DOCKER build -t $IMAGE $PROJECT_DIR/sandbox' 重建）"
fi

# ── 4. 啟動 / 重建容器
if $DOCKER ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    # 已存在 → 確認是否 running
    if $DOCKER ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        echo "✓ 容器 $CONTAINER 已經在跑"
    else
        echo "==> 容器 $CONTAINER 存在但已停止，啟動中..."
        $DOCKER start "$CONTAINER"
    fi
else
    echo "==> 建立並啟動容器 $CONTAINER..."
    # ── Bind mount 策略 ──────────────────────────────────────────
    # 需要讓容器看到三類檔案（都用「同路徑映射」，不翻譯路徑）：
    #   (1) 專案本體：$PROJECT_DIR（讓使用者工作流產出存 ai_output/ 時兩邊同步）
    #   (2) Agent Skills：$USER_HOME_WSL/.agents/（skill 掛載時 LLM 呼叫 scripts/）
    #   (3) 容器內的 $HOME 也指到同一份 .agents，這樣 Path.home() / ".agents"
    #       在容器跟 Windows 都指向同一個地方
    # 找出 Windows 使用者 home 對應的 WSL 路徑（/mnt/c/Users/XXX）
    WIN_USER=$(echo "$PROJECT_DIR" | sed -n 's|^/mnt/\([a-z]\)/Users/\([^/]*\)/.*|\2|p')
    DRIVE_LETTER=$(echo "$PROJECT_DIR" | sed -n 's|^/mnt/\([a-z]\)/.*|\1|p')
    if [[ -n "$WIN_USER" && -n "$DRIVE_LETTER" ]]; then
        USER_HOME_WSL="/mnt/$DRIVE_LETTER/Users/$WIN_USER"
    else
        # 專案不在 /mnt/c/Users/... 下（例如放在 D:\ 或其他位置）
        # → 仍讓 ~/.agents 有 fallback，指到 Windows 預設 C:\Users\<current>\.agents
        USER_HOME_WSL="/mnt/c/Users/$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r')"
        echo "  ⚠ 專案不在 C:\\Users\\... 下，.agents 掛載將嘗試：$USER_HOME_WSL"
    fi
    AGENTS_DIR="$USER_HOME_WSL/.agents"

    # 若 .agents 尚未建立（使用者還沒裝任何 skill）就建空資料夾避免 mount 失敗
    if [[ ! -d "$AGENTS_DIR" ]]; then
        echo "  ℹ .agents 資料夾尚未存在，建立空白目錄：$AGENTS_DIR"
        mkdir -p "$AGENTS_DIR/skills"
    fi

    $DOCKER run -d \
        --name "$CONTAINER" \
        --restart unless-stopped \
        -v "$PROJECT_DIR:$PROJECT_DIR" \
        -v "$AGENTS_DIR:$AGENTS_DIR" \
        -v "$AGENTS_DIR:/root/.agents" \
        -w "$PROJECT_DIR" \
        "$IMAGE"
    echo "✓ 容器已啟動，掛載："
    echo "    $PROJECT_DIR → $PROJECT_DIR（專案本體）"
    echo "    $AGENTS_DIR → $AGENTS_DIR（Agent Skills，絕對路徑相容）"
    echo "    $AGENTS_DIR → /root/.agents（容器內 ~/.agents 相容）"
fi

# ── 5. 冒煙測試
echo ""
echo "==> 冒煙測試："
if $DOCKER exec "$CONTAINER" python -c "import pandas, openpyxl, numpy, requests; print('✓ 核心套件載入 OK')"; then
    echo ""
    echo "══════════════════════════════════════════════════════"
    echo "✓ 沙盒就緒！"
    echo "  容器名：$CONTAINER"
    echo "  掛載：$HOST_HOME_WSL → $HOST_HOME_WSL"
    echo "══════════════════════════════════════════════════════"
else
    echo "✗ 冒煙測試失敗，請檢查上面訊息"
    exit 1
fi
