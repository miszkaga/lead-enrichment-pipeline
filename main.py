import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin, urlencode

import pandas as pd
import requests
from bs4 import BeautifulSoup

GUNB_BASE_URL = os.getenv("GUNB_BASE_URL", "https://wyszukiwarka.gunb.gov.pl/")
GUNB_SEARCH_PATH = os.getenv("GUNB_SEARCH_PATH", "")
OLX_BASE_URL = os.getenv("OLX_BASE_URL", "https://www.olx.pl")
OLX_CATEGORY_PATH = os.getenv("OLX_CATEGORY_PATH", "/praca/produkcja/")

OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

REQUEST_TIMEOUT = 20
REQUEST_DELAY_RANGE = (1.0, 2.5)
MAX_TEXT_LENGTH = 6000
TRUNCATION_BUFFER = 200

GUNB_KEYWORDS = (
    "hala",
    "magazyn",
    "produkcyj",
    "rozbudow",
    "budow",
    "centrum logistyczne",
    "zakład",
)

EXPANSION_ROLE_KEYWORDS = (
    "główny technolog",
    "chief technologist",
    "process engineer",
    "inżynier procesu",
    "inżynier procesowy",
    "kierownik produkcji",
    "manager produkcji",
    "technolog",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


@dataclass
class ScrapedItem:
    source: str
    title: Optional[str]
    company_name: Optional[str]
    location: Optional[str]
    url: Optional[str]
    text: str


def sleep_with_jitter() -> None:
    delay = random.uniform(*REQUEST_DELAY_RANGE)
    time.sleep(delay)


def fetch_html(session: requests.Session, url: str, params: Optional[dict] = None) -> Optional[str]:
    for attempt in range(1, 4):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            logging.warning("Request failed (%s/%s) for %s: %s", attempt, 3, url, exc)
            if attempt == 3:
                return None
            time.sleep(2 * attempt)
    return None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def truncate_text(text: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - TRUNCATION_BUFFER] + " ... [truncated]"


def is_relevant_gunb_row(text: str) -> bool:
    lower_text = text.lower()
    return any(keyword in lower_text for keyword in GUNB_KEYWORDS)


def extract_table_rows(table: BeautifulSoup) -> Iterable[tuple[BeautifulSoup, list[str]]]:
    for row in table.find_all("tr"):
        cols = [normalize_text(col.get_text(" ", strip=True)) for col in row.find_all(["td", "th"])]
        if cols:
            yield row, cols


def map_gunb_row(headers: list[str], row: list[str]) -> dict:
    mapped = {"company_name": None, "location": None, "details": " ".join(row)}
    for idx, header in enumerate(headers):
        value = row[idx] if idx < len(row) else ""
        header_lower = header.lower()
        if any(term in header_lower for term in ("inwestor", "nazwa", "firma")):
            mapped["company_name"] = value
        elif any(term in header_lower for term in ("miejsc", "miasto", "lokalizacja", "adres")):
            mapped["location"] = value
        elif any(term in header_lower for term in ("opis", "rodzaj", "zamierzenie", "obiekt")):
            mapped["details"] = value
    return mapped


def scrape_gunb_recent_applications(
    session: requests.Session, max_pages: int = 2, max_items: int = 40
) -> list[ScrapedItem]:
    items: list[ScrapedItem] = []
    base_url = urljoin(GUNB_BASE_URL, GUNB_SEARCH_PATH)
    if not base_url.endswith("/"):
        base_url += "/"

    for page in range(1, max_pages + 1):
        params = {"page": page}
        url = f"{base_url}?{urlencode(params)}" if params else base_url
        html = fetch_html(session, url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            logging.warning("No tables found on GUNB page %s", url)
            continue
        for table in tables:
            rows = list(extract_table_rows(table))
            if not rows:
                continue
            headers = rows[0][1]
            for row_element, row in rows[1:]:
                row_text = normalize_text(" ".join(row))
                if not is_relevant_gunb_row(row_text):
                    continue
                mapped = map_gunb_row(headers, row)
                link = row_element.find("a", href=True)
                detail_url = urljoin(GUNB_BASE_URL, link["href"]) if link else None
                items.append(
                    ScrapedItem(
                        source="GUNB",
                        title=mapped.get("details"),
                        company_name=mapped.get("company_name"),
                        location=mapped.get("location"),
                        url=detail_url,
                        text=row_text,
                    )
                )
                if len(items) >= max_items:
                    return items
        sleep_with_jitter()
    return items


def build_olx_search_url(page: int) -> str:
    base_url = urljoin(OLX_BASE_URL, OLX_CATEGORY_PATH)
    if page <= 1:
        return base_url
    return f"{base_url}?page={page}"


def parse_olx_listing_links(soup: BeautifulSoup) -> list[str]:
    links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/oferta/" not in href:
            continue
        if href.startswith("/"):
            href = urljoin(OLX_BASE_URL, href)
        if "olx.pl/oferta" in href:
            links.add(href.split("#")[0])
    return list(links)


def parse_olx_description(soup: BeautifulSoup) -> str:
    description_block = soup.select_one('[data-testid="ad_description"]')
    if description_block:
        return normalize_text(description_block.get_text(" ", strip=True))
    text_content = soup.find(id="textContent")
    if text_content:
        return normalize_text(text_content.get_text(" ", strip=True))
    return ""


def parse_olx_location(soup: BeautifulSoup) -> Optional[str]:
    location_node = soup.select_one('[data-testid="ad-location"]')
    if location_node:
        return normalize_text(location_node.get_text(" ", strip=True))
    location_date = soup.select_one('span[data-testid="location-date"]')
    if location_date:
        text = normalize_text(location_date.get_text(" ", strip=True))
        return text.split(" - ")[0]
    return None


def scrape_olx_job_offers(
    session: requests.Session, max_pages: int = 2, max_items: int = 40
) -> list[ScrapedItem]:
    items: list[ScrapedItem] = []
    for page in range(1, max_pages + 1):
        url = build_olx_search_url(page)
        html = fetch_html(session, url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        listing_links = parse_olx_listing_links(soup)
        for link in listing_links:
            detail_html = fetch_html(session, link)
            if not detail_html:
                continue
            detail_soup = BeautifulSoup(detail_html, "html.parser")
            title_node = detail_soup.find("h1")
            title = normalize_text(title_node.get_text(" ", strip=True)) if title_node else None
            description = parse_olx_description(detail_soup)
            location = parse_olx_location(detail_soup)
            combined_text = normalize_text(" ".join(filter(None, [title, location, description])))
            if not combined_text:
                continue
            if not any(keyword in combined_text.lower() for keyword in EXPANSION_ROLE_KEYWORDS):
                continue
            items.append(
                ScrapedItem(
                    source="OLX",
                    title=title,
                    company_name=None,
                    location=location,
                    url=link,
                    text=combined_text,
                )
            )
            if len(items) >= max_items:
                return items
            sleep_with_jitter()
        sleep_with_jitter()
    return items


def build_system_prompt() -> str:
    return (
        "You are a strict B2B lead qualification engine for Polish manufacturing SMEs. "
        "Evaluate if the company fits ALL criteria: manufacturing SME (micro/small/medium), "
        "located in Poland but NOT in Warsaw, Wrocław, or Poznań, NOT in agricultural production, "
        "and shows strong signals of green business/innovation investments (circular economy, "
        "energy-efficient machinery, thermomodernization, new green technologies) worth at least "
        "3,000,000 PLN. If any criterion is missing, reject. "
        "Respond ONLY with a JSON object matching schema: "
        '{"is_lead": boolean, "company_name": string|null, "location": string|null, '
        '"investment_signal": string, "reason": string}.'
    )


def call_openai_api(text: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is required.")
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": truncate_text(text)},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        OPENAI_API_URL, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return parse_json_response(content)


def parse_json_response(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def qualify_items(items: list[ScrapedItem]) -> list[dict]:
    leads: list[dict] = []
    for item in items:
        try:
            payload_text = normalize_text(
                "\n".join(
                    filter(
                        None,
                        [
                            f"Source: {item.source}",
                            f"Title: {item.title}",
                            f"Company: {item.company_name}",
                            f"Location: {item.location}",
                            f"URL: {item.url}",
                            f"Details: {item.text}",
                        ],
                    )
                )
            )
            result = call_openai_api(payload_text)
        except Exception as exc:
            logging.error("OpenAI qualification failed for %s: %s", item.url or item.title, exc)
            continue
        if result.get("is_lead") is True:
            result["source"] = item.source
            result["source_url"] = item.url
            leads.append(result)
        sleep_with_jitter()
    return leads


def export_leads(leads: list[dict], output_path: str) -> None:
    if not leads:
        logging.info("No leads qualified. Skipping export.")
        return
    df = pd.DataFrame(leads)
    df.to_excel(output_path, index=False)
    logging.info("Exported %s leads to %s", len(leads), output_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = requests.Session()
    session.headers.update(HEADERS)

    logging.info("Scraping GUNB data...")
    gunb_items = scrape_gunb_recent_applications(session)
    logging.info("Found %s GUNB items", len(gunb_items))

    logging.info("Scraping OLX data (HTML scraping chosen for robustness)...")
    olx_items = scrape_olx_job_offers(session)
    logging.info("Found %s OLX items", len(olx_items))

    all_items = gunb_items + olx_items
    if not all_items:
        logging.warning("No data collected from sources.")
        return

    logging.info("Qualifying %s items with OpenAI...", len(all_items))
    leads = qualify_items(all_items)
    export_leads(leads, "Green_Investments_Leads_MVP.xlsx")


if __name__ == "__main__":
    main()
