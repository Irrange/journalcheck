from __future__ import annotations

import csv
import queue
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import tkinter as tk

from journalcheck.bootstrap import bootstrap_vendor

bootstrap_vendor()

from journalcheck.config import (
    DEFAULT_NETWORK_VALUES,
    DEFAULT_NOTIFICATION_VALUES,
    DEFAULT_OUTPUT_VALUES,
    NETWORK_KEYS,
    NOTIFICATION_KEYS,
    OUTPUT_KEYS,
    SITE_FIELD_SPECS,
    apply_env_map,
    build_env_map,
    collect_site_configs,
    parse_env_file,
    write_env_file,
)
from journalcheck.notifications import send_test_email
from journalcheck.service import format_change_for_display, load_saved_rows, patched_environment, refresh_statuses
from journalcheck.single_instance import acquire_single_instance, show_already_running_message
from journalcheck.storage import relabel_saved_snapshot
from journalcheck.utils import load_env, site_family
from journalcheck.windows_tray import WindowsTrayIcon

SITE_NAMES = {"aha": "AHA", "em": "Editorial Manager", "bmc": "BMC"}
FIELD_LABELS = {
    "base_url": "Base URL",
    "submission_url": "Submission URL",
    "journal_code": "Journal Code",
    "username": "Username",
    "password": "Password",
}


class AccountFamilyEditor(ttk.LabelFrame):
    def __init__(self, master: tk.Misc, site_key: str) -> None:
        super().__init__(master, text=f"{SITE_NAMES[site_key]} Accounts", padding=12)
        self.site_key = site_key
        self.rows: list[dict[str, object]] = []

        top_bar = ttk.Frame(self)
        top_bar.pack(fill="x")
        ttk.Label(top_bar, text="Add or remove accounts for this site family.").pack(side="left")
        ttk.Button(top_bar, text="Add Account", command=self.add_row).pack(side="right")

        self.rows_container = ttk.Frame(self)
        self.rows_container.pack(fill="x", pady=(10, 0))

    def add_row(self, values: dict[str, str] | None = None) -> None:
        values = values or {}
        frame = ttk.Frame(self.rows_container, padding=10)
        frame.pack(fill="x", pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        title = ttk.Label(frame, text="")
        title.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(frame, text="Remove", command=lambda: self.remove_row(frame)).grid(row=0, column=2, sticky="e")

        vars_by_field: dict[str, tk.StringVar] = {}
        for row_index, (field_name, _) in enumerate(SITE_FIELD_SPECS[self.site_key], start=1):
            ttk.Label(frame, text=FIELD_LABELS.get(field_name, field_name)).grid(
                row=row_index, column=0, sticky="w", padx=(0, 10), pady=4
            )
            var = tk.StringVar(value=values.get(field_name, ""))
            entry = ttk.Entry(frame, textvariable=var, show="*" if field_name == "password" else "")
            entry.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=4)
            vars_by_field[field_name] = var

        self.rows.append({"frame": frame, "title": title, "vars": vars_by_field})
        self._refresh_titles()

    def remove_row(self, frame: ttk.Frame) -> None:
        self.rows = [row for row in self.rows if row["frame"] is not frame]
        frame.destroy()
        self._refresh_titles()

    def load_configs(self, configs: list[dict[str, str]]) -> None:
        for row in self.rows:
            row["frame"].destroy()
        self.rows.clear()
        for config in configs:
            self.add_row(config)
        if not configs:
            self.add_row()

    def get_configs(self) -> list[dict[str, str]]:
        configs: list[dict[str, str]] = []
        for index, row in enumerate(self.rows, start=1):
            vars_by_field: dict[str, tk.StringVar] = row["vars"]  # type: ignore[assignment]
            values = {field_name: var.get().strip() for field_name, var in vars_by_field.items()}
            if not any(values.values()):
                continue
            missing = [FIELD_LABELS.get(field_name, field_name) for field_name, value in values.items() if not value]
            if missing:
                raise ValueError(f"{SITE_NAMES[self.site_key]} account {index} is incomplete: {', '.join(missing)}")
            configs.append(values)
        return configs

    def get_named_configs(self) -> list[tuple[str, dict[str, str]]]:
        return [(f"{self.site_key}_{index}", config) for index, config in enumerate(self.get_configs(), start=1)]

    def plan_removal(self, site_names: set[str]) -> tuple[list[dict[str, str]], list[str], dict[str, str]]:
        remaining_configs: list[dict[str, str]] = []
        removed_sites: list[str] = []
        renamed_sites: dict[str, str] = {}

        next_index = 1
        for site_name, config in self.get_named_configs():
            if site_name in site_names:
                removed_sites.append(site_name)
                continue
            new_site_name = f"{self.site_key}_{next_index}"
            if site_name != new_site_name:
                renamed_sites[site_name] = new_site_name
            remaining_configs.append(config)
            next_index += 1

        return remaining_configs, removed_sites, renamed_sites

    def _refresh_titles(self) -> None:
        for index, row in enumerate(self.rows, start=1):
            title: ttk.Label = row["title"]  # type: ignore[assignment]
            title.config(text=f"{SITE_NAMES[self.site_key]} Account {index}")


