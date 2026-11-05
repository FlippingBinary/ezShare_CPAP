# ezShare_CPAP

A downloader for ResMed AirSense 10 and 11 CPAP data from an ez Share WiFi SD card running
firmware 4.4.0.

## Why

Firmware 4.4.0 on the ez Share card no longer has a directory listing endpoint
that earlier version had, making file enumeration impossible. This tool works
around the issue by:

1. Downloading `STR.edf` (which always works via direct URL)
2. Parsing MaskOn/MaskOff timestamps to discover therapy session start times
3. Constructing DATALOG filenames from those timestamps (ResMed uses a predictable
   naming convention)
4. Probing each candidate filename via HEAD request to find the exact seconds value
5. Downloading confirmed files

## Prerequisites

- Python 3.9+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Your computer connected to the ez Share card's WiFi network

## Installation

```sh
git clone https://github.com/FlippingBinary/ezShare_CPAP.git
cd ezShare_CPAP
uv sync
```

## Usage

Connect to the ez Share card's WiFi network, then run:

```sh
uv run download-cpap
```

This syncs the last 7 days of CPAP data to `~/CPAP_Data`.

### Options

```text
--output-dir, -o DIR   Local directory to save files (default: ~/CPAP_Data)
--days, -d N           Number of days back to sync; 0 = all (default: 7)
--card-ip IP           Card IP address (default: 192.168.4.1)
--str-only             Only download STR.edf (quick summary data)
--timeout SECONDS      HTTP connect/read inactivity timeout (default: 8)
--chunk-size BYTES     Range size (default: 8192)
--retries N            Retries per range after first attempt (default: 5)
```

### Examples

```sh
uv run download-cpap                          # Sync last 7 days to ~/CPAP_Data
uv run download-cpap --days 30                # Sync last 30 days
uv run download-cpap --days 0                 # Sync ALL available data
uv run download-cpap --output-dir /mnt/cpap   # Custom output directory
uv run download-cpap --str-only               # Only download STR.edf
```

## How It Works

ResMed AirSense 10 and 11 machines write therapy data to the SD card using a specific
file naming convention:

- **Root files** — `STR.edf`, `STR.crc`, `Identification.tgt`, `Identification.json`,
  `Identification.crc`, `journal.dat`, `journal.jnl`
- **Settings** — `SETTINGS/sig.dat`, `SETTINGS/set.crc`,
  `SETTINGS/CurrentSettings.json`, `SETTINGS/CurrentSettings.crc`
- **DATALOG directories** — `DATALOG/<record_date>/` where `record_date` uses
  a noon-split (sessions before noon belong to the previous calendar day)
- **DATALOG files** — `<calendar_date>_<HHMMSS>_<TYPE>.edf` where the types are
  `EVE`, `CSL`, `BRP`, `PLD`, `SAD` (AirSense 10), and `SA2` (AirSense 11)

Since `STR.edf` only provides minute-level precision for session start times,
the tool probes the start minute plus the first 15 seconds of the next minute
for `BRP`, then searches ±15 seconds for the remaining types. Searches cross
minute and calendar-day boundaries while retaining the noon-split directory.
This covers the observed 5–15 second EVE/CSL → BRP/PLD/SA2 gap.
A HEAD timeout or connection closed without a reply counts as missing.

All downloads use HTTP Range requests: whole-file GETs can stall on firmware
4.4.0. Each chunk must return status 206, the exact Content-Range and the
requested byte count before any bytes are appended. Failed chunks are retried
up to `--retries` times, then the download reports failure and keeps its validated
prefix. Run the same command again to resume from the local file size.
The final size must match the probed size. Files already matching that size are
skipped; oversized local files are refused and must be removed to restart.

Resume and same-size skipping assume the remote file and local prefix have not
changed; there is no checksum or remote-version check. Remove a local copy to
refresh mutable metadata such as STR.edf or settings. The timeout is a requests
connect/read inactivity timeout, not a total transfer deadline.

## Tests

```sh
uv sync --group dev
uv run pytest -q
```

Tests use a mocked HTTP session and synthetic bytes; they do not contact a card.

## License

[MIT](LICENSE)
