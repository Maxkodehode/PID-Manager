import os
import time
import subprocess
import re
import sqlite3
import threading
import dearpygui.dearpygui as dpg
import psutil

# Database file for process descriptions
DB_FILE = "process_cache.db"

# Application state flags
is_table_refresh_paused = False
selected_process_pid = None
was_click_on_process_row = False

# State tracking for expanded multi-instance process groups
expanded_process_groups = set()

# Global cache storing persistent psutil.Process objects across polling intervals
persistent_process_cache = {}

# Retrieve total logical CPU count for total-system percentage normalization
total_system_cpu_count = psutil.cpu_count(logical=True) or 1

# Prime system-wide CPU percentage baseline on startup
psutil.cpu_percent(interval=None)

def initialize_database():
    """Initializes the SQLite database table for process descriptions if it doesn't exist."""
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS process_descriptions (
                executable_name TEXT PRIMARY KEY,
                description TEXT,
                source TEXT,
                needs_generation INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

def initial_scan_processes():
    """Scans currently active unique process names in the background, checks man pages, and flags unknowns for AI."""
    unique_executables = set()
    
    for running_process in psutil.process_iter(['name']):
        try:
            name = running_process.info['name']
            if name and not name.startswith('['):
                base_name = name.split('/')[-1].split('.')[0]
                unique_executables.add(base_name)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
            
    with sqlite3.connect(DB_FILE) as connection:
        cursor = connection.cursor()
        for base_executable_name in unique_executables:
            # Skip if already documented in the database
            cursor.execute("SELECT 1 FROM process_descriptions WHERE executable_name = ?", (base_executable_name,))
            if cursor.fetchone():
                continue
                
            # Try fetching man page
            try:
                shell_command = f"man {base_executable_name} | col -b"
                command_result = subprocess.run(shell_command, shell=True, capture_output=True, text=True, timeout=1)
                
                if command_result.returncode == 0:
                    manual_output_text = command_result.stdout
                    description_regex_match = re.search(r'(?:^|\n)(?:DESCRIPTION)\s*\n(.*?)(?=\n[A-Z\s]{3,}\n|\Z)', manual_output_text, re.DOTALL | re.IGNORECASE)
                    
                    if description_regex_match:
                        raw_desc = description_regex_match.group(1).strip()
                        cleaned_desc = '\n'.join([line.strip() for line in raw_desc.splitlines() if line.strip()])
                        
                        cursor.execute("""
                            INSERT OR REPLACE INTO process_descriptions (executable_name, description, source, needs_generation)
                            VALUES (?, ?, 'man', 0)
                        """, (base_executable_name, cleaned_desc))
                        continue
            except Exception:
                pass
                
            # If no man page exists, insert stub and flag for agent generation
            fallback_msg = f"No documentation found for '{base_executable_name}'. Queued for AI analysis."
            cursor.execute("""
                INSERT OR IGNORE INTO process_descriptions (executable_name, description, source, needs_generation)
                VALUES (?, ?, 'pending', 1)
            """, (base_executable_name, fallback_msg))
            
        connection.commit()

def get_process_description(process_name):
    """Queries DB first, falls back to live man check if missing, or returns queued status."""
    base_executable_name = process_name.split('/')[-1].split('.')[0]
    
    with sqlite3.connect(DB_FILE) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT description FROM process_descriptions WHERE executable_name = ?", (base_executable_name,))
        row = cursor.fetchone()
        
        if row and row[0]:
            return row[0]

        # Live fallback if added after startup scan
        return f"No documentation found for '{base_executable_name}'. Queued for AI analysis."

def load_process_details_into_panel(process_pid, process_name):
    """Fetches process metadata, parent relationship, description, and populates the UI panel."""
    global is_table_refresh_paused, selected_process_pid
    
    is_table_refresh_paused = True
    selected_process_pid = process_pid
    
    dpg.set_value("SelectedHeader", f"Selected: PID {process_pid} ({process_name})")
    
    parent_metadata = None
    try:
        process_object = psutil.Process(process_pid)
        parent_process = process_object.parent()
        if parent_process:
            parent_metadata = {
                'pid': parent_process.pid,
                'name': parent_process.name()
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    dpg.delete_item("DescPanel", children_only=True)
    
    if parent_metadata:
        dpg.add_text("Parent Process:", color=(200, 200, 100), parent="DescPanel")
        dpg.add_button(
            label=f"PID {parent_metadata['pid']} ({parent_metadata['name']})",
            callback=lambda s, a, data: load_process_details_into_panel(data['pid'], data['name']),
            user_data=parent_metadata,
            parent="DescPanel"
        )
        dpg.add_spacer(height=5, parent="DescPanel")
        dpg.add_separator(parent="DescPanel")
        dpg.add_spacer(height=5, parent="DescPanel")

    fetched_description = get_process_description(process_name)
    dpg.add_text(fetched_description, wrap=900, parent="DescPanel")

def handle_process_row_click(sender, app_data, process_metadata):
    """Event handler triggered when a user clicks an individual process row or child instance."""
    global was_click_on_process_row
    was_click_on_process_row = True
    load_process_details_into_panel(process_metadata['pid'], process_metadata['name'])

def toggle_group_expansion_callback(sender, app_data, group_name):
    """Toggles the expansion state of an exact-name process group when clicked."""
    global expanded_process_groups, is_table_refresh_paused, was_click_on_process_row
    was_click_on_process_row = True
    is_table_refresh_paused = True
    
    if group_name in expanded_process_groups:
        expanded_process_groups.remove(group_name)
    else:
        expanded_process_groups.add(group_name)
    
    refresh_process_table_contents()

def handle_global_mouse_click(sender, app_data):
    """Event handler to detect clicks outside process rows, clearing selections and resuming table updates."""
    global is_table_refresh_paused, selected_process_pid, was_click_on_process_row
    
    if not was_click_on_process_row:
        selected_process_pid = None
        is_table_refresh_paused = False
        dpg.set_value("SelectedHeader", "Process Description (Database Cache)")
        dpg.delete_item("DescPanel", children_only=True)
        dpg.add_text("Click a process row above to load its description...", tag="DescText", wrap=900, parent="DescPanel")
    else:
        was_click_on_process_row = False

def fetch_filtered_user_processes():
    """Fetches user-owned processes using PSS memory metrics and persistent caching."""
    global persistent_process_cache
    active_process_list = []
    current_logged_in_uid = os.getuid()
    current_pids_discovered = set()
    
    for running_process in psutil.process_iter(['pid', 'name', 'uids']):
        try:
            process_user_ids = running_process.info['uids']
            if process_user_ids and process_user_ids.real != current_logged_in_uid:
                continue
                
            process_pid = running_process.info['pid']
            process_name = running_process.info['name']
            
            if not process_name or process_name.startswith('['):
                continue
                
            current_pids_discovered.add(process_pid)
            
            if process_pid in persistent_process_cache:
                target_process_object = persistent_process_cache[process_pid]
            else:
                target_process_object = running_process
                persistent_process_cache[process_pid] = target_process_object
                target_process_object.cpu_percent(interval=None)
                continue
                
            raw_per_core_cpu_percentage = target_process_object.cpu_percent(interval=None) or 0.0
            normalized_total_system_cpu = raw_per_core_cpu_percentage / total_system_cpu_count
            
            process_full_memory_info = target_process_object.memory_full_info()
            process_memory_bytes = getattr(process_full_memory_info, 'pss', None)
            if process_memory_bytes is None:
                process_memory_bytes = process_full_memory_info.rss
                
            memory_megabytes = process_memory_bytes / (1024 * 1024)
            
            active_process_list.append({
                'pid': process_pid,
                'name': process_name,
                'cpu': round(normalized_total_system_cpu, 1),
                'mem': round(memory_megabytes, 1)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
            
    cached_process_pids = list(persistent_process_cache.keys())
    for cached_pid in cached_process_pids:
        if cached_pid not in current_pids_discovered:
            del persistent_process_cache[cached_pid]
            
    return sorted(active_process_list, key=lambda process_item: process_item['cpu'], reverse=True)

def aggregate_processes_by_exact_name(raw_process_list):
    """Groups processes by their exact name, aggregating resource totals and tracking instances."""
    aggregated_group_dictionary = {}
    
    for process_item in raw_process_list:
        process_name = process_item['name']
        
        if process_name not in aggregated_group_dictionary:
            aggregated_group_dictionary[process_name] = {
                'name': process_name,
                'instances': [],
                'total_cpu': 0.0,
                'total_memory_mb': 0.0
            }
            
        group_record = aggregated_group_dictionary[process_name]
        group_record['instances'].append(process_item)
        group_record['total_cpu'] += process_item['cpu']
        group_record['total_memory_mb'] += process_item['mem']
        
    return sorted(
        list(aggregated_group_dictionary.values()),
        key=lambda group_record: group_record['total_cpu'],
        reverse=True
    )

def refresh_process_table_contents():
    """Clears and repopulates the Dear PyGui table container with a system total row and grouped metrics."""
    if not dpg.does_item_exist("ProcessTable"):
        return
        
    dpg.delete_item("ProcessTable", children_only=True)
    
    dpg.add_table_column(label="PID / Group", parent="ProcessTable", width_fixed=True, width=120)
    dpg.add_table_column(label="Process Name", parent="ProcessTable")
    dpg.add_table_column(label="CPU %", parent="ProcessTable", width_fixed=True, width=80)
    dpg.add_table_column(label="Memory (MB)", parent="ProcessTable", width_fixed=True, width=100)
    
    total_system_cpu_percentage = psutil.cpu_percent(interval=None) or 0.0
    system_memory_info = psutil.virtual_memory()
    total_system_used_memory_mb = round(system_memory_info.used / (1024 * 1024), 1)
    
    with dpg.table_row(parent="ProcessTable"):
        dpg.add_text("TOTAL", color=(255, 180, 0))
        dpg.add_text("All System Processes & Kernel", color=(255, 180, 0))
        dpg.add_text(str(round(total_system_cpu_percentage, 1)), color=(255, 180, 0))
        dpg.add_text(str(total_system_used_memory_mb), color=(255, 180, 0))
        
    raw_processes = fetch_filtered_user_processes()
    process_groups = aggregate_processes_by_exact_name(raw_processes)
    
    for group_record in process_groups:
        group_name = group_record['name']
        is_group_expanded = group_name in expanded_process_groups
        instance_count = len(group_record['instances'])
        
        if instance_count > 1:
            expansion_indicator_symbol = "[-] " if is_group_expanded else "[+] "
            display_identifier_label = f"{expansion_indicator_symbol}({instance_count})"
        else:
            display_identifier_label = str(group_record['instances'][0]['pid'])
            
        with dpg.table_row(parent="ProcessTable"):
            if instance_count > 1:
                dpg.add_selectable(
                    label=display_identifier_label,
                    span_columns=True,
                    callback=toggle_group_expansion_callback,
                    user_data=group_name
                )
            else:
                dpg.add_selectable(
                    label=display_identifier_label,
                    span_columns=True,
                    callback=handle_process_row_click,
                    user_data=group_record['instances'][0]
                )
                
            dpg.add_text(group_name)
            dpg.add_text(str(round(group_record['total_cpu'], 1)))
            dpg.add_text(str(round(group_record['total_memory_mb'], 1)))
            
        if instance_count > 1 and is_group_expanded:
            for child_process_instance in group_record['instances']:
                with dpg.table_row(parent="ProcessTable"):
                    dpg.add_selectable(
                        label=f"    └ {child_process_instance['pid']}",
                        span_columns=True,
                        callback=handle_process_row_click,
                        user_data=child_process_instance
                    )
                    dpg.add_text(f"    {child_process_instance['name']}")
                    dpg.add_text(str(child_process_instance['cpu']))
                    dpg.add_text(str(child_process_instance['mem']))

# Initialize database and spawn background scan thread
initialize_database()
threading.Thread(target=initial_scan_processes, daemon=True).start()

# Initialize Dear PyGui context
dpg.create_context()

with dpg.handler_registry():
    dpg.add_mouse_click_handler(callback=handle_global_mouse_click)

with dpg.window(label="Active Processor Monitor", tag="PrimaryWindow", width=950, height=750):
    dpg.add_text("Filtered User Processes", color=(0, 255, 150))
    dpg.add_separator()
    
    with dpg.table(header_row=True, resizable=True, scrollY=True, height=400, tag="ProcessTable"):
        dpg.add_table_column(label="PID / Group", width_fixed=True, width=120)
        dpg.add_table_column(label="Process Name")
        dpg.add_table_column(label="CPU %", width_fixed=True, width=80)
        dpg.add_table_column(label="Memory (MB)", width_fixed=True, width=100)
        refresh_process_table_contents()
        
    dpg.add_spacer(height=10)
    dpg.add_separator()
    
    dpg.add_text("Process Description (Database Cache)", tag="SelectedHeader", color=(100, 200, 255))
    with dpg.child_window(tag="DescPanel", height=180, border=True):
        dpg.add_text("Click a process row above to load its description...", tag="DescText", wrap=900)

dpg.create_viewport(title='Active Processor Monitor', width=1000, height=800)
dpg.setup_dearpygui()
dpg.show_viewport()
dpg.set_primary_window("PrimaryWindow", True)

last_refresh_timestamp = time.time()
refresh_interval_seconds = 2.0

while dpg.is_dearpygui_running():
    current_timestamp = time.time()
    
    if not is_table_refresh_paused and (current_timestamp - last_refresh_timestamp >= refresh_interval_seconds):
        refresh_process_table_contents()
        last_refresh_timestamp = current_timestamp
        
    dpg.render_dearpygui_frame()

dpg.destroy_context()