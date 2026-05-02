import struct
import csv
import math
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SYNC_WORDS = {
    0x247: {'sf': 1, 'name': 'SF1'},
    0x5B8: {'sf': 2, 'name': 'SF2'},
    0xA47: {'sf': 3, 'name': 'SF3'},
    0xDB8: {'sf': 4, 'name': 'SF4'},
}
SYNC_NAMES = {1: 'SF1', 2: 'SF2', 3: 'SF3', 4: 'SF4'}
WORDS_PER_SECOND = 512
BYTES_PER_SECOND = WORDS_PER_SECOND * 2
WORDS_PER_SUBFRAME = 512
SUBFRAMES_PER_CYCLE = 4
NO_DATA = 0x0FFF
BASE_UTC = 288200


class SubframeRecord:
    def __init__(self, global_index, sync_word, raw_words):
        self.global_index = global_index
        self.second = global_index
        self.sf_in_cycle = global_index % SUBFRAMES_PER_CYCLE
        info = SYNC_WORDS.get(sync_word)
        self.sf_type = info['sf'] if info else None
        self.sf_name = info['name'] if info else f'0x{sync_word:03X}'
        self.raw_words = raw_words

    def get_word(self, local_index):
        if local_index >= len(self.raw_words):
            return None
        raw = self.raw_words[local_index]
        if raw == NO_DATA:
            return None
        return raw & 0x0FFF

    def total_words(self):
        return len(self.raw_words)

    def non_empty_count(self):
        return sum(1 for w in self.raw_words if w != NO_DATA)


