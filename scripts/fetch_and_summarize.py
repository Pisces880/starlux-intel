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

BACKFILL_STOP_DATE = "2025-09-01"
BACKFILL_STEP_DAYS = 30
PRIMARY_MODEL = "gemini-2.5-flash"

# 航空核心白名單 (涵蓋星宇 + 華航) 與雜訊黑名單
AVIATION_WHITELIST = [
    "星宇", "starlux", "華航", "中華航空", "china airlines", "cal", "2646", "2610",
    "航空", "機師", "飛行員", "培訓", "張國煒", "k董", "高星潢", "謝世謙", "陳漢銘",
    "機隊", "交機", "航線", "開航", "航點", "轉機", "桃機", "機場", "民航",
    "a321", "a330", "a350", "airbus", "空巴", "波音", "boeing", "777", "787", "747", "737-800",
    "載客率", "票價", "哩程", "貴賓室", "空服", "地勤", "機務", "維修", "華航園區",
    "寰宇一家", "oneworld", "天合聯盟", "skyteam", "阿提哈德", "阿拉斯加航空", "法說", "售後租回", "saf", "永續燃油"
]

NOISE_BLACKLIST = [
    "與航空業無關", "與航空無關", "不涉及民航", "不具參考效益", "不具面試參考",
    "無直接關聯", "並無相關性", "純屬外部政論", "政論節目", "政論直播",
    "味全龍", "中華職棒", "職棒", "中職", "總冠軍", "台灣大賽", "啦啦隊", "小龍女",
    "蔣萬安", "選戰", "選舉", "造勢", "民調", "立法院", "議員", "立委", "罷免", "八卦", "夜市拜票"
]

