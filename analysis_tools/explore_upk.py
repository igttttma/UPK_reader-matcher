import csv
import sys
from pathlib import Path
from upk_reader import UPKReader, WORDS_PER_SECOND, WORDS_PER_SUBFRAME, SYNC_WORDS

ROOT = Path(__file__).resolve().parent.parent


def compare_region_ends(reader):
    """Compare the last second of each full-sync region with its successor."""
    for i in range(len(reader.data_regions) - 1):
        end_a = reader.data_regions[i][1]
        start_b = reader.data_regions[i + 1][0]

        words_a = reader.get_second_words(end_a)
        words_b = reader.get_second_words(start_b)

        print(f"\n=== Region boundary: sec {end_a} -> sec {start_b} ===")

        if words_a is None or words_b is None:
            print("  Cannot compare")
            continue

        diffs = []
        for w_idx in range(WORDS_PER_SECOND):
            if words_a[w_idx] != words_b[w_idx] and words_a[w_idx] is not None and words_b[w_idx] is not None:
                diffs.append((w_idx, words_a[w_idx], words_b[w_idx]))

        print(f"  Differing non-None words: {len(diffs)}")
        for w_idx, va, vb in diffs[:15]:
            sf = w_idx // WORDS_PER_SUBFRAME
            w = w_idx % WORDS_PER_SUBFRAME
            va_str = f"0x{va:03X}" if va is not None else "EMPTY"
            vb_str = f"0x{vb:03X}" if vb is not None else "EMPTY"
            print(f"    W[{w_idx:4d}] (SF{sf+1}+{w:3d}): {va_str:>7s} -> {vb_str:>7s}")


