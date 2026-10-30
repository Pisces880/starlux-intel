import os
import json
import time
import hashlib
import urllib.parse
from datetime import datetime, timedelta
import feedparser
from thefuzz import fuzz
from pydantic import BaseModel, Field
from typing import List, Optional
from google import genai
from google.genai import types

# ==========================================
# 0. 路徑與全局設定
# ==========================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

# 歷史回溯終點與每次回推天數
BACKFILL_STOP_DATE = "2025-09-01"
BACKFILL_STEP_DAYS = 30

# 使用的模型名稱 (依據 AI_CONTEXT 規範設定，並具備自動容錯備援)
PRIMARY_MODEL = "gemini-2.5-flash" 

# ==========================================
# 1. Pydantic 結構化輸出 Schema 定義
# ==========================================
class NewsItemOutput(BaseModel):
    is_relevant: bool = Field(description="是否與星宇航空或航空產業實質相關？若為無關之純政治口水、娛樂八卦或同名無關事物請填 False")
    category: str = Field(description="分類代號：cat1(文化理念/高層談話/機隊航網異動), cat2(社群討論/培訓機師/Dcard/PTT話題), cat3(公司經營/營收財報/法說會/增資租機), cat4(一般新聞稿/國際航空外電)")
    title: str = Field(description="繁體中文標題（若為英文外電請翻譯為專業繁體中文標題，並將同事件多篇報導合併為單一精準標題）")
    date: str = Field(description="新聞日期 YYYY-MM-DD")
    source: str = Field(description="媒體或社群來源名稱")
    url: str = Field(description="原始新聞連結")
    summary: str = Field(description="繁體中文重點摘要（約 60-120 字，條理分明，提煉對培訓機師面試有價值的商業或營運細節）")
    tags: List[str] = Field(description="2到4個關鍵字標籤，例如：['機隊擴張', 'A350', '北美航網']")
    bull_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🟢正方公司派觀點；一般新聞填空字串")
    bear_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🔴反方市場派隱憂；一般新聞填空字串")
    pilot_view: Optional[str] = Field(default="", description="✈️培訓機師面試官視角：這則情報對飛行員人力需求、安全文化、航線派遣或面試回答的啟示")

class TimelineLogItem(BaseModel):
    date: str = Field(description="事件日期 YYYY-MM-DD 或 YYYY-MM")
    type: str = Field(description="異動類型：機隊異動 / 航網開拓 / 資本財務 / 策略合作")
    title: str = Field(description="簡短事件標題，例如：第 8 架 A350-900 交機")
    detail: str = Field(description="具體細節說明，包含對運能或財務的影響")

class FinanceLogItem(BaseModel):
    period: str = Field(description="財務期間或日期，例如：2026年9月營收 或 2026 Q2 財報 或 2026-08 重訊")
    event_type: str = Field(description="月營收 / 季財報 / 重大財務操作")
    headline: str = Field(description="核心數據標題，例如：單月營收 31.2 億元，年增 45%")
    metrics: str = Field(description="關鍵財務數據摘要（如 YoY、MoM、EPS、毛利率或租機金額）")
    bull_view: str = Field(description="🟢正方公司派觀點（成長動能、規模經濟、航網效益）")
    bear_view: str = Field(description="🔴反方市場派隱憂（折舊攤提壓力、油價匯率風險、高負債比或票價回歸正常化）")
    pilot_view: str = Field(description="✈️培訓機師面試官視角（如何展現對公司財務穩健度與擴張節奏的客觀認知）")

class BatchAIResponse(BaseModel):
    processed_news: List[NewsItemOutput] = Field(description="過濾並合併後的有效新聞清單")
    new_timeline_logs: List[TimelineLogItem] = Field(description="從本次新聞中偵測到的新機隊交機、新開航線、重要合作夥伴或重大資本異動（若無則回傳空陣列）")
    new_finance_logs: List[FinanceLogItem] = Field(description="從本次新聞中偵測到的新月營收公布、季財報、法說會或售後租回/增資重訊（若無則回傳空陣列）")

class MonthlyAuditResponse(BaseModel):
    philosophy: str = Field(description="企業核心理念與安全文化摘要")
    fleet_summary: str = Field(description="最新機隊總架數、機型組成（A321neo, A330neo, A350, A350F貨機訂單）與自有/租賃結構說明")
    route_summary: str = Field(description="最新航網佈局（亞洲區域航線、北美長程航線航點清單與轉機策略）")
    partners: str = Field(description="重要策略合作夥伴（如阿拉斯加航空、申請加入 Oneworld 寰宇一家進度等）")

