import io
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import pandas as pd
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

# ============================================================
# CONSTANTS & REGEX DEFINITIONS (From your V2 Scraper)
# ============================================================

SEARCH_URL_TEMPLATE = "https://news.google.com/search?q={query}&hl=en-IN&gl=IN&ceid=IN%3Aen"
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

ROLE_PATTERNS = [
    r"Chief Executive Officer", r"CEO", r"Managing Director", r"\bMD\b",
    r"Co-Founder", r"Co Founder", r"Founder", r"Managing Head", r"Chairman",
    r"President", r"Executive Director", r"Director", r"Head" , r"Vice President", r"VP", r"General Manager", r"GM", r"Partner",
    r"Chief Minister"
]
ROLE_ALTERNATION = "|".join(ROLE_PATTERNS)

TITLE_PREFIX_WORDS = [
    "Senior", "Group", "Global", "Regional", "Deputy", "Chief", "Vice",
    "Country", "Executive", "Managing", "Associate", "Assistant", "Joint",
]
TITLE_PREFIX_PATTERN = r"(?:(?:" + "|".join(TITLE_PREFIX_WORDS) + r")[\-\s]+)*"

NAME_TOKEN = r"[A-Z][a-zA-Z\.\-]+(?:\s+[A-Z][a-zA-Z\.\-]+){1,3}"
ROLE_REGEX = re.compile(
    r"(" + NAME_TOKEN + r"),\s+" + TITLE_PREFIX_PATTERN + r"(?:" + ROLE_ALTERNATION + r")"
)

NAME_BLOCKLIST = {
    "the republican", "supreme court", "tracking u.s", "tracking u.s.",
    "share", "read more", "top stories", "breaking news",
}

_REJECT_WORDS = {w.lower() for w in TITLE_PREFIX_WORDS} | {
    "president", "director", "head", "chairman", "founder", "ceo", "md",
    "officer", "chief", "india", "sri", "lanka", "bangladesh",
}

SENTENCE_SPLIT_REGEX = re.compile(r"(?<=[.!?])\s+")

@dataclass
class ArticleResult:
    account_name: str
    keyword: str
    query: str
    article_title: str = ""
    article_url: str = ""
    extracted_names: list = field(default_factory=list)
    extracted_roles: list = field(default_factory=list)
    status: str = "OK"

# ============================================================
# SCRAPER LOGIC
# ============================================================

def build_driver(headless: bool = False) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1000")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver

def build_search_url(query: str, days: int) -> str:
    full_query = f"{query} when:{days}d"
    encoded = urllib.parse.quote(full_query)
    return SEARCH_URL_TEMPLATE.format(query=encoded)

def find_article_elements(driver):
    for selector in ARTICLE_LINK_SELECTORS:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            return elements
    return []

def search_google_news(driver, account: str, keyword: str, query: str, days: int) -> list:
    url = build_search_url(query, days)
    driver.get(url)

    try:
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            lambda d: find_article_elements(d)
        )
    except TimeoutException:
        print(f"  [!] No results rendered for: {query}")
        return []

    time.sleep(1.5)  # let lazy content settle
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

        # RELEVANCE FILTER
        if account_lower not in title_lower or keyword_lower not in title_lower:
            continue

        absolute = urllib.parse.urljoin("https://news.google.com", href)
        candidates.append((title, absolute))

        if len(candidates) >= MAX_ARTICLES_PER_QUERY:
            break

    # Resolve each candidate's real publisher URL
    resolved = []
    for title, google_url in candidates:
        real_url = resolve_redirect(driver, google_url)
        if real_url:
            resolved.append((title, real_url))

    return resolved

def resolve_redirect(driver, google_news_url: str) -> str:
    original_window = driver.current_window_handle
    driver.execute_script("window.open(arguments[0]);", google_news_url)
    time.sleep(0.5)
    windows = driver.window_handles
    new_window = [w for w in windows if w != original_window][-1]
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

    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text

def clean_name(name: str) -> str:
    return re.sub(r"[.,]+$", "", name).strip()

