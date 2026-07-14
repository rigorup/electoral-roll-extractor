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

# ---- transposed sparse-page layout (this page was silently SKIPPED: the
# ---- serial+EPIC sit in a header row, the names in a separate row below).
TRANSPOSED = """Assembly Constituency No and Name : 58-KANUBARI
Section No and Name 1-DASATHONG

Part No. : 2

|  421 | CRC0276709 | 422 | CRC0276733  |
| --- | --- | --- | --- |
|  Name : PIRANG JAMIKHAM Fathers Name : JANKO JAMIKHAM House Number : E-579 Age : 23 Gender : Female |  | Name : TINGNYE JAMIKHAM Fathers Name : JANKO JAMIKHAM House Number : E-579 Age : 23 Gender : Male |   |
"""
trows, tissues = build_rows([PageText(index=16, markdown=TRANSPOSED)],
                            method="regex")
assert len(trows) == 2, f"transposed page lost voters: {len(trows)}"
t = {r["Serial_No"]: r for r in trows}
# EPIC must belong to the RIGHT voter (it used to be stolen from the next one)
assert t["421"]["EPIC_No"] == "CRC0276709", t["421"]
assert t["422"]["EPIC_No"] == "CRC0276733", t["422"]
assert t["421"]["Name"] == "PIRANG JAMIKHAM"
assert t["422"]["Name"] == "TINGNYE JAMIKHAM"
assert tissues["incomplete_rows"] == [], tissues
# the page header ("...No and Name : 58-KANUBARI") must never become a voter
assert all(r["Name"] != "58-KANUBARI" for r in trows)

# ---- "List of Additions" supplement page: an EXTRA numeric column sits
# ---- between the serial and the EPIC. The serial is therefore NOT the integer
# ---- nearest the EPIC. Reading the wrong one gave every voter serial "1", so
# ---- the whole page collapsed into a single row and vanished.
ADDITIONS = """Assembly Constituency No and Name : 58-KANUBARI
1- List of Additions 1 (29-10-2024 05-01-2025 )

Part No. : 12

|  798 | 1 | CRC0299412 | 799 | 1 | CRC0299446 | 800 | 1 | GYS0303487  |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  Name : Anyen Arangham Fathers Name: Pongbo Arangham House Number : E-136 Age : 18 Gender : Female |   |  | Name : Thialamla Wangsu Others: Fenyo Wangcha House Number : E-32 Age : 31 Gender : Female |   |  | Name : JITEN RAI Fathers Name: RAMJI RAI House Number : E-173 Age : 35 Gender : Male |   |   |
"""
arows, aissues = build_rows([PageText(index=29, markdown=ADDITIONS)],
                            method="regex")
assert len(arows) == 3, f"additions page lost voters: {len(arows)}"
a = {r["Serial_No"]: r for r in arows}
# the real serials -- not the supplement column "1", and not the EPIC's digits
assert set(a) == {"798", "799", "800"}, f"wrong serials: {sorted(a)}"
assert a["798"]["EPIC_No"] == "CRC0299412" and a["798"]["Name"] == "Anyen Arangham"
assert a["800"]["EPIC_No"] == "GYS0303487" and a["800"]["Name"] == "JITEN RAI"
assert aissues["incomplete_rows"] == [], aissues

print("\nALL ASSERTIONS PASSED")
