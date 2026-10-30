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

# 使用的模型名稱
PRIMARY_MODEL = "gemini-2.5-flash"

# 航空核心白名單與雜訊黑名單 (Milestone 5 硬性過濾門檻)
AVIATION_WHITELIST = [
    "星宇", "starlux", "航空", "機師", "飛行員", "培訓", "張國煒", "k董",
    "機隊", "交機", "航線", "開航", "航點", "轉機", "桃機", "機場", "民航",
    "a321", "a330", "a350", "airbus", "空巴", "波音", "boeing",
    "載客率", "票價", "哩程", "貴賓室", "空服", "地勤", "機務", "維修",
    "寰宇一家", "oneworld", "阿提哈德", "阿拉斯加航空", "2646", "法說", "售後租回"
]

NOISE_BLACKLIST = [
    "與航空業無關", "與航空無關", "不涉及民航", "不具參考效益", "不具面試參考",
    "無直接關聯", "並無相關性", "純屬外部政論", "政論節目", "政論直播",
    "味全龍", "中華職棒", "職棒", "中職", "總冠軍", "台灣大賽", "啦啦隊", "小龍女",
    "蔣萬安", "選戰", "選舉", "造勢", "民調", "立法院", "議員", "立委", "罷免", "八卦"
]

CATEGORY_META = {
    "cat1": {
        "name": "1.公司文化與高層談話",
        "overall_summary": "聚焦星宇航空「Safety is our attitude」飛安文化、張國煒董事長經營哲學、全空巴機隊擴張、北美與亞洲航網佈局及國際策略合作。"
    },
    "cat2": {
        "name": "2.社群討論與培訓考情",
        "overall_summary": "彙整 Dcard、PTT Aviation 版與航空社群針對星宇培訓機師面試準備、航線搭乘體驗、機隊服務與職場文化之實質討論（已嚴格過濾非航空雜訊）。"
    },
    "cat3": {
        "name": "3.公司經營與財報走向",
        "overall_summary": "追蹤每月營收、季度財報、法說會展望，以及新機售後租回（SLB）、現金增資、可轉債等重資產擴張期之關鍵財務操作。"
    },
    "cat4": {
        "name": "4.相關新聞稿與國際外電",
        "overall_summary": "收錄國內外媒體新聞稿與英文航空外電（自動翻譯繁中），掌握星宇航空國際能見度、跨界行銷與全球航空產業動態。"
    }
}

# ==========================================
# 1. Pydantic 結構化輸出 Schema 定義
# ==========================================
class NewsItemOutput(BaseModel):
    is_relevant: bool = Field(description="是否與星宇航空或航空產業實質相關？若為政治選舉、職棒體育、娛樂八卦或無關事物請務必填 False")
    category: str = Field(description="分類代號：cat1(文化理念/高層談話/機隊航網異動), cat2(社群討論/培訓機師/Dcard/PTT話題), cat3(公司經營/營收財報/法說會/增資租機), cat4(一般新聞稿/國際航空外電)")
    title: str = Field(description="繁體中文標題（若為英文外電請翻譯為專業繁體中文標題，並將同事件多篇報導合併為單一精準標題）")
    date: str = Field(description="新聞日期 YYYY-MM-DD")
    source: str = Field(description="媒體或社群來源名稱")
    url: str = Field(description="原始新聞連結")
    summary: str = Field(description="繁體中文重點摘要（約 60-120 字，條理分明，提煉對培訓機師面試有價值的商業或營運細節。切勿收錄無關新聞並在摘要中寫與航空無關）")
    tags: List[str] = Field(description="2到4個關鍵字標籤，例如：['機隊擴張', 'A350', '北美航網']")
    bull_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🟢正方公司派觀點；一般新聞填空字串")
    bear_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🔴反方市場派隱憂；一般新聞填空字串")
    pilot_view: Optional[str] = Field(default="", description="✈️培訓機師面試官視角：這則情報對飛行員人力需求、安全文化、航線派遣或面試回答的啟示")

