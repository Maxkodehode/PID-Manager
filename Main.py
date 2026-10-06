import os
import time
import dearpygui.dearpygui as dpg
import psutil

def get_filtered_processes():
    """Fetches processes owned by the current user, filtering out kernel threads and system daemons."""
    process_list = []
    current_uid = os.getuid()
    
    for proc in psutil.process_iter(['pid', 'name', 'uids', 'cpu_percent', 'memory_info']):
        try:
            # 1. Filter: Only show processes owned by your user account (hides root system daemons)
            if proc.info['uids'] and proc.info['uids'].real != current_uid:
                continue
                
            pid = proc.info['pid']
            name = proc.info['name']
            
            # 2. Skip kernel threads or empty names
            if not name or name.startswith('['):
                continue
                
            cpu = proc.info['cpu_percent'] or 0.0
            mem_mb = (proc.info['memory_info'].rss / (1024 * 1024)) if proc.info['memory_info'] else 0.0
            
            process_list.append({
                'pid': pid,
                'name': name,
                'cpu': cpu,
                'mem': round(mem_mb, 1)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
            
    return sorted(process_list, key=lambda x: x['cpu'], reverse=True)
def update_table():
    """Clears and repopulates the process table with fresh data."""
    if not dpg.does_item_exist("ProcessTable"):
        return
        
    # Clear existing rows (and columns)
    dpg.delete_item("ProcessTable", children_only=True)
    
    # Re-add columns
    dpg.add_table_column(label="PID", parent="ProcessTable", width_fixed=True, width=70)
    dpg.add_table_column(label="Process Name", parent="ProcessTable")
    dpg.add_table_column(label="CPU %", parent="ProcessTable", width_fixed=True, width=80)
    dpg.add_table_column(label="Memory (MB)", parent="ProcessTable", width_fixed=True, width=100)
    
    # Populate fresh rows
    procs = get_filtered_processes()
    for p in procs:
        with dpg.table_row(parent="ProcessTable"):
            dpg.add_text(str(p['pid']))
            dpg.add_text(p['name'])
            dpg.add_text(str(p['cpu']))
            dpg.add_text(str(p['mem']))

# Initialize Dear PyGui context
dpg.create_context()

# Build the window layout
with dpg.window(label="Active Processor Monitor", tag="PrimaryWindow", width=900, height=600):
    dpg.add_text("Filtered User Processes", color=(0, 255, 150))
    dpg.add_separator()
    
    with dpg.table(header_row=True, resizable=True, scrollY=True, height=450, tag="ProcessTable"):
        dpg.add_table_column(label="PID", width_fixed=True, width=70)
        dpg.add_table_column(label="Process Name")
        dpg.add_table_column(label="CPU %", width_fixed=True, width=80)
        dpg.add_table_column(label="Memory (MB)", width_fixed=True, width=100)
        
        # Initial populate
        update_table()

# Setup viewport
dpg.create_viewport(title='Active Processor Monitor', width=950, height=650)
dpg.setup_dearpygui()
dpg.show_viewport()
dpg.set_primary_window("PrimaryWindow", True)

# Timer variables for 2-second refresh rate
last_refresh = time.time()
refresh_interval = 2.0

# Main render loop
while dpg.is_dearpygui_running():
    current_time = time.time()
    if current_time - last_refresh >= refresh_interval:
        update_table()
        last_refresh = current_time
        
    dpg.render_dearpygui_frame()

dpg.destroy_context()