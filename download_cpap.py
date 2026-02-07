#!/usr/bin/env python3
"""
download_cpap.py - Download CPAP data from ez Share WiFi SD card (firmware 4.4.0)

Works around firmware 4.4.0's broken directory listing by:
1. Downloading STR.edf (which always works)
2. Parsing MaskOn/MaskOff timestamps to discover session start times
3. Constructing DATALOG filenames from those timestamps
4. Probing each candidate file via HEAD request to find the exact seconds
5. Downloading confirmed files

ResMed file naming:
- Directory: DATALOG/<record_date>/ where record_date uses noon-split
  (sessions before noon belong to the previous calendar day)
- Filename: <calendar_date>_<HHMMSS>_<TYPE>.edf where calendar_date is the
  actual date/time the file was created
- STR.edf only gives minute precision, so seconds must be found by probing

Usage:
    python3 download_cpap.py [--output-dir DIR] [--days N] [--card-ip IP]
"""

import argparse
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    print("Error: 'requests' library required. Install with: pip install requests")
    sys.exit(1)


# --- Configuration ---

DEFAULT_CARD_IP = "192.168.4.1"
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/CPAP_Data")
DEFAULT_DAYS = 7
CONFIRMED_FIRMWARE = "4.4.0"

# EDF file types in DATALOG directories
# EVE/CSL are written first (~8-9s before BRP/PLD/SAD)
DATALOG_TYPES_EARLY = ["EVE", "CSL"]
DATALOG_TYPES_LATE = ["BRP", "PLD", "SAD"]
DATALOG_ALL_TYPES = DATALOG_TYPES_EARLY + DATALOG_TYPES_LATE

ROOT_FILES = [
    "STR.edf",
    "STR.crc",
    "Identification.tgt",
    "Identification.crc",
    "journal.dat",
    "journal.jnl",
]

SETTINGS_FILES = [
    "sig.dat",
    "set.crc",
]


# --- EDF Parsing ---


def parse_str_edf(filepath):
    """Parse STR.edf to extract session start times.

    ResMed's STR.edf has one record per day starting at noon. MaskOn/MaskOff
    values are minutes since noon of the record date. Sessions typically start
    in the evening/night, so the actual calendar date is often record_date + 1.

    Returns list of dicts:
        - record_date: str 'YYYYMMDD' (the noon-epoch day -- used for directory name)
        - start_time: datetime (actual session start -- used for filename date/time)
        - duration_min: int
    """
    with open(filepath, "rb") as f:
        header = f.read(256)
        num_records = int(header[236:244].decode().strip())
        num_signals = int(header[252:256].decode().strip())
        header_bytes = int(header[184:192].decode().strip())

        date_str = header[168:176].decode().strip()
        time_str = header[176:184].decode().strip()
        dd, mm, yy = date_str.split(".")
        hh, mi, ss = time_str.split(".")
        year = 2000 + int(yy) if int(yy) < 85 else 1900 + int(yy)
        start_dt = datetime(year, int(mm), int(dd), int(hh), int(mi), int(ss))

        spr_offset = 256 + num_signals * (16 + 80 + 8 + 8 + 8 + 8 + 8 + 80)
        f.seek(spr_offset)
        samples_per_record = [
            int(f.read(8).decode().strip()) for _ in range(num_signals)
        ]
        total_samples = sum(samples_per_record)

        f.seek(256)
        labels = [f.read(16).decode().strip() for _ in range(num_signals)]

        maskon_idx = None
        maskoff_idx = None
        for i, label in enumerate(labels):
            if label == "MaskOn":
                maskon_idx = i
            elif label == "MaskOff":
                maskoff_idx = i

        if maskon_idx is None or maskoff_idx is None:
            raise ValueError("Could not find MaskOn/MaskOff signals in STR.edf")

        maskon_sample_offset = sum(samples_per_record[:maskon_idx])
        maskoff_sample_offset = sum(samples_per_record[:maskoff_idx])
        maskon_nsamples = samples_per_record[maskon_idx]
        maskoff_nsamples = samples_per_record[maskoff_idx]

        sessions = []

        for rec_idx in range(num_records):
            rec_start = header_bytes + rec_idx * total_samples * 2
            record_date_dt = start_dt + timedelta(days=rec_idx)
            noon = record_date_dt.replace(hour=12, minute=0, second=0)

            f.seek(rec_start + maskon_sample_offset * 2)
            maskon_vals = struct.unpack(
                "<" + "h" * maskon_nsamples,
                f.read(maskon_nsamples * 2),
            )

            f.seek(rec_start + maskoff_sample_offset * 2)
            maskoff_vals = struct.unpack(
                "<" + "h" * maskoff_nsamples,
                f.read(maskoff_nsamples * 2),
            )

            for i in range(min(maskon_nsamples, maskoff_nsamples)):
                on = maskon_vals[i]
                off = maskoff_vals[i]

                if on <= 0 or off <= 0:
                    continue
                if on >= 1440 or off >= 1440:
                    continue

                duration = off - on
                if duration <= 0:
                    continue

                session_start = noon + timedelta(minutes=on)

                sessions.append(
                    {
                        "record_date": record_date_dt.strftime("%Y%m%d"),
                        "start_time": session_start,
                        "duration_min": duration,
                    }
                )

    return sessions


