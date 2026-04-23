"""
桌面自動化引擎（computer_use 節點專用）。

核心能力：
- L1 basic template matching（cv2.matchTemplate + TM_CCOEFF_NORMED）
- L2 multi-scale matching（對 template 做 ±15% 縮放，解決 DPI/視窗大小差異）
- 動作執行：click_image / click_at / type_text / hotkey / wait / wait_image / screenshot
- Emergency abort：pyautogui.FAILSAFE（滑鼠移到左上角 0,0 立即觸發）+ run_id 中止訊號

不與 skill / recipe 系統共用 — 純 pyautogui + opencv 執行，無 LLM 參與。
"""
from __future__ import annotations
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


# ── Emergency abort signal（執行中可從外部 set，立即中斷）────────
_abort_flags: dict[str, bool] = {}


# ── 模板圖 LRU 快取 ───────────────────────────────────────────────
# 對同一張錨點圖反覆 read_bytes + imdecode + cvtColor + Canny 是浪費；
# 典型一個 step 會對同一圖做 2~14 次（multi-scale × edge fallback × retry）。
# 以 (abs_path, mtime) 當 key，mtime 變動（使用者重錄）會自動失效。
# 記憶體成本：每個 ~5-50KB，上限 64 張 → < 4MB
_TPL_CACHE_MAX = 64
_tpl_cache: "OrderedDict[tuple[str, float], tuple[np.ndarray, np.ndarray]]" = OrderedDict()


def _load_template(tpl_path: Path):
    """解碼錨點圖 → 回傳 (gray, edge) 灰階/Canny 邊緣陣列，兩者皆用於 find_template 的 mode 切換。
    命中快取直接回；未命中解碼一次存入。失敗回 (None, None, 錯誤訊息)。"""
    import cv2
    try:
        mtime = tpl_path.stat().st_mtime
    except OSError as e:
        return None, None, f"模板 stat 失敗：{e}"
    key = (str(tpl_path), mtime)
    cached = _tpl_cache.get(key)
    if cached is not None:
        _tpl_cache.move_to_end(key)
        return cached[0], cached[1], ""
    try:
        buf = np.frombuffer(tpl_path.read_bytes(), dtype=np.uint8)
        tpl_color = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:
        return None, None, f"模板讀取例外：{e}"
    if tpl_color is None:
        return None, None, f"模板解碼失敗（格式錯誤？）：{tpl_path}"
    tpl_gray = cv2.cvtColor(tpl_color, cv2.COLOR_BGR2GRAY)
    tpl_edge = cv2.Canny(tpl_gray, 50, 150)
    _tpl_cache[key] = (tpl_gray, tpl_edge)
    while len(_tpl_cache) > _TPL_CACHE_MAX:
        _tpl_cache.popitem(last=False)
    return tpl_gray, tpl_edge, ""


def clear_template_cache() -> None:
    """測試或使用者重錄大量錨點後手動清快取用"""
    _tpl_cache.clear()


def request_abort(run_id: str) -> None:
    """標記此 run 需立即中止；computer_use 引擎會在每個動作間檢查"""
    _abort_flags[run_id] = True


def clear_abort(run_id: str) -> None:
    _abort_flags.pop(run_id, None)


def _should_abort(run_id: Optional[str]) -> bool:
    return bool(run_id) and _abort_flags.get(run_id, False)


# ── 螢幕擷取與圖像比對 ──────────────────────────────────────────

def _capture_screen() -> tuple[np.ndarray, int, int]:
    """抓所有螢幕聯集的完整截圖，回傳 (BGR ndarray, 原點 x, 原點 y)。

    關鍵：用 monitors[0]（虛擬桌面聯集）而非 monitors[1]（主螢幕），
    讓 cv2 template matching 能在多螢幕環境下找到任意螢幕上的目標；
    多螢幕時主螢幕左上不一定是 (0,0)，回傳的 origin 用來把比對到的
    相對座標轉回絕對桌面座標（pyautogui.click 接受的就是絕對座標）。
    """
    import mss
    import cv2
    with mss.mss() as sct:
        mon = sct.monitors[0]      # 所有螢幕聯集
        img = np.array(sct.grab(mon))
    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return bgr, mon["left"], mon["top"]


def _point_in_any_screen(x: int, y: int) -> tuple[bool, str]:
    """檢查 (x, y) 是否落在目前任一螢幕可見範圍內（支援多螢幕負座標）。
    用途：scroll / click 前避免把滑鼠拉到超出桌面範圍的座標。
    回傳 (是否在範圍內, 目前螢幕配置描述)。"""
    import mss
    try:
        with mss.mss() as sct:
            for mon in sct.monitors[1:]:
                left = mon["left"]
                top = mon["top"]
                if left <= x < left + mon["width"] and top <= y < top + mon["height"]:
                    return True, ""
            layout = "; ".join(
                f"{m['width']}×{m['height']} @ ({m['left']},{m['top']})"
                for m in sct.monitors[1:]
            )
            return False, f"目前螢幕：{layout}"
    except Exception:
        return True, ""  # 抓不到資訊就寬容處理


@dataclass
class MatchResult:
    found: bool
    center: tuple[int, int] = (0, 0)   # (x, y) 螢幕座標
    confidence: float = 0.0
    scale: float = 1.0                  # 命中的縮放比例
    reason: str = ""
    mode: str = "gray"                  # "gray" = 灰階匹配，"edge" = Canny 邊緣匹配


def find_template(
    template_path: str,
    threshold: float = 0.85,
    multi_scale: bool = True,
    near_xy: Optional[tuple[int, int]] = None,
    search_radius: int = 400,
    region: Optional[tuple[int, int, int, int]] = None,
    mode: str = "gray",
) -> MatchResult:
    """在當前螢幕找指定模板圖，回傳中心座標與相似度。

    L1: 單一尺度 matchTemplate（快，~5ms）
    L2: multi_scale=True 時額外跑 0.85/0.9/0.95/1.05/1.1/1.15 倍縮放，
        取最高相似度（~30ms，吸收 DPI 125%/150% 縮放差異）

    mode:
      - "gray"（預設）：灰階像素比對
      - "edge"：Canny 邊緣偵測後再比對 — 兩張圖都先跑 Canny 只留輪廓，
               對色彩/光線/hover 動畫等差異更容忍（conf 通常略低但更穩）

    搜尋範圍優先序（三選一）：
      region 給定 > near_xy 給定 > 全螢幕
      - region: (left, top, width, height) 虛擬桌面絕對座標，使用者明確指定的紅框
      - near_xy: 錄製座標附近 ±search_radius px 的方形範圍（自動退回舊行為）
      - 皆未給：整個虛擬桌面都找（速度最慢、誤匹配風險最高）
    """
    import cv2

    tpl_path = Path(template_path)
    if not tpl_path.is_file():
        return MatchResult(False, reason=f"模板不存在：{template_path}")

    # 從 LRU 快取拿灰階 + Canny 邊緣，避免每次呼叫都重做 decode+cvtColor+Canny
    tpl_gray, tpl_edge, err = _load_template(tpl_path)
    if err:
        return MatchResult(False, reason=err)

    screen_color, origin_x, origin_y = _capture_screen()
    screen_gray_full = cv2.cvtColor(screen_color, cv2.COLOR_BGR2GRAY)

    # Edge 模式：template 在 _load_template 已預算好 Canny；螢幕每次都要重算（畫面會變）。
    # 閾值 50/150 是常用的 hysteresis 組合，對 UI 元素邊緣偵測穩定
    if mode == "edge":
        tpl_proc_full = tpl_edge
        screen_proc_full = cv2.Canny(screen_gray_full, 50, 150)
    else:
        tpl_proc_full = tpl_gray
        screen_proc_full = screen_gray_full

    # 三選一裁切策略：region > near_xy > 全螢幕
    clip_offset_x, clip_offset_y = origin_x, origin_y
    if region is not None:
        # 使用者明確指定的搜尋矩形（絕對桌面座標）
        rl, rt, rw, rh = region
        rel_x = rl - origin_x
        rel_y = rt - origin_y
        H, W = screen_proc_full.shape
        left = max(0, rel_x)
        top = max(0, rel_y)
        right = min(W, rel_x + rw)
        bottom = min(H, rel_y + rh)
        if right - left < 20 or bottom - top < 20:
            return MatchResult(False, reason=f"search_region ({rl},{rt},{rw},{rh}) 與目前桌面範圍重疊不足")
        screen_proc = screen_proc_full[top:bottom, left:right]
        clip_offset_x = origin_x + left
        clip_offset_y = origin_y + top
    elif near_xy is not None:
        nx, ny = near_xy
        # 絕對座標 → 相對截圖的座標
        rel_x = nx - origin_x
        rel_y = ny - origin_y
        H, W = screen_proc_full.shape
        left = max(0, rel_x - search_radius)
        top = max(0, rel_y - search_radius)
        right = min(W, rel_x + search_radius)
        bottom = min(H, rel_y + search_radius)
        if right - left < 20 or bottom - top < 20:
            # 範圍超出螢幕太多（錄製座標根本不在目前桌面範圍內）
            return MatchResult(False, reason=f"錄製座標 ({nx},{ny}) 超出目前桌面範圍")
        screen_proc = screen_proc_full[top:bottom, left:right]
        clip_offset_x = origin_x + left
        clip_offset_y = origin_y + top
    else:
        screen_proc = screen_proc_full

    scales = [1.0]
    if multi_scale:
        # L2：涵蓋常見 DPI 差（100%/125%/150%）
        scales = [0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15]

    best = MatchResult(False, mode=mode)
    for s in scales:
        if abs(s - 1.0) < 1e-6:
            tpl_scaled = tpl_proc_full
        else:
            new_w = max(1, int(tpl_proc_full.shape[1] * s))
            new_h = max(1, int(tpl_proc_full.shape[0] * s))
            if new_w >= screen_proc.shape[1] or new_h >= screen_proc.shape[0]:
                continue
            tpl_scaled = cv2.resize(tpl_proc_full, (new_w, new_h), interpolation=cv2.INTER_AREA)
        try:
            res = cv2.matchTemplate(screen_proc, tpl_scaled, cv2.TM_CCOEFF_NORMED)
        except cv2.error:
            continue
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best.confidence:
            h, w = tpl_scaled.shape
            # 比對結果是相對於裁切區域的座標；加上裁切原點換算成桌面絕對座標
            cx = max_loc[0] + w // 2 + clip_offset_x
            cy = max_loc[1] + h // 2 + clip_offset_y
            best = MatchResult(
                found=max_val >= threshold,
                center=(cx, cy),
                confidence=float(max_val),
                scale=s,
                mode=mode,
            )
    if not best.found:
        area = "附近範圍" if near_xy else "整個桌面"
        best.reason = f"最佳相似度 {best.confidence:.3f} 低於門檻 {threshold}（搜尋{area}，{mode} 模式）"
    return best