# 各航司專屬搜尋設定與精準機隊基本盤 (含總架數、自購/租賃、機齡)
COMPANIES_CONFIG = [
    {
        "company_id": "starlux",
        "company_name": "星宇航空",
        "stock_code": "2646",
        "en_name": "STARLUX Airlines",
        "ceo_kw": '"張國煒"',
        "cat_summaries": {
            "cat1": "聚焦星宇航空「Safety is our attitude」飛安文化、張國煒董事長經營哲學、全空巴機隊擴張、北美與亞洲航網佈局及國際策略合作。",
            "cat2": "彙整 Dcard、PTT Aviation 版、官方 IG/Threads 話題與航空社群針對星宇培訓機師面試、航線體驗與職場文化之實質討論。",
            "cat3": "追蹤星宇每月營收、季度財報、法說會展望，以及新機售後租回（SLB）、現金增資等重資產擴張期之關鍵財務操作。",
            "cat4": "收錄國內外媒體新聞稿與英文航空外電（自動翻譯繁中），掌握星宇航空國際能見度與全球航空產業動態。"
        },
        "default_profile": {
            "philosophy": {
                "title": "核心理念與企業文化",
                "highlight": "Safety is our attitude，精品航空與極致飛安導向",
                "details": [
                    "董事長張國煒親力親為，兼具企業家遠見與飛行員實務嚴謹態度。",
                    "重金投資自建維修與運籌基地，強化自主後勤與飛安韌性。"
                ]
            },
            "fleet": {
                "title": "機隊規模、機齡與自有/租賃結構",
                "highlight": "現役 39 架 (平均機齡 3.2 年)｜自購 14 架 / 租賃 25 架",
                "details": [
                    "窄體與中長程（全租賃）：A321neo 共 13 架（全租賃，年底達 16 架）、A330-900neo 共 10 架（全租賃，含新機交機即辦理『售後租回 SLB』活化百億現金流之機隊）。",
                    "越洋旗艦機隊（自購為主）：A350-900 共 10 架（自購 8 架 / 租賃 2 架）、A350-1000 共 6 架（全數自購，2026/1 起交付）。",
                    "待交付訂單：A350-1000 尚有 12 架自購、A350F 全貨機 10 架（全數自購，2027 年起交機），2026 年底總機隊預計達 42~43 架。"
                ]
            },
            "routes": {
                "title": "航線網絡佈局 (台灣出發)",
                "highlight": "深耕北美西岸航網，密集串聯日本與東南亞轉機市場",
                "details": [
                    "北美開闢洛杉磯、舊金山、西雅圖、安大略等航點，搶攻高收益商務與轉機客群。",
                    "積極佈局日本一二線城市與台中出發航線，擴大區域市場覆蓋率。"
                ]
            },
            "partners": {
                "title": "國際合作夥伴與聯盟策略",
                "highlight": "靈活雙邊結盟（阿拉斯加航空、美國航空、阿提哈德航空），爭取加入 Oneworld",
                "details": [
                    "與美國航空、阿拉斯加航空代碼共享，延伸北美內陸航網。",
                    "攜手阿提哈德航空開放雙向哩程累積與兌換，強化中東與歐洲接駁。"
                ]
            }
        }
    },
    {
        "company_id": "cal",
        "company_name": "中華航空",
        "stock_code": "2610",
        "en_name": "China Airlines",
        "ceo_kw": '"高星潢" OR "陳漢銘"',
        "cat_summaries": {
            "cat1": "聚焦中華航空「飛安第一、以客為尊」企業文化、高層經營策略、波音與空巴雙主流機隊汰舊換新、全球航網與 ESG 永續飛航領先指標。",
            "cat2": "彙整 Dcard、PTT Aviation 版、官方 IG/Threads 話題與航空社群針對華航培訓機師考情、航線服務與職場文化之實質討論。",
            "cat3": "追蹤華航每月客貨運營收、季度財報、法說會展望，以及 787/A350 新機引進、貨機機隊營運與財務結構表現。",
            "cat4": "收錄國內外媒體新聞稿與英文航空外電（自動翻譯繁中），掌握中華航空於天合聯盟（SkyTeam）及全球航空市場之最新動態。"
        },
        "default_profile": {
            "philosophy": {
                "title": "核心理念與企業文化",
                "highlight": "飛安第一、永續經營，台灣最具規模與歷史底蘊之旗艦航空",
                "details": [
                    "深耕嚴謹的飛航安全管理系統（SMS）與成熟的培訓機師養成體系。",
                    "領先國籍航空推動 ESG 淨零碳排與永續航空燃油（SAF）添加作業。"
                ]
            },
            "fleet": {
                "title": "機隊規模、機齡與自有/租賃結構",
                "highlight": "現役 85 架：客機 67 + 貨機 18 (平均機齡 9.6 年)",
                "details": [
                    "整體資產結構：自購約 51 架（約 60%）/ 營業租賃約 34 架（約 40%）；客機平均機齡 9.1 年、貨機 11.1 年。",
                    "廣體與窄體客機（67 架）：A350-900 共 17 架（自購 14 / 租機 3）、777-300ER 共 10 架（自購 4 / 租機 6）、A321neo 共 22 架（租賃為主）、A330-300 共 12 架（自購為主，逐步汰換）、737-800 共 6 架（租機為主，逐步退租）。",
                    "全貨機與次世代訂單：貨機 18 架全數自購（777F 共 10 架 + 747-400F 共 8 架）；新機訂單含 24 架波音 787（18 架 -9 / 6 架 -10）、15 架 A350-1000、15 架 777-9 與 8 架 777-8F。"
                ]
            },
            "routes": {
                "title": "航線網絡佈局 (台灣出發)",
                "highlight": "歐美長程航點齊全，東北亞、東南亞與兩岸航網極具規模",
                "details": [
                    "直飛北美（洛城、舊金山、安大略、溫哥華、西雅圖、紐約）與歐洲（法蘭克福、阿姆斯特丹、倫敦、羅馬、維也納、布拉格）。",
                    "亞洲區域航線班次密集，掌握高含金量商務與六大洲轉機客源。"
                ]
            },
            "partners": {
                "title": "國際合作夥伴與聯盟策略",
                "highlight": "天合聯盟（SkyTeam）正式成員，全球聯營與代碼共享網絡成熟",
                "details": [
                    "透過天合聯盟與達美航空、法荷航集團、大韓航空等全球夥伴深度互聯。",
                    "結合龐大貨運與客運網路，具備極強的抗景氣波動與國際聯運能力。"
                ]
            }
        }
    }
]

CATEGORY_NAMES = {
    "cat1": "1.公司文化與高層談話",
    "cat2": "2.社群與影音動態(Dcard/PTT/YouTube/IG話題)",
    "cat3": "3.公司經營與財報走向",
    "cat4": "4.相關新聞稿(中文與國際外電)"
}