# ==========================================
# 2. 輔助函式：讀寫 JSON 與產生短碼 ID
# ==========================================
def make_short_id(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]

def load_json(filepath: str, default_data: dict) -> dict:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 讀取 {filepath} 失敗，使用預設結構: {e}")
    return default_data

def save_json(filepath: str, data: dict):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==========================================
# 3. RSS 抓取與第一道防呆去重 (thefuzz + MD5)
# ==========================================
def build_rss_urls(after_date: str = None, before_date: str = None) -> List[dict]:
    time_filter = ""
    if after_date and before_date:
        time_filter = f" after:{after_date} before:{before_date}"

    queries = [
        # 1. 經營、財報、月營收、重訊
        {"hint": "cat3", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'("星宇航空" OR "2646") AND ("營收" OR "財報" OR "法說會" OR "增資" OR "售後租回" OR "交機" OR "張國煒"){time_filter}'},
        # 2. 社群話題、培訓機師、航線體驗
        {"hint": "cat2", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'("星宇航空" OR "星宇") AND (site:dcard.tw OR site:ptt.cc OR "培訓機師" OR "面試" OR "年終" OR "開航"){time_filter}'},
        # 3. 一般台灣航空新聞稿
        {"hint": "cat4", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'"星宇航空" -政論 -名嘴 -選舉{time_filter}'},
        # 4. 國際英文外電
        {"hint": "cat4", "hl": "en-US", "gl": "US", "ceid": "US:en",
         "q": f'"STARLUX Airlines"{time_filter}'}
    ]

    rss_list = []
    for item in queries:
        encoded_q = urllib.parse.quote(item["q"])
        url = f"https://news.google.com/rss/search?q={encoded_q}&hl={item['hl']}&gl={item['gl']}&ceid={item['ceid']}"
        rss_list.append({"url": url, "hint": item["hint"]})
    return rss_list

def fetch_raw_candidates(rss_configs: List[dict], history_data: dict, max_items: int = 18) -> List[dict]:
    seen_ids = set(history_data.get("seen_ids", []))
    seen_titles = history_data.get("seen_titles", [])
    candidates = []

    for cfg in rss_configs:
        try:
            feed = feedparser.parse(cfg["url"])
            for entry in feed.entries[:10]:
                raw_title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not raw_title or not link:
                    continue

                # 移除 Google News 標題尾巴的「 - 媒體名稱」以利精準比對
                clean_title = raw_title.rsplit(" - ", 1)[0].strip()
                source_name = raw_title.rsplit(" - ", 1)[1].strip() if " - " in raw_title else "新聞媒體"

                # 日期解析
                pub_date = datetime.now().strftime("%Y-%m-%d")
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = time.strftime("%Y-%m-%d", entry.published_parsed)

                item_id = make_short_id(clean_title)
                if item_id in seen_ids:
                    continue

                # thefuzz 模糊比對：若與歷史標題相似度 >= 80 則視為重複
                is_dup = False
                for old_title in seen_titles[-300:]:
                    if fuzz.token_set_ratio(clean_title, old_title) >= 80:
                        is_dup = True
                        break
                if is_dup:
                    continue

                # 通過第一關，加入候選並寫入暫存記憶
                seen_ids.add(item_id)
                seen_titles.append(clean_title)
                candidates.append({
                    "id": item_id,
                    "title": clean_title,
                    "source": source_name,
                    "url": link,
                    "date": pub_date,
                    "hint": cfg["hint"]
                })

                if len(candidates) >= max_items:
                    break
        except Exception as e:
            print(f"⚠️ RSS 抓取略過異常 ({cfg['hint']}): {e}")
        if len(candidates) >= max_items:
            break

    # 更新 history_data (保留最近 1000 筆避免無限膨脹)
    history_data["seen_ids"] = list(seen_ids)[-1000:]
    history_data["seen_titles"] = seen_titles[-1000:]
    return candidates

