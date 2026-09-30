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
        self.trash_vars = {}
        self.selected_paths = set()
        self.group_views = []
        self.group_page = 0
        self.group_page_ranges = []
        self.card_frames = {}
        self.all_groups_expanded = False
        self.gallery_page_size = 24
        self.gallery_generation = 0
        self._thumb_future = None
        self._active_thumb_job = None
        self._gallery_refresh_job = None
        self.preview_visible = True
        # One background page job at a time; that job uses a small bounded
        # worker pool so decoding is parallel without multiplying memory use.
        self.thumbnail_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._photo = None
        self._preview_path = None
        self.scanner = None
        self.trash_worker = None
        self._close_after_trash = False
        self.trash_cancel = threading.Event()
        self.trash_progress_win = None
        self.trash_progress_bar = None
        self.trash_progress_label = None
        self.btn_trash = None
        self.btn_select_all = None
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
        self.lbl_stat = tk.Label(root, text="Add folders and Scan. Compare each photo group, then select copies to trash.",
                                 fg="gray")
        self.lbl_stat.pack()

        # --- Results: grouped photo gallery + preview ---
        self.paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        left = tk.Frame(self.paned)
        gallery_tools = tk.Frame(left)
        gallery_tools.pack(fill=tk.X, padx=4, pady=(0, 4))
        tk.Button(gallery_tools, text="Expand This Page", command=self.expand_all_groups).pack(side=tk.LEFT)
        tk.Button(gallery_tools, text="Collapse This Page", command=self.collapse_all_groups).pack(
            side=tk.LEFT, padx=4)
        self.preview_button = tk.Button(gallery_tools, text="Hide Preview", command=self.toggle_preview)
        self.preview_button.pack(side=tk.LEFT, padx=4)
        group_nav = tk.Frame(gallery_tools)
        group_nav.pack(side=tk.RIGHT)
        self.prev_groups_button = tk.Button(group_nav, text="◀ Groups", command=self.previous_group_page)
        self.prev_groups_button.pack(side=tk.LEFT)
        self.group_page_label = tk.Label(group_nav, text="No groups")
        self.group_page_label.pack(side=tk.LEFT, padx=6)
        self.next_groups_button = tk.Button(group_nav, text="Groups ▶", command=self.next_group_page)
        self.next_groups_button.pack(side=tk.LEFT)
        self.results_canvas = tk.Canvas(left, highlightthickness=0)
        sy = ttk.Scrollbar(left, orient="vertical", command=self._scroll_gallery)
        self.results_canvas.configure(yscrollcommand=sy.set)
        self.results_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy.pack(side=tk.RIGHT, fill=tk.Y)
        self.results_inner = tk.Frame(self.results_canvas)
        self.results_window = self.results_canvas.create_window(
            (0, 0), window=self.results_inner, anchor="nw")
        self.results_inner.bind("<Configure>", self._on_gallery_configure)
        self.results_canvas.bind("<Configure>", self._on_gallery_resize)
        self.results_canvas.bind_all("<MouseWheel>", self._on_gallery_wheel)
        self.paned.add(left, weight=3)

        self.preview_panel = tk.Frame(self.paned)
        tk.Label(self.preview_panel, text="Preview", font=("Arial", 10, "bold")).pack(anchor="w", padx=6, pady=4)
        self.lbl_img = tk.Label(self.preview_panel, text="Select a file", fg="gray", bg="#f5f5f5")
        self.lbl_img.pack(fill=tk.BOTH, expand=True, padx=5)
        self.lbl_info = tk.Label(self.preview_panel, text="", font=("Arial", 8), fg="#555",
                                 wraplength=320, justify=tk.LEFT)
        self.lbl_info.pack(fill=tk.X, padx=5, pady=(5, 0))
        self.paned.add(self.preview_panel, weight=1)

        # --- Actions ---
        act = tk.Frame(root, pady=6)
        act.pack(fill=tk.X, padx=10)
        self.btn_trash = tk.Button(act, text="Trash Selected", bg="#ffcdd2", command=self.trash_selected)
        self.btn_trash.pack(side=tk.RIGHT, padx=4)
        self.btn_select_all = tk.Button(act, text="Select All But Best", bg="#c8e6c9",
                                        command=self.select_all_but_best)
        self.btn_select_all.pack(side=tk.RIGHT, padx=4)
        tk.Label(act, text="Expand groups and scroll; thumbnails load as needed (24 per page).",
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

        self.clusters = []
        self.selected_paths.clear()
        self._build_group_list(open_first=False)
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
        # A protected copy always wins keeper priority, even when another copy
        # has a higher resolution or larger file size. Within the same
        # protection status, prefer higher resolution and then larger size.
        for group in clusters:
            group.sort(
                key=lambda rec: (self.is_protected(rec["path"]),
                                 rec["w"] * rec["h"], rec["size"]),
                reverse=True,
            )
        self.clusters = clusters
        self.selected_paths.clear()
        self._build_group_list(open_first=True)
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

    def _compute_group_page_ranges(self):
        # Keep each embedded Tk canvas segment comfortably below Tk's size limit.
        # Estimate the worst expanded height for the first thumbnail page of each group.
        ranges = []
        start = 0
        used_height = 0
        height_limit = 20000
        for index, group in enumerate(self.clusters):
            rows = (min(len(group), self.gallery_page_size) + 3) // 4
            estimated = 100 + rows * 300
            if index > start and used_height + estimated > height_limit:
                ranges.append((start, index))
                start = index
                used_height = 0
            used_height += estimated
        if start < len(self.clusters):
            ranges.append((start, len(self.clusters)))
        return ranges

    def _build_group_list(self, open_first=False):
        self.gallery_generation += 1
        if self._thumb_future is not None:
            self._thumb_future.cancel()
            self._thumb_future = None
        self._active_thumb_job = None
        self.trash_vars.clear()
        self.card_frames.clear()
        self.group_views = []
        self.all_groups_expanded = False
        self._clear_gallery()
        self.group_page_ranges = self._compute_group_page_ranges()
        page_count = max(1, len(self.group_page_ranges))
        self.group_page = min(self.group_page, page_count - 1)
        if self.group_page_ranges:
            start, end = self.group_page_ranges[self.group_page]
            for cluster_index in range(start, end):
                group = self.clusters[cluster_index]
                group_frame = tk.Frame(self.results_inner, bd=1, relief=tk.GROOVE, padx=5, pady=4)
                group_frame.pack(fill=tk.X, padx=6, pady=4)
                header = tk.Button(
                    group_frame,
                    text=f"▶ Group {cluster_index + 1} · {len(group)} similar photos",
                    anchor="w",
                    command=lambda i=len(self.group_views): self._toggle_gallery_group(i),
                )
                header.pack(fill=tk.X)
                content = tk.Frame(group_frame)
                view = {"frame": group_frame, "header": header, "content": content,
                        "records": group, "cluster_index": cluster_index,
                        "expanded": False, "page": 0, "loading": False, "loaded": False,
                        "token": 0, "body": None, "photo_refs": [], "page_paths": [],
                        "reserved_height": 0}
                self.group_views.append(view)
        if self.clusters:
            start, end = self.group_page_ranges[self.group_page]
            self.group_page_label.config(
                text=f"Groups {start + 1}–{end} of {len(self.clusters)} · page {self.group_page + 1}/{page_count}")
        else:
            self.group_page_label.config(text="No groups")
        self.prev_groups_button.config(state=tk.NORMAL if self.group_page > 0 else tk.DISABLED)
        self.next_groups_button.config(state=tk.NORMAL if self.group_page + 1 < page_count else tk.DISABLED)
        if open_first and self.group_views:
            self._set_group_expanded(0, True)

    def previous_group_page(self):
        if self.group_page > 0:
            self.group_page -= 1
            self.results_canvas.yview_moveto(0)
            self._build_group_list(open_first=False)

    def next_group_page(self):
        if self.group_page + 1 < len(self.group_page_ranges):
            self.group_page += 1
            self.results_canvas.yview_moveto(0)
            self._build_group_list(open_first=False)

    def _clear_gallery(self):
        for child in self.results_inner.winfo_children():
            child.destroy()

    def _toggle_gallery_group(self, index):
        self._set_group_expanded(index, not self.group_views[index]["expanded"])
        self.all_groups_expanded = bool(self.group_views) and all(
            view["expanded"] for view in self.group_views)

    def _set_group_expanded(self, index, expanded):
        view = self.group_views[index]
        view["expanded"] = expanded
        if expanded:
            view["header"].config(text=f"▼ Group {view['cluster_index'] + 1} · {len(view['records'])} similar photos")
            view["content"].pack(fill=tk.X, padx=4, pady=(4, 0))
            if view["body"] is None:
                self._show_group_placeholder(view, "Scroll into view to load thumbnails")
            self._schedule_gallery_refresh()
        else:
            view["header"].config(text=f"▶ Group {view['cluster_index'] + 1} · {len(view['records'])} similar photos")
            view["content"].pack_forget()
            view["token"] += 1
            if self._active_thumb_job and self._active_thumb_job[0] == index and self._thumb_future:
                self._thumb_future.cancel()
            self._discard_group_images(view, keep_space=False)
            for child in view["content"].winfo_children():
                child.destroy()
            view["body"] = None
            view["loaded"] = False
            view["loading"] = False

    def expand_all_groups(self):
        self.all_groups_expanded = True
        for index in range(len(self.group_views)):
            self._set_group_expanded(index, True)
        self._schedule_gallery_refresh()

    def collapse_all_groups(self):
        self.all_groups_expanded = False
        for index in range(len(self.group_views)):
            self._set_group_expanded(index, False)

    def toggle_preview(self):
        if self.preview_visible:
            self.paned.forget(self.preview_panel)
            self.preview_button.config(text="Show Preview")
            self.preview_visible = False
        else:
            self.paned.add(self.preview_panel, weight=1)
            self.preview_button.config(text="Hide Preview")
            self.preview_visible = True
        self._schedule_gallery_refresh()

    def _show_group_placeholder(self, view, text):
        content = view["content"]
        for child in content.winfo_children():
            child.destroy()
        tk.Label(content, text=text, fg="#666", anchor="w").pack(fill=tk.X, padx=6, pady=4)
        view["body"] = None
        view["loaded"] = False

    def _discard_group_images(self, view, keep_space=True):
        body = view.get("body")
        if body is None:
            return
        if keep_space:
            try:
                view["reserved_height"] = max(view["reserved_height"], body.winfo_height())
            except Exception:
                pass
        for path in view.get("page_paths", []):
            self.trash_vars.pop(path, None)
            self.card_frames.pop(path, None)
        view["page_paths"] = []
        view["photo_refs"] = []
        for child in body.winfo_children():
            child.destroy()
        if keep_space and view["reserved_height"] > 0:
            body.configure(height=view["reserved_height"])
            body.pack_propagate(False)
            tk.Label(body, text="Thumbnails unloaded to save memory · scroll back to reload",
                     fg="#777").place(relx=0.5, rely=0.5, anchor="center")
        else:
            body.destroy()
            view["body"] = None
        view["loaded"] = False
        view["loading"] = False

    def _group_is_visible(self, view):
        if not view["expanded"] or not view["frame"].winfo_ismapped():
            return False
        canvas_top = self.results_canvas.winfo_rooty()
        canvas_bottom = canvas_top + self.results_canvas.winfo_height()
        group_top = view["frame"].winfo_rooty()
        group_bottom = group_top + view["frame"].winfo_height()
        return group_bottom > canvas_top and group_top < canvas_bottom

    def _schedule_gallery_refresh(self):
        if self._gallery_refresh_job is None:
            self._gallery_refresh_job = self.root.after_idle(self._refresh_visible_groups)

    def _refresh_visible_groups(self):
        self._gallery_refresh_job = None
        if not self.group_views:
            return
        for view in self.group_views:
            visible = self._group_is_visible(view)
            if not visible and view["loaded"]:
                self._discard_group_images(view, keep_space=True)
            elif not visible and view["loading"]:
                view["token"] += 1
                view["loading"] = False
        if self._thumb_future is not None:
            return
        for index, view in enumerate(self.group_views):
            if view["expanded"] and self._group_is_visible(view) and not view["loaded"] and not view["loading"]:
                self._load_visible_group(index)
                return

    @staticmethod
    def _load_thumbnail_batch(records):
        def load_one(rec):
            source = None
            thumb = None
            try:
                path = rec["path"]
                if os.path.splitext(path)[1].lower() in VIDEO_EXTS:
                    source = _load_video_frame(path)
                else:
                    source = Image.open(path)
                if source is not None:
                    source.thumbnail((140, 120), Image.LANCZOS)
                    thumb = source.convert("RGB")
            except Exception:
                thumb = None
            finally:
                if source is not None:
                    try:
                        source.close()
                    except Exception:
                        pass
            return rec, thumb

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, len(records) or 1)) as workers:
            return list(workers.map(load_one, records))

    def _render_gallery_page(self, group_index, page):
        view = self.group_views[group_index]
        view["token"] += 1
        self._discard_group_images(view, keep_space=False)
        view["page"] = page
        view["loading"] = False
        self._show_group_placeholder(view, "Scroll into view to load thumbnails")
        view["expanded"] = True
        view["header"].config(text=f"▼ Group {view['cluster_index'] + 1} · {len(view['records'])} similar photos")
        view["content"].pack(fill=tk.X, padx=4, pady=(4, 0))
        self._schedule_gallery_refresh()

    def _load_visible_group(self, group_index):
        view = self.group_views[group_index]
        group = view["records"]
        page = view["page"]
        start = page * self.gallery_page_size
        stop = min(start + self.gallery_page_size, len(group))
        records = group[start:stop]
        content = view["content"]
        for child in content.winfo_children():
            child.destroy()
        page_count = max(1, (len(group) + self.gallery_page_size - 1) // self.gallery_page_size)
        controls = tk.Frame(content)
        controls.pack(fill=tk.X, pady=(0, 5))
        tk.Button(controls, text="Previous", state=tk.NORMAL if page else tk.DISABLED,
                  command=lambda: self._render_gallery_page(group_index, page - 1)).pack(side=tk.LEFT)
        tk.Label(controls, text=f"Photos {start + 1}–{stop} of {len(group)} · page {page + 1}/{page_count}").pack(
            side=tk.LEFT, padx=8)
        tk.Button(controls, text="Next", state=tk.NORMAL if page + 1 < page_count else tk.DISABLED,
                  command=lambda: self._render_gallery_page(group_index, page + 1)).pack(side=tk.LEFT)
        body = tk.Frame(content)
        body.pack(fill=tk.X)
        view["body"] = body
        view["loading"] = True
        view["loaded"] = False
        view["token"] += 1
        token = view["token"]
        tk.Label(body, text="Loading thumbnails…", fg="#666").pack(pady=12)
        generation = self.gallery_generation
        future = self.thumbnail_pool.submit(self._load_thumbnail_batch, records)
        self._thumb_future = future
        self._active_thumb_job = (group_index, token)
        future.add_done_callback(
            lambda done, gi=group_index, pg=page, gen=generation, tok=token, frame=body:
                self._schedule_thumbnail_finish(gi, pg, gen, tok, frame, done))

    def _schedule_thumbnail_finish(self, group_index, page, generation, token, body, future):
        try:
            self.root.after(0, lambda: self._finish_thumbnail_batch(
                group_index, page, generation, token, body, future))
        except Exception:
            pass

    def _finish_thumbnail_batch(self, group_index, page, generation, token, body, future):
        try:
            batch = future.result()
        except Exception as e:
            batch = None
            error = e
        else:
            error = None
        if self._thumb_future is future:
            self._thumb_future = None
            self._active_thumb_job = None
        if (generation != self.gallery_generation or group_index >= len(self.group_views)):
            self._close_thumbnail_batch(batch)
            return
        view = self.group_views[group_index]
        if (view["token"] != token or not view["expanded"] or not self._group_is_visible(view)
                or not body.winfo_exists()):
            self._close_thumbnail_batch(batch)
            if view["token"] == token:
                view["loading"] = False
            self._schedule_gallery_refresh()
            return
        if error is not None:
            for child in body.winfo_children():
                child.destroy()
            tk.Label(body, text=f"Could not load thumbnails: {error}", fg="red").pack(pady=10)
            view["loading"] = False
            return
        for child in body.winfo_children():
            child.destroy()
        base_index = page * self.gallery_page_size
        view["photo_refs"] = []
        view["page_paths"] = []
        for offset, (rec, img) in enumerate(batch):
            path = rec["path"]
            absolute_index = base_index + offset
            protected = self.is_protected(path)
            card = tk.Frame(body, bd=1, relief=tk.GROOVE, padx=5, pady=5)
            card.grid(row=offset // 4, column=offset % 4, sticky="nsew", padx=4, pady=4)
            body.grid_columnconfigure(offset % 4, weight=1)
            if img is not None:
                try:
                    photo = ImageTk.PhotoImage(img)
                    view["photo_refs"].append(photo)
                    thumb = tk.Label(card, image=photo, cursor="hand2")
                except Exception:
                    thumb = tk.Label(card, text="Preview unavailable", width=18, height=7)
                finally:
                    try:
                        img.close()
                    except Exception:
                        pass
            else:
                thumb = tk.Label(card, text="Preview unavailable", width=18, height=7)
            thumb.pack(pady=(0, 4))
            thumb.bind("<Button-1>", lambda _e, p=path: self._show_preview(p))
            thumb.bind("<Double-Button-1>", lambda _e, p=path: self._open_file(p))

            if protected:
                label = "PROTECTED · KEEP" if absolute_index == 0 else "PROTECTED"
                tk.Label(card, text=label, fg="#d32f2f", font=("Arial", 9, "bold")).pack()
            elif absolute_index == 0:
                tk.Label(card, text="KEEP · suggested best", fg="#2e7d32",
                         font=("Arial", 9, "bold")).pack()
            else:
                var = tk.BooleanVar(value=path in self.selected_paths)
                self.trash_vars[path] = var
                tk.Checkbutton(card, text="Select to trash", variable=var,
                               command=lambda p=path, v=var: self._set_trash_selection(p, v)).pack()

            name = os.path.basename(path)
            if rec.get("partner"):
                name += "  + Live Photo MOV"
            tk.Label(card, text=name, wraplength=155, justify=tk.CENTER).pack()
            tk.Label(card, text=f"{rec['w']}×{rec['h']} · {_fmt_size(rec['size'])}",
                     fg="#666").pack()
            tk.Label(card, text=os.path.dirname(path), wraplength=155,
                     justify=tk.CENTER, fg="#777", font=("Arial", 7)).pack()
            self.card_frames[path] = card
            view["page_paths"].append(path)
        view["loading"] = False
        view["loaded"] = True
        view["reserved_height"] = max(view["reserved_height"], body.winfo_reqheight())
        self._schedule_gallery_refresh()

    @staticmethod
    def _close_thumbnail_batch(batch):
        for _, image in batch or []:
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass

    def _set_trash_selection(self, path, var):
        if var.get():
            self.selected_paths.add(path)
        else:
            self.selected_paths.discard(path)

    def _on_gallery_configure(self, event=None):
        self.results_canvas.configure(scrollregion=self.results_canvas.bbox("all"))
        self._schedule_gallery_refresh()

    def _on_gallery_resize(self, event):
        self.results_canvas.itemconfigure(self.results_window, width=event.width)
        self._schedule_gallery_refresh()

    def _scroll_gallery(self, *args):
        self.results_canvas.yview(*args)
        self._schedule_gallery_refresh()

    def _on_gallery_wheel(self, event):
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is self.results_canvas:
                self.results_canvas.yview_scroll(int(-event.delta / 120), "units")
                self._schedule_gallery_refresh()
                return
            widget = getattr(widget, "master", None)

    # --- preview ---
    def _show_preview(self, path):
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

    def _open_file(self, path=None):
        path = path or self._preview_path
        if path and os.path.exists(path):
            try:
                if platform.system() == "Windows":
                    os.startfile(path)
                elif platform.system() == "Darwin":
                    subprocess.call(["open", path])
                else:
                    subprocess.call(["xdg-open", path])
            except Exception:
                pass

    # --- selection / deletion ---
    def select_all_but_best(self):
        self.selected_paths = {
            rec["path"]
            for group in self.clusters
            for rec in group[1:]
            if not self.is_protected(rec["path"])
        }
        for path, var in self.trash_vars.items():
            var.set(path in self.selected_paths)
        self.lbl_stat.config(
            text=f"Selected {len(self.selected_paths)} non-best files (review before trashing).")

    def trash_selected(self):
        if send2trash is None:
            return messagebox.showerror("Missing dep", "send2trash is required to delete.")
        rec_by_path = {r["path"]: r for g in self.clusters for r in g}
        paths = [rec["path"] for group in self.clusters for rec in group
                 if rec["path"] in self.selected_paths]
        skipped = 0
        existing_paths = []
        for path in paths:
            if not os.path.isfile(path):
                continue
            if self.is_protected(path):
                skipped += 1
                continue
            existing_paths.append(path)
        paths = existing_paths

        if skipped:
            messagebox.showinfo("Protected", f"Skipped {skipped} file(s) in protected folders.")
        if not paths:
            return messagebox.showinfo("Nothing selected", "Check the photos you want to send to the Recycle Bin.")

        partners = []
        for path in paths:
            rec = rec_by_path.get(path)
            partner = rec.get("partner") if rec else None
            if (partner and os.path.isfile(partner) and not self.is_protected(partner)
                    and partner not in paths and partner not in partners):
                partners.append(partner)

        # Show every selected file, including folder locations and Live Photo
        # partners, so the user can review the exact operation before confirming.
        confirm = tk.Toplevel(self.root)
        confirm.title("Review files to send to the Recycle Bin")
        confirm.geometry("760x500")
        confirm.transient(self.root)
        confirm.grab_set()
        total = len(paths) + len(partners)
        tk.Label(confirm, text=f"Review {total} file(s) before sending them to the Recycle Bin.",
                 font=("Arial", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        list_frame = tk.Frame(confirm)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12)
        listing = ttk.Treeview(list_frame, columns=("kind", "name", "folder"), show="headings")
        listing.heading("kind", text="Type")
        listing.heading("name", text="File")
        listing.heading("folder", text="Folder")
        listing.column("kind", width=115, stretch=False)
        listing.column("name", width=250)
        listing.column("folder", width=360)
        listing.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listing.yview)
        listing.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        for path in paths:
            listing.insert("", "end", values=("Selected photo", os.path.basename(path),
                                                os.path.dirname(path)))
        for path in partners:
            listing.insert("", "end", values=("Live Photo MOV", os.path.basename(path),
                                                os.path.dirname(path)))
        confirmed = {"value": False}
        buttons = tk.Frame(confirm)
        buttons.pack(fill=tk.X, padx=12, pady=10)
        tk.Button(buttons, text="Cancel", command=confirm.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        tk.Button(buttons, text="Send to Recycle Bin", bg="#ffcdd2",
                  command=lambda: (confirmed.__setitem__("value", True), confirm.destroy())).pack(side=tk.RIGHT)
        self.root.wait_window(confirm)
        if not confirmed["value"]:
            return

        self._start_trash_batch(paths + partners)

    def _start_trash_batch(self, paths):
        self.trash_cancel.clear()
        total = len(paths)
        self.trash_progress_win = tk.Toplevel(self.root)
        self.trash_progress_win.title("Sending files to the Recycle Bin")
        self.trash_progress_win.geometry("480x150")
        self.trash_progress_win.transient(self.root)
        self.trash_progress_win.protocol("WM_DELETE_WINDOW", self._request_trash_cancel)
        self.trash_progress_label = tk.Label(
            self.trash_progress_win, text=f"Processed 0 of {total} files…")
        self.trash_progress_label.pack(anchor="w", padx=14, pady=(14, 6))
        self.trash_progress_bar = ttk.Progressbar(
            self.trash_progress_win, mode="determinate", maximum=total)
        self.trash_progress_bar.pack(fill=tk.X, padx=14, pady=6)
        self.trash_cancel_button = tk.Button(
            self.trash_progress_win, text="Cancel after current file", command=self._request_trash_cancel)
        self.trash_cancel_button.pack(anchor="e", padx=14, pady=8)
        self.btn_trash.config(state=tk.DISABLED)
        self.btn_select_all.config(state=tk.DISABLED)
        self.btn_scan.config(state=tk.DISABLED)
        self.trash_worker = threading.Thread(
            target=self._run_trash_batch, args=(list(paths),), daemon=True)
        self.trash_worker.start()

    def _request_trash_cancel(self):
        self.trash_cancel.set()
        if self.trash_progress_label is not None:
            self.trash_progress_label.config(text="Stopping after the current file…")
        if hasattr(self, "trash_cancel_button"):
            self.trash_cancel_button.config(state=tk.DISABLED)

    def _run_trash_batch(self, paths):
        failures = []
        trashed_paths = set()
        processed = 0
        for path in paths:
            if self.trash_cancel.is_set():
                break
            try:
                # Normalize Windows paths before the Recycle Bin API adds \\?\.
                trash_path = os.path.normpath(os.path.abspath(path))
                send2trash.send2trash(trash_path)
                trashed_paths.add(path)
            except Exception as e:
                failures.append((path, str(e)))
            processed += 1
            if processed == 1 or processed % 10 == 0 or processed == len(paths):
                try:
                    self.root.after(0, lambda n=processed, sent=len(trashed_paths), total=len(paths):
                                    self._update_trash_progress(n, sent, total))
                except Exception:
                    pass
        cancelled = self.trash_cancel.is_set() and processed < len(paths)
        try:
            self.root.after(0, lambda: self._finish_trash_batch(
                paths, trashed_paths, failures, processed, cancelled))
        except Exception:
            pass

    def _update_trash_progress(self, processed, sent, total):
        if self.trash_progress_win is None or not self.trash_progress_win.winfo_exists():
            return
        self.trash_progress_bar.configure(value=processed)
        self.trash_progress_label.config(
            text=f"Processed {processed} of {total} files · sent {sent} to the Recycle Bin…")

    def _finish_trash_batch(self, paths, trashed_paths, failures, processed, cancelled):
        if self.trash_progress_win is not None:
            try:
                self.trash_progress_win.destroy()
            except Exception:
                pass
        self.trash_progress_win = None
        self.trash_progress_label = None
        self.trash_progress_bar = None
        self.trash_worker = None
        self.btn_trash.config(state=tk.NORMAL)
        self.btn_select_all.config(state=tk.NORMAL)
        self.btn_scan.config(state=tk.NORMAL)

        # Remove successful files from the results; failures and cancelled files
        # remain selected so the user can retry or adjust the selection.
        self.selected_paths.difference_update(trashed_paths)
        self.clusters = [[rec for rec in group if rec["path"] not in trashed_paths]
                         for group in self.clusters]
        self.clusters = [group for group in self.clusters if len(group) > 1]
        visible_paths = {rec["path"] for group in self.clusters for rec in group}
        self.selected_paths.intersection_update(visible_paths)
        self._build_group_list(open_first=False)

        msg = f"Sent {len(trashed_paths)} of {len(paths)} files to the Recycle Bin."
        if cancelled:
            msg += f" Stopped after {processed}; remaining files were left untouched."
        if failures:
            msg += f" {len(failures)} failed; see the error details."
        self.lbl_stat.config(text=msg, fg="orange" if cancelled or failures else "green")
        if failures:
            details = "\n\n".join(f"{path}\n  {error}" for path, error in failures[:8])
            if len(failures) > 8:
                details += f"\n\n…and {len(failures) - 8} more failure(s)."
            messagebox.showwarning("Could not send files to the Recycle Bin", details)
        if self._close_after_trash:
            self._close_after_trash = False
            self.close()

    def close(self):
        if self.trash_worker is not None and self.trash_worker.is_alive():
            self._close_after_trash = True
            self._request_trash_cancel()
            self.lbl_stat.config(text="Closing after the current file finishes…", fg="orange")
            return
        self.thumbnail_pool.shutdown(wait=False, cancel_futures=True)
        save_cache(self.cache)
        self.root.destroy()


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
    # Persist the cache and stop queued thumbnail work on close.
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