# ==========================================
# 1. Pydantic 結構化輸出 Schema 定義
# ==========================================
class NewsItemOutput(BaseModel):
    is_relevant: bool = Field(description="是否與該航空公司或民航產業實質相關？若為政治選舉、職棒體育、娛樂八卦請填 False")
    category: str = Field(description="分類代號：cat1(文化理念/高層談話/機隊航網異動), cat2(社群討論/培訓機師/IG話題), cat3(公司經營/營收財報/法說會), cat4(一般新聞稿/國際外電)")
    title: str = Field(description="繁體中文標題（若為英文外電請翻譯為專業繁體中文標題；若為手動IG貼文請加上『[📸 官方IG精選] 』前綴並提煉精準標題）")
    date: str = Field(description="新聞或貼文日期 YYYY-MM-DD")
    source: str = Field(description="媒體或社群來源名稱")
    url: str = Field(description="原始新聞或貼文連結")
    summary: str = Field(description="繁體中文重點摘要（約 60-120 字，條理分明，提煉對培訓機師面試有價值的商業、航網或品牌細節）")
    tags: List[str] = Field(description="2到4個關鍵字標籤，例如：['機隊擴張', 'A350', '北美航網']")
    bull_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🟢正方公司派觀點；一般新聞填空字串")
    bear_view: Optional[str] = Field(default="", description="若屬重大經營/財務/機隊策略，請填寫🔴反方市場派隱憂；一般新聞填空字串")
    pilot_view: Optional[str] = Field(default="", description="✈️培訓機師面試官視角：這則情報對飛行員人力需求、安全文化、航線派遣或面試回答的啟示")

class TimelineLogItem(BaseModel):
    date: str = Field(description="事件日期 YYYY-MM-DD 或 YYYY-MM")
    type: str = Field(description="異動類型：機隊異動 / 航網開拓 / 資本財務 / 策略合作")
    title: str = Field(description="簡短事件標題，例如：第 8 架 A350-900 交機")
    detail: str = Field(description="具體細節說明，包含對運能或財務的影響")
    source: Optional[str] = Field(default="新聞動態", description="消息來源媒體名稱")
    url: Optional[str] = Field(default="", description="對應的原始新聞連結 URL")

class FinanceLogItem(BaseModel):
    period: str = Field(description="財務期間或日期，例如：2026-09 或 2026 Q3")
    event_type: str = Field(description="請務必從以下三類擇一填寫：'月營收'、'季財報'、'重大財務操作'")
    headline: str = Field(description="核心數據標題")
    metrics: str = Field(description="關鍵財務數據摘要（如 YoY、MoM、EPS 或租機購機金額）")
    bull_view: str = Field(description="🟢正方公司派觀點")
    bear_view: str = Field(description="🔴反方市場派隱憂")
    pilot_view: str = Field(description="✈️培訓機師面試官視角")
    url: Optional[str] = Field(default="", description="對應的原始新聞連結 URL")

class BatchAIResponse(BaseModel):
    processed_news: List[NewsItemOutput] = Field(description="過濾並合併後的有效情報清單")
    new_timeline_logs: List[TimelineLogItem] = Field(description="從本次新聞中偵測到的新機隊交機、新開航線、重要合作夥伴或重大資本異動")
    new_finance_logs: List[FinanceLogItem] = Field(description="從本次新聞中偵測到的新月營收公布、季財報、法說會或購租機/增資重訊")

# ==========================================
# 2. 輔助函式與雜訊檢驗邏輯
# ==========================================
def make_short_id(text: str, prefix: str = "") -> str:
    raw = f"{prefix}::{text}" if prefix else text
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{h}" if prefix else h

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

def is_clean_aviation_text(title: str, summary: str = "", is_manual: bool = False) -> bool:
    """手動 IG 貼文直接保送過關；一般新聞需通過航空白名單且未觸發黑名單"""
    if is_manual or "官方IG精選" in title or "官方 Instagram" in summary:
        return True
    combined = f"{title} {summary}".lower()
    for bad_kw in NOISE_BLACKLIST:
        if bad_kw.lower() in combined:
            return False
    for good_kw in AVIATION_WHITELIST:
        if good_kw.lower() in combined:
            return True
    return False

def ensure_company_structure(company_obj: dict, cfg: dict):
    """確保公司具備完整基本盤與四大分類結構，同時 100% 保留原本已有的所有文章"""
    company_obj["company_id"] = cfg["company_id"]
    company_obj["company_name"] = cfg["company_name"]

    # 直接同步最新機隊與基本盤定義 (保留已有的 timeline_logs)
    profile = company_obj.setdefault("company_profile", {})
    for k, v in cfg["default_profile"].items():
        profile[k] = v
    profile.setdefault("timeline_logs", [])
    company_obj.setdefault("finance_overview", {}).setdefault("history_logs", [])

    # 整理四大分類，保留既有文章
    raw_cats = company_obj.get("categories", [])
    cat_map = {k: [] for k in ["cat1", "cat2", "cat3", "cat4"]}
    summary_map = {k: cfg["cat_summaries"][k] for k in ["cat1", "cat2", "cat3", "cat4"]}

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
            "category_name": CATEGORY_NAMES[cid],
            "overall_summary": summary_map[cid],
            "articles": cat_map[cid]
        })
    company_obj["categories"] = normalized_list

