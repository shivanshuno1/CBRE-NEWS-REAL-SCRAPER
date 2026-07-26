import io
import json
import os
import re
import threading
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Any

import pandas as pd
import requests
import spacy
import trafilatura
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import chromedriver_autoinstaller  

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

# ============================================================
# CONSTANTS & REGEX DEFINITIONS
# ============================================================

SEARCH_URL_TEMPLATE = "https://news.google.com/search?q={query}&hl={hl}&gl={gl}&ceid={gl}%3A{lang}"
ARTICLE_LINK_SELECTORS = [
    "a.JtKRv",       # headline links on search results page (current)
    "a.WwrzSb",      # older markup, kept as fallback
    "article a",     # generic fallback
]

PAGE_LOAD_TIMEOUT = 25
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_QUERIES = 2
SLEEP_BETWEEN_ARTICLES = 1
MAX_ARTICLES_PER_QUERY = 3

# ============================================================
# CHECKPOINT SYSTEM
# ============================================================
CHECKPOINT_DIR = "checkpoints"
os.makedirs(CHECKPOINT_DIR, exist_ok=True)

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def job_dir(job_id: str) -> str:
    d = os.path.join(CHECKPOINT_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


# --- Atomic writers ---
def atomic_write_json(data: dict, path: str) -> None:
    dirname = os.path.dirname(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=dirname, delete=False, suffix='.json') as tmp:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, path)


def atomic_write_excel(df: pd.DataFrame, path: str) -> None:
    dirname = os.path.dirname(path)
    with tempfile.NamedTemporaryFile(mode='wb', dir=dirname, delete=False, suffix='.xlsx') as tmp:
        df.to_excel(tmp, index=False)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, path)


