"""
Pipeline YAML 設定模型。

範例 YAML：
  pipeline:
    name: 每日資料處理
    steps:
      - name: 資料抓取
        batch: python fetch_data.py
        timeout: 300
        output:
          path: /data/raw.csv
          expect: "CSV 檔，至少 100 列，含 date、price 欄位"
        retry: 2

      - name: 資料分析
        batch: python analyze.py
        timeout: 600
        output:
          path: /data/report.xlsx
          expect: "Excel 檔，大小大於 10KB"
        retry: 1
"""
from typing import Optional
import yaml
from pydantic import BaseModel


class StepOutput(BaseModel):
    """輸出檔案的預期描述（用自然語言，LLM 負責驗證）"""
    path: Optional[str] = None
    expect: str = ""
    description: str = ""  # 同 expect 的別名，YAML 可用 description 代替
    ai_validation: bool = True  # YAML 可用 ai_validation: true 明確啟用
    skill_mode: bool = False  # True = 使用 Skill agent 主動驗證

    def get_expect(self) -> str:
        """取得驗證描述（優先 expect，fallback 到 description）"""
        return self.expect or self.description


class ComputerUseAction(BaseModel):
    """單一桌面自動化動作。
    type 決定其餘欄位的解讀方式：
      - click_image：image 指定要尋找的錨點圖（相對 assets_dir 的檔名），點中心
      - click_at：x/y 絕對座標點擊（少用，僅當錨點失效時備援）
      - type_text：text 輸入純文字
      - hotkey：keys 為組合鍵陣列（如 ["ctrl", "c"]）
      - wait：seconds 靜態等待
      - wait_image：等某張圖出現（含 timeout），常用於等載入完成
      - screenshot：存一張截圖到 assets_dir（方便事後除錯，不影響流程）
      - drag / scroll：拖曳 / 捲動
      - assert_image：驗證某錨點圖「當下」必須可見，否則步驟失敗（短 timeout）
      - assert_text：OCR 驗證螢幕上必須有某段文字，否則步驟失敗
      - activate_window：把指定標題的視窗切到前景（解決錄製回放時視窗不在前的常見問題）
      - vlm_action：用自然語言描述目標 + VLM 視覺大模型決定要做什麼 primitive，不需錨點圖
    """
    type: str  # click_image | click_at | type_text | hotkey | wait | wait_image | screenshot | scroll | drag | assert_image | assert_text | activate_window | vlm_action
    image: str = ""       # 主錨點圖檔名（相對 assets_dir）
    image2: str = ""      # 次錨點圖檔名（多錨點驗證用，選填）
    dx2: int = 0          # 次錨點相對點擊點的位移 x
    dy2: int = 0          # 次錨點相對點擊點的位移 y
    anchor_off_x: int = 0 # 點擊位置相對錨點影像中心的偏移 x（螢幕邊緣擷取時非 0）
    anchor_off_y: int = 0 # 點擊位置相對錨點影像中心的偏移 y
    # 全螢幕截圖（錄製當下的虛擬桌面全景，供手動圈選參考）
    full_image: str = ""  # full_NNN.png 檔名
    full_left: int = 0    # 虛擬桌面原點 X（副螢幕在左側時會是負值）
    full_top: int = 0     # 虛擬桌面原點 Y
    x: int = 0
    y: int = 0
    x2: int = 0           # drag 終點 X
    y2: int = 0           # drag 終點 Y
    text: str = ""
    keys: list[str] = []
    seconds: float = 0.0
    timeout_sec: float = 10.0  # wait_image 的最大等待秒數
    dy: int = 0                # scroll 動作：滾輪缺口數（正數上、負數下）
    confidence: float = 0.65   # 圖像比對相似度門檻 (0.0-1.0)；實測錄製情境 0.65 最平衡
    button: str = "left"       # click 按鈕：left/right/middle
    clicks: int = 1            # click 次數：1=單擊, 2=雙擊
    description: str = ""      # 使用者可讀的動作描述（給 UI 顯示）
    use_coord: bool = True     # 預設 True = 用絕對座標點擊（快、不誤判，適合畫面穩定的場景）
                               # False = 切換到圖像比對（視窗會移動時才需要）
    hold_sec: float = 0.0      # click 按住不放的持續時間（> 0 會在回放時 mouseDown-sleep-mouseUp 取代瞬擊）
    modifiers: list[str] = []  # click 時按著的修飾鍵（如 ["ctrl"] 或 ["ctrl","shift"]）
    use_ocr: bool = False      # click_image 專用：顯式 OCR 啟用旗標。True 且 ocr_text 有值才跑 OCR
    ocr_text: str = ""         # OCR 目標文字（要跟 use_ocr=True 搭配才會生效）
    # OCR 搜尋範圍（per-action 藍框，虛擬桌面絕對座標）。width=0 表示沒自訂，
    # 回退使用 cv_search_radius 以紅十字為中心的預設區域
    ocr_box_left: int = 0
    ocr_box_top: int = 0
    ocr_box_width: int = 0
    ocr_box_height: int = 0
    # activate_window 專用：至少要填 title 或 title_contains 其一
    title: str = ""              # 精確視窗標題比對
    title_contains: str = ""     # 視窗標題子字串比對（大小寫不敏感）
    # CV 搜尋矩形（per-action 紅框，虛擬桌面絕對座標）。格式 [left, top, width, height]。
    # 給定時覆蓋預設的「錄製座標 ±cv_search_radius」範圍搜尋，適用於：
    #   1. 目標區域大、半徑 400 不夠；2. 有多個相似 UI 元素要精準定位；3. 加速（更小區域 = 更快）
    # click_image / wait_image / assert_image / vlm_action 都支援
    search_region: list[int] = []
    # ── VLM 視覺大模型比對（第 4 種 primary mode，peer to use_ocr / use_coord）──
    # 截圖流程完全不變：繼續用 _capture_screen() 抓全螢幕，送 VLM 前 JPEG q=70 壓縮節省 token。
    # 優先序：use_ocr 勾 → 走 OCR；未勾 + use_vlm 勾 → 走 VLM；皆未勾 → 走座標/CV
    use_vlm: bool = False         # click_image 專用：勾起來就用 VLM 找位置（送截圖+錨點圖）
    vlm_prompt: str = ""          # 可選額外自然語言描述（"紅色送出鈕，不是藍色取消"）
    vlm_cv_fallback: bool = False  # VLM 失敗時是否退回 CV 鏈（類比 ocr_cv_fallback）
    vlm_model: str = ""           # 可選 override：本動作使用的 VLM 模型名稱（留空 = 用 settings）


