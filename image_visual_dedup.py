"""
Visual (near-duplicate) image finder.

Finds images that look the same even when their bytes differ — resizes,
re-compressions, format changes (JPEG <-> HEIC), light re-saves. This is NOT
an exact byte-for-byte deduper; it uses perceptual hashing, which is fuzzy.

Because matching is fuzzy, results are REVIEW-ONLY: nothing is ever deleted
automatically. You select what to trash and confirm; files go to the Recycle
Bin via send2trash.

Handles:
  * Standard images (jpg/png/webp/tiff/bmp/gif)
  * HEIC/HEIF (via pillow-heif)
  * .MOV / video: if a .MOV has a same-named still (Live Photo), it's treated
    as that still's partner; an orphan .MOV has one frame extracted and compared.
"""

import os
import sys
import subprocess
import importlib
import importlib.util
import threading
import platform
import pickle
import tempfile
import multiprocessing
import concurrent.futures
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# True only in the main process. Worker processes spawned for parallel hashing
# re-import this module; this flag keeps them from re-printing startup banners.
_IS_MAIN_PROCESS = multiprocessing.parent_process() is None


# --- DEPENDENCY BOOTSTRAP ---------------------------------------------------
def _pip_install(pip_name):
    """Try hard to pip-install a package. Returns True on success.
    Falls back to a --user install, and bootstraps pip with ensurepip if needed."""
    attempts = [
        [sys.executable, "-m", "pip", "install", pip_name],
        [sys.executable, "-m", "pip", "install", "--user", pip_name],
    ]
    for cmd in attempts:
        try:
            subprocess.check_call(cmd)
            return True
        except Exception as e:
            print(f"  pip attempt failed: {' '.join(cmd[2:])} -> {e}")
    # pip itself might be missing — bootstrap it, then retry once.
    try:
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
        return True
    except Exception as e:
        print(f"  ensurepip bootstrap failed for {pip_name}: {e}")
    return False


def install_and_import(package, pip_name=None):
    """Import a package, automatically pip-installing it if it's missing or broken.
    Drives off import success (not just find_spec) so partial installs get repaired.
    Returns the imported module, or None if it truly can't be made to work."""
    pip_name = pip_name or package
    # 1. Already importable? Done.
    try:
        return importlib.import_module(package)
    except Exception:
        pass
    # 2. Not importable — install via pip and retry.
    print(f"Installing missing library: {pip_name} ...")
    _pip_install(pip_name)
    importlib.invalidate_caches()
    try:
        return importlib.import_module(package)
    except Exception as e:
        print(f"Could not import {package} even after install: {e}")
        return None


if _IS_MAIN_PROCESS:
    print("Installing dependencies... (first run may take a minute)")
send2trash = install_and_import("send2trash")
PIL = install_and_import("PIL", "Pillow")
imagehash = install_and_import("imagehash")
pillow_heif = install_and_import("pillow_heif", "pillow-heif")
# Video frame extraction (for orphan .MOV). Optional — MOVs are skipped if absent.
imageio = install_and_import("imageio")
install_and_import("imageio_ffmpeg", "imageio-ffmpeg")
if _IS_MAIN_PROCESS:
    print("Dependencies ready.")

if PIL:
    from PIL import Image, ImageTk
    # Don't choke on very large photos
    Image.MAX_IMAGE_PIXELS = None
if pillow_heif:
    try:
        pillow_heif.register_heif_opener()
    except Exception as e:
        print(f"Could not register HEIF opener: {e}")

HEIC_OK = pillow_heif is not None
VIDEO_OK = imageio is not None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp", ".gif"}
HEIC_EXTS = {".heic", ".heif"}
STILL_EXTS = IMAGE_EXTS | HEIC_EXTS          # things that can pair with a Live Photo MOV
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}

# Strictness -> (dhash threshold, extra slack allowed on average_hash)
STRICTNESS = {
    "Strict":  3,
    "Medium":  8,
    "Loose":   14,
}


