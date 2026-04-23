# Pipeline Orchestrator 開發路線圖：超越 N8n

> **定位**：AI-first + 桌面 / 瀏覽器 / API 三位一體的 workflow 引擎。
> **不打的戰場**：N8n 的 400+ integrations、UI 精緻度、社群節點市集 —— 這些要累積 3-5 年。
> **要贏的戰場**：AI 原生（Skill + Recipe + VLM）、OS 級桌面自動化（N8n 進不去）、離線企業內部友好（Ollama + WSL Docker sandbox）。

本文件是**給主 agent Claude Code 評估取捨用**的開發計畫，不是承諾路線。每一項都附**檔案:行數**讓實作者能快速定位。

---

## 目錄

1. [進度總覽](#1-進度總覽)
2. [Phase 0：已完成（本分支 `claude/explore-new-nodes-KujUf`）](#2-phase-0已完成本分支)
3. [Phase 1：控制流節點（優先）](#3-phase-1控制流節點)
4. [Phase 2：Skill 節點強化（取代原「Agent 節點」方案）](#4-phase-2skill-節點強化)
5. [Phase 3：Computer Use 進階強化](#5-phase-3computer-use-進階強化)
6. [Phase 4：整合節點（公司內部使用為主，低優先）](#6-phase-4整合節點)
7. [Phase 5：前端 DX](#7-phase-5前端-dx)
8. [時程建議](#8-時程建議)
9. [給實作者的規範](#9-給實作者的規範)

---

## 1. 進度總覽

| 類別 | 計畫項目 | 狀態 |
|---|---|---|
| 🟢 Computer Use 穩定度 | 模板 LRU 快取 + preflight | ✅ 已實作 |
| 🟢 Computer Use 新動作 | `assert_image` / `assert_text` | ✅ 已實作 |
| 🟢 Computer Use 新動作 | `activate_window` | ✅ 已實作 |
| 🟢 Computer Use 精準度 | `search_region` 欄位（紅框） | ✅ 已實作 |
| 🟢 **Computer Use AI** | **VLM 第 4 種比對模式 + `vlm_action`** | ✅ **已實作** |
| 🟡 控制流 | 變數 / 表達式系統 | 🔲 待實作 |
| 🟡 控制流 | `condition` 節點 | 🔲 待實作 |
| 🟡 控制流 | `loop` 節點 | 🔲 待實作 |
| 🟡 控制流 | `merge` / `split` 節點 | 🔲 待實作 |
| 🟡 Skill 強化 | 工具池擴充（取代 Agent 節點） | 🔲 待實作 |
| 🟡 Skill 強化 | 沙盒預設安全化 | 🔲 待實作 |
| 🟢 Computer Use 進階 | UIA 雙軌定位（Windows） | 🔲 長期 |
| 🟢 Computer Use 進階 | 錄製智慧錨點（Canny 特徵點） | 🔲 長期 |
| 🔵 整合節點 | 暫緩（公司內部用不到） | ⏸ 延後 |

---

## 2. Phase 0：已完成（本分支）

### Commit 1 `d0b28df` — 模板 LRU cache + preflight
**檔案**：`backend/pipeline/computer_use.py`

- `_load_template()` LRU 快取（64 張上限，mtime 變動自動失效），解決 `find_template` 每次都重新 decode + Canny 的浪費
- `validate_action_assets()` step 開始前掃一遍錨點圖，缺圖直接 FAIL（`exit_code=2`），避免跑到一半才發現

### Commit 2 `6151dfe` — assert_image / assert_text
**檔案**：`backend/pipeline/computer_use.py`, `backend/pipeline/models.py`

- 新增兩個「驗證」動作類型，用於在流程中插檢查點（點完登入→assert "歡迎回來"）
- `assert_text` 走現有 `pipeline.ocr.find_text_on_screen`

### Commit 3 `bb94376` — activate_window
**檔案**：`backend/pipeline/computer_use.py`, `backend/pipeline/models.py`

- 解決「目標視窗在背景」這個回放最大失敗原因
- 用 pygetwindow（已在 `skill_packages.txt`），Win32 `SetForegroundWindow` fallback

### Commit 4 `c5e047b` — search_region
**檔案**：`backend/pipeline/computer_use.py`, `backend/pipeline/models.py`

- Per-action 紅框（`[left, top, width, height]` 絕對桌面座標）限制搜尋區域
- 支援 `click_image` / `wait_image` / `assert_image`
- 效能提升 3-10x（2560×1440 → 400×300 搜尋）+ 解決多個相似元素的混淆

### Commit 5 `1dcd20d` — VLM 第 4 種比對模式 + vlm_action（本分支重頭戲）
**檔案**：`backend/pipeline/computer_use.py`, `backend/pipeline/models.py`, `backend/pipeline/runner.py`

**對應使用者討論的設計**：

- **不動既有邏輯**：截圖流程、既有 primitive、OCR / 座標 / CV 分支完全保留
- **use_vlm=True**：送截圖 + 錨點圖（+ 可選自然語言）給 VLM 找位置 → 點擊
- **vlm_action**：全新動作類型，無錨點圖，自然語言描述 → VLM 決定用哪個既有 primitive
- **安全 guardrails**：
  - JPEG q=70 壓縮 + 可選 `search_region` 裁切送 → 省 token
  - VLM 吐回的座標超出螢幕 → 視為失敗
  - JSON 解析失敗 → 視為失敗
  - `vlm_allowed_primitives` step-level 白名單（可禁 drag / hotkey）
- **模型支援**：走 `llm_factory.build_llm()`，Gemini / OpenRouter (Claude/GPT-4V) 全部支援

---

## 3. Phase 1：控制流節點

**為什麼優先**：沒有控制流等於沒有 workflow。目前 pipeline 只能線性執行，無法 if / loop / 匯流。

### Ticket 1：變數 / 表達式系統（所有控制流的前置）

**檔案**：新增 `backend/pipeline/expression.py`；改 `models.py`, `runner.py`, `executor.py`

**內容**：
- `jinja2.StrictUndefined` 包 `render(template, context)`
- Context：`{steps: {...}, env: os.environ, secrets: <vault>, input: run.input_params, loop: {...}}`
- 在 `runner.py:804` step 執行前呼叫 `render_step()` 把所有 str 欄位 render 過
- `PipelineRun.input_params: dict` 新欄位
- `POST /pipeline/run` 新增 optional `input_params` body

**驗收**：`batch: "python process.py --input {{ steps.step1.output.path }}"` 能正確替換。未定義變數 raise `UndefinedError`。

---

### Ticket 2：condition 節點（IF / Switch）

**檔案**：`models.py` + `runner.py`（dispatcher 改成 step-name map + current pointer）

```yaml
- name: check_size
  condition: true
  expression: "{{ steps.step1.output.rows | int }} > 100"
  on_true: bulk_step
  on_false: single_step

- name: route
  condition: true
  switch: "{{ steps.api.output.status }}"
  cases: { "200": ok_step, "404": retry_step }
  default: fail_step
```

**保護**：同一 step 訪問超過 `MAX_VISITS = 1000` → abort（防無限迴圈）

---

### Ticket 3：loop 節點（ForEach）

**檔案**：`models.py` + `runner.py` 新增 `execute_loop()`

```yaml
- name: batch_process
  loop: true
  items: "{{ steps.load.output.rows }}"    # 或 glob: "data/*.csv"
  max_concurrency: 5
  continue_on_error: false
  body:
    - { name: sub_call, skill_mode: true, batch: "處理 {{ loop.item.name }}" }
```

- 用 `asyncio.Semaphore(max_concurrency)` 並行
- iteration 之間注入 `loop: {item, index, total}` 到 context
- `continue_on_error=true` 時單筆失敗不中斷整個迴圈

---

### Ticket 4：merge / split 節點

```yaml
- name: combine
  merge: true
  mode: concat_csv | join_json | wait_all
  inputs: ["{{ steps.a.output.path }}", "{{ steps.b.output.path }}"]
  output_path: ai_output/combined.csv

- name: fanout
  split: true
  source: "{{ steps.load.output.path }}"
  chunk_size: 100
```

**concat_csv** 用 pandas；**join_json** 用 dict update；**split** 把一個 list 輸出拆成 N 條路徑給 loop 消費。

---

## 4. Phase 2：Skill 節點強化

### 背景（跟使用者討論的結論）

原本我提議「新增 Agent 節點」但使用者 push back：**Skill 節點本來就是 tool-using agent**（`executor.py:663-682`），沒必要另開一個。

真正的 gap 是 Skill 的**工具池太窄**（只有 run_python / run_shell / read_file / ask_user），打 API 還是得叫 LLM 寫 `requests`，慢又貴。

### Ticket 5：擴充 Skill 工具池

**檔案**：`backend/pipeline/executor.py:663-682`

把下列能力註冊為 Skill 可呼叫的 tool，讓 LLM 直接 tool-call 而非產程式碼：

```python
SKILL_TOOLS = {
    "run_python": ...,         # 既有
    "run_shell": ...,          # 既有
    "read_file": ...,          # 既有
    "ask_user": ...,           # 既有
    # 新增 ↓
    "http_request": ...,       # 包 httpx（已裝）
    "vision_analyze": ...,     # 包 _vlm_invoke，給 LLM 看圖
    "computer_use_action": ...,  # 把一組 computer_use action 當工具呼叫
    "call_workflow": ...,       # 呼叫子 workflow
}
```

**Schema 變更**（step 新增一個欄位控制工具白名單）：
```yaml
- name: research
  skill_mode: true
  batch: "查 AAPL 過去 30 天股價並產圖"
  tools: [http_request, run_python]   # 留空 = 全部
  max_iterations: 15
  use_recipe: false    # agent-like 任務關閉快取
```

**好處**：省 token、更穩（不 hallucinate API 呼叫）、Recipe 快取仍可用於純資料處理。

---

### Ticket 6：Skill 沙盒預設安全化

**檔案**：`backend/pipeline/executor.py:709`, `backend/settings.py`

**問題**：`skill_sandbox_mode` 預設 `"host"`，使用者不知情就讓 Skill 直接跑在 Windows host。

**修改**：
- 預設改 `"wsl_docker"`
- UI 顯示目前 sandbox 狀態 badge（`frontend/app/settings/page.tsx`）
- 新使用者首次使用前跳警告：「Skill 目前跑在主機上，建議切換到 WSL Docker 沙盒」
- 容器加資源上限：`docker run --memory 2g --cpus 2`（`sandbox.py`）
- stdout/stderr 加 size cap（目前 unbounded 有 OOM 風險）

---

### Ticket 7：Skill prompt-injection 硬化

**檔案**：`backend/skill_scanner.py:392-497`

**問題**：`get_skill_prompt_injection()` 把整份 SKILL.md 貼進 system prompt，惡意 skill 可在 `<!-- -->` comment 或 frontmatter 藏惡意指令。

**修改**：
- 剝除 HTML comments / control chars / 超長行
- 對未簽章的 skill 顯示「信任此 skill 的 prompt 注入嗎？」UI 確認
- 限 SKILL.md 長度 ≤ 8KB（超過截斷並警告）

---

## 5. Phase 3：Computer Use 進階強化

### Ticket 8：UIA 雙軌定位（Windows）

**檔案**：`backend/pipeline/recorder.py`, `backend/pipeline/computer_use.py`

**目標**：錄製時同步抓 UI 元素樹，回放時先試 UIA（精準）→ 失敗 fallback 到 CV → 最後座標。

- `requirements.txt` 加 `pywinauto`（Windows only；try/except import）
- 錄製時每個 click 記錄 AutomationId / ControlType / Name
- actions.json 加可選欄位：`uia: {automation_id, control_type, name}`
- 回放優先 UIA（成功率 95%+ vs 現有 CV 70-85%）

**ROI**：單一 ticket 能把解析度 / DPI / 主題變動的失敗率從 15-30% 壓到 < 5%。macOS 用 `pyatspi` 是長期工作。

---

### Ticket 9：錄製時智慧錨點

**檔案**：`backend/pipeline/recorder.py:146-220`

**問題**：目前固定擷取 80×80 px。按鈕剛好落在純色背景 → 錨點無特徵 → 回放匹配爛。

**修改**：
- 擷取後對區域跑 `cv2.goodFeaturesToTrack`
- 特徵點數 < 4 → 擴大到 120×120 重試
- 在 `meta.json` 記 `anchor_stability: "high" | "medium" | "low"`
- 前端 panel 對 low 穩定度標紅、提醒重錄

---

### Ticket 10：動作速度可調

**檔案**：`backend/pipeline/computer_use.py:221`

目前 `pyautogui.PAUSE = 0.15` 寫死。

新欄位：
- Step 層級：`cu_speed: Literal["fast", "normal", "slow"] = "normal"` → `0.05 / 0.15 / 0.4`
- 某些應用反應慢，必須 slow；demo 錄影可以 fast

---

### Ticket 11：多螢幕精確控制 + VLM for computer_use recipe

**檔案**：`backend/pipeline/computer_use.py`

- Action 層級新欄位：`monitor: int | None`（0=all virtual desktop, 1/2=指定螢幕）
- **可選**：computer_use 也加 recipe 機制（目前只有 Skill 節點有）—— 記錄每個錨點的成功率，低於閾值自動觸發重錄提醒或 VLM 輔助重建錨點

---

## 6. Phase 4：整合節點（暫緩）

> **使用者指示**：公司內部使用為主，外部 API / 整合**低優先**。以下 ticket 先 stub 放著等有需求再動工。

- **HTTP request 節點**（模板化參數，credential 注入）
- **database 節點**（SQLite / Postgres / MySQL / Mongo）
- **webhook trigger**（外部事件打進來啟動 workflow）
- **加密 credential vault**（Fernet + SQLite 表，取代 settings 明文）
- **sub_workflow 節點**（workflow 可呼叫另一個 workflow）
- **browser_use 節點**（Playwright codegen 錄製瀏覽器動作）

這批任務方向都已在先前討論定義過，需要時再取出規格動工。

---

## 7. Phase 5：前端 DX

### Ticket 12：所有新節點的 Panel

前端需要為每個新節點類型加 panel：
- `_conditionPanel.tsx`（Ticket 2）
- `_loopPanel.tsx`（Ticket 3）
- `_mergePanel.tsx`（Ticket 4）
- **VLM panel 整合**：既有 `_computerUsePanel.tsx` 加 `use_vlm` toggle + `vlm_prompt` textarea + `vlm_cv_fallback` toggle（對齊 OCR 欄位排版）
- 新動作類型 `vlm_action` 在 action list 顯示成紫色 icon

所有 panel 欄位支援 `{{ }}` 插值（Ticket 1 之後）。

### Ticket 13：變數 autocomplete + dry-run 預覽

- `_scriptPanel.tsx` 等的 textarea 換成 monaco-editor 或 codemirror
- 輸入 `{{` 彈出 upstream step outputs / env / secrets 清單
- `POST /pipeline/dry-run`：不執行 step，只 render `{{ }}` 回傳預覽
- Canvas 加「預覽變數」按鈕

### Ticket 14：computer_use VLM panel 擴充

- `use_vlm` toggle（peer to 既有 `use_ocr`）
- VLM prompt textarea（可選）
- `vlm_cv_fallback` toggle
- `vlm_action` 節點：大 textarea 放 description、VLM model override 下拉
- Step 層級 `vlm_allowed_primitives` 多選框

---

## 8. 時程建議

| Week | Tickets | 里程碑 |
|---|---|---|
| **已完成** | Phase 0 (5 個 commit) | Computer Use 穩定度 + VLM 上線 |
| 1-2 | Ticket 1 | 變數 / 表達式系統（所有控制流前置） |
| 3-4 | Ticket 2, 3, 4 | 控制流完整（condition + loop + merge） |
| 5 | Ticket 5 | Skill 工具池擴充 |
| 6 | Ticket 6, 7 | Skill 沙盒 / prompt 安全化 |
| 7-8 | Ticket 8, 9, 10 | Computer Use 穩定度 70% → 90%+ |
| 9 | Ticket 11 | Computer Use multi-monitor + VLM recipe |
| 10-12 | Ticket 12, 13, 14 | 前端完工 |
| 需要再做 | Phase 4 | 公司內部沒需求就不做 |

---

## 9. 給實作者的規範

### 共通規則

1. **中文註解優先**（Traditional Chinese），技術名詞保留英文
2. 所有新 FastAPI endpoint 自動進 Swagger UI
3. 所有前端 HTTP 走 `frontend/lib/api.ts`，不直接 `fetch`
4. `models.py` 新欄位**必須有 default**，不能破壞既有 workflow YAML
5. 每個 ticket 完成後：`cd backend && pytest`、`cd frontend && npm run build && npm run lint`
6. Commit message 格式：`feat(ticket-N): <summary>` 或 `perf(xx): ...`
7. 不跨 ticket 改動，保持 PR review 範圍可控

### 主 agent 評估現有 commit 時的建議問題清單

對 Phase 0 已交付的 5 個 commit，評估重點：

- [ ] `d0b28df`（LRU cache）：cache 大小 64 是否合理？mtime-based 失效會不會誤觸發？
- [ ] `6151dfe`（assert）：`assert_image` 和 `wait_image` 語意重疊，要不要合併？（當前論據：log 可讀性 + default timeout 不同）
- [ ] `bb94376`（activate_window）：pygetwindow 在 Linux 拋例外時失敗訊息是否清楚？是否該先 `platform.system()` 檢查再動作？
- [ ] `c5e047b`（search_region）：欄位型別是 `list[int]` 而非 `tuple`，Pydantic 是否會正確解 YAML list？
- [ ] `1dcd20d`（VLM）：VLM 失敗重試策略？（目前失敗就 FAIL 或 fallback CV，沒重試 VLM 本身）
- [ ] `1dcd20d`（VLM）：是否該在 step 層級加 token budget（防 VLM 被惡意 prompt 拉到天價 token）？

---

**最後更新**：2026-04-23
**分支**：`claude/explore-new-nodes-KujUf`
**當前主線（`main`）**：未動 — 本分支 5 個 commits 供主 Claude 評估 cherry-pick
