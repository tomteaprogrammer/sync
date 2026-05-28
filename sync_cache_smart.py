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

# --- LOGGING ---
logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)

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

def get_free_space_gb(folder):
    try:
        if not folder or not os.path.exists(folder):
            return 100
        total, used, free = shutil.disk_usage(folder)
        return free / (1024**3)
    except Exception:
        return 100

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

                # Check disk space every 50 files
                if i % 50 == 0:
                    free_gb = get_free_space_gb(folder)
                    if free_gb < 5.0:
                        self.stop_event.set()
                        self.alert("CRITICAL WARNING", f"Disk Space Low! ({free_gb:.2f} GB left).\nStopping scan.")
                        return {}

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
        self.lbl_space = tk.Label(root, text="Disk Guard Active (<5GB Stops Scan)", fg="green", font=("Arial", 8))
        self.lbl_space.pack()
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
            t_paths = [e[0] for e in t_entries]
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

        # Remove trashed items from UI without rescanning
        self.cross_dupes = []
        self.clear_trees()
        self.lbl_summary.config(text=f"Trashed {trashed} files.")

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
            self.lbl_stat.config(text=f"Trashed {trashed} files.")

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

        # Refresh without full rescan - just clear and update
        self.internal_dupes = []
        self.clear_trees()
        self.lbl_summary.config(text=f"Trashed {trashed} files. Run a new scan to verify.")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