# --- Card Communication ---


class EzShareCard:
    """Interface to ez Share WiFi SD card (firmware 4.4.0 compatible)."""

    def __init__(self, ip=DEFAULT_CARD_IP, timeout=10):
        self.base_url = f"http://{ip}"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
            }
        )

    def ping(self):
        """Check if the card is reachable and return its firmware version.

        Parses the XML response from the version endpoint, which looks like:
            <response><device><version>
            LZ1001EDPG:4.4.0:2014-07-28:62 LZ1001EDRS:4.4.0:2014-07-28:62 ...
            </version></device></response>

        Returns the first version string found (e.g. "4.4.0"), or None if
        the card is unreachable or the response is unrecognized.
        """
        try:
            r = self.session.get(
                f"{self.base_url}/client?command=version",
                timeout=2,
            )
            if r.status_code != 200:
                return None
            root = ET.fromstring(r.text)
            version_el = root.find("device/version")
            if version_el is None or not version_el.text:
                return None
            match = re.search(r":(\d+\.\d+\.\d+):", version_el.text)
            if match:
                return match.group(1)
            return None
        except (requests.RequestException, ET.ParseError):
            return None

    def is_real_file(self, path):
        """Probe whether a file exists on the card via HEAD request.

        Real files return Content-Type: text/plain with Content-Length.
        Non-existent files return Content-Type: text/html with chunked encoding.
        Both return HTTP 200.

        HEAD responses have quirky Content-Disposition (e.g. "10000" instead of
        "attachment"), but Content-Type is reliable for distinguishing real vs fake.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            r = self.session.head(url, timeout=self.timeout, allow_redirects=False)
            ct = r.headers.get("Content-Type", "")
            if "text/html" in ct:
                return False, 0
            content_length = int(r.headers.get("Content-Length", 0))
            return True, content_length
        except requests.RequestException:
            return False, 0

    def is_real_directory(self, path):
        exists, size = self.is_real_file(path.rstrip("/"))
        return exists and size == 0

    def download_file(self, remote_path, local_path, expected_size=None):
        url = f"{self.base_url}/{remote_path.lstrip('/')}"
        try:
            r = self.session.get(url, timeout=self.timeout, stream=True)

            ct = r.headers.get("Content-Type", "")
            if "text/html" in ct:
                return False

            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

            actual_size = os.path.getsize(local_path)
            if expected_size and actual_size != expected_size:
                print(
                    f"  WARNING: Size mismatch for {remote_path}: "
                    f"expected {expected_size}, got {actual_size}"
                )

            return True

        except requests.RequestException as e:
            print(f"  ERROR downloading {remote_path}: {e}")
            return False


# --- DATALOG Probing ---


def find_seconds_for_type(card, dir_date, file_date, hhmm, ftype):
    """Brute-force scan seconds 0-59 to find a DATALOG file.

    Returns (seconds_str, size) or (None, 0).
    """
    for ss in range(60):
        path = f"DATALOG/{dir_date}/{file_date}_{hhmm}{ss:02d}_{ftype}.edf"
        exists, size = card.is_real_file(path)
        if exists and size > 0:
            return f"{ss:02d}", size
    return None, 0


def find_seconds_near(card, dir_date, file_date, hhmm, ftype, reference_ss, window=15):
    """Search for a file's seconds value near a known reference.

    Checks reference_ss first, then expands outward within +/-window.
    """
    ref = int(reference_ss)
    for offset in range(window + 1):
        candidates = [ref + offset] if offset == 0 else [ref + offset, ref - offset]
        for candidate in candidates:
            if candidate < 0 or candidate >= 60:
                continue
            ss_str = f"{candidate:02d}"
            path = f"DATALOG/{dir_date}/{file_date}_{hhmm}{ss_str}_{ftype}.edf"
            exists, size = card.is_real_file(path)
            if exists and size > 0:
                return ss_str, size
    return None, 0


# --- Main Download Logic ---


def download_root_files(card, output_dir):
    print("\n=== Downloading root files ===")
    downloaded = 0
    skipped = 0

    for fname in ROOT_FILES:
        local_path = os.path.join(output_dir, fname)

        exists, remote_size = card.is_real_file(fname)
        if not exists:
            continue

        if os.path.exists(local_path) and os.path.getsize(local_path) == remote_size:
            print(f"  SKIP {fname} (same size: {remote_size})")
            skipped += 1
            continue

        print(f"  GET  {fname} ({remote_size} bytes)...", end=" ", flush=True)
        if card.download_file(fname, local_path, expected_size=remote_size):
            print("OK")
            downloaded += 1
        else:
            print("FAILED")

    print(f"  Root files: {downloaded} downloaded, {skipped} skipped")
    return downloaded


def download_settings(card, output_dir):
    print("\n=== Downloading SETTINGS ===")
    downloaded = 0
    skipped = 0

    settings_dir = os.path.join(output_dir, "SETTINGS")

    for fname in SETTINGS_FILES:
        remote_path = f"SETTINGS/{fname}"
        local_path = os.path.join(settings_dir, fname)

        exists, remote_size = card.is_real_file(remote_path)
        if not exists:
            continue

        if os.path.exists(local_path) and os.path.getsize(local_path) == remote_size:
            print(f"  SKIP SETTINGS/{fname} (same size: {remote_size})")
            skipped += 1
            continue

        print(f"  GET  SETTINGS/{fname} ({remote_size} bytes)...", end=" ", flush=True)
        if card.download_file(remote_path, local_path, expected_size=remote_size):
            print("OK")
            downloaded += 1
        else:
            print("FAILED")

    print(f"  Settings: {downloaded} downloaded, {skipped} skipped")
    return downloaded


def download_datalog(card, sessions, output_dir, days_back=None):
    """Download DATALOG session files.

    For each session from STR.edf:
    - Directory = record_date (noon-split epoch day)
    - Filename date = actual calendar date of session start
    - Filename time = HHMM from STR.edf + brute-forced seconds (0-59)

    Strategy: find BRP seconds first (full 0-59 scan), then search +/-15s
    for other types. EVE/CSL are typically ~8-9s earlier than BRP/PLD/SAD.
    """
    print("\n=== Downloading DATALOG files ===")

    cutoff = None
    if days_back is not None:
        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")

    filtered = []
    for s in sessions:
        if cutoff and s["record_date"] < cutoff:
            continue
        filtered.append(s)

    if not filtered:
        print("  No sessions found in the requested date range.")
        return 0

    by_dir = {}
    for s in filtered:
        d = s["record_date"]
        if d not in by_dir:
            by_dir[d] = []
        by_dir[d].append(s)

    dates = sorted(by_dir.keys())
    print(f"  Date range: {dates[0]} to {dates[-1]} ({len(dates)} record days)")

    total_downloaded = 0
    total_skipped = 0
    total_probed = 0
    total_not_found = 0

    for dir_date in dates:
        if not card.is_real_directory(f"DATALOG/{dir_date}"):
            total_probed += 1
            continue

        dir_sessions = by_dir[dir_date]
        date_downloaded = 0
        print(f"\n  [{dir_date}] {len(dir_sessions)} session(s)")

        for s in dir_sessions:
            start = s["start_time"]
            file_date = start.strftime("%Y%m%d")
            hhmm = start.strftime("%H%M")

            print(
                f"    Session {file_date} {hhmm} ({s['duration_min']} min):", flush=True
            )

            print("      Scanning BRP seconds...", end=" ", flush=True)
            brp_ss, brp_size = find_seconds_for_type(
                card, dir_date, file_date, hhmm, "BRP"
            )
            total_probed += 60 if brp_ss is None else (int(brp_ss) + 1)

            if brp_ss is None:
                print("not found, skipping session")
                total_not_found += len(DATALOG_ALL_TYPES)
                continue

            print(f"found SS={brp_ss}")

            files_to_download = [("BRP", brp_ss, brp_size)]

            for ftype in DATALOG_TYPES_LATE:
                if ftype == "BRP":
                    continue
                ss, size = find_seconds_near(
                    card, dir_date, file_date, hhmm, ftype, brp_ss
                )
                total_probed += 1
                if ss is not None:
                    files_to_download.append((ftype, ss, size))

            for ftype in DATALOG_TYPES_EARLY:
                ss, size = find_seconds_near(
                    card, dir_date, file_date, hhmm, ftype, brp_ss
                )
                total_probed += 1
                if ss is not None:
                    files_to_download.append((ftype, ss, size))

            for ftype, ss, size in files_to_download:
                basename = f"{file_date}_{hhmm}{ss}_{ftype}"
                remote_edf = f"DATALOG/{dir_date}/{basename}.edf"
                remote_crc = f"DATALOG/{dir_date}/{basename}.crc"
                local_edf = os.path.join(output_dir, remote_edf)
                local_crc = os.path.join(output_dir, remote_crc)

                if os.path.exists(local_edf) and os.path.getsize(local_edf) == size:
                    print(f"      SKIP {ftype} (same size: {size})")
                    total_skipped += 1
                else:
                    print(
                        f"      GET  {ftype} ({size:,} bytes)...", end=" ", flush=True
                    )
                    if card.download_file(remote_edf, local_edf, expected_size=size):
                        print("OK")
                        total_downloaded += 1
                        date_downloaded += 1
                    else:
                        print("FAILED")

                crc_exists, crc_size = card.is_real_file(remote_crc)
                if crc_exists and crc_size > 0:
                    if not (
                        os.path.exists(local_crc)
                        and os.path.getsize(local_crc) == crc_size
                    ):
                        card.download_file(
                            remote_crc, local_crc, expected_size=crc_size
                        )

        if date_downloaded > 0:
            print(f"    -> {date_downloaded} files downloaded")

    print(
        f"\n  DATALOG totals: {total_downloaded} downloaded, "
        f"{total_skipped} skipped, {total_not_found} not found, "
        f"~{total_probed} probes sent"
    )
    return total_downloaded


# --- Entry Point ---


def main():
    parser = argparse.ArgumentParser(
        description="Download CPAP data from ez Share WiFi SD card",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Sync last 7 days to ~/CPAP_Data
  %(prog)s --days 30                # Sync last 30 days
  %(prog)s --days 0                 # Sync ALL available data
  %(prog)s --output-dir /mnt/cpap   # Custom output directory
  %(prog)s --str-only               # Only download STR.edf (fast summary)
        """,
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Local directory to save files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Number of days back to sync (0 = all, default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--card-ip",
        default=DEFAULT_CARD_IP,
        help=f"Card IP address (default: {DEFAULT_CARD_IP})",
    )
    parser.add_argument(
        "--str-only",
        action="store_true",
        help="Only download STR.edf (for quick summary data)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="HTTP request timeout in seconds (default: 10)",
    )
    args = parser.parse_args()

    output_dir = os.path.expanduser(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    card = EzShareCard(ip=args.card_ip, timeout=args.timeout)

    print(f"Connecting to card at {args.card_ip}...")
    version = card.ping()
    if version is None:
        print(
            f"ERROR: Cannot reach card at {args.card_ip}. "
            "Is the card accessible on this network?"
        )
        sys.exit(1)
    print(f"Card detected! Firmware version: {version}")
    if version != CONFIRMED_FIRMWARE:
        print(
            f"  WARNING: This script is confirmed to work with firmware {CONFIRMED_FIRMWARE}. "
            f"Detected {version} — behavior may differ."
        )

    download_root_files(card, output_dir)

    if args.str_only:
        print("\n--str-only mode: skipping DATALOG and SETTINGS.")
        print("Done.")
        return

    download_settings(card, output_dir)

    str_path = os.path.join(output_dir, "STR.edf")
    if not os.path.exists(str_path):
        print("ERROR: STR.edf not available. Cannot enumerate DATALOG files.")
        sys.exit(1)

    print("\nParsing STR.edf for session timestamps...")
    sessions = parse_str_edf(str_path)
    print(f"  Found {len(sessions)} therapy sessions")

    days_back = args.days if args.days > 0 else None
    download_datalog(card, sessions, output_dir, days_back=days_back)

    print("\nDone.")


if __name__ == "__main__":
    main()
