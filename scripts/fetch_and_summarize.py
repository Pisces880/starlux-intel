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
MANUAL_INBOX_FILE = os.path.join(BASE_DIR, "manual_ig_inbox.txt")

# 歷史回溯終點與每次回推天數
BACKFILL_STOP_DATE = "2025-09-01"
BACKFILL_STEP_DAYS = 30

# 使用的模型名稱
PRIMARY_MODEL = "gemini-2.5-flash"

# 多航司專屬白名單與雜訊黑名單
AVIATION_WHITELIST = [
    "星宇", "starlux", "華航", "中華航空", "china airlines", "cal", "2610", "2646",
    "航空", "機師", "飛行員", "培訓", "張國煒", "k董", "高星潢", "謝世謙",
    "機隊", "交機", "航線", "開航", "航點", "轉機", "桃機", "機場", "民航",
    "a321", "a330", "a350", "boeing", "波音", "airbus", "空巴", "777", "787",
    "載客率", "票價", "哩程", "貴賓室", "空服", "地勤", "機務", "維修",
    "天合聯盟", "skyteam", "寰宇一家", "oneworld", "法說", "售後租回"
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
        "overall_summary": "聚焦企業飛安文化、經營高層哲學、機隊擴張、航網佈局及國際策略合作。"
    },
    "cat2": {
        "name": "2.社群討論與培訓考情",
        "overall_summary": "彙整 Dcard、PTT Aviation 版、官方 IG/Threads 話題與航空社群針對培訓機師面試、航線搭乘體驗與職場文化之實質討論。"
    },
    "cat3": {
        "name": "3.公司經營與財報走向",
        "overall_summary": "追蹤每月營收、季度財報、法說會展望，以及機隊購置、增資與重資產商業操作之關鍵財務分析。"
    },
    "cat4": {
        "name": "4.相關新聞稿與國際外電",
        "overall_summary": "收錄國內外媒體新聞稿與英文航空外電（自動翻譯繁中），掌握國際能見度與全球航空產業動態。"
    }
}

# ==========================================
# 1. Pydantic 結構化輸出 Schema 定義
# ==========================================
class NewsItemOutput(BaseModel):
    is_relevant: bool = Field(description="是否與該航空或民航產業實質相關？若為政治選舉、職棒體育、娛樂八卦請填 False")
    category: str = Field(description="分類代號：cat1(文化理念/高層談話/機隊航網異動), cat2(社群討論/培訓機師/IG話題), cat3(公司經營/營收財報/法說會), cat4(一般新聞稿/國際外電)")
    title: str = Field(description="繁體中文標題（若為英文外電請翻譯為專業繁體中文標題）")
    date: str = Field(description="新聞日期 YYYY-MM-DD")
    source: str = Field(description="媒體或社群來源名稱")
    url: str = Field(description="原始新聞或貼文連結")
    summary: str = Field(description="繁體中文重點摘要（約 60-120 字，提煉對培訓機師面試有價值的商業或營運細節）")
    tags: List[str] = Field(description="2到4個關鍵字標籤，例如：['機隊擴張', 'A350', '北美航網']")
    bull_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🟢正方公司派觀點；一般新聞填空字串")
    bear_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🔴反方市場派隱憂；一般新聞填空字串")
    pilot_view: Optional[str] = Field(default="", description="✈️培訓機師面試官視角：這則情報對飛行員人力需求、安全文化或面試回答的啟示")

class TimelineLogItem(BaseModel):
    date: str = Field(description="事件日期 YYYY-MM-DD 或 YYYY-MM")
    type: str = Field(description="異動類型：機隊異動 / 航網開拓 / 資本財務 / 策略合作")
    title: str = Field(description="簡短事件標題")
    detail: str = Field(description="具體細節說明，包含對運能或財務的影響")
    source: Optional[str] = Field(default="新聞動態", description="消息來源")
    url: Optional[str] = Field(default="", description="原始連結 URL")