def sanitize_existing_data(company_obj: dict, cfg: dict):
    """清除殘留的非航空政論/職棒雜訊，但完整保留所有正常航空新聞與手動 IG 貼文"""
    ensure_company_structure(company_obj, cfg)
    total_removed = 0
    cid = cfg["company_id"]

    for cat_obj in company_obj.get("categories", []):
        cleaned_articles = []
        seen_titles = set()
        for art in cat_obj.get("articles", []):
            if art.get("is_relevant") is False:
                total_removed += 1
                continue
            title = (art.get("translated_title") or art.get("title") or "").strip()
            summary = (art.get("summary") or "").strip()
            is_manual = bool(art.get("is_manual")) or ("官方IG" in title)

            if not title or not is_clean_aviation_text(title, summary, is_manual=is_manual):
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
                art["id"] = make_short_id(title, prefix=cid)
            cleaned_articles.append(art)

        cleaned_articles.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        cat_obj["articles"] = cleaned_articles[:80]

    print(f"🧹 [{cfg['company_name']}] 巡檢完成：已剔除 {total_removed} 則非航空雜訊，保留所有有效航空情報！")

# ==========================================
# 3. RSS 抓取 (修復語法 + 雙航司獨立防重) 與手動 IG 投遞箱讀取
# ==========================================
def build_rss_urls(cfg: dict, after_date: str = None, before_date: str = None) -> List[dict]:
    """
    修復原因 A：採用最穩定、必出結果的 Google News RSS 關鍵字組合。
    避免無引號人名或複雜 AND/OR 巢狀語法導致 Google News 回傳 0 筆。
    """
    time_filter = f" after:{after_date} before:{before_date}" if (after_date and before_date) else ""
    cid = cfg["company_id"]
    en_name = cfg["en_name"]

    if cid == "starlux":
        queries = [
            # 1. 經營、財報、月營收、重訊
            {"hint": "cat3", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
             "q": f'"星宇航空" ("營收" OR "財報" OR "法說會" OR "增資" OR "售後租回" OR "交機" OR "張國煒"){time_filter}'},
            # 2. 社群話題、培訓機師、航點與新機
            {"hint": "cat2", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
             "q": f'"星宇航空" ("培訓機師" OR "機師" OR "開航" OR "A350" OR "A330" OR "Dcard" OR "PTT"){time_filter}'},
            # 3. 一般台灣航空新聞稿 (最廣域保底必出結果)
            {"hint": "cat4", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
             "q": f'"星宇航空" -職棒 -味全龍 -選舉{time_filter}'},
            # 4. 國際英文外電
            {"hint": "cat4", "hl": "en-US", "gl": "US", "ceid": "US:en",
             "q": f'"{en_name}"{time_filter}'}
        ]
    else:  # cal (中華航空)
        queries = [
            # 1. 經營、財報、月營收、重訊、高層談話 (加上完整雙引號)
            {"hint": "cat3", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
             "q": f'("中華航空" OR "華航") ("營收" OR "財報" OR "獲利" OR "法說會" OR "高星潢" OR "陳漢銘" OR "股利"){time_filter}'},
            # 2. 機隊、航點、培訓機師、社群話題
            {"hint": "cat2", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
             "q": f'("中華航空" OR "華航") ("培訓機師" OR "機師" OR "787" OR "A350" OR "777F" OR "開航" OR "天合聯盟"){time_filter}'},
            # 3. 一般台灣航空新聞稿 (最廣域保底必出結果)
            {"hint": "cat4", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
             "q": f'("中華航空" OR "華航") -職棒 -味全龍 -選舉{time_filter}'},
            # 4. 國際英文外電
            {"hint": "cat4", "hl": "en-US", "gl": "US", "ceid": "US:en",
             "q": f'"{en_name}"{time_filter}'}
        ]

    rss_list = []
    for item in queries:
        encoded_q = urllib.parse.quote(item["q"])
        url = f"https://news.google.com/rss/search?q={encoded_q}&hl={item['hl']}&gl={item['gl']}&ceid={item['ceid']}"
        rss_list.append({"url": url, "hint": item["hint"]})
    return rss_list