def dump_subframe_hex(reader, second, subframe, max_words=30):
    """Dump a subframe as a hex table."""
    words = reader.get_subframe_words(second, subframe)
    sync = words[0] if (words and words[0] is not None) else None
    sync_info = SYNC_WORDS.get(sync, None)
    sf_name = sync_info['name'] if sync_info else ('NO SYNC' if sync else 'EMPTY')

    print(f"\n  Subframe {subframe+1} of sec {second} (sync={sf_name}):")
    for row in range(min(5, len(words) // 16)):
        start = row * 16
        vals = []
        for w in words[start:start+16]:
            if w is not None:
                vals.append(f"0x{w:03X}")
            else:
                vals.append("  --  ")
        print(f"    W[{start:3d}-{start+15:3d}]: " + " ".join(vals))


def export_region_raw(reader, output_path, word_list):
    """Export specific word positions with raw hex values.

    Args:
        word_list: List of (word_index, label) tuples.
        label should be descriptive but clearly marked as GUESS unless
        verified against the DFL document.
    """
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        header = ['Second', 'SF', 'AbsWord']
        for _, label in word_list:
            header.append(label)
        writer.writerow(header)

        for region_start, region_end in reader.data_regions:
            for sec in range(region_start, region_end + 1):
                for sf in range(4):
                    sf_start = sf * WORDS_PER_SUBFRAME
                    row = [sec, sf + 1, sf_start]
                    any_data = False
                    for w_idx, _ in word_list:
                        abs_idx = sf_start + w_idx
                        if abs_idx >= WORDS_PER_SECOND:
                            continue
                        val = reader.get_word(sec, abs_idx)
                        row.append(f'0x{val:03X}' if val is not None else '')
                        if val is not None:
                            any_data = True
                    if any_data:
                        writer.writerow(row)
    print(f"Exported to {output_path}")


def search_value_pattern(reader, word_index, start_sec, end_sec):
    """Show raw values for a specific word across a range of seconds."""
    vals = []
    for sec in range(start_sec, end_sec + 1):
        v = reader.get_word(sec, word_index)
        if v is not None:
            vals.append((sec, v))
    print(f"\nWord {word_index} (SF{word_index//WORDS_PER_SUBFRAME+1}+{word_index%WORDS_PER_SUBFRAME}):")
    print(f"  {len(vals)} values in seconds {start_sec}-{end_sec}")
    if vals:
        print(f"  Range: {min(v[1] for v in vals)} - {max(v[1] for v in vals)}")
        for sec, v in vals[:25]:
            print(f"    Sec {sec:4d}: 0x{v:03X} = {v:4d}")
        if len(vals) > 25:
            print(f"    ... and {len(vals)-25} more")


def find_stable_words(reader, start_sec, end_sec, max_range=10):
    """Find word positions with near-constant values (likely discrete params)."""
    stable = []
    for w_idx in range(WORDS_PER_SECOND):
        vals = set()
        for sec in range(start_sec, end_sec + 1):
            v = reader.get_word(sec, w_idx)
            if v is not None:
                vals.add(v)
        if 1 <= len(vals) <= 5:
            stable.append((w_idx, sorted(vals)))

    print(f"\nWords with 1-5 unique values in seconds {start_sec}-{end_sec}:")
    print(f"  Found {len(stable)} word positions (likely discrete/binary parameters)")
    for w_idx, vals in stable[:40]:
        sf = w_idx // WORDS_PER_SUBFRAME
        w = w_idx % WORDS_PER_SUBFRAME
        vals_str = ', '.join(f'0x{v:03X}' for v in vals)
        print(f"  W[{w_idx:4d}] (SF{sf+1}+{w:3d}): {vals_str}")


def find_dramatic_changes(reader, region_a_start, region_a_end, region_c_start, region_c_end):
    """Find words with large value changes between two regions."""
    changes = []
    for w_idx in range(WORDS_PER_SECOND):
        vals_a = []
        vals_c = []
        for sec in range(region_a_start, region_a_end + 1):
            v = reader.get_word(sec, w_idx)
            if v is not None:
                vals_a.append(v)
        for sec in range(region_c_start, region_c_end + 1):
            v = reader.get_word(sec, w_idx)
            if v is not None:
                vals_c.append(v)
        if len(vals_a) >= 3 and len(vals_c) >= 3:
            avg_a = sum(vals_a) / len(vals_a)
            avg_c = sum(vals_c) / len(vals_c)
            changes.append((abs(avg_c - avg_a), w_idx, avg_a, avg_c))

    changes.sort(key=lambda x: -x[0])
    print(f"\nTop 50 word positions with largest avg change "
          f"(Region A sec {region_a_start}-{region_a_end} vs C sec {region_c_start}-{region_c_end}):")
    for i, (delta, w_idx, avg_a, avg_c) in enumerate(changes[:50]):
        sf = w_idx // WORDS_PER_SUBFRAME
        w = w_idx % WORDS_PER_SUBFRAME
        print(f"  #{i:2d}: W[{w_idx:4d}] (SF{sf+1}+{w:3d}): "
              f"avg {avg_a:7.1f} -> {avg_c:7.1f} (Δ={delta:7.1f})")


if __name__ == '__main__':
    reader = UPKReader(str(ROOT / '12minute.upk'))
    reader.summary()

    reader.load_appendix_b(str(ROOT / 'parameters.csv'))

    if not reader.data_regions:
        print("No data regions found!")
        exit(1)

    r_start, r_end = reader.data_regions[0]

    print("\n" + "=" * 70)
    print("Sync quality per second across data region (first 20 sec)")
    print("=" * 70)
    for sec in range(r_start, min(r_start + 20, r_end + 1)):
        sync = reader.sync_status(sec)
        present = [n for n, ok in sorted(sync.items()) if ok]
        missing = [n for n, ok in sorted(sync.items()) if not ok]
        density = reader.get_second_data_density(sec)
        print("  Sec {:4d}: OK={}, missing={}, data={}/{}".format(
            sec, present, missing, density[0], density[1]))

    print("\n" + "=" * 70)
    print("Discrete/binary parameter candidates (stable values across data region)")
    print("=" * 70)
    find_stable_words(reader, r_start, r_end)

    print("\n" + "=" * 70)
    print("Dramatic value changes: early (sec 140-160) vs late (sec 170-182)")
    print("=" * 70)
    find_dramatic_changes(reader, 140, 160, 170, 182)

    print("\n" + "=" * 70)
    print("Sample raw data (first second, SF1 hex)")
    print("=" * 70)
    dump_subframe_hex(reader, r_start, 0, max_words=16)

    print("\n" + "=" * 70)
    print("Chip U2 dropout: SF2 check across the data stream")
    print("=" * 70)
    for sec in [164, 165, 166, 168, 169, 170, 181, 182, 183]:
        if sec <= r_end:
            sync = reader.sync_status(sec)
            present = [n for n, ok in sorted(sync.items()) if ok]
            missing = [n for n, ok in sorted(sync.items()) if not ok]
            density = reader.get_second_data_density(sec)
            print("    Sec {}: OK={}, missing={}, data={}/{}".format(
                sec, present, missing, density[0], density[1]))

    print("\n" + "=" * 70)
    print("HOW TO COMPLETE THE READER")
    print("=" * 70)
    print("""
  To convert raw 12-bit values to engineering units, you need:
    1. Boeing 737-800 Data Frame Layout (DFL) document
       - Defines: word_position -> parameter_name
       - Defines: slope and offset for each parameter
    2. Once you have the DFL, replace the values in dfl_map.csv
    3. The Appendix B parameters.csv gives you the target parameter
       names and units.
""")