class FinanceLogItem(BaseModel):
    period: str = Field(description="財務期間，例如：2026-09 或 2026 Q2")
    event_type: str = Field(description="月營收 / 季財報 / 重大財務操作")
    headline: str = Field(description="核心數據標題")
    metrics: str = Field(description="關鍵財務數據摘要")
    bull_view: str = Field(description="🟢正方公司派觀點")
    bear_view: str = Field(description="🔴反方市場派隱憂")
    pilot_view: str = Field(description="✈️培訓機師面試官視角")
    url: Optional[str] = Field(default="", description="原始連結 URL")

class BatchAIResponse(BaseModel):
    processed_news: List[NewsItemOutput] = Field(description="過濾並合併後的有效情報清單")
    new_timeline_logs: List[TimelineLogItem] = Field(description="偵測到的新機隊、新航線或重大資本異動")
    new_finance_logs: List[FinanceLogItem] = Field(description="偵測到的月營收、財報或重訊")

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
    combined = f"{title} {summary}".lower()
    for bad_kw in NOISE_BLACKLIST:
        if bad_kw.lower() in combined:
            return False
    for good_kw in AVIATION_WHITELIST:
        if good_kw.lower() in combined:
            return True
    return False

def normalize_categories_to_list(company_obj: dict):
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

    print(f"🧹 [{company_obj.get('company_name')}] 大掃除完成，已清除 {total_removed} 筆雜訊或重複項！")

# ==========================================
# 3. RSS 抓取與手動 IG 投遞箱讀取
# ==========================================
def build_rss_urls(company_name: str, company_keyword: str, after_date: str = None, before_date: str = None) -> List[dict]:
    time_filter = ""
    if after_date and before_date:
        time_filter = f" after:{after_date} before:{before_date}"

    queries = [
        {"hint": "cat3", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'("{company_name}" OR "{company_keyword}") AND ("營收" OR "財報" OR "法說會" OR "增資" OR "售後租回" OR "交機"){time_filter}'},
        {"hint": "cat2", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'"{company_name}" AND (site:dcard.tw OR site:ptt.cc OR "培訓機師" OR "面試" OR "飛行員" OR "IG" OR "Threads") -政論 -選舉 -味全龍 -職棒{time_filter}'},
        {"hint": "cat4", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
         "q": f'"{company_name}" -政論 -名嘴 -選舉 -味全龍 -職棒{time_filter}'}
    ]
    rss_list = []
    for item in queries:
        encoded_q = urllib.parse.quote(item["q"])
        url = f"https://news.google.com/rss/search?q={encoded_q}&hl={item['hl']}&gl={item['gl']}&ceid={item['ceid']}"
        rss_list.append({"url": url, "hint": item["hint"]})
    return rss_list

def fetch_raw_candidates(rss_configs: List[dict], history_data: dict, max_items: int = 15) -> List[dict]:
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

