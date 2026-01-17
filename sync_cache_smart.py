import os
import sys
import hashlib
import subprocess
import importlib.util
import threading
import platform
import shutil
import pickle  # Added for saving/loading
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
    try:
        if not folder or not os.path.exists(folder): return 100
        total, used, free = shutil.disk_usage(folder)
        return free / (1024**3)
    except:
        return 100

# --- 2. LOGIC ENGINE ---
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
        
        try: self.max_threads = (os.cpu_count() or 4) + 2
        except: self.max_threads = 4

    def get_identifier(self, filepath):
        if self.stop_event.is_set(): return (filepath, None)
        try:
            stat = os.stat(filepath)
            size = stat.st_size
            if not self.use_hash:
                return (filepath, (os.path.basename(filepath), size))
            
            # Hash Mode
            hasher = hashlib.md5()
            with open(filepath, 'rb') as f:
                while True:
                    if self.stop_event.is_set(): return (filepath, None)
                    data = f.read(65536)
                    if not data: break
                    hasher.update(data)
            return (filepath, (size, hasher.hexdigest()))
        except: return (filepath, None)

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

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_file = {executor.submit(self.get_identifier, f): f for f in all_files}
            
            for i, future in enumerate(concurrent.futures.as_completed(future_to_file)):
                if self.stop_event.is_set(): break
                
                # Check disk space every 50 files
                if i % 50 == 0:
                    free_gb = get_free_space_gb(folder)
                    if free_gb < 5.0:
                        self.stop_event.set()
                        self.alert("CRITICAL WARNING", f"Disk Space Low! ({free_gb:.2f} GB left).\nStopping scan.")
                        return {}

                path, ident = future.result()
                if ident:
                    if ident not in index: index[ident] = []
                    index[ident].append(path)
                
                completed += 1
                if completed % 100 == 0:
                    self.update_ui(f"Processing {label}: {completed}/{total}", (completed/total)*100)
                    
        return index

    def run(self):
        target_idx = self.process_folder_parallel(self.target, "Target")
        if self.stop_event.is_set(): return
        
        master_idx = {}
        if self.master:
            master_idx = self.process_folder_parallel(self.master, "Master")
            if self.stop_event.is_set(): return
        
        self.update_ui("Finishing...", 100)
        # Pass raw indexes back to App so we can save them if requested
        self.finish(master_idx, target_idx)

# --- 3. GUI APPLICATION ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Cloud Sync: Cache Supported")
        self.root.geometry("1200x850")
        
        self.master_path = None
        self.target_path = None
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
        tk.Label(root, text="SYNC: SMART CACHE", font=("Arial", 16, "bold"), pady=10).pack()

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

        # SCAN
        frame_act = tk.Frame(root, pady=10)
        frame_act.pack(fill=tk.X, padx=10)
        self.use_hash = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_act, text="Deep Scan (Hash) - Uncheck for Fast Mode", variable=self.use_hash).pack(side=tk.LEFT)
        self.btn_scan = tk.Button(frame_act, text="START SCAN", bg="#4caf50", fg="white", font=("Arial", 10, "bold"), height=2, command=self.start_scan)
        self.btn_scan.pack(side=tk.RIGHT)

        # STATUS
        self.lbl_space = tk.Label(root, text="Disk Guard Active (<5GB Stops Scan)", fg="green", font=("Arial", 8))
        self.lbl_space.pack()
        self.progress = ttk.Progressbar(root, mode='determinate')
        self.progress.pack(fill=tk.X, padx=10, pady=5)
        self.lbl_stat = tk.Label(root, text="Select folders or Load Cache.", fg="gray")
        self.lbl_stat.pack()

        # TABS
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.tab_cross = tk.Frame(self.notebook)
        self.notebook.add(self.tab_cross, text="1. Safe to Delete (Matches Master)")
        self.tree_cross = self.create_tree(self.tab_cross, ["File Name", "Folder", "Status", "Full Path"])
        
        f_cross_act = tk.Frame(self.tab_cross, pady=5, bg="#eeeeee")
        f_cross_act.pack(fill=tk.X)
        tk.Button(f_cross_act, text="Trash ALL listed here", bg="#ffcdd2", command=self.trash_cross).pack(side=tk.RIGHT, padx=5)

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
        tk.Button(f_int_act, text="Delete Selected", bg="#ffcdd2", command=self.delete_selected_internal).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_int_act, text="Auto-Keep Shortest", bg="#bbdefb", command=self.auto_cull_internal).pack(side=tk.RIGHT, padx=5)

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

    # --- SCANNING ---
    def start_scan(self):
        m = self.ent_master.get() if self.mode.get() == 1 else None
        t = self.ent_target.get()
        if not t: return messagebox.showerror("Error", "Select Target/Cleanup folder.")
        
        self.master_path, self.target_path = m, t
        self.btn_scan.config(state=tk.DISABLED)
        self.clear_results()
        
        Comparator(m, t, self.use_hash.get(), self.update_ui, self.scan_finished, self.alert_user).start()

    def update_ui(self, msg, pct):
        self.root.after(0, lambda: [self.lbl_stat.config(text=msg), self.progress.configure(value=pct)])

    def scan_finished(self, master_idx, target_idx):
        # Scan done. Now we calculate and populate.
        self.last_master_idx = master_idx
        self.last_target_idx = target_idx
        self.root.after(0, self.calculate_and_populate)

    # --- LOGIC & CACHE ---
    def calculate_and_populate(self):
        """Runs the comparison logic on the currently stored indices (whether scanned or loaded)."""
        self.lbl_stat.config(text="Processing Results...")
        
        cross_dupes = []
        internal_dupes = []
        
        # Logic: Compare Target vs Master
        for ident, t_paths in self.last_target_idx.items():
            if self.mode.get() == 1 and self.last_master_idx and ident in self.last_master_idx:
                cross_dupes.append({
                    'master_file': self.last_master_idx[ident][0],
                    'target_files': t_paths 
                })
            else:
                if len(t_paths) > 1:
                    internal_dupes.append(t_paths)

        self.cross_dupes = cross_dupes
        self.internal_dupes = internal_dupes
        
        self.populate_trees()
        self.btn_scan.config(state=tk.NORMAL)
        self.progress['value'] = 100
        self.lbl_stat.config(text=f"Done. Found {len(internal_dupes)} internal groups.")

    def populate_trees(self):
        self.clear_trees()
        
        if self.mode.get() == 1:
            for d in self.cross_dupes:
                for t_file in d['target_files']:
                    self.tree_cross.insert("", "end", values=(os.path.basename(t_file), os.path.basename(os.path.dirname(t_file)), "Safe to Delete", t_file))
        
        for group in self.internal_dupes:
            grp_id = self.tree_internal.insert("", "end", values=(f"[GROUP] {len(group)} Copies", "--", ""), open=True)
            for path in group:
                self.tree_internal.insert(grp_id, "end", values=(os.path.basename(path), os.path.basename(os.path.dirname(path)), path))

    def clear_trees(self):
        for t in [self.tree_cross, self.tree_internal]:
            for x in t.get_children(): t.delete(x)

    def clear_results(self):
        self.clear_trees()
        self.last_master_idx = {}
        self.last_target_idx = {}
        self.cross_dupes = []
        self.internal_dupes = []

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
        # After trash, remove from UI locally to avoid full rescan
        self.clear_trees() # Simple refresh needed usually, but for now clear
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