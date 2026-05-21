import os
import json
import time
import logging
import pandas as pd
from dotenv import load_dotenv
from tavily import TavilyClient
from google import genai
from google.genai import types

# Ładowanie kluczy
load_dotenv()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TAVILY_API_KEY or not GEMINI_API_KEY:
    raise ValueError("Brak kluczy API w pliku .env")

tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
gemini_client = genai.Client()

INPUT_EXCEL = "OLX_Leads_MVP.xlsx"
OUTPUT_EXCEL = "OLX_Enriched_Scored_Leads.xlsx"
CACHE_FILE = "tavily_cache_olx.json"

def build_verification_instruction() -> str:
    return (
        "Jesteś analitykiem wywiadu gospodarczego (OSINT). Twoim zadaniem jest przeanalizowanie "
        "wyników wyszukiwania (z Tavily) dla konkretnej firmy i wyciągnięcie kluczowych danych biznesowych, "
        "oraz ocena jej potencjału pod program dotacyjny 'Zielony Biznes i Innowacje'.\n\n"
        "KRYTERIA PROGRAMU (MŚP):\n"
        "- Firma musi zatrudniać poniżej 250 pracowników. Odrzuć globalne korporacje.\n\n"
        "LEAD SCORING (1-10):\n"
        "Oceń potencjał firmy do wdrożenia innowacji i oszczędności energii (np. OZE, pompy ciepła, automatyzacja):\n"
        "- 8-10 pkt: Ciężka produkcja, odlewnie, obróbka metali (CNC), przetwórstwo tworzyw sztucznych (bardzo energochłonne).\n"
        "- 5-7 pkt: Lekka produkcja, montażownie, stolarnie, przetwórstwo spożywcze, szwalnie.\n"
        "- 1-4 pkt: Małe firmy usługowe, rzemieślnicze, czysta logistyka (przeładunek bez produkcji).\n\n"
        "ZADANIA:\n"
        "1. Oceń, czy to MŚP (true/false) i podaj uzasadnienie.\n"
        "2. Wystaw ocenę (lead_score) od 1 do 10 i podaj 1 zdanie uzasadnienia (scoring_reason).\n"
        "3. Znajdź imię i nazwisko Prezesa/Właściciela z wyników KRS.\n"
        "4. Znajdź oficjalną domenę www.\n"
        "5. Wyłap adresy e-mail z tekstu."
    )

def verify_and_score_company(company_name: str) -> tuple[dict, list]:
    logging.info(f"🔍 [Tavily] Szukam informacji o: {company_name}...")

    raw_tavily_results = []
    try:
        search_query = f'"{company_name}" NIP KRS zarząd kontakt email produkcja'
        tavily_response = tavily_client.search(query=search_query, search_depth="basic", max_results=5)
        raw_tavily_results = tavily_response.get("results", [])

        search_context = "\n\n".join([
            f"Tytuł: {res.get('title', '')}\nURL: {res.get('url', '')}\nTreść: {res.get('content', '')}"
            for res in raw_tavily_results
        ])

        if not search_context.strip():
            search_context = "Brak wyników w internecie dla tej firmy."

    except Exception as e:
        logging.error(f"Błąd Tavily dla {company_name}: {e}")
        search_context = "Błąd pobierania danych z internetu."

    # Nowy, rozszerzony schemat JSON z Lead Scoringiem
    json_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "is_sme": types.Schema(type=types.Type.BOOLEAN, description="Czy to MŚP?"),
            "sme_reason": types.Schema(type=types.Type.STRING, description="Uzasadnienie wielkości"),
            "lead_score": types.Schema(type=types.Type.INTEGER, description="Ocena 1-10"),
            "scoring_reason": types.Schema(type=types.Type.STRING, description="Uzasadnienie oceny biznesowej"),
            "board_members": types.Schema(type=types.Type.STRING, description="Zarząd/Właściciel (lub 'Brak danych')"),
            "domain": types.Schema(type=types.Type.STRING, description="Domena (lub 'Brak danych')"),
            "emails_found": types.Schema(type=types.Type.STRING, description="Maile (lub 'Brak danych')")
        },
        required=["is_sme", "sme_reason", "lead_score", "scoring_reason", "board_members", "domain", "emails_found"]
    )

    config = types.GenerateContentConfig(
        system_instruction=build_verification_instruction(),
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=json_schema
    )

    payload_text = f"Firma: {company_name}\n\nWyniki wyszukiwania:\n{search_context}"

    logging.info(f"🧠 [Gemini] Ocenia profil i wielkość dla: {company_name}...")
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=payload_text,
            config=config
        )
        return json.loads(response.text), raw_tavily_results
    except Exception as e:
        logging.error(f"Błąd Gemini dla {company_name}: {e}")
        return {
            "is_sme": False, "sme_reason": "Błąd AI", "lead_score": 0, "scoring_reason": "Błąd AI",
            "board_members": "Błąd", "domain": "Błąd", "emails_found": "Błąd"
        }, raw_tavily_results

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(INPUT_EXCEL):
        logging.error(f"Nie znaleziono pliku {INPUT_EXCEL}. Uruchom najpierw analizator OLX.")
        return

    df = pd.read_excel(INPUT_EXCEL)

    # Do testów MVP na razie zostawiam 20 pierwszych rekordów
    df_test = df.head(20).copy()

    logging.info(f"Załadowano {len(df_test)} firm z OLX do weryfikacji i scoringu.")

    excel_results = []
    tavily_cache_data = []

    for index, row in df_test.iterrows():
        company_name = str(row.get("Nazwa Firmy", ""))
        if not company_name or company_name.lower() == "nan":
            continue

        # AI wykonuje pracę i zwraca również surowe dane z Tavily
        ai_data, raw_tavily = verify_and_score_company(company_name)

        # 1. Dane czyste dla Handlowca (Excel)
        excel_results.append({
            "Nazwa Firmy": company_name,
            "Sygnał (OLX)": row.get("Sygnał Inwestycyjny", ""),
            "MŚP": ai_data.get("is_sme"),
            "Potencjał (1-10)": ai_data.get("lead_score", 0),
            "Uzasadnienie Potencjału": ai_data.get("scoring_reason", ""),
            "Zarząd": ai_data.get("board_members"),
            "Domena WWW": ai_data.get("domain"),
            "Znalezione Maile": ai_data.get("emails_found")
        })

        # 2. Jezioro Danych (Data Lake) dla Ciebie / Systemu (JSON)
        tavily_cache_data.append({
            "company_name": company_name,
            "ai_evaluation": ai_data,
            "raw_tavily_snippets": raw_tavily
        })

        time.sleep(2)

    # Zapis Excela
    df_results = pd.DataFrame(excel_results)
    df_verified = df_results[df_results["MŚP"] == True].copy()

    # Sortujemy od najlepszych leadów (10) do najsłabszych
    df_verified = df_verified.sort_values(by="Potencjał (1-10)", ascending=False)
    df_verified.to_excel(OUTPUT_EXCEL, index=False)

    # Zapis Jeziora Danych (JSON)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(tavily_cache_data, f, ensure_ascii=False, indent=4)

    logging.info(f"=========================================")
    logging.info(f"Gotowe! Handlowiec otrzymał posortowany plik: {OUTPUT_EXCEL}")
    logging.info(f"Surowe dane zeskładowane bezpiecznie w: {CACHE_FILE}")

if __name__ == "__main__":
    main()