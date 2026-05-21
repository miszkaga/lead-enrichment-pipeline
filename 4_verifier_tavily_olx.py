import json
import logging
import os
import time

import pandas as pd
from dotenv import load_dotenv
from google.genai import types

from osint_pipeline import (
    build_search_queries,
    extract_emails_phones,
    fetch_contact_raw_content,
    gemini_generate_json,
    guess_domain_from_company,
    infer_domain_from_results,
    tavily_search,
)

load_dotenv()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TAVILY_API_KEY or not GEMINI_API_KEY:
    raise ValueError("Brak kluczy API w pliku .env")

INPUT_EXCEL = "OLX_Leads_MVP.xlsx"
OUTPUT_EXCEL = "OLX_Enriched_Scored_Leads.xlsx"
CACHE_FILE = "tavily_cache_olx.json"

TAVILY_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "basic")
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))
GEMINI_MIN_INTERVAL_SECONDS = float(os.environ.get("GEMINI_MIN_INTERVAL_SECONDS", "1.0"))


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
        "4. Znajdź oficjalną domenę www. Jeśli Structured Context zawiera domenę, traktuj ją jako priorytet.\n"
        "5. Użyj Structured Context do e-maili/telefonów; nie zgaduj, jeśli brak danych."
    )


def _collect_tavily_snippets(company_name: str) -> list[dict]:
    merged_results: list[dict] = []
    seen_urls: set[str] = set()

    for query in build_search_queries(company_name):
        response = tavily_search(query=query, depth=TAVILY_DEPTH, max_results=TAVILY_MAX_RESULTS)
        if response.get("error"):
            logging.error("[Tavily ERROR] %s | %s", company_name, response["error"])
        for item in response.get("results", []):
            url = item.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged_results.append(item)

    if not merged_results:
        logging.info("[Tavily EMPTY] Brak wyników snippetów dla: %s", company_name)

    return merged_results