class TimelineLogItem(BaseModel):
    date: str = Field(description="事件日期 YYYY-MM-DD 或 YYYY-MM")
    type: str = Field(description="異動類型：機隊異動 / 航網開拓 / 資本財務 / 策略合作")
    title: str = Field(description="簡短事件標題，例如：第 8 架 A350-900 交機")
    detail: str = Field(description="具體細節說明，包含對運能或財務的影響")
    source: Optional[str] = Field(default="新聞動態", description="消息來源媒體名稱，例如：經濟日報、公開資訊觀測站")
    url: Optional[str] = Field(default="", description="對應的原始新聞連結 URL（請務必從候選新聞中帶入對應的 url）")

class FinanceLogItem(BaseModel):
    period: str = Field(description="財務期間或日期，例如：2026-09 或 2026 Q2")
    event_type: str = Field(description="月營收 / 季財報 / 重大財務操作")
    headline: str = Field(description="核心數據標題，例如：單月營收 31.2 億元，年增 45%")
    metrics: str = Field(description="關鍵財務數據摘要（如 YoY、MoM、EPS、毛利率或租機金額）")
    bull_view: str = Field(description="🟢正方公司派觀點（成長動能、規模經濟、航網效益）")
    bear_view: str = Field(description="🔴反方市場派隱憂（折舊攤提壓力、油價匯率風險、高負債比或票價回歸正常化）")
    pilot_view: str = Field(description="✈️培訓機師面試官視角（如何展現對公司財務穩健度與擴張節奏的客觀認知）")
    url: Optional[str] = Field(default="", description="對應的原始新聞連結 URL")

class BatchAIResponse(BaseModel):
    processed_news: List[NewsItemOutput] = Field(description="過濾並合併後的有效新聞清單（僅保留與航空相關者）")
    new_timeline_logs: List[TimelineLogItem] = Field(description="從本次新聞中偵測到的新機隊交機、新開航線、重要合作夥伴或重大資本異動（若無則回傳空陣列）")
    new_finance_logs: List[FinanceLogItem] = Field(description="從本次新聞中偵測到的新月營收公布、季財報、法說會或售後租回/增資重訊（若無則回傳空陣列）")

# ==========================================
# 2. 輔助函式與雜訊檢驗邏輯
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

def is_clean_aviation_text(title: str, summary: str = "") -> bool:
    """檢查標題與摘要是否通過『航空白名單』且未觸發『政論/體育黑名單』"""
    combined = f"{title} {summary}".lower()
    for bad_kw in NOISE_BLACKLIST:
        if bad_kw.lower() in combined:
            return False
    for good_kw in AVIATION_WHITELIST:
        if good_kw.lower() in combined:
            return True
    return False

def normalize_categories_to_list(company_obj: dict):
    """將 categories 統一轉為前端 index.html 標準預期的 List 結構"""
    raw_cats = company_obj.get("categories", [])
    cat_map = {k: [] for k in ["cat1", "cat2", "cat3", "cat4"]}
    summary_map = {k: CATEGORY_META[k]["overall_summary"] for k in CATEGORY_META}

    if isinstance(raw_cats, dict):
        for k in cat_map:
            if isinstance(raw_cats.get(k), list):
                cat_map[k].extend(raw_cats[k])
    elif isinstance(raw_cats, list):
        for c_obj in raw_cats:
            cid = c_obj.get("category_id")
            if cid in cat_map:
                cat_map[cid].extend(c_obj.get("articles", []))
                if c_obj.get("overall_summary"):
                    summary_map[cid] = c_obj["overall_summary"]

    normalized_list = []
    for cid in ["cat1", "cat2", "cat3", "cat4"]:
        normalized_list.append({
            "category_id": cid,
            "category_name": CATEGORY_META[cid]["name"],
            "overall_summary": summary_map[cid],
            "articles": cat_map[cid]
        })
    company_obj["categories"] = normalized_list

def sanitize_existing_data(company_obj: dict):
    """Milestone 5 核心升級：自動巡檢並清除 data.json 內殘留的非航空政論/職棒雜訊與重複項"""
    normalize_categories_to_list(company_obj)
    total_removed = 0

    for cat_obj in company_obj.get("categories", []):
        cleaned_articles = []
        seen_titles = set()
        for art in cat_obj.get("articles", []):
            if art.get("is_relevant") is False:
                total_removed += 1
                continue
            title = (art.get("translated_title") or art.get("title") or "").strip()
            summary = (art.get("summary") or "").strip()
            if not title or not is_clean_aviation_text(title, summary):
                total_removed += 1
                continue
            if title in seen_titles:
                total_removed += 1
                continue
            seen_titles.add(title)

            # 確保新舊欄位雙向相容
            pub_date = art.get("published_at") or art.get("date") or "2026-01-01"
            link_url = art.get("link") or art.get("url") or ""
            art["published_at"] = pub_date
            art["date"] = pub_date
            art["link"] = link_url
            art["url"] = link_url
            if not art.get("id"):
                art["id"] = make_short_id(title)
            cleaned_articles.append(art)

        cleaned_articles.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        cat_obj["articles"] = cleaned_articles[:80]

    print(f"🧹 [大掃除完成] 已從既有 data.json 中清除 {total_removed} 則非航空雜訊或重複文章！")