def fetch_raw_candidates(company_id: str, rss_configs: List[dict], history_data: dict, max_items: int = 15) -> List[dict]:
    """
    修復原因 B-2：使用 company_id 作為 seen_ids 前綴，並將 seen_titles 按航司分開儲存，
    確保星宇 (starlux) 與華航 (cal) 絕對不會互相誤擋新聞！
    """
    seen_ids = set(history_data.get("seen_ids", []))
    titles_by_comp = history_data.setdefault("seen_titles_by_company", {})
    seen_titles = titles_by_comp.setdefault(company_id, [])
    candidates = []

    for cfg in rss_configs:
        try:
            feed = feedparser.parse(cfg["url"])
            for entry in feed.entries[:12]:
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

                # 冠上 company_id 專屬前綴 (例如 cal_a1b2c3d4ff)
                item_id = make_short_id(clean_title, prefix=company_id)
                if item_id in seen_ids:
                    continue

                is_dup = False
                for old_title in seen_titles[-300:]:
                    if fuzz.token_set_ratio(clean_title, old_title) >= 82:
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
                    "hint": cfg["hint"],
                    "is_manual": False
                })

                if len(candidates) >= max_items:
                    break
        except Exception as e:
            print(f"⚠️ RSS 抓取略過異常 ({cfg['hint']}): {e}")
        if len(candidates) >= max_items:
            break

    history_data["seen_ids"] = list(seen_ids)[-2000:]
    titles_by_comp[company_id] = seen_titles[-800:]
    return candidates

def parse_all_manual_ig_inbox() -> dict:
    """讀取 manual_ig_inbox.txt，自動依標籤分發給 starlux 或 cal"""
    result = {"starlux": [], "cal": []}
    if not os.path.exists(MANUAL_INBOX_FILE):
        return result

    try:
        with open(MANUAL_INBOX_FILE, "r", encoding="utf-8") as f:
            lines = [line for line in f.read().splitlines() if not line.strip().startswith("#")]
            content = "\n".join(lines).strip()
    except Exception:
        return result

    if not content:
        return result

    blocks = content.split("---")
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        target_id = "starlux"
        lower_b = block.lower()
        if any(k in lower_b for k in ["中華航空", "華航", "[cal]", "china airlines"]):
            target_id = "cal"

        url = "https://www.instagram.com/"
        post_date = datetime.now().strftime("%Y-%m-%d")
        body_lines = []

        for line in block.splitlines():
            s_line = line.strip()
            if s_line in ["[星宇航空]", "[中華航空]", "[華航]", "[星宇]", "[STARLUX]", "[CAL]"]:
                continue
            if s_line.lower().startswith(("網址:", "網址：", "url:", "link:")):
                url = s_line.split(":", 1)[-1].split("：", 1)[-1].strip() or url
            elif s_line.lower().startswith(("日期:", "日期：", "date:")):
                post_date = s_line.split(":", 1)[-1].split("：", 1)[-1].strip() or post_date
            elif s_line.lower().startswith(("內容:", "內容：")):
                body_lines.append(s_line.split(":", 1)[-1].split("：", 1)[-1].strip())
            else:
                body_lines.append(s_line)

        full_text = "\n".join(body_lines).strip()
        if len(full_text) >= 5:
            result[target_id].append({
                "id": make_short_id(full_text, prefix=target_id),
                "title": f"[📸 官方IG精選] {full_text[:120]}",
                "raw_post_text": full_text,
                "source": "官方 Instagram / Threads",
                "url": url,
                "date": post_date,
                "hint": "cat2",
                "is_manual": True
            })
    return result

def reset_manual_ig_inbox():
    """分析完成後將投遞箱重置為乾淨提示模板"""
    template = "# 請在下方貼上 IG 或 Threads 貼文，多篇請用 --- 分隔（井字號開頭的說明文字會被自動忽略）\n"
    try:
        with open(MANUAL_INBOX_FILE, "w", encoding="utf-8") as f:
            f.write(template)
        print("🗑️ 手動 IG 投遞箱 (`manual_ig_inbox.txt`) 已分析完畢並自動重置！")
    except Exception as e:
        print(f"⚠️ 重置投遞箱失敗: {e}")