# ==========================================
# 4. Gemini AI 批次分析、語意去重與正反觀點生成
# ==========================================
def analyze_with_gemini(client: genai.Client, candidates: List[dict]) -> Optional[BatchAIResponse]:
    if not candidates:
        return None

    prompt = f"""
你是一位專業的「航空產業分析師」與「星宇航空（STARLUX Airlines）培訓機師面試教練」。
請審閱以下抓取到的原始新聞清單，並嚴格執行以下任務：

1. **雜訊過濾 (`is_relevant`)**：
   - 若新聞與星宇航空、航空產業、飛安、機隊、航線、財報、旅遊運能無實質關聯（例如：純政治口水、無關名嘴政論、娛樂八卦），請將 `is_relevant` 設為 `false`。
2. **語意去重與外電翻譯**：
   - 若清單中有多篇新聞在講「同一個事件」（例如多家媒體同時報導 9 月營收或同一架新機交付），請只保留最完整的一筆（其餘相同事件請將 `is_relevant` 設為 `false`），並整合資訊。
   - 若為英文外電，請自動翻譯為台灣航空業慣用的繁體中文術語（如：窄體客機、廣體客機、載客率 Load Factor、單位收益 Yield）。
3. **精準分類 (`category`)**：
   - `cat1`：公司文化、張國煒/高層談話、企業理念、機隊交機、新航點開闢、國際航空聯盟/夥伴合作。
   - `cat2`：社群討論（Dcard/PTT）、培訓機師考情、員工福利、乘客體驗話題。
   - `cat3`：公司經營面、每月營收、季度財報、法說會、增資、發債、售後租回（SLB）等商業操作。
   - `cat4`：一般新聞稿、促銷活動、聯名行銷、國際航空外電。
4. **重大商業與財務操作之「三欄視角判讀」**：
   - 針對 `cat3`（財報/營收/租機/增資）或重大機隊航網決策，請務必提供：
     - `bull_view`（🟢正方公司派觀點：長程線佈局效益、機隊年輕省油、轉機客成長等）
     - `bear_view`（🔴反方市場派隱憂：初期折舊與利息成本高、北美線競爭激烈、票價紅利消退風險等）
     - `pilot_view`（✈️培訓機師面試官視角：這代表公司需要什麼特質的機師？面試被問到這題時，如何展現「既認同公司願景、又具備風險意識與商業素養」的回答方向？）
5. **自動擷取基本盤與財務時間軸異動 (`new_timeline_logs` & `new_finance_logs`)**：
   - 若新聞中包含具體的「新機交付、新航線開航/增班、重要結盟、資本增資」，請同時提煉至 `new_timeline_logs`。
   - 若新聞中包含具體的「某月營收數據、某季財報數據、重大財務租機/發債」，請同時提煉至 `new_finance_logs`。

原始候選新聞 JSON 如下：
{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""

    for model_name in [PRIMARY_MODEL, "gemini-2.5-flash", "gemini-2.0-flash"]:
        try:
            print(f"🤖 呼叫 Gemini 模型 ({model_name}) 進行批次語意分析...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=BatchAIResponse,
                    temperature=0.2,
                ),
            )
            time.sleep(5)  # 遵守免費額度保護規範
            return BatchAIResponse.model_validate_json(response.text)
        except Exception as e:
            print(f"⚠️ 模型 {model_name} 呼叫異常，嘗試備援或略過: {e}")
            time.sleep(5)
    return None

# ==========================================
# 5. 每月 1 號定期對帳 (Google Search Grounding)
# ==========================================
def monthly_grounding_audit(client: genai.Client, company_obj: dict):
    today = datetime.now()
    force_audit = os.environ.get("FORCE_MONTHLY_AUDIT", "false").lower() == "true"
    
    # 僅在每月 1 號或手動強制觸發時執行
    if today.day != 1 and not force_audit:
        return

    print("🔍 [每月 1 號定期對帳] 啟動 Google Search Grounding 核對星宇航空最新基本盤...")
    try:
        audit_prompt = """
請透過 Google Search 搜尋星宇航空（STARLUX Airlines）官方網站、維基百科與民航局最新公開資訊，
核對並整理截至目前最新的：
1. 核心經營理念與飛安文化 (philosophy)
2. 最新營運中機隊總架數、各機型（A321neo、A330-900neo、A350-900、A350-1000、A350F貨機）架數與自有/租賃結構 (fleet_summary)
3. 最新營運航點清單（包含日本、東南亞、港澳及北美洛杉磯、舊金山、西雅圖等長程航網佈局）(route_summary)
4. 重要策略合作夥伴與航空聯盟申請進度（如阿拉斯加航空、寰宇一家 Oneworld 等）(partners)

