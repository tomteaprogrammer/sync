# Sync Cache Smart

A Tkinter GUI for finding duplicate files by **content** (not by name), cleaning folders, copying/moving data, unzipping archives, and syncing files between folders.

Matching is hash-based, so renamed and moved copies are still detected as duplicates.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Modes](#modes)
- [How Matching Works](#how-matching-works)
- [Tabs Overview](#tabs-overview)
  - [1. Safe to Delete (Matches Master)](#1-safe-to-delete-matches-master)
  - [2. Only in Target (Not in Master)](#2-only-in-target-not-in-master)
  - [3. Internal Duplicates](#3-internal-duplicates)
  - [4. Copy / Move Folder](#4-copy--move-folder)
  - [5. Unzip](#5-unzip)
  - [6. Empty Folders](#6-empty-folders)
  - [7. Log](#7-log)
- [Save / Load Scan Results](#save--load-scan-results)
- [Safety Notes](#safety-notes)
- [Recommended Workflows](#recommended-workflows)
- [License](#license)

## Features

- **Content-based duplicate detection** — partial hash + full MD5 verification (no false positives)
- **Cross-folder duplicate finder** — files match even when renamed or moved
- **Folder sync** — copy unique files from target into master, mirroring directory structure
- **Internal duplicate cleanup** — auto-suggests the best path to keep
- **Image / PDF / video preview pane** with resolution info
- **Robocopy and Python copy engines** with auto-detected optimal thread counts
- **Built-in unzip** (Windows `tar` or Shell.Application)
- **Empty folder scanner and cleaner**
- **Recycle Bin deletion** (batched, multithreaded, non-blocking)
- **Protected folders** that are never deleted from
- **Cache save/load** so large scans aren't re-run

## Requirements

- **Python 3.8+**
- **Windows recommended** (Robocopy, `tar.exe`, and Shell.Application features are Windows-only; the rest is cross-platform)
- The script auto-installs / upgrades these on first run:
  - `send2trash >= 1.5.0`
  - `Pillow`
  - `PyMuPDF`
- **Optional**: install `ffmpeg` (with `ffprobe` on `PATH`) to show video resolutions in the duplicate list.

## Installation

```bash
git clone https://github.com/<your-user>/<your-repo>.git
cd <your-repo>
python sync_cache_smart_v2.py
```

No manual `pip install` step needed — missing/outdated packages are handled automatically at startup.

## Usage

```bash
python sync_cache_smart_v2.py
```

A window opens with seven tabs. Pick a mode, choose folders, and click **START SCAN**.

## Modes

| Mode | What it does |
|---|---|
| **Compare Two Folders** | Pick a **Master** (source of truth) and a **Target** (folder to clean/sync). Enables Tabs 1, 2, and 3. |
| **Single Folder Cleanup** | Pick one folder. Enables Tab 3 only. |

You can also mark **Protected folders** — files inside these are never deleted, regardless of mode or tab.

## How Matching Works

Files are compared by content hash, not by filename or location.

- **Smart mode (recommended)**: partial hash (first + last 64 KB + size) for the initial pass, then full MD5 to verify candidates. Fast and accurate.
- **Full mode**: full MD5 of every file. Slower; use only if you want every file fully fingerprinted.

A photo named `IMG_001.jpg` in one folder will match a renamed copy `vacation.jpg` in another folder if their bytes are identical.

## Tabs Overview

### 1. Safe to Delete (Matches Master)

Files in **target** whose content also exists in **master** — safe to delete from target.

- **Trash ALL listed here** — sends all to the Recycle Bin (batched, background thread).

### 2. Only in Target (Not in Master)

Files in **target** with no matching content in master, grouped by folder.

- **Copy These to Master** — copies every listed file into master, **mirroring the target's folder structure**. Collision-safe: never overwrites; appends `(from target N)` if needed.
- **Export List...** — writes a folder-by-folder text report.

### 3. Internal Duplicates

Groups of identical files within the target folder. Each group is sorted by a heuristic path score — the suggested keeper is at the top, marked `KEEP`. Protected files appear in red.

- **Select All Except Best** — pre-selects every duplicate, leaving keepers untouched.
- **Trash Selected** — sends highlighted files to the Recycle Bin.
- **Auto-Keep Best Path** — one-click: keep best, trash the rest.
- **Preview pane** (right side) — image thumbnails, PDF first-page render, text snippets, video file info.
- **Resolution column** — image and video dimensions.

### 4. Copy / Move Folder

Standalone folder-to-folder transfer.

- **Source** + **Destination** folder pickers
- **Mode**: Copy (keep source) or Move (delete source after)
- **Engine**: Robocopy (Windows, fast) or Python (cross-platform, threaded)
- **Threads**: auto-detect based on CPU and drive type, or set manually (NVMe handles up to 128 robocopy threads)
- **Cancel** button to abort mid-transfer

### 5. Unzip

Pick one or more `.zip` files and extract them.

- **Engine**: Windows `tar.exe` (fast) or Explorer / Shell.Application
- Options: overwrite existing files, or extract each zip into its own subfolder

### 6. Empty Folders

Scan a directory for empty folders. Select All / Delete Selected. Empty folders are also auto-cleaned after every duplicate-trash operation.

### 7. Log

Live log of every action — scans, deletions, copies, errors. Clear button included.

## Save / Load Scan Results

The `Cache / Save` menu lets you save scan results to a `.cache` file and reload them later, so you don't re-scan large folders. Reloading repopulates all tabs automatically.

## Safety Notes

- All deletions go to the **Recycle Bin** (`send2trash`), not permanent removal — restore from the bin if you change your mind.
- Trash operations run in **batches of 50 in a background thread**, so the GUI stays responsive and the Recycle Bin doesn't lag.
- **Copy These to Master** never overwrites files at the destination; numeric suffix is appended on collision.
- Files inside **Protected folders** are never deleted by any tab.
- Hash-then-verify means a "match" is byte-identical — no false positives.

## Recommended Workflows

### Cleaning up after a backup or file move

1. **Mode**: Compare Two Folders
2. **Master** = where you want files to live, **Target** = the messy folder
3. Run Smart scan
4. **Tab 1**: trash everything (these are already in master)
5. **Tab 2**: review unique files → **Copy These to Master** to sync them over
6. **Tab 3**: clean up any leftover internal duplicates in target
7. **Tab 6**: remove now-empty folders

### Deduping a single folder

1. **Mode**: Single Folder Cleanup
2. Smart scan
3. **Tab 3** → **Select All Except Best** → **Trash Selected**, or click **Auto-Keep Best Path** for one-click cleanup

## License

MIT (or whatever you prefer — update this section to match your repo).
