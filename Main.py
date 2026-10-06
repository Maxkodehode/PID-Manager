import dearpygui.dearpygui as dpg
import psutil

def get_filtered_processes():
    """Fetches processes and filters out kernel threads or low-end system PIDs."""
    process_list = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
        try:
            pid = proc.info['pid']
            name = proc.info['name']
            
            # Filtering logic: 
            # 1. Skip system PIDs <= 300
            # 2. Skip processes with empty or bracketed names (kernel threads)
            if pid <= 300 or not name or name.startswith('['):
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
            
    # Sort descending by CPU usage
    return sorted(process_list, key=lambda x: x['cpu'], reverse=True)

# Initialize Dear PyGui context
dpg.create_context()

# Build the window layout
with dpg.window(label="Active Processor Monitor", tag="PrimaryWindow", width=900, height=600):
    dpg.add_text("Filtered User Processes", color=(0, 255, 150))
    dpg.add_separator()
    
    # Create a table to display processes (using correct scrollY capitalization)
    with dpg.table(header_row=True, resizable=True, scrollY=True, height=450, tag="ProcessTable"):
        dpg.add_table_column(label="PID", width_fixed=True, width=70)
        dpg.add_table_column(label="Process Name")
        dpg.add_table_column(label="CPU %", width_fixed=True, width=80)
        dpg.add_table_column(label="Memory (MB)", width_fixed=True, width=100)
        
        # Populate initial rows
        procs = get_filtered_processes()
        for p in procs:
            with dpg.table_row():
                dpg.add_text(str(p['pid']))
                dpg.add_text(p['name'])
                dpg.add_text(str(p['cpu']))
                dpg.add_text(str(p['mem']))

# Setup and show viewport
dpg.create_viewport(title='Active Processor Monitor', width=950, height=650)
dpg.setup_dearpygui()
dpg.show_viewport()

# Set primary window to scale with viewport resizing
dpg.set_primary_window("PrimaryWindow", True)

# Main render loop using the correct frame-rendering hook
while dpg.is_dearpygui_running():
    # TODO: Add dynamic background refresh loop here later
    dpg.render_dearpygui_frame()

dpg.destroy_context()