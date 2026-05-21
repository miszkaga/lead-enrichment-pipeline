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

FILE_TO_FIX = "GUNB_Enriched_Scored_Leads.xlsx"
TAVILY_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "basic")
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))
GEMINI_MIN_INTERVAL_SECONDS = float(os.environ.get("GEMINI_MIN_INTERVAL_SECONDS", "1.0"))


def build_verification_instruction() -> str:
    return (
        "Jesteś analitykiem wywiadu gospodarczego (OSINT). Analizujesz firmy pod kątem programu 'Zielony Biznes i Innowacje'.\n\n"
        "1. STATUS: Przypisz 'POZYTYWNY' (MŚP), 'DO_WERYFIKACJI' (Brak danych) lub 'ODRZUCONY' (Korporacja/Deweloper/Urząd).\n"
        "2. LEAD SCORING (1-10): Oceń potencjał pod oszczędności energii (np. OZE, CNC, ciężka produkcja = 8-10, lekka = 5-7, usługi = 1-4).\n"
        "3. Znajdź Prezesa/Właściciela z wyników KRS.\n"
        "4. Znajdź domenę www.\n"
        "5. Użyj Structured Context do e-maili/telefonów; nie zgaduj jeśli brak danych."
    )


def retry_company(company_name: str) -> dict | None:
    logging.info("🔄 Ponawiam pobieranie dla: %s...", company_name)

    raw_results: list[dict] = []
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
            raw_results.append(item)

    search_context = "\n\n".join(
        [
            f"Tytuł: {res.get('title', '')}\nURL: {res.get('url', '')}\nTreść: {res.get('content', '')}"
            for res in raw_results
        ]
    )
    if not search_context.strip():
        search_context = "Brak danych."
        logging.info("[Tavily EMPTY] Brak wyników snippetów dla: %s", company_name)

    inferred_domain = infer_domain_from_results(raw_results)
    snippet_contacts = extract_emails_phones(search_context)

    should_rescue = not snippet_contacts["emails"] or not inferred_domain
    rescue_contacts = {"emails": [], "phones": []}
    if should_rescue:
        domain_for_rescue = inferred_domain or guess_domain_from_company(company_name)
        raw_contact_context = fetch_contact_raw_content(domain_for_rescue, company_name=company_name)
        rescue_contacts = extract_emails_phones(raw_contact_context)

    merged_emails = list(dict.fromkeys(snippet_contacts["emails"] + rescue_contacts["emails"]))
    merged_phones = list(dict.fromkeys(snippet_contacts["phones"] + rescue_contacts["phones"]))

    structured_context = {
        "inferred_domain": inferred_domain or "Brak danych",
        "emails_from_regex": merged_emails or ["Brak danych"],
        "phones_from_regex": merged_phones or ["Brak danych"],
    }

    json_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "status": types.Schema(type=types.Type.STRING),
            "scoring_reason": types.Schema(type=types.Type.STRING),
            "lead_score": types.Schema(type=types.Type.INTEGER),
            "board_members": types.Schema(type=types.Type.STRING),
            "domain": types.Schema(type=types.Type.STRING),
            "emails_found": types.Schema(type=types.Type.STRING),
        },
        required=["status", "scoring_reason", "lead_score", "board_members", "domain", "emails_found"],
    )

    payload_text = (
        f"Firma: {company_name}\n\n"
        f"Structured Context (regex + domain):\n{json.dumps(structured_context, ensure_ascii=False)}\n\n"
        f"Wyniki:\n{search_context}"
    )

    try:
        data = gemini_generate_json(
            payload_text=payload_text,
            schema=json_schema,
            system_instruction=build_verification_instruction(),
            model="gemini-2.5-flash",
            retries=3,
            backoff=3.0,
            min_interval_seconds=GEMINI_MIN_INTERVAL_SECONDS,
        )
        if isinstance(data, dict):
            if str(data.get("domain", "")).strip().lower() in {"", "brak", "brak danych", "błąd", "blad"}:
                data["domain"] = structured_context["inferred_domain"]
            if str(data.get("emails_found", "")).strip().lower() in {"", "brak", "brak danych", "błąd", "blad"}:
                data["emails_found"] = ", ".join(merged_emails) if merged_emails else "Brak danych"
        return data
    except Exception as exc:  # noqa: BLE001
        logging.error("Nadal błąd dla %s: %s", company_name, exc)
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(FILE_TO_FIX):
        logging.error("Brak pliku %s", FILE_TO_FIX)
        return

    df = pd.read_excel(FILE_TO_FIX)

    start_idx = 152
    end_idx = 160

    if end_idx >= len(df):
        end_idx = len(df) - 1

    failed_rows = df.iloc[start_idx : end_idx + 1]

    logging.info("Znaleziono %s firm w wierszach od 154 do 162. Rozpoczynam naprawę (po 1 wątku na spokojnie).", len(failed_rows))

    for idx, row in failed_rows.iterrows():
        name = str(row.get("Nazwa Firmy", ""))
        if not name or name.lower() == "nan":
            continue

        ai_data = retry_company(name)
        if ai_data:
            df.at[idx, "Status"] = ai_data.get("status")
            df.at[idx, "Potencjał (1-10)"] = ai_data.get("lead_score", 0)
            df.at[idx, "Uzasadnienie Potencjału"] = ai_data.get("scoring_reason", "")
            df.at[idx, "Zarząd / Właściciel"] = ai_data.get("board_members", "")
            df.at[idx, "Domena WWW"] = ai_data.get("domain", "")
            df.at[idx, "Znalezione Maile"] = ai_data.get("emails_found", "")
            logging.info("✅ Naprawiono Excel wiersz %s: %s (Ocena: %s)", idx + 2, name, ai_data.get("lead_score", 0))

        time.sleep(2)

    df.to_excel(FILE_TO_FIX, index=False)
    logging.info("=========================================")
    logging.info("Zakończono! Wiersze w pliku %s zostały naprawione i nadpisane pomyślnie.", FILE_TO_FIX)


if __name__ == "__main__":
    main()
