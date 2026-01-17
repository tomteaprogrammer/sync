import os
import sys
import hashlib
import subprocess
import importlib.util
import threading
import platform
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --- 1. SETUP & UTILS ---
def install_and_import(package):
    if importlib.util.find_spec(package) is None:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        except: pass
    try: return importlib.import_module(package)
    except: return None

send2trash = install_and_import('send2trash')

def prepare_path(path):
    path = os.path.normpath(path)
    path = os.path.abspath(path)
    if platform.system() == 'Windows' and len(path) > 259 and not path.startswith('\\\\?\\'):
        path = '\\\\?\\' + path
    return path

def is_subpath(path, parent):
    try:
        path = os.path.abspath(path)
        parent = os.path.abspath(parent)
        return os.path.commonpath([parent, path]) == parent
    except ValueError:
        return False

def reveal_in_explorer(path):
    """Opens the folder and selects the file."""
    path = prepare_path(path)
    try:
        if platform.system() == 'Windows':
            subprocess.Popen(f'explorer /select,"{path}"')
        elif platform.system() == 'Darwin':
            subprocess.call(['open', '-R', path])
        else:
            subprocess.call(['xdg-open', os.path.dirname(path)])
    except Exception as e:
        print(f"Error opening folder: {e}")

# --- 2. LOGIC ENGINE ---
class Comparator(threading.Thread):
    def __init__(self, master_path, target_path, use_hash, callback_update, callback_finish):
        super().__init__()
        self.master = master_path
        self.target = target_path
        self.use_hash = use_hash
        self.update_ui = callback_update
        self.finish = callback_finish
        self.daemon = True

    def get_identifier(self, filepath):
        try:
            stat = os.stat(filepath)
            size = stat.st_size
            if not self.use_hash:
                return (os.path.basename(filepath), size)
            
            # Hash Mode
            hasher = hashlib.md5()
            with open(filepath, 'rb') as f:
                while True:
                    data = f.read(65536)
                    if not data: break
                    hasher.update(data)
            return (size, hasher.hexdigest())
        except: return None

    def index_folder(self, folder, label):
        index = {}
        all_files = []
        for root, _, files in os.walk(folder):
            for file in files:
                all_files.append(prepare_path(os.path.join(root, file)))
        
        total = len(all_files)
        for i, path in enumerate(all_files):
            if i % 50 == 0: self.update_ui(f"Scanning {label}: {i}/{total}", (i/total)*50)
            ident = self.get_identifier(path)
            if ident:
                if ident not in index: index[ident] = []
                index[ident].append(path)
        return index

    def run(self):
        self.update_ui("Indexing PROTECTED Folder (Master)...", 0)
        master_idx = self.index_folder(self.master, "Master")
        
        self.update_ui("Indexing CLEANUP Folder (Target)...", 50)
        target_idx = self.index_folder(self.target, "Target")
        
        self.update_ui("Comparing logic...", 90)
        
        duplicates = [] 
        unique_master = [] 
        unique_target = [] 
        
        # 1. Find Duplicates (Files in Target that match Master)
        for ident, t_paths in target_idx.items():
            if ident in master_idx:
                duplicates.append({
                    'master_file': master_idx[ident][0],
                    'target_files': t_paths 
                })
            else:
                for p in t_paths: unique_target.append(p)

        # 2. Find Unique Master Files
        for ident, m_paths in master_idx.items():
            if ident not in target_idx:
                for p in m_paths: unique_master.append(p)

        self.finish(duplicates, unique_master, unique_target)

