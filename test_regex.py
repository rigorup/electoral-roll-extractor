"""Offline sanity check of the parser on REAL Mistral-OCR output format.

The snippet below is copied verbatim from an actual `mistral-ocr-latest`
response for a 2025 electoral roll page (markdown table, one voter per cell,
EPIC in the following cell). Run: ./.venv/bin/python test_regex.py
"""
from ocr_providers import PageText
from extractor import build_rows, extract_header

SAMPLE = """Assembly Constituency No and Name : 58-KANUBARI
Section No and Name : 1-KANUBARI H.Q.

Part No. : 1

|  **91** Name : MITA DEY Husbands Name: MONOTOSH LAL DEY House Number : E-68 Age : 50 Gender : Female | CRC0108787 | **92** Name : MANJORI DEY Fathers Name: MONOTOSH LAL DEY House Number : E-68 Age : 29 Gender : Female | CRC0219642 | **93** Name : ANJANA DAS Husbands Name: SANAAN DAS House Number : E-69 Age : 46 Gender : Female | CRC0206318  |
| --- | --- | --- | --- | --- | --- |
|  168 Name : PHOTONG WANGSU Fathers Name : B WANGSU House Number : E-119 Age : 33 Gender : Male | CRC0107987 | **99** Name : ARUN BASPHOR Mothers Name: SHANTI BASFOR BASFOR House Number : E-72 Age : 21 Gender : Male | CRC0285122 | **111** Name : Phepan Bohham Fathers Name: Horiai Bohham House Number : E-77 Age : 22 Gender : Female | CRC0281394  |
"""

pages = [PageText(index=2, markdown=SAMPLE)]
header = extract_header(pages)
print("HEADER:", header)
assert header == {"Constituency_No": "58", "Constituency_Name": "KANUBARI",
                  "Part_No": "1"}

rows, issues = build_rows(pages, method="regex")
for r in rows:
    print(r)
print("ISSUES:", issues)
assert len(rows) == 6, f"expected 6, got {len(rows)}"
by_serial = {r["Serial_No"]: r for r in rows}

r91 = by_serial["91"]
assert r91["EPIC_No"] == "CRC0108787" and r91["Name"] == "MITA DEY"
assert r91["Relation_Type"] == "Husband"
assert r91["Relation_Name"] == "MONOTOSH LAL DEY"
assert r91["House_Number"] == "E-68" and r91["Age"] == "50"
assert r91["Gender"] == "Female" and r91["Page"] == 3

# The one that broke production: a name starting with "Photo".
r168 = by_serial["168"]
assert r168["Name"] == "PHOTONG WANGSU" and r168["EPIC_No"] == "CRC0107987"

# Mothers relation + un-bolded serial + mixed-case names.
assert by_serial["99"]["Relation_Type"] == "Mother"
assert by_serial["111"]["Name"] == "Phepan Bohham"

# Every field populated on every row.
for r in rows:
    for f in ["Serial_No", "EPIC_No", "Name", "Relation_Type",
              "Relation_Name", "House_Number", "Age", "Gender"]:
        assert r[f], f"empty {f} in {r}"

# Photo_Id column exists and is empty when photos are off.
assert "Photo_Id" in rows[0] and rows[0]["Photo_Id"] == ""

# ---- repair pass: a strict-parser miss must be recovered by the lenient one.
# Here serial 200's marker has odd spacing that the strict split can trip on;
# the merge/repair must still yield a complete record and no gap 200..201.
REPAIR = """Assembly Constituency No and Name : 58-KANUBARI
Part No. : 1

| **200** Name : ALPHA ONE Fathers Name : X ONE House Number : E-1 Age : 40 Gender : Male | CRC0200000 | **201** Name : BETA TWO Fathers Name : Y TWO House Number : E-2 Age : 41 Gender : Male | CRC0200001 |
"""
rrows, rissues = build_rows([PageText(index=0, markdown=REPAIR)], method="regex")
rser = {r["Serial_No"] for r in rrows}
assert {"200", "201"} <= rser, f"repair lost a serial: {rser}"
for r in rrows:
    for f in ["EPIC_No", "Name", "Relation_Type", "Age", "Gender"]:
        assert r[f], f"repair left empty {f}: {r}"
assert rissues["missing_serials"] == [], rissues
assert rissues["incomplete_rows"] == [], rissues

print("\nALL ASSERTIONS PASSED")