def load_manual_ig_inbox(target_company_name: str) -> List[dict]:
    """讀取 manual_ig_inbox.txt，取出屬於該公司的手動 IG 貼文並轉為候選格式"""
    if not os.path.exists(MANUAL_INBOX_FILE):
        return []

    try:
        with open(MANUAL_INBOX_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception:
        return []

    if not content:
        return []

    blocks = content.split("---")
    manual_candidates = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        
        # 簡易解析格式 [公司名稱] 網址: xxx 內容: xxx
        if target_company_name in block or "星宇" in target_company_name and "星宇" in block or "華航" in target_company_name and "華航" in block:
            lines = block.split("\n")
            url = ""
            text_content = []
            for line in lines:
                if line.startswith("網址:") or line.startswith("URL:"):
                    url = line.split(":", 1)[1].strip()
                else:
                    text_content.append(line)
            
            full_text = "\n".join(text_content).strip()
            if full_text:
                title_snippet = full_text[:40].replace("\n", " ") + "..."
                manual_candidates.append({
                    "id": make_short_id(full_text),
                    "title": f"[📸 官方IG精選] {title_snippet}",
                    "source": "官方 Instagram / Threads",
                    "url": url or "https://www.instagram.com/",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "hint": "cat2"  # 強制歸類至社群討論
                })
    return manual_candidates

def clear_manual_ig_inbox():
    """處理完畢後清空投遞箱"""
    try:
        with open(MANUAL_INBOX_FILE, "w", encoding="utf-8") as f:
            f.write("")
        print("🗑️ 手動 IG 投遞箱 (`manual_ig_inbox.txt`) 已自動清空歸零。")
    except Exception as e:
        print(f"⚠️ 清空投遞箱失敗: {e}")

# ==========================================
# 4. Gemini AI 批次分析
# ==========================================
def analyze_with_gemini(client: genai.Client, company_name: str, candidates: List[dict]) -> Optional[BatchAIResponse]:
    if not candidates:
        return None

    prompt = f"""
你是一位專業的「航空產業分析師」與「{company_name} 培訓機師面試教練」。
請審閱以下抓取到的原始情報與社群貼文，並嚴格執行以下任務：

1. **雜訊剔除 (`is_relevant`)**：
   - 若內容與該航空、民航產業、飛安、機隊、航線無實質關聯，請將 `is_relevant` 設為 `false`。
2. **精準分類 (`category`)**：
   - `cat1`：公司文化、高層談話、機隊交機、新航點、聯盟合作。
   - `cat2`：社群討論、Dcard/PTT、官方 IG/Threads 話題、培訓機師考情。
   - `cat3`：公司經營、月營收、季度財報、法說會、增資租機。
   - `cat4`：一般新聞稿、國際外電。
3. **重大商業與財務操作之「三欄視角判讀」**：
   - 針對 `cat3` 或重大決策，請提供 `bull_view`、`bear_view` 與 `pilot_view`。
4. **自動擷取基本盤與財務時間軸異動 (`new_timeline_logs` & `new_finance_logs`)**：
   - 若包含新機交付、新航線或重大資本異動，請提煉至 `new_timeline_logs` (務必帶入 `url`)。
   - 若包含月營收、財報數據，請提煉至 `new_finance_logs` (務必帶入 `url`)。

原始候選情報 JSON 如下：
{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""

    for model_name in [PRIMARY_MODEL, "gemini-2.0-flash"]:
        try:
            print(f"🤖 呼叫 Gemini 模型 ({model_name}) 分析 {company_name} 情報...")
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
            print(f"⚠️ 模型 {model_name} 異常: {e}")
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

    c_name = company_obj.get("company_name")
    print(f"🔍 [每月 1 號定期對帳] 啟動 Google Search Grounding 核對 {c_name} 最新基本盤...")
    try:
        audit_prompt = f"""
請透過 Google Search 搜尋「{c_name}」最新公開資訊，整理以下四個區塊（每個區塊包含 highlight 與 details 陣列），輸出純 JSON：
{{
  "philosophy": {{"title": "核心理念與企業文化", "highlight": "...", "details": ["...", "..."]}},
  "fleet": {{"title": "機隊規模與自有/租賃結構", "highlight": "...", "details": ["...", "..."]}},
  "routes": {{"title": "航線網絡佈局 (台灣出發)", "highlight": "...", "details": ["...", "..."]}},
  "partners": {{"title": "國際合作夥伴與聯盟策略", "highlight": "...", "details": ["...", "..."]}}
}}
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=audit_prompt,
            config=types.GenerateContentConfig(tools=[{"google_search": {}}], temperature=0.1),
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
        print(f"✅ [{c_name}] 企業基本盤更新完成！")
    except Exception as e:
        print(f"🛡️ 每月對帳搜尋發生異常: {e}")

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

    # 更新 timeline_logs
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

    # 更新 finance_overview
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

    print(f"📥 [{company_obj.get('company_name')}] 本批次成功寫入 {added_count} 則情報！")

# ==========================================
# 7. 主程式執行流程 (支援多航司：星宇 + 華航)
# ==========================================
def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ 找不到 GEMINI_API_KEY 環境變數！")

    client = genai.Client(api_key=api_key)

    default_data = {
        "last_updated": "",
        "backfill_cursor": datetime.now().strftime("%Y-%m-%d"),
        "backfill_target_stop": BACKFILL_STOP_DATE,
        "companies": [
            {
                "company_id": "starlux",
                "company_name": "星宇航空",
                "company_profile": {"timeline_logs": []},
                "finance_overview": {"history_logs": []},
                "categories": []
            },
            {
                "company_id": "cal",
                "company_name": "中華航空",
                "company_profile": {"timeline_logs": []},
                "finance_overview": {"history_logs": []},
                "categories": []
            }
        ]
    }
    data = load_json(DATA_FILE, default_data)
    
    # 確保 companies 陣列完整包含星宇與華航
    companies_list = data.get("companies", [])
    if not any(c.get("company_id") == "starlux" or c.get("id") == "starlux" for c in companies_list):
        companies_list.insert(0, default_data["companies"][0])
    if not any(c.get("company_id") == "cal" or c.get("id") == "cal" for c in companies_list):
        companies_list.append(default_data["companies"][1])
    data["companies"] = companies_list

    history_default = {
        "backfill_cursor": data.get("backfill_cursor") or datetime.now().strftime("%Y-%m-%d"),
        "seen_ids": [],
        "seen_titles": []
    }
    history = load_json(HISTORY_FILE, history_default)

    # 定義要巡檢的航司清單
    targets = [
        {"id": "starlux", "name": "星宇航空", "keyword": "2646"},
        {"id": "cal", "name": "中華航空", "keyword": "2610"}
    ]

    for t in targets:
        # 尋找對應的 company_obj
        c_obj = None
        for c in data["companies"]:
            if c.get("company_id") == t["id"] or c.get("id") == t["id"]:
                c_obj = c
                break
        if not c_obj:
            c_obj = {"company_id": t["id"], "company_name": t["name"], "company_profile": {}, "finance_overview": {}, "categories": []}
            data["companies"].append(c_obj)

        c_obj["company_id"] = t["id"]
        c_obj["company_name"] = t["name"]

        print(fn_header := f"\n================ 正在處理：{t['name']} ================")
        sanitize_existing_data(c_obj)

        # 1. 讀取手動 IG 投遞箱內容
        manual_candidates = load_manual_ig_inbox(t["name"])
        if manual_candidates:
            print(f"📸 發現來自手動 IG 投遞箱的 {t['name']} 貼文共 {len(manual_candidates)} 篇，送交 AI 保送分析...")
            ai_res_manual = analyze_with_gemini(client, t["name"], manual_candidates)
            merge_results_into_data(c_obj, ai_res_manual)

        # 2. 抓取今日最新 RSS 情報
        print(f"🚀 開始抓取 {t['name']} 最新即時情報...")
        latest_rss = build_rss_urls(t["name"], t["keyword"])
        latest_candidates = fetch_raw_candidates(latest_rss, history, max_items=12)
        if latest_candidates:
            ai_res_latest = analyze_with_gemini(client, t["name"], latest_candidates)
            merge_results_into_data(c_obj, ai_res_latest)
        else:
            print(f"ℹ️ {t['name']} 今日無新增之未讀即時新聞。")

        # 3. 每月 1 號對帳
        monthly_grounding_audit(client, c_obj)

    # 清空手動 IG 投遞箱（確保每次只處理當次新投遞的貼文）
    if os.path.exists(MANUAL_INBOX_FILE) and os.path.getsize(MANUAL_INBOX_FILE) > 0:
        clear_manual_ig_inbox()

    # --- 儲存結果 ---
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["last_updated"] = now_str
    data["backfill_cursor"] = history.get("backfill_cursor", BACKFILL_STOP_DATE)
    data["backfill_target_stop"] = BACKFILL_STOP_DATE

    for c in data["companies"]:
        c["last_updated"] = now_str

    save_json(DATA_FILE, data)
    save_json(HISTORY_FILE, history)
    print(f"\n✅ 全部航司任務執行完畢！資料已同步寫入 data.json ({now_str})")

if __name__ == "__main__":
    main()
