# Pipeline Orchestrator V3 — Skill 執行沙盒
#
# 用途：LLM 生成的 Python / Shell 程式碼在此容器內執行，避免直接碰 Windows host。
# 原則：只裝「純運算 / I/O」類套件。桌面自動化（pyautogui / mss / pynput）走不了
# 容器（沒有 display），保留在 host 上跑。
#
# 映像大小目標：<1GB。套件清單對齊 backend/skill_packages.txt 的可用部分。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Debian 語系包：讓 subprocess 的 locale 能正確處理中文檔名/內容
RUN apt-get update && apt-get install -y --no-install-recommends \
        locales ca-certificates curl \
    && sed -i '/zh_TW.UTF-8/s/^# //g; /en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*
ENV LANG=zh_TW.UTF-8 LC_ALL=zh_TW.UTF-8

# 預裝 skill_packages.txt 中可跨平台的套件（排除 pywin32 + 桌面自動化）
# 如果要加新套件，改 requirements.txt 後重 build image 即可
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# 長駐容器用的掛載目錄。backend 會把 project root bind mount 進來。
WORKDIR /project

# tail -f /dev/null：讓容器保持 running 等 exec 指令
CMD ["tail", "-f", "/dev/null"]
