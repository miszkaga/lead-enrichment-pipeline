import os
import time
import json
import logging
import pandas as pd
from dotenv import load_dotenv
from tavily import TavilyClient
from google import genai
from google.genai import types

load_dotenv()
tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY"))
gemini_client = genai.Client()

# Wskaż plik, w którym wystąpiły błędy (upewnij się, że nazwa się zgadza z Twoim plikiem)
FILE_TO_FIX = "GUNB_Enriched_Scored_Leads.xlsx"

def build_verification_instruction() -> str:
    return (
        "Jesteś analitykiem wywiadu gospodarczego (OSINT). Analizujesz firmy pod kątem programu 'Zielony Biznes i Innowacje'.\n\n"
        "1. STATUS: Przypisz 'POZYTYWNY' (MŚP), 'DO_WERYFIKACJI' (Brak danych) lub 'ODRZUCONY' (Korporacja/Deweloper/Urząd).\n"
        "2. LEAD SCORING (1-10): Oceń potencjał pod oszczędności energii (np. OZE, CNC, ciężka produkcja = 8-10, lekka = 5-7, usługi = 1-4).\n"
        "3. Znajdź Prezesa/Właściciela z wyników KRS.\n"
        "4. Znajdź domenę www.\n"
        "5. Wyłap adresy e-mail."
    )

def retry_company(company_name: str) -> dict:
    logging.info(f"🔄 Ponawiam pobieranie dla: {company_name}...")
    try:
        search_query = f'"{company_name}" NIP KRS zarząd kontakt email produkcja branża'
        tavily_response = tavily_client.search(query=search_query, search_depth="basic", max_results=5)
        search_context = "\n\n".join([f"Tytuł: {res.get('title', '')}\nURL: {res.get('url', '')}\nTreść: {res.get('content', '')}" for res in tavily_response.get("results", [])])
        if not search_context.strip(): search_context = "Brak danych."
    except Exception as e:
        logging.error(f"Błąd Tavily: {e}")
        search_context = "Błąd."

    json_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "status": types.Schema(type=types.Type.STRING),
            "scoring_reason": types.Schema(type=types.Type.STRING),
            "lead_score": types.Schema(type=types.Type.INTEGER),
            "board_members": types.Schema(type=types.Type.STRING),
            "domain": types.Schema(type=types.Type.STRING),
            "emails_found": types.Schema(type=types.Type.STRING)
        },
        required=["status", "scoring_reason", "lead_score", "board_members", "domain", "emails_found"]
    )

    config = types.GenerateContentConfig(
        system_instruction=build_verification_instruction(),
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=json_schema
    )

    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Firma: {company_name}\n\nWyniki:\n{search_context}",
            config=config
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"Nadal błąd dla {company_name}: {e}")
        return None

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(FILE_TO_FIX):
        logging.error(f"Brak pliku {FILE_TO_FIX}")
        return

    df = pd.read_excel(FILE_TO_FIX)

    # Obliczamy indeksy dla Pandas: wiersz 154 w Excelu to index 152, wiersz 162 to index 160.
    start_idx = 152
    end_idx = 160

    # Upewniamy się, że nie wyjdziemy poza zakres pliku
    if end_idx >= len(df):
        end_idx = len(df) - 1

    # Pobieramy określone wiersze do poprawy (152 do 160 włącznie to .iloc[152:161])
    failed_rows = df.iloc[start_idx : end_idx + 1]

    logging.info(f"Znaleziono {len(failed_rows)} firm w wierszach od 154 do 162. Rozpoczynam naprawę (po 1 wątku na spokojnie).")

    for idx, row in failed_rows.iterrows():
        name = str(row.get("Nazwa Firmy", ""))
        if not name or name.lower() == "nan":
            continue

        ai_data = retry_company(name)
        if ai_data:
            # Nadpisujemy dane w DataFrame w tych samych wierszach
            df.at[idx, 'Status'] = ai_data.get('status')
            df.at[idx, 'Potencjał (1-10)'] = ai_data.get('lead_score', 0)
            df.at[idx, 'Uzasadnienie Potencjału'] = ai_data.get('scoring_reason', '')
            df.at[idx, 'Zarząd / Właściciel'] = ai_data.get('board_members', '')
            df.at[idx, 'Domena WWW'] = ai_data.get('domain', '')
            df.at[idx, 'Znalezione Maile'] = ai_data.get('emails_found', '')
            logging.info(f"✅ Naprawiono Excel wiersz {idx + 2}: {name} (Ocena: {ai_data.get('lead_score', 0)})")

        # Czekamy 3 sekundy po każdym zapytaniu, żeby API nie zgłosiło błędu
        time.sleep(3)

    df.to_excel(FILE_TO_FIX, index=False)
    logging.info(f"=========================================")
    logging.info(f"Zakończono! Wiersze w pliku {FILE_TO_FIX} zostały naprawione i nadpisane pomyślnie.")

if __name__ == "__main__":
    main()