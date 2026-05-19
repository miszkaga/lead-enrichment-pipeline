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
INPUT_JSON_FILE = "raw_scraped_data.json"
OUTPUT_EXCEL_FILE = "Green_Investments_Leads_MVP.xlsx"

# Kryteria z dokumentacji klienta
INVESTMENT_MIN_PLN = 3_000_000
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
        "KRYTERIA POZYTYWNE (Złagodzone): "
        "Nie szukaj twardych dowodów na inwestycję rzędu 3 mln PLN. Uznaj firmę za LEAD ('is_lead': true), "
        "jeśli tekst ogłoszenia sugeruje, że to stabilny zakład produkcyjny, który się ROZWIJA. "
        "Szukaj sygnałów takich jak: 'nowoczesny park maszynowy', 'automatyzacja', 'optymalizacja procesów', "
        "'nowa linia produkcyjna', 'innowacje', 'rozwój działu', 'utrzymanie ruchu', 'nowe technologie'. "
        "Nawet chęć zatrudnienia inżyniera procesu lub technologa do usprawnienia produkcji to wystarczający powód, "
        "by uznać to za szansę na inwestycję i wysłać maila zapoznawczego."
    )
def call_gemini_api(client: genai.Client, text: str) -> dict:
    """Wysyła zapytanie do Gemini żądając strukturyzowanego JSON-a"""

    # Definiujemy dokładną strukturę JSON, jakiej oczekujemy od Gemini
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

    # Konfiguracja żądania
    config = types.GenerateContentConfig(
        system_instruction=build_system_instruction(),
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=json_schema
    )

    # Wywołanie modelu gemini-1.5-flash (szybki, tani i świetny do struktur)
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=text[:8000],  # Bezpieczne przycięcie tekstu
        config=config
    )

    return json.loads(response.text)

def process_leads(raw_items: list[dict]) -> list[dict]:
    # Inicjalizacja oficjalnego klienta Gemini (automatycznie bierze klucz z GEMINI_API_KEY)
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("Brak zmiennej środowiskowej GEMINI_API_KEY w pliku .env")

    client = genai.Client()
    valid_leads = []

    for idx, item in enumerate(raw_items):
        logging.info("Analiza przez Gemini (%d/%d): %s", idx + 1, len(raw_items), item.get('title', 'Brak tytułu')[:40])

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
    for file_name in ["raw_olx_data.json", "raw_gunb_data.json"]:
        if os.path.exists(file_name):
            with open(file_name, "r", encoding="utf-8") as f:
                raw_items.extend(json.load(f))

    if not raw_items:
        logging.error("Nie znaleziono żadnych danych z OLX ani GUNB. Uruchom najpierw skrypty scrapujące.")
        return

    logging.info("Załadowano łącznie %d elementów do analizy przez Gemini.", len(raw_items))

    leads = process_leads(raw_items)


    if leads:
        df = pd.DataFrame(leads)

        # 1. Usuwamy ukryte znaki kontrolne (błąd Excela)
        df = df.replace(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]', '', regex=True)

        # 2. Usuwamy wiersze, gdzie AI nie znalazło nazwy firmy (null / Nieznana)
        df = df[df["Nazwa Firmy"].notna()]
        df = df[df["Nazwa Firmy"] != "Nieznana"]
        df = df[df["Nazwa Firmy"] != "null"]

        # 3. AUTOMATYCZNA DEDUPLIKACJA: Zostawiamy tylko jeden rekord dla danej firmy
        # Dzięki temu prezes dostanie tylko 1 maila, nawet jeśli wystawił 10 ogłoszeń
        df = df.drop_duplicates(subset=["Nazwa Firmy"], keep="first")

        df.to_excel(OUTPUT_EXCEL_FILE, index=False)
        logging.info("Sukces! Wyeksportowano %d UNIKALNYCH, czystych leadów do %s", len(df), OUTPUT_EXCEL_FILE)
    else:
        logging.info("Zakończono analizę. Żaden element nie spełnił kryteriów.")
if __name__ == "__main__":
    main()