# ── 動作執行 ────────────────────────────────────────────────────

@dataclass
class ActionResult:
    ok: bool
    action_index: int
    action_type: str
    message: str = ""
    duration_ms: int = 0


def _check_abort(run_id: Optional[str]) -> None:
    if _should_abort(run_id):
        raise RuntimeError("使用者中止（emergency abort）")


def _parse_search_region(action: dict) -> Optional[tuple[int, int, int, int]]:
    """解析 action['search_region'] = [left, top, width, height]（虛擬桌面絕對座標）。
    格式不對或尺寸 <= 0 回 None（代表不限制，走 near_xy / 全螢幕邏輯）。"""
    sr = action.get("search_region") or []
    if not isinstance(sr, (list, tuple)) or len(sr) != 4:
        return None
    try:
        l, t, w, h = int(sr[0]), int(sr[1]), int(sr[2]), int(sr[3])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (l, t, w, h)


# ── VLM（視覺 LLM）比對：第 4 種 primary mode，OCR 同級 peer ──────
# 設計原則：
#   1. 純「新增」不改舊：use_ocr / use_coord 關掉 + use_vlm=True 才走這條
#   2. 截圖流程沿用 _capture_screen() 不變；送 VLM 前 JPEG q=70 壓縮省 token
#   3. 有 search_region 就送裁切後的小圖（更省 token + 更精準）
#   4. VLM 吐 JSON 座標 → 絕對桌面座標 → 走現有 _do_click primitive
#   5. 解析失敗、座標超出螢幕一律視為 found=false，由 vlm_cv_fallback 決定退不退

_VLM_JPEG_QUALITY = 70
# 解析 LLM 回應時容忍前後有 ```json 包裝或多餘文字，抓最外層 {...}
_VLM_JSON_RE = None  # lazy init


def _encode_bgr_to_jpeg_b64(bgr: np.ndarray, quality: int = _VLM_JPEG_QUALITY) -> Optional[str]:
    """把 BGR ndarray 編成 JPEG base64 字串（送 VLM 用）。失敗回 None。"""
    import base64
    import cv2
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _encode_file_to_jpeg_b64(path: Path, quality: int = _VLM_JPEG_QUALITY) -> Optional[str]:
    """讀檔 → 解碼 → 重新 JPEG 壓縮 → base64。PNG 錨點圖也會被轉 JPEG 省 token。"""
    import cv2
    try:
        buf = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None
    if img is None:
        return None
    return _encode_bgr_to_jpeg_b64(img, quality)


def _parse_vlm_json(text: str) -> Optional[dict]:
    """從 LLM 回應中抓出 JSON 物件。容忍 ```json 包裝 / 前後廢話。失敗回 None。"""
    import re
    global _VLM_JSON_RE
    if _VLM_JSON_RE is None:
        # 非貪婪匹配最外層 {...}
        _VLM_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
    if not text:
        return None
    m = _VLM_JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


def _point_in_virtual_desktop(x: int, y: int) -> bool:
    """內部 util：VLM 回的座標是否落在目前任一螢幕上。超出視為 hallucination 不點。"""
    ok, _ = _point_in_any_screen(x, y)
    return ok


def _vlm_invoke(messages_content: list, model_override: str, logger: logging.Logger) -> Optional[str]:
    """用 llm_factory.build_llm() 叫目前設定的模型，回傳 str（VLM 的文字回應）。失敗 None。
    model_override 預留給未來 action 層級覆蓋模型（目前忽略，走 settings）。"""
    try:
        # 延遲 import 避免 pipeline/computer_use 在沒裝 LLM deps 環境載入失敗
        import sys as _sys
        import os as _os
        _backend_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _backend_root not in _sys.path:
            _sys.path.insert(0, _backend_root)
        from llm_factory import build_llm  # type: ignore
        from langchain_core.messages import HumanMessage  # type: ignore
    except Exception as e:
        logger.error(f"[computer_use/vlm] 無法載入 LLM stack：{e}")
        return None

    try:
        llm = build_llm(temperature=0.0)
    except Exception as e:
        logger.error(f"[computer_use/vlm] build_llm 失敗：{e}")
        return None

    try:
        resp = llm.invoke([HumanMessage(content=messages_content)])
        # langchain BaseMessage .content 可能是 str 或 list（多模態回應）
        content = resp.content
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
                elif isinstance(p, str):
                    parts.append(p)
            content = "\n".join(parts)
        return str(content or "")
    except Exception as e:
        logger.error(f"[computer_use/vlm] LLM 呼叫失敗：{e}")
        return None


@dataclass
class VlmResult:
    found: bool
    x: int = 0
    y: int = 0
    confidence: float = 0.0
    primitive: str = ""       # 僅 vlm_action 使用：click/type_text/hotkey/drag
    primitive_args: dict = None  # type: ignore[assignment]
    reason: str = ""


