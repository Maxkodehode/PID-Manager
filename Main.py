import os
import time
import subprocess
import re
import sqlite3
import threading
import dearpygui.dearpygui as dpg
import psutil
import webbrowser
import signal

DATABASE_FILE_PATH = "process_cache.db"
BYTES_PER_MEGABYTE = 1024 * 1024
DESCRIPTION_TEXT_WRAP_WIDTH_PIXELS = 900
TABLE_REFRESH_INTERVAL_SECONDS = 2.0
SORT_DIRECTION_ASCENDING = 1

DEFAULT_SORT_COLUMN_LABEL = "CPU %"
DEFAULT_HEADER_BUTTON_LABEL = "Process Description (Database Cache)"
NO_PROCESS_SELECTED_PLACEHOLDER_TEXT = "Click a process row above to load its description..."

HEADER_TEXT_COLOR = (0, 255, 150)
TOTAL_ROW_TEXT_COLOR = (255, 180, 0)
PARENT_LABEL_TEXT_COLOR = (200, 200, 100)
LOG_TITLE_TEXT_COLOR = (100, 200, 255)
SUCCESS_TEXT_COLOR = (0, 255, 150)
WARNING_TEXT_COLOR = (255, 200, 0)
ERROR_TEXT_COLOR = (255, 100, 100)

is_table_refresh_paused = False
selected_process_pid = None
was_click_on_process_row = False
current_sort_column = DEFAULT_SORT_COLUMN_LABEL
current_sort_ascending = False

expanded_process_group_names = set()

cached_psutil_processes_by_pid = {}

logical_cpu_core_count = psutil.cpu_count(logical=True) or 1

psutil.cpu_percent(interval=None)

URL_PATTERN = re.compile(r'https?://[^\s<>"\)\]]+')


def extract_base_executable_name(process_name):
    return process_name.split('/')[-1].split('.')[0]