def save_checkpoint(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        snapshot = {
            "job_id": job_id,
            "status": job["status"],
            "total_accounts": job["total_accounts"],
            "completed_accounts": job["completed_accounts"],
            "results": job["results"],
            "location_suffix": job.get("location_suffix", ""),
        }

    json_path = os.path.join(job_dir(job_id), "checkpoint.json")
    atomic_write_json(snapshot, json_path)

    # Also update Excel file (atomic)
    if snapshot["results"]:
        df = pd.DataFrame(snapshot["results"])
        xlsx_path = os.path.join(job_dir(job_id), "checkpoint.xlsx")
        atomic_write_excel(df, xlsx_path)


def load_checkpoint(job_id: str) -> dict | None:
    json_path = os.path.join(job_dir(job_id), "checkpoint.json")
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            return json.load(f)
    return None


def load_all_checkpoints() -> None:
    """Load all existing checkpoint jobs into JOBS dict on server startup."""
    for job_id in os.listdir(CHECKPOINT_DIR):
        json_path = os.path.join(CHECKPOINT_DIR, job_id, "checkpoint.json")
        if os.path.exists(json_path):
            data = load_checkpoint(job_id)
            if data:
                with JOBS_LOCK:
                    JOBS[job_id] = {
                        "status": data["status"],
                        "results": data["results"],
                        "stop_requested": False,
                        "total_accounts": data["total_accounts"],
                        "completed_accounts": data["completed_accounts"],
                        "location_suffix": data.get("location_suffix", ""),
                        "error": None,
                    }


# ============================================================
# COUNTRY & ROLE HELPERS
# ============================================================

COUNTRY_CODE_MAP = {
    "india": ("IN", "en"),
    "united states": ("US", "en"), "usa": ("US", "en"), "us": ("US", "en"),
    "united kingdom": ("GB", "en"), "uk": ("GB", "en"),
    "canada": ("CA", "en"),
    "australia": ("AU", "en"),
    "singapore": ("SG", "en"),
    "uae": ("AE", "en"), "united arab emirates": ("AE", "en"),
    "germany": ("DE", "de"),
    "france": ("FR", "fr"),
    "japan": ("JP", "ja"),
    "china": ("CN", "zh-CN"),
    "brazil": ("BR", "pt-419"),
    "south africa": ("ZA", "en"),
}
DEFAULT_COUNTRY = ("US", "en")


def resolve_country_code(location: str):
    key = (location or "").strip().lower()
    key = re.sub(r"^\s*in\s+(the\s+)?", "", key)
    return COUNTRY_CODE_MAP.get(key, DEFAULT_COUNTRY)


ROLE_PATTERNS = [
    "Chief Executive Officer", "CEO", "Managing Director", "MD",
    "Co-Founder", "Co Founder", "Founder", "Managing Head", "Chairman",
    "President", "Executive Director", "Director", "Head", "Vice President",
    "VP", "General Manager", "GM", "Partner", "Chief Minister",
]
ROLE_SET_LOWER = {r.lower() for r in ROLE_PATTERNS}
ROLE_ALTERNATION = re.compile(
    r"\b(" + "|".join(re.escape(r) for r in sorted(ROLE_PATTERNS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

NON_NAME_WORDS = {
    "retail", "expansion", "growth", "global", "group", "digital", "plan",
    "launch", "bets", "partners", "india", "asia", "south", "north", "east",
    "west", "office", "plant", "region", "market", "sector", "news", "update",
    "read", "more", "share", "top", "stories", "breaking", "said", "told",
    "added", "noted", "according", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday",
    "who", "he", "she", "they", "after", "before", "when", "while", "that",
    "which", "whom", "whose",
}

NAME_CANDIDATE = re.compile(
    r"([A-Z][a-zA-Z.\-]+(?:\s+[A-Z][a-zA-Z.\-]+){0,3}),\s*(?:" + ROLE_ALTERNATION.pattern + r")"
)

SENTENCE_SPLIT_REGEX = re.compile(r"(?<=[.!?])\s+")

_NLP = spacy.load("en_core_web_sm")


@dataclass
class ExtractedPerson:
    name: str
    role: str


@dataclass
class ArticleResult:
    account_name: str
    keyword: str
    query: str
    article_title: str = ""
    article_url: str = ""
    extracted_people: List[ExtractedPerson] = field(default_factory=list)
    status: str = "OK"


# ============================================================
# SCRAPER LOGIC
# ============================================================

_DRIVER_PATH = None
_DRIVER_PATH_LOCK = threading.Lock()

def get_chromedriver_path() -> str:
    global _DRIVER_PATH
    with _DRIVER_PATH_LOCK:
        if _DRIVER_PATH is None:
            _DRIVER_PATH = ChromeDriverManager().install()
        return _DRIVER_PATH


def build_driver(headless: bool = False) -> webdriver.Chrome:
    print(f"🔍 Building driver with headless={headless}")
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--start-maximized")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
    try:
        driver_path = get_chromedriver_path()
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"❌ Failed to launch Chrome driver: {e}")
        raise
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver
def build_search_url(query: str, days: int, gl: str = "US", lang: str = "en") -> str:
    full_query = f"{query} when:{days}d"
    encoded = urllib.parse.quote(full_query)
    hl = f"{lang}-{gl}"
    return SEARCH_URL_TEMPLATE.format(query=encoded, hl=hl, gl=gl, lang=lang)


def find_article_elements(driver):
    for selector in ARTICLE_LINK_SELECTORS:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            return elements
    return []


def search_google_news(driver, account: str, keyword: str, query: str, days: int, gl: str = "US", lang: str = "en") -> list:
    url = build_search_url(query, days, gl=gl, lang=lang)
    driver.get(url)

    try:
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            lambda d: find_article_elements(d)
        )
    except TimeoutException:
        print(f"  [!] No results rendered for: {query}")
        return []

    time.sleep(1.5)
    elements = find_article_elements(driver)

    account_lower = account.lower()
    keyword_lower = keyword.lower()
    candidates = []

    for el in elements:
        try:
            title = el.text.strip()
            href = el.get_attribute("href")
        except Exception:
            continue

        if not title or not href:
            continue

        title_lower = title.lower()

        if account_lower not in title_lower or keyword_lower not in title_lower:
            continue

        absolute = urllib.parse.urljoin("https://news.google.com", href)
        candidates.append((title, absolute))

        if len(candidates) >= MAX_ARTICLES_PER_QUERY:
            break

    resolved = []
    for title, google_url in candidates:
        real_url = resolve_redirect(driver, google_url)
        if real_url:
            resolved.append((title, real_url))

    return resolved


def resolve_redirect(driver, google_news_url: str) -> str:
    original_window = driver.current_window_handle
    driver.execute_script("window.open(arguments[0]);", google_news_url)
    # Wait for the new window to appear
    WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > 1)
    new_window = [w for w in driver.window_handles if w != original_window][0]
    driver.switch_to.window(new_window)

    try:
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            lambda d: d.current_url and "news.google.com" not in d.current_url
        )
        final_url = driver.current_url
    except TimeoutException:
        final_url = driver.current_url

    driver.close()
    driver.switch_to.window(original_window)
    return final_url