# ==========================================
# 4. Gemini AI 批次分析 (API 間隔調為 12 秒)
# ==========================================
def analyze_with_gemini(client: genai.Client, company_name: str, candidates: List[dict]) -> Optional[BatchAIResponse]:
    if not candidates:
        return None

    prompt = f"""
你是一位專業的「航空產業分析師」與「{company_name} 培訓機師面試教練」。
請審閱以下抓取到的原始新聞與官方 IG 手動貼文清單，並嚴格執行以下任務：

1. **嚴格雜訊剔除 (`is_relevant`)**：
   - 若 `is_manual` 為 `true`（代表使用者手動擷取的官方 IG/Threads 貼文），**請務必將 `is_relevant` 設為 `true`**，分類設為 `cat2`，標題加上 `[📸 官方IG精選] ` 前綴，並深入提煉其品牌行銷方向與培訓機師面試觀點（`pilot_view`）！
   - 一般新聞若與 {company_name}、民航產業、飛安、機隊、航線、財報無實質關聯（例如：政治選舉、職棒、娛樂八卦），請務必將 `is_relevant` 設為 `false`！
2. **語意去重與外電翻譯**：
   - 若清單中有多篇新聞在講「同一個事件」，請只保留最完整的一筆（其餘設為 `false`）。
   - 若為英文外電，請自動翻譯為台灣航空業慣用的繁體中文術語。
3. **精準分類 (`category`)**：
   - `cat1`：公司文化、高層談話、企業理念、機隊交機、新航點開闢、國際航空聯盟/夥伴合作。
   - `cat2`：社群討論（Dcard/PTT）、官方 IG/Threads 貼文分析、培訓機師考情、乘客體驗話題。
   - `cat3`：公司經營面、每月營收、季度財報、法說會、增資、發債、售後租回（SLB）或購機等商業操作。
   - `cat4`：一般新聞稿、促銷活動、聯名行銷、國際航空外電。
4. **重大商業與財務操作之「三欄視角判讀」**：
   - 針對 `cat3`、重大機隊航網決策或手動 IG 貼文，請提供 `bull_view`（🟢正方觀點）、`bear_view`（🔴反方隱憂）與 `pilot_view`（✈️培訓機師面試官視角）。
5. **自動擷取基本盤與財務時間軸異動 (`new_timeline_logs` & `new_finance_logs`)**：
   - 若包含具體的「新機交付、售後租回、新航線開航/增班、重要結盟、資本增資」，請提煉至 `new_timeline_logs`（務必帶入 `url` 與 `source`）。
   - 若包含具體的「某月營收數據、某季財報數據、重大財務購租機」，請提煉至 `new_finance_logs`（`event_type` 請務必歸類為 `月營收`、`季財報` 或 `重大財務操作`，並務必帶入 `url`）。

原始候選清單 JSON 如下：
{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""

    for model_name in [PRIMARY_MODEL, "gemini-2.0-flash"]:
        try:
            print(f"🤖 呼叫 Gemini 模型 ({model_name}) 進行 [{company_name}] 批次語意分析 (共 {len(candidates)} 筆候選)...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=BatchAIResponse,
                    temperature=0.2,
                ),
            )
            # 修復原因 B-1：強制等待 12 秒，避免觸發免費版 Rate Limit (429)
            time.sleep(12)
            return BatchAIResponse.model_validate_json(response.text)
        except Exception as e:
            err_msg = str(e)
            print(f"⚠️ 模型 {model_name} 呼叫異常: {err_msg[:200]}")
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                print("⏳ 偵測到 API 429 限流，等待 25 秒冷卻後重試...")
                time.sleep(25)
            else:
                time.sleep(12)
    return None

# ==========================================
# 5. 每月 1 號定期對帳 (Google Search Grounding)
# ==========================================
def monthly_grounding_audit(client: genai.Client, company_obj: dict):
    today = datetime.now()
    force_audit = os.environ.get("FORCE_MONTHLY_AUDIT", "false").lower() == "true"

    if today.day != 1 and not force_audit:
        return

    c_name = company_obj.get("company_name", "航空公司")
    print(f"🔍 [每月 1 號定期對帳] 啟動 Google Search Grounding 核對 {c_name} 最新基本盤...")
    try:
        audit_prompt = f"""
