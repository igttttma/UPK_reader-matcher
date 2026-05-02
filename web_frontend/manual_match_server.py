import csv
import json
import math
import re
import sys
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "analysis_tools"))

from robust_candidate_search import (
    BASE_UTC,
    NO_DATA,
    WORDS_PER_SECOND,
    load_upk,
    parse_discrete_map,
    series_for_column,
)


EXACT_CSV = ROOT / "ExactSample.csv"
GENERATED_DIR = ROOT / "generated"
WORD_MAP_CSV = GENERATED_DIR / "validated_param_word_map.csv"
CORE_PARAMS_CSV = GENERATED_DIR / "core_49_params.csv"
UPK_PATH = ROOT / "12minute.upk"
HTML_PATH = SCRIPT_DIR / "manual_matcher.html"
START_SECOND = 600
DISPLAY_SHIFT = 43
DEFAULT_SHIFTS = [43]


def finite_float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def word_label(word):
    return f"{word}:W{word}"


def read_exact():
    with open(EXACT_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    data_idx = next(i for i, row in enumerate(rows) if row and row[0].strip() == "DATA")
    headers = [h.strip() for h in rows[data_idx + 1]]
    units = [u.strip() for u in rows[data_idx + 2]]
    discrete_defs = [d.strip() for d in rows[data_idx + 3]]
    data_rows = [r for r in rows[data_idx + 4 :] if r and r[0].strip()]
    return headers, units, discrete_defs, data_rows


def read_word_map():
    out = {}
    if not WORD_MAP_CSV.exists():
        return out
    with open(WORD_MAP_CSV, newline="", encoding="utf-8-sig") as f:
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
                "id": row.get("AppendixID", "").strip(),
                "validated_name": row.get("ValidatedName", "").strip(),
                "description": row.get("Description", "").strip(),
                "timestamp_words": words,
                "sample_count": row.get("SampleCount", "").strip(),
                "word_count_per_second": row.get("WordCountPerSecond", "").strip(),
            }
    return out


