"""Прозорецът. Отделен модул, защото внася tkinter.

``gui.py`` държи логиката и се внася навсякъде; този модул се внася само
когато наистина се отваря прозорец. Така инструментът върви и на машина
без tcl/tk.
"""

from __future__ import annotations

import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Callable

from PIL import Image, ImageTk

from .burn import MediaInfo, decode_rgb_frames, probe
from .gui import (JSON_TYPES, LANGUAGES, MODELS, PALETTE, PREVIEW_FPS,
                  PREVIEW_HEIGHT, PREVIEW_SECONDS, Row, VIDEO_TYPES, Worker,
                  default_output, parse_time, preview_window, reveal,
                  rows_from_transcript, transcript_from_rows, validate)
from .models import ANIMATIONS
from .pipeline import build_blocks, load_words, render, save_words
from .styles import PRESETS, get_style
from .transcribe import TranscribeOptions, transcribe

PAD = 8


class App(tk.Tk):
    def __init__(self, argv: list[str] | None = None) -> None:
        super().__init__()
        self.title("subs — анимирани субтитри")
        self.geometry("1360x840")
        self.minsize(1080, 660)

        self.worker = Worker()
        self.rows: list[Row] = []
        self.media: MediaInfo | None = None
        self.notes: list[str] = []
        self.preview_image: ImageTk.PhotoImage | None = None
        self.editor: tk.Entry | None = None

        self.video = tk.StringVar()
        self.style_name = tk.StringVar(value="stack")
        self.language = tk.StringVar(value="български")
        self.model = tk.StringVar(value="small")
        self.preview_at = tk.StringVar(value="2.50")
        self.status = tk.StringVar(value="Избери видео, за да започнеш.")
        self.follow = tk.BooleanVar(value=True)

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

        columns = ("text", "start", "end", "marks", "color", "anim")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings",
                                 selectmode="browse")
        for name, title in (("#0", "№"), ("text", "Дума"), ("start", "Начало"),
                            ("end", "Край"), ("marks", "★ ●"), ("color", "Цвят"),
                            ("anim", "Анимация")):
            self.tree.heading(name, text=title)
        self.tree.column("#0", width=44, stretch=False, anchor="e")
        self.tree.column("text", width=190)
        self.tree.column("start", width=68, anchor="e", stretch=False)
        self.tree.column("end", width=68, anchor="e", stretch=False)
        self.tree.column("marks", width=48, anchor="center", stretch=False)
        self.tree.column("color", width=84, anchor="center", stretch=False)
        self.tree.column("anim", width=112, anchor="center", stretch=False)

        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double_click)
        # Стрелките вече местят избора сами; ние само реагираме на смяната.
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Return>", self._edit_selected_text)
        self.tree.bind("<Delete>", lambda _e: self._set_color(None))

        tools = ttk.Frame(left)
        tools.pack(side="bottom", fill="x", pady=(4, 0))
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

        ttk.Label(tools, text="Анимация:").pack(side="left", padx=(PAD, 2))
        self.animation = tk.StringVar(value=ANIMATIONS[0])
        animations = ttk.Combobox(tools, textvariable=self.animation, width=13,
                                  state="readonly", values=list(ANIMATIONS))
        animations.pack(side="left")
        animations.bind("<<ComboboxSelected>>",
                        lambda _e: self._set_animation(self.animation.get()))

        ttk.Label(left, text="↑ ↓ между думите · Enter редактира · "
                             "★ ● с двоен клик · Delete маха цвета",
                  foreground="#666").pack(side="bottom", fill="x", padx=4)

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

        self.canvas = tk.Label(right, background="#1c1c1c",
                               text="Тук се показва кадър от рендера.",
                               foreground="#888")
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))

    def _place_sash(self, pane: ttk.PanedWindow) -> None:
        try:
            pane.sashpos(0, 660)
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
                       self.button_layer, self.button_preview, self.button_play):
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
                             values=row.values(), tags=self._tag_for(row))
        if self.rows:
            self.tree.selection_set("0")
            self.tree.focus("0")

    def _tag_for(self, row: Row) -> tuple[str, ...]:
        """Оцветява реда в цвета на думата — вижда се, без да се чете кодът."""
        if not row.color:
            return ()
        tag = f"colour{row.color.lstrip('#')}"
        self.tree.tag_configure(tag, foreground=row.color)
        return (tag,)

    def _refresh_row(self, index: int) -> None:
        row = self.rows[index]
        self.tree.item(str(index), values=row.values(), tags=self._tag_for(row))

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
        self.play_frames = [
            ImageTk.PhotoImage(Image.frombytes("RGB", (width, height), data))
            for data in raw
        ]
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
                                    language=self._selected_language())
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
        if what == "play":
            self._start_playback(result)
            self.status.set("Преглед — върти се в кръг, ■ спира.")
            return

        if what == "preview" and outputs:
            self._show_preview(Path(outputs[0]))
            self.status.set("Кадърът е готов.")
            return
        for path in outputs:
            self.log(f"готово: {path}")
        if outputs:
            self.status.set("Готово.")
            if messagebox.askyesno("Готово", "Да отворя ли папката?"):
                reveal(Path(outputs[0]))

    def _show_preview(self, path: Path) -> None:
        image = Image.open(path)
        area = (max(200, self.canvas.winfo_width() - 8),
                max(200, self.canvas.winfo_height() - 8))
        image.thumbnail(area, Image.Resampling.LANCZOS)
        self.preview_image = ImageTk.PhotoImage(image)
        self.canvas.configure(image=self.preview_image, text="")
        self.log(f"кадър: {path.name}")
