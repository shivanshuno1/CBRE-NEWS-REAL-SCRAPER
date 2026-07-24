import io
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import pandas as pd
import requests
import spacy
import trafilatura
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

# Maps a free-text location the user types in the frontend (e.g. "India",
# "United States", "UK") to the Google News gl (country) / hl (language)
# codes. Falls back to a sane global default if the location isn't recognized.
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
    # allow things like "in India" or "in the United States" typed straight in
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

# Words that are capitalized in headlines/business copy but are never part of
# a person's name. Used to reject false positives from the regex fallback.
NON_NAME_WORDS = {
    "retail", "expansion", "growth", "global", "group", "digital", "plan",
    "launch", "bets", "partners", "india", "asia", "south", "north", "east",
    "west", "office", "plant", "region", "market", "sector", "news", "update",
    "read", "more", "share", "top", "stories", "breaking", "said", "told",
    "added", "noted", "according", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday",
    # relative pronouns / conjunctions that should never appear inside a
    # person's name, as a second line of defense behind the capitalization
    # check above (covers the rare case where such a word is capitalized
    # because it sits at a sentence boundary)
    "who", "he", "she", "they", "after", "before", "when", "while", "that",
    "which", "whom", "whose",
}

NAME_CANDIDATE = re.compile(
    r"([A-Z][a-zA-Z.\-]+(?:\s+[A-Z][a-zA-Z.\-]+){0,3}),\s*(?:" + ROLE_ALTERNATION.pattern + r")"
)

SENTENCE_SPLIT_REGEX = re.compile(r"(?<=[.!?])\s+")

# Load the spaCy English model once at import time. This is the primary
# name-extraction signal (see extract_names_and_roles below); it is far more
# reliable than plain capitalization regexes because it understands sentence
# grammar, not just letter casing.
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
    extracted_people: list = field(default_factory=list)  # list[ExtractedPerson]
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

    # Primary path: trafilatura isolates the actual article body and drops
    # boilerplate - ads, video player captions, "related articles" widgets,
    # comment sections, cookie banners, etc. This is what was previously
    # letting junk like an unrelated ad's text ("Bets On") leak into the
    # name extractor, since raw BeautifulSoup get_text() on the full page
    # includes every visible string on the page, not just the article.
    extracted = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if extracted and len(extracted.strip()) > 200:
        return re.sub(r"\s+", " ", extracted).strip()

    # Fallback: some sites (heavy JS rendering, unusual markup) confuse
    # trafilatura's boilerplate detector. Fall back to the broader page-text
    # sweep so we still get *something*, rather than nothing.
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
    """A bare role word ('Partner', 'President'...) is never a person's name.
    Guards against spaCy occasionally mistagging a standalone title as PERSON
    in comma-separated title lists like 'Name, President, Partner and VP...'"""
    return name.lower().strip() in ROLE_SET_LOWER

def _is_properly_capitalized(name: str) -> bool:
    """Every word in a real name starts with an uppercase letter. spaCy's
    PERSON entity boundaries occasionally swallow surrounding pronouns/verbs
    on noisy or malformed article text (e.g. 'who recovered after he'), which
    are lowercase mid-sentence words. NER gives us a candidate span; this is
    the sanity check that the span actually looks like a name before we
    trust it, applied uniformly to both extraction passes."""
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

def _spacy_person_pass(account: str, sentence: str, doc, role_matches) -> list:
    """Primary extraction path: spaCy's NER PERSON entities. This is what
    correctly drops reporting-verb artifacts like 'said Tim Coogan' (spaCy
    recognizes 'said' as a verb, not part of the name) and headline noise
    like 'Bets On' (never tagged as a person)."""
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

def _regex_fallback_pass(account: str, sentence: str, doc, role_matches) -> list:
    """Fallback path, used only for sentences where spaCy found no PERSON
    entity at all (e.g. single-token Indian names like 'Rajanna' that a
    small English NER model sometimes misses). Every candidate must satisfy
    the common name filters AND have every token POS-tagged as a proper
    noun (PROPN) by spaCy - this is what rejects false positives such as
    'Retail Expansion, Partner' that a plain capitalization regex would
    otherwise happily accept."""
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

def extract_names_and_roles(account: str, article_text: str) -> list:
    """Returns a list of ExtractedPerson(name, role) pairs found in the
    article text, restricted to sentences that mention both the account
    name and a known role keyword."""
    account_lower = account.lower()
    people: list = []
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
    headless: bool = Form(True),
    location: str = Form("India"),
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
        gl, lang = resolve_country_code(location)
        location_suffix = location.strip() if location and location.strip() else "India"
        
        driver = build_driver(headless=headless)
        all_results = []
        
        try:
            for account in account_names:
                for keyword in growth_keywords:
                    query = f"{account} {keyword} in {location_suffix}"
                    print(f"Searching: {query}")
                    
                    try:
                        articles = search_google_news(driver, account, keyword, query, days=days, gl=gl, lang=lang)
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
                            
                        people = extract_names_and_roles(account, text)
                        all_results.append(ArticleResult(
                            account_name=account, keyword=keyword, query=query,
                            article_title=title, article_url=url,
                            extracted_people=people,
                            status="OK" if people else "NO_NAME_FOUND"
                        ))
                        time.sleep(SLEEP_BETWEEN_ARTICLES)
                    
                    time.sleep(SLEEP_BETWEEN_QUERIES)
        finally:
            driver.quit()
            
        # Format the scraped results into flat dictionaries for the React table.
        # Each person gets their own row with their own correctly matched role
        # (previously names and roles were collected into two separate lists
        # with no pairing between them, which could mismatch a name with the
        # wrong role when an article mentioned multiple people).
        rows = []
        for r in all_results:
            base_dict = {
                "Account Name": r.account_name,
                "Growth Keyword": r.keyword,
                "Search Query": r.query,
                "Article Title": r.article_title,
                "Article URL": r.article_url,
                "Status": r.status,
            }
            if r.extracted_people:
                for person in r.extracted_people:
                    rows.append({
                        **base_dict,
                        "Extracted Name": person.name,
                        "Roles Found": person.role,
                    })
            else:
                rows.append({**base_dict, "Extracted Name": "", "Roles Found": ""})
                
        return {"data": rows}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # This block allows you to just run `python main.py` directly if you want
    uvicorn.run(app, host="0.0.0.0", port=8000)