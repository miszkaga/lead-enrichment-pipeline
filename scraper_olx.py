import json
import logging
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- KONFIGURACJA OLX ---
OLX_BASE_URL = "https://www.olx.pl"
OLX_CATEGORY_PATH = "/praca/produkcja/"
OUTPUT_JSON_FILE = "raw_olx_data.json"

INVESTMENT_KEYWORDS = (
    "nowa hala", "nowej hali", "nowy zakład", "nowego zakładu",
    "nowa linia", "nowej linii", "rozbudow", "park maszynowy", "nowe maszyny",
    "inwestycj", "odpad", "recykling", "goz", "obieg zamknięty",
    "obiegu zamkniętego", "efektywność energetyczna", "oszczędność energii",
    "ochrona środowiska", "ekologiczn", "główny technolog", "inżynier procesu",
    "technolog", "kierownik projektu"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}

@dataclass
class ScrapedItem:
    source: str
    title: str
    company_name: Optional[str]
    location: Optional[str]
    url: str
    text: str

def sleep_with_jitter():
    time.sleep(random.uniform(1.0, 2.5))

def fetch_html(session, url):
    try:
        response = session.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logging.warning(f"Błąd pobierania OLX: {url} -> {e}")
        return None

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def parse_olx_description(soup: BeautifulSoup) -> str:
    selectors = ['[data-testid="ad_description"]', '[data-cy="ad_description"]', 'div.css-bgzo2k', 'div.css-1t507yq', '#textContent']
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            return normalize_text(node.get_text(" ", strip=True))
    paragraphs = soup.find_all("p")
    if paragraphs:
        full_text = " ".join([p.get_text(" ", strip=True) for p in paragraphs])
        if len(full_text) > 50: return normalize_text(full_text)
    return ""

def scrape_olx(session, max_pages=15):
    items = []
    for page in range(1, max_pages + 1):
        url = urljoin(OLX_BASE_URL, f"{OLX_CATEGORY_PATH}?page={page}") if page > 1 else urljoin(OLX_BASE_URL, OLX_CATEGORY_PATH)
        html = fetch_html(session, url)
        if not html: continue

        soup = BeautifulSoup(html, "html.parser")
        links = {urljoin(OLX_BASE_URL, a["href"].split("#")[0]) for a in soup.find_all("a", href=True) if "/oferta/" in a["href"]}

        for link in list(links):
            detail_html = fetch_html(session, link)
            if not detail_html: continue

            d_soup = BeautifulSoup(detail_html, "html.parser")
            t_node = d_soup.find("h1")
            title = normalize_text(t_node.get_text()) if t_node else ""
            desc = parse_olx_description(d_soup)

            combined = f"{title} {desc}"
            if any(kw in combined.lower() for kw in INVESTMENT_KEYWORDS):
                items.append(ScrapedItem(source="OLX", title=title, company_name=None, location=None, url=link, text=combined))
            sleep_with_jitter()
    return items

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = requests.Session()
    logging.info("Rozpoczynam scraping OLX...")
    olx_data = scrape_olx(session, max_pages=15) # Liczba stron do zeskrapowania

    with open(OUTPUT_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(item) for item in olx_data], f, ensure_ascii=False, indent=2)
    logging.info(f"Zapisano {len(olx_data)} surowych leadów z OLX do {OUTPUT_JSON_FILE}")

if __name__ == "__main__":
    main()