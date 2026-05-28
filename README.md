Sync Cache Smart — Duplicate Finder & Folder Sync Tool
A Tkinter GUI that finds duplicate files by content (not by name), helps clean up folders, copies/moves data, unzips archives, and syncs files between folders.

Requirements
Python 3.8+ (tested on 3.14)
Windows recommended (some features use robocopy / Shell.Application; the rest is cross-platform)
The script auto-installs/upgrades its Python dependencies on first run:
send2trash >= 1.5.0 (for safe deletion to Recycle Bin, batched)
Pillow (for image previews + resolution detection)
PyMuPDF (for PDF previews)
Optional, install separately if you want video resolutions in the list:
ffmpeg (must be on PATH — provides ffprobe)
Run
python sync_cache_smart_v2.py
How matching works
Files are compared by content hash, not by filename or folder location:

Smart mode (recommended): partial hash (first + last 64 KB + size) used for the initial pass, then full MD5 to verify any candidate matches. Fast and 100% accurate.
Full mode: full MD5 of every file. Slower; only useful if you want every file fingerprinted.
A photo named IMG_001.jpg in one folder will match a renamed copy vacation.jpg in another folder if their bytes are identical.

Modes
Compare Two Folders — pick a Master (the "source of truth") and a Target (the folder to clean up or sync). Shows what's in both, what's only in target, etc.
Single Folder Cleanup — pick one folder; finds internal duplicates only.
You can also mark Protected folders that are never deleted from, regardless of mode.

The Tabs
1. Safe to Delete (Matches Master)
Files in target whose content also exists in master. These are safe to remove from target — master still has a copy.

Trash ALL listed here — sends all to the Recycle Bin (batched, in the background).
2. Only in Target (Not in Master)
Files in target with no matching content anywhere in master. Grouped by folder.

Copy These to Master — copies every listed file into master, mirroring the target's folder structure. Collision-safe (existing files at the destination get (from target N) suffix; nothing is overwritten).
Export List... — writes a folder-by-folder text report.
3. Internal Duplicates (Clean Single Folder)
Groups of identical files within the target folder. Each group is sorted by a heuristic path score — the suggested keeper (best path) is at the top of each group, marked KEEP. Protected files appear in red and are never selected for deletion.

Select All Except Best — pre-selects the duplicates in every group, leaving the keeper untouched.
Trash Selected — sends the highlighted files to the Recycle Bin.
Auto-Keep Best Path — in one click, keeps the best file in each group and trashes the rest.
A right-side preview panel shows the selected file: image thumbnails, PDF first-page render, text preview, or a "Video file" label with an Open button. A Resolution column shows image/video dimensions.

4. Copy / Move Folder
Standalone folder-to-folder transfer.

Source + Destination pickers.
Mode: Copy (keep source) or Move (delete source after).
Engine: Robocopy (Windows, very fast) or Python (cross-platform, threaded).
Threads: auto-detect based on your CPU/drive type, or set manually. NVMe SSDs handle up to 128 robocopy threads.
Cancel button to abort mid-copy.
5. Unzip
Pick one or more .zip files and extract them.

Engine: Windows tar.exe (fast) or Explorer / Shell.Application (slower but mirrors right-click Extract All behavior).
Options: overwrite existing files, or extract each zip into its own subfolder.
6. Empty Folders
Scan a directory for empty folders. Select All / Delete Selected. Empty folders are also auto-cleaned after every duplicate-trash operation.

7. Log
Live log of everything the app does — scans, deletions, copies, errors. Clear Log button.

Save / Load Scan Results
Cache / Save menu lets you save scan results to a .cache file and reload them later, so you don't have to re-scan large folders. Reloading repopulates all the tabs.

Safety Notes
All deletions go to the Recycle Bin (via send2trash), not permanent removal — you can restore from the bin if you change your mind.
Trash operations run in batches of 50 in a background thread, so the GUI stays responsive and the Recycle Bin doesn't lag your computer.
The Copy These to Master action never overwrites files at the destination; it appends a numeric suffix if a name collision exists.
Files inside Protected folders are never deleted, regardless of which tab you use.
The hash-then-verify approach means a "match" is byte-identical content — false positives are not possible.
Recommended Workflow
Cleaning up after a backup / file move:

Mode: Compare Two Folders.
Master = where you want files to live. Target = the messy folder you're cleaning.
Smart scan.
Tab 1: trash everything (these are already in master).
Tab 2: review what's unique → click Copy These to Master to sync them over.
Tab 3: clean up any leftover internal duplicates in target.
Empty Folders tab: remove the now-empty husk folders.
Deduping a single folder:

Mode: Single Folder Cleanup.
Smart scan.
Tab 3 → Select All Except Best → Trash Selected, or use Auto-Keep Best Path for one-click cleanup.
