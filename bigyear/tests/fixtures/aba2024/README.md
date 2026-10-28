# Frozen public ABA 2024 checklist test fixture

`sources/ABA_Checklist-8.16.csv.zip` is the official ABA Checklist 8.16 CSV
archive used by the historical checklist parser and membership regression.
It is a taxonomic reference list, not eBird observation data.

- Source: https://www.aba.org/wp-content/uploads/2020/03/ABA_Checklist-8.16.csv.zip
- SHA-256: `a74796ba8bb15c8edf327771daf8f216212def2bab067e1bde2729914ddfdad9`
- Copied unchanged from the analysis's frozen reference on October 28, 2026.

The archive contains 1,151 coded species rows; the parser deliberately retains
that count rather than inventing a row to match the publisher's 1,152 header.
Keeping the snapshot here makes the regression independent of ignored local
datasets and future changes to the publisher's download. This fixture does not
replace the reviewed countability policy required for a real EBD/EOD import.
