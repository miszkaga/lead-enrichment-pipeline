import concurrent.futures
import json
import logging
import os

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
    raise ValueError("Brak kluczy API w pliku .env (TAVILY_API_KEY lub GEMINI_API_KEY)")

INPUT_EXCEL = "Green_Investments_Leads_MVP.xlsx"
OUTPUT_EXCEL = "GUNB_Enriched_Scored_Leads.xlsx"
CACHE_FILE = "tavily_cache_gunb.json"

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "5"))
TAVILY_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "basic")
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))
GEMINI_MIN_INTERVAL_SECONDS = float(os.environ.get("GEMINI_MIN_INTERVAL_SECONDS", "1.2"))


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
        "Ważne: Jeśli dane są niepełne, ustaw STATUS 'DO_WERYFIKACJI' i niską ocenę (np. 3), ale NIE odrzucaj.\n"
        "Jeśli Structured Context zawiera domenę/maile/telefony, traktuj je jako priorytet."
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
    logging.info("🔍 [Tavily] Szukam: %s...", company_name)

    raw_results = _collect_tavily_snippets(company_name)
    search_context = "\n\n".join(
        [
            f"Tytuł: {res.get('title', '')}\nURL: {res.get('url', '')}\nTreść: {res.get('content', '')}"
            for res in raw_results
        ]
    )
    if not search_context.strip():
        search_context = "Brak danych."

    inferred_domain = infer_domain_from_results(raw_results)
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
            "status": types.Schema(type=types.Type.STRING, description="POZYTYWNY, DO_WERYFIKACJI, ODRZUCONY"),
            "scoring_reason": types.Schema(type=types.Type.STRING, description="Uzasadnienie"),
            "lead_score": types.Schema(type=types.Type.INTEGER, description="Ocena 1-10"),
            "board_members": types.Schema(type=types.Type.STRING, description="Prezes/Właściciel"),
            "domain": types.Schema(type=types.Type.STRING, description="Domena www"),
            "emails_found": types.Schema(type=types.Type.STRING, description="Maile"),
        },
        required=["status", "scoring_reason", "lead_score", "board_members", "domain", "emails_found"],
    )

    payload_text = (
        f"Firma: {company_name}\n\n"
        f"Structured Context (regex + domain):\n{json.dumps(structured_context, ensure_ascii=False)}\n\n"
        f"Wyniki:\n{search_context}"
    )

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
        return ai_data, raw_results
    except Exception as exc:  # noqa: BLE001
        logging.error("[Gemini ERROR] %s | %s", company_name, exc)
        return {
            "status": "DO_WERYFIKACJI",
            "scoring_reason": "Błąd połączenia z AI",
            "lead_score": 0,
            "board_members": "Brak",
            "domain": structured_context["inferred_domain"],
            "emails_found": ", ".join(merged_emails) if merged_emails else "Brak danych",
        }, raw_results


def process_single_row(row):
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
        "Znalezione Maile": ai_data.get("emails_found"),
    }

    cache_row = {"name": name, "ai": ai_data, "raw": raw_data}
    return excel_row, cache_row


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(INPUT_EXCEL):
        logging.error("Brak pliku %s", INPUT_EXCEL)
        return

    df = pd.read_excel(INPUT_EXCEL)
    df_test = df.copy()

    excel_results = []
    tavily_cache_data = []

    logging.info("🚀 Startujemy! Analiza %s firm na %s wątkach jednocześnie.", len(df_test), MAX_WORKERS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_single_row, row): row for _, row in df_test.iterrows()}

        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                if res:
                    excel_row, cache_row = res
                    excel_results.append(excel_row)
                    tavily_cache_data.append(cache_row)
            except Exception as exc:  # noqa: BLE001
                logging.error("Wyjątek krytyczny w wątku: %s", exc)

    df_out = pd.DataFrame(excel_results)

    if not df_out.empty:
        df_out["Status"] = pd.Categorical(
            df_out["Status"],
            categories=["POZYTYWNY", "DO_WERYFIKACJI", "ODRZUCONY"],
            ordered=True,
        )
        df_out = df_out.sort_values(by=["Status", "Potencjał (1-10)"], ascending=[True, False])
        df_out.to_excel(OUTPUT_EXCEL, index=False)
        logging.info("✅ Gotowe! Zapisano %s przetworzonych firm do %s", len(df_out), OUTPUT_EXCEL)
    else:
        logging.warning("Brak wyników do zapisania.")

    with open(CACHE_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(tavily_cache_data, file_handle, ensure_ascii=False, indent=4)
        logging.info("✅ Zapisano zrzut pamięci do %s", CACHE_FILE)


if __name__ == "__main__":
    main()