def _vlm_locate(
    screen_bgr: np.ndarray,
    origin_x: int, origin_y: int,
    anchor_path: Optional[Path],
    prompt_extra: str,
    region: Optional[tuple[int, int, int, int]],
    model_override: str,
    logger: logging.Logger,
) -> VlmResult:
    """VLM 找錨點：送截圖（+ 可選錨點圖）給 VLM，回傳桌面絕對座標。
    region 有給就只送裁切後的小圖（省 token、更準）；否則送整個虛擬桌面截圖。"""
    import cv2
    # 決定要送的截圖範圍：region 優先，否則整個虛擬桌面
    base_x = origin_x
    base_y = origin_y
    to_send = screen_bgr
    if region is not None:
        rl, rt, rw, rh = region
        rel_x = rl - origin_x
        rel_y = rt - origin_y
        H, W = screen_bgr.shape[:2]
        left = max(0, rel_x)
        top = max(0, rel_y)
        right = min(W, rel_x + rw)
        bottom = min(H, rel_y + rh)
        if right - left < 20 or bottom - top < 20:
            return VlmResult(False, reason=f"search_region ({rl},{rt},{rw},{rh}) 與目前桌面範圍重疊不足")
        to_send = screen_bgr[top:bottom, left:right]
        base_x = origin_x + left
        base_y = origin_y + top

    img_h, img_w = to_send.shape[:2]
    screen_b64 = _encode_bgr_to_jpeg_b64(to_send)
    if screen_b64 is None:
        return VlmResult(False, reason="截圖 JPEG 編碼失敗")

    anchor_b64 = None
    if anchor_path is not None:
        anchor_b64 = _encode_file_to_jpeg_b64(anchor_path)
        if anchor_b64 is None:
            logger.warning(f"[computer_use/vlm] 錨點圖讀不到或解碼失敗：{anchor_path}（繼續不含錨點）")

    text_parts = [
        "You are a UI element localization assistant. Given a screenshot, find the pixel center of the target UI element.",
        f"Screenshot is {img_w}×{img_h} pixels. Origin (0,0) is the upper-left corner; x increases to the right, y increases downward.",
    ]
    if anchor_b64:
        text_parts.append("The FIRST image below is the target's anchor (small crop from a previous recording). The SECOND image is the current full screenshot. Find the anchor's location in the full screenshot.")
    else:
        text_parts.append("No anchor image is provided; rely on the description below.")
    if prompt_extra:
        text_parts.append(f"Additional description of the target: {prompt_extra}")
    text_parts.append(
        "Reply with ONLY a single-line JSON object, no markdown, no prose:\n"
        "{\"found\": true, \"x\": <int>, \"y\": <int>, \"confidence\": <0.0-1.0>, \"reason\": \"<short>\"}\n"
        "If you cannot confidently identify the target, reply:\n"
        "{\"found\": false, \"confidence\": 0.0, \"reason\": \"<why>\"}"
    )
    text_prompt = "\n".join(text_parts)

    content: list = [{"type": "text", "text": text_prompt}]
    if anchor_b64:
        content.append({"type": "image_url", "image_url": f"data:image/jpeg;base64,{anchor_b64}"})
    content.append({"type": "image_url", "image_url": f"data:image/jpeg;base64,{screen_b64}"})

    raw = _vlm_invoke(content, model_override, logger)
    if raw is None:
        return VlmResult(False, reason="VLM 呼叫失敗")
    parsed = _parse_vlm_json(raw)
    if parsed is None:
        return VlmResult(False, reason=f"VLM 回應無法解析為 JSON：{raw[:120]}")
    if not parsed.get("found"):
        return VlmResult(False, confidence=float(parsed.get("confidence", 0.0) or 0.0),
                         reason=str(parsed.get("reason", "VLM 回 found=false")))
    # 轉絕對桌面座標
    try:
        img_x = int(parsed["x"])
        img_y = int(parsed["y"])
    except (KeyError, TypeError, ValueError):
        return VlmResult(False, reason=f"VLM 回應缺 x/y 或非整數：{parsed}")
    abs_x = img_x + base_x
    abs_y = img_y + base_y
    if not _point_in_virtual_desktop(abs_x, abs_y):
        return VlmResult(False, x=abs_x, y=abs_y,
                         confidence=float(parsed.get("confidence", 0.0) or 0.0),
                         reason=f"VLM 回座標 ({abs_x},{abs_y}) 超出目前桌面範圍 → 視為失敗")
    return VlmResult(
        found=True, x=abs_x, y=abs_y,
        confidence=float(parsed.get("confidence", 0.0) or 0.0),
        reason=str(parsed.get("reason", "")),
    )


_VLM_PRIMITIVE_ALL = ("click", "double_click", "right_click", "type_text", "hotkey", "drag")


def _vlm_decide_action(
    screen_bgr: np.ndarray,
    origin_x: int, origin_y: int,
    description: str,
    prompt_extra: str,
    region: Optional[tuple[int, int, int, int]],
    allowed: list[str],
    model_override: str,
    logger: logging.Logger,
) -> VlmResult:
    """vlm_action 專用：送截圖 + 自然語言目標，VLM 回「要做什麼 primitive + 參數」。
    allowed 空 list = 全部允許；非空 = 白名單（step 層級的 vlm_allowed_primitives）。"""
    base_x = origin_x
    base_y = origin_y
    to_send = screen_bgr
    if region is not None:
        rl, rt, rw, rh = region
        rel_x = rl - origin_x
        rel_y = rt - origin_y
        H, W = screen_bgr.shape[:2]
        left = max(0, rel_x)
        top = max(0, rel_y)
        right = min(W, rel_x + rw)
        bottom = min(H, rel_y + rh)
        if right - left < 20 or bottom - top < 20:
            return VlmResult(False, reason=f"search_region 與桌面重疊不足")
        to_send = screen_bgr[top:bottom, left:right]
        base_x = origin_x + left
        base_y = origin_y + top

    img_h, img_w = to_send.shape[:2]
    screen_b64 = _encode_bgr_to_jpeg_b64(to_send)
    if screen_b64 is None:
        return VlmResult(False, reason="截圖 JPEG 編碼失敗")

    effective_allowed = [p for p in (allowed or _VLM_PRIMITIVE_ALL) if p in _VLM_PRIMITIVE_ALL]
    if not effective_allowed:
        effective_allowed = list(_VLM_PRIMITIVE_ALL)

    text = (
        "You decide ONE desktop-automation primitive to execute based on the screenshot and the task description.\n"
        f"Screenshot is {img_w}×{img_h} px; origin (0,0) upper-left.\n"
        f"Task: {description}\n"
        + (f"Extra context: {prompt_extra}\n" if prompt_extra else "")
        + "Allowed primitives (pick EXACTLY ONE):\n"
          "- click: {\"primitive\":\"click\",\"x\":<int>,\"y\":<int>,\"button\":\"left|right|middle\",\"clicks\":1}\n"
          "- double_click: {\"primitive\":\"double_click\",\"x\":<int>,\"y\":<int>}\n"
          "- right_click: {\"primitive\":\"right_click\",\"x\":<int>,\"y\":<int>}\n"
          "- type_text: {\"primitive\":\"type_text\",\"text\":\"...\"}\n"
          "- hotkey: {\"primitive\":\"hotkey\",\"keys\":[\"ctrl\",\"c\"]}\n"
          "- drag: {\"primitive\":\"drag\",\"x\":<int>,\"y\":<int>,\"x2\":<int>,\"y2\":<int>}\n"
          f"ONLY these primitives are allowed this step: {effective_allowed}\n"
          "Coordinates are image-local (relative to the screenshot upper-left).\n"
          "Reply JSON ONLY, single line, no markdown:\n"
          "{\"found\":true,\"primitive\":\"<name>\",...args...,\"confidence\":0.9,\"reason\":\"<short>\"}\n"
          "If the target is not visible or cannot be acted upon:\n"
          "{\"found\":false,\"confidence\":0.0,\"reason\":\"<why>\"}"
    )

    content = [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": f"data:image/jpeg;base64,{screen_b64}"},
    ]
    raw = _vlm_invoke(content, model_override, logger)
    if raw is None:
        return VlmResult(False, reason="VLM 呼叫失敗")
    parsed = _parse_vlm_json(raw)
    if parsed is None:
        return VlmResult(False, reason=f"VLM 回應無法解析為 JSON：{raw[:120]}")
    if not parsed.get("found"):
        return VlmResult(False, confidence=float(parsed.get("confidence", 0.0) or 0.0),
                         reason=str(parsed.get("reason", "VLM 回 found=false")))
    prim = str(parsed.get("primitive", "")).strip()
    if prim not in effective_allowed:
        return VlmResult(False, reason=f"VLM 回傳 primitive='{prim}' 不在允許清單 {effective_allowed}")

    args: dict = {}
    if prim in ("click", "double_click", "right_click", "drag"):
        try:
            img_x = int(parsed["x"])
            img_y = int(parsed["y"])
        except (KeyError, TypeError, ValueError):
            return VlmResult(False, reason=f"{prim} 缺 x/y 或非整數")
        abs_x = img_x + base_x
        abs_y = img_y + base_y
        if not _point_in_virtual_desktop(abs_x, abs_y):
            return VlmResult(False, reason=f"VLM 回座標 ({abs_x},{abs_y}) 超出桌面範圍")
        args["x"] = abs_x
        args["y"] = abs_y
        if prim == "click":
            args["button"] = str(parsed.get("button", "left"))
            try:
                args["clicks"] = int(parsed.get("clicks", 1))
            except (TypeError, ValueError):
                args["clicks"] = 1
        if prim == "drag":
            try:
                img_x2 = int(parsed["x2"])
                img_y2 = int(parsed["y2"])
            except (KeyError, TypeError, ValueError):
                return VlmResult(False, reason="drag 缺 x2/y2")
            abs_x2 = img_x2 + base_x
            abs_y2 = img_y2 + base_y
            if not _point_in_virtual_desktop(abs_x2, abs_y2):
                return VlmResult(False, reason=f"drag 終點 ({abs_x2},{abs_y2}) 超出桌面範圍")
            args["x2"] = abs_x2
            args["y2"] = abs_y2
            args["button"] = str(parsed.get("button", "left"))
    elif prim == "type_text":
        args["text"] = str(parsed.get("text", ""))
        if not args["text"]:
            return VlmResult(False, reason="type_text 缺 text")
    elif prim == "hotkey":
        keys = parsed.get("keys", [])
        if not isinstance(keys, list) or not keys:
            return VlmResult(False, reason="hotkey 缺 keys 或格式不對")
        args["keys"] = [str(k) for k in keys]

    return VlmResult(
        found=True,
        x=args.get("x", 0), y=args.get("y", 0),
        primitive=prim,
        primitive_args=args,
        confidence=float(parsed.get("confidence", 0.0) or 0.0),
        reason=str(parsed.get("reason", "")),
    )