請透過 Google Search 搜尋「{c_name}」最新公開資訊，整理以下四個區塊（每個區塊包含 highlight 短標題與 details 三點說明陣列，其中 fleet 必須清楚列出總架數、各機型架數、自購與租賃架數比例及平均機齡），直接輸出純 JSON：
{{
  "philosophy": {{"title": "核心理念與企業文化", "highlight": "...", "details": ["...", "..."]}},
  "fleet": {{"title": "機隊規模、機齡與自有/租賃結構", "highlight": "...", "details": ["...", "...", "..."]}},
  "routes": {{"title": "航線網絡佈局 (台灣出發)", "highlight": "...", "details": ["...", "..."]}},
  "partners": {{"title": "國際合作夥伴與聯盟策略", "highlight": "...", "details": ["...", "..."]}}
}}
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=audit_prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        time.sleep(12)

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
        print(f"✅ [{c_name}] 每月 1 號企業基本盤更新完成！")
    except Exception as e:
        print(f"🛡️ [防呆保護啟動] {c_name} 每月對帳搜尋發生異常（已安全略過）: {e}")
        time.sleep(12)

# ==========================================
# 6. 資料合併與結構同步
# ==========================================
def merge_results_into_data(company_obj: dict, cfg: dict, ai_result: BatchAIResponse, force_manual: bool = False):
    if not ai_result:
        return

    cid = cfg["company_id"]
    ensure_company_structure(company_obj, cfg)
    cat_dict = {c["category_id"]: c["articles"] for c in company_obj["categories"]}

    added_count = 0
    for item in ai_result.processed_news:
        if not item.is_relevant and not force_manual:
            continue
        if not is_clean_aviation_text(item.title, item.summary, is_manual=force_manual):
            continue

        cat_key = "cat2" if force_manual else (item.category if item.category in cat_dict else "cat4")
        is_english = any(ord(ch) < 128 for ch in item.source) and " " in item.source and not force_manual

        news_dict = {
            "id": make_short_id(item.title, prefix=cid),
            "title": item.title,
            "translated_title": item.title,
            "date": item.date,
            "published_at": item.date,
            "source": item.source,
            "url": item.url,
            "link": item.url,
            "lang": "en" if is_english else "zh",
            "is_relevant": True,
            "is_manual": force_manual,
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

    # 更新 timeline_logs (近 5 筆異動)
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

    # 更新 finance_overview (擴充容量至 60 筆供前端月份篩選與展開歷史紀錄使用)
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
    fin_overview["history_logs"] = fin_logs[:60]

    print(f"📥 [{cfg['company_name']}] 本批次成功寫入 {added_count} 則精選情報！")

# ==========================================
# 7. 主程式執行流程 (雙航司完整回溯 + IG 投遞箱)
# ==========================================
def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ 找不到 GEMINI_API_KEY 環境變數！請確認已在 GitHub Secrets 設定。")

    client = genai.Client(api_key=api_key)
    today_str = datetime.now().strftime("%Y-%m-%d")

    default_data = {
        "last_updated": "",
        "backfill_cursor": today_str,
        "backfill_target_stop": BACKFILL_STOP_DATE,
        "companies": []
    }
    data = load_json(DATA_FILE, default_data)
    if "companies" not in data or not isinstance(data["companies"], list):
        data["companies"] = []

    default_history = {
        "backfill_cursor": data.get("backfill_cursor") or today_str,
        "backfill_cursors": {
            "starlux": data.get("backfill_cursor") or today_str,
            "cal": today_str
        },
        "seen_ids": [],
        "seen_titles_by_company": {"starlux": [], "cal": []}
    }
    history = load_json(HISTORY_FILE, default_history)
    cursors_map = history.setdefault("backfill_cursors", {
        "starlux": history.get("backfill_cursor") or today_str,
        "cal": today_str
    })

    # 先一次性讀取手動 IG 投遞箱
    manual_inbox_map = parse_all_manual_ig_inbox()
    has_manual_items = any(len(v) > 0 for v in manual_inbox_map.values())

    # 依序處理星宇航空 (starlux) 與中華航空 (cal)
    for cfg in COMPANIES_CONFIG:
        cid = cfg["company_id"]
        cname = cfg["company_name"]
        print(f"\n================ ✈️ 開始處理：{cname} ({cid}) ================")

        # 尋找或建立該航司物件 (絕不覆蓋既有文章)
        comp_obj = next((c for c in data["companies"] if (c.get("company_id") or c.get("id")) == cid), None)
        if not comp_obj:
            comp_obj = {"company_id": cid, "company_name": cname, "categories": []}
            data["companies"].append(comp_obj)

        # 階段 0：大掃除與基本盤掛載
        sanitize_existing_data(comp_obj, cfg)

        # 若發現該航司目前總文章數為 0 (例如先前華航因語法問題抓到 0 篇)，自動重置該航司的回溯游標與歷史紀錄，確保立刻抓滿！
        total_existing_articles = sum(len(cat.get("articles", [])) for cat in comp_obj.get("categories", []))
        if total_existing_articles == 0:
            print(f"🔄 偵測到 [{cname}] 目前文章數為 0，自動重置 {cname} 回溯游標至今日 ({today_str}) 以利完整抓取！")
            cursors_map[cid] = today_str
            history.setdefault("seen_titles_by_company", {})[cid] = []
            history["seen_ids"] = [sid for sid in history.get("seen_ids", []) if not sid.startswith(f"{cid}_")]

        # 階段 IG：處理手動投遞的 IG 貼文 (強制保送 cat2)
        manual_items = manual_inbox_map.get(cid, [])
        if manual_items:
            print(f"📸 [手動 IG 投遞箱] 偵測到 {len(manual_items)} 則 {cname} 貼文，送交 AI 深度分析...")
            ai_res_ig = analyze_with_gemini(client, cname, manual_items)
            merge_results_into_data(comp_obj, cfg, ai_res_ig, force_manual=True)

        # 階段 A：抓取今日最新情報
        print(f"🚀 [階段 A] 抓取 {cname} 今日最新即時情報...")
        latest_rss = build_rss_urls(cfg)
        latest_candidates = fetch_raw_candidates(cid, latest_rss, history, max_items=15)
        if latest_candidates:
            ai_res_latest = analyze_with_gemini(client, cname, latest_candidates)
            merge_results_into_data(comp_obj, cfg, ai_res_latest)
        else:
            print(f"ℹ️ {cname} 今日無新增之未讀即時新聞。")

        # 階段 B：漸進式歷史回溯 (各自獨立游標回推 30 天至 2025-09-01)
        c_cursor_str = cursors_map.get(cid, today_str)
        cursor_dt = datetime.strptime(c_cursor_str, "%Y-%m-%d")
        stop_dt = datetime.strptime(BACKFILL_STOP_DATE, "%Y-%m-%d")

        if cursor_dt > stop_dt:
            start_dt = max(stop_dt, cursor_dt - timedelta(days=BACKFILL_STEP_DAYS))
            after_str = start_dt.strftime("%Y-%m-%d")
            before_str = cursor_dt.strftime("%Y-%m-%d")
            print(f"⏪ [階段 B：{cname} 歷史回溯] 正在回補區間：{after_str} ~ {before_str} ...")

            backfill_rss = build_rss_urls(cfg, after_date=after_str, before_date=before_str)
            backfill_candidates = fetch_raw_candidates(cid, backfill_rss, history, max_items=18)
            if backfill_candidates:
                ai_res_backfill = analyze_with_gemini(client, cname, backfill_candidates)
                merge_results_into_data(comp_obj, cfg, ai_res_backfill)
            else:
                print(f"ℹ️ {cname} 歷史區間 {after_str} ~ {before_str} 無額外新情報。")

            cursors_map[cid] = after_str
            comp_obj["backfill_cursor"] = after_str
            print(f"📌 [{cname}] 歷史回溯游標已推進至：{after_str}（終點：{BACKFILL_STOP_DATE}）")
        else:
            comp_obj["backfill_cursor"] = BACKFILL_STOP_DATE
            print(f"🎉 [{cname}] 歷史回溯已抵達終點 ({BACKFILL_STOP_DATE})！")

        # 階段 C：每月 1 號 Google Search 對帳
        monthly_grounding_audit(client, comp_obj)
        comp_obj["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 若本次有處理手動 IG 貼文，重置投遞箱
    if has_manual_items:
        reset_manual_ig_inbox()

    # 階段 D：儲存結果並同步前端狀態
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["last_updated"] = now_str
    data["backfill_cursor"] = cursors_map.get("starlux", BACKFILL_STOP_DATE)
    data["backfill_target_stop"] = BACKFILL_STOP_DATE
    history["backfill_cursor"] = cursors_map.get("starlux", BACKFILL_STOP_DATE)

    save_json(DATA_FILE, data)
    save_json(HISTORY_FILE, history)
    print(f"\n✅ 全部航司任務執行完畢！資料已寫入 data.json 與 history.json ({now_str})")

if __name__ == "__main__":
    main()
