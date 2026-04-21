# Pipeline Orchestrator V3 — Skill 執行沙盒
#
# 用途：LLM 生成的 Python / Shell 程式碼在此容器內執行，避免直接碰 Windows host。
# 原則：只裝「純運算 / I/O」類套件。桌面自動化（pyautogui / mss / pynput）走不了
# 容器（沒有 display），保留在 host 上跑。
#
# 分層策略：故意把 pip install 拆成多個 RUN，每層獨立釋放記憶體，避免在
# WSL2 預設 8GB 下一次壓縮 20 個套件導致 buildkit OOM（SIGBUS）。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ── System：locale + ca-certificates + curl ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        locales ca-certificates curl \
    && sed -i '/zh_TW.UTF-8/s/^# //g; /en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*
ENV LANG=zh_TW.UTF-8 LC_ALL=zh_TW.UTF-8

# ── Tier 1：核心數值/文件處理（小而快）──
RUN pip install --no-cache-dir \
    pandas openpyxl numpy python-dateutil pytz defusedxml Pillow python-docx

# ── Tier 2：HTTP/解析（中量）──
RUN pip install --no-cache-dir \
    requests beautifulsoup4 lxml "httpx[http2]"

# ── Tier 3：繪圖/影像處理（重，單獨一層避免與 Tier 1/2 同時壓縮）──
RUN pip install --no-cache-dir \
    matplotlib opencv-python-headless

WORKDIR /project

# tail -f /dev/null：讓容器保持 running 等 exec 指令
CMD ["tail", "-f", "/dev/null"]