def _pyautogui_with_failsafe():
    """lazy import pyautogui 並設好 failsafe / 節流"""
    import pyautogui
    pyautogui.FAILSAFE = True  # 滑鼠甩到左上角 (0,0) 立即 FailSafeException
    pyautogui.PAUSE = 0.15     # 每個 pyautogui 呼叫後自動等 150ms，防過快
    return pyautogui


def _do_click(pg, x: int, y: int, button: str, clicks: int, hold_sec: float, modifiers: list) -> None:
    """統一的點擊執行器：處理長按 + 修飾鍵。
    modifiers: ["ctrl"], ["ctrl","shift"] 等 — 按下→click→放開。"""
    # 按下修飾鍵
    for mod in (modifiers or []):
        pg.keyDown(mod)
    try:
        if hold_sec > 0.1:
            pg.moveTo(x, y)
            pg.mouseDown(button=button)
            time.sleep(hold_sec)
            pg.mouseUp(button=button)
        else:
            pg.click(x=x, y=y, button=button, clicks=clicks)
    finally:
        # 反序放開修飾鍵，即使 click 拋例外也確保按鍵會放
        for mod in reversed(modifiers or []):
            pg.keyUp(mod)


def execute_action(
    action: dict,
    assets_dir: Path,
    index: int,
    logger: logging.Logger,
    run_id: Optional[str] = None,
    allow_coord_fallback: bool = True,
    cv_threshold: float = 0.65,
    cv_search_only_near: bool = False,
    cv_search_radius: int = 400,
    cv_trigger_hover: bool = True,
    cv_hover_wait_ms: int = 200,
    cv_coord_fallback: bool = False,
    ocr_threshold: float = 0.6,
    ocr_cv_fallback: bool = False,
    vlm_allowed_primitives: Optional[list[str]] = None,
) -> ActionResult:
    """執行單一 action。action 是 ComputerUseAction.model_dump() 結果的 dict。"""
    t0 = time.time()
    atype = action.get("type", "")
    desc = action.get("description") or atype
    logger.info(f"[computer_use] 動作 #{index + 1} ({atype})：{desc}")

    _check_abort(run_id)

    try:
        pg = _pyautogui_with_failsafe()

        if atype == "click_image":
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "click_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            # 門檻：action-level confidence 覆蓋 step 層級 cv_threshold，皆缺就用 0.65
            threshold = float(action.get("confidence") or cv_threshold)
            button = action.get("button", "left")
            clicks = int(action.get("clicks", 1))
            fx = action.get("x")
            fy = action.get("y")
            has_coord = isinstance(fx, (int, float)) and isinstance(fy, (int, float))

            hold_sec = float(action.get("hold_sec", 0) or 0)
            modifiers = list(action.get("modifiers", []) or [])
            mods_tag = f"[{'+'.join(modifiers)}]" if modifiers else ""

            # 四種 primary mode 獨立互不 coupling（per user feedback），但執行時還是要
            # 有優先順序。直覺是「使用者明確勾起來的 mode 優先」：
            #   OCR 勾起 + 有 ocr_text → 走 OCR
            #   VLM 勾起              → 走 VLM（第 4 種模式，送截圖+錨點給視覺 LLM 找位置）
            #   OCR / VLM 都沒勾 + use_coord=true → 走絕對座標
            #   都沒勾 + use_coord=false → 走 CV 圖像比對
            # 優先序 OCR > VLM 是因為 OCR 更快更便宜；兩個都勾預設走 OCR
            use_ocr = bool(action.get("use_ocr", False))
            ocr_text = (action.get("ocr_text") or "").strip()
            ocr_will_run = use_ocr and bool(ocr_text)
            use_vlm = bool(action.get("use_vlm", False))
            vlm_will_run = use_vlm and not ocr_will_run

            # 預設使用絕對座標（快速且穩定）；只有使用者主動切到圖像比對模式才跑 template matching
            # 注意：get 第二引數 True 表示若 action 根本沒 use_coord 欄位，也視為座標模式
            # OCR/VLM 有啟用就跳過座標短路，讓新模式先試（失敗再依各自 fallback 決定退不退）
            if action.get("use_coord", True) and has_coord and not ocr_will_run and not vlm_will_run:
                _do_click(pg, int(fx), int(fy), button, clicks, hold_sec, modifiers)
                hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                msg = f"[強制座標]{mods_tag} 點擊 ({fx},{fy}) button={button} clicks={clicks}{hold_tag}"
                duration = int((time.time() - t0) * 1000)
                logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                return ActionResult(True, index, atype, msg, duration)

            # Hover 預熱：錄製當下游標停在按鈕上、錨點擷取到 Windows hover highlight
            # 狀態；回放用 pyautogui 瞬移沒觸發 hover → 螢幕與錨點不一樣 conf 掉
            # 把游標移到錄製座標附近、等指定 ms 讓 hover 效果渲染後再比對
            # OCR / VLM 模式跳過 hover（文字偵測/VLM 不靠 hover 效果）
            if cv_trigger_hover and has_coord and not ocr_will_run and not vlm_will_run:
                try:
                    pg.moveTo(int(fx), int(fy))
                    time.sleep(max(50, int(cv_hover_wait_ms)) / 1000.0)
                except Exception:
                    pass  # 移動失敗就略過（例如座標超出螢幕），後面搜尋仍然照跑

            # ── OCR 模式分支 ──
            # 只有 use_ocr=True 且 ocr_text 有值才跑。OCR 失敗時的後續行為由 ocr_cv_fallback 控制：
            #   ocr_cv_fallback=False（預設）→ 失敗立即 FAIL（符合「選 OCR 就代表 CV 不適用」的直覺）
            #   ocr_cv_fallback=True         → 失敗繼續走下面的 CV 比對鏈（再受 cv_coord_fallback 接棒）
            if ocr_will_run:
                find_text_on_screen = None
                try:
                    from pipeline.ocr import find_text_on_screen
                except Exception:
                    try:
                        from .ocr import find_text_on_screen  # type: ignore
                    except Exception as _e:
                        logger.error(f"[computer_use]   ✗ 無法載入 OCR 模組：{_e}")
                if find_text_on_screen is not None:
                    screen_bgr, sx, sy = _capture_screen()
                    near = (int(fx), int(fy)) if has_coord else None
                    # 藍框：per-action 顯式 OCR 搜尋範圍（絕對桌面座標）
                    ocr_region = None
                    _box_w = int(action.get("ocr_box_width", 0) or 0)
                    _box_h = int(action.get("ocr_box_height", 0) or 0)
                    if _box_w > 0 and _box_h > 0:
                        ocr_region = (
                            int(action.get("ocr_box_left", 0) or 0),
                            int(action.get("ocr_box_top", 0) or 0),
                            _box_w,
                            _box_h,
                        )
                    ocr_res = find_text_on_screen(
                        screen_bgr, ocr_text, origin_x=sx, origin_y=sy,
                        lang_tag="zh-Hant-TW",
                        near_xy=near, search_radius=cv_search_radius,
                        threshold=ocr_threshold,
                        region=ocr_region,
                    )
                    if ocr_res.found:
                        _do_click(pg, ocr_res.center[0], ocr_res.center[1],
                                  button, clicks, hold_sec, modifiers)
                        hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                        msg = (f"{mods_tag} 點擊 OCR 文字 '{ocr_text}' @ {ocr_res.center} "
                               f"(matched='{ocr_res.text[:30]}', conf={ocr_res.confidence:.2f}){hold_tag}")
                        duration = int((time.time() - t0) * 1000)
                        logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                        return ActionResult(True, index, atype, msg, duration)
                    # OCR 失敗
                    if not ocr_cv_fallback:
                        fail_msg = f"{ocr_res.reason}（ocr_cv_fallback=off → 失敗直接 FAIL 不退回 CV/座標）"
                        logger.error(f"[computer_use]   ✗ {fail_msg}")
                        return ActionResult(False, index, atype, fail_msg)
                    logger.info(f"[computer_use]   {ocr_res.reason[:120]}，ocr_cv_fallback=on → 改試 CV 比對")
                elif not ocr_cv_fallback:
                    # OCR 模組載不進來且使用者沒開 fallback → 直接 FAIL（不偷偷走 CV）
                    return ActionResult(False, index, atype, "OCR 模組無法載入且 ocr_cv_fallback=off")

            # ── VLM 模式分支（第 4 種 primary mode）──
            # 送「全螢幕截圖 + 錨點圖 + 可選自然語言描述」給視覺 LLM，回傳要點擊的座標。
            # 失敗行為：vlm_cv_fallback=False（預設）→ 立即 FAIL；=True → 繼續走下面 CV 鏈。
            # 搜尋範圍：若 action 有 search_region 就只送裁切圖（省 token 更準）；否則送全桌面。
            if vlm_will_run:
                vlm_cv_fallback = bool(action.get("vlm_cv_fallback", False))
                vlm_prompt = str(action.get("vlm_prompt", "") or "")
                vlm_model = str(action.get("vlm_model", "") or "")
                region_rect_vlm = _parse_search_region(action)
                screen_bgr_vlm, vsx, vsy = _capture_screen()
                vlm_res = _vlm_locate(
                    screen_bgr_vlm, vsx, vsy,
                    anchor_path=(tpl_path if tpl_path.is_file() else None),
                    prompt_extra=vlm_prompt,
                    region=region_rect_vlm,
                    model_override=vlm_model,
                    logger=logger,
                )
                if vlm_res.found:
                    _do_click(pg, vlm_res.x, vlm_res.y, button, clicks, hold_sec, modifiers)
                    hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                    msg = (f"{mods_tag} 點擊 VLM 找到的位置 @ ({vlm_res.x},{vlm_res.y}) "
                           f"(conf={vlm_res.confidence:.2f}){hold_tag}")
                    duration = int((time.time() - t0) * 1000)
                    logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                    return ActionResult(True, index, atype, msg, duration)
                # VLM 失敗
                if not vlm_cv_fallback:
                    fail_msg = f"VLM 失敗：{vlm_res.reason}（vlm_cv_fallback=off → 直接 FAIL）"
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                logger.info(f"[computer_use]   VLM 失敗：{vlm_res.reason[:120]}，vlm_cv_fallback=on → 改試 CV 比對")

            # 搜尋策略：
            # 1. 有錄製座標 → 先在附近 ±cv_search_radius 範圍搜尋（防跨螢幕假陽性）
            #    首次 match 若 conf 低於門檻，等 150ms 再 retry 一次（最多 2 次）
            #    吸收 hover fade-in / transition 動畫未穩定造成的瞬時誤判
            #    典型 case：Windows 關閉鈕第一次 match 得 0.56、再等 150ms 變 0.97
            # 2. 仍找不到：若 cv_search_only_near=True → 直接 FAIL
            #              否則擴大到整個桌面
            # 3. 全螢幕也找不到 → 退回絕對座標 fallback（下方 else 分支）
            _SETTLE_RETRIES = 2          # 第一次 + 最多 1 次 retry
            _SETTLE_WAIT_MS = 150        # retry 前 sleep

            # 使用者明確指定的搜尋紅框（優先於錄製座標附近搜尋）
            region_rect = _parse_search_region(action)

            def _search(nx_: Optional[int], ny_: Optional[int]) -> MatchResult:
                """先跑 gray 模式，若 conf < threshold 再跑 edge 模式，取較高 conf。
                edge 對 hover fade / 主題色差異更容忍，代價 +20ms。
                搜尋區域優先序：region_rect > near_xy + radius > 全螢幕。"""
                def _find(m: str) -> MatchResult:
                    if region_rect is not None:
                        return find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                             region=region_rect, mode=m)
                    if nx_ is not None and ny_ is not None:
                        return find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                             near_xy=(nx_, ny_), search_radius=cv_search_radius, mode=m)
                    return find_template(str(tpl_path), threshold=threshold, multi_scale=True, mode=m)
                gray = _find("gray")
                if gray.found:
                    return gray
                # Gray 沒過門檻 → 試 edge 救一下
                edge = _find("edge")
                # 以 conf 做仲裁，但考量 edge 先天分數偏低，edge 要多給 0.05 才可以勝出
                # 避免 gray 比較接近但仍低、edge 亂抓到邊緣多的位置
                if edge.found or edge.confidence >= gray.confidence + 0.05:
                    logger.info(f"[computer_use]   edge fallback: gray={gray.confidence:.2f}, edge={edge.confidence:.2f} → 採用 edge")
                    return edge
                return gray

            if has_coord:
                m = MatchResult(False)
                for _attempt in range(_SETTLE_RETRIES):
                    m = _search(int(fx), int(fy))
                    if m.found:
                        break
                    if _attempt + 1 < _SETTLE_RETRIES:
                        logger.info(f"[computer_use]   附近首次比對 conf={m.confidence:.2f} < {threshold}，等 {_SETTLE_WAIT_MS}ms 讓動畫穩定後 retry")
                        time.sleep(_SETTLE_WAIT_MS / 1000.0)
                if not m.found and not cv_search_only_near:
                    logger.info(f"[computer_use]   附近 ±{cv_search_radius}px 找不到（best={m.confidence:.2f}），擴大到整個桌面")
                    m = _search(None, None)
            else:
                m = _search(None, None)

            if m.found:
                # 螢幕邊緣擷取時，點擊位置不在錨點影像中心，加上偏移校正
                off_x = int(action.get("anchor_off_x", 0) or 0)
                off_y = int(action.get("anchor_off_y", 0) or 0)
                click_x = m.center[0] + int(off_x * m.scale)
                click_y = m.center[1] + int(off_y * m.scale)
                _do_click(pg, click_x, click_y, button, clicks, hold_sec, modifiers)
                hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                off_tag = f" off=({off_x},{off_y})" if (off_x or off_y) else ""
                msg = f"{mods_tag} 點擊 {img_name} @ ({click_x},{click_y}) (conf={m.confidence:.2f} [{m.mode}], scale={m.scale}){off_tag}{hold_tag}"
            else:
                # Fallback 判斷（三個條件皆需 True 才退回座標）：
                #   1. 有錄製座標 (has_coord)
                #   2. allow_coord_fallback：系統層級信心（螢幕解析度跟錄製時相同）
                #   3. cv_coord_fallback：使用者層級意願（panel toggle，預設 On）
                if has_coord and allow_coord_fallback and cv_coord_fallback:
                    logger.warning(f"[computer_use]   ⚠ 圖像比對失敗（{m.reason}），退回錄製座標 ({fx},{fy})")
                    _do_click(pg, int(fx), int(fy), button, clicks, hold_sec, modifiers)
                    hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                    msg = f"[fallback]{mods_tag} 點擊絕對座標 ({fx},{fy}){hold_tag}（原圖 {img_name} 找不到）"
                elif has_coord and not allow_coord_fallback:
                    fail_msg = (f"找不到錨點圖 {img_name}（{m.reason}），且目前螢幕解析度與錄製時不同，"
                        f"絕對座標 ({fx},{fy}) 不可信，請重錄或調整到原螢幕布局")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                elif has_coord and not cv_coord_fallback:
                    fail_msg = (f"找不到錨點圖 {img_name}（{m.reason}），且使用者關閉了「CV 失敗退回座標」。"
                        f"若要容錯請到 panel 打開該 toggle。")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                else:
                    fail_msg = f"找不到錨點圖 {img_name}（{m.reason}），且無 fallback 座標可用"
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)

        elif atype == "click_at":
            x, y = int(action.get("x", 0)), int(action.get("y", 0))
            in_range, layout_info = _point_in_any_screen(x, y)
            if not in_range:
                return ActionResult(False, index, atype,
                    f"座標 ({x},{y}) 超出目前螢幕範圍（{layout_info}）")
            button = action.get("button", "left")
            clicks = int(action.get("clicks", 1))
            hold_sec = float(action.get("hold_sec", 0) or 0)
            modifiers = list(action.get("modifiers", []) or [])
            mods_tag = f"[{'+'.join(modifiers)}]" if modifiers else ""
            _do_click(pg, x, y, button, clicks, hold_sec, modifiers)
            hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
            msg = f"{mods_tag} 點擊絕對座標 ({x}, {y}){hold_tag}"

        elif atype == "type_text":
            text = action.get("text", "")
            if not text:
                return ActionResult(False, index, atype, "type_text 缺 text 欄位")
            # interval 控制打字節奏（每個字之間的間隔秒數）；中文用 write 可能失效，改 copy-paste
            if any(ord(c) > 127 for c in text):
                import pyperclip
                try:
                    pyperclip.copy(text)
                    pg.hotkey("ctrl", "v")
                    msg = f"輸入非 ASCII 文字（clipboard）：{text[:30]}"
                except Exception:
                    # 沒 pyperclip 就 fallback
                    pg.write(text, interval=0.03)
                    msg = f"輸入文字（逐字）：{text[:30]}"
            else:
                pg.write(text, interval=0.03)
                msg = f"輸入文字：{text[:30]}"

        elif atype == "hotkey":
            keys = action.get("keys", [])
            if not keys:
                return ActionResult(False, index, atype, "hotkey 缺 keys 欄位")
            # 單獨按修飾鍵（Shift / Ctrl / Alt / Win）要特別處理：
            # pyautogui.hotkey("shift") 底層用老 API keybd_event，Windows IME 的
            # 中英切換 hotkey 常常觸發不到。改用 pynput（SendInput）並明確拉長
            # press→release 間隔，讓 IME 有時間辨識為「獨立按 tap」。
            _MOD_TO_PYNPUT = {"shift": "shift", "ctrl": "ctrl", "alt": "alt",
                              "win": "cmd", "cmd": "cmd"}
            if len(keys) == 1 and keys[0].lower() in _MOD_TO_PYNPUT:
                from pynput.keyboard import Controller as _KC, Key as _K
                _kc = _KC()
                _pk = getattr(_K, _MOD_TO_PYNPUT[keys[0].lower()])
                _kc.press(_pk)
                time.sleep(0.12)
                _kc.release(_pk)
                msg = f"單按 {keys[0]}（pynput tap，IME-safe）"
            else:
                pg.hotkey(*keys)
                msg = f"熱鍵：{'+'.join(keys)}"

        elif atype == "wait":
            sec = float(action.get("seconds", 0.0))
            # 分段 sleep，中間可以 abort
            total, step = sec, 0.2
            while total > 0:
                _check_abort(run_id)
                time.sleep(min(step, total))
                total -= step
            msg = f"等待 {sec}s"

        elif atype == "wait_image":
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "wait_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 10.0))
            threshold = float(action.get("confidence", 0.85))
            region_rect = _parse_search_region(action)
            deadline = time.time() + timeout
            last_conf = 0.0
            while time.time() < deadline:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if m.found:
                    msg = f"{img_name} 出現（conf={m.confidence:.2f}）"
                    break
                last_conf = max(last_conf, m.confidence)
                time.sleep(0.3)
            else:
                return ActionResult(False, index, atype,
                    f"等待 {timeout}s 仍未出現 {img_name}（最佳 {last_conf:.2f} < {threshold}）")

        elif atype == "drag":
            x1 = int(action.get("x", 0))
            y1 = int(action.get("y", 0))
            x2 = int(action.get("x2", 0))
            y2 = int(action.get("y2", 0))
            button = action.get("button", "left")
            # 起點：預設使用絕對座標；只有使用者切到圖像模式（use_coord=False）才嘗試圖像定位校正
            img_name = action.get("image", "")
            if img_name and action.get("use_coord", True) is False:
                tpl_path = assets_dir / img_name
                # drag 也吃 step 層級 cv_threshold / cv_search_radius
                threshold = float(action.get("confidence") or cv_threshold)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  near_xy=(x1, y1), search_radius=cv_search_radius)
                if m.found:
                    dx = m.center[0] - x1
                    dy_shift = m.center[1] - y1
                    x1, y1 = m.center[0], m.center[1]
                    # 終點同步偏移，保持相對位移
                    x2 += dx
                    y2 += dy_shift
                elif cv_search_only_near:
                    return ActionResult(False, index, atype,
                        f"【只搜附近模式】drag 起點在 ({x1},{y1}) ±{cv_search_radius}px 內找不到錨點 {img_name}")
            # 座標防護：超出螢幕就拒絕執行
            for cx, cy, label in [(x1, y1, "起點"), (x2, y2, "終點")]:
                in_range, layout_info = _point_in_any_screen(cx, cy)
                if not in_range:
                    return ActionResult(False, index, atype,
                        f"拖曳{label}座標 ({cx},{cy}) 超出目前螢幕（{layout_info}）")
            # Windows 的 DragDetect 要求 mouseDown 後第一個 move 必須**嚴格超過 SM_CXDRAG (~4px)**
            # 才觸發真正的 OLE Drag-Drop。pyautogui.dragTo + 平順 lerp 常常第一步 < 4px 就被當
            # 普通點擊。解法：press 前從偏移位置抵達產生「pre-move delta」，press 後立刻做一個
            # 6px 的明顯跳躍突破閾值，再開始平滑 lerp。
            # 參考：https://devblogs.microsoft.com/oldnewthing/20100304-00/?p=14733
            from pynput.mouse import Controller as _MC, Button as _Btn
            _mc = _MC()
            _btn_map = {"left": _Btn.left, "right": _Btn.right, "middle": _Btn.middle}
            _btn = _btn_map.get(button, _Btn.left)
            drag_mods = list(action.get("modifiers", []) or [])
            # 修飾鍵在整個拖曳期間都要按著（Shift+drag=移動、Ctrl+drag=複製）
            for mod in drag_mods:
                pg.keyDown(mod)
            try:
                # 計算單位方向（用來做 6px 初始跨閾值跳躍；若起終點距離 < 6px 就固定往右跳）
                dx = x2 - x1
                dy = y2 - y1
                dist = max(1, (dx * dx + dy * dy) ** 0.5)
                nx, ny = dx / dist, dy / dist

                # 1. 先從偏移位置抵達起點，產生真實的 pre-move event
                _mc.position = (int(x1 - nx * 3), int(y1 - ny * 3))
                time.sleep(0.05)
                _mc.position = (x1, y1)
                time.sleep(0.08)
                # 2. 按下
                _mc.press(_btn)
                time.sleep(0.10)
                # 3. 關鍵：press 後第一個 move 必須 > 4px 突破 SM_CXDRAG
                _mc.position = (int(x1 + nx * 6), int(y1 + ny * 6))
                time.sleep(0.06)
                # 4. 剩餘距離分段平滑移動到終點
                steps = 25
                total_move_sec = 0.6
                for i in range(1, steps + 1):
                    t = i / steps
                    mx = int(x1 + nx * 6 + (x2 - (x1 + nx * 6)) * t)
                    my = int(y1 + ny * 6 + (y2 - (y1 + ny * 6)) * t)
                    _mc.position = (mx, my)
                    time.sleep(total_move_sec / steps)
                # 5. 在終點停頓，讓 drop target highlight 起來再放手
                time.sleep(0.25)
                _mc.release(_btn)
            finally:
                # 即使過程拋例外也要放開修飾鍵，避免使用者鍵盤卡在按下狀態
                for mod in reversed(drag_mods):
                    pg.keyUp(mod)
            mods_tag = f"[{'+'.join(drag_mods)}] " if drag_mods else ""
            msg = f"{mods_tag}拖曳 ({x1},{y1}) → ({x2},{y2}) button={button}"

        elif atype == "scroll":
            x = int(action.get("x", 0))
            y = int(action.get("y", 0))
            dy = int(action.get("dy", 0))
            if dy == 0:
                logger.warning(f"[computer_use]   ⚠ scroll action dy=0，略過（action={action}）")
                return ActionResult(False, index, atype, "scroll 缺 dy 欄位或為 0")
            modifiers = list(action.get("modifiers", []) or [])
            # 座標防護：超出螢幕時不移動滑鼠直接在當前位置捲
            in_range, _ = _point_in_any_screen(x, y)
            if in_range:
                pg.moveTo(x, y)
                # Windows 上滑鼠移入新視窗需要短時間觸發 hover，否則後續 scroll 會被吞掉
                time.sleep(0.15)
            # 用 pynput 取代 pyautogui.scroll（pyautogui 在 Windows 有 known bug）
            from pynput.mouse import Controller as _MC
            _mc = _MC()
            # 按下修飾鍵（Ctrl+滾輪 = 縮放）→ scroll → 放開
            for mod in modifiers:
                pg.keyDown(mod)
            try:
                _mc.scroll(0, dy)
            finally:
                for mod in reversed(modifiers):
                    pg.keyUp(mod)
            mods_tag = f"[{'+'.join(modifiers)}] " if modifiers else ""
            msg = f"{mods_tag}在 ({x},{y}) 捲動 dy={dy}"

        elif atype == "activate_window":
            # 將指定標題的視窗帶到前景。解決錄製回放最常見的失敗原因：
            # 目標視窗在背景 → 點擊被其他視窗截去 or hover 作用在錯的視窗。
            # Linux 下 pygetwindow 支援很薄，用 try/except 吞例外並回 FAIL 讓使用者知情。
            title = (action.get("title") or "").strip()
            title_contains = (action.get("title_contains") or "").strip()
            if not title and not title_contains:
                return ActionResult(False, index, atype,
                    "activate_window 缺 title 或 title_contains 欄位")
            timeout = float(action.get("timeout_sec", 3.0))
            try:
                import pygetwindow as gw
            except Exception as e:
                return ActionResult(False, index, atype,
                    f"pygetwindow 無法載入（此平台可能不支援）：{e}")

            def _find_win():
                try:
                    all_wins = gw.getAllWindows()
                except Exception:
                    return []
                if title:
                    wins = [w for w in all_wins if (w.title or "") == title]
                else:
                    needle = title_contains.lower()
                    wins = [w for w in all_wins if needle in (w.title or "").lower()]
                return [w for w in wins if (w.title or "").strip()]

            deadline = time.time() + timeout
            target = None
            while True:
                _check_abort(run_id)
                matched = _find_win()
                if matched:
                    target = matched[0]
                    break
                if time.time() >= deadline:
                    break
                time.sleep(0.2)

            if target is None:
                needle = title or title_contains
                return ActionResult(False, index, atype,
                    f"{timeout}s 內找不到視窗標題 ~= '{needle}'")

            activated = False
            try:
                # 最小化的視窗必須先 restore 才能被 activate（pygetwindow 已實作這邏輯但不保證）
                if getattr(target, "isMinimized", False):
                    try:
                        target.restore()
                    except Exception:
                        pass
                target.activate()
                activated = True
            except Exception as _gw_err:
                # pygetwindow 在 foreground lock 等情境會拋 PyGetWindowException；改用 Win32 直接搶焦點
                try:
                    import ctypes  # type: ignore
                    hwnd = getattr(target, "_hWnd", None)
                    if hwnd:
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                        activated = True
                except Exception:
                    pass
                if not activated:
                    return ActionResult(False, index, atype,
                        f"找到視窗 '{target.title[:60]}' 但無法 activate：{_gw_err}")
            # 給 Window Manager 時間切換焦點，避免下個動作時視窗還沒完全在前
            time.sleep(0.25)
            msg = f"已將視窗 '{(target.title or '')[:60]}' 切到前景"

        elif atype == "assert_image":
            # 驗證某張錨點圖「當下」必須可見（和 wait_image 相似但語意不同：
            # wait_image 等畫面載入、timeout 較長；assert_image 檢查當前狀態、timeout 較短）。
            # 失敗訊息也更精確，方便排查為什麼流程走到這一步畫面長得不對。
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "assert_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("confidence") or cv_threshold)
            region_rect = _parse_search_region(action)
            deadline = time.time() + timeout
            last_conf = 0.0
            found_m: Optional[MatchResult] = None
            while True:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if m.found:
                    found_m = m
                    break
                last_conf = max(last_conf, m.confidence)
                if time.time() >= deadline:
                    break
                time.sleep(0.2)
            if found_m is None:
                return ActionResult(False, index, atype,
                    f"assert 失敗：{timeout}s 內 {img_name} 未出現（最佳 {last_conf:.2f} < {threshold}）")
            msg = f"assert 通過：{img_name} 可見（conf={found_m.confidence:.2f}）"

        elif atype == "assert_text":
            # OCR 版本的 assert：驗證螢幕上應該有某段文字。
            # 常見用途：登入成功後檢查「歡迎回來」、錯誤訊息檢查、狀態列文字等。
            text = (action.get("text") or action.get("ocr_text") or "").strip()
            if not text:
                return ActionResult(False, index, atype, "assert_text 缺 text 欄位")
            find_text_on_screen = None
            try:
                from pipeline.ocr import find_text_on_screen
            except Exception:
                try:
                    from .ocr import find_text_on_screen  # type: ignore
                except Exception as _e:
                    return ActionResult(False, index, atype, f"無法載入 OCR 模組：{_e}")
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("ocr_threshold") or ocr_threshold)
            # 沿用既有 ocr_box_* 欄位做 OCR 搜尋範圍（藍框）
            _box_w = int(action.get("ocr_box_width", 0) or 0)
            _box_h = int(action.get("ocr_box_height", 0) or 0)
            region = None
            if _box_w > 0 and _box_h > 0:
                region = (
                    int(action.get("ocr_box_left", 0) or 0),
                    int(action.get("ocr_box_top", 0) or 0),
                    _box_w, _box_h,
                )
            deadline = time.time() + timeout
            last_reason = ""
            found_ocr = None
            while True:
                _check_abort(run_id)
                screen_bgr, sx, sy = _capture_screen()
                ocr_res = find_text_on_screen(
                    screen_bgr, text, origin_x=sx, origin_y=sy,
                    lang_tag="zh-Hant-TW",
                    threshold=threshold, region=region,
                )
                if ocr_res.found:
                    found_ocr = ocr_res
                    break
                last_reason = ocr_res.reason
                if time.time() >= deadline:
                    break
                time.sleep(0.3)
            if found_ocr is None:
                return ActionResult(False, index, atype,
                    f"assert 失敗：{timeout}s 內未偵測到文字 '{text}'（{last_reason}）")
            msg = (f"assert 通過：文字 '{text}' 可見 @ {found_ocr.center} "
                   f"(matched='{found_ocr.text[:30]}', conf={found_ocr.confidence:.2f})")

        elif atype == "vlm_action":
            # 自由形式 VLM 動作：沒有錨點圖，用自然語言描述 → VLM 看截圖決定要做哪個 primitive。
            # VLM 輸出受白名單限制，最終仍走現有 pyautogui primitive（不發明新動作）。
            description = (action.get("description") or action.get("vlm_prompt") or "").strip()
            if not description:
                return ActionResult(False, index, atype,
                    "vlm_action 缺 description（自然語言目標必填）")
            vlm_prompt = str(action.get("vlm_prompt", "") or "")
            vlm_model = str(action.get("vlm_model", "") or "")
            region_rect_vlm = _parse_search_region(action)
            allowed = list(vlm_allowed_primitives or [])
            screen_bgr_vlm, vsx, vsy = _capture_screen()
            decision = _vlm_decide_action(
                screen_bgr_vlm, vsx, vsy,
                description=description,
                prompt_extra=vlm_prompt,
                region=region_rect_vlm,
                allowed=allowed,
                model_override=vlm_model,
                logger=logger,
            )
            if not decision.found:
                return ActionResult(False, index, atype,
                    f"VLM 無法決定動作：{decision.reason}")
            # 按 VLM 決定的 primitive 送到既有 primitive 執行路徑
            args = decision.primitive_args or {}
            prim = decision.primitive
            if prim == "click":
                _do_click(pg, args["x"], args["y"],
                          args.get("button", "left"), int(args.get("clicks", 1)), 0.0, [])
            elif prim == "double_click":
                _do_click(pg, args["x"], args["y"], "left", 2, 0.0, [])
            elif prim == "right_click":
                _do_click(pg, args["x"], args["y"], "right", 1, 0.0, [])
            elif prim == "type_text":
                txt = args["text"]
                if any(ord(c) > 127 for c in txt):
                    import pyperclip
                    try:
                        pyperclip.copy(txt)
                        pg.hotkey("ctrl", "v")
                    except Exception:
                        pg.write(txt, interval=0.03)
                else:
                    pg.write(txt, interval=0.03)
            elif prim == "hotkey":
                pg.hotkey(*args["keys"])
            elif prim == "drag":
                # 簡化的 drag：先 moveTo 起點、mouseDown → moveTo 終點 → mouseUp。
                # 不重現 click_image 那套 OLE DragDetect 路徑（那是給錄製流程的，VLM 互動場景少見）。
                pg.moveTo(args["x"], args["y"])
                pg.mouseDown(button=args.get("button", "left"))
                try:
                    pg.moveTo(args["x2"], args["y2"], duration=0.5)
                finally:
                    pg.mouseUp(button=args.get("button", "left"))
            else:
                return ActionResult(False, index, atype,
                    f"VLM 回傳未支援的 primitive：{prim}")
            msg = (f"VLM 決定 '{prim}' @ conf={decision.confidence:.2f}"
                   f"（reason：{decision.reason[:80]}）")

        elif atype == "screenshot":
            import cv2
            img, _ox, _oy = _capture_screen()
            ts = int(time.time())
            out = assets_dir / f"debug_screenshot_{ts}.png"
            # 用 imencode + write_bytes 避免中文路徑問題
            ok, buf = cv2.imencode(".png", img)
            if ok:
                out.write_bytes(buf.tobytes())
                msg = f"已存 screenshot：{out.name}"
            else:
                msg = "screenshot imencode 失敗"

        else:
            return ActionResult(False, index, atype, f"未知動作類型：{atype}")

        duration = int((time.time() - t0) * 1000)
        logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
        return ActionResult(True, index, atype, msg, duration)

    except RuntimeError as e:
        # abort signal
        raise
    except Exception as e:
        # pyautogui.FailSafeException / 其他意外
        import traceback
        logger.error(f"[computer_use]   ✗ {atype} 失敗：{e}")
        logger.debug(traceback.format_exc())
        return ActionResult(False, index, atype, f"{type(e).__name__}: {e}",
                            int((time.time() - t0) * 1000))


