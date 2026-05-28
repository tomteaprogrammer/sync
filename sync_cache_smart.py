import os
import sys
import hashlib
import subprocess
import importlib.util
import threading
import platform
import shutil
import pickle
import logging
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import concurrent.futures
import re
import json
import tempfile
from pathlib import Path

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

class TextHandler(logging.Handler):
    def __init__(self, text_widget, root):
        super().__init__()
        self.text_widget = text_widget
        self.root = root

    def emit(self, record):
        msg = self.format(record) + "\n"
        self.root.after(0, self._append, msg)

    def _append(self, msg):
        self.text_widget.config(state=tk.NORMAL)
        self.text_widget.insert(tk.END, msg)
        self.text_widget.see(tk.END)
        self.text_widget.config(state=tk.DISABLED)

# --- 1. SETUP & UTILS ---
def install_and_import(package):
    if importlib.util.find_spec(package) is None:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        except Exception as e:
            log.warning(f"Could not install {package}: {e}")
    try:
        return importlib.import_module(package)
    except ImportError as e:
        log.warning(f"Could not import {package}: {e}")
        return None

send2trash = install_and_import('send2trash')

def prepare_path(path):
    path = os.path.normpath(path)
    path = os.path.abspath(path)
    if platform.system() == 'Windows' and len(path) > 259 and not path.startswith('\\\\?\\'):
        path = '\\\\?\\' + path
    return path

def is_subpath(path, parent):
    if not parent:
        return False
    try:
        path = os.path.abspath(path)
        parent = os.path.abspath(parent)
        return os.path.commonpath([parent, path]) == parent
    except ValueError:
        return False

def reveal_in_explorer(path):
    path = prepare_path(path)
    try:
        if platform.system() == 'Windows':
            subprocess.Popen(f'explorer /select,"{path}"')
        elif platform.system() == 'Darwin':
            subprocess.call(['open', '-R', path])
        else:
            subprocess.call(['xdg-open', os.path.dirname(path)])
    except Exception as e:
        log.warning(f"Could not open explorer for {path}: {e}")

def detect_optimal_threads():
    """Analyze CPU cores and drive type to recommend max copy threads."""
    cpu_cores = os.cpu_count() or 4
    drive_type = "Unknown"
    recommended = cpu_cores * 4

    if platform.system() == "Windows":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-PhysicalDisk | Select-Object MediaType,BusType | ConvertTo-Json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                disks = json.loads(result.stdout)
                if not isinstance(disks, list):
                    disks = [disks]
                for disk in disks:
                    media = str(disk.get("MediaType", "")).lower()
                    bus = str(disk.get("BusType", "")).lower()
                    if "nvme" in bus or "nvme" in media:
                        drive_type = "NVMe SSD"
                        recommended = 128
                        break
                    elif "ssd" in media or "solid" in media:
                        drive_type = "SATA SSD"
                        recommended = 64
                        break
                    elif "hdd" in media or "unspecified" in media:
                        drive_type = "HDD"
                        recommended = min(16, cpu_cores * 2)
        except Exception:
            pass

    return {
        "cpu_cores": cpu_cores,
        "drive_type": drive_type,
        "recommended": recommended,
        "label": f"Auto ({recommended}) — {cpu_cores} cores, {drive_type}"
    }

def format_size(size_bytes):
    """Human-readable file size."""
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"

def hash_file_full(filepath, stop_event=None):
    """Full MD5 hash of entire file."""
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        while True:
            if stop_event and stop_event.is_set():
                return None
            data = f.read(65536)
            if not data:
                break
            hasher.update(data)
    return hasher.hexdigest()

def hash_file_partial(filepath):
    """Fast partial hash: first 64KB + last 64KB + file size.
    Catches 99%+ of duplicates without reading the full file.
    Much faster for large video files."""
    CHUNK = 65536
    size = os.path.getsize(filepath)
    hasher = hashlib.md5()
    hasher.update(str(size).encode())  # include size in hash
    with open(filepath, 'rb') as f:
        # Read first chunk
        hasher.update(f.read(CHUNK))
        # Read last chunk (if file is big enough)
        if size > CHUNK * 2:
            f.seek(-CHUNK, 2)
            hasher.update(f.read(CHUNK))
    return hasher.hexdigest()


# --- 2. SCAN MODES ---
# Mode 0: Smart = partial hash then full-hash only matches (fast + 100% reliable)
# Mode 1: Full  = full MD5 hash everything (slow but simple)
SCAN_SMART = 0
SCAN_FULL = 1