def is_valid_name(name: str, account: str = "") -> bool:
    if not name: return False
    lname = name.lower().strip(" .,")
    if lname in NAME_BLOCKLIST: return False
    words = name.split()
    if len(words) < 2 or len(words) > 4: return False
    if any(len(w.strip(".-")) < 2 for w in words): return False
    if any(w.strip(".,-").lower() in _REJECT_WORDS for w in words): return False
    if account and account.lower() in lname: return False
    return True

def extract_names_and_roles(account: str, article_text: str) -> tuple:
    account_lower = account.lower()
    names_found = set()
    roles_found = set()

    sentences = SENTENCE_SPLIT_REGEX.split(article_text)

    for sentence in sentences:
        sentence_lower = sentence.lower()
        has_company = account_lower in sentence_lower
        has_role = re.search(ROLE_ALTERNATION, sentence, flags=re.IGNORECASE)

        if not (has_company and has_role):
            continue

        for match in ROLE_REGEX.finditer(sentence):
            name = match.group(1)
            if name:
                name = clean_name(name)
                if is_valid_name(name, account):
                    names_found.add(name)

        for role in ROLE_PATTERNS:
            if re.search(role, sentence, flags=re.IGNORECASE):
                roles_found.add(role.replace(r"\b", ""))

    return list(names_found), list(roles_found)


# ============================================================
# FASTAPI BACKEND SERVER
# ============================================================

app = FastAPI()

# Allow React (Vite uses 5173 by default, older React apps use 3000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/scrape")
async def run_scraper_endpoint(
    file: UploadFile = File(...),
    name_col: str = Form("Account_name"),
    keywords_str: str = Form("opens,launches"),
    days: int = Form(120),
    headless: bool = Form(True)
):
    try:
        # Read the uploaded Excel file into Pandas
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        
        if name_col not in df.columns:
            raise HTTPException(
                status_code=400, 
                detail=f"Column '{name_col}' not found. Available columns: {list(df.columns)}"
            )
            
        account_names = df[name_col].dropna().astype(str).tolist()
        growth_keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
        
        driver = build_driver(headless=headless)
        all_results = []
        
        try:
            for account in account_names:
                for keyword in growth_keywords:
                    query = f"{account} {keyword} in India"
                    print(f"Searching: {query}")
                    
                    try:
                        articles = search_google_news(driver, account, keyword, query, days=days)
                    except Exception as e:
                        print(f"  [!] Search failed: {e}")
                        all_results.append(ArticleResult(
                            account_name=account, keyword=keyword, query=query, 
                            status=f"SEARCH_FAILED: {e}"
                        ))
                        time.sleep(SLEEP_BETWEEN_QUERIES)
                        continue
                        
                    if not articles:
                        all_results.append(ArticleResult(
                            account_name=account, keyword=keyword, query=query, 
                            status="NO_RELEVANT_NEWS"
                        ))
                        time.sleep(SLEEP_BETWEEN_QUERIES)
                        continue
                        
                    for title, url in articles:
                        text = fetch_article(url)
                        if not text:
                            all_results.append(ArticleResult(
                                account_name=account, keyword=keyword, query=query, 
                                article_title=title, article_url=url, status="FETCH_FAILED"
                            ))
                            continue
                            
                        names, roles = extract_names_and_roles(account, text)
                        all_results.append(ArticleResult(
                            account_name=account, keyword=keyword, query=query,
                            article_title=title, article_url=url,
                            extracted_names=names, extracted_roles=roles,
                            status="OK" if names else "NO_NAME_FOUND"
                        ))
                        time.sleep(SLEEP_BETWEEN_ARTICLES)
                    
                    time.sleep(SLEEP_BETWEEN_QUERIES)
        finally:
            driver.quit()
            
        # Format the scraped results into flat dictionaries for the React table
        rows = []
        for r in all_results:
            base_dict = {
                "Account Name": r.account_name, 
                "Growth Keyword": r.keyword,
                "Search Query": r.query, 
                "Article Title": r.article_title,
                "Article URL": r.article_url, 
                "Roles Found": ", ".join(r.extracted_roles),
                "Status": r.status
            }
            if r.extracted_names:
                for name in r.extracted_names:
                    rows.append({**base_dict, "Extracted Name": name})
            else:
                rows.append({**base_dict, "Extracted Name": ""})
                
        return {"data": rows}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # This block allows you to just run `python main.py` directly if you want
    uvicorn.run(app, host="0.0.0.0", port=8000)