class UPKReader:
    """Reader for NTSB .upk files containing ARINC 573/717 QAR flight data.

    Format: 512 12-bit words per second/subframe, stored as 16-bit values
    (little-endian).
    Sync words (12-bit): SF1=0x247, SF2=0x5B8, SF3=0xA47, SF4=0xDB8 at word 0.
    0x0FFF marks no-data/erased words.

    This file is from a public NTSB recorder data release for a specific event.
    The data was manually corrected by NTSB to fix timing issues caused by a
    missing U2 flash chip in the HFR5-D recorder.

    The file contains one contiguous data region of about 735 seconds, followed
    by 0x0FFF padding. Earlier scripts incorrectly grouped four 512-word
    subframes as one second, compressing the timeline by 4x.
    """

    def __init__(self, filepath):
        with open(filepath, 'rb') as f:
            self.data = f.read()
        self.total_seconds = len(self.data) // BYTES_PER_SECOND

        self.subframes = []
        self._scan_all_subframes()

        self.data_regions = []
        self._identify_regions()

        self.param_map = None
        self.core_locations = {}

    def _scan_all_subframes(self):
        """Scan every 512-word subframe boundary for sync words.

        Instead of only checking at fixed second boundaries, this scans
        each subframe independently. This correctly handles:
        - Missing subframes (chip U2 dropout)
        - Subframes that exist but lack sync (data still present)
        - Transitions on partial subframe boundaries
        """
        total_subframes = len(self.data) // (WORDS_PER_SUBFRAME * 2)

        for sf_idx in range(total_subframes):
            offset = sf_idx * WORDS_PER_SUBFRAME * 2
            raw = struct.unpack_from(f'<{WORDS_PER_SUBFRAME}H', self.data, offset)
            sync_raw = raw[0]
            sync12 = sync_raw & 0x0FFF if sync_raw != NO_DATA else None

            if sync12 in SYNC_WORDS:
                self.subframes.append(SubframeRecord(sf_idx, sync12, raw))
            elif sync_raw != NO_DATA:
                non_empty = sum(1 for w in raw if w != NO_DATA)
                if non_empty > 0:
                    self.subframes.append(SubframeRecord(sf_idx, None, raw))

    def _identify_regions(self):
        """Identify contiguous data regions based on data presence.

        A data region is a contiguous range of seconds that have any
        non-0xFFF data. Sync status varies within each region due to
        chip U2 dropout (some seconds lack SF2) - this is tracked
        separately via sync_status().

        The file contains one continuous data region (seconds 0-183,
        184 seconds ≈ 3.1 minutes). Within it, even-numbered seconds
        typically have ~97% data density and all 4 syncs, while
        odd-numbered seconds have ~65% density and are missing SF2.
        Seconds 165, 169 lack SF2; second 183 lacks SF3+SF4 (end of
        data stream).
        """
        regions = []
        start = None

        for sec in range(self.total_seconds):
            has_data = self.second_has_any_data(sec)
            if has_data:
                if start is None:
                    start = sec
            else:
                if start is not None:
                    regions.append((start, sec - 1))
                    start = None

        if start is not None:
            regions.append((start, self.total_seconds - 1))

        self.data_regions = regions

    def get_word(self, second, word_index):
        """Get a single 12-bit word value. Returns None if no data.

        Args:
            second: Which second of recording (0-indexed).
            word_index: Word position within that second/subframe (0-511).
        """
        offset = second * BYTES_PER_SECOND + word_index * 2
        if offset + 2 > len(self.data):
            return None
        raw = struct.unpack_from('<H', self.data, offset)[0]
        if raw == NO_DATA:
            return None
        return raw & 0x0FFF

    def get_subframe_words(self, second, subframe=None):
        """Get all 512 words for a recorded second/subframe."""
        return self.get_second_words(second)

    def get_second_words(self, second):
        """Get all 512 12-bit words for a second/subframe."""
        offset = second * BYTES_PER_SECOND
        if offset + BYTES_PER_SECOND > len(self.data):
            return None
        raw = struct.unpack_from(f'<{WORDS_PER_SECOND}H', self.data, offset)
        result = []
        for w in raw:
            if w == NO_DATA:
                result.append(None)
            else:
                result.append(w & 0x0FFF)
        return result

    def sync_status(self, second):
        """Return sync status for this 512-word second/subframe."""
        offset = second * BYTES_PER_SECOND
        if offset + 2 > len(self.data):
            return {}
        raw = struct.unpack_from('<H', self.data, offset)[0] & 0x0FFF
        expected_sf = (second % SUBFRAMES_PER_CYCLE) + 1
        expected = {1: 0x247, 2: 0x5B8, 3: 0xA47, 4: 0xDB8}[expected_sf]
        return {expected_sf: raw == expected, "actual": SYNC_WORDS.get(raw, {}).get("sf")}

    def second_has_any_data(self, second):
        """Check if a second has any non-0xFFF data at all."""
        offset = second * BYTES_PER_SECOND
        if offset + BYTES_PER_SECOND > len(self.data):
            return False
        raw = struct.unpack_from(f'<{WORDS_PER_SECOND}H', self.data, offset)
        return any(w != NO_DATA for w in raw)

    def summary(self):
        """Print a comprehensive summary of the UPK file."""
        print("=" * 70)
        print("UPK FDR Data File Summary")
        print("=" * 70)
        print(f"  Source:            Public NTSB recorder data release for a specific event")
        print(f"  Recorder:          Honeywell HFR5-D SSFDR")
        print(f"  Format:            ARINC 573/717, 512 words/sec")
        print(f"  File size:         {len(self.data):,} bytes ({len(self.data)/1024/1024:.2f} MB)")
        print(f"  Total seconds:     {self.total_seconds} ({self.total_seconds/60:.1f} min)")
        print(f"  Words/second:      {WORDS_PER_SECOND}")
        print(f"  Subframe cycle:    {SUBFRAMES_PER_CYCLE} x {WORDS_PER_SUBFRAME} words")
        print(f"  Sync words:        SF1=0x247 SF2=0x5B8 SF3=0xA47 SF4=0xDB8")
        print(f"  No-data marker:    0x0FFF")
        print()

        total_sfs = len(self.subframes)
        sf_by_type = defaultdict(int)
        for sf in self.subframes:
            if sf.sf_type:
                sf_by_type[sf.sf_type] += 1
        sf_no_type = total_sfs - sum(sf_by_type.values())

        print(f"  Total subframes:   {total_sfs}")
        print(f"  Subframes by type: SF1={sf_by_type[1]}, SF2={sf_by_type[2]}, "
              f"SF3={sf_by_type[3]}, SF4={sf_by_type[4]}")
        if sf_no_type > 0:
            print(f"  Subframes w/o sync:{sf_no_type} (data present but no sync word)")
        print()

        print(f"  Data regions:      {len(self.data_regions)}")
        for i, (start, end) in enumerate(self.data_regions):
            duration = end - start + 1
            sfs_in_region = sum(1 for sf in self.subframes if start <= sf.second <= end)
            print(f"    Region {i+1}: seconds {start}-{end} ({duration}s = {duration/60:.1f} min, "
                  f"{sfs_in_region} populated 512-word seconds)")

        sync_ok_count = 0
        sync_bad_examples = []
        for sec in range(start, end + 1):
            status = self.sync_status(sec)
            expected_sf = (sec % SUBFRAMES_PER_CYCLE) + 1
            if status.get(expected_sf):
                sync_ok_count += 1
            else:
                sync_bad_examples.append(sec)

        print()
        print(f"  Sync quality within data region:")
        print(f"    Expected sync OK: {sync_ok_count} seconds")
        print(f"    Missing/bad sync: {len(sync_bad_examples)} seconds")
        if sync_bad_examples:
            print(f"    Missing/bad sync details:")
            for sec in sync_bad_examples[:20]:
                density = self.get_second_data_density(sec)
                print(f"      Sec {sec}: data={density[0]}/{density[1]} words")
        else:
            print(f"    (no bad-sync seconds)")

        # Show chip dropout pattern
        print(f"\n  First seconds in data region:")
        for sec in range(start, min(start + 8, end + 1)):
            density = self.get_second_data_density(sec)
            status = self.sync_status(sec)
            print(f"    Sec {sec}: {density[0]}/{density[1]} words, sync={status}")

    def get_second_data_density(self, second):
        """Return (non_empty, total) word counts for a second."""
        words = self.get_second_words(second)
        if words is None:
            return (0, 0)
        non_empty = sum(1 for w in words if w is not None)
        return (non_empty, len(words))

    def export_csv(self, output_path, param_indices, start_second=0, end_second=None):
        """Export selected parameters to CSV.

        Args:
            output_path: Path to output CSV file.
            param_indices: Dict of {column_name: word_index} mappings.
            start_second: Starting second index (default 0).
            end_second: Ending second index (default last with data).
        """
        if end_second is None:
            end_second = self.total_seconds - 1

        names = list(param_indices.keys())
        indices = list(param_indices.values())

        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Second', 'Subframe', 'WordOffset'] + names)

            for sec in range(start_second, end_second + 1):
                row = [sec, (sec % SUBFRAMES_PER_CYCLE) + 1, "0x0000"]
                all_none = True
                for idx in indices:
                    val = self.get_word(sec, idx)
                    row.append(val if val is not None else '')
                    if val is not None:
                        all_none = False
                if not all_none:
                    writer.writerow(row)

    def load_param_map_from_csv(self, csv_path):
        """Load parameter definitions from a NTSB Scratch CSV file."""
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            lines = list(reader)

        header_idx = unit_idx = discrete_idx = None
        for i, row in enumerate(lines):
            if row and row[0].strip() == 'DATA':
                header_idx = i + 1
                unit_idx = i + 2
                discrete_idx = i + 3
                break

        if header_idx is None:
            return None

        headers = lines[header_idx]
        units = lines[unit_idx] if unit_idx < len(lines) else []
        discretes = lines[discrete_idx] if discrete_idx < len(lines) else []

        params = []
        for i, name in enumerate(headers):
            unit = units[i] if i < len(units) else ''
            discrete = discretes[i] if i < len(discretes) else ''
            params.append({
                'index': i,
                'name': name.strip(),
                'unit': unit.strip(),
                'discrete': discrete.strip()
            })

        self.param_map = {
            'headers': headers,
            'params': params,
            'data_rows': [row for row in lines[discrete_idx+1:] if row and row[0].strip()]
        }
        return self.param_map

    def load_appendix_b(self, csv_path):
        """Load NTSB Appendix B: Validated Parameters list.

        This is the authoritative list of 153 parameters from the public NTSB
        report for this event. Each parameter has a name, unit, and
        description. Note: the sequential ID (1-153) is NOT the ARINC word
        position. The word-to-parameter mapping requires the Boeing 737-800
        Data Frame Layout (DFL) document.
        """
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            lines = list(reader)

        self.validated_params = []
        for row in lines:
            if len(row) >= 2 and row[0].strip().isdigit():
                self.validated_params.append({
                    'appendix_id': int(row[0].strip()),
                    'name': row[1].strip(),
                    'unit': row[2].strip() if len(row) > 2 else '',
                    'description': row[3].strip() if len(row) > 3 else '',
                })

        self.param_by_name = {p['name']: p for p in self.validated_params}
        return self.validated_params

    def load_dfl_map(self, csv_path):
        """Load the inferred word-to-parameter mapping (DFL map).

        This mapping was reverse-engineered from ExactSample.csv fractional
        timestamps. Word positions are verified correct. Slope/offset values
        are best-effort from linear regression and may be unreliable for
        parameters that barely change in the recorded data window.

        The mapping file (dfl_map.csv) contains one row per parameter with:
        ParamName, Type, WordPositions, Slope, Offset, PearsonR, RMSE, Unit
        """
        self.dfl = {}
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                pname = row['ParamName'].strip()
                words_str = row['WordPositions'].strip()
                words = [int(w) for w in words_str.split(',') if w.strip()]
                ptype = row['Type'].strip()

                entry = {
                    'name': pname,
                    'type': ptype,
                    'words': words,
                }

                if ptype == 'numeric':
                    entry['slope'] = float(row['Slope']) if row['Slope'].strip() else 0.0
                    entry['offset'] = float(row['Offset']) if row['Offset'].strip() else 0.0
                    entry['r'] = float(row['PearsonR']) if row['PearsonR'].strip() else 0.0
                    entry['rmse'] = float(row['RMSE']) if row['RMSE'].strip() else 0.0
                    entry['unit'] = row.get('Unit', '').strip()
                else:
                    entry['unit'] = row.get('Unit', '').strip()

                self.dfl[pname] = entry

        return self.dfl

    def load_core_locations(self, md_path='accident_core_final_locations.md'):
        """Load the current 49-variable core accident location table.

        The table is intentionally separate from the old DFL map.  It stores
        the most likely UPK word/bit locations only; engineering-unit decoding
        still requires per-parameter calibration or the real DFL rules.
        """
        locations = {}
        with open(md_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('|') or line.startswith('|---') or 'Parameter' in line:
                    continue
                cells = [c.strip() for c in line.strip('|').split('|')]
                if len(cells) < 8 or not cells[0].isdigit():
                    continue
                words = [int(m.group(1)) for m in re.finditer(r'(\d+):SF\d+\+\d+', cells[3])]
                bit_match = re.search(r':bit(\d+)', cells[3])
                if bit_match is None:
                    bit_match = re.search(r'bit\s+(\d+)', cells[3])
                locations[cells[1]] = {
                    'id': int(cells[0]),
                    'name': cells[1],
                    'unit': cells[2],
                    'words': words,
                    'bit': int(bit_match.group(1)) if bit_match else None,
                    'inverted': 'inverted' in cells[3].lower(),
                    'shift': int(cells[4]) if cells[4].strip().isdigit() else None,
                    'confidence': cells[5],
                    'basis': cells[6],
                    'check': cells[7],
                    'position_text': cells[3],
                }
        self.core_locations = locations
        return self.core_locations

    def list_core_params(self):
        """List parameters loaded by load_core_locations()."""
        return sorted(self.core_locations)

    def get_core_location(self, param_name):
        """Return the loaded core-location entry for a parameter."""
        return self.core_locations.get(param_name)

    def iter_core_raw_samples(self, param_name, start_second=None, end_second=None):
        """Yield raw samples for a core parameter from its final location.

        Yields dicts with second, word, time_offset, raw, and value.  For bit
        parameters value is 0/1 after inversion.  For numeric parameters value
        is the unsigned 12-bit raw word; callers can apply calibration.
        """
        entry = self.get_core_location(param_name)
        if entry is None:
            raise KeyError("Core parameter not loaded: {}".format(param_name))
        if start_second is None:
            start_second = self.data_regions[0][0] if self.data_regions else 0
        if end_second is None:
            end_second = self.data_regions[-1][1] if self.data_regions else self.total_seconds - 1

        for sec in range(start_second, end_second + 1):
            for word in entry['words']:
                raw = self.get_word(sec, word)
                if raw is None:
                    continue
                if entry['bit'] is None:
                    value = raw
                else:
                    value = (raw >> entry['bit']) & 1
                    if entry['inverted']:
                        value = 1 - value
                yield {
                    'second': sec,
                    'word': word,
                    'time_offset': sec + word / WORDS_PER_SECOND,
                    'raw': raw,
                    'value': value,
                }

    def _read_exact_sample_column(self, csv_path, param_name):
        with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
        data_idx = None
        for i, row in enumerate(rows):
            if row and row[0].strip() == 'DATA':
                data_idx = i
                break
        if data_idx is None:
            raise RuntimeError("DATA marker not found in {}".format(csv_path))

        headers = [h.strip() for h in rows[data_idx + 1]]
        discretes = [d.strip() for d in rows[data_idx + 3]]
        if param_name not in headers:
            raise KeyError("Parameter not found in ExactSample: {}".format(param_name))
        col = headers.index(param_name)
        discrete_map = {}
        if col < len(discretes) and discretes[col] and discretes[col] != 'NUMBER':
            for num, label in re.findall(r'([-+]?\d+(?:\.\d+)?)(?=:[^=]*="):[^=]*="([^"]+)"', discretes[col]):
                discrete_map[label.strip().lower()] = float(num)

        samples = []
        for row in rows[data_idx + 4:]:
            if not row or col >= len(row):
                continue
            try:
                t = float(row[0].strip())
            except ValueError:
                continue
            text = row[col].strip()
            if not text:
                continue
            try:
                value = float(text)
            except ValueError:
                value = discrete_map.get(text.lower())
            if value is not None:
                samples.append((t, value))
        return samples

    def fit_core_to_exact_sample(self, param_name, csv_path='ExactSample.csv', signed=None):
        """Fit a temporary linear calibration from core raw values to ExactSample.

        This is for plotting and sanity checks only.  It is not a replacement
        for the aircraft DFL conversion rules.
        """
        entry = self.get_core_location(param_name)
        if entry is None:
            raise KeyError("Core parameter not loaded: {}".format(param_name))
        if entry['bit'] is not None:
            return {'type': 'bit', 'slope': 1.0, 'offset': 0.0, 'n': 0, 'r': None, 'rmse': None}
        if entry['shift'] is None:
            return None

        exact = self._read_exact_sample_column(csv_path, param_name)
        raw_vals = []
        eng_vals = []
        for t, value in exact:
            rel = t - BASE_UTC
            csv_sec = math.floor(rel)
            sec = csv_sec - entry['shift']
            if sec < 0 or sec >= self.total_seconds:
                continue
            if entry['basis'] == 'all-word-recheck':
                candidate_words = entry['words']
            else:
                frac = rel - csv_sec
                scheduled_word = int(round(frac * WORDS_PER_SECOND)) % WORDS_PER_SECOND
                candidate_words = [scheduled_word] if scheduled_word in entry['words'] else []
            for word in candidate_words:
                raw = self.get_word(sec, word)
                if raw is None:
                    continue
                raw_vals.append(raw)
                eng_vals.append(value)

        if len(raw_vals) < 3:
            return None

        def fit_one(values):
            n = len(values)
            sx = sum(values)
            sy = sum(eng_vals)
            sxx = sum(x * x for x in values)
            syy = sum(y * y for y in eng_vals)
            sxy = sum(x * y for x, y in zip(values, eng_vals))
            denom = n * sxx - sx * sx
            ydenom = n * syy - sy * sy
            if denom == 0 or ydenom == 0:
                return None
            slope = (n * sxy - sx * sy) / denom
            offset = (sy - slope * sx) / n
            pred = [slope * x + offset for x in values]
            rmse = math.sqrt(sum((p - y) ** 2 for p, y in zip(pred, eng_vals)) / n)
            r = (n * sxy - sx * sy) / math.sqrt(denom * ydenom)
            return {'slope': slope, 'offset': offset, 'r': r, 'rmse': rmse, 'n': n}

        unsigned_fit = fit_one(raw_vals)
        signed_vals = [x - 4096 if x >= 2048 else x for x in raw_vals]
        signed_fit = fit_one(signed_vals)
        if signed is True:
            best = signed_fit
            interp = 'signed12'
        elif signed is False:
            best = unsigned_fit
            interp = 'unsigned12'
        else:
            choices = [('unsigned12', unsigned_fit), ('signed12', signed_fit)]
            choices = [(name, item) for name, item in choices if item is not None]
            if not choices:
                return None
            interp, best = sorted(choices, key=lambda x: (abs(x[1]['r']), -x[1]['rmse']), reverse=True)[0]
        best = dict(best)
        best['type'] = 'linear'
        best['interpretation'] = interp
        return best

    def decode_core_samples(self, param_name, calibration=None, start_second=None, end_second=None):
        """Return core samples, optionally converted with a fitted calibration."""
        if calibration is None:
            calibration = self.fit_core_to_exact_sample(param_name)
        out = []
        for sample in self.iter_core_raw_samples(param_name, start_second, end_second):
            item = dict(sample)
            if calibration and calibration.get('type') == 'linear':
                raw = item['raw']
                if calibration.get('interpretation') == 'signed12' and raw >= 2048:
                    raw = raw - 4096
                item['decoded'] = calibration['slope'] * raw + calibration['offset']
            elif calibration and calibration.get('type') == 'bit':
                item['decoded'] = item['value']
            else:
                item['decoded'] = item['value']
            out.append(item)
        return out

    def decode_param_at_word(self, param_name, second, word_index):
        """Decode a parameter from a specific absolute word position.

        Returns the engineering value if this word belongs to the
        parameter, or None if not.
        """
        if not hasattr(self, 'dfl') or param_name not in self.dfl:
            return None

        entry = self.dfl[param_name]
        if entry['type'] != 'numeric':
            return None
        if word_index not in entry['words']:
            return None

        raw = self.get_word(second, word_index)
        if raw is None:
            return None
        return entry['slope'] * raw + entry['offset']

    def list_dfl_params(self):
        """List all parameters available in the DFL map."""
        if not hasattr(self, 'dfl'):
            return []
        return sorted(self.dfl.keys())

    def export_decoded_csv(self, output_path, param_names=None, start_second=0,
                           end_second=None):
        """Export decoded values at native per-sample resolution.

        One row per individual word position that carries any requested
        parameter. Columns: Second, Word, TimeOffset, Param1, Param2, ...

        This preserves the native sampling rate of each parameter
        (1 Hz, 4 Hz, 8 Hz, 16 Hz). No filtering or downsampling is
        applied; callers should post-process as needed.

        Args:
            output_path: Path to output CSV file.
            param_names: List of parameter names, or None for all DFL params.
            start_second: First second.
            end_second: Last second.
        """
        if end_second is None:
            end_second = self.data_regions[-1][1] if self.data_regions else self.total_seconds - 1

        if param_names is None:
            param_names = self.list_dfl_params()

        all_words = set()
        for pname in param_names:
            if pname in self.dfl:
                all_words.update(self.dfl[pname]['words'])

        header = ['Second', 'Word', 'TimeOffset']
        header += param_names
        rows = []

        for sec in range(start_second, end_second + 1):
            for w in sorted(all_words):
                row = [sec, w, round(w / WORDS_PER_SECOND, 6)]
                has_data = False
                for pname in param_names:
                    val = self.decode_param_at_word(pname, sec, w)
                    if val is not None:
                        row.append(round(val, 6))
                        has_data = True
                    else:
                        row.append('')
                if has_data:
                    rows.append(row)

        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        print("Exported {} rows to {}".format(len(rows), output_path))

    def correlate_word_with_exact_sample(self, csv_path, word_index, max_rows=500):
        """Attempt to correlate a raw word position with ExactSample data.

        Loads the ExactSample.csv and for a given word position in the
        .upk file, shows the raw values alongside the closest engineering
        values to help manually identify word-to-parameter mappings.

        THIS IS AN EXPERIMENTAL TOOL. Accurate mapping requires the
        Boeing DFL document.
        """
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            lines = list(reader)

        data_start = None
        for i, row in enumerate(lines):
            if row and row[0].strip() == 'DATA':
                data_start = i
                break

        if data_start is None:
            return

        headers = lines[data_start + 1]
        data_rows = [r for r in lines[data_start + 4:] if r and r[0].strip()]

        print(f"\n{'='*70}")
        print(f"Correlation attempt for Word {word_index} (W{word_index})")
        print(f"{'='*70}")

        raw_vals = []
        for sec in range(self.total_seconds):
            v = self.get_word(sec, word_index)
            if v is not None:
                raw_vals.append((sec, v))

        print(f"  Raw data: {len(raw_vals)} non-empty values across {self.total_seconds} seconds")
        if raw_vals:
            mn, mx = min(v[1] for v in raw_vals), max(v[1] for v in raw_vals)
            avg = sum(v[1] for v in raw_vals) / len(raw_vals)
            print(f"  Range: {mn} - {mx} (avg={avg:.1f})")

        return raw_vals

    def dump_subframe(self, second, subframe=None, max_words=40):
        """Pretty-print a 512-word second/subframe's contents."""
        words = self.get_subframe_words(second, subframe)
        sync = words[0] if (words and words[0] is not None) else None
        sync_name = SYNC_WORDS[sync]['name'] if sync in SYNC_WORDS else ('?NO SYNC?' if sync else 'EMPTY')

        print(f"\n--- Second {second} (sync={sync_name}, words 0-{len(words)-1}) ---")
        non_empty = sum(1 for w in words if w is not None)
        print(f"  Non-empty words: {non_empty}/{len(words)}")

        print(f"  First {max_words} words:")
        for i in range(min(max_words, len(words))):
            if words[i] is not None:
                print(f"    W[{i:3d}]: 0x{words[i]:03X} = {words[i]:4d}")


if __name__ == '__main__':
    reader = UPKReader(str(ROOT / '12minute.upk'))
    reader.summary()

    reader.load_appendix_b(str(ROOT / 'parameters.csv'))
    reader.load_core_locations(str(ROOT / 'accident_core_final_locations.md'))

    params = reader.list_core_params()
    print()
    print("=" * 70)
    print("Core accident map: {} parameters loaded".format(len(params)))
    print("=" * 70)
    for name in ["Altitude Press", "Airspeed Comp", "Pitch Angle", "Roll Angle", "Eng1 N1", "Eng2 N1"]:
        entry = reader.get_core_location(name)
        if not entry:
            continue
        fit = reader.fit_core_to_exact_sample(name, str(ROOT / 'ExactSample.csv'))
        print("  {}: {} | basis={} | shift={} | fit={}".format(
            name, entry['position_text'], entry['basis'], entry['shift'], fit))