# --- 3. GUI APPLICATION ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Safe Sync: Master vs Target")
        self.root.geometry("1200x750") # Slightly wider for new column
        
        self.master_path = ""
        self.target_path = ""
        self.scan_results = []

        # Header
        tk.Label(root, text="SAFE SYNC TOOL", font=("Arial", 16, "bold"), pady=10).pack()

        # INPUT FRAME
        frame_in = tk.LabelFrame(root, text="Configuration", padx=10, pady=10)
        frame_in.pack(fill=tk.X, padx=10)

        # Row 1: Master
        tk.Label(frame_in, text="1. PROTECTED Folder (e.g. Google Drive):", fg="#d32f2f", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(frame_in, text="(Files here will NEVER be touched)", fg="gray", font=("Arial", 8)).grid(row=1, column=0, sticky="w")
        self.ent_master = tk.Entry(frame_in, width=70, bg="#ffebee")
        self.ent_master.grid(row=0, column=1, rowspan=2, padx=5)
        tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_master)).grid(row=0, column=2, rowspan=2)

        # Row 2: Target
        tk.Label(frame_in, text="2. CLEANUP Folder (e.g. OneDrive/USB):", fg="#1976d2", font=("Arial", 9, "bold")).grid(row=2, column=0, sticky="w", pady=(10,0))
        tk.Label(frame_in, text="(Files here can be moved/deleted)", fg="gray", font=("Arial", 8)).grid(row=3, column=0, sticky="w")
        self.ent_target = tk.Entry(frame_in, width=70, bg="#e3f2fd")
        self.ent_target.grid(row=2, column=1, rowspan=2, padx=5, pady=(10,0))
        tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_target)).grid(row=2, column=2, rowspan=2, pady=(10,0))

        # Checkbox & Scan
        frame_act = tk.Frame(root, pady=10)
        frame_act.pack(fill=tk.X, padx=10)
        self.use_hash = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_act, text="Enable 'Deep Content Scan' (Slower, but 100% precise)", variable=self.use_hash).pack(side=tk.LEFT)
        self.btn_scan = tk.Button(frame_act, text="COMPARE FOLDERS", bg="#4caf50", fg="white", font=("Arial", 10, "bold"), height=2, command=self.start_scan)
        self.btn_scan.pack(side=tk.RIGHT)

        # Status
        self.progress = ttk.Progressbar(root, mode='determinate')
        self.progress.pack(fill=tk.X, padx=10, pady=5)
        self.lbl_stat = tk.Label(root, text="Select folders to begin.", fg="gray")
        self.lbl_stat.pack()
        
        tk.Label(root, text="Double-click any file below to open its folder.", font=("Arial", 9, "italic"), fg="#FF9800").pack(pady=(0,5))

        # Tabs
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Tab 1: Duplicates
        self.tab_dup = tk.Frame(self.notebook)
        self.notebook.add(self.tab_dup, text="Duplicates (Found in Both)")
        
        # Columns: Name, Folder, Status, Path
        self.tree_dup = ttk.Treeview(self.tab_dup, columns=("name", "folder", "action", "path"), show="headings")
        self.tree_dup.heading("name", text="File Name")
        self.tree_dup.heading("folder", text="Parent Folder")
        self.tree_dup.heading("action", text="Status")
        self.tree_dup.heading("path", text="Full Path (Hidden)")
        
        self.tree_dup.column("name", width=250)
        self.tree_dup.column("folder", width=250)
        self.tree_dup.column("action", width=150)
        self.tree_dup.column("path", width=0, stretch=False) # Hide full path visually
        
        self.tree_dup.pack(fill=tk.BOTH, expand=True)
        self.tree_dup.bind("<Double-1>", self.on_double_click)
        
        # Actions for Duplicates
        f_dup_act = tk.Frame(self.tab_dup, pady=5, bg="#eeeeee")
        f_dup_act.pack(fill=tk.X)
        tk.Label(f_dup_act, text="Actions for TARGET files only:", bg="#eeeeee", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=5)
        tk.Button(f_dup_act, text="Move Target Files to Folder...", bg="#fff9c4", command=self.move_dupes).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_dup_act, text="Trash Target Files", bg="#ffcdd2", command=self.trash_dupes).pack(side=tk.RIGHT, padx=5)

        # Tab 2: Unique
        self.tab_uniq = tk.Frame(self.notebook)
        self.notebook.add(self.tab_uniq, text="Unique Files (Differences)")
        
        # Columns: Name, Folder, Location, Path
        self.tree_uniq = ttk.Treeview(self.tab_uniq, columns=("name", "folder", "loc", "path"), show="headings")
        self.tree_uniq.heading("name", text="File Name")
        self.tree_uniq.heading("folder", text="Parent Folder")
        self.tree_uniq.heading("loc", text="Exists ONLY In")
        self.tree_uniq.heading("path", text="Full Path (Hidden)")

        self.tree_uniq.column("name", width=300)
        self.tree_uniq.column("folder", width=300)
        self.tree_uniq.column("loc", width=150)
        self.tree_uniq.column("path", width=0, stretch=False) # Hide full path visually

        self.tree_uniq.pack(fill=tk.BOTH, expand=True)
        self.tree_uniq.bind("<Double-1>", self.on_double_click)

    def browse(self, entry):
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, tk.END)
            entry.insert(0, prepare_path(d))

    def start_scan(self):
        m, t = self.ent_master.get(), self.ent_target.get()
        if not m or not t: return messagebox.showerror("Error", "Select both folders.")
        if m == t: return messagebox.showerror("Error", "Master and Target cannot be the same folder!")
        
        self.master_path, self.target_path = m, t
        self.btn_scan.config(state=tk.DISABLED)
        
        # Clear UI
        for x in self.tree_dup.get_children(): self.tree_dup.delete(x)
        for x in self.tree_uniq.get_children(): self.tree_uniq.delete(x)
        
        Comparator(m, t, self.use_hash.get(), self.update_ui, self.finish_scan).start()

    def update_ui(self, msg, pct):
        self.root.after(0, lambda: [self.lbl_stat.config(text=msg), self.progress.configure(value=pct)])

    def finish_scan(self, duplicates, uniq_m, uniq_t):
        self.root.after(0, lambda: self._populate(duplicates, uniq_m, uniq_t))

    def _populate(self, duplicates, uniq_m, uniq_t):
        self.scan_results = duplicates
        self.btn_scan.config(state=tk.NORMAL)
        self.progress['value'] = 100
        self.lbl_stat.config(text=f"Done. Duplicates: {len(duplicates)} | Master Unique: {len(uniq_m)} | Target Unique: {len(uniq_t)}")

        # Fill Duplicates
        for d in duplicates:
            for t_file in d['target_files']:
                fname = os.path.basename(t_file)
                folder = os.path.basename(os.path.dirname(t_file))
                self.tree_dup.insert("", "end", values=(fname, folder, "DUPLICATE (Safe)", t_file))
                
        # Fill Unique
        for p in uniq_m:
            fname = os.path.basename(p)
            folder = os.path.basename(os.path.dirname(p))
            self.tree_uniq.insert("", "end", values=(fname, folder, "MASTER (Protected)", p), tags=('master',))
            
        for p in uniq_t:
            fname = os.path.basename(p)
            folder = os.path.basename(os.path.dirname(p))
            self.tree_uniq.insert("", "end", values=(fname, folder, "TARGET (Cleanup)", p), tags=('target',))
        
        self.tree_uniq.tag_configure('master', foreground='red')
        self.tree_uniq.tag_configure('target', foreground='blue')

    def on_double_click(self, event):
        tree = event.widget
        item = tree.selection()
        if not item: return
        # Path is now the 4th column (index 3)
        values = tree.item(item, "values")
        if values and len(values) >= 4:
            path = values[3]
            reveal_in_explorer(path)

    # --- SAFETY ACTION: MOVE ---
    def move_dupes(self):
        if not self.scan_results: return
        dest = filedialog.askdirectory(title="Select Folder to Move TARGET files into")
        if not dest: return
        
        count = 0
        skipped = 0
        
        for d in self.scan_results:
            for t_path in d['target_files']:
                if is_subpath(t_path, self.master_path):
                    skipped += 1
                    continue
                try:
                    fname = os.path.basename(t_path)
                    target = os.path.join(dest, fname)
                    c = 1
                    while os.path.exists(target):
                        name, ext = os.path.splitext(fname)
                        target = os.path.join(dest, f"{name}_{c}{ext}")
                        c += 1
                    shutil.move(t_path, target)
                    count += 1
                except Exception as e:
                    print(f"Error moving {t_path}: {e}")

        msg = f"Moved {count} files from Target folder."
        if skipped > 0: msg += f"\n\nSAFETY ALERT: {skipped} files were skipped because they were inside the Protected folder."
        messagebox.showinfo("Move Complete", msg)
        self.start_scan()

    # --- SAFETY ACTION: TRASH ---
    def trash_dupes(self):
        if not self.scan_results: return
        if not messagebox.askyesno("Confirm Trash", "Move all visible TARGET duplicates to Trash?"): return
        
        count = 0
        skipped = 0
        
        for d in self.scan_results:
            for t_path in d['target_files']:
                if is_subpath(t_path, self.master_path):
                    skipped += 1
                    continue
                try:
                    send2trash.send2trash(t_path)
                    count += 1
                except:
                    try: 
                        os.remove(t_path)
                        count += 1
                    except: pass
        
        msg = f"Trashed {count} files from Target folder."
        if skipped > 0: msg += f"\n\nSAFETY ALERT: {skipped} files were skipped because they were inside the Protected folder."
        messagebox.showinfo("Trash Complete", msg)
        self.start_scan()

if __name__ == "__main__":
    root = tk.Tk()
    try: App(root)
    except: pass
    root.mainloop()