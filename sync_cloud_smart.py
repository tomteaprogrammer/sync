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
import concurrent.futures

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
    if not parent: return False
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
    except: pass

def get_free_space_gb(folder):
    """Returns free space in GB for the drive containing the folder"""
    try:
        total, used, free = shutil.disk_usage(folder)
        return free / (1024**3)
    except:
        return 100 # Assume plenty if check fails

# --- 2. LOGIC ENGINE (MULTI-THREADED + SPACE GUARD) ---
class Comparator(threading.Thread):
    def __init__(self, master_path, target_path, use_hash, callback_update, callback_finish, callback_alert):
        super().__init__()
        self.master = master_path
        self.target = target_path
        self.use_hash = use_hash
        self.update_ui = callback_update
        self.finish = callback_finish
        self.alert = callback_alert
        self.daemon = True
        self.stop_event = threading.Event()
        
        try:
            self.max_threads = (os.cpu_count() or 4) + 2
        except:
            self.max_threads = 4

    def get_identifier(self, filepath):
        if self.stop_event.is_set(): return (filepath, None)
        try:
            stat = os.stat(filepath)
            size = stat.st_size
            if not self.use_hash:
                return (filepath, (os.path.basename(filepath), size))
            
            # SLOW MODE (Hash)
            hasher = hashlib.md5()
            with open(filepath, 'rb') as f:
                while True:
                    if self.stop_event.is_set(): return (filepath, None)
                    data = f.read(65536)
                    if not data: break
                    hasher.update(data)
            return (filepath, (size, hasher.hexdigest()))
        except:
            return (filepath, None)

    def process_folder_parallel(self, folder, label):
        if self.stop_event.is_set(): return {}
        
        index = {}
        all_files = []
        
        self.update_ui(f"Listing files in {label}...", 0)
        for root, _, files in os.walk(folder):
            if self.stop_event.is_set(): return {}
            for file in files:
                all_files.append(prepare_path(os.path.join(root, file)))
        
        total = len(all_files)
        if total == 0: return {}

        completed = 0
        self.update_ui(f"Processing {label}...", 0)

        # SPACE CHECK: Check drive space before massive processing
        start_drive = os.path.splitdrive(folder)[0] if platform.system() == 'Windows' else '/'

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_file = {executor.submit(self.get_identifier, f): f for f in all_files}
            
            for i, future in enumerate(concurrent.futures.as_completed(future_to_file)):
                if self.stop_event.is_set(): break
                
                # Check disk space every 50 files
                if i % 50 == 0:
                    free_gb = get_free_space_gb(folder)
                    if free_gb < 5.0: # STOP IF LESS THAN 5GB LEFT
                        self.stop_event.set()
                        self.alert("CRITICAL WARNING", f"Disk Space Low! ({free_gb:.2f} GB left).\n\nStopping scan to prevent computer crash.\n\nPlease clear space or use 'Fast Mode' (Uncheck Deep Scan).")
                        return {}

                path, ident = future.result()
                if ident:
                    if ident not in index: index[ident] = []
                    index[ident].append(path)
                
                completed += 1
                if completed % 50 == 0:
                    self.update_ui(f"Processing {label}: {completed}/{total} (Free Space: {get_free_space_gb(folder):.1f} GB)", (completed/total)*100)
                    
        return index

    def run(self):
        # 1. Index Target
        target_idx = self.process_folder_parallel(self.target, "Target")
        if self.stop_event.is_set(): return
        
        # 2. Index Master
        master_idx = {}
        if self.master:
            master_idx = self.process_folder_parallel(self.master, "Master")
            if self.stop_event.is_set(): return
        
        self.update_ui("Comparing Logic...", 90)
        
        cross_duplicates = []
        internal_duplicates = []
        
        for ident, t_paths in target_idx.items():
            if self.master and ident in master_idx:
                cross_duplicates.append({
                    'master_file': master_idx[ident][0],
                    'target_files': t_paths 
                })
            else:
                if len(t_paths) > 1:
                    internal_duplicates.append(t_paths)

        self.finish(cross_duplicates, internal_duplicates)

