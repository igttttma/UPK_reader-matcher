"""Build the core 49-parameter list used by the manual matcher."""
import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = ROOT / "generated"
PARAMETERS_CSV = ROOT / "parameters.csv"
WORD_MAP_CSV = GENERATED_DIR / "validated_param_word_map.csv"
OUT_CSV = GENERATED_DIR / "core_49_params.csv"

CORE_IDS = {
    1, 2, 3, 4, 6, 7, 8, 9, 10, 15, 25,
    39, 40, 41, 42, 43, 44, 45, 46, 48, 49, 50, 51,
    52, 57, 58, 59, 62, 63, 64, 68, 69, 74, 75, 76, 79, 80, 81, 85,
    100, 106, 129, 130, 131, 132, 133, 134, 135, 153,
}


def read_word_map_names():
    names = {}
    if not WORD_MAP_CSV.exists():
        return names
    with open(WORD_MAP_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            appendix_id = row.get("AppendixID", "").strip()
            if appendix_id.isdigit():
                names[int(appendix_id)] = row.get("ExactSampleName", "").strip()
    return names


def main():
    GENERATED_DIR.mkdir(exist_ok=True)
    exact_names = read_word_map_names()
    rows = []
    with open(PARAMETERS_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            appendix_id = row.get("", "").strip()
            if not appendix_id.isdigit():
                continue
            appendix_id = int(appendix_id)
            if appendix_id not in CORE_IDS:
                continue
            name = exact_names.get(appendix_id) or row.get("Validated Parameter Name", "").strip()
            rows.append(
                {
                    "id": appendix_id,
                    "name": name,
                    "units": row.get("Units", "").strip(),
                    "description": row.get("Description", "").strip(),
                }
            )
    rows.sort(key=lambda item: item["id"])
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "units", "description"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {OUT_CSV}")
    print(f"Core parameters: {len(rows)}")


if __name__ == "__main__":
    main()