# --- DECODING & HASHING -----------------------------------------------------
def _load_video_frame(path):
    """Return a PIL image of a representative frame from a video, or None."""
    if not VIDEO_OK:
        return None
    reader = None
    try:
        reader = imageio.get_reader(path)  # ffmpeg backend
        frame = None
        for idx in (10, 0):                # frame 0 is often black; try ~1/3 sec in first
            try:
                frame = reader.get_data(idx)
                break
            except Exception:
                continue
        if frame is None:
            return None
        return Image.fromarray(frame).convert("RGB")
    except Exception:
        return None
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass


def load_image(path):
    """Decode any supported file to a PIL RGB image, or None if unsupported/broken."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in VIDEO_EXTS:
            return _load_video_frame(path)
        img = Image.open(path)
        return img.convert("RGB")
    except Exception:
        return None


def _hash_worker(path):
    """Runs in a worker PROCESS: decode + perceptual-hash one file.
    Returns (path, dhash_hex, ahash_hex, w, h, size); hashes are None on failure.
    Returns plain strings/ints only, so results pickle cheaply between processes."""
    img = load_image(path)
    if img is None:
        return (path, None, None, 0, 0, 0)
    try:
        dh = imagehash.dhash(img, hash_size=8)
        ah = imagehash.average_hash(img, hash_size=8)
        w, h = img.size
    except Exception:
        return (path, None, None, 0, 0, 0)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return (path, str(dh), str(ah), w, h, size)


def _record_from_hex(path, dhash_hex, ahash_hex, w, h, size):
    """Rebuild an in-memory record (with ImageHash objects) from hex strings."""
    return {
        "path": path,
        "dhash": imagehash.hex_to_hash(dhash_hex),
        "ahash": imagehash.hex_to_hash(ahash_hex),
        "w": w, "h": h, "size": size, "partner": None,
    }


# --- PERSISTENT HASH CACHE --------------------------------------------------
# Maps abspath(lower) -> (mtime_ns, size, dhash_hex, ahash_hex, w, h). A file is
# a cache hit only if its current mtime + size still match, so edited files are
# automatically re-hashed. Bump CACHE_VERSION if the hash params ever change.
CACHE_VERSION = 1


def cache_file_path():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "image_visual_dedup_cache.pkl")


def load_cache():
    try:
        with open(cache_file_path(), "rb") as f:
            data = pickle.load(f)
        if data.get("version") == CACHE_VERSION:
            return data.get("entries", {})
    except Exception:
        pass
    return {}


def save_cache(entries):
    try:
        tmp = cache_file_path() + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"version": CACHE_VERSION, "entries": entries}, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cache_file_path())
    except Exception as e:
        print(f"Could not save cache: {e}")


# --- BK-TREE (fast near-neighbour search on Hamming distance) ---------------
class BKTree:
    """Metric tree for finding all items within a Hamming radius. Distance is
    taken between the stored ImageHash objects (a - b == hamming distance)."""
    def __init__(self, records):
        self.records = records
        self.root = None  # (index, {dist: child_node})
        for i in range(len(records)):
            self._add(i)

    def _dist(self, i, j):
        return self.records[i]["dhash"] - self.records[j]["dhash"]

    def _add(self, idx):
        if self.root is None:
            self.root = (idx, {})
            return
        node = self.root
        while True:
            parent, children = node
            d = self._dist(idx, parent)
            child = children.get(d)
            if child is None:
                children[d] = (idx, {})
                return
            node = child

    def query(self, idx, threshold):
        """Return indices whose dhash is within `threshold` of records[idx]."""
        if self.root is None:
            return []
        out = []
        stack = [self.root]
        while stack:
            parent, children = stack.pop()
            d = self._dist(idx, parent)
            if d <= threshold:
                out.append(parent)
            lo, hi = d - threshold, d + threshold
            for dist, child in children.items():
                if lo <= dist <= hi:
                    stack.append(child)
        return out


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# --- SCANNER ----------------------------------------------------------------
class Scanner(threading.Thread):
    def __init__(self, folders, threshold, cache, on_progress, on_done, on_error):
        super().__init__(daemon=True)
        self.folders = folders
        self.threshold = threshold
        self.cache = cache                       # shared persistent hash cache (dict)
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error
        self.stop_event = threading.Event()
        try:
            self.max_workers = (os.cpu_count() or 4)
        except Exception:
            self.max_workers = 4

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.on_error(f"Scan failed: {e}")

    def _run(self):
        # 1. Walk and collect files
        self.on_progress("Listing files...", 0)
        stills, videos = [], []
        for folder in self.folders:
            for root, _, files in os.walk(folder):
                if self.stop_event.is_set():
                    return
                for name in files:
                    ext = os.path.splitext(name)[1].lower()
                    full = os.path.join(root, name)
                    if ext in STILL_EXTS:
                        stills.append(full)
                    elif ext in VIDEO_EXTS:
                        videos.append(full)

        # 2. Pair Live Photo MOVs with same-folder, same-stem stills
        still_by_key = {}   # (folder_lower, stem_lower) -> still path
        for s in stills:
            key = (os.path.dirname(s).lower(), os.path.splitext(os.path.basename(s))[0].lower())
            still_by_key.setdefault(key, s)

        partner_of_still = {}   # still path -> partner mov path
        orphan_videos = []
        for v in videos:
            key = (os.path.dirname(v).lower(), os.path.splitext(os.path.basename(v))[0].lower())
            partner_still = still_by_key.get(key)
            if partner_still:
                partner_of_still[partner_still] = v          # MOV rides with its still
            else:
                orphan_videos.append(v)                       # standalone clip -> hash a frame

        to_hash = stills + orphan_videos
        if not to_hash:
            self.on_done([], {"files": 0, "movs_paired": 0, "groups": 0, "cached": 0})
            return

        # 3. Split into cache hits vs files that need (re)hashing
        records = []
        compute = []   # (path, key, mtime_ns, size) for files not in cache
        for path in to_hash:
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = os.path.abspath(path).lower()
            ce = self.cache.get(key)
            if ce and ce[0] == st.st_mtime_ns and ce[1] == st.st_size and ce[2] and ce[3]:
                records.append(_record_from_hex(path, ce[2], ce[3], ce[4], ce[5], st.st_size))
            else:
                compute.append((path, key, st.st_mtime_ns, st.st_size))

        cached_n = len(records)
        if compute:
            new_records = self._hash_many(compute, cached_n)
            if new_records is None:      # cancelled mid-hash
                return
            records.extend(new_records)
            save_cache(self.cache)

        # Attach Live Photo MOV partners now that we have the full record list
        for r in records:
            r["partner"] = partner_of_still.get(r["path"])

        # 4. Cluster by Hamming distance (dhash within threshold AND ahash within slack)
        self.on_progress("Grouping similar images...", 92)
        n = len(records)
        tree = BKTree(records)
        uf = UnionFind(n)
        a_slack = self.threshold + 4
        for i in range(n):
            if self.stop_event.is_set():
                return
            for j in tree.query(i, self.threshold):
                if i == j:
                    continue
                if (records[i]["ahash"] - records[j]["ahash"]) <= a_slack:
                    uf.union(i, j)

        groups = {}
        for i in range(n):
            groups.setdefault(uf.find(i), []).append(records[i])
        clusters = [g for g in groups.values() if len(g) > 1]

        # Sort each group best-first (highest resolution, then largest file)
        for g in clusters:
            g.sort(key=lambda r: (r["w"] * r["h"], r["size"]), reverse=True)
        # Biggest groups first
        clusters.sort(key=len, reverse=True)

        stats = {"files": n, "movs_paired": len(partner_of_still),
                 "groups": len(clusters), "cached": cached_n}
        self.on_done(clusters, stats)

    def _hash_many(self, compute, cached_n):
        """Decode + hash the `compute` files using a process pool across all CPU
        cores (falls back to a thread pool if processes can't start). Updates the
        shared cache as it goes. Returns a list of records, or None if cancelled."""
        total = len(compute)

        def drain(executor_factory):
            recs = []
            done = 0
            with executor_factory() as pool:
                futs = {pool.submit(_hash_worker, c[0]): c for c in compute}
                for fut in concurrent.futures.as_completed(futs):
                    if self.stop_event.is_set():
                        pool.shutdown(wait=False, cancel_futures=True)
                        return None
                    path, dhx, ahx, w, h, size = fut.result()
                    c = futs[fut]
                    if dhx and ahx:
                        self.cache[c[1]] = (c[2], c[3], dhx, ahx, w, h)
                        recs.append(_record_from_hex(path, dhx, ahx, w, h, size))
                    done += 1
                    if done % 25 == 0 or done == total:
                        self.on_progress(f"Hashing {done}/{total} new (+{cached_n} cached)...",
                                         (done / total) * 90)
            return recs

        try:
            return drain(lambda: concurrent.futures.ProcessPoolExecutor(max_workers=self.max_workers))
        except Exception as e:
            # Process pool unavailable (e.g. frozen exe) — fall back to threads.
            # Any cache entries already written by the process attempt are kept.
            print(f"Process pool failed ({e}); using threads instead.")
            return drain(lambda: concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers))


# --- GUI --------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("Visual Duplicate Image Finder (review-only)")
        root.geometry("1150x820")

        self.folders = []
        self.protected = []
        self.clusters = []
        self._photo = None
        self._preview_path = None
        self.scanner = None
        self.cache = load_cache()   # persistent hash cache — makes re-scans near-instant

        # --- Folders ---
        top = tk.LabelFrame(root, text="Folders to scan", padx=8, pady=6)
        top.pack(fill=tk.X, padx=10, pady=(10, 4))
        self.lst_folders = tk.Listbox(top, height=3, selectmode=tk.EXTENDED, bg="#e3f2fd")
        self.lst_folders.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        fb = tk.Frame(top)
        fb.pack(side=tk.RIGHT)
        tk.Button(fb, text="Add...", width=9, command=self.add_folder).pack(pady=2)
        tk.Button(fb, text="Remove", width=9, command=self.remove_folder).pack(pady=2)

        # --- Protected ---
        prot = tk.LabelFrame(root, text="Protected folders (never selected for deletion)",
                             fg="#388e3c", padx=8, pady=6)
        prot.pack(fill=tk.X, padx=10, pady=4)
        self.lst_prot = tk.Listbox(prot, height=2, selectmode=tk.EXTENDED, bg="#e8f5e9")
        self.lst_prot.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        pb = tk.Frame(prot)
        pb.pack(side=tk.RIGHT)
        tk.Button(pb, text="Add...", width=9, command=self.add_protected).pack(pady=2)
        tk.Button(pb, text="Remove", width=9, command=self.remove_protected).pack(pady=2)

        # --- Options / scan ---
        opt = tk.Frame(root)
        opt.pack(fill=tk.X, padx=10, pady=4)
        tk.Label(opt, text="Match strictness:", font=("Arial", 9, "bold")).pack(side=tk.LEFT)
        self.strictness = ttk.Combobox(opt, values=list(STRICTNESS.keys()), state="readonly", width=10)
        self.strictness.set("Strict")
        self.strictness.pack(side=tk.LEFT, padx=5)
        self.strictness.bind("<<ComboboxSelected>>", self._sync_threshold)
        tk.Label(opt, text="Distance:").pack(side=tk.LEFT, padx=(8, 2))
        self.sp_thresh = tk.Spinbox(opt, from_=0, to=32, width=4)
        self.sp_thresh.delete(0, tk.END)
        self.sp_thresh.insert(0, str(STRICTNESS["Strict"]))
        self.sp_thresh.pack(side=tk.LEFT)
        tk.Label(opt, text="(lower = stricter)", fg="gray").pack(side=tk.LEFT, padx=4)

        caps = []
        if not HEIC_OK:
            caps.append("HEIC unavailable")
        if not VIDEO_OK:
            caps.append("MOV/video unavailable")
        cap_txt = ("  ⚠ " + ", ".join(caps)) if caps else "  HEIC + MOV supported"
        tk.Label(opt, text=cap_txt, fg=("#d32f2f" if caps else "#388e3c")).pack(side=tk.LEFT, padx=8)

        self.btn_scan = tk.Button(opt, text="SCAN", bg="#4caf50", fg="white",
                                  font=("Arial", 10, "bold"), width=12, command=self.start_scan)
        self.btn_scan.pack(side=tk.RIGHT)
        self.btn_clear_cache = tk.Button(opt, text="Clear Cache", command=self.clear_cache)
        self.btn_clear_cache.pack(side=tk.RIGHT, padx=(0, 8))

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=10, pady=(4, 2))
        self.lbl_stat = tk.Label(root, text="Add folders and Scan. Matches are review-only — nothing is auto-deleted.",
                                 fg="gray")
        self.lbl_stat.pack()

        # --- Results: tree + preview ---
        paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        left = tk.Frame(paned)
        self.tree = ttk.Treeview(left, columns=("name", "res", "size", "folder", "path"),
                                 show="tree headings", selectmode="extended")
        self.tree.heading("name", text="File")
        self.tree.heading("res", text="Resolution")
        self.tree.heading("size", text="Size")
        self.tree.heading("folder", text="Folder")
        self.tree.heading("path", text="Path")
        self.tree.column("#0", width=28)
        self.tree.column("name", width=240)
        self.tree.column("res", width=90)
        self.tree.column("size", width=80)
        self.tree.column("folder", width=200)
        self.tree.column("path", width=0, stretch=False)
        sy = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._open_file)
        self.tree.tag_configure("keep", foreground="#2e7d32")
        self.tree.tag_configure("dupe", foreground="#666666")
        self.tree.tag_configure("protected", foreground="#d32f2f", font=("Arial", 9, "bold"))
        paned.add(left, weight=3)

        pv = tk.LabelFrame(paned, text="Preview", padx=5, pady=5)
        self.lbl_img = tk.Label(pv, text="Select a file", fg="gray", bg="#f5f5f5")
        self.lbl_img.pack(fill=tk.BOTH, expand=True)
        self.lbl_info = tk.Label(pv, text="", font=("Arial", 8), fg="#555",
                                 wraplength=320, justify=tk.LEFT)
        self.lbl_info.pack(fill=tk.X, pady=(5, 0))
        paned.add(pv, weight=1)

        # --- Actions ---
        act = tk.Frame(root, pady=6)
        act.pack(fill=tk.X, padx=10)
        tk.Button(act, text="Trash Selected", bg="#ffcdd2", command=self.trash_selected).pack(side=tk.RIGHT, padx=4)
        tk.Button(act, text="Select All But Best", bg="#c8e6c9",
                  command=self.select_all_but_best).pack(side=tk.RIGHT, padx=4)
        tk.Label(act, text="Live Photo .MOV partners are trashed together with their still.",
                 fg="#777").pack(side=tk.LEFT)

        self._sync_threshold()

    # --- folder lists ---
    def add_folder(self):
        d = filedialog.askdirectory()
        if d and d not in self.lst_folders.get(0, tk.END):
            self.lst_folders.insert(tk.END, d)

    def remove_folder(self):
        for i in reversed(self.lst_folders.curselection()):
            self.lst_folders.delete(i)

    def add_protected(self):
        d = filedialog.askdirectory(title="Protect folder")
        if d and d not in self.lst_prot.get(0, tk.END):
            self.lst_prot.insert(tk.END, d)

    def remove_protected(self):
        for i in reversed(self.lst_prot.curselection()):
            self.lst_prot.delete(i)

    def _sync_threshold(self, event=None):
        val = STRICTNESS.get(self.strictness.get())
        if val is not None:
            self.sp_thresh.delete(0, tk.END)
            self.sp_thresh.insert(0, str(val))

    def clear_cache(self):
        """Discard all cached file hashes and delete the cache file from disk.
        The next scan will re-hash everything from scratch."""
        n = len(self.cache)
        if n and not messagebox.askyesno(
                "Clear cache",
                f"Discard {n} cached file hash(es)?\nThe next scan will re-hash everything."):
            return
        self.cache.clear()
        try:
            cf = cache_file_path()
            if os.path.exists(cf):
                os.remove(cf)
        except Exception as e:
            print(f"Could not delete cache file: {e}")
        self.lbl_stat.config(text=f"Cache cleared ({n} entries). Next scan will rebuild it.", fg="gray")

    def is_protected(self, path):
        p = os.path.abspath(path).lower()
        for prot in self.protected:
            pr = os.path.abspath(prot).lower()
            if p == pr or p.startswith(pr + os.sep):
                return True
        return False

    # --- scanning ---
    def start_scan(self):
        if not (PIL and imagehash):
            return messagebox.showerror("Missing deps", "Pillow and imagehash are required.")
        folders = list(self.lst_folders.get(0, tk.END))
        if not folders:
            return messagebox.showerror("Error", "Add at least one folder to scan.")
        try:
            threshold = int(self.sp_thresh.get())
        except ValueError:
            threshold = STRICTNESS["Strict"]
        self.protected = list(self.lst_prot.get(0, tk.END))

        self._clear_tree()
        self.btn_scan.config(state=tk.DISABLED)
        self.scanner = Scanner(folders, threshold, self.cache,
                               self._progress, self._done, self._error)
        self.scanner.start()

    def _progress(self, msg, pct):
        self.root.after(0, lambda: [self.lbl_stat.config(text=msg), self.progress.configure(value=pct)])

    def _error(self, msg):
        def show():
            self.lbl_stat.config(text=msg, fg="red")
            self.btn_scan.config(state=tk.NORMAL)
            messagebox.showerror("Scan error", msg)
        self.root.after(0, show)

    def _done(self, clusters, stats):
        self.root.after(0, lambda: self._populate(clusters, stats))

    def _populate(self, clusters, stats):
        self.clusters = clusters
        self._clear_tree()
        for gi, group in enumerate(clusters):
            best = group[0]
            grp = self.tree.insert("", "end",
                                   values=(f"[GROUP] {len(group)} similar", f"{best['w']}x{best['h']}",
                                           "", "", ""), open=True)
            for i, rec in enumerate(group):
                path = rec["path"]
                protected = self.is_protected(path)
                name = os.path.basename(path)
                if rec.get("partner"):
                    name += "  +LivePhoto"
                if protected:
                    tag, prefix = "protected", "PROTECTED  "
                elif i == 0:
                    tag, prefix = "keep", "KEEP  "
                else:
                    tag, prefix = "dupe", ""
                self.tree.insert(grp, "end", tags=(tag,),
                                 values=(prefix + name, f"{rec['w']}x{rec['h']}",
                                         _fmt_size(rec["size"]),
                                         os.path.basename(os.path.dirname(path)), path))
        self.progress.configure(value=100)
        self.btn_scan.config(state=tk.NORMAL)
        cached = stats.get("cached", 0)
        if clusters:
            self.lbl_stat.config(
                text=f"{stats['groups']} similar group(s) from {stats['files']} files "
                     f"({stats['movs_paired']} Live Photo MOVs paired, {cached} from cache). "
                     f"Review, then Trash Selected.",
                fg="#d32f2f")
        else:
            self.lbl_stat.config(
                text=f"No visual duplicates found among {stats['files']} files ({cached} from cache).",
                fg="green")

    def _clear_tree(self):
        for x in self.tree.get_children():
            self.tree.delete(x)

    # --- preview ---
    def _on_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        path = vals[-1] if vals else ""
        if not path or not os.path.isfile(path):
            self.lbl_img.config(image="", text="Select a file", fg="gray")
            self.lbl_info.config(text="")
            self._photo = None
            return
        self._preview_path = path
        img = load_image(path)
        if img is None:
            self.lbl_img.config(image="", text="No preview", fg="red")
        else:
            try:
                img.thumbnail((360, 360), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(img)
                self.lbl_img.config(image=self._photo, text="")
            except Exception:
                self.lbl_img.config(image="", text="No preview", fg="red")
        try:
            size = _fmt_size(os.path.getsize(path))
        except OSError:
            size = "?"
        self.lbl_info.config(text=f"{os.path.basename(path)}\n{size}\n{path}")

    def _open_file(self, event=None):
        if self._preview_path and os.path.exists(self._preview_path):
            try:
                if platform.system() == "Windows":
                    os.startfile(self._preview_path)
                elif platform.system() == "Darwin":
                    subprocess.call(["open", self._preview_path])
                else:
                    subprocess.call(["xdg-open", self._preview_path])
            except Exception:
                pass

    # --- selection / deletion ---
    def select_all_but_best(self):
        self.tree.selection_remove(*self.tree.selection())
        pick = []
        for grp in self.tree.get_children():
            children = self.tree.get_children(grp)
            for child in children[1:]:           # skip best/keeper
                vals = self.tree.item(child, "values")
                path = vals[-1] if vals else ""
                if path and not self.is_protected(path):
                    pick.append(child)
        if pick:
            self.tree.selection_set(*pick)
            self.lbl_stat.config(text=f"Selected {len(pick)} non-best files (review before trashing).")

    def trash_selected(self):
        if send2trash is None:
            return messagebox.showerror("Missing dep", "send2trash is required to delete.")
        sel = self.tree.selection()
        paths, partners, item_ids, skipped = [], [], [], 0
        # Map a path back to its record so we can find Live Photo partners
        rec_by_path = {r["path"]: r for g in self.clusters for r in g}
        for iid in sel:
            vals = self.tree.item(iid, "values")
            path = vals[-1] if vals else ""
            if not path or not os.path.isfile(path):
                continue
            if self.is_protected(path):
                skipped += 1
                continue
            paths.append(path)
            item_ids.append(iid)
            rec = rec_by_path.get(path)
            if rec and rec.get("partner") and os.path.isfile(rec["partner"]):
                partners.append(rec["partner"])

        if skipped:
            messagebox.showinfo("Protected", f"Skipped {skipped} file(s) in protected folders.")
        if not paths:
            return
        extra = f"\n(+ {len(partners)} Live Photo .MOV partner(s))" if partners else ""
        if not messagebox.askyesno("Trash", f"Send {len(paths)} file(s) to the Recycle Bin?{extra}"):
            return

        trashed, failed = 0, 0
        for p in paths + partners:
            try:
                send2trash.send2trash(p)
                trashed += 1
            except Exception as e:
                print(f"Could not trash {p}: {e}")
                failed += 1
        for iid in item_ids:
            try:
                self.tree.delete(iid)
            except Exception:
                pass
        msg = f"Trashed {trashed} file(s)."
        if failed:
            msg += f" {failed} failed."
        self.lbl_stat.config(text=msg, fg="green" if not failed else "orange")


def _fmt_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


if __name__ == "__main__":
    multiprocessing.freeze_support()   # required for process pools under Windows / frozen exes
    root = tk.Tk()
    try:
        app = App(root)
    except Exception:
        import traceback
        err = traceback.format_exc()
        traceback.print_exc()
        try:
            messagebox.showerror("Startup Error", err)
        except Exception:
            pass
        sys.exit(1)
    # Persist the hash cache on close so the next run starts warm.
    root.protocol("WM_DELETE_WINDOW", lambda: (save_cache(app.cache), root.destroy()))
    root.mainloop()