# --- 3. GUI APPLICATION ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Cloud-Smart Duplicate Cleaner")
        self.root.geometry("1200x850")
        
        self.master_path = None
        self.target_path = None
        self.cross_dupes = []     
        self.internal_dupes = []  
        self.mode = tk.IntVar(value=2) 

        # Header
        tk.Label(root, text="SYNC: CLOUD OPTIMIZED", font=("Arial", 16, "bold"), pady=10).pack()

        # MODE
        frame_mode = tk.Frame(root)
        frame_mode.pack(pady=5)
        tk.Label(frame_mode, text="Select Mode: ", font=("Arial", 10, "bold")).pack(side=tk.LEFT)
        tk.Radiobutton(frame_mode, text="Two-Folder Compare", variable=self.mode, value=1, command=self.toggle_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(frame_mode, text="Single Folder Cleanup", variable=self.mode, value=2, command=self.toggle_mode).pack(side=tk.LEFT, padx=10)

        # INPUT
        frame_in = tk.LabelFrame(root, text="Configuration", padx=10, pady=10)
        frame_in.pack(fill=tk.X, padx=10)

        self.lbl_master = tk.Label(frame_in, text="1. PROTECTED Folder (Master):", fg="#d32f2f", font=("Arial", 9, "bold"))
        self.lbl_master.grid(row=0, column=0, sticky="w")
        self.ent_master = tk.Entry(frame_in, width=70, bg="#ffebee")
        self.ent_master.grid(row=0, column=1, padx=5)
        self.btn_master = tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_master))
        self.btn_master.grid(row=0, column=2)

        self.lbl_target = tk.Label(frame_in, text="2. CLEANUP Folder (Target):", fg="#1976d2", font=("Arial", 9, "bold"))
        self.lbl_target.grid(row=1, column=0, sticky="w", pady=5)
        self.ent_target = tk.Entry(frame_in, width=70, bg="#e3f2fd")
        self.ent_target.grid(row=1, column=1, padx=5, pady=5)
        tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_target)).grid(row=1, column=2, pady=5)

        # SCAN OPTIONS
        frame_act = tk.Frame(root, pady=10)
        frame_act.pack(fill=tk.X, padx=10)
        
        self.use_hash = tk.BooleanVar(value=False)
        # Detailed Checkbox Label
        lbl_hash = tk.Label(frame_act, text="Deep Scan (Content Hash)", font=("Arial", 9, "bold"))
        lbl_hash.pack(side=tk.LEFT)
        
        self.chk_hash = tk.Checkbutton(frame_act, text="Enable (Will download cloud files)", variable=self.use_hash)
        self.chk_hash.pack(side=tk.LEFT, padx=5)
        
        self.btn_scan = tk.Button(frame_act, text="START SCAN", bg="#4caf50", fg="white", font=("Arial", 10, "bold"), height=2, command=self.start_scan)
        self.btn_scan.pack(side=tk.RIGHT)

        # DISK SPACE WARNING
        self.lbl_space = tk.Label(root, text="Disk Guard: Active (Stops if < 5GB Free)", fg="green", font=("Arial", 8))
        self.lbl_space.pack()

        # PROGRESS
        self.progress = ttk.Progressbar(root, mode='determinate')
        self.progress.pack(fill=tk.X, padx=10, pady=5)
        self.lbl_stat = tk.Label(root, text=f"Ready. CPU Cores: {os.cpu_count()}", fg="gray")
        self.lbl_stat.pack()

        # TABS
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # TAB 1
        self.tab_cross = tk.Frame(self.notebook)
        self.notebook.add(self.tab_cross, text="1. Safe to Delete (Matches Master)")
        self.tree_cross = self.create_tree(self.tab_cross, ["File Name", "Folder", "Status", "Full Path"])
        
        f_cross_act = tk.Frame(self.tab_cross, pady=5, bg="#eeeeee")
        f_cross_act.pack(fill=tk.X)
        tk.Button(f_cross_act, text="Trash ALL listed here", bg="#ffcdd2", command=self.trash_cross).pack(side=tk.RIGHT, padx=5)

        # TAB 2
        self.tab_internal = tk.Frame(self.notebook)
        self.notebook.add(self.tab_internal, text="2. Internal Duplicates (Clean Single Folder)")
        self.tree_internal = ttk.Treeview(self.tab_internal, columns=("name", "folder", "path"), show="headings")
        self.tree_internal.heading("name", text="File Name")
        self.tree_internal.heading("folder", text="Folder")
        self.tree_internal.heading("path", text="Full Path")
        self.tree_internal.pack(fill=tk.BOTH, expand=True)
        self.tree_internal.bind("<Double-1>", self.on_double_click)
        
        f_int_act = tk.Frame(self.tab_internal, pady=5, bg="#e3f2fd")
        f_int_act.pack(fill=tk.X)
        tk.Label(f_int_act, text="Duplicates found inside the cleanup folder:", bg="#e3f2fd").pack(side=tk.LEFT, padx=5)
        tk.Button(f_int_act, text="Delete Selected File", bg="#ffcdd2", command=self.delete_selected_internal).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_int_act, text="Auto-Keep Shortest Paths", bg="#bbdefb", command=self.auto_cull_internal).pack(side=tk.RIGHT, padx=5)

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
        for c in cols: tree.heading(c, text=c)
        tree.column(cols[0], width=250)
        tree.column(cols[-1], width=0, stretch=False) 
        tree.pack(fill=tk.BOTH, expand=True)
        tree.bind("<Double-1>", self.on_double_click)
        return tree

    def browse(self, entry):
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, tk.END)
            entry.insert(0, prepare_path(d))

    def alert_user(self, title, msg):
        self.root.after(0, lambda: messagebox.showwarning(title, msg))
        self.root.after(0, lambda: self.btn_scan.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.lbl_stat.config(text="Scan Aborted (Disk Space Low)."))

    def start_scan(self):
        m = self.ent_master.get() if self.mode.get() == 1 else None
        t = self.ent_target.get()
        if not t: return messagebox.showerror("Error", "Please select a Target/Cleanup folder.")
        
        self.master_path, self.target_path = m, t
        self.btn_scan.config(state=tk.DISABLED)
        
        for tree in [self.tree_cross, self.tree_internal]:
            for x in tree.get_children(): tree.delete(x)
        
        Comparator(m, t, self.use_hash.get(), self.update_ui, self.finish_scan, self.alert_user).start()

    def update_ui(self, msg, pct):
        self.root.after(0, lambda: [self.lbl_stat.config(text=msg), self.progress.configure(value=pct)])

    def finish_scan(self, cross, internal):
        self.root.after(0, lambda: self._populate(cross, internal))

    def _populate(self, cross, internal):
        self.cross_dupes = cross
        self.internal_dupes = internal
        self.btn_scan.config(state=tk.NORMAL)
        self.progress['value'] = 100
        
        if self.mode.get() == 1:
            for d in cross:
                for t_file in d['target_files']:
                    self.tree_cross.insert("", "end", values=(os.path.basename(t_file), os.path.basename(os.path.dirname(t_file)), "Safe to Delete", t_file))
        
        for group in internal:
            grp_id = self.tree_internal.insert("", "end", values=(f"[GROUP] {len(group)} Copies", "--", ""), open=True)
            for path in group:
                self.tree_internal.insert(grp_id, "end", values=(os.path.basename(path), os.path.basename(os.path.dirname(path)), path))
                
        self.lbl_stat.config(text=f"Scan Complete. Found {len(internal)} groups of internal duplicates.")

    def on_double_click(self, event):
        tree = event.widget
        item = tree.selection()
        if not item: return
        vals = tree.item(item, "values")
        if vals: reveal_in_explorer(vals[-1])

    def trash_cross(self):
        if not self.cross_dupes: return
        if not messagebox.askyesno("Confirm", "Trash ALL files in Tab 1?"): return
        count = 0
        for d in self.cross_dupes:
            for path in d['target_files']:
                if self.master_path and is_subpath(path, self.master_path): continue
                try: 
                    send2trash.send2trash(path)
                    count += 1
                except: pass
        messagebox.showinfo("Done", f"Trashed {count} files.")
        self.start_scan()

    def delete_selected_internal(self):
        sel = self.tree_internal.selection()
        if not sel: return
        path = self.tree_internal.item(sel[0], "values")[-1]
        if not path or not os.path.exists(path): return
        
        if messagebox.askyesno("Delete", f"Trash this file?\n{path}"):
            try:
                send2trash.send2trash(path)
                self.tree_internal.delete(sel[0])
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def auto_cull_internal(self):
        if not self.internal_dupes: return
        if not messagebox.askyesno("Auto-Clean", "Keep shortest path in each group and trash the rest?"): return
        count = 0
        for group in self.internal_dupes:
            group.sort(key=len)
            to_delete = group[1:]
            for path in to_delete:
                try:
                    send2trash.send2trash(path)
                    count += 1
                except: pass
        messagebox.showinfo("Done", f"Auto-cleaned {count} files.")
        self.start_scan()

if __name__ == "__main__":
    root = tk.Tk()
    try: App(root)
    except: pass
    root.mainloop()