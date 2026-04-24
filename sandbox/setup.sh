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
#
# 用法：
#   setup.sh <project_dir_in_wsl>              # 一般安裝（跳過已存在的 image / container）
#   setup.sh <project_dir_in_wsl> --rebuild    # 強制 rebuild image + 重建 container
#                                              # （改了 Dockerfile / requirements.txt 後用）
set -euo pipefail

# ── 參數：專案根目錄 + 可選 --rebuild 旗標
PROJECT_DIR="${1:-}"
REBUILD="no"
for arg in "${@:2}"; do
    case "$arg" in
        --rebuild|-r) REBUILD="yes" ;;
    esac
done
if [[ -z "$PROJECT_DIR" ]]; then
    echo "用法：$0 <project_dir_in_wsl> [--rebuild]"
    echo "範例：$0 /mnt/c/Users/GU605_PR_MZ/pipeline-orchestratorV3"
    echo "改了 Dockerfile / requirements.txt 要重裝：$0 ... --rebuild"
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

# ── 3. Build 沙盒映像檔
# --rebuild：強制砍掉舊 image + 舊 container，重裝（改 Dockerfile / requirements.txt 後用）
# 沒 --rebuild：沒 image 才 build；有就跳過（fresh clone 會 build；重跑不浪費時間）
if [[ "$REBUILD" == "yes" ]]; then
    echo "==> 強制 rebuild（--rebuild）：先移除舊 container + image..."
    $DOCKER rm -f "$CONTAINER" 2>/dev/null || true
    $DOCKER rmi -f "$IMAGE" 2>/dev/null || true
    echo "==> 重建映像檔 $IMAGE（約 5-10 分鐘，含 Node.js + 所有 pip 套件）..."
    $DOCKER build --no-cache -t "$IMAGE" "$PROJECT_DIR/sandbox"
    echo "✓ 映像檔已 rebuild"
elif [[ "$($DOCKER images -q $IMAGE 2>/dev/null)" == "" ]]; then
    echo "==> Build 沙盒映像檔 $IMAGE（首次約 5-10 分鐘）..."
    $DOCKER build -t "$IMAGE" "$PROJECT_DIR/sandbox"
    echo "✓ 映像檔已建立"
else
    echo "✓ 映像檔已存在：$IMAGE"
    echo "  （改了 Dockerfile / requirements.txt 要生效，加 --rebuild 重跑本腳本）"
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
echo "==> 冒煙測試 — 核心套件："
if ! $DOCKER exec "$CONTAINER" python -c "import pandas, openpyxl, numpy, requests; print('  ✓ Tier 1-2 OK')"; then
    echo "✗ 核心套件測試失敗"
    exit 1
fi

echo "==> 冒煙測試 — 進階套件（Tier 4-5）："
$DOCKER exec "$CONTAINER" python -c "
missing = []
for name in ['pptx', 'pdfplumber', 'newspaper', 'cloudscraper', 'feedparser', 'fake_useragent']:
    try:
        __import__(name)
    except Exception as e:
        missing.append(f'{name} ({e.__class__.__name__})')
if missing:
    print('  ⚠ 缺少：', ', '.join(missing))
    print('    解法：setup_sandbox.bat --rebuild 重建')
else:
    print('  ✓ python-pptx / pdfplumber / newspaper3k / cloudscraper / feedparser / fake_useragent 全部 OK')
" || true

echo "==> 冒煙測試 — Node.js + pptxgenjs："
if $DOCKER exec "$CONTAINER" bash -c 'node --version && npm list -g --depth=0 2>/dev/null | grep pptxgenjs' >/dev/null 2>&1; then
    NODE_VER=$($DOCKER exec "$CONTAINER" node --version 2>/dev/null)
    echo "  ✓ Node.js $NODE_VER + pptxgenjs OK"
else
    echo "  ⚠ Node.js 或 pptxgenjs 未安裝（解法：setup_sandbox.bat --rebuild）"
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo "✓ 沙盒就緒！"
echo "  容器名：$CONTAINER"
$DOCKER inspect "$CONTAINER" --format '{{range .Mounts}}    {{.Source}} → {{.Destination}}{{"\n"}}{{end}}' 2>/dev/null || true
echo "══════════════════════════════════════════════════════"