def read_core_params():
    out = {}
    if not CORE_PARAMS_CSV.exists():
        return out
    with open(CORE_PARAMS_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = row.get("name", "").strip()
            if name:
                out[name] = row
    return out


@lru_cache(maxsize=1)
def data_cache():
    headers, units, discrete_defs, data_rows = read_exact()
    raw, unsigned, signed, valid = load_upk(UPK_PATH)
    word_map = read_word_map()
    core_params = read_core_params()
    return {
        "headers": headers,
        "units": units,
        "discrete_defs": discrete_defs,
        "data_rows": data_rows,
        "raw": raw,
        "unsigned": unsigned,
        "signed": signed,
        "valid": valid,
        "word_map": word_map,
        "core_params": core_params,
    }


def fit_one_word(x, y, min_samples):
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(mask.sum())
    if n < min_samples:
        return None
    x = x[mask].astype(np.float64)
    y = y[mask].astype(np.float64)
    if x.size < min_samples or np.nanstd(x) == 0 or np.nanstd(y) == 0:
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
    r = (n * sxy - sx * sy) / math.sqrt(denom_product)
    pred = slope * x + offset
    rmse = math.sqrt(float(((pred - y) ** 2).sum()) / n)
    return {
        "n": n,
        "slope": float(slope),
        "offset": float(offset),
        "r": float(r),
        "rmse": float(rmse),
    }


def fit_one_bit(raw_word, valid_word, y, bit, inverted, min_samples):
    expected = np.rint(y).astype(np.int64)
    comparable = valid_word & ((expected == 0) | (expected == 1))
    n = int(comparable.sum())
    if n < min_samples:
        return None
    bit_values = ((raw_word >> bit) & 1).astype(np.int64)
    if inverted:
        bit_values = 1 - bit_values
    x = bit_values[comparable]
    yy = expected[comparable]
    matches = int((x == yy).sum())
    ones = int((yy == 1).sum())
    zeros = int((yy == 0).sum())
    pred_ones = int((x == 1).sum())
    balanced = None
    if ones and zeros:
        tp = int(((x == 1) & (yy == 1)).sum())
        tn = int(((x == 0) & (yy == 0)).sum())
        balanced = 0.5 * ((tp / ones) + (tn / zeros))
    return {
        "n": n,
        "accuracy": matches / n,
        "balanced_accuracy": balanced,
        "matches": matches,
        "ones": ones,
        "zeros": zeros,
        "pred_ones": pred_ones,
        "constant_exact": not (ones and zeros),
    }


def get_param_series(name):
    cache = data_cache()
    try:
        col = cache["headers"].index(name)
    except ValueError:
        raise KeyError(name)
    discrete_def = cache["discrete_defs"][col] if col < len(cache["discrete_defs"]) else ""
    discrete_map = parse_discrete_map(discrete_def) if discrete_def and discrete_def != "NUMBER" else None
    times, values = series_for_column(cache["data_rows"], col, discrete_map)
    return col, discrete_def, times, values


def parse_shifts(query):
    raw = query.get("shifts", [""])[0].strip()
    if not raw:
        return DEFAULT_SHIFTS
    shifts = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = [int(x) for x in part.split("-", 1)]
            shifts.extend(range(min(a, b), max(a, b) + 1))
        else:
            shifts.append(int(part))
    return sorted(set(shifts))


def candidate_rows(name, shifts, min_samples, include_all):
    cache = data_cache()
    _, discrete_def, times, values = get_param_series(name)
    rel_times = times - BASE_UTC
    csv_seconds = np.floor(rel_times).astype(np.int64)
    sample_words = np.rint((rel_times - csv_seconds) * WORDS_PER_SECOND).astype(np.int64) % WORDS_PER_SECOND
    hint_words = set(cache["word_map"].get(name, {}).get("timestamp_words", []))
    rows = []
    for shift in shifts:
        upk_seconds = csv_seconds - shift
        valid_seconds = (upk_seconds >= START_SECOND) & (upk_seconds < cache["unsigned"].shape[0])
        if int(valid_seconds.sum()) < min_samples:
            continue
        if discrete_def and discrete_def != "NUMBER":
            for word in range(WORDS_PER_SECOND):
                phase_mask = valid_seconds & (sample_words == word)
                if int(phase_mask.sum()) < min_samples:
                    continue
                y = values[phase_mask]
                raw_word = cache["raw"][upk_seconds[phase_mask], word]
                valid_word = cache["valid"][upk_seconds[phase_mask], word]
                for bit in range(12):
                    for inverted in (False, True):
                        fit = fit_one_bit(raw_word, valid_word, y, bit, inverted, min_samples)
                        if fit is None:
                            continue
                        rows.append(
                            {
                                "mode": "discrete",
                                "word": word,
                                "bit": bit,
                                "inverted": inverted,
                                "label": f"{word_label(word)} b{bit}{' inv' if inverted else ''}",
                                "shift": int(shift),
                                "interp": "bit",
                                "accuracy": finite_float(fit["accuracy"]),
                                "balanced_accuracy": finite_float(fit["balanced_accuracy"]),
                                "matches": int(fit["matches"]),
                                "n": int(fit["n"]),
                                "ones": int(fit["ones"]),
                                "zeros": int(fit["zeros"]),
                                "pred_ones": int(fit["pred_ones"]),
                                "constant_exact": bool(fit["constant_exact"]),
                                "timestamp": word in hint_words,
                            }
                        )
        else:
            for interp, raw_values in (("unsigned12", cache["unsigned"]), ("signed12", cache["signed"])):
                for word in range(WORDS_PER_SECOND):
                    phase_mask = valid_seconds & (sample_words == word)
                    if int(phase_mask.sum()) < min_samples:
                        continue
                    y = values[phase_mask]
                    x = raw_values[upk_seconds[phase_mask], word]
                    fit = fit_one_word(x, y, min_samples)
                    if fit is None:
                        continue
                    rows.append(
                        {
                            "mode": "continuous",
                            "word": word,
                            "label": word_label(word),
                            "shift": int(shift),
                            "interp": interp,
                            "r": finite_float(fit["r"]),
                            "rmse": finite_float(fit["rmse"]),
                            "n": int(fit["n"]),
                            "slope": finite_float(fit["slope"]),
                            "offset": finite_float(fit["offset"]),
                            "timestamp": word in hint_words,
                        }
                    )
    if discrete_def and discrete_def != "NUMBER":
        rows.sort(
            key=lambda r: (
                r["constant_exact"],
                -(r["balanced_accuracy"] if r["balanced_accuracy"] is not None else r["accuracy"] or 0),
                -(r["accuracy"] or 0),
                not r["timestamp"],
                -r["n"],
            )
        )
    else:
        rows.sort(key=lambda r: (-abs(r["r"] or 0), -r["n"]))
    if not include_all:
        rows = rows[: max(1, min(len(rows), 400))]
    seen = set()
    deduped = []
    for row in rows:
        key = (row["word"], row["shift"], row["interp"], row.get("bit"), row.get("inverted"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def row_score(row):
    if row.get("mode") == "discrete":
        if row.get("constant_exact"):
            return row.get("accuracy") or 0
        score = row.get("balanced_accuracy")
        return score if score is not None else row.get("accuracy") or 0
    return abs(row.get("r") or 0)


def params_payload():
    cache = data_cache()
    params = []
    for i, name in enumerate(cache["headers"]):
        if i == 0 or not name:
            continue
        info = cache["word_map"].get(name, {})
        if not info:
            continue
        core_info = cache["core_params"].get(name, {})
        params.append(
            {
                "name": name,
                "units": cache["units"][i] if i < len(cache["units"]) else "",
                "discrete": bool(cache["discrete_defs"][i] and cache["discrete_defs"][i] != "NUMBER"),
                "core": name in cache["core_params"],
                "id": core_info.get("id") or info.get("id", ""),
                "description": info.get("description", ""),
                "timestamp_words": info.get("timestamp_words", []),
                "relocated_location": "",
                "relocated_confidence": "core-list-only" if name in cache["core_params"] else "",
            }
        )
    return {"upk_words_per_second": WORDS_PER_SECOND, "params": params}


def candidates_payload(query):
    name = query.get("param", [""])[0]
    if not name:
        raise ValueError("Missing param")
    shifts = parse_shifts(query)
    min_samples = int(query.get("min_samples", ["8"])[0])
    limit = int(query.get("limit", ["250"])[0])
    include_all = query.get("all", ["0"])[0] == "1"
    rows = candidate_rows(name, shifts, min_samples, include_all)
    if not include_all:
        timestamp_rows = [r for r in candidate_rows(name, shifts, min_samples, True) if r["timestamp"]]
        rows = rows + timestamp_rows
        rows.sort(key=lambda r: (not r["timestamp"], r.get("constant_exact", False), -row_score(r), -r["n"]))
    unique = []
    seen = set()
    for row in rows:
        key = (row["word"], row["shift"], row["interp"], row.get("bit"), row.get("inverted"))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return {"param": name, "count": len(unique), "rows": unique[:limit]}


def plot_payload(query):
    cache = data_cache()
    name = query.get("param", [""])[0]
    word = int(query.get("word", ["0"])[0])
    bit = query.get("bit", [""])[0]
    inverted = query.get("inverted", ["0"])[0] == "1"
    shift = int(query.get("shift", ["583"])[0])
    interp = query.get("interp", ["unsigned12"])[0]
    _, discrete_def, times, values = get_param_series(name)
    csv_seconds = np.floor(times - BASE_UTC).astype(np.int64)
    display_seconds = csv_seconds - DISPLAY_SHIFT
    exact_ok = (display_seconds >= START_SECOND) & (display_seconds < cache["unsigned"].shape[0])
    raw_values = cache["signed"] if interp == "signed12" else cache["unsigned"]

    # Plot raw data from the UPK stream itself. Do not drive raw dots from
    # ExactSample rows: high-rate parameters would duplicate one raw value many
    # times, and tail fill gaps would look like timing drift.
    raw_seconds = np.arange(START_SECOND, cache["unsigned"].shape[0], dtype=np.int64)
    if bit != "":
        bit_idx = int(bit)
        raw_sample = ((cache["raw"][raw_seconds, word] >> bit_idx) & 1).astype(np.float64)
        if inverted:
            raw_sample = 1.0 - raw_sample
        raw_valid = cache["valid"][raw_seconds, word]
    else:
        raw_sample = raw_values[raw_seconds, word]
        raw_valid = np.isfinite(raw_sample)
    raw_x = raw_seconds.astype(np.float64) + word / WORDS_PER_SECOND
    exact_points = [
        {"x": float(a), "y": float(b)}
        for a, b in zip((times[exact_ok] - BASE_UTC) - DISPLAY_SHIFT, values[exact_ok])
        if math.isfinite(float(b))
    ]
    raw_points = [
        {"x": float(a), "y": float(b)}
        for a, b in zip(raw_x[raw_valid], raw_sample[raw_valid])
        if math.isfinite(float(b))
    ]
    return {
        "param": name,
        "word": word,
        "bit": int(bit) if bit != "" else None,
        "inverted": inverted,
        "label": f"{word_label(word)} b{bit}{' inv' if inverted else ''}" if bit != "" else word_label(word),
        "shift": shift,
        "interp": interp,
        "mode": "discrete" if discrete_def and discrete_def != "NUMBER" else "continuous",
        "exact_points": exact_points,
        "raw_points": raw_points,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/", "/manual_matcher.html"):
                self.send_file(HTML_PATH, "text/html; charset=utf-8")
            elif path == "/api/params":
                self.send_json(params_payload())
            elif path == "/api/candidates":
                self.send_json(candidates_payload(parse_qs(parsed.query)))
            elif path == "/api/plot":
                self.send_json(plot_payload(parse_qs(parsed.query)))
            else:
                self.send_error(404, "Not found")
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    def send_file(self, path, content_type):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    data_cache()
    port = 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Manual matcher: http://127.0.0.1:{port}/")
    print(f"UPK candidate universe: {WORDS_PER_SECOND} word positions per second, plus signed/unsigned views.")
    server.serve_forever()


if __name__ == "__main__":
    main()
