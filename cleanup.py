import pandas as pd
import os

FILES = ["GUNB_Enriched_Scored_Leads.xlsx", "OLX_Enriched_Scored_Leads.xlsx"]
BAD_DOMAINS = ["uj.edu.pl", "coi.gov.pl", "gov.pl", "szczecin.pl", "w3.org", "sentry.io"]

def clean_file(file_name):
    if not os.path.exists(file_name):
        print(f"Brak pliku {file_name}")
        return

    print(f"\n--- Czyszczenie pliku: {file_name} ---")
    df = pd.read_excel(file_name)

    # 1. Dynamiczne wykrywanie kolumny mailowej
    email_col = 'Final_Email' if 'Final_Email' in df.columns else 'Email'

    # 2. Usuwanie błędnych maili
    def is_bad_email(email):
        email = str(email).lower()
        if any(d in email for d in BAD_DOMAINS):
            return True
        if email in ['brak', 'nan', 'none', 'null', 'brak danych']:
            return True
        return False

    if email_col in df.columns:
        df.loc[df[email_col].apply(is_bad_email), email_col] = 'BRAK'

    # 3. Usuwanie rekordów "ODRZUCONYCH" (tylko jeśli kolumna istnieje)
    if 'Status' in df.columns:
        df = df[df['Status'] != 'ODRZUCONY'].copy()
        print("Usunięto odrzucone rekordy (kolumna Status znaleziona).")
    else:
        print("Kolumna 'Status' nie istnieje – pomijam filtrowanie odrzuconych.")

    # 4. Zapis
    output_name = f"Cleaned_{file_name}"
    df.to_excel(output_name, index=False)
    print(f"✅ Gotowe! Zapisano jako: {output_name}")
    print(f"Ilość rekordów: {len(df)}")

if __name__ == "__main__":
    for f in FILES:
        clean_file(f)