def fetch_article(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] Failed to fetch {url}: {e}")
        return ""

    extracted = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if extracted and len(extracted.strip()) > 200:
        return re.sub(r"\s+", " ", extracted).strip()

    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav",
                       "aside", "form", "button", "figcaption"]):
        tag.decompose()
    for tag in soup.select(
        "[class*=ad], [id*=ad], [class*=advert], [class*=promo], [class*=related], "
        "[class*=recommend], [class*=video], [class*=player], [class*=cookie], "
        "[class*=newsletter], [class*=subscribe], [class*=comment]"
    ):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text


def clean_name(name: str) -> str:
    return re.sub(r"[.,]+$", "", name).strip()


def _is_role_word(name: str) -> bool:
    return name.lower().strip() in ROLE_SET_LOWER


def _is_properly_capitalized(name: str) -> bool:
    words = name.split()
    if not words:
        return False
    return all(w[0].isupper() for w in words if w)


def _passes_common_filters(name: str, account: str) -> bool:
    if not name or _is_role_word(name):
        return False
    if not _is_properly_capitalized(name):
        return False
    words = name.split()
    if len(words) < 1 or len(words) > 4:
        return False
    if any(w.strip(".-").lower() in NON_NAME_WORDS for w in words):
        return False
    if account and account.lower() in name.lower():
        return False
    return True


def _nearest_role(role_matches, char_pos: int) -> str:
    if not role_matches:
        return ""
    nearest = min(role_matches, key=lambda m: abs(m.start() - char_pos))
    return nearest.group(0)


def _spacy_person_pass(account: str, sentence: str, doc, role_matches) -> List[ExtractedPerson]:
    out = []
    for ent in doc.ents:
        if ent.label_ != "PERSON":
            continue
        name = clean_name(ent.text)
        if not _passes_common_filters(name, account):
            continue
        role = _nearest_role(role_matches, ent.start_char)
        out.append(ExtractedPerson(name=name, role=role))
    return out


def _regex_fallback_pass(account: str, sentence: str, doc, role_matches) -> List[ExtractedPerson]:
    out = []
    for m in NAME_CANDIDATE.finditer(sentence):
        name = clean_name(m.group(1))
        if not _passes_common_filters(name, account):
            continue
        span = doc.char_span(m.start(1), m.start(1) + len(m.group(1)), alignment_mode="expand")
        if span is None or not all(tok.pos_ == "PROPN" for tok in span):
            continue
        role = _nearest_role(role_matches, m.start(1))
        out.append(ExtractedPerson(name=name, role=role))
    return out


def extract_names_and_roles(account: str, article_text: str) -> List[ExtractedPerson]:
    account_lower = account.lower()
    people: List[ExtractedPerson] = []
    seen_names = set()

    sentences = SENTENCE_SPLIT_REGEX.split(article_text)

    for sentence in sentences:
        if account_lower not in sentence.lower():
            continue
        role_matches = list(ROLE_ALTERNATION.finditer(sentence))
        if not role_matches:
            continue

        doc = _NLP(sentence)
        found = _spacy_person_pass(account, sentence, doc, role_matches)
        if not found:
            found = _regex_fallback_pass(account, sentence, doc, role_matches)

        for person in found:
            key = person.name.lower()
            if key not in seen_names:
                seen_names.add(key)
                people.append(person)

    return people


def article_result_to_rows(r: ArticleResult) -> List[Dict[str, Any]]:
    base_dict = {
        "Account Name": r.account_name,
        "Growth Keyword": r.keyword,
        "Search Query": r.query,
        "Article Title": r.article_title,
        "Article URL": r.article_url,
        "Status": r.status,
    }
    rows = []
    if r.extracted_people:
        for person in r.extracted_people:
            rows.append({**base_dict, "Extracted Name": person.name, "Roles Found": person.role})
    else:
        rows.append({**base_dict, "Extracted Name": "", "Roles Found": ""})
    return rows


