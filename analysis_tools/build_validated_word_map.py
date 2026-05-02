"""Build an audit table for Appendix B validated parameters.

This derives ARINC word offsets from ExactSample.csv timestamps.  The
fractional second identifies the native word location within a 512 wps
ARINC 717 subframe/second.  Use nearest-word rounding because the CSV timestamps are
decimal-rounded; using floor creates many off-by-one word positions.
"""
import csv
import difflib
import math
import re
from collections import Counter
from pathlib import Path


BASE_UTC = 288200
WORDS_PER_SECOND = 512
WORDS_PER_SUBFRAME = 512


ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = ROOT / "generated"

PARAMETERS_CSV = ROOT / "parameters.csv"
EXACT_SAMPLE_CSV = ROOT / "ExactSample.csv"
OLD_DFL_CSV = "dfl_map.csv"
OUT_CSV = GENERATED_DIR / "validated_param_word_map.csv"
OUT_MD = GENERATED_DIR / "validated_param_word_map.md"


def norm_name(name):
    s = name.lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def read_exact():
    with open(EXACT_SAMPLE_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    data_idx = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "DATA":
            data_idx = i
            break
    if data_idx is None:
        raise RuntimeError("DATA marker not found in ExactSample.csv")

    headers = [h.strip() for h in rows[data_idx + 1]]
    units = [u.strip() for u in rows[data_idx + 2]]
    discrete_defs = [d.strip() for d in rows[data_idx + 3]]
    data_rows = [r for r in rows[data_idx + 4 :] if r and r[0].strip()]
    return headers, units, discrete_defs, data_rows


def read_validated_params():
    out = []
    with open(PARAMETERS_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            idx = row.get("", "").strip()
            name = row.get("Validated Parameter Name", "").strip()
            if not idx.isdigit() or not name:
                continue
            out.append(
                {
                    "AppendixID": int(idx),
                    "ValidatedName": name,
                    "Units": row.get("Units", "").strip(),
                    "Description": row.get("Description", "").strip(),
                }
            )
    return out


def read_old_dfl():
    out = {}
    try:
        with open(OLD_DFL_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row["ParamName"].strip()
                words = [int(x) for x in row["WordPositions"].split(",") if x.strip()]
                out[name] = words
    except FileNotFoundError:
        pass
    return out


def resolve_exact_name(validated_name, exact_names):
    if validated_name in exact_names:
        return validated_name, "exact"

    exact_by_norm = {norm_name(n): n for n in exact_names}
    n = norm_name(validated_name)
    if n in exact_by_norm:
        return exact_by_norm[n], "normalized"

    choices = list(exact_by_norm.keys())
    matches = difflib.get_close_matches(n, choices, n=1, cutoff=0.90)
    if matches:
        candidate = exact_by_norm[matches[0]]
        # Do not let fuzzy matching turn SMYDC-1 into SMYDC-2, etc.
        left_digits = re.findall(r"\d+", validated_name)
        right_digits = re.findall(r"\d+", candidate)
        if left_digits and right_digits and left_digits != right_digits:
            return "", "missing"
        return candidate, "fuzzy"
    return "", "missing"


def word_summary(words):
    return "; ".join(f"{w}:W{w % WORDS_PER_SUBFRAME}" for w in words)


def extract_word_positions(headers, data_rows, col_idx):
    counts = Counter()
    sample_count = 0
    for row in data_rows:
        if col_idx >= len(row) or not row[col_idx].strip():
            continue
        try:
            t = float(row[0])
        except ValueError:
            continue
        rel = t - BASE_UTC
        sec = math.floor(rel)
        frac = rel - sec
        word = int(round(frac * WORDS_PER_SECOND)) % WORDS_PER_SECOND
        counts[word] += 1
        sample_count += 1
    return sorted(counts), counts, sample_count


def main():
    GENERATED_DIR.mkdir(exist_ok=True)
    validated = read_validated_params()
    headers, units, discrete_defs, data_rows = read_exact()
    exact_names = [h for h in headers if h and h != "Time"]
    exact_col = {h: i for i, h in enumerate(headers)}
    old_dfl = read_old_dfl()

    rows = []
    for p in validated:
        exact_name, match_kind = resolve_exact_name(p["ValidatedName"], exact_names)
        status = "mapped" if exact_name else "not_in_exact_sample"
        col_idx = exact_col.get(exact_name)
        words = []
        counts = Counter()
        sample_count = 0
        exact_unit = ""
        discrete_def = ""
        if col_idx is not None:
            words, counts, sample_count = extract_word_positions(headers, data_rows, col_idx)
            exact_unit = units[col_idx] if col_idx < len(units) else ""
            discrete_def = discrete_defs[col_idx] if col_idx < len(discrete_defs) else ""

        old_words = old_dfl.get(exact_name, [])
        old_status = ""
        if old_words:
            old_status = "same" if old_words == words else "DIFF"
        elif exact_name:
            old_status = "not_in_old_dfl"

        rows.append(
            {
                **p,
                "ExactSampleName": exact_name,
                "NameMatch": match_kind,
                "Status": status,
                "ExactUnit": exact_unit,
                "DiscreteDefinition": discrete_def,
                "SampleCount": sample_count,
                "WordCountPerSecond": len(words),
                "WordPositions0": ",".join(str(w) for w in words),
                "SubframeOffsets0": word_summary(words),
                "PerWordSampleCounts": ",".join(f"{w}:{counts[w]}" for w in words),
                "OldDflPositions0": ",".join(str(w) for w in old_words),
                "OldDflCompare": old_status,
            }
        )

    fieldnames = [
        "AppendixID",
        "ValidatedName",
        "ExactSampleName",
        "NameMatch",
        "Status",
        "Units",
        "ExactUnit",
        "Description",
        "DiscreteDefinition",
        "SampleCount",
        "WordCountPerSecond",
        "WordPositions0",
        "SubframeOffsets0",
        "PerWordSampleCounts",
        "OldDflPositions0",
        "OldDflCompare",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    mapped = [r for r in rows if r["Status"] == "mapped"]
    missing = [r for r in rows if r["Status"] != "mapped"]
    old_diff = [r for r in rows if r["OldDflCompare"] == "DIFF"]
    by_rate = Counter(r["WordCountPerSecond"] for r in mapped)

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# Validated Parameter Word Map Audit\n\n")
        f.write(f"- Validated parameters considered: {len(rows)}\n")
        f.write(f"- Mapped from ExactSample timestamps: {len(mapped)}\n")
        f.write(f"- Not present in ExactSample: {len(missing)}\n")
        f.write(f"- Existing dfl_map.csv differs from rounded timestamp positions: {len(old_diff)}\n\n")
        f.write("## Native Word Count Distribution\n\n")
        for rate, n in sorted(by_rate.items()):
            f.write(f"- {rate} word positions/sec: {n} parameters\n")
        f.write("\n## Parameters Missing From ExactSample\n\n")
        for r in missing:
            f.write(f"- {r['AppendixID']}: {r['ValidatedName']}\n")
        f.write("\n## Existing dfl_map.csv Position Differences\n\n")
        for r in old_diff:
            f.write(
                f"- {r['ValidatedName']}: old `{r['OldDflPositions0']}` -> "
                f"rounded `{r['WordPositions0']}`\n"
            )
        f.write("\n## Key Parameters\n\n")
        key = {
            "Altitude Press",
            "Ground Spd",
            "Heading",
            "Pitch Angle",
            "Roll Angle",
            "Accel Vert",
            "Eng1 N1",
            "Eng2 N1",
            "Eng1 Cutoff SW",
            "Eng2 Cutoff SW",
        }
        for r in rows:
            if r["ValidatedName"] in key:
                f.write(
                    f"- {r['ValidatedName']}: `{r['SubframeOffsets0']}` "
                    f"({r['WordCountPerSecond']} Hz-ish, samples={r['SampleCount']})\n"
                )

    print(f"Wrote {OUT_CSV}")
    print(f"Wrote {OUT_MD}")
    print(f"Mapped {len(mapped)}/{len(rows)} validated parameters")
    print(f"Missing from ExactSample: {len(missing)}")
    print(f"Old dfl_map differences: {len(old_diff)}")


if __name__ == "__main__":
    main()