請直接輸出純 JSON 格式，包含以下四個字串欄位：
{"philosophy": "...", "fleet_summary": "...", "route_summary": "...", "partners": "..."}
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=audit_prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        time.sleep(5)
        
        # 從回傳文字中擷取 JSON 區塊
        raw_text = response.text.strip()
        if "```json" in raw_text:
            raw_text = raw_text.split("```json")[1].split("```")[0].strip()
        elif "```" in raw_text:
            raw_text = raw_text.split("```")[1].split("```")[0].strip()
            
        audit_data = json.loads(raw_text)
        profile = company_obj.setdefault("company_profile", {})
        for key in ["philosophy", "fleet_summary", "route_summary", "partners"]:
            if audit_data.get(key):
                profile[key] = audit_data[key]
        profile["last_audit_date"] = today.strftime("%Y-%m-%d")
        print("✅ [每月 1 號定期對帳] 企業基本盤更新完成！")
    except Exception as e:
        print(f"🛡️ [防呆保護啟動] 每月對帳搜尋發生異常（已安全略過，不影響主程式運作）: {e}")

# ==========================================
# 6. 資料合併與結構同步 (確保前端 index.html 完美讀取)
# ==========================================
def merge_results_into_data(company_obj: dict, ai_result: BatchAIResponse):
    if not ai_result:
        return

    categories = company_obj.setdefault("categories", {"cat1": [], "cat2": [], "cat3": [], "cat4": []})
    
    # 1. 寫入分類新聞
    added_count = 0
    for item in ai_result.processed_news:
        if not item.is_relevant:
            continue
        cat_key = item.category if item.category in categories else "cat4"
        news_dict = {
            "id": make_short_id(item.title),
            "title": item.title,
            "date": item.date,
            "source": item.source,
            "url": item.url,
            "summary": item.summary,
            "tags": item.tags,
            "bull_view": item.bull_view or "",
            "bear_view": item.bear_view or "",
            "pilot_view": item.pilot_view or ""
        }
        # 避免前端列表重複
        existing_titles = {x.get("title") for x in categories[cat_key]}
        if news_dict["title"] not in existing_titles:
            categories[cat_key].append(news_dict)
            added_count += 1

    # 依日期由新到舊排序各分類新聞，每個分類最多保留 80 筆保持輕量
    for cat_key in categories:
        categories[cat_key].sort(key=lambda x: x.get("date", ""), reverse=True)
        categories[cat_key] = categories[cat_key][:80]

    # 2. 更新企業基本盤近 5 筆異動時間軸 (company_profile.timeline_logs)
    profile = company_obj.setdefault("company_profile", {})
    timeline_logs = profile.setdefault("timeline_logs", [])
    existing_tl_titles = {x.get("title") for x in timeline_logs}
    for tl in ai_result.new_timeline_logs:
        if tl.title not in existing_tl_titles:
            timeline_logs.append(tl.model_dump())
            existing_tl_titles.add(tl.title)
    timeline_logs.sort(key=lambda x: x.get("date", ""), reverse=True)
    profile["timeline_logs"] = timeline_logs[:5]  # 嚴格維持最新 5 筆

    # 3. 更新財務統計趨勢與正反觀點專區 (finance_overview)
    fin_overview = company_obj.setdefault("finance_overview", {})
    fin_logs = fin_overview.setdefault("history_logs", [])
    existing_fin_periods = {x.get("headline") for x in fin_logs}
    for fl in ai_result.new_finance_logs:
        if fl.headline not in existing_fin_periods:
            fin_logs.append(fl.model_dump())
            existing_fin_periods.add(fl.headline)
    fin_logs.sort(key=lambda x: x.get("period", ""), reverse=True)
    fin_overview["history_logs"] = fin_logs[:24]  # 保留近兩年歷月紀錄

    print(f"📥 本批次成功寫入 {added_count} 則精選情報！")