# ============================================================
# BACKGROUND JOB RUNNER
# ============================================================
def run_scrape_job(job_id: str, account_names: List[str], growth_keywords: List[str],
                    days: int, headless: bool, gl: str, lang: str):
    driver = None
    try:
        driver = build_driver(headless=headless)

        for account in account_names:
            # Check stop at start of account
            with JOBS_LOCK:
                if JOBS[job_id]["stop_requested"]:
                    JOBS[job_id]["status"] = "stopped"
                    break

            for keyword in growth_keywords:
                with JOBS_LOCK:
                    if JOBS[job_id]["stop_requested"]:
                        JOBS[job_id]["status"] = "stopped"
                        save_checkpoint(job_id)
                        return

                query = f"{account} {keyword} in {JOBS[job_id]['location_suffix']}"
                print(f"Searching: {query}")

                try:
                    articles = search_google_news(driver, account, keyword, query, days=days, gl=gl, lang=lang)
                except Exception as e:
                    print(f"  [!] Search failed: {e}")
                    rows = article_result_to_rows(ArticleResult(
                        account_name=account, keyword=keyword, query=query,
                        status=f"SEARCH_FAILED: {e}"
                    ))
                    with JOBS_LOCK:
                        JOBS[job_id]["results"].extend(rows)
                    time.sleep(SLEEP_BETWEEN_QUERIES)
                    continue

                if not articles:
                    rows = article_result_to_rows(ArticleResult(
                        account_name=account, keyword=keyword, query=query,
                        status="NO_RELEVANT_NEWS"
                    ))
                    with JOBS_LOCK:
                        JOBS[job_id]["results"].extend(rows)
                    time.sleep(SLEEP_BETWEEN_QUERIES)
                    continue

                for title, url in articles:
                    with JOBS_LOCK:
                        if JOBS[job_id]["stop_requested"]:
                            JOBS[job_id]["status"] = "stopped"
                            save_checkpoint(job_id)
                            return

                    text = fetch_article(url)
                    if not text:
                        rows = article_result_to_rows(ArticleResult(
                            account_name=account, keyword=keyword, query=query,
                            article_title=title, article_url=url, status="FETCH_FAILED"
                        ))
                        with JOBS_LOCK:
                            JOBS[job_id]["results"].extend(rows)
                        continue

                    people = extract_names_and_roles(account, text)
                    rows = article_result_to_rows(ArticleResult(
                        account_name=account, keyword=keyword, query=query,
                        article_title=title, article_url=url,
                        extracted_people=people,
                        status="OK" if people else "NO_NAME_FOUND"
                    ))
                    with JOBS_LOCK:
                        JOBS[job_id]["results"].extend(rows)
                    time.sleep(SLEEP_BETWEEN_ARTICLES)

                save_checkpoint(job_id)
                time.sleep(SLEEP_BETWEEN_QUERIES)

            with JOBS_LOCK:
                JOBS[job_id]["completed_accounts"] += 1
            save_checkpoint(job_id)

        with JOBS_LOCK:
            if JOBS[job_id]["status"] != "stopped":
                JOBS[job_id]["status"] = "completed"

    except Exception as e:
        print(f"❌ Job {job_id} failed: {e}")
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
    finally:
        if driver is not None:
            driver.quit()
        save_checkpoint(job_id)

# ============================================================
# FASTAPI BACKEND SERVER
# ============================================================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/scrape/start")
async def start_scrape(
    file: UploadFile = File(...),
    name_col: str = Form("Account_name"),
    keywords_str: str = Form("opens,launches"),
    days: int = Form(120),
    headless: str = Form("true"),   # ← changed to string
    location: str = Form("India"),
):
    contents = await file.read()
    df = pd.read_excel(io.BytesIO(contents))

    if name_col not in df.columns:
        raise HTTPException(
            status_code=400,
            detail=f"Column '{name_col}' not found. Available columns: {list(df.columns)}"
        )

    account_names = df[name_col].dropna().astype(str).tolist()
    growth_keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
    gl, lang = resolve_country_code(location)
    location_suffix = location.strip() if location and location.strip() else "India"

    headless_bool = headless.lower() == "true"   # ← convert to boolean

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "results": [],
            "stop_requested": False,
            "total_accounts": len(account_names),
            "completed_accounts": 0,
            "location_suffix": location_suffix,
            "error": None,
        }

    thread = threading.Thread(
        target=run_scrape_job,
        args=(job_id, account_names, growth_keywords, days, headless_bool, gl, lang),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/scrape/status/{job_id}")
async def scrape_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job_id")
        return {
            "status": job["status"],
            "total_accounts": job["total_accounts"],
            "completed_accounts": job["completed_accounts"],
            "results": job["results"],
            "error": job["error"],
        }


@app.post("/api/scrape/stop/{job_id}")
async def stop_scrape(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job_id")
        job["stop_requested"] = True
    save_checkpoint(job_id)
    return {"ok": True}


@app.get("/api/scrape/download/{job_id}")
async def download_checkpoint(job_id: str):
    xlsx_path = os.path.join(job_dir(job_id), "checkpoint.xlsx")
    if not os.path.exists(xlsx_path):
        raise HTTPException(status_code=404, detail="No checkpoint saved yet for this job")
    return FileResponse(
        xlsx_path,
        filename=f"scraper_checkpoint_{job_id[:8]}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ============================================================
# STARTUP – LOAD EXISTING CHECKPOINTS
# ============================================================
load_all_checkpoints()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)