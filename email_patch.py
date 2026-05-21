import os
import time
import logging
import pandas as pd
from dotenv import load_dotenv
from google import genai

# 1. Załadowanie zmiennych z pliku .env
load_dotenv()

# 2. Pobranie klucza i walidacja
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("Nie znaleziono GEMINI_API_KEY w pliku .env! Upewnij się, że plik istnieje i ma odpowiednią zmienną.")

# 3. Inicjalizacja klienta z kluczem
client = genai.Client(api_key=api_key)

# Konfiguracja
FILES = ["GUNB_Enriched_Scored_Leads.xlsx", "OLX_Enriched_Scored_Leads.xlsx"]

def get_email_category(email, company_name):
    """Ocena przez Gemini: PERSONAL, GENERIC, NO_EMAIL"""
    if pd.isna(email) or str(email).lower() in ["brak", "brak danych", "nan", ""]:
        return "NO_EMAIL"

    prompt = f"""
    Oceń adres email dla firmy '{company_name}'.
    Adres: {email}
    Zwróć TYLKO jedno słowo:
    - PERSONAL: jeśli to mail imienny, bezpośredni do osoby.
    - GENERIC: jeśli to mail typu biuro@, kontakt@, info@, sprzedaz@, inquiries@.
    - NO_EMAIL: jeśli adres jest błędny.
    """
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text.strip().upper()
    except Exception as e:
        print(f"Błąd Gemini dla {email}: {e}")
        return "ERROR"

def process_file(file_name):
    print(f"--- Przetwarzam: {file_name} ---")
    df = pd.read_excel(file_name)

    # 1. POSPRZĄTAJ KOLUMNY EMAIL
    potential_cols = [c for c in df.columns if "Maile" in c or "Email" in c]

    if potential_cols:
        # Bierzemy pierwszą znalezioną kolumnę z mailami, resztę usuwamy
        main_email_col = potential_cols[0]
        # Tworzymy nową kolumnę 'Email' jeśli nie istnieje, lub używamy istniejącej
        df['Email'] = df[main_email_col]
        # Usuwamy stare kolumny mailowe (tylko te, które nie nazywają się 'Email')
        cols_to_drop = [c for c in potential_cols if c != 'Email']
        df = df.drop(columns=cols_to_drop)
    else:
        df['Email'] = "Brak danych"

    # 2. EVALUACJA
    # Jeśli kolumna już istnieje, nie nadpisujmy PENDING, żeby nie robić zapytań od nowa
    if 'Email_Status' not in df.columns:
        df['Email_Status'] = "PENDING"

    for idx, row in df.iterrows():
        # Pomijamy ODRZUCONE
        if row.get("Status", "") == "ODRZUCONY":
            df.at[idx, 'Email_Status'] = "SKIPPED"
            continue

        # Jeśli już mamy wynik, nie pytamy Gemini ponownie
        if df.at[idx, 'Email_Status'] not in ["PENDING", "ERROR"]:
            continue

        category = get_email_category(row['Email'], row.get("Nazwa Firmy", "Firma"))
        df.at[idx, 'Email_Status'] = category
        print(f"[{idx}] {row.get('Nazwa Firmy')}: {category}")

        # Pauza dla bezpieczeństwa API
        time.sleep(0.5)

    # 3. ZAPIS
    df.to_excel(file_name, index=False)
    print(f"Zapisano czysty plik: {file_name}")

if __name__ == "__main__":
    for f in FILES:
        if os.path.exists(f):
            process_file(f)
        else:
            print(f"Plik {f} nie istnieje.")