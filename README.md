# ezShare_CPAP

A downloader for ResMed AirSense 10 CPAP data from an ez Share WiFi SD card running
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
--timeout SECONDS      HTTP request timeout (default: 10)
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

ResMed AirSense 10 machines write therapy data to the SD card using a specific
file naming convention:

- **Root files** — `STR.edf`, `STR.crc`, `Identification.tgt`, etc.
- **Settings** — `SETTINGS/sig.dat`, `SETTINGS/set.crc`
- **DATALOG directories** — `DATALOG/<record_date>/` where `record_date` uses
  a noon-split (sessions before noon belong to the previous calendar day)
- **DATALOG files** — `<calendar_date>_<HHMMSS>_<TYPE>.edf` where the types are
  `EVE`, `CSL`, `BRP`, `PLD`, and `SAD`

Since `STR.edf` only provides minute-level precision for session start times,
the tool brute-force probes seconds 0–59 for the `BRP` file, then searches nearby
(±15s) for the remaining types.

## License

[MIT](LICENSE)