# ==========================================
# 3. RSS 抓取與第一道防呆去重 (thefuzz + 預篩)
# ==========================================
def build_rss_urls(after_date: str = None, before_date: str = None) -> List[dict]:
    time_filter = ""
    if after_date and before_date:
        time_filter = f" after:{after_date} before:{before_date}"

    queries = [
        # 1. 經營、財報、月營收、重訊
        {"hint": "cat3", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'("星宇航空" OR "2646") AND ("營收" OR "財報" OR "法說會" OR "增資" OR "售後租回" OR "交機" OR "張國煒"){time_filter}'},
        # 2. 社群話題、培訓機師、航線體驗 (嚴格限定星宇航空，排除單純同名干擾)
        {"hint": "cat2", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'"星宇航空" AND (site:dcard.tw OR site:ptt.cc OR "培訓機師" OR "面試" OR "飛行員" OR "開航" OR "A350") -政論 -選舉 -味全龍 -職棒{time_filter}'},
        # 3. 一般台灣航空新聞稿
        {"hint": "cat4", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'"星宇航空" -政論 -名嘴 -選舉 -味全龍 -職棒{time_filter}'},
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

                clean_title = raw_title.rsplit(" - ", 1)[0].strip()
                source_name = raw_title.rsplit(" - ", 1)[1].strip() if " - " in raw_title else "新聞媒體"

                # Milestone 5 源頭硬性攔截：不符航空白名單或觸發黑名單者，直接跳過不送 AI
                if not is_clean_aviation_text(clean_title):
                    continue

                pub_date = datetime.now().strftime("%Y-%m-%d")
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = time.strftime("%Y-%m-%d", entry.published_parsed)

                item_id = make_short_id(clean_title)
                if item_id in seen_ids:
                    continue

                is_dup = False
                for old_title in seen_titles[-300:]:
                    if fuzz.token_set_ratio(clean_title, old_title) >= 80:
                        is_dup = True
                        break
                if is_dup:
                    continue

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

1. **嚴格雜訊剔除 (`is_relevant`)**：
   - 若新聞與星宇航空、民航產業、飛安、機隊、航線、財報無實質關聯（例如：政治選舉、蔣萬安、味全龍職棒、名嘴政論、娛樂八卦），請務必將 `is_relevant` 設為 `false`！絕不要為了湊數而保留無關新聞。
2. **語意去重與外電翻譯**：
   - 若清單中有多篇新聞在講「同一個事件」，請只保留最完整的一筆（其餘相同事件請將 `is_relevant` 設為 `false`）。
   - 若為英文外電，請自動翻譯為台灣航空業慣用的繁體中文術語。
3. **精準分類 (`category`)**：
   - `cat1`：公司文化、張國煒/高層談話、企業理念、機隊交機、新航點開闢、國際航空聯盟/夥伴合作。
   - `cat2`：社群討論（Dcard/PTT）、培訓機師考情、員工福利、乘客體驗話題。
   - `cat3`：公司經營面、每月營收、季度財報、法說會、增資、發債、售後租回（SLB）等商業操作。
   - `cat4`：一般新聞稿、促銷活動、聯名行銷、國際航空外電。
4. **重大商業與財務操作之「三欄視角判讀」**：
   - 針對 `cat3` 或重大機隊航網決策，請提供 `bull_view`（🟢正方觀點）、`bear_view`（🔴反方隱憂）與 `pilot_view`（✈️培訓機師面試官視角）。
5. **自動擷取基本盤與財務時間軸異動 (`new_timeline_logs` & `new_finance_logs`)**：
   - 若新聞中包含具體的「新機交付、售後租回、新航線開航/增班、重要結盟、資本增資」，請提煉至 `new_timeline_logs`，並**務必將該則新聞的 `url` 與 `source` 填入 `TimelineLogItem`**！
   - 若新聞中包含具體的「某月營收數據、某季財報數據、重大財務租機/發債」，請同時提煉至 `new_finance_logs`（並附上對應 `url`）。

原始候選新聞 JSON 如下：
{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""

    for model_name in [PRIMARY_MODEL, "gemini-2.0-flash"]:
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
            time.sleep(5)
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

    if today.day != 1 and not force_audit:
        return

    print("🔍 [每月 1 號定期對帳] 啟動 Google Search Grounding 核對星宇航空最新基本盤...")
    try:
        audit_prompt = """
請透過 Google Search 搜尋星宇航空（STARLUX Airlines）最新公開資訊，整理以下四個區塊（每個區塊包含 highlight 短標題與 details 兩點說明陣列），直接輸出純 JSON：
{
  "philosophy": {"title": "核心理念與企業文化", "highlight": "...", "details": ["...", "..."]},
  "fleet": {"title": "機隊規模與自有/租賃結構", "highlight": "...", "details": ["...", "..."]},
  "routes": {"title": "航線網絡佈局 (台灣出發)", "highlight": "...", "details": ["...", "..."]},
  "partners": {"title": "國際合作夥伴與聯盟策略", "highlight": "...", "details": ["...", "..."]}
}
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

        raw_text = response.text.strip()
        if "```json" in raw_text:
            raw_text = raw_text.split("```json")[1].split("```")[0].strip()
        elif "```" in raw_text:
            raw_text = raw_text.split("```")[1].split("```")[0].strip()

        audit_data = json.loads(raw_text)
        profile = company_obj.setdefault("company_profile", {})
        for key in ["philosophy", "fleet", "routes", "partners"]:
            if isinstance(audit_data.get(key), dict):
                profile[key] = audit_data[key]
        profile["last_audit_date"] = today.strftime("%Y-%m-%d")
        print("✅ [每月 1 號定期對帳] 企業基本盤更新完成！")
    except Exception as e:
        print(f"🛡️ [防呆保護啟動] 每月對帳搜尋發生異常（已安全略過，不影響主程式運作）: {e}")

# ==========================================
# 6. 資料合併與結構同步
# ==========================================
def merge_results_into_data(company_obj: dict, ai_result: BatchAIResponse):
    if not ai_result:
        return

    normalize_categories_to_list(company_obj)
    cat_dict = {c["category_id"]: c["articles"] for c in company_obj["categories"]}

    added_count = 0
    for item in ai_result.processed_news:
        if not item.is_relevant:
            continue
        # 二次防呆檢查：確認 AI 產出的標題與摘要沒有偷渡無關雜訊
        if not is_clean_aviation_text(item.title, item.summary):
            continue

        cat_key = item.category if item.category in cat_dict else "cat4"
        is_english = any(ord(ch) < 128 for ch in item.source) and " " in item.source

        news_dict = {
            "id": make_short_id(item.title),
            "title": item.title,
            "translated_title": item.title,
            "date": item.date,
            "published_at": item.date,
            "source": item.source,
            "url": item.url,
            "link": item.url,
            "lang": "en" if is_english else "zh",
            "is_relevant": True,
            "summary": item.summary,
            "tags": item.tags,
            "bull_view": item.bull_view or "",
            "bear_view": item.bear_view or "",
            "pilot_view": item.pilot_view or ""
        }

        existing_titles = {(x.get("translated_title") or x.get("title")) for x in cat_dict[cat_key]}
        if news_dict["title"] not in existing_titles:
            cat_dict[cat_key].append(news_dict)
            added_count += 1

    for c_obj in company_obj["categories"]:
        c_obj["articles"].sort(key=lambda x: x.get("published_at") or x.get("date") or "", reverse=True)
        c_obj["articles"] = c_obj["articles"][:80]

    # 2. 更新企業基本盤近 5 筆異動時間軸 (company_profile.timeline_logs) 並綁定參考連結
    profile = company_obj.setdefault("company_profile", {})
    timeline_logs = profile.setdefault("timeline_logs", [])
    existing_tl_keys = {(x.get("title") or x.get("event", "")[:15]) for x in timeline_logs}

    color_map = {"機隊異動": "emerald", "航網開拓": "purple", "資本財務": "amber", "策略合作": "sky"}
    for tl in ai_result.new_timeline_logs:
        if tl.title not in existing_tl_keys:
            timeline_logs.append({
                "date": tl.date,
                "type": tl.type,
                "category": tl.type,
                "badge_color": color_map.get(tl.type, "amber"),
                "title": tl.title,
                "detail": tl.detail,
                "event": f"【{tl.title}】{tl.detail}",
                "source": tl.source or "新聞動態",
                "url": tl.url or ""
            })
            existing_tl_keys.add(tl.title)

    timeline_logs.sort(key=lambda x: x.get("date", ""), reverse=True)
    profile["timeline_logs"] = timeline_logs[:5]

    # 3. 更新財務統計趨勢與正反觀點專區 (finance_overview)
    fin_overview = company_obj.setdefault("finance_overview", {})
    fin_logs = fin_overview.setdefault("history_logs", [])
    existing_fin_titles = {(x.get("headline") or x.get("title")) for x in fin_logs}
    for fl in ai_result.new_finance_logs:
        if fl.headline not in existing_fin_titles:
            fin_logs.append({
                "date": fl.period,
                "period": fl.period,
                "type": fl.event_type,
                "event_type": fl.event_type,
                "title": fl.headline,
                "headline": fl.headline,
                "desc": fl.metrics,
                "metrics": fl.metrics,
                "bull_view": fl.bull_view,
                "bear_view": fl.bear_view,
                "pilot_view": fl.pilot_view,
                "link": fl.url or "",
                "url": fl.url or ""
            })
            existing_fin_titles.add(fl.headline)
    fin_logs.sort(key=lambda x: x.get("date") or x.get("period") or "", reverse=True)
    fin_overview["history_logs"] = fin_logs[:24]

    print(f"📥 本批次成功寫入 {added_count} 則精選情報！")

# ==========================================
# 7. 主程式執行流程
# ==========================================
def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ 找不到 GEMINI_API_KEY 環境變數！請確認已在 GitHub Secrets 設定。")

    client = genai.Client(api_key=api_key)

    default_data = {
        "last_updated": "",
        "backfill_cursor": datetime.now().strftime("%Y-%m-%d"),
        "backfill_target_stop": BACKFILL_STOP_DATE,
        "companies": [
            {
                "company_id": "starlux",
                "company_name": "星宇航空 STARLUX",
                "company_profile": {"timeline_logs": []},
                "finance_overview": {"history_logs": []},
                "categories": []
            }
        ]
    }
    data = load_json(DATA_FILE, default_data)

    if "companies" in data and isinstance(data["companies"], list) and len(data["companies"]) > 0:
        starlux_obj = data["companies"][0]
        starlux_obj["company_id"] = starlux_obj.get("company_id") or starlux_obj.get("id") or "starlux"
        starlux_obj["company_name"] = starlux_obj.get("company_name") or starlux_obj.get("name") or "星宇航空 STARLUX"
    else:
        starlux_obj = default_data["companies"][0]
        data["companies"] = [starlux_obj]

    # --- 階段 0：啟動既有 data.json 雜訊大掃除 ---
    sanitize_existing_data(starlux_obj)

    default_history = {
        "backfill_cursor": data.get("backfill_cursor") or datetime.now().strftime("%Y-%m-%d"),
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

        history["backfill_cursor"] = after_str
        print(f"📌 歷史回溯游標已推進至：{after_str}（目標終點：{BACKFILL_STOP_DATE}）")
    else:
        print(f"🎉 歷史回溯已抵達終點 ({BACKFILL_STOP_DATE})，無須再回推！")

    # --- 階段 C：每月 1 號 Google Search 對帳 ---
    monthly_grounding_audit(client, starlux_obj)

    # --- 階段 D：儲存結果並同步前端狀態 ---
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["last_updated"] = now_str
    data["backfill_cursor"] = history.get("backfill_cursor", BACKFILL_STOP_DATE)
    data["backfill_target_stop"] = BACKFILL_STOP_DATE
    starlux_obj["last_updated"] = now_str

    save_json(DATA_FILE, data)
    save_json(HISTORY_FILE, history)
    print(f"✅ 全部任務執行完畢！資料已寫入 data.json 與 history.json ({now_str})")

if __name__ == "__main__":
    main()
