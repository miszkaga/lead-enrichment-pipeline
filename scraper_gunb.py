import json
import logging
import zipfile
import io
import csv
from dataclasses import dataclass, asdict
from typing import Optional
import requests

# --- KONFIGURACJA GUNB API ---
DATASET_API_URL = "https://api.dane.gov.pl/1.4/datasets/3467/resources"

WOJEWODZTWA = ["śląskie", "łódzkie", "małopolskie", "pomorskie", "dolnośląskie", "zachodniopomorskie", "kujawsko-pomorskie"]
OUTPUT_JSON_FILE = "raw_gunb_data.json"

# Teraz szukamy tylko konkretnych inwestycji kubaturowych
GUNB_KEYWORDS = (
    "budowa hali", "rozbudowa hali",
    "budowa zakładu", "rozbudowa zakładu",
    "budowa budynku produkcyjnego",
    "budowa obiektu produkcyjnego",
    "budowa magazynu",
    "budowa centrum logistycznego"
)

@dataclass
class ScrapedItem:
    source: str
    title: str
    company_name: Optional[str]
    location: Optional[str]
    url: str
    text: str

def fetch_gunb_data():
    results = []
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
                                    content = raw_bytes.decode('cp1250', errors='replace')

                                # 2. Dynamiczne szukanie separatora
                                first_line = content.split('\n')[0]
                                if '#' in first_line: delimiter = '#'
                                elif '|' in first_line: delimiter = '|'
                                elif ';' in first_line: delimiter = ';'
                                else: delimiter = ','

                                reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
                                fieldnames = reader.fieldnames

                                if not fieldnames:
                                    continue

                                # 3. Złagodzone szukanie kolumn
                                col_nazwa = next((c for c in fieldnames if c and "zamierzeni" in c.lower()), None)
                                col_inwestor = next((c for c in fieldnames if c and "inwestor" in c.lower()), None)
                                col_miasto = next((c for c in fieldnames if c and ("miejscowo" in c.lower() or "gmina" in c.lower() or "miasto" in c.lower())), None)

                                if not col_nazwa or not col_inwestor:
                                    logging.warning(f"Pominięto plik {filename}. \nZNALAZŁEM TAKIE KOLUMNY: {fieldnames}")
                                    continue

                                for row in reader:
                                    nazwa_zamierzenia = str(row.get(col_nazwa, ""))
                                    if not nazwa_zamierzenia: continue

                                    nazwa_lower = nazwa_zamierzenia.lower()
                                    if any(kw in nazwa_lower for kw in GUNB_KEYWORDS):
                                        inwestor = str(row.get(col_inwestor, "Nieznany")).strip()

                                        # Odfiltrowujemy "Kowalskich" (Osoby fizyczne) i anonimowych
                                        if "osoba fizyczna" in inwestor.lower() or "nieznany" in inwestor.lower() or not inwestor:
                                            continue

                                        combined_text = " ".join(str(v) for v in row.values() if v)

                                        results.append(ScrapedItem(
                                            source="GUNB_API",
                                            title=nazwa_zamierzenia[:150],
                                            company_name=inwestor[:100],
                                            location=row.get(col_miasto, "Brak") if col_miasto else "Brak",
                                            url="https://wyszukiwarka.gunb.gov.pl/",
                                            text=combined_text[:1000]
                                        ))
            except Exception as e:
                logging.error(f"Błąd przetwarzania paczki {title}: {e}")

    return results

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    gunb_data = fetch_gunb_data()

    with open(OUTPUT_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(item) for item in gunb_data], f, ensure_ascii=False, indent=2)
    logging.info(f"Zapisano {len(gunb_data)} wniosków inwestycyjnych z GUNB do {OUTPUT_JSON_FILE}")

if __name__ == "__main__":
    main()