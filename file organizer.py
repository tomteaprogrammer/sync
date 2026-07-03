import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import hashlib
import time

class FileAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("File Organizer & Duplicate Finder")
        self.root.geometry("1400x800")

        self.seen_hashes = {}
        self.total_scanned_files = 0
        self.total_scanned_folders = 0
        self.last_gui_update = 0

        # --- Top Control Panel ---
        top_frame = tk.Frame(root)
        top_frame.pack(fill=tk.X, padx=10, pady=10)

        self.btn_select = tk.Button(
            top_frame, 
            text="Select Folder to Analyze", 
            command=self.select_folder,
            font=("Arial", 10, "bold")
        )
        self.btn_select.pack(side=tk.LEFT, padx=5)

        self.lbl_status = tk.Label(top_frame, text="Waiting for folder selection...", fg="gray")
        self.lbl_status.pack(side=tk.LEFT, padx=15)

        # --- Treeview Setup ---
        mid_frame = tk.Frame(root)
        mid_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        tree_scroll = tk.Scrollbar(mid_frame)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        tree_scroll_x = tk.Scrollbar(mid_frame, orient="horizontal")
        tree_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)

        self.tree = ttk.Treeview(mid_frame, yscrollcommand=tree_scroll.set, xscrollcommand=tree_scroll_x.set, selectmode="browse")
        self.tree.pack(fill=tk.BOTH, expand=True)
        tree_scroll.config(command=self.tree.yview)
        tree_scroll_x.config(command=self.tree.xview)

        # Define Columns
        self.tree['columns'] = ("MaxDepth", "Size", "FileCount", "Type", "Duplicate", "DupName", "DupPath", "FullPath", "RawSize")
        self.tree['displaycolumns'] = ("MaxDepth", "Size", "FileCount", "Type", "Duplicate", "DupName", "DupPath", "FullPath")
        
        # Format Columns
        self.tree.column("#0", width=250, minwidth=150) 
        self.tree.column("MaxDepth", width=120, minwidth=100, anchor=tk.CENTER)
        self.tree.column("Size", width=100, minwidth=80, anchor=tk.E)
        self.tree.column("FileCount", width=100, minwidth=80, anchor=tk.CENTER)
        self.tree.column("Type", width=80, minwidth=60, anchor=tk.CENTER)
        self.tree.column("Duplicate", width=80, minwidth=80, anchor=tk.CENTER)
        self.tree.column("DupName", width=150, minwidth=100)
        self.tree.column("DupPath", width=300, minwidth=150)
        self.tree.column("FullPath", width=300, minwidth=150)

        # Assign Headings
        self.tree.heading("#0", text="File / Folder Name", anchor=tk.W, command=lambda: self.sort_column("#0", False))
        self.tree.heading("MaxDepth", text="Max Depth Inside", anchor=tk.CENTER, command=lambda: self.sort_column("MaxDepth", False))
        self.tree.heading("Size", text="Size", anchor=tk.CENTER, command=lambda: self.sort_column("Size", False))
        self.tree.heading("FileCount", text="Total Files", anchor=tk.CENTER, command=lambda: self.sort_column("FileCount", False))
        self.tree.heading("Type", text="Type", anchor=tk.CENTER, command=lambda: self.sort_column("Type", False))
        self.tree.heading("Duplicate", text="Is Duplicate?", anchor=tk.CENTER, command=lambda: self.sort_column("Duplicate", False))
        self.tree.heading("DupName", text="Original File Name", anchor=tk.W, command=lambda: self.sort_column("DupName", False))
        self.tree.heading("DupPath", text="Original File Location", anchor=tk.W, command=lambda: self.sort_column("DupPath", False))
        self.tree.heading("FullPath", text="Full Path", anchor=tk.W, command=lambda: self.sort_column("FullPath", False))

        self.tree.bind("<Double-1>", self.open_in_explorer)

    def format_size(self, size_in_bytes):
        if size_in_bytes < 1024:
            return f"{size_in_bytes} B"
        elif size_in_bytes < 1024**2:
            return f"{size_in_bytes/1024:.2f} KB"
        elif size_in_bytes < 1024**3:
            return f"{size_in_bytes/(1024**2):.2f} MB"
        else:
            return f"{size_in_bytes/(1024**3):.2f} GB"

    def get_file_hash(self, filepath):
        hasher = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except (PermissionError, OSError):
            return None

    def sort_column(self, col, reverse):
        def sort_nodes(parent):
            children = list(self.tree.get_children(parent))
            if not children:
                return
            
            def get_sort_value(item):
                if col == "#0":
                    return self.tree.item(item, "text").lower()
                
                sort_col = "RawSize" if col == "Size" else col
                val = self.tree.set(item, sort_col)
                
                if sort_col in ("MaxDepth", "FileCount", "RawSize"):
                    try:
                        return float(val)
                    except ValueError:
                        return -1.0
                return str(val).lower()

            children.sort(key=get_sort_value, reverse=reverse)

            for index, child in enumerate(children):
                self.tree.move(child, parent, index)
                sort_nodes(child) 

        sort_nodes("")
        self.tree.heading(col, command=lambda: self.sort_column(col, not reverse))

    def select_folder(self):
        folder_path = filedialog.askdirectory(title="Select a Folder to Analyze")
        if folder_path:
            self.tree.delete(*self.tree.get_children())
            self.seen_hashes.clear()
            self.lbl_status.config(text=f"Starting scan...", fg="blue")
            self.btn_select.config(state=tk.DISABLED)

            threading.Thread(target=self.run_scan_thread, args=(folder_path,), daemon=True).start()

    def run_scan_thread(self, root_path):
        self.total_scanned_files = 0
        self.total_scanned_folders = 0
        self.last_gui_update = time.time()
        
        try:
            total_size, total_files, max_depth, root_children = self.scan_and_build(root_path)
            self.root.after(0, self.insert_into_gui, root_path, total_size, total_files, max_depth, root_children)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"An error occurred: {str(e)}"))
        finally:
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))

    def scan_and_build(self, current_path):
        self.total_scanned_folders += 1
        
        # 1. Print real-time progress numbers to the command line
        print(f"[{self.total_scanned_folders} Folders, {self.total_scanned_files} Files] -> {current_path}")
        
        # 2. Safely throttle GUI updates to prevent Tkinter from freezing
        if time.time() - self.last_gui_update > 0.1:
            display_path = current_path if len(current_path) < 50 else "..." + current_path[-47:]
            status_msg = f"Progress: {self.total_scanned_folders} Folders | {self.total_scanned_files} Files | {display_path}"
            self.root.after(0, lambda m=status_msg: self.lbl_status.config(text=m, fg="blue"))
            self.last_gui_update = time.time()

        size = 0
        file_count = 0
        max_depth = 0
        children_data = []

        try:
            for entry in os.scandir(current_path):
                if entry.is_dir(follow_symlinks=False):
                    c_size, c_fc, c_md, c_children = self.scan_and_build(entry.path)
                    
                    size += c_size
                    file_count += c_fc
                    
                    nested_depth = c_md + 1
                    if nested_depth > max_depth:
                        max_depth = nested_depth
                    
                    children_data.append({
                        'type': 'Folder',
                        'name': entry.name,
                        'path': entry.path,
                        'size': c_size,
                        'file_count': c_fc,
                        'max_depth': c_md,
                        'children': c_children,
                        'is_dup': "",
                        'dup_name': "",
                        'dup_path': ""
                    })
                else:
                    self.total_scanned_files += 1
                    
                    try:
                        f_size = entry.stat().st_size
                    except OSError:
                        f_size = 0
                        
                    size += f_size
                    file_count += 1
                    
                    f_hash = self.get_file_hash(entry.path)
                    is_dup, dup_name, dup_path = "", "", ""
                    
                    if f_hash:
                        if f_hash in self.seen_hashes:
                            is_dup = "✓ Yes"
                            dup_path, dup_name = self.seen_hashes[f_hash]
                        else:
                            self.seen_hashes[f_hash] = (entry.path, entry.name)
                            
                    children_data.append({
                        'type': 'File',
                        'name': entry.name,
                        'path': entry.path,
                        'size': f_size,
                        'file_count': 0, 
                        'max_depth': 0,  
                        'children': [],
                        'is_dup': is_dup,
                        'dup_name': dup_name,
                        'dup_path': dup_path
                    })
        except PermissionError:
            pass
            
        return size, file_count, max_depth, children_data

    def insert_into_gui(self, root_path, total_size, total_files, max_depth, root_children):
        root_node = self.tree.insert("", "end", text=os.path.basename(root_path) or root_path, 
            values=(
                max_depth, 
                self.format_size(total_size), 
                total_files, 
                "Folder", 
                "", "", "", 
                root_path, 
                total_size 
            ), open=True)
            
        self.populate_tree(root_node, root_children)
        self.lbl_status.config(text=f"Scan Complete! Processed {self.total_scanned_folders} Folders and {self.total_scanned_files} Files.", fg="green")

    def populate_tree(self, parent_node, children_data):
        for data in children_data:
            node = self.tree.insert(parent_node, "end", text=data['name'], 
                values=(
                    data['max_depth'] if data['type'] == 'Folder' else "-", 
                    self.format_size(data['size']), 
                    data['file_count'] if data['type'] == 'Folder' else "-", 
                    data['type'], 
                    data['is_dup'], 
                    data['dup_name'], 
                    data['dup_path'], 
                    data['path'], 
                    data['size'] 
                ))
            
            if data['children']:
                self.populate_tree(node, data['children'])

    def open_in_explorer(self, event):
        selected_item = self.tree.selection()
        if not selected_item:
            return

        item_values = self.tree.item(selected_item[0], 'values')
        if not item_values:
            return

        file_path = item_values[7] 

        if os.path.exists(file_path):
            subprocess.run(['explorer', '/select,', os.path.normpath(file_path)])
        else:
            messagebox.showwarning("Not Found", "The selected file or folder could not be found.")

if __name__ == "__main__":
    root = tk.Tk()
    app = FileAnalyzerApp(root)
    root.mainloop()