def initialize_database():
    with sqlite3.connect(DATABASE_FILE_PATH) as database_connection:
        database_connection.execute("""
            CREATE TABLE IF NOT EXISTS process_descriptions (
                executable_name TEXT PRIMARY KEY,
                description TEXT,
                source TEXT,
                needs_generation INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def scan_running_processes_into_description_database():
    unique_base_executable_names = set()

    for running_process in psutil.process_iter(['name']):
        try:
            process_name = running_process.info['name']
            if process_name and not process_name.startswith('['):
                unique_base_executable_names.add(extract_base_executable_name(process_name))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    with sqlite3.connect(DATABASE_FILE_PATH) as database_connection:
        database_cursor = database_connection.cursor()
        for base_executable_name in unique_base_executable_names:
            database_cursor.execute(
                "SELECT 1 FROM process_descriptions WHERE executable_name = ?",
                (base_executable_name,)
            )
            if database_cursor.fetchone():
                continue

            try:
                man_environment_variables = os.environ.copy()
                man_environment_variables["MANPAGER"] = "cat"

                man_command_result = subprocess.run(
                    ["man", base_executable_name],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    env=man_environment_variables,
                    shell=False
                )

                if man_command_result.returncode == 0:
                    manual_page_text = man_command_result.stdout
                    description_section_match = re.search(
                        r'(?:^|\n)(?:DESCRIPTION)\s*\n(.*?)(?=\n[A-Z\s]{3,}\n|\Z)',
                        manual_page_text,
                        re.DOTALL | re.IGNORECASE
                    )

                    if description_section_match:
                        raw_description = description_section_match.group(1).strip()
                        cleaned_description = '\n'.join(
                            text_line.strip()
                            for text_line in raw_description.splitlines()
                            if text_line.strip()
                        )

                        database_cursor.execute("""
                            INSERT OR REPLACE INTO process_descriptions (executable_name, description, source, needs_generation)
                            VALUES (?, ?, 'man', 0)
                        """, (base_executable_name, cleaned_description))
                        continue
            except Exception:
                pass

            queued_for_ai_message = f"No documentation found for '{base_executable_name}'. Queued for AI analysis."
            database_cursor.execute("""
                INSERT OR IGNORE INTO process_descriptions (executable_name, description, source, needs_generation)
                VALUES (?, ?, 'pending', 1)
            """, (base_executable_name, queued_for_ai_message))

        database_connection.commit()


def get_process_description(process_name):
    base_executable_name = extract_base_executable_name(process_name)

    with sqlite3.connect(DATABASE_FILE_PATH) as database_connection:
        database_cursor = database_connection.cursor()
        database_cursor.execute(
            "SELECT description FROM process_descriptions WHERE executable_name = ?",
            (base_executable_name,)
        )
        description_row = database_cursor.fetchone()

        if description_row and description_row[0]:
            return description_row[0]

        return f"No documentation found for '{base_executable_name}'. Queued for AI analysis."


def open_link_callback(sender, app_data, url_to_open):
    global was_click_on_process_row
    was_click_on_process_row = True
    webbrowser.open(url_to_open)


def add_description_with_links(description_text, parent_panel_tag):
    for text_line in description_text.splitlines():
        url_matches = list(URL_PATTERN.finditer(text_line))
        if not url_matches:
            dpg.add_text(text_line, wrap=DESCRIPTION_TEXT_WRAP_WIDTH_PIXELS, parent=parent_panel_tag)
            continue

        with dpg.group(horizontal=True, horizontal_spacing=0, parent=parent_panel_tag):
            text_cursor_position = 0
            for url_match in url_matches:
                link_url = url_match.group().rstrip('.,;:')
                if url_match.start() > text_cursor_position:
                    dpg.add_text(text_line[text_cursor_position:url_match.start()])
                link_button = dpg.add_button(label=link_url, callback=open_link_callback, user_data=link_url)
                dpg.bind_item_theme(link_button, "LinkTheme")
                text_cursor_position = url_match.start() + len(link_url)
            if text_cursor_position < len(text_line):
                dpg.add_text(text_line[text_cursor_position:])


def load_process_details_into_panel(process_pid, process_name):
    global is_table_refresh_paused, selected_process_pid

    is_table_refresh_paused = True
    selected_process_pid = process_pid

    dpg.set_item_user_data("SelectedProcessHeaderButton", {"pid": process_pid, "name": process_name})

    dpg.set_item_label(
        "SelectedProcessHeaderButton",
        f"Selected: PID {process_pid} ({process_name}) [Click for Actions]"
    )

    parent_process_summary = None
    try:
        selected_process_object = psutil.Process(process_pid)
        parent_process = selected_process_object.parent()
        if parent_process:
            parent_process_summary = {
                'pid': parent_process.pid,
                'name': parent_process.name()
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    dpg.delete_item("DescriptionPanel", children_only=True)

    if parent_process_summary:
        dpg.add_text("Parent Process:", color=PARENT_LABEL_TEXT_COLOR, parent="DescriptionPanel")
        dpg.add_button(
            label=f"PID {parent_process_summary['pid']} ({parent_process_summary['name']})",
            callback=lambda sender, app_data, parent_summary: load_process_details_into_panel(
                parent_summary['pid'], parent_summary['name']
            ),
            user_data=parent_process_summary,
            parent="DescriptionPanel"
        )
        dpg.add_spacer(height=5, parent="DescriptionPanel")
        dpg.add_separator(parent="DescriptionPanel")
        dpg.add_spacer(height=5, parent="DescriptionPanel")

    process_description = get_process_description(process_name)
    add_description_with_links(process_description, "DescriptionPanel")


def handle_process_row_click(sender, app_data, process_summary):
    global was_click_on_process_row, selected_process_pid
    was_click_on_process_row = True

    selected_process_pid = process_summary['pid']
    load_process_details_into_panel(process_summary['pid'], process_summary['name'])


def toggle_group_expansion_callback(sender, app_data, process_group_record):
    global is_table_refresh_paused, was_click_on_process_row

    was_click_on_process_row = True
    is_table_refresh_paused = True

    if isinstance(process_group_record, dict):
        group_name = process_group_record['name']
        if process_group_record.get('instances'):
            primary_process_pid = process_group_record['instances'][0]['pid']
            load_process_details_into_panel(primary_process_pid, group_name)
    else:
        group_name = str(process_group_record)

    if group_name in expanded_process_group_names:
        expanded_process_group_names.remove(group_name)
    else:
        expanded_process_group_names.add(group_name)

    refresh_process_table_contents()


def handle_global_mouse_click(sender, app_data):
    global is_table_refresh_paused, selected_process_pid, was_click_on_process_row

    if not was_click_on_process_row:
        selected_process_pid = None
        is_table_refresh_paused = False
        dpg.set_item_label("SelectedProcessHeaderButton", DEFAULT_HEADER_BUTTON_LABEL)
        dpg.delete_item("DescriptionPanel", children_only=True)
        dpg.add_text(
            NO_PROCESS_SELECTED_PLACEHOLDER_TEXT,
            tag="DescriptionPlaceholderText",
            wrap=DESCRIPTION_TEXT_WRAP_WIDTH_PIXELS,
            parent="DescriptionPanel"
        )
    else:
        was_click_on_process_row = False


def terminate_selected_process(signal_to_send=signal.SIGTERM):
    global selected_process_pid, is_table_refresh_paused, was_click_on_process_row

    was_click_on_process_row = True

    selected_process_button_data = dpg.get_item_user_data("SelectedProcessHeaderButton")

    if (
        not selected_process_button_data
        or not isinstance(selected_process_button_data, dict)
        or "pid" not in selected_process_button_data
    ):
        dpg.delete_item("DescriptionPanel", children_only=True)
        dpg.add_text(
            "[-] ERROR: No process PID is currently selected or loaded.",
            color=ERROR_TEXT_COLOR,
            parent="DescriptionPanel"
        )
        return

    target_process_pid = selected_process_button_data["pid"]
    target_process_name = selected_process_button_data.get("name", "Unknown")

    dpg.delete_item("DescriptionPanel", children_only=True)
    dpg.add_text("--- Process Termination Command Log ---", color=LOG_TITLE_TEXT_COLOR, parent="DescriptionPanel")
    dpg.add_spacer(height=5, parent="DescriptionPanel")

    signal_display_names = {
        signal.SIGTERM: "SIGTERM (Safe Termination)",
        signal.SIGKILL: "SIGKILL (Force Kill)",
        signal.SIGHUP: "SIGHUP (Reload Config)"
    }
    signal_display_label = signal_display_names.get(signal_to_send, f"Signal {signal_to_send}")

    try:
        target_process = psutil.Process(target_process_pid)
        target_process_name = target_process.name()

        dpg.add_text(f"[*] Targeting PID {target_process_pid} ({target_process_name})...", parent="DescriptionPanel")
        dpg.add_text(f"[*] Dispatching signal: {signal_display_label}", parent="DescriptionPanel")

        if signal_to_send == signal.SIGKILL:
            target_process.kill()
        elif signal_to_send == signal.SIGTERM:
            target_process.terminate()
        else:
            target_process.send_signal(signal_to_send)

        try:
            target_process.wait(timeout=0.5)
            dpg.add_text(
                f"[+] SUCCESS: Process {target_process_pid} ({target_process_name}) terminated successfully.",
                color=SUCCESS_TEXT_COLOR,
                parent="DescriptionPanel"
            )
        except psutil.TimeoutExpired:
            dpg.add_text(
                f"[!] NOTICE: Signal sent to PID {target_process_pid}, but process has not exited yet.",
                color=WARNING_TEXT_COLOR,
                parent="DescriptionPanel"
            )

        dpg.set_item_user_data("SelectedProcessHeaderButton", None)
        selected_process_pid = None
        is_table_refresh_paused = False
        refresh_process_table_contents()

    except psutil.AccessDenied:
        dpg.add_text(
            f"[-] ERROR: Permission Denied! Unable to terminate PID {target_process_pid}.\n"
            f"    The process is owned by another user/session or requires root privileges.",
            color=ERROR_TEXT_COLOR,
            wrap=DESCRIPTION_TEXT_WRAP_WIDTH_PIXELS,
            parent="DescriptionPanel"
        )
        print(f"Permission denied when sending {signal_display_label} to PID {target_process_pid}.")

    except psutil.NoSuchProcess:
        dpg.add_text(
            f"[-] ERROR: Process PID {target_process_pid} no longer exists.",
            color=ERROR_TEXT_COLOR,
            parent="DescriptionPanel"
        )
        dpg.set_item_user_data("SelectedProcessHeaderButton", None)
        selected_process_pid = None
        is_table_refresh_paused = False
        refresh_process_table_contents()

    except Exception as unexpected_error:
        dpg.add_text(
            f"[-] ERROR: Unexpected exception: {unexpected_error}",
            color=ERROR_TEXT_COLOR,
            wrap=DESCRIPTION_TEXT_WRAP_WIDTH_PIXELS,
            parent="DescriptionPanel"
        )
        print(f"Error terminating PID {target_process_pid}: {unexpected_error}")


def fetch_filtered_user_processes():
    user_process_summaries = []
    current_user_id = os.getuid()
    discovered_process_pids = set()

    for running_process in psutil.process_iter(['pid', 'name', 'uids', 'status']):
        try:
            if running_process.info['status'] in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                continue

            process_user_ids = running_process.info['uids']
            if process_user_ids and process_user_ids.real != current_user_id:
                continue

            process_pid = running_process.info['pid']
            process_name = running_process.info['name']

            if not process_name or process_name.startswith('['):
                continue

            discovered_process_pids.add(process_pid)

            if process_pid in cached_psutil_processes_by_pid:
                target_process_object = cached_psutil_processes_by_pid[process_pid]
            else:
                target_process_object = running_process
                cached_psutil_processes_by_pid[process_pid] = target_process_object
                target_process_object.cpu_percent(interval=None)
                continue

            per_core_cpu_percent = target_process_object.cpu_percent(interval=None) or 0.0
            system_normalized_cpu_percent = per_core_cpu_percent / logical_cpu_core_count

            process_full_memory_info = target_process_object.memory_full_info()
            process_memory_bytes = getattr(process_full_memory_info, 'pss', None)
            if process_memory_bytes is None:
                process_memory_bytes = process_full_memory_info.rss

            process_memory_megabytes = process_memory_bytes / BYTES_PER_MEGABYTE

            user_process_summaries.append({
                'pid': process_pid,
                'name': process_name,
                'cpu_percent': round(system_normalized_cpu_percent, 1),
                'memory_mb': round(process_memory_megabytes, 1)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    for cached_pid in list(cached_psutil_processes_by_pid.keys()):
        if cached_pid not in discovered_process_pids:
            del cached_psutil_processes_by_pid[cached_pid]

    return sorted(
        user_process_summaries,
        key=lambda process_summary: process_summary['cpu_percent'],
        reverse=True
    )


def aggregate_processes_by_exact_name(process_summaries, sort_column=DEFAULT_SORT_COLUMN_LABEL, is_ascending=False):
    process_groups_by_name = {}

    for process_summary in process_summaries:
        process_name = process_summary['name']

        if process_name not in process_groups_by_name:
            process_groups_by_name[process_name] = {
                'name': process_name,
                'instances': [],
                'total_cpu_percent': 0.0,
                'total_memory_mb': 0.0,
                'lowest_pid': process_summary['pid']
            }

        process_group_record = process_groups_by_name[process_name]
        process_group_record['instances'].append(process_summary)
        process_group_record['total_cpu_percent'] += process_summary['cpu_percent']
        process_group_record['total_memory_mb'] += process_summary['memory_mb']
        process_group_record['lowest_pid'] = min(process_group_record['lowest_pid'], process_summary['pid'])

    def get_group_sort_key(process_group_record):
        if sort_column == "PID":
            return process_group_record['lowest_pid']
        elif sort_column == "Process Name":
            return process_group_record['name'].lower()
        elif sort_column == "Memory (MB)":
            return process_group_record['total_memory_mb']
        else:
            return process_group_record['total_cpu_percent']

    def get_instance_sort_key(process_instance):
        if sort_column == "PID":
            return process_instance['pid']
        elif sort_column == "Process Name":
            return process_instance['name'].lower()
        elif sort_column == "Memory (MB)":
            return process_instance['memory_mb']
        else:
            return process_instance['cpu_percent']

    for process_group_record in process_groups_by_name.values():
        if len(process_group_record['instances']) > 1:
            process_group_record['instances'].sort(key=get_instance_sort_key, reverse=not is_ascending)

    multi_instance_groups = [
        process_group_record
        for process_group_record in process_groups_by_name.values()
        if len(process_group_record['instances']) > 1
    ]
    single_instance_groups = [
        process_group_record
        for process_group_record in process_groups_by_name.values()
        if len(process_group_record['instances']) == 1
    ]

    multi_instance_groups.sort(key=get_group_sort_key, reverse=not is_ascending)
    single_instance_groups.sort(key=get_group_sort_key, reverse=not is_ascending)

    return multi_instance_groups + single_instance_groups


def handle_table_sort_callback(sender, sort_specs):
    global current_sort_column, current_sort_ascending

    if not sort_specs:
        return

    sorted_column_id = sort_specs[0][0]
    sort_direction = sort_specs[0][1]

    sort_column_label_by_column_tag = {
        "ColumnPid": "PID",
        "ColumnProcessName": "Process Name",
        "ColumnCpuPercent": "CPU %",
        "ColumnMemoryMb": "Memory (MB)"
    }

    sorted_column_tag = dpg.get_item_user_data(sorted_column_id)
    current_sort_column = sort_column_label_by_column_tag.get(sorted_column_tag, DEFAULT_SORT_COLUMN_LABEL)
    current_sort_ascending = (sort_direction == SORT_DIRECTION_ASCENDING)

    refresh_process_table_contents()


def refresh_process_table_contents():
    if not dpg.does_item_exist("ProcessTable"):
        return

    dpg.delete_item("ProcessTable", children_only=True)

    dpg.add_table_column(
        label="PID", tag="ColumnPid", user_data="ColumnPid", parent="ProcessTable",
        width_fixed=True, init_width_or_weight=80
    )
    dpg.add_table_column(
        label="Group", parent="ProcessTable",
        width_fixed=True, init_width_or_weight=40, no_sort=True
    )
    dpg.add_table_column(
        label="Process Name", tag="ColumnProcessName", user_data="ColumnProcessName", parent="ProcessTable"
    )
    dpg.add_table_column(
        label="CPU %", tag="ColumnCpuPercent", user_data="ColumnCpuPercent", parent="ProcessTable",
        width_fixed=True, init_width_or_weight=80, prefer_sort_descending=True
    )
    dpg.add_table_column(
        label="Memory (MB)", tag="ColumnMemoryMb", user_data="ColumnMemoryMb", parent="ProcessTable",
        width_fixed=True, init_width_or_weight=100, prefer_sort_descending=True
    )

    system_wide_cpu_percent = psutil.cpu_percent(interval=None) or 0.0
    system_memory_info = psutil.virtual_memory()
    system_used_memory_mb = round(system_memory_info.used / BYTES_PER_MEGABYTE, 1)

    with dpg.table_row(parent="ProcessTable"):
        dpg.add_text("TOTAL", color=TOTAL_ROW_TEXT_COLOR)
        dpg.add_text("", color=TOTAL_ROW_TEXT_COLOR)
        dpg.add_text("All System Processes & Kernel", color=TOTAL_ROW_TEXT_COLOR)
        dpg.add_text(str(round(system_wide_cpu_percent, 1)), color=TOTAL_ROW_TEXT_COLOR)
        dpg.add_text(str(system_used_memory_mb), color=TOTAL_ROW_TEXT_COLOR)

    user_process_summaries = fetch_filtered_user_processes()

    process_name_filter_text = (
        dpg.get_value("ProcessSearchFilter").strip().lower()
        if dpg.does_item_exist("ProcessSearchFilter")
        else ""
    )
    if process_name_filter_text:
        user_process_summaries = [
            process_summary
            for process_summary in user_process_summaries
            if process_name_filter_text in process_summary['name'].lower()
        ]

    process_groups = aggregate_processes_by_exact_name(
        user_process_summaries,
        sort_column=current_sort_column,
        is_ascending=current_sort_ascending
    )

    for process_group_record in process_groups:
        group_name = process_group_record['name']
        is_group_expanded = group_name in expanded_process_group_names
        instance_count = len(process_group_record['instances'])

        if instance_count > 1:
            row_label_text = "[-]" if is_group_expanded else "[+]"
            instance_count_text = f"({instance_count})"
        else:
            row_label_text = str(process_group_record['instances'][0]['pid'])
            instance_count_text = ""

        with dpg.table_row(parent="ProcessTable"):
            if instance_count > 1:
                dpg.add_selectable(
                    label=row_label_text,
                    span_columns=True,
                    callback=toggle_group_expansion_callback,
                    user_data=process_group_record
                )
            else:
                dpg.add_selectable(
                    label=row_label_text,
                    span_columns=True,
                    callback=handle_process_row_click,
                    user_data=process_group_record['instances'][0]
                )

            dpg.add_text(instance_count_text)
            dpg.add_text(group_name)
            dpg.add_text(str(round(process_group_record['total_cpu_percent'], 1)))
            dpg.add_text(str(round(process_group_record['total_memory_mb'], 1)))

        if instance_count > 1 and is_group_expanded:
            for child_process_summary in process_group_record['instances']:
                with dpg.table_row(parent="ProcessTable"):
                    dpg.add_selectable(
                        label=f"- {child_process_summary['pid']}",
                        span_columns=True,
                        callback=handle_process_row_click,
                        user_data=child_process_summary
                    )
                    dpg.add_text("")
                    dpg.add_text(f"    {child_process_summary['name']}")
                    dpg.add_text(str(child_process_summary['cpu_percent']))
                    dpg.add_text(str(child_process_summary['memory_mb']))


initialize_database()
threading.Thread(target=scan_running_processes_into_description_database, daemon=True).start()

dpg.create_context()

with dpg.theme(tag="LinkTheme"):
    with dpg.theme_component(dpg.mvButton):
        dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (255, 255, 255, 30))
        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (255, 255, 255, 60))
        dpg.add_theme_color(dpg.mvThemeCol_Text, (90, 160, 255))
        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 0)

with dpg.handler_registry():
    dpg.add_mouse_click_handler(callback=handle_global_mouse_click)

with dpg.window(label="Active Processor Monitor", tag="PrimaryWindow", width=950, height=750):
    dpg.add_text("Filtered User Processes", color=HEADER_TEXT_COLOR)

    dpg.add_input_text(
        label="Filter Processes",
        tag="ProcessSearchFilter",
        hint="Type to filter processes...",
        callback=lambda sender, app_data: refresh_process_table_contents()
    )

    dpg.add_separator()

    with dpg.table(
        header_row=True,
        resizable=True,
        scrollY=True,
        height=400,
        tag="ProcessTable",
        sortable=True,
        callback=handle_table_sort_callback
    ):
        pass

    dpg.add_spacer(height=10)
    dpg.add_separator()

    dpg.add_button(
        label=DEFAULT_HEADER_BUTTON_LABEL,
        tag="SelectedProcessHeaderButton"
    )

    with dpg.popup("SelectedProcessHeaderButton", mousebutton=dpg.mvMouseButton_Left):
        dpg.add_text("Process Actions:")
        dpg.add_separator()
        dpg.add_menu_item(
            label="Terminate (SIGTERM - Safe)",
            callback=lambda: terminate_selected_process(signal.SIGTERM)
        )
        dpg.add_menu_item(
            label="Kill Immediately (SIGKILL - Force)",
            callback=lambda: terminate_selected_process(signal.SIGKILL)
        )
        dpg.add_menu_item(
            label="Reload Config (SIGHUP)",
            callback=lambda: terminate_selected_process(signal.SIGHUP)
        )

    with dpg.child_window(tag="DescriptionPanel", height=180, border=True):
        dpg.add_text(
            NO_PROCESS_SELECTED_PLACEHOLDER_TEXT,
            tag="DescriptionPlaceholderText",
            wrap=DESCRIPTION_TEXT_WRAP_WIDTH_PIXELS
        )

refresh_process_table_contents()

dpg.create_viewport(title='Active Processor Monitor', width=1000, height=800)
dpg.setup_dearpygui()
dpg.show_viewport()
dpg.set_primary_window("PrimaryWindow", True)

last_table_refresh_timestamp = time.time()

while dpg.is_dearpygui_running():
    current_timestamp = time.time()

    if (
        not is_table_refresh_paused
        and current_timestamp - last_table_refresh_timestamp >= TABLE_REFRESH_INTERVAL_SECONDS
    ):
        refresh_process_table_contents()
        last_table_refresh_timestamp = current_timestamp

    dpg.render_dearpygui_frame()

dpg.destroy_context()