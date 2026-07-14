# Electoral Roll → Excel

Upload an electoral-roll PDF, extract per-voter records with **Mistral Document
OCR**, and download a clean Excel file.

## Extracted columns

`Constituency_No`, `Constituency_Name`, `Part_No`, `Serial_No` (the number in the
box), `EPIC_No`, `Name`, `Relation_Type` (Father/Husband/Mother/Other),
`Relation_Name`, `House_Number`, `Age`, `Gender`, `Page`.

## Setup

```bash
# from the project folder (a .venv already exists)
./.venv/bin/pip install -r requirements.txt

cp .env.example .env        # then paste your Mistral API key into .env
```

Get a key at https://console.mistral.ai.

## Run

```bash
./.venv/bin/streamlit run app.py
```

Then in the browser: upload a PDF → **Convert to Excel** → download.

## Options (sidebar)

- **Auto-remove cover pages** — toggle that drops the first 2 and last 2 pages
  (cover / maps / summary / legend). Counts are adjustable.
- **Structuring method**
  - **LLM** (default) — Mistral turns the OCR text into clean rows; most robust
    against multi-column reading-order noise. Uses a few extra API calls.
  - **Regex** — free, fully local parsing. Good when OCR text is tidy.

## Swapping the OCR provider

The provider lives behind an adapter in [`ocr_providers.py`](ocr_providers.py).
To use a different one, add an `OCRProvider` subclass, register it in
`get_provider`, and set `OCR_PROVIDER=<name>` in `.env`. Nothing else changes.

## Offline test

```bash
./.venv/bin/python test_regex.py   # verifies the parser on sample roll text
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI (upload, toggle, download) |
| `pdf_utils.py` | Trim cover/summary pages |
| `ocr_providers.py` | Swappable OCR adapter (Mistral by default) |
| `extractor.py` | OCR text → structured voter rows (LLM + regex) |