# ── 對外入口：執行一整個 computer_use 步驟 ─────────────────────────

@dataclass
class StepResult:
    success: bool
    total_actions: int
    succeeded: int
    failed_at: int = -1        # 首次失敗的 index；-1 = 全部成功
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


MAX_ACTIONS_PER_STEP = 500  # 單步動作數上限，防止失控腳本無限循環


def validate_action_assets(actions: list[dict], assets_dir: Path) -> list[str]:
    """Preflight：掃一遍 actions 裡引用到的所有錨點圖是否存在。
    提早 FAIL 比回放跑到一半才發現圖不見好太多，也讓使用者錯誤訊息更集中。
    回傳缺失檔名 list（保留順序、去重）。"""
    missing: list[str] = []
    seen: set[str] = set()
    for a in actions:
        for key in ("image", "image2"):
            name = a.get(key) or ""
            if not name or name in seen:
                continue
            seen.add(name)
            if not (assets_dir / name).is_file():
                missing.append(name)
    return missing


def _screen_layout_match(meta_path: Path, logger: logging.Logger) -> bool:
    """比對錄製時與回放時的螢幕解析度。
    True = 一致（絕對座標 fallback 仍可靠）；False = 已改變（座標 fallback 不可信，應禁用）"""
    if not meta_path.is_file():
        return True  # 沒 meta 就寬容處理
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rec_w, rec_h = meta.get("screen_width"), meta.get("screen_height")
        if not rec_w or not rec_h:
            return True
        import mss
        with mss.mss() as sct:
            cur = sct.monitors[1]
        if cur["width"] == rec_w and cur["height"] == rec_h:
            return True
        logger.warning(
            f"[computer_use] ⚠ 螢幕解析度變了："
            f"錄製 {rec_w}×{rec_h} → 目前 {cur['width']}×{cur['height']}；"
            f"將禁用絕對座標 fallback，強制圖像比對（常見於接/拔外接螢幕後）"
        )
        return False
    except Exception as e:
        logger.warning(f"[computer_use] 讀 meta.json 失敗：{e}")
        return True


