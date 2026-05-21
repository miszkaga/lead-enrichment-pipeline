import os
import json
import time
import logging
import pandas as pd
import concurrent.futures
from dotenv import load_dotenv
from tavily import TavilyClient
from google import genai
from google.genai import types

# 1. Ładowanie kluczy
load_dotenv()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TAVILY_API_KEY or not GEMINI_API_KEY:
    raise ValueError("Brak kluczy API w pliku .env (TAVILY_API_KEY lub GEMINI_API_KEY)")

tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
gemini_client = genai.Client()

INPUT_EXCEL = "Green_Investments_Leads_MVP.xlsx"
OUTPUT_EXCEL = "GUNB_Enriched_Scored_Leads.xlsx"
CACHE_FILE = "tavily_cache_gunb.json"

# Konfiguracja wielowątkowości
MAX_WORKERS = 15  # Ilość równoległych zapytań. Zwiększ do 10, jeśli masz podpiętą kartę kredytową w Google Cloud

def build_verification_instruction() -> str:
    return (
        "Jesteś analitykiem wywiadu gospodarczego (OSINT). Analizujesz firmy pod kątem programu 'Zielony Biznes i Innowacje'.\n\n"
        "Twoim zadaniem jest ocena potencjału firmy:\n"
        "1. STATUS: Przypisz:\n"
        "   - 'POZYTYWNY' - Jeśli to MŚP (produkcja, usługi, < 250 os.).\n"
        "   - 'DO_WERYFIKACJI' - Jeśli brakuje danych (nie odrzucaj na siłę!).\n"
        "   - 'ODRZUCONY' - TYLKO jeśli masz twardy dowód: korporacja, deweloper, agencja pracy, urząd.\n\n"
        "2. LEAD SCORING (1-10):\n"
        "   - 8-10: Ciężka produkcja, odlewnie, CNC (wysoka energochłonność).\n"
        "   - 5-7: Lekka produkcja, stolarnie, przetwórstwo spożywcze.\n"
        "   - 1-4: Usługi, logistyka, handel.\n\n"
        "Ważne: Jeśli dane są niepełne, ustaw STATUS 'DO_WERYFIKACJI' i niską ocenę (np. 3), ale NIE odrzucaj."
    )

def verify_and_score_company(company_name: str, retries: int = 3) -> tuple[dict, list]:
    logging.info(f"🔍 [Tavily] Szukam: {company_name}...")

    raw_results = []
    try:
        search_query = f'"{company_name}" NIP KRS zarząd kontakt email produkcja branża'
        tavily_response = tavily_client.search(query=search_query, search_depth="basic", max_results=5)
        raw_results = tavily_response.get("results", [])

        search_context = "\n\n".join([f"Tytuł: {res.get('title', '')}\nURL: {res.get('url', '')}\nTreść: {res.get('content', '')}" for res in raw_results])
        if not search_context.strip(): search_context = "Brak danych."
    except Exception as e:
        logging.error(f"Błąd Tavily dla {company_name}: {e}")
        search_context = "Błąd."

    json_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "status": types.Schema(type=types.Type.STRING, description="POZYTYWNY, DO_WERYFIKACJI, ODRZUCONY"),
            "scoring_reason": types.Schema(type=types.Type.STRING, description="Uzasadnienie"),
            "lead_score": types.Schema(type=types.Type.INTEGER, description="Ocena 1-10"),
            "board_members": types.Schema(type=types.Type.STRING, description="Prezes/Właściciel"),
            "domain": types.Schema(type=types.Type.STRING, description="Domena www"),
            "emails_found": types.Schema(type=types.Type.STRING, description="Maile")
        },
        required=["status", "scoring_reason", "lead_score", "board_members", "domain", "emails_found"]
    )

    config = types.GenerateContentConfig(
        system_instruction=build_verification_instruction(),
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=json_schema
    )

    # Pętla RETRY (Ponawiania) jeśli wyczerpiemy limit API
    for attempt in range(retries):
        try:
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=f"Firma: {company_name}\n\nWyniki:\n{search_context}",
                config=config
            )
            return json.loads(response.text), raw_results
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                logging.warning(f"⚠️ Limit API Gemini! Czekam 10s... ({company_name}) [Próba {attempt+1}/{retries}]")
                time.sleep(10)
            else:
                logging.error(f"Błąd Gemini dla {company_name}: {e}")
                break

    # Fallback jeśli AI się podda
    return {
        "status": "DO_WERYFIKACJI", "scoring_reason": "Błąd połączenia z AI",
        "lead_score": 0, "board_members": "Brak", "domain": "Brak", "emails_found": "Brak"
    }, raw_results

def process_single_row(row):
    """Funkcja pomocnicza do przetwarzania jednego wiersza (dla wielowątkowości)"""
    name = str(row.get("Nazwa Firmy", ""))
    if not name or name.lower() == "nan":
        return None

    ai_data, raw_data = verify_and_score_company(name)

    excel_row = {
        "Nazwa Firmy": name,
        "Sygnał Inwestycyjny (GUNB)": row.get("Sygnał Inwestycyjny", ""),
        "Status": ai_data.get("status"),
        "Potencjał (1-10)": ai_data.get("lead_score", 0),
        "Uzasadnienie Potencjału": ai_data.get("scoring_reason", ""),
        "Zarząd / Właściciel": ai_data.get("board_members"),
        "Domena WWW": ai_data.get("domain"),
        "Znalezione Maile": ai_data.get("emails_found")
    }

    cache_row = {"name": name, "ai": ai_data, "raw": raw_data}

    return excel_row, cache_row

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(INPUT_EXCEL):
        logging.error(f"Brak pliku {INPUT_EXCEL}")
        return

    df = pd.read_excel(INPUT_EXCEL)
    # df_test = df.head(20).copy() # Zakomentowane - robimy pełne 182 pozycje!
    df_test = df.copy()

    excel_results = []
    tavily_cache_data = []

    logging.info(f"🚀 Startujemy! Analiza {len(df_test)} firm na {MAX_WORKERS} wątkach jednocześnie.")

    # Wielowątkowe przetwarzanie
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Zlecamy wszystkie zadania
        futures = {executor.submit(process_single_row, row): row for _, row in df_test.iterrows()}

        # Odbieramy wyniki w miarę ich kończenia
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                if res:
                    excel_row, cache_row = res
                    excel_results.append(excel_row)
                    tavily_cache_data.append(cache_row)
            except Exception as e:
                logging.error(f"Wyjątek krytyczny w wątku: {e}")

    # Tworzenie Excela
    df_out = pd.DataFrame(excel_results)

    # 1. Zabezpieczenie przed pustym plikiem
    if not df_out.empty:
        # 2. POPRAWIONE SORTOWANIE (Kategoria Statusu, a potem Oceny)
        df_out['Status'] = pd.Categorical(
            df_out['Status'],
            categories=['POZYTYWNY', 'DO_WERYFIKACJI', 'ODRZUCONY'],
            ordered=True
        )
        df_out = df_out.sort_values(by=['Status', 'Potencjał (1-10)'], ascending=[True, False])
        df_out.to_excel(OUTPUT_EXCEL, index=False)
        logging.info(f"✅ Gotowe! Zapisano {len(df_out)} przetworzonych firm do {OUTPUT_EXCEL}")
    else:
        logging.warning("Brak wyników do zapisania.")

    # Zapis Data Lake
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(tavily_cache_data, f, ensure_ascii=False, indent=4)
        logging.info(f"✅ Zapisano zrzut pamięci do {CACHE_FILE}")

if __name__ == "__main__":
    main()