class JournalCheckGUI:
    def __init__(self, root: tk.Tk, project_root: Path) -> None:
        self.root = root
        self.project_root = project_root
        self.env_path = project_root / ".env"
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.task_thread: threading.Thread | None = None
        self.auto_refresh_enabled = False
        self.auto_after_id: str | None = None
        self.current_rows = []
        self.current_changes: list[dict[str, object]] = []
        self.current_warnings: list[str] = []
        self.row_lookup: dict[str, object] = {}
        self.change_lookup: dict[str, dict[str, object]] = {}
        self.overview_split_initialized = False
        self.all_article_lookup: dict[str, dict[str, object]] = {}
        self.all_article_history_lookup: dict[str, list[dict[str, str]]] = {}
        self.mousewheel_targets: dict[str, tk.Misc] = {}
        self.app_icon_png_path = project_root / "assets" / "journalcheck_icon.png"
        self.app_icon_path = project_root / "assets" / "journalcheck_icon.ico"
        self.app_icon_photo: tk.PhotoImage | None = None
        self.tray_icon: WindowsTrayIcon | None = None
        self.hidden_to_tray = False
        self.is_closing = False
        self.extra_values = {**DEFAULT_OUTPUT_VALUES, **DEFAULT_NOTIFICATION_VALUES, **DEFAULT_NETWORK_VALUES}
        self.current_task_mode = 'idle'

        self.root.title("JournalCheck")
        self.root.geometry("1380x900")
        self.root.minsize(1120, 760)

        self._configure_style()
        self._configure_window_icon()
        self._build_layout()
        self._setup_tray_support()
        self._load_env_into_form()
        self._load_saved_snapshot()
        self.root.after(200, self._poll_result_queue)
        self.root.after(300, self._ensure_overview_split_expanded)
        self.root.bind("<Control-s>", self._on_save_shortcut)
        self.root.bind("<Control-S>", self._on_save_shortcut)
        self._bind_global_mousewheel()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("vista", "clam", "default"):
            try:
                style.theme_use(theme)
                break
            except Exception:
                continue
        style.configure("Toolbar.TButton", padding=(10, 6))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Subtle.TLabel", foreground="#666666")
        style.configure("Treeview", rowheight=28)

    def _bind_global_mousewheel(self) -> None:
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_global_mousewheel, add="+")

    def _register_mousewheel_target(self, widget: tk.Misc, target: tk.Misc | None = None) -> None:
        self.mousewheel_targets[str(widget)] = target or widget

    def _resolve_mousewheel_target(self) -> tk.Misc | None:
        try:
            pointer_x, pointer_y = self.root.winfo_pointerxy()
            widget = self.root.winfo_containing(pointer_x, pointer_y)
        except Exception:
            return None

        while widget is not None:
            target = self.mousewheel_targets.get(str(widget))
            if target is not None:
                return target
            parent_name = widget.winfo_parent()
            if not parent_name:
                return None
            try:
                widget = widget.nametowidget(parent_name)
            except Exception:
                return None
        return None

    @staticmethod
    def _mousewheel_units(event: tk.Event) -> int:
        if getattr(event, "num", None) == 4:
            return -1
        if getattr(event, "num", None) == 5:
            return 1
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:
            return 0
        if abs(delta) >= 120:
            return -int(delta / 120)
        return -1 if delta > 0 else 1

    def _on_global_mousewheel(self, event: tk.Event) -> str | None:
        target = self._resolve_mousewheel_target()
        if target is None:
            return None
        units = self._mousewheel_units(event)
        if units == 0:
            return None
        try:
            target.yview_scroll(units, "units")
        except Exception:
            return None
        return "break"

    def _configure_window_icon(self) -> None:
        if self.app_icon_png_path.exists():
            try:
                self.app_icon_photo = tk.PhotoImage(file=str(self.app_icon_png_path))
                self.root.iconphoto(True, self.app_icon_photo)
            except Exception:
                self.app_icon_photo = None
        if self.app_icon_path.exists():
            try:
                self.root.iconbitmap(str(self.app_icon_path))
            except Exception:
                pass

    def _setup_tray_support(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)
        if sys.platform != "win32" or not self.app_icon_path.exists():
            return
        self.tray_icon = WindowsTrayIcon(
            self.app_icon_path,
            "JournalCheck",
            lambda: self.result_queue.put(("tray_restore", None)),
        )
        self.root.bind("<Unmap>", self._on_root_unmap, add="+")

    def _on_root_unmap(self, _event=None) -> None:
        if self.tray_icon is None or self.hidden_to_tray or self.is_closing:
            return
        self.root.after(0, self._maybe_minimize_to_tray)

    def _maybe_minimize_to_tray(self) -> None:
        if self.tray_icon is None or self.hidden_to_tray or self.is_closing:
            return
        try:
            state = self.root.state()
        except Exception:
            return
        if state != "iconic":
            return
        if not self.tray_icon.show():
            return
        self.hidden_to_tray = True
        self.root.withdraw()
        self.status_var.set("Minimized to tray. Click the tray icon to restore.")

    def _restore_from_tray(self) -> None:
        if not self.hidden_to_tray:
            return
        self.hidden_to_tray = False
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.root.deiconify()
        self.root.state("normal")
        self.root.after(50, self._raise_window)
        self.status_var.set("Restored from tray.")

    def _raise_window(self) -> None:
        try:
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _on_close_window(self) -> None:
        self.is_closing = True
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.root.destroy()

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="JournalCheck", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Refresh status, manage accounts, and review changes.", style="Subtle.TLabel").grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(14, 12))
        toolbar.columnconfigure(8, weight=1)

        self.refresh_button = ttk.Button(toolbar, text="Refresh Now", style="Toolbar.TButton", command=self.refresh_now)
        self.refresh_button.grid(row=0, column=0, padx=(0, 8))
        self.save_button = ttk.Button(toolbar, text="Save All Settings", style="Toolbar.TButton", command=self.save_configuration)
        self.save_button.grid(row=0, column=1, padx=(0, 8))
        self.test_email_button = ttk.Button(toolbar, text="Test Email", style="Toolbar.TButton", command=self.test_email)
        self.test_email_button.grid(row=0, column=2, padx=(0, 14))

        ttk.Label(toolbar, text="Auto Refresh").grid(row=0, column=3, padx=(0, 6))
        self.auto_interval_var = tk.StringVar(value="30")
        self.auto_interval_box = ttk.Combobox(
            toolbar,
            textvariable=self.auto_interval_var,
            values=["15", "30", "60", "120"],
            width=8,
            state="readonly",
        )
        self.auto_interval_box.grid(row=0, column=4, padx=(0, 8))
        ttk.Button(toolbar, text="Start", command=self.start_auto_refresh).grid(row=0, column=5, padx=(0, 6))
        ttk.Button(toolbar, text="Stop", command=self.stop_auto_refresh).grid(row=0, column=6, padx=(0, 14))
        self.next_refresh_var = tk.StringVar(value="Next: not scheduled")
        ttk.Label(toolbar, textvariable=self.next_refresh_var, style="Subtle.TLabel").grid(row=0, column=7, sticky="w")

        notebook = ttk.Notebook(outer)
        notebook.grid(row=2, column=0, sticky="nsew")

        self.overview_tab = ttk.Frame(notebook, padding=12)
        self.all_articles_tab = ttk.Frame(notebook, padding=12)
        self.accounts_tab = ttk.Frame(notebook, padding=12)
        self.notifications_tab = ttk.Frame(notebook, padding=12)
        notebook.add(self.overview_tab, text="Overview")
        notebook.add(self.all_articles_tab, text="All Articles")
        notebook.add(self.accounts_tab, text="Accounts")
        notebook.add(self.notifications_tab, text="Notifications")

        self._build_overview_tab()
        self._build_all_articles_tab()
        self._build_accounts_tab()
        self._build_notifications_tab()

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(outer, textvariable=self.status_var, style="Subtle.TLabel").grid(row=3, column=0, sticky="ew", pady=(10, 0))

    def _build_overview_tab(self) -> None:
        self.overview_tab.columnconfigure(0, weight=1)
        self.overview_tab.rowconfigure(2, weight=1)

        summary = ttk.Frame(self.overview_tab)
        summary.grid(row=0, column=0, sticky="ew")
        for col in range(4):
            summary.columnconfigure(col, weight=1)

        self.last_refresh_var = tk.StringVar(value="-")
        self.total_count_var = tk.StringVar(value="0")
        self.change_count_var = tk.StringVar(value="No changes")
        self.auto_state_var = tk.StringVar(value="Manual mode")

        self._make_stat_card(summary, 0, "Last Refresh", self.last_refresh_var)
        self._make_stat_card(summary, 1, "Active Submissions", self.total_count_var)
        self._make_stat_card(summary, 2, "Change Status", self.change_count_var)
        self._make_stat_card(summary, 3, "Auto Refresh", self.auto_state_var)

        change_row = ttk.Frame(self.overview_tab)
        change_row.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        change_row.columnconfigure(0, weight=1)
        change_frame = ttk.LabelFrame(change_row, text="What Changed", padding=10)
        change_frame.grid(row=0, column=0, sticky="ew")
        change_frame.columnconfigure(0, weight=1)
        self.change_text = ScrolledText(change_frame, height=4, wrap="word", font=("Microsoft YaHei UI", 10))
        self.change_text.grid(row=0, column=0, sticky="ew")
        self.change_text.tag_configure("changed", foreground="#b42318")
        self.change_text.tag_configure("warning", foreground="#b54708")
        self.change_text.tag_configure("muted", foreground="#666666")
        self.change_text.config(state="disabled")
        self._register_mousewheel_target(change_frame, self.change_text)
        self._register_mousewheel_target(self.change_text)

        self.overview_split = ttk.Panedwindow(self.overview_tab, orient="vertical")
        self.overview_split.grid(row=2, column=0, sticky="nsew")

        list_frame = ttk.LabelFrame(self.overview_split, text="Active Submissions", padding=8)
        detail_frame = ttk.LabelFrame(self.overview_split, text="Details", padding=8)
        self.overview_split.add(list_frame, weight=3)
        self.overview_split.add(detail_frame, weight=2)

        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("summary", "status", "site", "updated", "reviewers")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "summary": "Submission",
            "status": "Status",
            "site": "Site",
            "updated": "Status Date",
            "reviewers": "Reviewers",
        }
        widths = {
            "summary": 760,
            "status": 220,
            "site": 110,
            "updated": 120,
            "reviewers": 130,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.tag_configure("changed", background="#fdecea")
        self.tree.tag_configure("normal", background="#ffffff")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_row_selected)
        self.tree.bind("<Double-1>", lambda _event: self.open_detail_link())
        self._register_mousewheel_target(list_frame, self.tree)
        self._register_mousewheel_target(self.tree)

        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)
        detail_header = ttk.Frame(detail_frame)
        detail_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        detail_header.columnconfigure(0, weight=1)
        self.detail_title_var = tk.StringVar(value="Select a submission to view details.")
        ttk.Label(detail_header, textvariable=self.detail_title_var, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        button_bar = ttk.Frame(detail_header)
        button_bar.grid(row=0, column=1, sticky="e")
        ttk.Button(button_bar, text="Open Detail Link", command=self.open_detail_link).pack(side="left", padx=(0, 8))
        ttk.Button(button_bar, text="Open Review Link", command=self.open_review_link).pack(side="left")

        self.detail_text = ScrolledText(detail_frame, wrap="word", font=("Microsoft YaHei UI", 10))
        self.detail_text.grid(row=1, column=0, sticky="nsew")
        self.detail_text.config(state="disabled")
        self._register_mousewheel_target(detail_frame, self.detail_text)
        self._register_mousewheel_target(self.detail_text)

    def _make_stat_card(self, parent: ttk.Frame, column: int, title: str, variable: tk.StringVar) -> ttk.Frame:
        card = ttk.Frame(parent, padding=12, relief="ridge")
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        ttk.Label(card, text=title, style="Subtle.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=variable, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(8, 0))
        return card

    def _build_all_articles_tab(self) -> None:
        self.all_articles_tab.columnconfigure(0, weight=1)
        self.all_articles_tab.rowconfigure(1, weight=1)

        header = ttk.Frame(self.all_articles_tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="All queried articles are collected from the cumulative refresh log.",
            style="Subtle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.all_article_summary_var = tk.StringVar(value="0 total | 0 active | 0 archived")
        ttk.Label(header, textvariable=self.all_article_summary_var, style="Subtle.TLabel").grid(row=0, column=1, sticky="e")

        split = ttk.Panedwindow(self.all_articles_tab, orient="vertical")
        split.grid(row=1, column=0, sticky="nsew")

        list_frame = ttk.LabelFrame(split, text="All Articles", padding=8)
        detail_frame = ttk.LabelFrame(split, text="Article Details", padding=8)
        split.add(list_frame, weight=3)
        split.add(detail_frame, weight=2)

        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("summary", "status", "state", "site", "last_seen")
        self.all_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "summary": "Submission",
            "status": "Latest Status",
            "state": "State",
            "site": "Site",
            "last_seen": "Last Seen",
        }
        widths = {
            "summary": 720,
            "status": 240,
            "state": 90,
            "site": 110,
            "last_seen": 180,
        }
        for column in columns:
            self.all_tree.heading(column, text=headings[column])
            self.all_tree.column(column, width=widths[column], anchor="w")
        self.all_tree.tag_configure("active", background="#eefaf2")
        self.all_tree.tag_configure("archived", background="#f7f7f7")
        self.all_tree.grid(row=0, column=0, sticky="nsew")
        all_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.all_tree.yview)
        all_scroll.grid(row=0, column=1, sticky="ns")
        self.all_tree.configure(yscrollcommand=all_scroll.set)
        self.all_tree.bind("<<TreeviewSelect>>", self.on_all_row_selected)
        self.all_tree.bind("<Double-1>", lambda _event: self.open_all_detail_link())
        self._register_mousewheel_target(list_frame, self.all_tree)
        self._register_mousewheel_target(self.all_tree)

        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)
        detail_header = ttk.Frame(detail_frame)
        detail_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        detail_header.columnconfigure(0, weight=1)
        self.all_detail_title_var = tk.StringVar(value="Select an article to view its history.")
        ttk.Label(detail_header, textvariable=self.all_detail_title_var, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(detail_header, text="Open Detail Link", command=self.open_all_detail_link).grid(row=0, column=1, sticky="e")

        self.all_detail_text = ScrolledText(detail_frame, wrap="word", font=("Microsoft YaHei UI", 10))
        self.all_detail_text.grid(row=1, column=0, sticky="nsew")
        self.all_detail_text.config(state="disabled")
        self._register_mousewheel_target(detail_frame, self.all_detail_text)
        self._register_mousewheel_target(self.all_detail_text)

    def _build_accounts_tab(self) -> None:
        self.accounts_tab.columnconfigure(0, weight=1)
        self.accounts_tab.rowconfigure(1, weight=1)

        header = ttk.Frame(self.accounts_tab)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="Edit AHA, EM, and BMC accounts here. Click Save Accounts to write the changes to .env.",
            style="Subtle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            header,
            text="Save Accounts",
            style="Toolbar.TButton",
            command=self.save_configuration,
        ).grid(row=0, column=1, sticky="e")

        canvas = tk.Canvas(self.accounts_tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.accounts_tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(canvas_window, width=event.width))
        self._register_mousewheel_target(self.accounts_tab, canvas)
        self._register_mousewheel_target(header, canvas)
        self._register_mousewheel_target(canvas)
        self._register_mousewheel_target(inner, canvas)

        self.account_editors = {
            "aha": AccountFamilyEditor(inner, "aha"),
            "em": AccountFamilyEditor(inner, "em"),
            "bmc": AccountFamilyEditor(inner, "bmc"),
        }
        self.account_editors["aha"].pack(fill="x", pady=(0, 12))
        self.account_editors["em"].pack(fill="x", pady=(0, 12))
        self.account_editors["bmc"].pack(fill="x")

    def _build_notifications_tab(self) -> None:
        self.notifications_tab.columnconfigure(0, weight=1)
        self.notifications_tab.columnconfigure(1, weight=1)
        self.notifications_tab.rowconfigure(0, weight=1)

        email_box = ttk.LabelFrame(self.notifications_tab, text="Email", padding=12)
        email_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        email_box.columnconfigure(1, weight=1)

        self.notification_vars = {
            "SMTP_HOST": tk.StringVar(),
            "SMTP_PORT": tk.StringVar(),
            "SMTP_USERNAME": tk.StringVar(),
            "SMTP_PASSWORD": tk.StringVar(),
            "SMTP_FROM": tk.StringVar(),
            "SMTP_TO": tk.StringVar(),
            "WECOM_WEBHOOK_URL": tk.StringVar(),
        }
        self.network_vars = {
            "HTTP_PROXY": tk.StringVar(),
            "HTTPS_PROXY": tk.StringVar(),
            "NO_PROXY": tk.StringVar(),
        }
        self.smtp_tls_var = tk.BooleanVar(value=True)
        self.smtp_ssl_var = tk.BooleanVar(value=False)

        email_fields = [
            ("SMTP_HOST", "SMTP Host"),
            ("SMTP_PORT", "SMTP Port"),
            ("SMTP_USERNAME", "SMTP Username"),
            ("SMTP_PASSWORD", "SMTP Password"),
            ("SMTP_FROM", "SMTP From"),
            ("SMTP_TO", "SMTP To"),
        ]
        for row_index, (key, label) in enumerate(email_fields):
            ttk.Label(email_box, text=label).grid(row=row_index, column=0, sticky="w", padx=(0, 10), pady=6)
            ttk.Entry(
                email_box,
                textvariable=self.notification_vars[key],
                show="*" if key == "SMTP_PASSWORD" else "",
            ).grid(row=row_index, column=1, sticky="ew", pady=6)

        ttk.Checkbutton(email_box, text="Use TLS", variable=self.smtp_tls_var).grid(row=6, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(email_box, text="Use SSL", variable=self.smtp_ssl_var).grid(row=6, column=1, sticky="w", pady=(6, 0))

        wecom_box = ttk.LabelFrame(self.notifications_tab, text="WeCom", padding=12)
        wecom_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        wecom_box.columnconfigure(1, weight=1)
        ttk.Label(wecom_box, text="Webhook URL").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=6)
        ttk.Entry(wecom_box, textvariable=self.notification_vars["WECOM_WEBHOOK_URL"]).grid(
            row=0, column=1, sticky="ew", pady=6
        )
        ttk.Label(
            wecom_box,
            text="Configure SMTP or a WeCom webhook here. The Refresh action uses these settings immediately.",
            style="Subtle.TLabel",
            wraplength=360,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))

        proxy_box = ttk.LabelFrame(self.notifications_tab, text="Network Proxy", padding=12)
        proxy_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        proxy_box.columnconfigure(1, weight=1)
        proxy_fields = [
            ("HTTP_PROXY", "HTTP Proxy"),
            ("HTTPS_PROXY", "HTTPS Proxy"),
            ("NO_PROXY", "No Proxy"),
        ]
        for row_index, (key, label) in enumerate(proxy_fields):
            ttk.Label(proxy_box, text=label).grid(row=row_index, column=0, sticky="w", padx=(0, 10), pady=6)
            ttk.Entry(proxy_box, textvariable=self.network_vars[key]).grid(row=row_index, column=1, sticky="ew", pady=6)
        ttk.Label(
            proxy_box,
            text=(
                "Use explicit proxy URLs such as http://proxy-host:proxy-port. "
                "NO_PROXY accepts a comma-separated bypass list such as "
                "example.com,example.org."
            ),
            style="Subtle.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))

        footer = ttk.Frame(self.notifications_tab)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="Save writes notification and proxy settings to .env. Refresh and Test Email use the current values in this form.",
            style="Subtle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            footer,
            text="Save Notifications",
            style="Toolbar.TButton",
            command=self.save_configuration,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(
            footer,
            text="Test Email",
            style="Toolbar.TButton",
            command=self.test_email,
        ).grid(row=0, column=2, sticky="e", padx=(8, 0))

    def _load_env_into_form(self) -> None:
        env_values = parse_env_file(self.env_path)
        self.extra_values = {**DEFAULT_OUTPUT_VALUES, **DEFAULT_NOTIFICATION_VALUES, **DEFAULT_NETWORK_VALUES}
        for key in [*OUTPUT_KEYS, *NOTIFICATION_KEYS, *NETWORK_KEYS]:
            if key in env_values:
                self.extra_values[key] = env_values[key]

        for site_key, editor in self.account_editors.items():
            try:
                configs = collect_site_configs(site_key, env_map=env_values)
            except Exception as exc:
                messagebox.showwarning("Configuration warning", str(exc))
                configs = []
            editor.load_configs([config for _, config in configs])

        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO", "WECOM_WEBHOOK_URL"):
            self.notification_vars[key].set(self.extra_values.get(key, DEFAULT_NOTIFICATION_VALUES.get(key, "")))
        self.smtp_tls_var.set(self.extra_values.get("SMTP_USE_TLS", "true").lower() == "true")
        self.smtp_ssl_var.set(self.extra_values.get("SMTP_USE_SSL", "false").lower() == "true")
        for key in NETWORK_KEYS:
            self.network_vars[key].set(self.extra_values.get(key, DEFAULT_NETWORK_VALUES.get(key, "")))

    def _load_saved_snapshot(self) -> None:
        try:
            last_refreshed_at, rows = load_saved_rows(self.project_root, env_map=self.extra_values)
        except Exception as exc:
            self.status_var.set(f"Failed to load saved snapshot: {exc}")
            return
        if rows:
            self._update_result_view(rows=rows, changes=[], refreshed_at=last_refreshed_at or "-", warnings=[])
            self.status_var.set("Loaded saved snapshot.")
        else:
            self._refresh_all_articles_view()

    def _collect_form_env_map(self, site_config_overrides: dict[str, list[dict[str, str]]] | None = None) -> dict[str, str]:
        site_config_overrides = site_config_overrides or {}
        site_configs = {
            site_key: site_config_overrides.get(site_key, editor.get_configs())
            for site_key, editor in self.account_editors.items()
        }
        extras = {key: self.extra_values.get(key, "") for key in [*OUTPUT_KEYS, *NOTIFICATION_KEYS, *NETWORK_KEYS]}
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO", "WECOM_WEBHOOK_URL"):
            extras[key] = self.notification_vars[key].get().strip()
        extras["SMTP_USE_TLS"] = "true" if self.smtp_tls_var.get() else "false"
        extras["SMTP_USE_SSL"] = "true" if self.smtp_ssl_var.get() else "false"
        for key in NETWORK_KEYS:
            extras[key] = self.network_vars[key].get().strip()
        return build_env_map(site_configs, extras=extras)

    def _persist_env_map(self, env_map: dict[str, str]) -> None:
        write_env_file(self.env_path, env_map)
        apply_env_map(env_map)
        self.extra_values = {**DEFAULT_OUTPUT_VALUES, **DEFAULT_NOTIFICATION_VALUES, **DEFAULT_NETWORK_VALUES}
        for key in [*OUTPUT_KEYS, *NOTIFICATION_KEYS, *NETWORK_KEYS]:
            if key in env_map:
                self.extra_values[key] = env_map[key]

    def save_configuration(self) -> None:
        try:
            env_map = self._collect_form_env_map()
            self._persist_env_map(env_map)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.status_var.set(f"Saved configuration to {self.env_path}")
        messagebox.showinfo("Saved", "Configuration saved.")

    def _on_save_shortcut(self, _event: tk.Event | None = None) -> str:
        self.save_configuration()
        return "break"

    def refresh_now(self, trigger: str = "manual") -> None:
        if self.task_thread and self.task_thread.is_alive():
            return
        try:
            env_map = self._collect_form_env_map()
        except Exception as exc:
            messagebox.showerror("Refresh failed", str(exc))
            return
        self.current_task_mode = trigger
        self._set_busy(True, "Refreshing status...")
        self.task_thread = threading.Thread(target=self._run_refresh_task, args=(env_map,), daemon=True)
        self.task_thread.start()

    def _run_refresh_task(self, env_map: dict[str, str]) -> None:
        try:
            result = refresh_statuses(self.project_root, save=True, notify=True, env_map=env_map)
            self.result_queue.put(("refresh_ok", result))
        except Exception as exc:
            self.result_queue.put(("refresh_error", str(exc)))

    def test_email(self) -> None:
        if self.task_thread and self.task_thread.is_alive():
            return
        try:
            env_map = self._collect_form_env_map()
        except Exception as exc:
            messagebox.showerror("Email test failed", str(exc))
            return
        self.current_task_mode = "email"
        self._set_busy(True, "Sending test email...")
        self.task_thread = threading.Thread(target=self._run_test_email_task, args=(env_map,), daemon=True)
        self.task_thread.start()

    def _run_test_email_task(self, env_map: dict[str, str]) -> None:
        try:
            with patched_environment(env_map):
                send_test_email()
            self.result_queue.put(("email_ok", None))
        except Exception as exc:
            self.result_queue.put(("email_error", str(exc)))

    def start_auto_refresh(self) -> None:
        interval = self._get_auto_interval_minutes()
        if interval <= 0:
            messagebox.showinfo("Auto refresh", "Please select a valid interval.")
            return
        self.auto_refresh_enabled = True
        self.auto_state_var.set(f"Every {interval} min")
        self.status_var.set("Auto refresh started.")
        self.refresh_now(trigger="auto")

    def stop_auto_refresh(self) -> None:
        self.auto_refresh_enabled = False
        self.auto_state_var.set("Manual mode")
        self.next_refresh_var.set("Next: not scheduled")
        if self.auto_after_id is not None:
            self.root.after_cancel(self.auto_after_id)
            self.auto_after_id = None
        self.status_var.set("Auto refresh stopped.")

    def _schedule_next_refresh(self) -> None:
        if not self.auto_refresh_enabled:
            self.next_refresh_var.set("Next: not scheduled")
            return
        interval = self._get_auto_interval_minutes()
        if interval <= 0:
            self.stop_auto_refresh()
            return
        if self.auto_after_id is not None:
            self.root.after_cancel(self.auto_after_id)
        next_run = datetime.now().astimezone() + timedelta(minutes=interval)
        self.next_refresh_var.set(f"Next: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        self.auto_after_id = self.root.after(interval * 60 * 1000, lambda: self.refresh_now(trigger="auto"))

    def _get_auto_interval_minutes(self) -> int:
        try:
            return int(self.auto_interval_var.get())
        except Exception:
            return 0

    def _poll_result_queue(self) -> None:
        try:
            while True:
                event_type, payload = self.result_queue.get_nowait()
                if event_type == "refresh_ok":
                    self._handle_refresh_success(payload)
                elif event_type == "refresh_error":
                    self._handle_task_error(f"Refresh failed: {payload}")
                elif event_type == "email_ok":
                    self._handle_email_success()
                elif event_type == "email_error":
                    self._handle_task_error(f"Test email failed: {payload}")
                elif event_type == "tray_restore":
                    self._restore_from_tray()
        except queue.Empty:
            pass
        self.root.after(200, self._poll_result_queue)

    def _handle_refresh_success(self, result) -> None:
        auto_cleanup_summary = None
        try:
            auto_cleanup_summary = self._auto_remove_inactive_bmc_accounts(result)
        except Exception as exc:
            result.warnings.append(f"Inactive BMC account cleanup failed: {exc}")
        self._set_busy(False, "Refresh completed.")
        self._update_result_view(result.rows, result.changes, result.refreshed_at, result.warnings)
        if auto_cleanup_summary:
            self.status_var.set(auto_cleanup_summary)
        elif result.warnings:
            self.status_var.set("Refresh completed with warnings.")
        elif result.notifications_sent:
            self.status_var.set(f"Refresh completed. Notification sent via: {', '.join(result.notifications_sent)}")
        else:
            self.status_var.set("Refresh completed.")
        if self.auto_refresh_enabled:
            self._schedule_next_refresh()
        self.current_task_mode = "idle"

    def _auto_remove_inactive_bmc_accounts(self, result) -> str | None:
        removable_sites = {
            str(change.get("site", "")).strip()
            for change in result.changes
            if change.get("type") == "no_longer_active" and site_family(str(change.get("site", ""))) == "bmc"
        }
        if not removable_sites:
            return None

        remaining_configs, removed_sites, renamed_sites = self.account_editors["bmc"].plan_removal(removable_sites)
        if not removed_sites:
            return None

        env_map = self._collect_form_env_map({"bmc": remaining_configs})
        self._persist_env_map(env_map)
        self.account_editors["bmc"].load_configs(remaining_configs)

        for row in result.rows:
            if row.site in renamed_sites:
                row.site = renamed_sites[row.site]

        for change in result.changes:
            if change.get("type") == "no_longer_active":
                continue
            site_name = str(change.get("site", "")).strip()
            if site_name in renamed_sites:
                change["site"] = renamed_sites[site_name]

        if result.json_path is not None and result.csv_path is not None and renamed_sites:
            relabel_saved_snapshot(result.json_path, result.csv_path, renamed_sites)

        summary = "Auto-removed inactive BMC account(s): " + ", ".join(removed_sites)
        if renamed_sites:
            summary += ". Renumbered remaining BMC accounts."
        result.warnings.append(summary)
        return summary

    def _handle_email_success(self) -> None:
        self._set_busy(False, "Test email sent successfully.")
        self.current_task_mode = "idle"
        messagebox.showinfo("Email", "Test email sent successfully.")

    def _handle_task_error(self, message: str) -> None:
        mode = self.current_task_mode
        self._set_busy(False, message)
        if mode == "auto":
            self._render_runtime_warning(message)
            if self.auto_refresh_enabled:
                self._schedule_next_refresh()
            self.current_task_mode = "idle"
            return
        if self.auto_refresh_enabled:
            self._schedule_next_refresh()
        self.current_task_mode = "idle"
        messagebox.showerror("JournalCheck", message)

    def _set_busy(self, busy: bool, message: str) -> None:
        state = "disabled" if busy else "normal"
        self.refresh_button.config(state=state)
        self.save_button.config(state=state)
        self.test_email_button.config(state=state)
        self.status_var.set(message)

    def _update_result_view(self, rows, changes, refreshed_at: str, warnings: list[str] | None = None) -> None:
        self.current_rows = list(rows)
        self.current_changes = list(changes)
        self.current_warnings = list(warnings or [])
        self.row_lookup = {row.key(): row for row in rows}
        self.change_lookup = {change["key"]: change for change in changes if change.get("key")}

        self.last_refresh_var.set(refreshed_at)
        self.total_count_var.set(str(len(rows)))
        if self.current_warnings:
            self.change_count_var.set(f"{len(self.current_warnings)} warning(s)")
        elif changes:
            self.change_count_var.set(f"{len(changes)} changed")
        else:
            self.change_count_var.set("No changes")

        self._render_change_box(changes, self.current_warnings)
        self._render_tree(rows)
        self._refresh_all_articles_view()
        self._ensure_overview_split_expanded()
        if rows:
            first_key = rows[0].key()
            self.tree.selection_set(first_key)
            self.tree.focus(first_key)
            self._show_row_details(first_key)
        else:
            self.detail_title_var.set("No active submissions")
            self._set_detail_text("No active submissions found.")

    def _render_change_box(self, changes: list[dict[str, object]], warnings: list[str]) -> None:
        self.change_text.config(state="normal")
        self.change_text.delete("1.0", "end")
        if warnings:
            self.change_text.insert("end", "Warnings\n", "warning")
            for item in warnings:
                self.change_text.insert("end", f"- {item}\n", "warning")
            self.change_text.insert("end", "\n")
        if not changes:
            if not warnings:
                self.change_text.insert("end", "No changes detected in the latest refresh.", "muted")
        else:
            self.change_text.insert("end", "Changes\n", "changed")
            for change in changes:
                self.change_text.insert(
                    "end",
                    f"[{change.get('site', '-')}] {change.get('manuscript_number', '-')}\n",
                    "changed",
                )
                self.change_text.insert("end", f"{format_change_for_display(change)}\n\n")
        self.change_text.config(state="disabled")

    def _ensure_overview_split_expanded(self) -> None:
        if self.overview_split_initialized:
            return
        try:
            split_height = self.overview_split.winfo_height()
        except Exception:
            return
        if split_height <= 1:
            self.root.after(200, self._ensure_overview_split_expanded)
            return
        target = max(320, int(split_height * 0.6))
        try:
            self.overview_split.sashpos(0, target)
            self.overview_split_initialized = True
        except Exception:
            self.root.after(200, self._ensure_overview_split_expanded)

    def _render_runtime_warning(self, message: str) -> None:
        existing_warnings = list(self.current_warnings)
        existing_warnings.insert(0, message)
        self.current_warnings = existing_warnings[:10]
        self.change_count_var.set(f"{len(self.current_warnings)} warning(s)")
        self._render_change_box(self.current_changes, self.current_warnings)

    def _render_tree(self, rows) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in rows:
            reviewers = "-"
            reviewer_values = [
                row.reviewer_value_display("reviewer_invited"),
                row.reviewer_value_display("reviewer_accepted"),
                row.reviewer_value_display("review_reports_received"),
            ]
            if any(value != "-" for value in reviewer_values):
                reviewers = f"I:{reviewer_values[0]} A:{reviewer_values[1]} R:{reviewer_values[2]}"
            summary = f"{row.manuscript_number} | {row.title or '-'}"
            tags = ("changed",) if row.key() in self.change_lookup else ("normal",)
            self.tree.insert(
                "",
                "end",
                iid=row.key(),
                values=(summary, row.status, row.site, row.status_date or "", reviewers),
                tags=tags,
            )

    def _get_output_file_path(self, setting_key: str, default_name: str) -> Path:
        output_dir = self.project_root / self.extra_values.get("OUTPUT_DIR", "outputs")
        return output_dir / self.extra_values.get(setting_key, default_name)

    def _make_all_article_key(self, site_name: str, manuscript_number: str) -> str:
        return f"{site_family(site_name)}:{manuscript_number}"

    def _load_all_articles(self) -> tuple[list[dict[str, object]], dict[str, list[dict[str, str]]]]:
        entries_by_key: dict[str, dict[str, object]] = {}
        history_by_key: dict[str, list[dict[str, str]]] = {}
        log_path = self._get_output_file_path("REFRESH_LOG_NAME", "refresh_log.csv")

        if log_path.exists():
            with log_path.open(encoding="utf-8", newline="") as fh:
                for raw in csv.DictReader(fh):
                    event_type = (raw.get("event_type") or "").strip()
                    site = (raw.get("site") or "").strip()
                    manuscript_number = (raw.get("manuscript_number") or "").strip()
                    if event_type == "refresh_summary" or not site or not manuscript_number:
                        continue
                    record = {key: (value.strip() if isinstance(value, str) else value) for key, value in raw.items()}
                    key = self._make_all_article_key(site, manuscript_number)
                    history_by_key.setdefault(key, []).append(record)
                    entry = entries_by_key.setdefault(
                        key,
                        {
                            "key": key,
                            "site": site,
                            "manuscript_number": manuscript_number,
                            "title": record.get("title", ""),
                            "status": record.get("status", ""),
                            "status_date": record.get("status_date", ""),
                            "submission_date": record.get("submission_date", ""),
                            "source": record.get("source", ""),
                            "detail_url": record.get("detail_url", ""),
                            "first_seen_at": record.get("refreshed_at", ""),
                            "last_seen_at": record.get("refreshed_at", ""),
                            "last_event_type": record.get("event_type", ""),
                            "last_change_type": record.get("change_type", ""),
                            "last_change_detail": record.get("change_detail", ""),
                            "reviewer_invited": record.get("reviewer_invited", ""),
                            "reviewer_accepted": record.get("reviewer_accepted", ""),
                            "review_reports_received": record.get("review_reports_received", ""),
                        },
                    )
                    refreshed_at = record.get("refreshed_at", "")
                    if refreshed_at and (not entry["first_seen_at"] or refreshed_at < entry["first_seen_at"]):
                        entry["first_seen_at"] = refreshed_at
                    if refreshed_at and (not entry["last_seen_at"] or refreshed_at >= entry["last_seen_at"]):
                        entry["last_seen_at"] = refreshed_at
                        for field in (
                            "title",
                            "status",
                            "status_date",
                            "submission_date",
                            "source",
                            "detail_url",
                            "last_event_type",
                            "last_change_type",
                            "last_change_detail",
                            "reviewer_invited",
                            "reviewer_accepted",
                            "review_reports_received",
                        ):
                            if field.startswith("last_"):
                                source_field = field.replace("last_", "", 1)
                                entry[field] = record.get(source_field, "")
                            elif record.get(field, ""):
                                entry[field] = record.get(field, "")
                    else:
                        for field in ("title", "source", "detail_url"):
                            if not entry.get(field) and record.get(field, ""):
                                entry[field] = record.get(field, "")

        for row in self.current_rows:
            key = self._make_all_article_key(row.site, row.manuscript_number)
            entry = entries_by_key.setdefault(
                key,
                {
                    "key": key,
                    "site": row.site,
                    "manuscript_number": row.manuscript_number,
                    "title": row.title,
                    "status": row.status,
                    "status_date": row.status_date or "",
                    "submission_date": row.submission_date or "",
                    "source": row.source,
                    "detail_url": row.detail_url or "",
                    "first_seen_at": self.last_refresh_var.get(),
                    "last_seen_at": self.last_refresh_var.get(),
                    "last_event_type": "snapshot",
                    "last_change_type": "",
                    "last_change_detail": "",
                    "reviewer_invited": "",
                    "reviewer_accepted": "",
                    "review_reports_received": "",
                    "active_row_key": row.key(),
                },
            )
            entry["active_row_key"] = row.key()
            entry["site"] = row.site
            entry["manuscript_number"] = row.manuscript_number
            entry["title"] = row.title
            entry["status"] = row.status
            entry["status_date"] = row.status_date or ""
            entry["submission_date"] = row.submission_date or ""
            entry["source"] = row.source
            entry["detail_url"] = row.detail_url or entry.get("detail_url", "")
            entry["reviewer_invited"] = row.reviewer_value_display("reviewer_invited")
            entry["reviewer_accepted"] = row.reviewer_value_display("reviewer_accepted")
            entry["review_reports_received"] = row.reviewer_value_display("review_reports_received")

        active_row_by_article_key = {
            self._make_all_article_key(row.site, row.manuscript_number): row
            for row in self.current_rows
        }
        articles = list(entries_by_key.values())
        for article in articles:
            key = str(article["key"])
            active_row = active_row_by_article_key.get(key)
            article["state"] = "Active" if active_row is not None else "Archived"
            if active_row is not None:
                reviewer_values = [
                    active_row.reviewer_value_display("reviewer_invited"),
                    active_row.reviewer_value_display("reviewer_accepted"),
                    active_row.reviewer_value_display("review_reports_received"),
                ]
                article["reviewers"] = (
                    f"I:{reviewer_values[0]} A:{reviewer_values[1]} R:{reviewer_values[2]}"
                    if any(value != "-" for value in reviewer_values)
                    else "-"
                )
                article["review_comments_url"] = active_row.review_comments_url or ""
            else:
                reviewer_values = [
                    str(article.get("reviewer_invited", "") or "-"),
                    str(article.get("reviewer_accepted", "") or "-"),
                    str(article.get("review_reports_received", "") or "-"),
                ]
                article["reviewers"] = (
                    f"I:{reviewer_values[0]} A:{reviewer_values[1]} R:{reviewer_values[2]}"
                    if any(value != "-" for value in reviewer_values)
                    else "-"
                )
                article["review_comments_url"] = ""
            if not article.get("status"):
                article["status"] = article.get("last_change_detail") or "-"

        articles.sort(key=lambda item: (item.get("state") == "Active", item.get("last_seen_at", ""), item.get("manuscript_number", "")), reverse=True)
        return articles, history_by_key

    def _refresh_all_articles_view(self) -> None:
        selected = self.all_tree.selection()
        selected_key = selected[0] if selected else None
        articles, history_lookup = self._load_all_articles()
        self.all_article_lookup = {str(item["key"]): item for item in articles}
        self.all_article_history_lookup = history_lookup

        for item in self.all_tree.get_children():
            self.all_tree.delete(item)

        active_count = sum(1 for item in articles if item.get("state") == "Active")
        archived_count = len(articles) - active_count
        self.all_article_summary_var.set(f"{len(articles)} total | {active_count} active | {archived_count} archived")

        for article in articles:
            summary = f"{article.get('manuscript_number', '-')} | {article.get('title', '-') or '-'}"
            tag = "active" if article.get("state") == "Active" else "archived"
            self.all_tree.insert(
                "",
                "end",
                iid=str(article["key"]),
                values=(
                    summary,
                    article.get("status", "-") or "-",
                    article.get("state", "-"),
                    article.get("site", "-"),
                    article.get("last_seen_at", "-") or "-",
                ),
                tags=(tag,),
            )

        if articles:
            target_key = selected_key if selected_key in self.all_article_lookup else str(articles[0]["key"])
            self.all_tree.selection_set(target_key)
            self.all_tree.focus(target_key)
            self._show_all_article_details(target_key)
        else:
            self.all_detail_title_var.set("No article history available")
            self._set_all_detail_text("No article history available yet.")

    def on_all_row_selected(self, _event=None) -> None:
        selection = self.all_tree.selection()
        if selection:
            self._show_all_article_details(selection[0])

    def _show_all_article_details(self, article_key: str) -> None:
        article = self.all_article_lookup.get(article_key)
        if article is None:
            self.all_detail_title_var.set("No article details available")
            self._set_all_detail_text("No article details available.")
            return

        self.all_detail_title_var.set(f"{article.get('manuscript_number', '-')} | {article.get('status', '-')}")
        lines = [
            f"State: {article.get('state', '-')}",
            f"Site: {article.get('site', '-')}",
            f"Manuscript: {article.get('manuscript_number', '-')}",
            f"Title: {article.get('title', '-') or '-'}",
            f"Latest Status: {article.get('status', '-') or '-'}",
            f"Source: {article.get('source', '-') or '-'}",
        ]
        if article.get("submission_date"):
            lines.append(f"Submission Date: {article.get('submission_date')}")
        if article.get("status_date"):
            lines.append(f"Status Date: {article.get('status_date')}")
        if article.get("first_seen_at"):
            lines.append(f"First Seen: {article.get('first_seen_at')}")
        if article.get("last_seen_at"):
            lines.append(f"Last Seen: {article.get('last_seen_at')}")
        if article.get("detail_url"):
            lines.append(f"Detail URL: {article.get('detail_url')}")

        reviewers = article.get("reviewers", "-")
        if reviewers != "-":
            lines.append(f"Reviewer Stats: {reviewers}")
        if article.get("last_change_detail"):
            lines.append(f"Latest Change: {article.get('last_change_detail')}")

        active_row_key = article.get("active_row_key")
        active_row = self.row_lookup.get(active_row_key) if active_row_key else None
        if active_row is not None and active_row.review_comments_url:
            lines.append(f"Review URL: {active_row.review_comments_url}")
        if active_row is not None and active_row.notes:
            lines.append("")
            lines.append("Current Notes:")
            lines.extend(f"- {note}" for note in active_row.notes)
        if active_row is not None and active_row.review_comments:
            lines.append("")
            lines.append("Current Review Comments:")
            lines.extend(f"- {comment}" for comment in active_row.review_comments)

        history = self.all_article_history_lookup.get(article_key, [])
        if history:
            lines.append("")
            lines.append("Refresh History:")
            for item in reversed(history[-12:]):
                status = item.get("status") or "-"
                summary = item.get("change_detail") or item.get("event_type") or "snapshot"
                lines.append(f"- {item.get('refreshed_at', '-')} | {status} | {summary}")

        self._set_all_detail_text("\n".join(lines))

    def _set_all_detail_text(self, text: str) -> None:
        self.all_detail_text.config(state="normal")
        self.all_detail_text.delete("1.0", "end")
        self.all_detail_text.insert("end", text)
        self.all_detail_text.config(state="disabled")

    def open_all_detail_link(self) -> None:
        article = self._get_selected_all_article()
        if article and article.get("detail_url"):
            webbrowser.open(str(article["detail_url"]))

    def _get_selected_all_article(self):
        selection = self.all_tree.selection()
        if not selection:
            return None
        return self.all_article_lookup.get(selection[0])

    def on_row_selected(self, _event=None) -> None:
        selection = self.tree.selection()
        if selection:
            self._show_row_details(selection[0])

    def _show_row_details(self, row_key: str) -> None:
        row = self.row_lookup.get(row_key)
        if row is None:
            self.detail_title_var.set("No details available")
            self._set_detail_text("No details available.")
            return

        self.detail_title_var.set(f"{row.manuscript_number} | {row.status}")
        lines = [
            f"Site: {row.site}",
            f"Manuscript: {row.manuscript_number}",
            f"Title: {row.title or '-'}",
            f"Status: {row.status}",
            f"Source: {row.source}",
        ]
        if row.submission_date:
            lines.append(f"Submission Date: {row.submission_date}")
        if row.status_date:
            lines.append(f"Status Date: {row.status_date}")
        if row.detail_url:
            lines.append(f"Detail URL: {row.detail_url}")
        if row.review_comments_url:
            lines.append(f"Review URL: {row.review_comments_url}")

        change = self.change_lookup.get(row_key)
        if change is not None:
            lines.append("")
            lines.append(f"Change: {format_change_for_display(change)}")

        reviewer_values = [
            row.reviewer_value_display("reviewer_invited"),
            row.reviewer_value_display("reviewer_accepted"),
            row.reviewer_value_display("review_reports_received"),
        ]
        if any(value != "-" for value in reviewer_values):
            lines.append("")
            lines.append(
                "Reviewer Stats: "
                f"invited={reviewer_values[0]}, "
                f"accepted={reviewer_values[1]}, "
                f"reports={reviewer_values[2]}"
            )

        if row.notes:
            lines.append("")
            lines.append("Notes:")
            lines.extend(f"- {note}" for note in row.notes)

        if row.review_comments:
            lines.append("")
            lines.append("Review Comments:")
            lines.extend(f"- {comment}" for comment in row.review_comments)

        self._set_detail_text("\n".join(lines))

    def _set_detail_text(self, text: str) -> None:
        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("end", text)
        self.detail_text.config(state="disabled")

    def open_detail_link(self) -> None:
        row = self._get_selected_row()
        if row and row.detail_url:
            webbrowser.open(row.detail_url)

    def open_review_link(self) -> None:
        row = self._get_selected_row()
        if row and row.review_comments_url:
            webbrowser.open(row.review_comments_url)

    def _get_selected_row(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return self.row_lookup.get(selection[0])


def main() -> int:
    instance_lock = acquire_single_instance()
    if instance_lock is None:
        show_already_running_message()
        return 0

    try:
        project_root = Path(__file__).resolve().parent.parent
        load_env(project_root)
        root = tk.Tk()
        JournalCheckGUI(root, project_root)
        root.mainloop()
        return 0
    finally:
        instance_lock.close()
