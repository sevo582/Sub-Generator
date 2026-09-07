"""Прозорецът. Отделен модул, защото внася tkinter.

``gui.py`` държи логиката и се внася навсякъде; този модул се внася само
когато наистина се отваря прозорец. Така инструментът върви и на машина
без tcl/tk.
"""

from __future__ import annotations

import math
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Callable

from PIL import Image, ImageDraw, ImageTk

from .burn import MediaInfo, decode_rgb_frames, probe
from .export import BACKGROUNDS, FORMATS
from .gui import (JSON_TYPES, LANGUAGES, MODELS, PALETTE, PREVIEW_FPS,
                  PREVIEW_HEIGHT, PREVIEW_SECONDS, SCALE_RANGE, Row,
                  VIDEO_TYPES, Worker, default_output, nudged, parse_time,
                  preview_window, reveal, rows_from_transcript, stepped_scale,
                  transcript_from_rows, validate)
from .models import ANIMATIONS, BlockLayout, Placed
from .pipeline import build_blocks, export_layers, load_words, render, save_words
from .styles import PRESETS, get_style
from .transcribe import TranscribeOptions, transcribe

PAD = 8


class App(tk.Tk):
    def __init__(self, argv: list[str] | None = None) -> None:
        super().__init__()
        self.title("subs — анимирани субтитри")
        self.geometry("1560x860")
        self.minsize(1080, 660)

        self.worker = Worker()
        self.rows: list[Row] = []
        self.media: MediaInfo | None = None
        self.notes: list[str] = []
        self.preview_image: ImageTk.PhotoImage | None = None
        self.editor: tk.Entry | None = None

        #: Последният показан кадър (без рамка на думата) и разположението
        #: на думите в него — за да можем да теглим рамка отгоре и да
        #: превръщаме координати в прозореца обратно в пиксели от видеото.
        self.preview_base: Image.Image | None = None
        self.preview_path: Path | None = None
        self.preview_layouts: list[BlockLayout] = []
        self.preview_scale: float = 1.0
        self.preview_offset: tuple[float, float] = (0.0, 0.0)
        self._drag: dict | None = None

        self.video = tk.StringVar()
        self.style_name = tk.StringVar(value="stack")
        self.language = tk.StringVar(value="български")
        self.model = tk.StringVar(value="small")
        self.preview_at = tk.StringVar(value="2.50")
        self.status = tk.StringVar(value="Избери видео, за да започнеш.")
        self.follow = tk.BooleanVar(value=True)
        #: Прегледът в рамка на телефон — само външност, кадърът е същият.
        self.phone = tk.BooleanVar(value=False)
        self.export_format = tk.StringVar(value=FORMATS[0])
        self.export_background = tk.StringVar(value=BACKGROUNDS[0])

        #: Кадри на пуснатото парче и докъде е стигнало възпроизвеждането.
        self.play_frames: list[ImageTk.PhotoImage] = []
        self.play_index = 0
        self.play_job: str | None = None
        self.follow_job: str | None = None

        self._build()
        self._pump()
        if argv:
            self._set_video(Path(argv[0]))

    # ------------------------------------------------------------------
    # Изграждане
    # ------------------------------------------------------------------

    def _build(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        self._build_source()
        self._build_middle()
        self._build_actions()
        self._build_log()
        ttk.Label(self, textvariable=self.status, relief="sunken",
                  anchor="w", padding=(PAD, 4)).pack(fill="x", side="bottom")

    def _build_source(self) -> None:
        frame = ttk.LabelFrame(self, text="Източник", padding=PAD)
        frame.pack(fill="x", padx=PAD, pady=(PAD, 0))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Видео:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.video).grid(row=0, column=1, sticky="ew",
                                                       padx=(4, 4))
        ttk.Button(frame, text="Избери…", command=self._choose_video).grid(row=0, column=2)

        options = ttk.Frame(frame)
        options.grid(row=1, column=0, columnspan=3, sticky="w", pady=(PAD, 0))

        ttk.Label(options, text="Стил:").pack(side="left")
        combo = ttk.Combobox(options, textvariable=self.style_name, width=12,
                             state="readonly", values=sorted(PRESETS))
        combo.pack(side="left", padx=(4, PAD * 2))
        combo.bind("<<ComboboxSelected>>", lambda _event: self._on_style_change())

        ttk.Label(options, text="Език:").pack(side="left")
        ttk.Combobox(options, textvariable=self.language, width=12, state="readonly",
                     values=[name for name, _ in LANGUAGES]
                     ).pack(side="left", padx=(4, PAD * 2))

        ttk.Label(options, text="Модел:").pack(side="left")
        ttk.Combobox(options, textvariable=self.model, width=10, state="readonly",
                     values=list(MODELS)).pack(side="left", padx=(4, PAD * 2))

        self.button_transcribe = ttk.Button(options, text="Транскрибирай",
                                            command=self._transcribe)
        self.button_transcribe.pack(side="left", padx=(0, 4))
        ttk.Button(options, text="Зареди JSON", command=self._load_json).pack(side="left",
                                                                             padx=4)
        ttk.Button(options, text="Запиши JSON", command=self._save_json).pack(side="left",
                                                                             padx=4)

    def _build_middle(self) -> None:
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        # Таблицата има шест колони и си иска ширина; без изрична преграда
        # тя изяжда прегледа и от него остава ивица.
        self.after(60, lambda: self._place_sash(pane))

        left = ttk.LabelFrame(pane, text="Думи — двоен клик за редакция", padding=4)
        pane.add(left, weight=3)

        # Подсказката и лентата с инструменти се създават преди таблицата:
        # ``pack`` раздава мястото по реда на извикване, а таблицата е с
        # ``expand=True`` и иначе не оставя нищо на подредените след нея.
        ttk.Label(left, text="↑ ↓ между думите · Enter редактира · + − размер · "
                             "★ ● с двоен клик · Shift+стрелки мести · Delete маха цвета",
                  foreground="#666").pack(side="bottom", fill="x", padx=4)
        # Два реда: на един ред всичко това не се побира в панела и
        # десният му край просто изчезва.
        second = ttk.Frame(left)
        second.pack(side="bottom", fill="x", pady=(2, 2))
        bulk = ttk.Frame(left)
        bulk.pack(side="bottom", fill="x", pady=(2, 0))
        tools = ttk.Frame(left)
        tools.pack(side="bottom", fill="x", pady=(4, 0))
        holder = ttk.Frame(left)
        holder.pack(side="top", fill="both", expand=True)

        columns = ("text", "start", "end", "marks", "color", "anim", "size", "offset")
        self.tree = ttk.Treeview(holder, columns=columns, show="tree headings",
                                 selectmode="browse")
        for name, title in (("#0", "№"), ("text", "Дума"), ("start", "Начало"),
                            ("end", "Край"), ("marks", "★ ●"), ("color", "Цвят"),
                            ("anim", "Анимация"), ("size", "Размер"),
                            ("offset", "Място")):
            self.tree.heading(name, text=title)
        self.tree.column("#0", width=44, stretch=False, anchor="e")
        self.tree.column("text", width=190)
        self.tree.column("start", width=68, anchor="e", stretch=False)
        self.tree.column("end", width=68, anchor="e", stretch=False)
        self.tree.column("marks", width=48, anchor="center", stretch=False)
        self.tree.column("color", width=92, anchor="center", stretch=False)
        self.tree.column("anim", width=150, anchor="w", stretch=False)
        self.tree.column("size", width=76, anchor="e", stretch=False)
        self.tree.column("offset", width=96, anchor="e", stretch=False)

        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double_click)
        # Стрелките вече местят избора сами; ние само реагираме на смяната.
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Return>", self._edit_selected_text)
        self.tree.bind("<Delete>", lambda _e: self._set_color(None))
        for key in ("<plus>", "<KP_Add>", "<equal>"):
            self.tree.bind(key, lambda _e: self._step_scale(1))
        for key in ("<minus>", "<KP_Subtract>"):
            self.tree.bind(key, lambda _e: self._step_scale(-1))
        # Стрелките сами местят избора; със Shift местят самата дума.
        for key, right, down in (("<Shift-Left>", -1, 0), ("<Shift-Right>", 1, 0),
                                 ("<Shift-Up>", 0, -1), ("<Shift-Down>", 0, 1)):
            self.tree.bind(key, lambda _e, r=right, d=down: self._nudge(r, d))

        ttk.Label(tools, text="Цвят:").pack(side="left", padx=(4, 2))
        for colour in PALETTE:
            swatch = tk.Button(tools, background=colour, width=2, relief="ridge",
                               borderwidth=1,
                               command=lambda c=colour: self._set_color(c))
            swatch.pack(side="left", padx=1)
        ttk.Button(tools, text="…", width=3,
                   command=self._pick_color).pack(side="left", padx=(4, 2))
        ttk.Button(tools, text="Изчисти", width=8,
                   command=lambda: self._set_color(None)).pack(side="left")

        ttk.Label(second, text="Размер:").pack(side="left", padx=(4, 2))
        ttk.Button(second, text="−", width=3,
                   command=lambda: self._step_scale(-1)).pack(side="left")
        ttk.Button(second, text="+", width=3,
                   command=lambda: self._step_scale(1)).pack(side="left", padx=1)
        ttk.Button(second, text="1×", width=4,
                   command=lambda: self._set_scale(1.0)).pack(side="left")

        ttk.Label(second, text="Място:").pack(side="left", padx=(PAD, 2))
        for label, right, down in (("◀", -1, 0), ("▲", 0, -1), ("▼", 0, 1), ("▶", 1, 0)):
            ttk.Button(second, text=label, width=3,
                       command=lambda r=right, d=down: self._nudge(r, d)
                       ).pack(side="left", padx=1)
        ttk.Button(second, text="⌂", width=3,
                   command=lambda: self._set_offset(0.0, 0.0)).pack(side="left", padx=1)

        ttk.Label(second, text="Анимация:").pack(side="left", padx=(PAD, 2))
        self.animation = tk.StringVar(value=ANIMATIONS[0])
        # Ширината е в средни знаци, а кирилицата е по-широка от латиницата
        # — с тясна кутия от „избледняване" се виждат две-три букви.
        animations = ttk.Combobox(second, textvariable=self.animation, width=18,
                                  state="readonly", values=list(ANIMATIONS))
        animations.pack(side="left")
        animations.bind("<<ComboboxSelected>>",
                        lambda _e: self._set_animation(self.animation.get()))

        # За разлика от лентата по-горе (само избраната дума), тази винаги
        # действа върху цялата транскрипция — независимо кой стил или модел
        # е избран, цветът на всяка дума вече бие цвета по подразбиране.
        ttk.Label(bulk, text="Цвят на всички думи:").pack(side="left", padx=(4, 2))
        for colour in PALETTE:
            swatch = tk.Button(bulk, background=colour, width=2, relief="ridge",
                               borderwidth=1,
                               command=lambda c=colour: self._set_color_all(c))
            swatch.pack(side="left", padx=1)
        ttk.Button(bulk, text="…", width=3,
                   command=self._pick_color_all).pack(side="left", padx=(4, 2))
        ttk.Button(bulk, text="Изчисти", width=8,
                   command=lambda: self._set_color_all(None)).pack(side="left")

        right = ttk.LabelFrame(pane, text="Преглед", padding=4)
        pane.add(right, weight=4)

        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Label(bar, text="Секунда:").pack(side="left")
        ttk.Entry(bar, textvariable=self.preview_at, width=7).pack(side="left", padx=4)
        self.button_preview = ttk.Button(bar, text="Кадър", command=self._preview)
        self.button_preview.pack(side="left", padx=2)
        self.button_play = ttk.Button(bar, text="▶ Пусни", command=self._play)
        self.button_play.pack(side="left", padx=2)
        self.button_stop = ttk.Button(bar, text="■", width=3, state="disabled",
                                      command=self._stop_playback)
        self.button_stop.pack(side="left", padx=2)
        ttk.Checkbutton(bar, text="следвай избора", variable=self.follow
                        ).pack(side="left", padx=(PAD, 0))
        ttk.Checkbutton(bar, text="телефон", variable=self.phone,
                        command=self._on_phone_toggle).pack(side="left", padx=(PAD, 0))

        ttk.Label(right, foreground="#666",
                 text="Избраната дума е с рамка и кръгчета в ъглите: тегли "
                      "кръгче — сменя размера; тегли отвътре — мести думата; "
                      "двоен клик — връща я по стил.").pack(fill="x", pady=(2, 0))

        self.canvas = tk.Label(right, background="#1c1c1c",
                               text="Тук се показва кадър от рендера.",
                               foreground="#888")
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))
        # Размерът и позицията на избраната дума се теглят направо върху
        # кадъра — рамката с кръгчетата в ъглите показва къде може да се
        # хване, без значение накъде теглиш.
        self.canvas.bind("<ButtonPress-1>", self._on_preview_press)
        self.canvas.bind("<B1-Motion>", self._on_preview_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_preview_release)
        self.canvas.bind("<Double-Button-1>", self._on_preview_double_click)

    def _place_sash(self, pane: ttk.PanedWindow) -> None:
        try:
            pane.sashpos(0, 860)
        except tk.TclError:
            pass

    def _build_actions(self) -> None:
        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=PAD)
        self.button_render = ttk.Button(frame, text="Рендирай видео",
                                        command=lambda: self._render(layer_only=False))
        self.button_render.pack(side="left")
        self.button_layer = ttk.Button(
            frame, text="Само слой с прозрачност",
            command=lambda: self._render(layer_only=True))
        self.button_layer.pack(side="left", padx=PAD)
        self.button_export = ttk.Button(frame, text="Думите поотделно",
                                        command=self._export_layers)
        self.button_export.pack(side="left")
        ttk.Combobox(frame, textvariable=self.export_format, width=6, state="readonly",
                     values=list(FORMATS)).pack(side="left", padx=(4, 2))
        ttk.Combobox(frame, textvariable=self.export_background, width=12,
                     state="readonly", values=list(BACKGROUNDS)
                     ).pack(side="left", padx=(0, PAD))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=PAD)

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self, text="Дневник", padding=4)
        frame.pack(fill="x", padx=PAD, pady=(PAD, 0))
        self.log_text = tk.Text(frame, height=7, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # Помощни
    # ------------------------------------------------------------------

    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _busy(self, busy: bool, what: str = "") -> None:
        state = "disabled" if busy else "normal"
        for button in (self.button_transcribe, self.button_render,
                       self.button_layer, self.button_preview, self.button_play,
                       self.button_export):
            button.configure(state=state)
        if busy:
            self.progress.start(12)
            self.status.set(what)
        else:
            self.progress.stop()

    def _selected_language(self) -> str | None:
        return dict(LANGUAGES)[self.language.get()]

    def _video_path(self) -> Path | None:
        text = self.video.get().strip()
        if not text:
            messagebox.showinfo("Липсва видео", "Първо избери видео файл.")
            return None
        path = Path(text)
        if not path.exists():
            messagebox.showerror("Няма такъв файл", str(path))
            return None
        return path

    def _on_style_change(self) -> None:
        behind = get_style(self.style_name.get()).renderer == "raster_behind"
        self.button_layer.configure(state="normal" if behind else "disabled")

    # ------------------------------------------------------------------
    # Файлове
    # ------------------------------------------------------------------

    def _choose_video(self) -> None:
        name = filedialog.askopenfilename(title="Избери видео", filetypes=VIDEO_TYPES)
        if name:
            self._set_video(Path(name))

    def _set_video(self, path: Path) -> None:
        self.video.set(str(path))
        try:
            self.media = probe(path)
        except Exception as error:  # noqa: BLE001
            self.media = None
            self.log(f"не мога да прочета видеото: {error}")
            return
        turned = f", завъртяно на {self.media.rotation}°" if self.media.rotation else ""
        self.log(f"{path.name}: {self.media.width}x{self.media.height} @ "
                 f"{self.media.fps:.2f} к/с, {self.media.duration:.2f} s{turned}")
        if self.media.width > self.media.height:
            self.log("внимание: видеото е хоризонтално, а стиловете са мерени "
                     "за вертикално")
        self.preview_at.set(format(min(2.5, self.media.duration / 2), ".2f"))
        self.status.set("Готово за транскрипция.")
        sidecar = path.with_suffix(".words.json")
        if sidecar.exists():
            self.log(f"намерен е {sidecar.name} — зареждам го")
            self._read_json(sidecar)

    def _load_json(self) -> None:
        name = filedialog.askopenfilename(title="Зареди JSON с думи",
                                          filetypes=JSON_TYPES)
        if name:
            self._read_json(Path(name))

    def _read_json(self, path: Path | str) -> None:
        path = Path(path)
        try:
            transcript = load_words(path)
        except Exception as error:  # noqa: BLE001
            messagebox.showerror("Не мога да прочета файла", str(error))
            return
        self.notes = list(transcript.notes)
        for note in self.notes:
            self.log(note)
        self._set_rows(rows_from_transcript(transcript))
        self.log(f"{len(self.rows)} думи от {path.name}")

    def _save_json(self) -> None:
        if not self.rows:
            messagebox.showinfo("Няма думи", "Първо транскрибирай или зареди JSON.")
            return
        video = self.video.get().strip()
        initial = Path(video).with_suffix(".words.json").name if video else "words.json"
        name = filedialog.asksaveasfilename(title="Запиши JSON", defaultextension=".json",
                                            initialfile=initial, filetypes=JSON_TYPES)
        if not name:
            return
        save_words(transcript_from_rows(self.rows, self._selected_language(), self.notes),
                   Path(name))
        self.log(f"записано в {name}")

    # ------------------------------------------------------------------
    # Таблица
    # ------------------------------------------------------------------

    def _set_rows(self, rows: list[Row]) -> None:
        self.rows = rows
        self._refresh_tree()
        for problem in validate(self.rows):
            self.log(f"внимание: {problem}")
        self._log_blocks()

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(index), text=str(index + 1),
                             values=row.values(self._height()),
                             tags=self._tag_for(row))
        if self.rows:
            self.tree.selection_set("0")
            self.tree.focus("0")

    def _height(self) -> int:
        """Височината на кадъра — отместванията се показват в нейни пиксели."""
        return self.media.height if self.media else 1920

    def _tag_for(self, row: Row) -> tuple[str, ...]:
        """Оцветява реда в цвета на думата — вижда се, без да се чете кодът."""
        if not row.color:
            return ()
        tag = f"colour{row.color.lstrip('#')}"
        self.tree.tag_configure(tag, foreground=row.color)
        return (tag,)

    def _refresh_row(self, index: int) -> None:
        row = self.rows[index]
        self.tree.item(str(index), values=row.values(self._height()),
                       tags=self._tag_for(row))

    def _log_blocks(self) -> None:
        """Показва как ще се разбият блоковете и коя дума е подчертана."""
        if not self.rows:
            return
        style = get_style(self.style_name.get())
        transcript = transcript_from_rows(self.rows, self._selected_language())
        blocks = build_blocks(transcript, style)
        self.log(f"{len(blocks)} блока при стил {style.name!r}:")
        for block in blocks:
            marked = " ".join(f"[{w.text}]" if i == block.highlight else w.text
                              for i, w in enumerate(block.words))
            self.log(f"   {block.start:6.2f}–{block.end:6.2f}  {marked}")

    def _on_double_click(self, event: tk.Event) -> None:
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item:
            return
        index = int(item)

        if column == "#4":  # маркерите се превключват, не се пишат
            row = self.rows[index]
            if not row.emphasis and not row.accent:
                row.emphasis = True
            elif row.emphasis and not row.accent:
                row.accent = True
            else:
                row.emphasis = row.accent = False
            self._refresh_row(index)
            self._log_blocks()
            return

        if column == "#7":  # размерът се сменя с бутоните, не се пише
            self._step_scale(1)
            return
        field = {"#1": "text", "#2": "start", "#3": "end"}.get(column)
        if field is None:
            return
        self._edit_cell(item, column, index, field)

    def _edit_cell(self, item: str, column: str, index: int, field: str) -> None:
        x, y, width, height = self.tree.bbox(item, column)
        current = getattr(self.rows[index], field)
        entry = tk.Entry(self.tree)
        entry.insert(0, current if field == "text" else format(current, ".2f"))
        entry.select_range(0, "end")
        entry.place(x=x, y=y, width=width, height=height)
        entry.focus_set()
        self.editor = entry

        def commit(_event: tk.Event | None = None) -> None:
            raw = entry.get()
            entry.destroy()
            self.editor = None
            try:
                value = raw.strip() if field == "text" else parse_time(raw)
            except ValueError:
                self.log(f"невалидна стойност: {raw!r}")
                return
            setattr(self.rows[index], field, value)
            self._refresh_row(index)
            for problem in validate(self.rows):
                self.log(f"внимание: {problem}")
            self._log_blocks()

        def cancel(_event: tk.Event) -> None:
            entry.destroy()
            self.editor = None

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", cancel)

    # ------------------------------------------------------------------
    # Избор, цвят, анимация
    # ------------------------------------------------------------------

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _on_select(self, _event: tk.Event | None = None) -> None:
        """Смяна на избора: показва настройките на думата и по желание
        премества прегледа при нея."""
        index = self._selected_index()
        if index is None:
            return
        row = self.rows[index]
        self.animation.set(row.animation)
        self.preview_at.set(format(row.middle, ".2f"))
        self.status.set(f"Дума {index + 1} от {len(self.rows)}: {row.text!r}")
        # Рамката за дърпане на размера следва избора веднага — не чака
        # следващия рендер, дори когато „следвай избора" е изключено.
        self._render_preview_overlay()

        if self.follow_job is not None:
            self.after_cancel(self.follow_job)
            self.follow_job = None
        if self.follow.get() and not self.worker.busy:
            # Изчакваме малко: при задържана стрелка иначе се пуска по един
            # рендер на всяка дума, през която минаваме.
            self.follow_job = self.after(400, self._preview)

    def _edit_selected_text(self, _event: tk.Event | None = None) -> str:
        index = self._selected_index()
        if index is not None:
            self._edit_cell(str(index), "#1", index, "text")
        return "break"

    def _apply_to_selection(self, change) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("Няма избрана дума", "Първо избери ред в таблицата.")
            return
        change(self.rows[index])
        self._refresh_row(index)
        self._log_blocks()

    def _set_color(self, colour: str | None) -> None:
        def change(row: Row) -> None:
            row.color = colour
        self._apply_to_selection(change)

    def _pick_color(self) -> None:
        index = self._selected_index()
        current = self.rows[index].color if index is not None else None
        chosen = colorchooser.askcolor(color=current or "#FFFFFF",
                                       title="Цвят на думата")[1]
        if chosen:
            self._set_color(chosen.upper())

    def _apply_to_all(self, change) -> None:
        if not self.rows:
            messagebox.showinfo("Няма думи", "Първо транскрибирай или зареди JSON.")
            return
        for row in self.rows:
            change(row)
        for index in range(len(self.rows)):
            self._refresh_row(index)
        self._log_blocks()

    def _set_color_all(self, colour: str | None) -> None:
        def change(row: Row) -> None:
            row.color = colour
        self._apply_to_all(change)

    def _pick_color_all(self) -> None:
        chosen = colorchooser.askcolor(color="#FFFFFF",
                                       title="Цвят на всички думи")[1]
        if chosen:
            self._set_color_all(chosen.upper())

    def _set_scale(self, value: float) -> None:
        def change(row: Row) -> None:
            row.scale = value
        self._apply_to_selection(change)

    def _step_scale(self, direction: int) -> str:
        index = self._selected_index()
        if index is not None:
            self._set_scale(stepped_scale(self.rows[index].scale, direction))
        return "break"

    def _set_offset(self, dx: float, dy: float) -> None:
        def change(row: Row) -> None:
            row.dx, row.dy = dx, dy
        self._apply_to_selection(change)

    def _nudge(self, right: int, down: int) -> str:
        index = self._selected_index()
        if index is not None:
            row = self.rows[index]
            self._set_offset(*nudged(row.dx, row.dy, right, down))
        return "break"

    def _set_animation(self, name: str) -> None:
        def change(row: Row) -> None:
            row.animation = name
        self._apply_to_selection(change)

    # ------------------------------------------------------------------
    # Възпроизвеждане на парче
    # ------------------------------------------------------------------

    def _play(self) -> None:
        """Рендира кратко парче от текущото място и го пуска в прозореца.

        Минава през същия рендерер както готовото видео, само смалено и с
        по-малко кадри — иначе прегледът щеше да показва нещо, което не е
        това, което ще излезе.
        """
        path = self._video_path()
        if path is None or self.worker.busy:
            return
        if not self.rows:
            messagebox.showinfo("Няма думи", "Първо транскрибирай или зареди JSON.")
            return

        self._stop_playback()
        index = self._selected_index()
        if index is None:
            try:
                start = parse_time(self.preview_at.get())
            except ValueError:
                start = 0.0
            index = min(range(len(self.rows)),
                        key=lambda i: abs(self.rows[i].middle - start))
        limit = self.media.duration if self.media else 1e9
        window = preview_window(self.rows, index, PREVIEW_SECONDS, limit)

        style = get_style(self.style_name.get())
        transcript = transcript_from_rows(self.rows, self._selected_language())
        temp = Path(tempfile.mkdtemp(prefix="subs-play-")) / "preview.mp4"
        self.log(f"правя преглед {window[0]:.2f}–{window[0] + window[1]:.2f} s …")
        self._busy(True, "Правя преглед…")

        def work(log: Callable[[str], None]) -> object:
            render(path, transcript, style, output=temp, media=self.media,
                   segment=window, scale_height=PREVIEW_HEIGHT, fps=PREVIEW_FPS,
                   progress=lambda _m: None)
            return decode_rgb_frames(temp)

        self.worker.start(work)
        self.pending = ("play", temp)

    def _start_playback(self, decoded: tuple[int, int, float, list[bytes]]) -> None:
        width, height, fps, raw = decoded
        if not raw:
            self.log("прегледът излезе празен")
            return
        area = self._preview_area()
        frames: list[ImageTk.PhotoImage] = []
        for data in raw:
            picture = Image.frombytes("RGB", (width, height), data)
            if self.phone.get():
                picture, _inset = self._phone_frame(picture, area)
            frames.append(ImageTk.PhotoImage(picture))
        self.play_frames = frames
        self.play_index = 0
        self.button_stop.configure(state="normal")
        self.log(f"{len(self.play_frames)} кадъра @ {fps:.0f} к/с")
        self._advance(max(20, int(1000 / max(1.0, fps))))

    def _advance(self, delay: int) -> None:
        if not self.play_frames:
            return
        self.canvas.configure(image=self.play_frames[self.play_index], text="")
        self.play_index += 1
        if self.play_index >= len(self.play_frames):
            self.play_index = 0  # въртим в кръг, за да се огледа спокойно
        self.play_job = self.after(delay, lambda: self._advance(delay))

    def _stop_playback(self) -> None:
        if self.play_job is not None:
            self.after_cancel(self.play_job)
            self.play_job = None
        self.play_frames = []
        self.button_stop.configure(state="disabled")

    # ------------------------------------------------------------------
    # Задачи
    # ------------------------------------------------------------------

    def _transcribe(self) -> None:
        path = self._video_path()
        if path is None or self.worker.busy:
            return
        options = TranscribeOptions(model=self.model.get(),
                                    language=self._selected_language(),
                                    batch_size=8)
        self.log(f"транскрибирам с {options.model!r} … първото пускане тегли модела")
        self._busy(True, "Транскрибиране…")

        def work(log: Callable[[str], None]) -> object:
            log("това може да отнеме няколко минути")
            return transcribe(path, options)

        self.worker.start(work)
        self.pending = ("transcribe", None)

    def _preview(self) -> None:
        path = self._video_path()
        if path is None or self.worker.busy:
            return
        if not self.rows:
            messagebox.showinfo("Няма думи", "Първо транскрибирай или зареди JSON.")
            return
        try:
            at = parse_time(self.preview_at.get())
        except ValueError:
            messagebox.showerror("Невалидна секунда", self.preview_at.get())
            return
        if self.media and not 0 <= at <= self.media.duration:
            messagebox.showerror("Извън видеото",
                                 f"Видеото е {self.media.duration:.2f} s.")
            return

        style = get_style(self.style_name.get())
        transcript = transcript_from_rows(self.rows, self._selected_language())
        self._busy(True, "Рисувам кадър…")

        def work(log: Callable[[str], None]) -> object:
            return render(path, transcript, style, output=path, media=self.media,
                          preview_times=[at], progress=log)

        self.worker.start(work)
        self.pending = ("preview", None)

    def _export_layers(self) -> None:
        """Изнася всяка дума като отделен прозрачен PNG за редактор."""
        path = self._video_path()
        if path is None or self.worker.busy:
            return
        if not self.rows:
            messagebox.showinfo("Няма думи", "Първо транскрибирай или зареди JSON.")
            return

        style = get_style(self.style_name.get())
        transcript = transcript_from_rows(self.rows, self._selected_language())
        fmt = self.export_format.get()
        background = self.export_background.get()
        destination = path.with_name(path.stem + ".layers")
        self.log(f"изнасям {len(self.rows)} думи като {fmt.upper()} "
                 f"с {background} фон в {destination.name} …")
        self._busy(True, "Изнасям думите…")

        def work(log: Callable[[str], None]) -> object:
            export_layers(path, transcript, style, destination, media=self.media,
                          fmt=fmt, background=background, progress=log)
            return destination

        self.worker.start(work)
        self.pending = ("export", destination)

    def _render(self, layer_only: bool) -> None:
        path = self._video_path()
        if path is None or self.worker.busy:
            return
        if not self.rows:
            messagebox.showinfo("Няма думи", "Първо транскрибирай или зареди JSON.")
            return

        style = get_style(self.style_name.get())
        transcript = transcript_from_rows(self.rows, self._selected_language())
        output = None if layer_only else default_output(path, style.name)
        layer = path.with_name(path.stem + ".layer.mov") if (
            layer_only or style.renderer == "raster_behind") else None

        self.log("рендирам… при стил behind това отнема минути")
        self._busy(True, "Рендиране…")

        def work(log: Callable[[str], None]) -> object:
            return render(path, transcript, style, output=output, layer=layer,
                          media=self.media, progress=log)

        self.worker.start(work)
        self.pending = ("render", None)

    # ------------------------------------------------------------------
    # Опашка
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        while True:
            try:
                kind, payload = self.worker.queue.get_nowait()
            except Exception:  # noqa: BLE001 — Empty
                break
            if kind == "log":
                self.log(str(payload))
            elif kind == "error":
                message, trace = payload
                self._busy(False)
                self.status.set("Грешка.")
                self.log(f"грешка: {message}")
                self.log(trace.strip().splitlines()[-1])
                messagebox.showerror("Грешка", message)
            elif kind == "done":
                self._busy(False)
                self._finish(payload)
        self.after(120, self._pump)

    def _finish(self, result: object) -> None:
        what = getattr(self, "pending", ("", None))[0]
        if what == "transcribe":
            transcript = result
            self.notes = list(transcript.notes)
            for note in self.notes:
                self.log(note)
            self._set_rows(rows_from_transcript(transcript))
            video = Path(self.video.get())
            save_words(transcript, video.with_suffix(".words.json"))
            self.log(f"думите са записани в {video.stem}.words.json")
            self.status.set(f"{len(self.rows)} думи. Поправи каквото трябва.")
            return

        outputs = getattr(result, "outputs", [])
        for note in getattr(result, "notes", []):
            self.log(note)
        if what == "export":
            destination = Path(result)
            self.status.set("Думите са изнесени.")
            self.log(f"готово: {destination}")
            self.log("в редактора: внеси всички PNG-та — всяко ляга на мястото си; "
                     "времената са в layers.csv")
            if messagebox.askyesno("Готово", "Да отворя ли папката?"):
                reveal(destination / "layers.csv")
            return

        if what == "play":
            self._start_playback(result)
            self.status.set("Преглед — върти се в кръг, ■ спира.")
            return

        if what == "preview" and outputs:
            self._show_preview(Path(outputs[0]), getattr(result, "layouts", []))
            self.status.set("Кадърът е готов.")
            return
        for path in outputs:
            self.log(f"готово: {path}")
        if outputs:
            self.status.set("Готово.")
            if messagebox.askyesno("Готово", "Да отворя ли папката?"):
                reveal(Path(outputs[0]))

    #: Рамка на телефон: дебелина и заобляне в пиксели на екрана.
    PHONE_BEZEL = 14
    PHONE_RADIUS = 26

    def _preview_area(self) -> tuple[int, int]:
        return (max(200, self.canvas.winfo_width() - 8),
                max(200, self.canvas.winfo_height() - 8))

    def _phone_frame(self, image: Image.Image,
                     area: tuple[int, int]) -> tuple[Image.Image, tuple[int, int]]:
        """Кадърът, сложен в проста рамка на телефон.

        Връща и отместването на самото видео в готовата картинка — без него
        рамката около думата би се разминала с думата, щом теглачката смята
        в координати на видеото.
        """
        bezel, radius = self.PHONE_BEZEL, self.PHONE_RADIUS
        shot = image.copy()
        shot.thumbnail((max(60, area[0] - 2 * bezel), max(60, area[1] - 2 * bezel)),
                       Image.Resampling.LANCZOS)

        body = Image.new("RGB", (shot.width + 2 * bezel, shot.height + 2 * bezel),
                        "#1c1c1c")
        draw = ImageDraw.Draw(body)
        draw.rounded_rectangle([0, 0, body.width - 1, body.height - 1], radius=radius,
                               fill="#0b0b0b", outline="#3c3c3c", width=2)

        # Заоблени ъгли и на екрана — иначе кадърът щръква от рамката.
        mask = Image.new("L", shot.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, shot.width - 1, shot.height - 1],
            radius=max(4, radius - bezel), fill=255)
        body.paste(shot, (bezel, bezel), mask)

        # Изрезът горе и чертата долу — колкото да се познае, че е телефон.
        centre = body.width // 2
        notch_w, notch_h = max(40, shot.width // 4), max(8, bezel)
        draw.rounded_rectangle([centre - notch_w // 2, bezel - 1,
                                centre + notch_w // 2, bezel + notch_h],
                               radius=notch_h // 2, fill="#0b0b0b")
        bar_w = max(60, shot.width // 3)
        bar_y = body.height - bezel - max(5, bezel // 2)
        draw.rounded_rectangle([centre - bar_w // 2, bar_y, centre + bar_w // 2,
                                bar_y + 3], radius=2, fill="#DDDDDD")
        return body, (bezel, bezel)

    def _show_preview(self, path: Path, layouts: list[BlockLayout] | None = None) -> None:
        if layouts is not None:
            self.preview_layouts = layouts
        self.preview_path = path
        image = Image.open(path).convert("RGB")
        area = self._preview_area()
        if self.phone.get():
            frame, inset = self._phone_frame(image, area)
            shot_width = frame.width - 2 * self.PHONE_BEZEL
        else:
            image.thumbnail(area, Image.Resampling.LANCZOS)
            frame, inset, shot_width = image, (0, 0), image.width

        self.preview_base = frame
        self.preview_scale = (shot_width / self.media.width) if self.media else 1.0
        self.preview_offset = (
            (self.canvas.winfo_width() - frame.width) / 2.0 + inset[0],
            (self.canvas.winfo_height() - frame.height) / 2.0 + inset[1],
        )
        self._render_preview_overlay()
        self.log(f"кадър: {path.name}")

    def _on_phone_toggle(self) -> None:
        """Само пречертава последния кадър — нов рендер не е нужен."""
        if self.preview_path is not None and self.preview_path.exists():
            self._show_preview(self.preview_path)

    # ------------------------------------------------------------------
    # Размер и място направо върху кадъра
    # ------------------------------------------------------------------

    def _selected_placed(self) -> Placed | None:
        """Разположението на избраната дума в последния показан кадър.

        Съвпада по тайминг с реда в таблицата — той е единственото общо
        между ``Row`` и ``Placed``, а и се пази непроменен през рендера.
        Връща None, ако думата не е била на екрана в този кадър — тогава
        просто няма какво да се хване.
        """
        index = self._selected_index()
        if index is None:
            return None
        row = self.rows[index]
        for layout in self.preview_layouts:
            for placed in layout.placed:
                if (abs(placed.start - row.start) < 1e-6
                        and abs(placed.end - row.end) < 1e-6):
                    return placed
        return None

    def _placed_rect(self, placed: Placed) -> tuple[float, float, float, float]:
        ox, oy = self.preview_offset
        scale = self.preview_scale
        x0 = ox + placed.x * scale
        y0 = oy + placed.y * scale
        x1 = ox + (placed.x + placed.width) * scale
        y1 = oy + (placed.y + placed.height) * scale
        return x0, y0, x1, y1

    #: Радиус на кръгчето в ъгъла — и колкото се вижда, и колкото се хваща.
    HANDLE_RADIUS = 5
    HANDLE_GRAB = 10

    @staticmethod
    def _corners(rect: tuple[float, float, float, float]
                 ) -> list[tuple[float, float]]:
        x0, y0, x1, y1 = rect
        return [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]

    def _draw_overlay_rect(self, rect: tuple[float, float, float, float]) -> None:
        if self.preview_base is None:
            return
        x0, y0, x1, y1 = rect
        frame = self.preview_base.copy()
        draw = ImageDraw.Draw(frame)
        draw.rectangle([round(x0), round(y0), round(x1), round(y1)],
                       outline="#0A84FF", width=2)
        r = self.HANDLE_RADIUS
        for cx, cy in self._corners(rect):
            draw.ellipse([round(cx - r), round(cy - r), round(cx + r), round(cy + r)],
                        fill="#0A84FF", outline="#FFFFFF", width=1)
        self.preview_image = ImageTk.PhotoImage(frame)
        self.canvas.configure(image=self.preview_image, text="")

    def _render_preview_overlay(self) -> None:
        if self.preview_base is None:
            return
        placed = self._selected_placed()
        if placed is None:
            self.preview_image = ImageTk.PhotoImage(self.preview_base)
            self.canvas.configure(image=self.preview_image, text="")
            return
        self._draw_overlay_rect(self._placed_rect(placed))

    def _on_preview_press(self, event: tk.Event) -> None:
        placed = self._selected_placed()
        index = self._selected_index()
        if placed is None or index is None:
            return
        rect = self._placed_rect(placed)
        x0, y0, x1, y1 = rect
        row = self.rows[index]

        # Ъглите се проверяват първо — там се хваща за смяна на размера.
        for cx, cy in self._corners(rect):
            if math.hypot(event.x - cx, event.y - cy) <= self.HANDLE_GRAB:
                centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
                start_dist = max(6.0, math.hypot(cx - centre[0], cy - centre[1]))
                self._drag = {
                    "mode": "resize",
                    "index": index,
                    "rect": rect,
                    "centre": centre,
                    "start_dist": start_dist,
                    "start_scale": row.scale,
                }
                return

        margin = 6
        if not (x0 - margin <= event.x <= x1 + margin
                and y0 - margin <= event.y <= y1 + margin):
            return  # кликът е извън думата — не пипаме нищо

        # Навсякъде другаде вътре в думата — плъзгане накъдето пожелаеш.
        self._drag = {
            "mode": "move",
            "index": index,
            "rect": rect,
            "start_x": event.x,
            "start_y": event.y,
            "start_dx": row.dx,
            "start_dy": row.dy,
        }

    def _on_preview_drag(self, event: tk.Event) -> None:
        drag = self._drag
        if drag is None:
            return
        if drag["mode"] == "resize":
            self._drag_resize(drag, event)
        else:
            self._drag_move(drag, event)

    def _drag_resize(self, drag: dict, event: tk.Event) -> None:
        cx, cy = drag["centre"]
        dist = max(6.0, math.hypot(event.x - cx, event.y - cy))
        ratio = dist / drag["start_dist"]
        low, high = SCALE_RANGE
        scale = min(high, max(low, round(drag["start_scale"] * ratio, 2)))
        index = drag["index"]
        if scale != self.rows[index].scale:
            self.rows[index].scale = scale
            self._refresh_row(index)

        # Рамката расте/се смалява веднага около центъра си — истинският
        # рендер идва едва след пускането на бутона (виж release-а).
        grow = scale / drag["start_scale"]
        x0, y0, x1, y1 = drag["rect"]
        half_w = (x1 - x0) / 2.0 * grow
        half_h = (y1 - y0) / 2.0 * grow
        self._draw_overlay_rect((cx - half_w, cy - half_h, cx + half_w, cy + half_h))
        self.status.set(f"Размер: {scale:.2f}×")

    def _drag_move(self, drag: dict, event: tk.Event) -> None:
        moved_x = event.x - drag["start_x"]
        moved_y = event.y - drag["start_y"]
        index = drag["index"]

        # Отместването се пази като дроб от височината за двете оси — виж
        # ``Word.dx``/``Word.dy`` — за да е стъпката еднаква нагоре-надолу
        # и настрани, независимо от резолюцията.
        if self.media and self.preview_scale > 0:
            new_dx = drag["start_dx"] + moved_x / self.preview_scale / self.media.height
            new_dy = drag["start_dy"] + moved_y / self.preview_scale / self.media.height
            row = self.rows[index]
            if (new_dx, new_dy) != (row.dx, row.dy):
                row.dx, row.dy = new_dx, new_dy
            self.status.set(f"Място: {new_dx * self.media.height:+.0f}, "
                            f"{new_dy * self.media.height:+.0f} px")

        x0, y0, x1, y1 = drag["rect"]
        self._draw_overlay_rect((x0 + moved_x, y0 + moved_y, x1 + moved_x, y1 + moved_y))

    def _on_preview_release(self, _event: tk.Event) -> None:
        drag = self._drag
        self._drag = None
        if drag is None:
            return
        self._log_blocks()
        # Реалният рендер идва след пускането на бутона — по време на самото
        # плъзгане е достатъчна синята рамка, честото рендиране би бавило.
        if not self.worker.busy:
            self._preview()

    def _on_preview_double_click(self, event: tk.Event) -> None:
        """Двоен клик върху тялото на думата връща позицията ѝ по стил."""
        placed = self._selected_placed()
        index = self._selected_index()
        if placed is None or index is None:
            return
        x0, y0, x1, y1 = self._placed_rect(placed)
        if x0 <= event.x <= x1 and y0 <= event.y <= y1:
            self._set_offset(0.0, 0.0)
            if not self.worker.busy:
                self._preview()
