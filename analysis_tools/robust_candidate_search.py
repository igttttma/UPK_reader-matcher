"""Search UPK word/bit candidates against NTSB ExactSample parameters.

This is a reproducible replacement for one-off parameter searches.  It treats
timestamp-derived word positions as hints only, then scans every UPK word across
a configurable alignment-shift window.
"""
import argparse
import csv
import math
import re
import struct
from pathlib import Path

import numpy as np


BASE_UTC = 288200
WORDS_PER_SECOND = 512
NO_DATA = 0x0FFF
ROOT = Path(__file__).resolve().parent.parent


def read_exact(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    data_idx = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "DATA":
            data_idx = i
            break
    if data_idx is None:
        raise RuntimeError(f"DATA marker not found in {path}")

    headers = [h.strip() for h in rows[data_idx + 1]]
    units = [u.strip() for u in rows[data_idx + 2]]
    discrete_defs = [d.strip() for d in rows[data_idx + 3]]
    data_rows = [r for r in rows[data_idx + 4 :] if r and r[0].strip()]
    return headers, units, discrete_defs, data_rows


def read_word_map(path):
    out = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = row.get("ExactSampleName", "").strip()
            if not name:
                continue
            words = []
            for item in row.get("WordPositions0", "").split(","):
                item = item.strip()
                if item:
                    words.append(int(item))
            out[name] = {
                "appendix_id": row.get("AppendixID", "").strip(),
                "validated_name": row.get("ValidatedName", "").strip(),
                "timestamp_words": words,
            }
    return out


def load_upk(path):
    raw = np.fromfile(path, dtype="<u2")
    if raw.size % WORDS_PER_SECOND:
        raise RuntimeError(f"{path} word count is not divisible by {WORDS_PER_SECOND}")
    raw = (raw & 0x0FFF).reshape((-1, WORDS_PER_SECOND))
    valid = raw != NO_DATA
    unsigned = raw.astype(np.float64)
    unsigned[~valid] = np.nan
    signed_raw = raw.astype(np.int16)
    signed_raw = np.where(signed_raw >= 2048, signed_raw - 4096, signed_raw)
    signed = signed_raw.astype(np.float64)
    signed[~valid] = np.nan
    return raw, unsigned, signed, valid


def safe_float(value):
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_discrete_map(definition):
    mapping = {}
    if not definition or definition == "NUMBER":
        return mapping
    for number, label in re.findall(r"([-+]?\d+(?:\.\d+)?)(?=:[^=]*=\"):[^=]*=\"([^\"]+)\"", definition):
        mapping[label.strip().lower()] = float(number)
    return mapping


def parse_cell_value(value, discrete_map=None):
    numeric = safe_float(value)
    if numeric is not None:
        return numeric
    if discrete_map:
        return discrete_map.get(value.strip().lower())
    return None


def series_for_column(data_rows, col_idx, discrete_map=None):
    times = []
    vals = []
    for row in data_rows:
        if col_idx >= len(row):
            continue
        t = safe_float(row[0])
        v = parse_cell_value(row[col_idx], discrete_map)
        if t is None or v is None or not math.isfinite(v):
            continue
        times.append(t)
        vals.append(v)
    return np.asarray(times, dtype=np.float64), np.asarray(vals, dtype=np.float64)


def words_label(words):
    return ",".join(str(w) for w in words)


def word_offset(word):
    return f"W{word}"


def fit_all_words(raw_by_sample, y, min_samples):
    mask = np.isfinite(raw_by_sample)
    n = mask.sum(axis=0).astype(np.float64)
    good_n = n >= min_samples
    x = np.where(mask, raw_by_sample, 0.0)
    yy = np.where(mask, y[:, None], 0.0)

    sx = x.sum(axis=0)
    sy = yy.sum(axis=0)
    sxx = (x * x).sum(axis=0)
    syy = (yy * yy).sum(axis=0)
    sxy = (x * yy).sum(axis=0)

    x_denom = n * sxx - sx * sx
    y_denom = n * syy - sy * sy
    denom_product = x_denom * y_denom
    denom = np.full(raw_by_sample.shape[1], np.nan)
    positive_denom = denom_product > 0
    denom[positive_denom] = np.sqrt(denom_product[positive_denom])
    valid_fit = good_n & (x_denom > 0) & (y_denom > 0) & np.isfinite(denom) & (denom > 0)

    slope = np.full(raw_by_sample.shape[1], np.nan)
    offset = np.full(raw_by_sample.shape[1], np.nan)
    pearson = np.full(raw_by_sample.shape[1], np.nan)
    rmse = np.full(raw_by_sample.shape[1], np.nan)

    slope[valid_fit] = (n[valid_fit] * sxy[valid_fit] - sx[valid_fit] * sy[valid_fit]) / x_denom[valid_fit]
    offset[valid_fit] = (sy[valid_fit] - slope[valid_fit] * sx[valid_fit]) / n[valid_fit]
    pearson[valid_fit] = (n[valid_fit] * sxy[valid_fit] - sx[valid_fit] * sy[valid_fit]) / denom[valid_fit]

    sse = (
        slope * slope * sxx
        + 2 * slope * offset * sx
        + offset * offset * n
        - 2 * slope * sxy
        - 2 * offset * sy
        + syy
    )
    rmse[valid_fit] = np.sqrt(np.maximum(sse[valid_fit], 0.0) / n[valid_fit])
    return n, slope, offset, pearson, rmse


def fit_one_word(x, y, min_samples):
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(mask.sum())
    if n < min_samples:
        return None
    x = x[mask].astype(np.float64)
    y = y[mask].astype(np.float64)
    if np.nanstd(x) == 0 or np.nanstd(y) == 0:
        return None
    sx = x.sum()
    sy = y.sum()
    sxx = (x * x).sum()
    syy = (y * y).sum()
    sxy = (x * y).sum()
    x_denom = n * sxx - sx * sx
    y_denom = n * syy - sy * sy
    denom_product = x_denom * y_denom
    if x_denom <= 0 or y_denom <= 0 or denom_product <= 0:
        return None
    slope = (n * sxy - sx * sy) / x_denom
    offset = (sy - slope * sx) / n
    pearson = (n * sxy - sx * sy) / math.sqrt(denom_product)
    pred = slope * x + offset
    rmse = math.sqrt(float(((pred - y) ** 2).sum()) / n)
    return {
        "n": n,
        "slope": float(slope),
        "offset": float(offset),
        "pearson": float(pearson),
        "absr": float(abs(pearson)),
        "rmse": float(rmse),
        "raw_min": float(np.nanmin(x)),
        "raw_max": float(np.nanmax(x)),
    }


def top_continuous_candidates(param, times, values, unsigned, signed, shifts, min_samples, top_k, hint_words):
    rel_times = times - BASE_UTC
    csv_seconds = np.floor(rel_times).astype(np.int64)
    sample_words = np.rint((rel_times - csv_seconds) * WORDS_PER_SECOND).astype(np.int64) % WORDS_PER_SECOND
    rows = []
    for shift in shifts:
        upk_seconds = csv_seconds - shift
        valid_seconds = (upk_seconds >= 0) & (upk_seconds < unsigned.shape[0])
        if int(valid_seconds.sum()) < min_samples:
            continue
        for interp_name, raw_values in (("unsigned12", unsigned), ("signed12", signed)):
            for word in range(WORDS_PER_SECOND):
                phase_mask = valid_seconds & (sample_words == word)
                if int(phase_mask.sum()) < min_samples:
                    continue
                fit = fit_one_word(raw_values[upk_seconds[phase_mask], word], values[phase_mask], min_samples)
                if fit is None:
                    continue
                rows.append(
                    {
                        "ParamName": param,
                        "Shift": shift,
                        "Interpretation": interp_name,
                        "CandidateWord": word,
                        "SF": word // 512 + 1,
                        "WordInSF": word % 512,
                        "PearsonR": fit["pearson"],
                        "AbsR": fit["absr"],
                        "RMSE": fit["rmse"],
                        "NSamples": fit["n"],
                        "RawMin": fit["raw_min"],
                        "RawMax": fit["raw_max"],
                        "Slope": fit["slope"],
                        "Offset": fit["offset"],
                        "TimestampWordPositions": words_label(hint_words),
                        "IsTimestampDerived": word in hint_words,
                    }
                )
    rows.sort(key=lambda r: (-r["AbsR"], r["RMSE"], -r["NSamples"]))
    return rows[:top_k]


def top_discrete_candidates(param, times, values, raw, valid, shifts, min_samples, top_k, hint_words):
    expected = np.rint(values).astype(np.int64)
    rel_times = times - BASE_UTC
    csv_seconds = np.floor(rel_times).astype(np.int64)
    sample_words = np.rint((rel_times - csv_seconds) * WORDS_PER_SECOND).astype(np.int64) % WORDS_PER_SECOND
    rows = []
    for shift in shifts:
        upk_seconds = csv_seconds - shift
        valid_seconds = (upk_seconds >= 0) & (upk_seconds < raw.shape[0])
        if int(valid_seconds.sum()) < min_samples:
            continue
        y = expected[valid_seconds]
        if np.unique(y[(y == 0) | (y == 1)]).size < 2:
            continue
        for bit in range(12):
            for word in range(WORDS_PER_SECOND):
                phase_mask = valid_seconds & (sample_words == word)
                if int(phase_mask.sum()) < min_samples:
                    continue
                comparable = valid[upk_seconds[phase_mask], word] & ((expected[phase_mask] == 0) | (expected[phase_mask] == 1))
                n = int(comparable.sum())
                if n < min_samples:
                    continue
                bit_values = ((raw[upk_seconds[phase_mask], word] >> bit) & 1).astype(np.int8)
                for inverted in (False, True):
                    x = 1 - bit_values if inverted else bit_values
                    matches = int(((x == expected[phase_mask]) & comparable).sum())
                    accuracy = matches / n if n else 0.0
                    rows.append(
                        {
                            "ParamName": param,
                            "Shift": shift,
                            "CandidateWord": word,
                            "SF": word // 512 + 1,
                            "WordInSF": word % 512,
                            "Bit": bit,
                            "Inverted": inverted,
                            "Accuracy": accuracy,
                            "Matches": matches,
                            "NSamples": n,
                            "TimestampWordPositions": words_label(hint_words),
                            "IsTimestampDerived": word in hint_words,
                        }
                    )
    rows.sort(key=lambda r: (-r["Accuracy"], -r["NSamples"]))
    return rows[:top_k]


def parse_param_filter(value):
    if not value:
        return None
    return {v.strip() for v in re.split(r"[;,]", value) if v.strip()}


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upk", default=str(ROOT / "12minute.upk"))
    parser.add_argument("--exact", default=str(ROOT / "ExactSample.csv"))
    parser.add_argument("--word-map", default=str(ROOT / "generated" / "validated_param_word_map.csv"))
    parser.add_argument("--out-prefix", default="candidate_search")
    parser.add_argument("--shift-start", type=int, default=578)
    parser.add_argument("--shift-end", type=int, default=588)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--params", default="", help="Optional semicolon/comma separated parameter-name filter")
    args = parser.parse_args()

    headers, units, discrete_defs, data_rows = read_exact(Path(args.exact))
    exact_col = {h: i for i, h in enumerate(headers)}
    word_map = read_word_map(Path(args.word_map))
    raw, unsigned, signed, valid = load_upk(Path(args.upk))
    shifts = list(range(args.shift_start, args.shift_end + 1))
    param_filter = parse_param_filter(args.params)

    continuous_rows = []
    discrete_rows = []
    considered = 0
    skipped = 0

    for name, meta in word_map.items():
        if param_filter and name not in param_filter and meta["validated_name"] not in param_filter:
            continue
        col_idx = exact_col.get(name)
        if col_idx is None:
            skipped += 1
            continue
        discrete_def = discrete_defs[col_idx] if col_idx < len(discrete_defs) else ""
        is_discrete = discrete_def and discrete_def != "NUMBER"
        times, values = series_for_column(data_rows, col_idx, parse_discrete_map(discrete_def) if is_discrete else None)
        if values.size < args.min_samples:
            skipped += 1
            continue
        considered += 1
        hint_words = meta["timestamp_words"]
        if is_discrete:
            discrete_rows.extend(
                top_discrete_candidates(name, times, values, raw, valid, shifts, args.min_samples, args.top_k, hint_words)
            )
        else:
            continuous_rows.extend(
                top_continuous_candidates(name, times, values, unsigned, signed, shifts, args.min_samples, args.top_k, hint_words)
            )

    cont_fields = [
        "ParamName",
        "Shift",
        "Interpretation",
        "CandidateWord",
        "SF",
        "WordInSF",
        "PearsonR",
        "AbsR",
        "RMSE",
        "NSamples",
        "RawMin",
        "RawMax",
        "Slope",
        "Offset",
        "TimestampWordPositions",
        "IsTimestampDerived",
    ]
    disc_fields = [
        "ParamName",
        "Shift",
        "CandidateWord",
        "SF",
        "WordInSF",
        "Bit",
        "Inverted",
        "Accuracy",
        "Matches",
        "NSamples",
        "TimestampWordPositions",
        "IsTimestampDerived",
    ]

    cont_path = f"{args.out_prefix}_continuous.csv"
    disc_path = f"{args.out_prefix}_discrete.csv"
    write_csv(cont_path, continuous_rows, cont_fields)
    write_csv(disc_path, discrete_rows, disc_fields)

    with open(f"{args.out_prefix}_summary.md", "w", encoding="utf-8") as f:
        f.write("# Robust Candidate Search Summary\n\n")
        f.write(f"- UPK: `{args.upk}`\n")
        f.write(f"- ExactSample: `{args.exact}`\n")
        f.write(f"- Shift window: `{args.shift_start}` to `{args.shift_end}`\n")
        f.write(f"- Top candidates per parameter: `{args.top_k}`\n")
        f.write(f"- Minimum aligned samples: `{args.min_samples}`\n")
        f.write(f"- Parameters considered: `{considered}`\n")
        f.write(f"- Parameters skipped: `{skipped}`\n")
        f.write(f"- Continuous candidate rows: `{len(continuous_rows)}`\n")
        f.write(f"- Discrete candidate rows: `{len(discrete_rows)}`\n\n")
        f.write("## Best Continuous Candidates\n\n")
        for row in continuous_rows[:40]:
            f.write(
                f"- {row['ParamName']}: word `{row['CandidateWord']}` ({word_offset(row['CandidateWord'])}), "
                f"shift `{row['Shift']}`, {row['Interpretation']}, "
                f"r `{row['PearsonR']:.5f}`, rmse `{row['RMSE']:.5f}`\n"
            )
        f.write("\n## Best Discrete Bit Candidates\n\n")
        for row in discrete_rows[:40]:
            inv = ", inverted" if row["Inverted"] else ""
            f.write(
                f"- {row['ParamName']}: word `{row['CandidateWord']}` ({word_offset(row['CandidateWord'])}) "
                f"bit `{row['Bit']}`{inv}, shift `{row['Shift']}`, "
                f"accuracy `{row['Accuracy']:.5f}` ({row['Matches']}/{row['NSamples']})\n"
            )

    print(f"Wrote {cont_path}")
    print(f"Wrote {disc_path}")
    print(f"Wrote {args.out_prefix}_summary.md")
    print(f"Considered {considered} parameters, skipped {skipped}")


if __name__ == "__main__":
    main()