def verify_and_score_company(company_name: str) -> tuple[dict, list]:
    logging.info("🔍 [Tavily] Szukam informacji o: %s...", company_name)
    raw_tavily_results = _collect_tavily_snippets(company_name)

    search_context = "\n\n".join(
        [
            f"Tytuł: {res.get('title', '')}\nURL: {res.get('url', '')}\nTreść: {res.get('content', '')}"
            for res in raw_tavily_results
        ]
    )
    if not search_context.strip():
        search_context = "Brak wyników w internecie dla tej firmy."

    inferred_domain = infer_domain_from_results(raw_tavily_results)
    snippet_contacts = extract_emails_phones(search_context)

    should_rescue = not snippet_contacts["emails"] or not inferred_domain
    rescue_source = "none"
    rescue_contacts = {"emails": [], "phones": []}

    if should_rescue:
        domain_for_rescue = inferred_domain or guess_domain_from_company(company_name)
        raw_contact_context = fetch_contact_raw_content(domain_for_rescue, company_name=company_name)
        rescue_contacts = extract_emails_phones(raw_contact_context)
        rescue_source = "tavily_raw" if raw_contact_context else "none"

    merged_emails = list(dict.fromkeys(snippet_contacts["emails"] + rescue_contacts["emails"]))
    merged_phones = list(dict.fromkeys(snippet_contacts["phones"] + rescue_contacts["phones"]))

    structured_context = {
        "inferred_domain": inferred_domain or "Brak danych",
        "emails_from_regex": merged_emails or ["Brak danych"],
        "phones_from_regex": merged_phones or ["Brak danych"],
        "rescue_source": rescue_source,
    }

    json_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "is_sme": types.Schema(type=types.Type.BOOLEAN, description="Czy to MŚP?"),
            "sme_reason": types.Schema(type=types.Type.STRING, description="Uzasadnienie wielkości"),
            "lead_score": types.Schema(type=types.Type.INTEGER, description="Ocena 1-10"),
            "scoring_reason": types.Schema(type=types.Type.STRING, description="Uzasadnienie oceny biznesowej"),
            "board_members": types.Schema(type=types.Type.STRING, description="Zarząd/Właściciel (lub 'Brak danych')"),
            "domain": types.Schema(type=types.Type.STRING, description="Domena (lub 'Brak danych')"),
            "emails_found": types.Schema(type=types.Type.STRING, description="Maile (lub 'Brak danych')"),
        },
        required=["is_sme", "sme_reason", "lead_score", "scoring_reason", "board_members", "domain", "emails_found"],
    )

    payload_text = (
        f"Firma: {company_name}\n\n"
        f"Structured Context (regex + domain):\n{json.dumps(structured_context, ensure_ascii=False)}\n\n"
        f"Wyniki wyszukiwania:\n{search_context}"
    )

    logging.info("🧠 [Gemini] Ocenia profil i wielkość dla: %s...", company_name)
    try:
        ai_data = gemini_generate_json(
            payload_text=payload_text,
            schema=json_schema,
            system_instruction=build_verification_instruction(),
            model="gemini-2.5-flash",
            retries=3,
            backoff=3.0,
            min_interval_seconds=GEMINI_MIN_INTERVAL_SECONDS,
        )

        if isinstance(ai_data, dict):
            if str(ai_data.get("domain", "")).strip().lower() in {"", "brak danych", "brak", "błąd", "blad"}:
                ai_data["domain"] = structured_context["inferred_domain"]
            if str(ai_data.get("emails_found", "")).strip().lower() in {"", "brak danych", "brak", "błąd", "blad"}:
                ai_data["emails_found"] = ", ".join(merged_emails) if merged_emails else "Brak danych"
        return ai_data, raw_tavily_results
    except Exception as exc:  # noqa: BLE001
        logging.error("[Gemini ERROR] %s | %s", company_name, exc)
        return {
            "is_sme": False,
            "sme_reason": "Błąd AI",
            "lead_score": 0,
            "scoring_reason": "Błąd AI",
            "board_members": "Błąd",
            "domain": structured_context["inferred_domain"],
            "emails_found": ", ".join(merged_emails) if merged_emails else "Brak danych",
        }, raw_tavily_results


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(INPUT_EXCEL):
        logging.error("Nie znaleziono pliku %s. Uruchom najpierw analizator OLX.", INPUT_EXCEL)
        return

    df = pd.read_excel(INPUT_EXCEL)
    df_test = df.head(20).copy()

    logging.info("Załadowano %s firm z OLX do weryfikacji i scoringu.", len(df_test))

    excel_results = []
    tavily_cache_data = []

    for _, row in df_test.iterrows():
        company_name = str(row.get("Nazwa Firmy", ""))
        if not company_name or company_name.lower() == "nan":
            continue

        ai_data, raw_tavily = verify_and_score_company(company_name)

        excel_results.append(
            {
                "Nazwa Firmy": company_name,
                "Sygnał (OLX)": row.get("Sygnał Inwestycyjny", ""),
                "MŚP": ai_data.get("is_sme"),
                "Potencjał (1-10)": ai_data.get("lead_score", 0),
                "Uzasadnienie Potencjału": ai_data.get("scoring_reason", ""),
                "Zarząd": ai_data.get("board_members"),
                "Domena WWW": ai_data.get("domain"),
                "Znalezione Maile": ai_data.get("emails_found"),
            }
        )

        tavily_cache_data.append(
            {
                "company_name": company_name,
                "ai_evaluation": ai_data,
                "raw_tavily_snippets": raw_tavily,
            }
        )

        time.sleep(1)

    df_results = pd.DataFrame(excel_results)
    df_verified = df_results[df_results["MŚP"] == True].copy()
    df_verified = df_verified.sort_values(by="Potencjał (1-10)", ascending=False)
    df_verified.to_excel(OUTPUT_EXCEL, index=False)

    with open(CACHE_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(tavily_cache_data, file_handle, ensure_ascii=False, indent=4)

    logging.info("=========================================")
    logging.info("Gotowe! Handlowiec otrzymał posortowany plik: %s", OUTPUT_EXCEL)
    logging.info("Surowe dane zeskładowane bezpiecznie w: %s", CACHE_FILE)


if __name__ == "__main__":
    main()
