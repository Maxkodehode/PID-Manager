import os
import time
import subprocess
import re
import dearpygui.dearpygui as dpg
import psutil

# Application state flags
is_table_refresh_paused = False
selected_process_pid = None
was_click_on_process_row = False

# Global cache storing persistent psutil.Process objects across polling intervals
persistent_process_cache = {}

# Retrieve total logical CPU count for total-system percentage normalization
total_system_cpu_count = psutil.cpu_count(logical=True) or 1

def extract_manual_page_description(process_name):
    """Executes the man command for the given process and extracts the DESCRIPTION text block."""
    base_executable_name = process_name.split('/')[-1].split('.')[0]
    
    try:
        shell_command = f"man {base_executable_name} | col -b"
        command_result = subprocess.run(shell_command, shell=True, capture_output=True, text=True, timeout=2)
        
        if command_result.returncode != 0:
            return f"No manual page found for '{base_executable_name}'."
            
        manual_output_text = command_result.stdout
        description_regex_match = re.search(r'(?:^|\n)(?:DESCRIPTION)\s*\n(.*?)(?=\n[A-Z\s]{3,}\n|\Z)', manual_output_text, re.DOTALL | re.IGNORECASE)
        
        if description_regex_match:
            raw_description_text = description_regex_match.group(1).strip()
            cleaned_description_lines = [line.strip() for line in raw_description_text.splitlines() if line.strip()]
            return '\n'.join(cleaned_description_lines)
            
        return "Description section not found in manual page."
    except subprocess.TimeoutExpired:
        return "Timeout while fetching manual page."
    except Exception as error_details:
        return f"Could not load description: {error_details}"

def handle_process_row_click(sender, app_data, process_metadata):
    """Event handler triggered when a user clicks a process row in the table."""
    global is_table_refresh_paused, selected_process_pid, was_click_on_process_row
    
    was_click_on_process_row = True
    is_table_refresh_paused = True  # Freeze table updates so rows stop shifting
    
    process_pid = process_metadata['pid']
    process_name = process_metadata['name']
    selected_process_pid = process_pid
    
    dpg.set_value("SelectedHeader", f"Selected: PID {process_pid} ({process_name})")
    fetched_description = extract_manual_page_description(process_name)
    dpg.set_value("DescText", fetched_description)

def handle_global_mouse_click(sender, app_data):
    """Event handler to detect clicks outside process rows, clearing selections and resuming table updates."""
    global is_table_refresh_paused, selected_process_pid, was_click_on_process_row
    
    if not was_click_on_process_row:
        selected_process_pid = None
        is_table_refresh_paused = False
        dpg.set_value("SelectedHeader", "Process Description (Manual Page)")
        dpg.set_value("DescText", "Click a process row above to load its description...")
    else:
        was_click_on_process_row = False

def fetch_filtered_user_processes():
    """Fetches user-owned processes using persistent caching for precise interval-based CPU metrics."""
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
            
            # Retrieve or initialize the persistent process object from cache
            if process_pid in persistent_process_cache:
                target_process_object = persistent_process_cache[process_pid]
            else:
                target_process_object = running_process
                persistent_process_cache[process_pid] = target_process_object
                target_process_object.cpu_percent(interval=None) # Prime baseline
                continue
                
            # Calculate interval CPU usage normalized across total system cores
            raw_per_core_cpu_percentage = target_process_object.cpu_percent(interval=None) or 0.0
            normalized_total_system_cpu = raw_per_core_cpu_percentage / total_system_cpu_count
            
            process_memory_info = target_process_object.memory_info()
            memory_megabytes = (process_memory_info.rss / (1024 * 1024)) if process_memory_info else 0.0
            
            active_process_list.append({
                'pid': process_pid,
                'name': process_name,
                'cpu': round(normalized_total_system_cpu, 1),
                'mem': round(memory_megabytes, 1)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
            
    # Purge terminated processes from cache
    cached_process_pids = list(persistent_process_cache.keys())
    for cached_pid in cached_process_pids:
        if cached_pid not in current_pids_discovered:
            del persistent_process_cache[cached_pid]
            
    return sorted(active_process_list, key=lambda process_item: process_item['cpu'], reverse=True)

def refresh_process_table_contents():
    """Clears and repopulates the Dear PyGui table container with latest process metrics."""
    if not dpg.does_item_exist("ProcessTable"):
        return
        
    dpg.delete_item("ProcessTable", children_only=True)
    
    dpg.add_table_column(label="PID", parent="ProcessTable", width_fixed=True, width=70)
    dpg.add_table_column(label="Process Name", parent="ProcessTable")
    dpg.add_table_column(label="CPU %", parent="ProcessTable", width_fixed=True, width=80)
    dpg.add_table_column(label="Memory (MB)", parent="ProcessTable", width_fixed=True, width=100)
    
    updated_processes = fetch_filtered_user_processes()
    for process_data in updated_processes:
        with dpg.table_row(parent="ProcessTable"):
            dpg.add_selectable(label=str(process_data['pid']), span_columns=True, callback=handle_process_row_click, user_data=process_data)
            dpg.add_text(process_data['name'])
            dpg.add_text(str(process_data['cpu']))
            dpg.add_text(str(process_data['mem']))

# Initialize Dear PyGui context
dpg.create_context()

# Register global mouse click listener for panel deselection
with dpg.handler_registry():
    dpg.add_mouse_click_handler(callback=handle_global_mouse_click)

# Build graphical user interface layout
with dpg.window(label="Active Processor Monitor", tag="PrimaryWindow", width=950, height=750):
    dpg.add_text("Filtered User Processes", color=(0, 255, 150))
    dpg.add_separator()
    
    # Upper Section: Scrollable Process Table Grid
    with dpg.table(header_row=True, resizable=True, scrollY=True, height=400, tag="ProcessTable"):
        dpg.add_table_column(label="PID", width_fixed=True, width=70)
        dpg.add_table_column(label="Process Name")
        dpg.add_table_column(label="CPU %", width_fixed=True, width=80)
        dpg.add_table_column(label="Memory (MB)", width_fixed=True, width=100)
        refresh_process_table_contents()
        
    dpg.add_spacer(height=10)
    dpg.add_separator()
    
    # Lower Section: Manual Page Description Panel
    dpg.add_text("Process Description (Manual Page)", tag="SelectedHeader", color=(100, 200, 255))
    with dpg.child_window(tag="DescPanel", height=180, border=True):
        dpg.add_text("Click a process row above to load its description...", tag="DescText", wrap=900)

# Configure viewport parameters
dpg.create_viewport(title='Active Processor Monitor', width=1000, height=800)
dpg.setup_dearpygui()
dpg.show_viewport()
dpg.set_primary_window("PrimaryWindow", True)

last_refresh_timestamp = time.time()
refresh_interval_seconds = 2.0

# Main application execution loop
while dpg.is_dearpygui_running():
    current_timestamp = time.time()
    
    # Refresh table data periodically unless paused by user row selection
    if not is_table_refresh_paused and (current_timestamp - last_refresh_timestamp >= refresh_interval_seconds):
        refresh_process_table_contents()
        last_refresh_timestamp = current_timestamp
        
    dpg.render_dearpygui_frame()

dpg.destroy_context()