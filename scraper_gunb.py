import json
import logging
import zipfile
import io
import csv
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
import requests

# Zwiększenie limitu wielkości pola CSV do maksymalnej wartości obsługiwanej przez system
csv.field_size_limit(sys.maxsize)

# --- KONFIGURACJA GUNB API ---
DATASET_API_URL = "https://api.dane.gov.pl/1.4/datasets/3467/resources"
WOJEWODZTWA = ["śląskie", "łódzkie", "małopolskie", "pomorskie", "dolnośląskie", "zachodniopomorskie", "kujawsko-pomorskie"]
OUTPUT_JSON_FILE = "raw_gunb_data.json"

# Ustawione na 3650 do testów. Zmień z powrotem na 7, gdy upewnisz się, że działa!
DATE_FILTER_DAYS = 180
MAX_ROWS_PER_FILE = 500

# Rozszerzone słowa kluczowe dla inwestycji kubaturowych / produkcyjnych
GUNB_KEYWORDS = (
    "budowa hali", "rozbudowa hali", "hala produkcyjna", "hala magazynowa",
    "budowa zakładu", "rozbudowa zakładu", "zakład produkcyjny",
    "budowa budynku produkcyjnego", "budynek przemysłowy",
    "budowa obiektu produkcyjnego", "obiekt przemysłowy",
    "budowa magazynu", "zespół produkcyjny", "wytwórnia",
    "budowa centrum logistycznego", "park logistyczny"
)

@dataclass
class ScrapedItem:
    source: str
    title: str
    company_name: Optional[str]
    location: Optional[str]
    url: str
    text: str

def parse_polish_date(date_value: str) -> Optional[datetime]:
    if not date_value:
        return None
    raw = str(date_value).strip()
    if not raw:
        return None

    # Normalizacja znaków oddzielających
    normalized = raw.replace("/", "-").replace(".", "-")
    date_part = normalized.split(" ")[0]

    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%m-%y", "%Y%m%d"):
        try:
            return datetime.strptime(date_part, fmt)
        except ValueError:
            continue
    return None

def fetch_gunb_data():
    results = []
    seen_hashes = set()

    # Inicjalizacja liczników statystyk
    stats = {
        "date_filtered_rows": 0,
        "limited_rows": 0,
        "duplicates": 0
    }

    cutoff_date = datetime.now() - timedelta(days=DATE_FILTER_DAYS)

    logging.info("Łączenie z API Otwarte Dane Rządu RP (dane.gov.pl)...")
    try:
        resp = requests.get(DATASET_API_URL, timeout=20)
        resp.raise_for_status()
        resources = resp.json().get("data", [])
    except Exception as e:
        logging.error(f"Błąd pobierania metadanych API: {e}")
        return []

    for res in resources:
        title = res["attributes"]["title"].lower()

        # Filtrujemy tylko wnioski z interesujących nas województw
        if "wnioski i decyzje" in title and any(woj in title for woj in WOJEWODZTWA):
            file_url = res["attributes"]["file_url"]
            logging.info(f"Pobieranie paczki ZIP: {title} ...")

            try:
                zip_resp = requests.get(file_url, timeout=120)
                zip_resp.raise_for_status()

                with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
                    for filename in z.namelist():
                        if filename.endswith(".csv"):
                            with z.open(filename) as f:
                                raw_bytes = f.read()

                                # 1. Pancerne dekodowanie (jeśli nie UTF-8 to stary urzędowy Windows-1250)
                                try:
                                    content = raw_bytes.decode('utf-8-sig')
                                except UnicodeDecodeError:
                                    content = raw_bytes.decode('windows-1250', errors='replace')

                                # Używamy odpowiedniego separatora: #
                                reader = csv.DictReader(io.StringIO(content), delimiter='#')
                                fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]

                                # Normalizujemy nagłówki w readerze
                                reader.fieldnames = fieldnames

                                added_from_file = 0

                                for row in reader:
                                    # Pobieramy obie kolumny i łączymy je w jeden tekst
                                    kat_zamierzenia = str(row.get("nazwa_zamierzenia_bud", "")).strip()
                                    opis_zamierzenia = str(row.get("nazwa_zam_budowlanego", "")).strip()
                                    zamierzenie = f"{kat_zamierzenia} {opis_zamierzenia}".strip()

                                    inwestor = str(row.get("nazwa_inwestor", "")).strip()
                                    miejscowosc = str(row.get("miasto", "")).strip()
                                    data_wplywu_raw = str(row.get("data_wplywu_wniosku", ""))

                                    # Pomijamy puste rekordy
                                    if not zamierzenie:
                                        continue

                                    # 1. Sprawdzamy datę
                                    parsed_date = parse_polish_date(data_wplywu_raw)
                                    if not parsed_date or parsed_date < cutoff_date:
                                        stats["date_filtered_rows"] += 1
                                        continue

                                    # 2. Szukamy słów kluczowych (case-insensitive)
                                    zamierzenie_lower = zamierzenie.lower()
                                    if not any(kw in zamierzenie_lower for kw in GUNB_KEYWORDS):
                                        continue

                                    # 3. Deduping - unikamy tych samych wniosków
                                    item_hash = hash((zamierzenie, inwestor, miejscowosc))
                                    if item_hash in seen_hashes:
                                        stats["duplicates"] += 1
                                        continue

                                    seen_hashes.add(item_hash)

                                    # Przycinamy opisy jeśli API dodaje śmieci (tzw. summary text)
                                    text_summary = f"{zamierzenie} (Zgłoszone: {data_wplywu_raw}, Inwestor: {inwestor})"

                                    item = ScrapedItem(
                                        source="GUNB",
                                        title=zamierzenie[:150] + ("..." if len(zamierzenie) > 150 else ""),
                                        company_name=inwestor if inwestor else None,
                                        location=miejscowosc if miejscowosc else None,
                                        url=file_url,
                                        text=text_summary
                                    )

                                    results.append(item)
                                    added_from_file += 1

                                    if added_from_file >= MAX_ROWS_PER_FILE:
                                        stats["limited_rows"] += 1
                                        break

            except Exception as e:
                logging.error(f"Błąd przetwarzania pliku {title}: {e}")

    logging.info(f"Pobrano {len(results)} leadów z GUNB.")
    logging.info(f"Odrzucono po dacie: {stats['date_filtered_rows']}")
    logging.info(f"Odrzucone duplikaty: {stats['duplicates']}")

    return results

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_gunb_data()
    with open(OUTPUT_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(item) for item in data], f, ensure_ascii=False, indent=2)