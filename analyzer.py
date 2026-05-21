import json
import logging
import os
import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Ładowanie zmiennych środowiskowych z .env
load_dotenv()

# --- KONFIGURACJA ANALIZATORA ---
OUTPUT_EXCEL_FILE = "Green_Investments_Leads_MVP.xlsx"

# Kryteria z dokumentacji klienta
EXCLUDED_CITIES = ("Warszawa", "Wrocław", "Poznań", "Warszawy", "Wrocławia", "Poznania")
EXCLUDED_INDUSTRIES = ("rolnictwo", "produkcja rolna")

def build_system_instruction() -> str:
    ex_cities = ", ".join(EXCLUDED_CITIES)
    ex_ind = ", ".join(EXCLUDED_INDUSTRIES)
    return (
        "Jesteś analitykiem biznesowym. Szukasz potencjalnych leadów dla programu dotacyjnego 'Zielony Biznes i Innowacje'. "
        f"BEZWZGLĘDNE WYKLUCZENIA (odrzuć od razu, 'is_lead': false): "
        f"1. Firma znajduje się w jednym z miast: {ex_cities}. "
        f"2. Firma działa w branży: {ex_ind}. "
        f"3. Firma to potężna globalna korporacja (szukamy MŚP) lub agencja pracy tymczasowej.\n\n"
        "KRYTERIA POZYTYWNE:\n"
        "1. JEŚLI ŹRÓDŁO TO OGŁOSZENIE O PRACĘ (OLX): Uznaj firmę za LEAD ('is_lead': true), "
        "jeśli tekst sugeruje, że to stabilny zakład produkcyjny, który się ROZWIJA. "
        "Szukaj sygnałów: 'nowoczesny park maszynowy', 'automatyzacja', 'nowa linia produkcyjna'. "
        "Nawet chęć zatrudnienia inżyniera procesu lub technologa to wystarczający powód na lead.\n"
        "2. JEŚLI ŹRÓDŁO TO WNIOSEK BUDOWLANY (GUNB): Uznaj firmę za LEAD ('is_lead': true), "
        "jeśli zamierzenie dotyczy budowy, rozbudowy hali produkcyjnej, magazynu, zakładu lub instalacji "
        "fotowoltaicznej/pomp ciepła dla firmy. To twardy dowód na potężną inwestycję."
    )

def call_gemini_api(client: genai.Client, text: str) -> dict:
    """Wysyła zapytanie do Gemini żądając strukturyzowanego JSON-a"""

    json_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "is_lead": types.Schema(type=types.Type.BOOLEAN, description="Czy firma spełnia wszystkie kryteria programu?"),
            "company_name": types.Schema(type=types.Type.STRING, description="Nazwa firmy lub null"),
            "location": types.Schema(type=types.Type.STRING, description="Miasto/Lokalizacja firmy lub null"),
            "investment_signal": types.Schema(type=types.Type.STRING, description="Opis wykrytego sygnału inwestycyjnego"),
            "reason": types.Schema(type=types.Type.STRING, description="Uzasadnienie decyzji (max 2 zdania)")
        },
        required=["is_lead", "company_name", "location", "investment_signal", "reason"]
    )

    config = types.GenerateContentConfig(
        system_instruction=build_system_instruction(),
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=json_schema
    )

    # Używamy wersji flash - jest najszybsza i najtańsza do masowej analizy
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=text[:8000],
        config=config
    )

    return json.loads(response.text)

def process_leads(raw_items: list[dict]) -> list[dict]:
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("Brak zmiennej środowiskowej GEMINI_API_KEY w pliku .env")

    client = genai.Client()
    valid_leads = []

    for idx, item in enumerate(raw_items):
        logging.info("Analiza przez Gemini (%d/%d): %s...", idx + 1, len(raw_items), item.get('title', 'Brak tytułu')[:40])

        # Jeśli to GUNB, pobieramy odpowiednie nazwy z JSONA
        if "GUNB" in item.get('source', ''):
            payload_text = f"Źródło: {item['source']}\nZamierzenie budowlane: {item['title']}\nInwestor: {item.get('company_name', 'Nieznany')}\nSzczegóły: {item['text']}"
        else:
            payload_text = f"Źródło: {item['source']}\nTytuł: {item['title']}\nTreść: {item['text']}"

        try:
            ai_result = call_gemini_api(client, payload_text)

            if ai_result.get("is_lead") is True:
                valid_leads.append({
                    "Nazwa Firmy": ai_result.get("company_name") or item.get("company_name") or "Nieznana",
                    "Lokalizacja": ai_result.get("location") or item.get("location") or "Nieznana",
                    "Źródło": item["source"],
                    "Sygnał Inwestycyjny": ai_result.get("investment_signal"),
                    "Uzasadnienie (Gemini)": ai_result.get("reason"),
                    "Link": item["url"]
                })
                logging.info("[+] ZNALEZIONO LEAD: %s", valid_leads[-1]["Nazwa Firmy"])
            else:
                logging.info("[-] Odrzucono. Powód: %s", ai_result.get("reason"))
        except Exception as e:
            logging.error("Błąd API Gemini dla elementu: %s", e)

    return valid_leads

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # 1. Zbieramy dane z obydwu źródeł
    raw_items = []

    # TUTAJ ZMIANA: Dodano nową nazwę pliku z bota z Playwright
    for file_name in ["raw_olx_data.json", "gunb_live_data.json"]:
        if os.path.exists(file_name):
            with open(file_name, "r", encoding="utf-8") as f:
                dane = json.load(f)
                raw_items.extend(dane)
                logging.info("Załadowano %d elementów z pliku %s", len(dane), file_name)

    if not raw_items:
        logging.error("Nie znaleziono żadnych danych z OLX ani GUNB. Uruchom najpierw skrypty scrapujące.")
        return

    logging.info("Załadowano łącznie %d elementów do analizy przez Gemini.", len(raw_items))

    leads = process_leads(raw_items)

    if leads:
        df = pd.DataFrame(leads)

        # 1. Usuwamy ukryte znaki kontrolne (błąd Excela)
        df = df.replace(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]', '', regex=True)

        # 2. Usuwamy wiersze, gdzie AI nie znalazło nazwy firmy
        df = df[df["Nazwa Firmy"].notna()]
        df = df[df["Nazwa Firmy"] != "Nieznana"]
        df = df[df["Nazwa Firmy"] != "null"]

        # 3. AUTOMATYCZNA DEDUPLIKACJA: Zostawiamy tylko jeden rekord dla danej firmy
        df = df.drop_duplicates(subset=["Nazwa Firmy"], keep="first")

        df.to_excel(OUTPUT_EXCEL_FILE, index=False)
        logging.info("Sukces! Wyeksportowano %d UNIKALNYCH, czystych leadów do %s", len(df), OUTPUT_EXCEL_FILE)
    else:
        logging.info("Zakończono analizę. Żaden element nie spełnił kryteriów.")

if __name__ == "__main__":
    main()