class PipelineStep(BaseModel):
    name: str
    batch: str = ""       # Shell 命令（skill_mode 時可為自然語言描述）
    working_dir: str = ""  # 工作目錄（run_python/run_shell 的 cwd）
    timeout: int = 300    # 秒
    output: Optional[StepOutput] = None
    retry: int = 1        # 自動重試次數（超過才問用戶）
    skill_mode: bool = False  # True = batch 為自然語言，由 LLM Skill agent 執行
    skill: str = ""            # 掛載的 Claude Code skill 名稱（~/.agents/skills/ 下的資料夾名）
    readonly: bool = False  # True = 唯讀驗證模式，禁止修改檔案
    ask_mode: bool = False  # True = 詢問模式：LLM 遇到任何不確定就主動用 ask_user 問用戶
    human_confirm: bool = False  # True = 人工確認節點，暫停等待確認
    message: str = ""            # 人工確認時的自訂訊息
    notify_telegram: bool = True  # 人工確認時是否發 Telegram
    screenshot: bool = False     # True = 暫停前自動截圖，附帶到 Telegram
    # ── 桌面自動化節點（computer_use）────────────────────────────────
    # 此為獨立第 4 種節點，不與 skill / script / human_confirm 混用。
    # 當 computer_use=True 時，runner 走桌面自動化引擎（pyautogui + cv2 比對），
    # 完全跳過 LLM 與 recipe 系統。
    computer_use: bool = False   # True = 桌面自動化節點
    actions: list[ComputerUseAction] = []  # 錄製/手編的動作序列
    assets_dir: str = ""         # 錨點圖片資料夾（相對路徑掛到工作流目錄下）
    fail_fast: bool = True       # True = 任一動作失敗立即中止；False = 警告後繼續
    # ── CV 比對設定（套用到本節點所有 click_image/drag 動作）──────────
    cv_threshold: float = 0.65   # 比對門檻：0.65 寬鬆 / 0.80 標準 / 0.90 嚴格
    cv_search_only_near: bool = False  # True = 只在錄製座標附近搜尋，不擴大到全螢幕
    cv_search_radius: int = 400  # 附近搜尋半徑（像素）；實際搜尋範圍為 (2r × 2r)
    cv_trigger_hover: bool = True  # True = 比對前先把游標移到錄製座標並等，讓 Windows hover 效果出現
    cv_hover_wait_ms: int = 200    # hover 等待時間：200（快）/ 400（保險，Windows 部分動畫較慢）
    cv_coord_fallback: bool = False # True = CV 完全找不到時退回錄製座標硬點下去；False（預設）= 失敗就 FAIL 不亂點
    # ── OCR 比對設定 ──────────────────────────────────────────────────
    ocr_threshold: float = 0.6     # OCR 最小 confidence：低於這數字視為沒匹配到
                                   # 分級: 1.0 精確 / 0.9 target⊆word / 0.8 跨詞行層級 / 0.6 模糊
    ocr_cv_fallback: bool = False  # True = OCR 失敗時退到 CV 比對鏈（再受 cv_coord_fallback 接棒）；False（預設）= 失敗就 FAIL
    # ── VLM 比對設定 ──────────────────────────────────────────────────
    # 白名單：vlm_action 時 VLM 可以回傳哪些 primitive。空 list（預設）= 全部允許
    # 可用值：click / double_click / right_click / type_text / hotkey / drag
    # 安全用途：高風險 step 可設 ["click"] 禁止 VLM 亂下 hotkey 或 drag
    vlm_allowed_primitives: list[str] = []


class PipelineConfig(BaseModel):
    name: str
    steps: list[PipelineStep]
    validate: bool = True  # False = 跳過 LLM 驗證，僅靠 exit code

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # 支援頂層有 "pipeline:" 或直接是 {name, steps}
        raw = data.get("pipeline", data)
        filtered = {k: v for k, v in raw.items() if not k.startswith("_")}
        return cls(**filtered)

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineConfig":
        # 過濾掉非 schema 的內部旗標（如 _use_recipe）
        filtered = {k: v for k, v in data.items() if not k.startswith("_")}
        return cls(**filtered)