def execute_computer_use_step(
    actions: list[dict],
    assets_dir: str,
    logger: logging.Logger,
    run_id: Optional[str] = None,
    fail_fast: bool = True,
    cv_threshold: float = 0.65,
    cv_search_only_near: bool = False,
    cv_search_radius: int = 400,
    cv_trigger_hover: bool = True,
    cv_hover_wait_ms: int = 200,
    cv_coord_fallback: bool = False,
    ocr_threshold: float = 0.6,
    ocr_cv_fallback: bool = False,
    vlm_allowed_primitives: Optional[list[str]] = None,
) -> StepResult:
    """執行一整個 computer_use 步驟。

    - actions: ComputerUseAction 物件的 list of dict
    - assets_dir: 錨點圖片資料夾（絕對路徑，通常是 ai_output/<name>/ 下的子資料夾）
    - fail_fast: True 則遇到失敗立刻中止；False 則繼續但記錄失敗數
    - cv_threshold: CV 比對門檻（0.65 寬鬆 / 0.80 標準 / 0.90 嚴格）
    - cv_search_only_near: True = 只搜錄製座標附近、找不到直接 FAIL（不退回全螢幕也不退回座標）
    - cv_search_radius: 附近搜尋半徑（像素）；實際搜尋範圍 (2r × 2r)
    - cv_trigger_hover: True = 比對前先 moveTo(錄製座標) + 200ms 讓 Windows hover 效果出現
    """
    import json  # 供 _screen_layout_match 讀 meta.json
    clear_abort(run_id or "")
    if len(actions) > MAX_ACTIONS_PER_STEP:
        return StepResult(
            success=False,
            total_actions=len(actions),
            succeeded=0,
            failed_at=-1,
            stdout="",
            stderr=f"動作數 {len(actions)} 超過安全上限 {MAX_ACTIONS_PER_STEP}，拒絕執行",
            exit_code=2,
        )
    assets = Path(assets_dir)
    if not assets.is_dir():
        # 沒有 assets 目錄也可能 OK（例如只有 type_text / wait），不直接失敗
        logger.warning(f"[computer_use] assets 目錄不存在：{assets_dir}")
    else:
        # 錨點圖 preflight：避免跑到一半才發現圖不見
        missing_imgs = validate_action_assets(actions, assets)
        if missing_imgs:
            preview = ", ".join(missing_imgs[:5])
            more = f"...（共 {len(missing_imgs)} 張）" if len(missing_imgs) > 5 else ""
            return StepResult(
                success=False,
                total_actions=len(actions),
                succeeded=0,
                failed_at=0,
                stdout="",
                stderr=f"preflight 失敗：assets_dir 缺少錨點圖：{preview}{more}",
                exit_code=2,
            )

    # 螢幕解析度比對：若改變（接/拔外接螢幕）就禁用座標 fallback
    layout_ok = _screen_layout_match(assets / "meta.json", logger) if assets.is_dir() else True

    logger.info(f"[computer_use] ▶ 開始執行 {len(actions)} 個動作 "
                f"（assets: {assets_dir}, fail_fast={fail_fast}）")
    logger.info(f"[computer_use] 🛡 Safety: 滑鼠移到螢幕左上角 (0,0) 可立即中止")

    succeeded = 0
    failed_at = -1
    messages: list[str] = []

    for i, action in enumerate(actions):
        try:
            res = execute_action(action, assets, i, logger, run_id,
                                 allow_coord_fallback=layout_ok,
                                 cv_threshold=cv_threshold,
                                 cv_search_only_near=cv_search_only_near,
                                 cv_search_radius=cv_search_radius,
                                 cv_trigger_hover=cv_trigger_hover,
                                 cv_hover_wait_ms=cv_hover_wait_ms,
                                 cv_coord_fallback=cv_coord_fallback,
                                 ocr_threshold=ocr_threshold,
                                 ocr_cv_fallback=ocr_cv_fallback,
                                 vlm_allowed_primitives=vlm_allowed_primitives)
        except RuntimeError as abort_err:
            logger.warning(f"[computer_use] {abort_err}")
            return StepResult(
                success=False,
                total_actions=len(actions),
                succeeded=succeeded,
                failed_at=i,
                stdout="\n".join(messages),
                stderr=str(abort_err),
                exit_code=130,  # SIGINT-ish
            )
        messages.append(f"#{i+1} [{res.action_type}] {'OK' if res.ok else 'FAIL'}: {res.message}")
        if res.ok:
            succeeded += 1
        else:
            if failed_at < 0:
                failed_at = i
            if fail_fast:
                return StepResult(
                    success=False,
                    total_actions=len(actions),
                    succeeded=succeeded,
                    failed_at=i,
                    stdout="\n".join(messages),
                    stderr=f"動作 #{i + 1} ({res.action_type}) 失敗：{res.message}",
                    exit_code=1,
                )

    all_ok = (failed_at < 0)
    logger.info(f"[computer_use] ■ 結束：{succeeded}/{len(actions)} 成功")
    return StepResult(
        success=all_ok,
        total_actions=len(actions),
        succeeded=succeeded,
        failed_at=failed_at,
        stdout="\n".join(messages),
        stderr="" if all_ok else f"失敗動作數：{len(actions) - succeeded}",
        exit_code=0 if all_ok else 1,
    )