# ==========================================
# 7. 主程式執行流程 (日常更新 + 歷史回溯雙引擎)
# ==========================================
def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ 找不到 GEMINI_API_KEY 環境變數！請確認已在 GitHub Secrets 或本地環境設定。")

    client = genai.Client(api_key=api_key)

    # 載入 data.json 與 history.json
    default_data = {
        "last_updated": "",
        "companies": [
            {
                "id": "starlux",
                "name": "星宇航空 STARLUX",
                "company_profile": {
                    "philosophy": "以「安全」為最高指導原則，打造精品航空體驗，立志成為台灣連結北美與亞洲的高端轉機樞紐。",
                    "fleet_summary": "全空巴（Airbus）機隊配置：A321neo（區域主力）、A330-900neo（中長程區域高運量）、A350-900 / A350-1000（北美長程旗艦）及 A350F 貨機訂單，兼顧自有與租賃彈性。",
                    "route_summary": "深耕日本一二線城市與東南亞航網，並開拓洛杉磯、舊金山、西雅圖等北美長程航點，發展亞洲經桃園轉機赴美之六航權業務。",
                    "partners": "與阿拉斯加航空（Alaska Airlines）深度代碼共享，並積極爭取加入寰宇一家（Oneworld）航空聯盟。",
                    "timeline_logs": []
                },
                "finance_overview": {
                    "summary": "追蹤每月 10 日營收公告、每季財報與重大資本/機隊租賃操作。",
                    "history_logs": []
                },
                "categories": {"cat1": [], "cat2": [], "cat3": [], "cat4": []}
            }
        ]
    }
    data = load_json(DATA_FILE, default_data)
    
    # 確保相容舊版或單一物件結構
    if "companies" in data and isinstance(data["companies"], list) and len(data["companies"]) > 0:
        starlux_obj = data["companies"][0]
    else:
        starlux_obj = data

    default_history = {
        "backfill_cursor": datetime.now().strftime("%Y-%m-%d"),
        "seen_ids": [],
        "seen_titles": []
    }
    history = load_json(HISTORY_FILE, default_history)

    # --- 階段 A：抓取今日最新情報 ---
    print("🚀 [階段 A] 開始抓取最新即時情報...")
    latest_rss = build_rss_urls()
    latest_candidates = fetch_raw_candidates(latest_rss, history, max_items=15)
    if latest_candidates:
        ai_res_latest = analyze_with_gemini(client, latest_candidates)
        merge_results_into_data(starlux_obj, ai_res_latest)
    else:
        print("ℹ️ 今日無新增之未讀即時新聞。")

    # --- 階段 B：漸進式歷史回溯 (Backfill 至 2025-09-01) ---
    cursor_str = history.get("backfill_cursor", datetime.now().strftime("%Y-%m-%d"))
    cursor_dt = datetime.strptime(cursor_str, "%Y-%m-%d")
    stop_dt = datetime.strptime(BACKFILL_STOP_DATE, "%Y-%m-%d")

    if cursor_dt > stop_dt:
        start_dt = max(stop_dt, cursor_dt - timedelta(days=BACKFILL_STEP_DAYS))
        after_str = start_dt.strftime("%Y-%m-%d")
        before_str = cursor_dt.strftime("%Y-%m-%d")
        print(f"⏪ [階段 B：歷史回溯] 正在回補區間：{after_str} ~ {before_str} ...")

        backfill_rss = build_rss_urls(after_date=after_str, before_date=before_str)
        backfill_candidates = fetch_raw_candidates(backfill_rss, history, max_items=25)
        if backfill_candidates:
            ai_res_backfill = analyze_with_gemini(client, backfill_candidates)
            merge_results_into_data(starlux_obj, ai_res_backfill)
        else:
            print(f"ℹ️ 歷史區間 {after_str} ~ {before_str} 無額外新情報。")

        # 推進游標並儲存
        history["backfill_cursor"] = after_str
        print(f"📌 歷史回溯游標已推進至：{after_str}（目標終點：{BACKFILL_STOP_DATE}）")
    else:
        print(f"🎉 歷史回溯已抵達終點 ({BACKFILL_STOP_DATE})，無須再回推！")

    # --- 階段 C：每月 1 號 Google Search 對帳 ---
    monthly_grounding_audit(client, starlux_obj)

    # --- 階段 D：儲存結果 ---
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["last_updated"] = now_str
    starlux_obj["last_updated"] = now_str

    save_json(DATA_FILE, data)
    save_json(HISTORY_FILE, history)
    print(f"✅ 全部任務執行完畢！資料已寫入 data.json 與 history.json ({now_str})")

if __name__ == "__main__":
    main()