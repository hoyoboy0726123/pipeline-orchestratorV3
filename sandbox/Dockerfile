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

# ── Tier 3a：繪圖（matplotlib + deps）──
# 單獨一層，讓 layer commit 的資料量不會跟 Tier 3b 疊加
RUN pip install --no-cache-dir matplotlib

# ── Tier 3b：影像處理（opencv headless，最大的單一套件）──
RUN pip install --no-cache-dir opencv-python-headless

# ── Tier 4：文件產出（PPT / PDF）──
# python-pptx：Python 原生產 PPT；pdfplumber：PDF 文字抽取
RUN pip install --no-cache-dir python-pptx pdfplumber

# ── Tier 5：爬蟲進階（新聞 + Cloudflare 繞過 + RSS + UA rotation）──
# newspaper3k 會拉 nltk / tldextract / feedparser（~100MB）；cloudscraper 繞 CF 5s 挑戰
# lxml_html_clean：lxml 5.x 後 html.clean 被拆成獨立套件，newspaper3k 需要
# fake_useragent：隨機 User-Agent 避免網站封 bot（比自己寫 UA 表簡單）
RUN pip install --no-cache-dir newspaper3k cloudscraper feedparser lxml_html_clean fake_useragent

# ── Tier 6：Node.js runtime + pptxgenjs（JS 版 PPT 產生器）──
# debian 12 預設 nodejs 是 v18，對 pptxgenjs 3.x 足夠用
# 把 node + pptxgenjs 放在這，這樣 LLM agent 收到 `.agents/skills/pptx` skill 時能直接 `require('pptxgenjs')`
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pptxgenjs \
    && npm cache clean --force

WORKDIR /project

# tail -f /dev/null：讓容器保持 running 等 exec 指令
CMD ["tail", "-f", "/dev/null"]