# --- 3. LOGIC ENGINE ---
class Comparator(threading.Thread):
    def __init__(self, master_path, target_path, scan_mode, callback_update, callback_finish, callback_alert):
        super().__init__()
        self.master = master_path
        self.target = target_path
        self.scan_mode = scan_mode
        self.update_ui = callback_update
        self.finish = callback_finish
        self.alert = callback_alert
        self.daemon = True
        self.stop_event = threading.Event()

        try:
            self.max_threads = (os.cpu_count() or 4) + 2
        except Exception:
            self.max_threads = 4

    def get_identifier(self, filepath):
        if self.stop_event.is_set():
            return (filepath, None, 0)
        try:
            stat = os.stat(filepath)
            size = stat.st_size

            if self.scan_mode == SCAN_SMART:
                # Partial hash - fast, then verified with full hash on matches
                phash = hash_file_partial(filepath)
                return (filepath, ('partial', size, phash), size)

            else:
                # Full hash - 100% reliable
                fhash = hash_file_full(filepath, self.stop_event)
                if fhash is None:
                    return (filepath, None, size)
                return (filepath, ('full', size, fhash), size)

        except Exception as e:
            log.warning(f"Error processing {filepath}: {e}")
            return (filepath, None, 0)

    def process_folder_parallel(self, folder, label):
        if self.stop_event.is_set():
            return {}
        index = {}
        all_files = []

        self.update_ui(f"Listing files in {label}...", 0)
        for root, _, files in os.walk(folder):
            if self.stop_event.is_set():
                return {}
            for file in files:
                all_files.append(prepare_path(os.path.join(root, file)))

        total = len(all_files)
        if total == 0:
            return {}

        completed = 0
        self.update_ui(f"Processing {label}: 0/{total}", 0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_file = {executor.submit(self.get_identifier, f): f for f in all_files}

            for i, future in enumerate(concurrent.futures.as_completed(future_to_file)):
                if self.stop_event.is_set():
                    break

                path, ident, size = future.result()
                if ident:
                    if ident not in index:
                        index[ident] = []
                    index[ident].append((path, size))

                completed += 1
                if completed % 100 == 0 or completed == total:
                    self.update_ui(f"Processing {label}: {completed}/{total}", (completed / total) * 100)

        return index

    def verify_with_full_hash(self, all_indices):
        """Stage 2 for Smart mode: full-hash only the files that matched by partial hash.
        Takes a list of index dicts, finds groups with 2+ files, full-hashes those files,
        and returns corrected indices."""
        # Collect all partial-hash keys that have duplicates (across all indices combined)
        combined = {}
        for idx in all_indices:
            for ident, entries in idx.items():
                if ident not in combined:
                    combined[ident] = []
                combined[ident].extend(entries)

        # Only need to verify keys where 2+ files matched
        needs_verify = {k: v for k, v in combined.items() if len(v) > 1}
        all_files_to_hash = []
        for entries in needs_verify.values():
            all_files_to_hash.extend(entries)

        total = len(all_files_to_hash)
        if total == 0:
            return all_indices

        self.update_ui(f"Verifying {total} candidates with full hash...", 0)

        # Full-hash just the candidate files
        full_hashes = {}  # path -> full_hash
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_map = {}
            for path, size in all_files_to_hash:
                future_map[executor.submit(hash_file_full, path, self.stop_event)] = (path, size)

            for future in concurrent.futures.as_completed(future_map):
                if self.stop_event.is_set():
                    return all_indices
                path, size = future_map[future]
                try:
                    fhash = future.result()
                    if fhash:
                        full_hashes[path] = fhash
                except Exception as e:
                    log.warning(f"Verify hash failed for {path}: {e}")

                completed += 1
                if completed % 20 == 0 or completed == total:
                    self.update_ui(f"Verifying: {completed}/{total}", (completed / total) * 100)

        # Rebuild each index: replace partial-hash keys with full-hash keys for verified files
        new_indices = []
        for idx in all_indices:
            new_idx = {}
            for ident, entries in idx.items():
                if ident in needs_verify:
                    # Re-key these entries by full hash
                    for path, size in entries:
                        if path in full_hashes:
                            new_key = ('full', size, full_hashes[path])
                            if new_key not in new_idx:
                                new_idx[new_key] = []
                            new_idx[new_key].append((path, size))
                else:
                    # Single file with this partial hash — keep as-is
                    new_idx[ident] = entries
            new_indices.append(new_idx)

        return new_indices

    def run(self):
        target_idx = self.process_folder_parallel(self.target, "Target")
        if self.stop_event.is_set():
            return

        master_idx = {}
        if self.master:
            master_idx = self.process_folder_parallel(self.master, "Master")
            if self.stop_event.is_set():
                return

        # Smart mode: verify partial-hash matches with full hash
        if self.scan_mode == SCAN_SMART:
            self.update_ui("Verifying matches with full hash...", 0)
            if self.master:
                master_idx, target_idx = self.verify_with_full_hash([master_idx, target_idx])
            else:
                target_idx, = self.verify_with_full_hash([target_idx])

        self.update_ui("Finishing...", 100)
        self.finish(master_idx, target_idx)


# --- 4. GUI APPLICATION ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Cloud Sync: Smart Duplicate Finder")
        self.root.geometry("1200x850")

        self.master_path = None
        self.target_path = None
        self.protected_paths = []
        self.cross_dupes = []
        self.internal_dupes = []

        # Raw Data Storage (RAM)
        self.last_master_idx = {}
        self.last_target_idx = {}

        self.mode = tk.IntVar(value=2)

        # --- MENU BAR ---
        menubar = tk.Menu(root)
        cache_menu = tk.Menu(menubar, tearoff=0)
        cache_menu.add_command(label="Save Scan Results to File...", command=self.save_cache)
        cache_menu.add_command(label="Load Scan Results from File...", command=self.load_cache)
        cache_menu.add_separator()
        cache_menu.add_command(label="Clear Current Results", command=self.clear_results)
        menubar.add_cascade(label="Cache / Save", menu=cache_menu)
        root.config(menu=menubar)

        # Header
        tk.Label(root, text="SYNC: SMART DUPLICATE FINDER", font=("Arial", 16, "bold"), pady=10).pack()

        # MODE
        frame_mode = tk.Frame(root)
        frame_mode.pack(pady=5)
        tk.Label(frame_mode, text="Mode:", font=("Arial", 10, "bold")).pack(side=tk.LEFT)
        tk.Radiobutton(frame_mode, text="Compare Two Folders", variable=self.mode, value=1, command=self.toggle_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(frame_mode, text="Single Folder Cleanup", variable=self.mode, value=2, command=self.toggle_mode).pack(side=tk.LEFT, padx=10)

        # CONFIG
        frame_in = tk.LabelFrame(root, text="Configuration", padx=10, pady=10)
        frame_in.pack(fill=tk.X, padx=10)

        self.lbl_master = tk.Label(frame_in, text="1. PROTECTED Folder:", fg="#d32f2f", font=("Arial", 9, "bold"))
        self.lbl_master.grid(row=0, column=0, sticky="w")
        self.ent_master = tk.Entry(frame_in, width=70, bg="#ffebee")
        self.ent_master.grid(row=0, column=1, padx=5)
        self.btn_master = tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_master))
        self.btn_master.grid(row=0, column=2)

        self.lbl_target = tk.Label(frame_in, text="2. CLEANUP Folder:", fg="#1976d2", font=("Arial", 9, "bold"))
        self.lbl_target.grid(row=1, column=0, sticky="w", pady=5)
        self.ent_target = tk.Entry(frame_in, width=70, bg="#e3f2fd")
        self.ent_target.grid(row=1, column=1, padx=5, pady=5)
        tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_target)).grid(row=1, column=2, pady=5)

        # Protected folders (works in both modes)
        frame_prot = tk.LabelFrame(frame_in, text="Protected Folders (never deleted)", fg="#388e3c", padx=5, pady=5)
        frame_prot.grid(row=2, column=0, columnspan=3, sticky="ew", pady=5)

        self.lst_protected = tk.Listbox(frame_prot, height=3, bg="#e8f5e9", selectmode=tk.EXTENDED)
        self.lst_protected.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        btn_frame = tk.Frame(frame_prot)
        btn_frame.pack(side=tk.RIGHT)
        tk.Button(btn_frame, text="Add...", width=8, command=self.add_protected).pack(pady=2)
        tk.Button(btn_frame, text="Remove", width=8, command=self.remove_protected).pack(pady=2)

        # SCAN OPTIONS
        frame_act = tk.Frame(root, pady=10)
        frame_act.pack(fill=tk.X, padx=10)

        self.scan_mode = tk.IntVar(value=SCAN_SMART)
        scan_frame = tk.LabelFrame(frame_act, text="Scan Depth", padx=8, pady=4)
        scan_frame.pack(side=tk.LEFT)
        tk.Radiobutton(scan_frame, text="Smart (recommended)", variable=self.scan_mode, value=SCAN_SMART).pack(side=tk.LEFT, padx=4)
        tk.Radiobutton(scan_frame, text="Full (hash everything)", variable=self.scan_mode, value=SCAN_FULL).pack(side=tk.LEFT, padx=4)

        self.btn_scan = tk.Button(frame_act, text="START SCAN", bg="#4caf50", fg="white", font=("Arial", 10, "bold"), height=2, command=self.start_scan)
        self.btn_scan.pack(side=tk.RIGHT)

        # STATUS
        self.progress = ttk.Progressbar(root, mode='determinate')
        self.progress.pack(fill=tk.X, padx=10, pady=5)
        self.lbl_stat = tk.Label(root, text="Select folders or Load Cache.", fg="gray")
        self.lbl_stat.pack()

        # SUMMARY BAR
        self.lbl_summary = tk.Label(root, text="", font=("Arial", 10, "bold"), fg="#d32f2f")
        self.lbl_summary.pack()

        # TABS
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Tab 1: Cross duplicates
        self.tab_cross = tk.Frame(self.notebook)
        self.notebook.add(self.tab_cross, text="1. Safe to Delete (Matches Master)")
        self.tree_cross = self.create_tree(self.tab_cross, ["File Name", "Size", "Folder", "Status", "Full Path"])

        f_cross_act = tk.Frame(self.tab_cross, pady=5, bg="#eeeeee")
        f_cross_act.pack(fill=tk.X)
        tk.Button(f_cross_act, text="Trash ALL listed here", bg="#ffcdd2", command=self.trash_cross).pack(side=tk.RIGHT, padx=5)

        # Tab 2: Internal duplicates
        self.tab_internal = tk.Frame(self.notebook)
        self.notebook.add(self.tab_internal, text="2. Internal Duplicates (Clean Single Folder)")

        self.tree_internal = ttk.Treeview(
            self.tab_internal,
            columns=("name", "size", "folder", "path"),
            show="tree headings",
            selectmode="extended"  # Allow multi-select
        )
        self.tree_internal.heading("name", text="File Name")
        self.tree_internal.heading("size", text="Size")
        self.tree_internal.heading("folder", text="Folder")
        self.tree_internal.heading("path", text="Full Path")
        self.tree_internal.column("#0", width=30)
        self.tree_internal.column("name", width=300)
        self.tree_internal.column("size", width=80)
        self.tree_internal.column("folder", width=250)
        self.tree_internal.column("path", width=0, stretch=False)

        # Scrollbar for internal tree
        scroll_y = ttk.Scrollbar(self.tab_internal, orient="vertical", command=self.tree_internal.yview)
        self.tree_internal.configure(yscrollcommand=scroll_y.set)
        self.tree_internal.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree_internal.bind("<Double-1>", self.on_double_click)

        f_int_act = tk.Frame(self.tab_internal, pady=5, bg="#e3f2fd")
        f_int_act.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Button(f_int_act, text="Trash Selected", bg="#ffcdd2", command=self.delete_selected_internal).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_int_act, text="Auto-Keep Best Path", bg="#bbdefb", command=self.auto_cull_internal).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_int_act, text="Select All Except Best", bg="#c8e6c9", command=self.select_all_except_best).pack(side=tk.RIGHT, padx=5)

        # Tab 3: Copy / Move
        self.tab_copy = tk.Frame(self.notebook)
        self.notebook.add(self.tab_copy, text="3. Copy / Move Folder")

        frame_copy = tk.LabelFrame(self.tab_copy, text="Robocopy Folder Transfer", padx=15, pady=15)
        frame_copy.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(frame_copy, text="Source Folder:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.ent_copy_src = tk.Entry(frame_copy, width=70)
        self.ent_copy_src.grid(row=0, column=1, padx=5, pady=5)
        tk.Button(frame_copy, text="Browse...", command=lambda: self.browse(self.ent_copy_src)).grid(row=0, column=2, pady=5)

        tk.Label(frame_copy, text="Destination Folder:", font=("Arial", 9, "bold")).grid(row=1, column=0, sticky="w")
        self.ent_copy_dst = tk.Entry(frame_copy, width=70)
        self.ent_copy_dst.grid(row=1, column=1, padx=5, pady=5)
        tk.Button(frame_copy, text="Browse...", command=lambda: self.browse(self.ent_copy_dst)).grid(row=1, column=2, pady=5)

        self.copy_mode = tk.IntVar(value=0)
        mode_frame = tk.Frame(frame_copy)
        mode_frame.grid(row=2, column=0, columnspan=3, pady=5)
        tk.Radiobutton(mode_frame, text="Copy (keep source)", variable=self.copy_mode, value=0).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(mode_frame, text="Move (delete source after)", variable=self.copy_mode, value=1).pack(side=tk.LEFT, padx=10)

        options_frame = tk.Frame(frame_copy)
        options_frame.grid(row=3, column=0, columnspan=3, pady=5)

        tk.Label(options_frame, text="Engine:", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.copy_engine = ttk.Combobox(options_frame, values=["Robocopy (fast, Windows)", "Python (cross-platform)"],
                                        state="readonly", width=30)
        self.copy_engine.current(0)
        self.copy_engine.pack(side=tk.LEFT)

        tk.Label(options_frame, text="   Threads:", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(10, 5))
        self.hw_info = detect_optimal_threads()
        thread_values = [self.hw_info["label"], "8", "16", "32", "64", "128"]
        self.copy_threads = ttk.Combobox(options_frame, values=thread_values, state="readonly", width=35)
        self.copy_threads.current(0)
        self.copy_threads.pack(side=tk.LEFT)

        btn_frame_copy = tk.Frame(frame_copy)
        btn_frame_copy.grid(row=4, column=0, columnspan=3, pady=10)
        self.btn_copy_start = tk.Button(btn_frame_copy, text="START", bg="#4caf50", fg="white",
                                        font=("Arial", 10, "bold"), width=15, height=2, command=self.start_copy)
        self.btn_copy_start.pack(side=tk.LEFT, padx=5)
        self.btn_copy_cancel = tk.Button(btn_frame_copy, text="CANCEL", bg="#f44336", fg="white",
                                          font=("Arial", 10, "bold"), width=15, height=2,
                                          command=self.cancel_copy, state=tk.DISABLED)
        self.btn_copy_cancel.pack(side=tk.LEFT, padx=5)

        self.copy_stop_event = threading.Event()
        self.copy_process = None

        self.lbl_copy_stat = tk.Label(self.tab_copy, text="", fg="gray")
        self.lbl_copy_stat.pack(pady=5)

        self.txt_copy_log = tk.Text(self.tab_copy, height=15, state=tk.DISABLED, bg="#f5f5f5")
        scroll_copy = ttk.Scrollbar(self.tab_copy, orient="vertical", command=self.txt_copy_log.yview)
        self.txt_copy_log.configure(yscrollcommand=scroll_copy.set)
        self.txt_copy_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_copy.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        # Tab 4: Unzip
        self.tab_unzip = tk.Frame(self.notebook)
        self.notebook.add(self.tab_unzip, text="4. Unzip")

        frame_unzip_top = tk.LabelFrame(self.tab_unzip, text="Select Zip Files", padx=10, pady=10)
        frame_unzip_top.pack(fill=tk.X, padx=10, pady=10)

        browse_frame = tk.Frame(frame_unzip_top)
        browse_frame.pack(fill=tk.X)
        tk.Button(browse_frame, text="Add Zip Files...", command=self.unzip_add_files).pack(side=tk.LEFT, padx=5)
        tk.Button(browse_frame, text="Add Folder of Zips...", command=self.unzip_add_folder).pack(side=tk.LEFT, padx=5)
        tk.Button(browse_frame, text="Remove Selected", command=self.unzip_remove_selected).pack(side=tk.LEFT, padx=5)
        tk.Button(browse_frame, text="Clear All", command=self.unzip_clear).pack(side=tk.LEFT, padx=5)

        self.lst_unzip = tk.Listbox(frame_unzip_top, height=8, selectmode=tk.EXTENDED, bg="#fff3e0")
        scroll_unzip_list = ttk.Scrollbar(frame_unzip_top, orient="vertical", command=self.lst_unzip.yview)
        self.lst_unzip.configure(yscrollcommand=scroll_unzip_list.set)
        self.lst_unzip.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(5, 0))
        scroll_unzip_list.pack(side=tk.RIGHT, fill=tk.Y, pady=(5, 0))

        frame_unzip_opts = tk.Frame(self.tab_unzip)
        frame_unzip_opts.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(frame_unzip_opts, text="Engine:", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.unzip_engine = ttk.Combobox(frame_unzip_opts,
                                          values=["Windows tar (fast)", "Explorer (Shell.Application)"],
                                          state="readonly", width=30)
        self.unzip_engine.current(0)
        self.unzip_engine.pack(side=tk.LEFT, padx=(0, 15))

        self.unzip_overwrite = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_unzip_opts, text="Overwrite existing files", variable=self.unzip_overwrite).pack(side=tk.LEFT, padx=5)

        self.unzip_subfolder = tk.BooleanVar(value=True)
        tk.Checkbutton(frame_unzip_opts, text="Extract into subfolder (zip name)", variable=self.unzip_subfolder).pack(side=tk.LEFT, padx=5)

        btn_unzip_frame = tk.Frame(self.tab_unzip)
        btn_unzip_frame.pack(pady=5)
        self.btn_unzip_start = tk.Button(btn_unzip_frame, text="UNZIP ALL", bg="#ff9800", fg="white",
                                          font=("Arial", 10, "bold"), width=15, height=2, command=self.start_unzip)
        self.btn_unzip_start.pack(side=tk.LEFT, padx=5)

        self.lbl_unzip_stat = tk.Label(self.tab_unzip, text="", fg="gray")
        self.lbl_unzip_stat.pack(pady=3)

        self.txt_unzip_log = tk.Text(self.tab_unzip, height=10, state=tk.DISABLED, bg="#f5f5f5")
        scroll_unzip_log = ttk.Scrollbar(self.tab_unzip, orient="vertical", command=self.txt_unzip_log.yview)
        self.txt_unzip_log.configure(yscrollcommand=scroll_unzip_log.set)
        self.txt_unzip_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_unzip_log.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        # Tab 5: Empty Folder Remover
        self.tab_empty = tk.Frame(self.notebook)
        self.notebook.add(self.tab_empty, text="5. Empty Folders")

        frame_empty_top = tk.LabelFrame(self.tab_empty, text="Scan for Empty Folders", padx=15, pady=10)
        frame_empty_top.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(frame_empty_top, text="Folder to scan:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.ent_empty_path = tk.Entry(frame_empty_top, width=70)
        self.ent_empty_path.grid(row=0, column=1, padx=5, pady=5)
        tk.Button(frame_empty_top, text="Browse...", command=lambda: self.browse(self.ent_empty_path)).grid(row=0, column=2, pady=5)

        btn_empty_frame = tk.Frame(frame_empty_top)
        btn_empty_frame.grid(row=1, column=0, columnspan=3, pady=5)
        self.btn_empty_scan = tk.Button(btn_empty_frame, text="SCAN", bg="#4caf50", fg="white",
                                         font=("Arial", 10, "bold"), width=12, command=self.scan_empty_folders)
        self.btn_empty_scan.pack(side=tk.LEFT, padx=5)
        self.btn_empty_delete = tk.Button(btn_empty_frame, text="DELETE SELECTED", bg="#f44336", fg="white",
                                           font=("Arial", 10, "bold"), width=15, command=self.delete_empty_folders,
                                           state=tk.DISABLED)
        self.btn_empty_delete.pack(side=tk.LEFT, padx=5)
        self.btn_empty_select_all = tk.Button(btn_empty_frame, text="SELECT ALL", bg="#bbdefb",
                                               font=("Arial", 10, "bold"), width=12, command=self.select_all_empty,
                                               state=tk.DISABLED)
        self.btn_empty_select_all.pack(side=tk.LEFT, padx=5)

        self.lbl_empty_stat = tk.Label(self.tab_empty, text="", fg="gray")
        self.lbl_empty_stat.pack(pady=3)

        self.tree_empty = ttk.Treeview(self.tab_empty, columns=("path",), show="headings", selectmode="extended")
        self.tree_empty.heading("path", text="Empty Folder Path")
        self.tree_empty.column("path", width=800)
        scroll_empty = ttk.Scrollbar(self.tab_empty, orient="vertical", command=self.tree_empty.yview)
        self.tree_empty.configure(yscrollcommand=scroll_empty.set)
        self.tree_empty.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_empty.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        # Tab 6: Log
        self.tab_log = tk.Frame(self.notebook)
        self.notebook.add(self.tab_log, text="6. Log")

        log_btn_frame = tk.Frame(self.tab_log)
        log_btn_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Button(log_btn_frame, text="Clear Log", command=self.clear_log).pack(side=tk.RIGHT, padx=5)

        self.txt_log = tk.Text(self.tab_log, state=tk.DISABLED, bg="#fafafa", font=("Consolas", 9))
        scroll_log = ttk.Scrollbar(self.tab_log, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll_log.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_log.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        handler = TextHandler(self.txt_log, root)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
        handler.setLevel(logging.INFO)
        log.addHandler(handler)
        log.setLevel(logging.INFO)

        log.info("Application started.")

        self.toggle_mode()

    def toggle_mode(self):
        m = self.mode.get()
        if m == 1:
            self.ent_master.config(state='normal')
            self.btn_master.config(state='normal')
            self.lbl_master.config(fg="#d32f2f")
            self.notebook.tab(0, state='normal')
        else:
            self.ent_master.delete(0, tk.END)
            self.ent_master.config(state='disabled')
            self.btn_master.config(state='disabled')
            self.lbl_master.config(fg="gray")
            self.notebook.tab(0, state='disabled')
            self.notebook.select(1)

    def create_tree(self, parent, cols):
        tree = ttk.Treeview(parent, columns=cols, show="headings")
        for c in cols:
            tree.heading(c, text=c)
        tree.column(cols[0], width=250)
        tree.column(cols[1], width=80)  # Size column
        tree.column(cols[-1], width=0, stretch=False)
        # Add scrollbar
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.bind("<Double-1>", self.on_double_click)
        return tree

    def browse(self, entry):
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, tk.END)
            entry.insert(0, prepare_path(d))

    def _get_thread_count(self):
        val = self.copy_threads.get()
        if val.startswith("Auto"):
            return self.hw_info["recommended"]
        try:
            return int(val)
        except ValueError:
            return 64

    def start_copy(self):
        src = self.ent_copy_src.get().strip()
        dst = self.ent_copy_dst.get().strip()
        if not src or not os.path.isdir(src):
            return messagebox.showerror("Error", "Select a valid source folder.")
        if not dst:
            return messagebox.showerror("Error", "Select a destination folder.")

        move = self.copy_mode.get() == 1
        action = "Move" if move else "Copy"
        engine = self.copy_engine.get()
        threads = self._get_thread_count()

        if not messagebox.askyesno("Confirm", f"{action} everything from:\n{src}\n\nTo:\n{dst}\n\nEngine: {engine}\nThreads: {threads}"):
            return

        self.copy_stop_event.clear()
        self.btn_copy_start.config(state=tk.DISABLED)
        self.btn_copy_cancel.config(state=tk.NORMAL)
        self.lbl_copy_stat.config(text=f"{action} in progress...", fg="blue")
        self.txt_copy_log.config(state=tk.NORMAL)
        self.txt_copy_log.delete("1.0", tk.END)
        self.txt_copy_log.config(state=tk.DISABLED)

        if engine.startswith("Robocopy"):
            threading.Thread(target=self._run_robocopy, args=(src, dst, move, action, threads), daemon=True).start()
        else:
            threading.Thread(target=self._run_python_copy, args=(src, dst, move, action, threads), daemon=True).start()

    def cancel_copy(self):
        self.copy_stop_event.set()
        if self.copy_process:
            try:
                self.copy_process.terminate()
            except Exception:
                pass
        self.lbl_copy_stat.config(text="Cancelled.", fg="red")
        self.btn_copy_start.config(state=tk.NORMAL)
        self.btn_copy_cancel.config(state=tk.DISABLED)

    def _copy_finished(self, msg, color):
        self.lbl_copy_stat.config(text=msg, fg=color)
        self.btn_copy_start.config(state=tk.NORMAL)
        self.btn_copy_cancel.config(state=tk.DISABLED)
        self.copy_process = None

    def _run_robocopy(self, src, dst, move, action, threads):
        log.info(f"Robocopy {action}: {src} → {dst} ({threads} threads)")
        cmd = ["robocopy", src, dst, "/E", f"/MT:{threads}", "/R:1", "/W:1", "/NP"]
        if move:
            cmd.append("/MOVE")
        try:
            self.copy_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                  text=True, encoding="utf-8", errors="replace")
            for line in self.copy_process.stdout:
                if self.copy_stop_event.is_set():
                    self.copy_process.terminate()
                    self.root.after(0, self._append_copy_log, "\n--- CANCELLED ---\n")
                    return
                self.root.after(0, self._append_copy_log, line)
            self.copy_process.wait()
            code = self.copy_process.returncode
            if code <= 3:
                msg = f"{action} completed successfully."
                color = "green"
            elif code <= 7:
                msg = f"{action} completed with some mismatches or extra files (code {code})."
                color = "orange"
            else:
                msg = f"{action} had errors (code {code})."
                color = "red"
        except Exception as e:
            msg = f"Error: {e}"
            color = "red"
        self.root.after(0, self._copy_finished, msg, color)

    def _copy_single_file(self, args):
        src_file, dst_file, move = args
        if self.copy_stop_event.is_set():
            return (src_file, False, "cancelled")
        try:
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            if move:
                shutil.move(src_file, dst_file)
            else:
                shutil.copy2(src_file, dst_file)
            return (src_file, True, None)
        except Exception as e:
            return (src_file, False, str(e))

    def _run_python_copy(self, src, dst, move, action, threads):
        log.info(f"Python {action}: {src} → {dst} ({threads} threads)")
        self.root.after(0, self._append_copy_log, "Listing files...\n")
        file_pairs = []
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            dest_dir = os.path.join(dst, rel)
            for f in files:
                file_pairs.append((os.path.join(root, f), os.path.join(dest_dir, f), move))

        total = len(file_pairs)
        copied = 0
        failed = 0
        workers = min(threads, total) if total > 0 else 1
        self.root.after(0, self._append_copy_log, f"Found {total} files. {action} with {workers} threads...\n\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._copy_single_file, pair): pair for pair in file_pairs}
            for future in concurrent.futures.as_completed(futures):
                if self.copy_stop_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    self.root.after(0, self._append_copy_log, f"\n--- CANCELLED after {copied} files ---\n")
                    self.root.after(0, self._copy_finished, f"Cancelled. {copied} files copied before stop.", "red")
                    return

                path, success, err = future.result()
                if success:
                    copied += 1
                else:
                    failed += 1
                    self.root.after(0, self._append_copy_log, f"FAILED: {path} — {err}\n")

                if copied % 100 == 0 and copied > 0:
                    self.root.after(0, self._append_copy_log, f"{action}: {copied}/{total} files...\n")

        if move and not self.copy_stop_event.is_set():
            for root, dirs, files in os.walk(src, topdown=False):
                try:
                    if not os.listdir(root):
                        os.rmdir(root)
                except Exception:
                    pass

        msg = f"{action} complete. {copied}/{total} files."
        if failed:
            msg += f" {failed} failed."
        color = "green" if failed == 0 else "orange"
        self.root.after(0, self._append_copy_log, f"\n{msg}\n")
        self.root.after(0, self._copy_finished, msg, color)

    def _append_copy_log(self, line):
        self.txt_copy_log.config(state=tk.NORMAL)
        self.txt_copy_log.insert(tk.END, line)
        self.txt_copy_log.see(tk.END)
        self.txt_copy_log.config(state=tk.DISABLED)

    # --- UNZIP ---
    def unzip_add_files(self):
        files = filedialog.askopenfilenames(title="Select Zip Files",
                                            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")])
        for f in files:
            p = prepare_path(f)
            current = list(self.lst_unzip.get(0, tk.END))
            if p not in current:
                self.lst_unzip.insert(tk.END, p)

    def unzip_add_folder(self):
        d = filedialog.askdirectory(title="Select folder containing zip files")
        if d:
            current = list(self.lst_unzip.get(0, tk.END))
            for z in sorted(Path(d).glob("*.zip")):
                p = prepare_path(str(z))
                if p not in current:
                    self.lst_unzip.insert(tk.END, p)

    def unzip_remove_selected(self):
        for i in reversed(self.lst_unzip.curselection()):
            self.lst_unzip.delete(i)

    def unzip_clear(self):
        self.lst_unzip.delete(0, tk.END)

    def clear_log(self):
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.config(state=tk.DISABLED)

    # --- EMPTY FOLDER REMOVER ---
    def scan_empty_folders(self):
        folder = self.ent_empty_path.get().strip()
        if not folder or not os.path.isdir(folder):
            return messagebox.showerror("Error", "Select a valid folder to scan.")

        for item in self.tree_empty.get_children():
            self.tree_empty.delete(item)

        self.btn_empty_scan.config(state=tk.DISABLED)
        self.lbl_empty_stat.config(text="Scanning...", fg="blue")

        def run():
            empty_dirs = []
            for dirpath, dirnames, filenames in os.walk(folder, topdown=False):
                if dirpath == folder:
                    continue
                try:
                    if not os.listdir(dirpath):
                        empty_dirs.append(dirpath)
                except Exception:
                    pass

            def populate():
                for d in sorted(empty_dirs):
                    self.tree_empty.insert("", "end", values=(d,))
                count = len(empty_dirs)
                self.lbl_empty_stat.config(text=f"Found {count} empty folder(s).", fg="green" if count == 0 else "#d32f2f")
                self.btn_empty_scan.config(state=tk.NORMAL)
                self.btn_empty_delete.config(state=tk.NORMAL if count > 0 else tk.DISABLED)
                self.btn_empty_select_all.config(state=tk.NORMAL if count > 0 else tk.DISABLED)
                log.info(f"Empty folder scan: {count} found in {folder}")

            self.root.after(0, populate)

        threading.Thread(target=run, daemon=True).start()

    def select_all_empty(self):
        items = self.tree_empty.get_children()
        if items:
            self.tree_empty.selection_set(*items)

    def delete_empty_folders(self):
        sel = self.tree_empty.selection()
        if not sel:
            return messagebox.showinfo("Info", "Select folders to delete first.")

        if not messagebox.askyesno("Confirm", f"Permanently delete {len(sel)} empty folder(s)?"):
            return

        removed = 0
        failed = 0
        for item_id in sel:
            path = self.tree_empty.item(item_id, "values")[0]
            try:
                os.rmdir(path)
                self.tree_empty.delete(item_id)
                removed += 1
            except Exception as e:
                log.warning(f"Could not remove {path}: {e}")
                failed += 1

        msg = f"Removed {removed} folder(s)."
        if failed:
            msg += f" {failed} failed."
        self.lbl_empty_stat.config(text=msg, fg="green" if failed == 0 else "orange")
        log.info(f"Empty folder delete: {removed} removed, {failed} failed")

        remaining = len(self.tree_empty.get_children())
        self.btn_empty_delete.config(state=tk.NORMAL if remaining > 0 else tk.DISABLED)
        self.btn_empty_select_all.config(state=tk.NORMAL if remaining > 0 else tk.DISABLED)

    def _append_unzip_log(self, line):
        self.txt_unzip_log.config(state=tk.NORMAL)
        self.txt_unzip_log.insert(tk.END, line)
        self.txt_unzip_log.see(tk.END)
        self.txt_unzip_log.config(state=tk.DISABLED)

    def start_unzip(self):
        zips = list(self.lst_unzip.get(0, tk.END))
        if not zips:
            return messagebox.showerror("Error", "Add zip files first.")

        engine = self.unzip_engine.get()
        if not messagebox.askyesno("Confirm", f"Unzip {len(zips)} file(s) to their same folder?\n\nEngine: {engine}"):
            return

        self.btn_unzip_start.config(state=tk.DISABLED)
        self.lbl_unzip_stat.config(text="Unzipping...", fg="blue")
        self.txt_unzip_log.config(state=tk.NORMAL)
        self.txt_unzip_log.delete("1.0", tk.END)
        self.txt_unzip_log.config(state=tk.DISABLED)

        overwrite = self.unzip_overwrite.get()
        subfolder = self.unzip_subfolder.get()
        threading.Thread(target=self._run_unzip, args=(zips, overwrite, subfolder, engine), daemon=True).start()

    def _unzip_tar(self, zp, extract_to, overwrite):
        tar_path = shutil.which("tar")
        if not tar_path:
            raise RuntimeError("Windows tar.exe not found on this computer.")
        extract_to.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [tar_path, "-xf", str(zp), "-C", str(extract_to)],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    def _unzip_explorer(self, zp, extract_to, overwrite):
        extract_to.mkdir(parents=True, exist_ok=True)
        vbs_code = f'''Set shell = CreateObject("Shell.Application")
Set source = shell.NameSpace("{str(zp)}")
Set destination = shell.NameSpace("{str(extract_to)}")
If source Is Nothing Then
    WScript.Echo "Could not open ZIP file."
    WScript.Quit 1
End If
If destination Is Nothing Then
    WScript.Echo "Could not open destination folder."
    WScript.Quit 1
End If
destination.CopyHere source.Items, 16
WScript.Sleep 2000
'''
        temp_vbs = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".vbs", delete=False, encoding="utf-8") as f:
                temp_vbs = f.name
                f.write(vbs_code)
            result = subprocess.run(
                ["cscript.exe", "//NoLogo", temp_vbs],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        finally:
            if temp_vbs:
                try:
                    os.unlink(temp_vbs)
                except Exception:
                    pass

    def _run_unzip(self, zips, overwrite, subfolder, engine):
        log.info(f"Unzip started — {len(zips)} files, engine={engine}, overwrite={overwrite}, subfolder={subfolder}")

        if engine.startswith("Windows tar"):
            unzip_fn = self._unzip_tar
        else:
            unzip_fn = self._unzip_explorer

        success = 0
        failed = 0
        for zip_path in zips:
            zp = Path(zip_path)
            if subfolder:
                extract_to = zp.parent / zp.stem
            else:
                extract_to = zp.parent

            self.root.after(0, self._append_unzip_log, f"Unzipping: {zp.name} → {extract_to}\n")

            try:
                unzip_fn(zp, extract_to, overwrite)
                self.root.after(0, self._append_unzip_log, f"  OK\n")
                success += 1
            except Exception as e:
                self.root.after(0, self._append_unzip_log, f"  FAILED: {e}\n")
                log.warning(f"Unzip failed for {zp.name}: {e}")
                failed += 1

        msg = f"Done. {success} extracted."
        if failed:
            msg += f" {failed} failed."
        color = "green" if failed == 0 else "orange"
        log.info(f"Unzip complete — {success} OK, {failed} failed")
        self.root.after(0, self._append_unzip_log, f"\n{msg}\n")
        self.root.after(0, self.lbl_unzip_stat.config, {"text": msg, "fg": color})
        self.root.after(0, self.btn_unzip_start.config, {"state": tk.NORMAL})

    def add_protected(self):
        d = filedialog.askdirectory(title="Select folder to protect")
        if d:
            p = prepare_path(d)
            current = list(self.lst_protected.get(0, tk.END))
            if p not in current:
                self.lst_protected.insert(tk.END, p)

    def remove_protected(self):
        sel = self.lst_protected.curselection()
        for i in reversed(sel):
            self.lst_protected.delete(i)

    def is_protected(self, path):
        for p in self.protected_paths:
            if is_subpath(path, p):
                return True
        return False

    def cleanup_empty_folders(self):
        """Walk scanned directories bottom-up and remove empty folders.
        Skips protected folders and scan root folders themselves."""
        roots = [self.target_path]
        if self.master_path:
            roots.append(self.master_path)

        removed = 0
        for scan_root in roots:
            if not scan_root or not os.path.isdir(scan_root):
                continue
            for dirpath, dirnames, filenames in os.walk(scan_root, topdown=False):
                if dirpath == scan_root:
                    continue
                if self.is_protected(dirpath):
                    continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                        removed += 1
                except Exception as e:
                    log.warning(f"Could not remove empty folder {dirpath}: {e}")

        if removed:
            log.info(f"Cleaned up {removed} empty folder(s)")
            self.lbl_stat.config(text=f"{self.lbl_stat.cget('text')} Removed {removed} empty folder(s).")

    def alert_user(self, title, msg):
        self.root.after(0, lambda: messagebox.showwarning(title, msg))
        self.root.after(0, lambda: self.btn_scan.config(state=tk.NORMAL))

    # --- SCANNING ---
    def start_scan(self):
        m = self.ent_master.get() if self.mode.get() == 1 else None
        t = self.ent_target.get()
        if not t:
            return messagebox.showerror("Error", "Select Target/Cleanup folder.")

        self.master_path, self.target_path = m, t
        self.protected_paths = list(self.lst_protected.get(0, tk.END))
        self.btn_scan.config(state=tk.DISABLED)
        self.clear_results()

        mode_name = "Smart" if self.scan_mode.get() == SCAN_SMART else "Full"
        log.info(f"Scan started — mode: {mode_name}, target: {t}" + (f", master: {m}" if m else ""))
        Comparator(m, t, self.scan_mode.get(), self.update_ui, self.scan_finished, self.alert_user).start()

    def update_ui(self, msg, pct):
        self.root.after(0, lambda: [self.lbl_stat.config(text=msg), self.progress.configure(value=pct)])

    def scan_finished(self, master_idx, target_idx):
        self.last_master_idx = master_idx
        self.last_target_idx = target_idx
        self.root.after(0, self.calculate_and_populate)

    # --- LOGIC & CACHE ---
    def calculate_and_populate(self):
        """Runs the comparison logic on the currently stored indices."""
        self.lbl_stat.config(text="Processing Results...")

        cross_dupes = []
        internal_dupes = []

        for ident, t_entries in self.last_target_idx.items():
            if self.mode.get() == 1 and self.last_master_idx and ident in self.last_master_idx:
                cross_dupes.append({
                    'master_file': self.last_master_idx[ident][0][0],
                    'target_files': t_entries
                })
            else:
                if len(t_entries) > 1:
                    internal_dupes.append(t_entries)

        self.cross_dupes = cross_dupes
        self.internal_dupes = internal_dupes

        self.populate_trees()
        self.btn_scan.config(state=tk.NORMAL)
        self.progress['value'] = 100

        # Calculate totals
        total_waste = 0
        total_extra = 0
        for group in internal_dupes:
            sizes = [e[1] for e in group]
            total_extra += len(group) - 1
            total_waste += sum(sorted(sizes)[:-1])  # everything except largest (they're identical so same size)
        for d in cross_dupes:
            for e in d['target_files']:
                total_extra += 1
                total_waste += e[1]

        self.lbl_stat.config(text=f"Done. {len(internal_dupes)} internal groups, {len(cross_dupes)} cross-folder matches.")
        self.lbl_summary.config(text=f"Recoverable: {format_size(total_waste)} from {total_extra} duplicate files")
        log.info(f"Scan complete — {len(internal_dupes)} internal groups, {len(cross_dupes)} cross matches, {format_size(total_waste)} recoverable")

    def populate_trees(self):
        self.clear_trees()

        if self.mode.get() == 1:
            for d in self.cross_dupes:
                for path, size in d['target_files']:
                    self.tree_cross.insert("", "end", values=(
                        os.path.basename(path),
                        format_size(size),
                        os.path.basename(os.path.dirname(path)),
                        "Safe to Delete",
                        path
                    ))

        for group in self.internal_dupes:
            group_size = format_size(group[0][1])
            grp_id = self.tree_internal.insert("", "end", values=(
                f"[GROUP] {len(group)} copies",
                group_size,
                "",
                ""
            ), open=True)

            # Sort: best path first (score-based)
            scored = sorted(group, key=lambda e: self._path_score(e[0]))
            for i, (path, size) in enumerate(scored):
                is_protected = self.is_protected(path)
                if is_protected:
                    tag = "protected"
                    prefix = "PROTECTED  "
                elif i == 0:
                    tag = "keeper"
                    prefix = "KEEP  "
                else:
                    tag = "dupe"
                    prefix = ""
                self.tree_internal.insert(grp_id, "end", values=(
                    prefix + os.path.basename(path),
                    format_size(size),
                    os.path.basename(os.path.dirname(path)),
                    path
                ), tags=(tag,))

        # Color the keeper vs dupes vs protected
        self.tree_internal.tag_configure("protected", foreground="#d32f2f", font=("Arial", 9, "bold"))
        self.tree_internal.tag_configure("keeper", foreground="#2e7d32")
        self.tree_internal.tag_configure("dupe", foreground="#666666")

    def _path_score(self, path):
        """Lower score = better path to keep.
        Protected folder files always win. Then prefers organized names, no (1)(2) suffixes."""
        rel = path.lower()
        score = 0

        # Protected folder files ALWAYS kept (lowest possible score)
        if self.is_protected(path):
            return -100000

        # Penalize (1)(2)(3) download duplicates heavily
        if re.search(r'\(\d+\)\.[a-z]+$', rel):
            score += 1000

        # Penalize "download " prefix
        basename = os.path.basename(rel)
        if basename.startswith('download '):
            score += 500

        # Penalize "copy of" or "- copy"
        if 'copy of' in rel or '- copy' in rel:
            score += 500

        # Prefer files with "CF " prefix (organized/renamed)
        if os.path.basename(path).startswith('CF '):
            score -= 200

        # Prefer files in named/organized folders
        if 'named scenes' in rel:
            score -= 100
        if 'edited' in rel and 'questionnaire' not in rel:
            score -= 50

        # For videos: prefer later processing stages
        if 'without captions' in rel:
            score -= 40
        elif 'edited' in rel:
            score -= 30
        elif 'cleaned' in rel:
            score -= 20
        elif 'original downloads' in rel:
            score -= 10

        # Tiebreaker: prefer shorter paths (usually more organized)
        score += len(path) * 0.01

        return score

    def clear_trees(self):
        for t in [self.tree_cross, self.tree_internal]:
            for x in t.get_children():
                t.delete(x)

    def clear_results(self):
        self.clear_trees()
        self.last_master_idx = {}
        self.last_target_idx = {}
        self.cross_dupes = []
        self.internal_dupes = []
        self.lbl_summary.config(text="")

    # --- SAVE / LOAD ---
    def save_cache(self):
        if not self.last_target_idx:
            return messagebox.showwarning("Empty", "No scan data to save. Run a scan first.")

        f = filedialog.asksaveasfilename(defaultextension=".cache", filetypes=[("Scan Cache", "*.cache")])
        if f:
            try:
                data = {
                    'master': self.last_master_idx,
                    'target': self.last_target_idx,
                    'mode': self.mode.get()
                }
                with open(f, 'wb') as outfile:
                    pickle.dump(data, outfile)
                messagebox.showinfo("Saved", "Scan results saved successfully.")
            except Exception as e:
                messagebox.showerror("Error", f"Could not save: {e}")

    def load_cache(self):
        f = filedialog.askopenfilename(filetypes=[("Scan Cache", "*.cache")])
        if f:
            try:
                with open(f, 'rb') as infile:
                    data = pickle.load(infile)

                self.last_master_idx = data.get('master', {})
                self.last_target_idx = data.get('target', {})
                self.mode.set(data.get('mode', 2))
                self.toggle_mode()

                self.calculate_and_populate()
                messagebox.showinfo("Loaded", "Cache loaded and results refreshed.")
            except Exception as e:
                messagebox.showerror("Error", f"Could not load: {e}")

    # --- ACTIONS ---
    def on_double_click(self, event):
        tree = event.widget
        item = tree.selection()
        if not item:
            return
        vals = tree.item(item, "values")
        if vals:
            reveal_in_explorer(vals[-1])

    def trash_cross(self):
        if not self.cross_dupes:
            return
        count = sum(len(d['target_files']) for d in self.cross_dupes)
        if not messagebox.askyesno("Confirm", f"Trash {count} files from Tab 1?"):
            return
        trashed = 0
        failed = 0
        for d in self.cross_dupes:
            for path, size in d['target_files']:
                if self.master_path and is_subpath(path, self.master_path):
                    continue
                try:
                    send2trash.send2trash(path)
                    trashed += 1
                except Exception as e:
                    log.warning(f"Could not trash {path}: {e}")
                    failed += 1

        msg = f"Trashed {trashed} files."
        if failed:
            msg += f"\n{failed} files could not be trashed."
        messagebox.showinfo("Done", msg)

        log.info(f"Cross-folder trash: {trashed} trashed, {failed} failed")
        self.cross_dupes = []
        self.clear_trees()
        self.lbl_summary.config(text=f"Trashed {trashed} files.")
        self.cleanup_empty_folders()

    def delete_selected_internal(self):
        """Trash all selected items (supports multi-select)."""
        sel = self.tree_internal.selection()
        if not sel:
            return

        # Collect paths from selection (skip group headers and protected files)
        paths_to_trash = []
        skipped_protected = 0
        for item_id in sel:
            vals = self.tree_internal.item(item_id, "values")
            path = vals[-1] if vals else ""
            if path and os.path.exists(path):
                if self.is_protected(path):
                    skipped_protected += 1
                    continue
                paths_to_trash.append((item_id, path))

        if skipped_protected:
            messagebox.showinfo("Protected", f"Skipped {skipped_protected} file(s) in protected folders.")

        if not paths_to_trash:
            return

        if not messagebox.askyesno("Delete", f"Trash {len(paths_to_trash)} selected file(s)?"):
            return

        trashed = 0
        for item_id, path in paths_to_trash:
            try:
                send2trash.send2trash(path)
                self.tree_internal.delete(item_id)
                trashed += 1
            except Exception as e:
                log.warning(f"Could not trash {path}: {e}")

        if trashed:
            log.info(f"Internal trash: {trashed} files deleted")
            self.lbl_stat.config(text=f"Trashed {trashed} files.")
            self.cleanup_empty_folders()

    def select_all_except_best(self):
        """Select all non-KEEP items in every group for easy bulk delete.
        Never selects files inside the protected folder."""
        self.tree_internal.selection_remove(*self.tree_internal.selection())
        to_select = []
        for group_id in self.tree_internal.get_children():
            children = self.tree_internal.get_children(group_id)
            # Skip the first child (best/keeper), select the rest
            for child_id in children[1:]:
                vals = self.tree_internal.item(child_id, "values")
                path = vals[-1] if vals else ""
                if path and self.is_protected(path):
                    continue
                to_select.append(child_id)
        if to_select:
            self.tree_internal.selection_set(*to_select)
            self.lbl_stat.config(text=f"Selected {len(to_select)} duplicate files (kept best from each group).")

    def auto_cull_internal(self):
        """Keep the best-scored path in each group and trash the rest."""
        if not self.internal_dupes:
            return

        total_dupes = sum(len(g) - 1 for g in self.internal_dupes)
        if not messagebox.askyesno("Auto-Clean",
                f"Keep the best path in each group and trash {total_dupes} duplicates?"):
            return

        trashed = 0
        failed = 0
        for group in self.internal_dupes:
            # Sort by score, keep the best (lowest score)
            scored = sorted(group, key=lambda e: self._path_score(e[0]))
            to_delete = scored[1:]  # everything except the best
            for path, size in to_delete:
                if self.is_protected(path):
                    continue
                try:
                    send2trash.send2trash(path)
                    trashed += 1
                except Exception as e:
                    log.warning(f"Could not trash {path}: {e}")
                    failed += 1

        msg = f"Trashed {trashed} duplicate files."
        if failed:
            msg += f"\n{failed} files could not be trashed."
        messagebox.showinfo("Done", msg)

        log.info(f"Auto-cull: {trashed} trashed, {failed} failed")
        self.internal_dupes = []
        self.clear_trees()
        self.lbl_summary.config(text=f"Trashed {trashed} files. Run a new scan to verify.")
        self.cleanup_empty_folders()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
