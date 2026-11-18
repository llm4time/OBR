#!/usr/bin/env python3
import io
import json
import re
import tkinter as tk
import tkinter.font as tkfont
from functools import lru_cache
from pathlib import Path
from tkinter import ttk

import pandas as pd
from PIL import Image, ImageDraw, ImageTk

try:
    from matplotlib import mathtext, rcParams
    from matplotlib.font_manager import FontProperties
    rcParams["mathtext.fontset"] = "stix"
except ImportError:
    mathtext = None

PROJECT_DIR = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_DIR / "data" / "processed"
CSV_FILE = PROJECT_DIR / "metadata" / "repository.csv"
REVIEW_FILE = PROJECT_DIR / "annotations" / "images_review.json"
PNGS_DIR = PROCESSED_DIR / "screenshots"
IMGS_DIR = PROCESSED_DIR / "images"

THEMES = {
    "dark": dict(
        BG="#0a0f1a", PANEL="#0e1524", CARD="#121b2e", BORDER="#1f2a42",
        BTN="#121b2e", BODY="#ffffff", FG="#e6ebf5", MUTED="#93a0b8", ACCENT="#1e6bff", ERR_FG="#ff7b8a",
    ),
    "light": dict(
        BG="#e3e7ef", PANEL="#eceff5", CARD="#ffffff", BORDER="#d0d6e2",
        BTN="#ffffff", BODY="#000000", FG="#141b2d", MUTED="#5d6b85", ACCENT="#1e6bff", ERR_FG="#c2263a",
    ),
}


def serif(pt, *style):
    return (SERIF, -round(pt * PX_PER_PT), *style)


def apply_theme(name):
    globals().update(THEMES[name])


apply_theme("light")

PAD, CARD_PAD = 12, 14
FONT = "Helvetica"
H1 = (FONT, 13, "bold")
BOLD = (FONT, 11, "bold")
TEXT = (FONT, 12)
SMALL = (FONT, 10)
SERIF = "Times New Roman"
BODY_SIZE = 16
PX_PER_PT = 96 / 72
BODY_WEIGHT = "normal"
LINE_GAP = 0
SERIF_CANDIDATES = ["Times New Roman", "Liberation Serif", "Tinos", "Nimbus Roman", "FreeSerif", "DejaVu Serif"]
HEADING_SIZES = {1: 16, 2: 14, 3: 13}

IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
SECTION_RE = re.compile(r"^#\s*(texto|text|quest[aã]o|question)\s*$", re.I | re.M)
HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*)$")
LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+(.*)$")
INLINE_RE = re.compile(
    r"\$\$(.+?)\$\$|\$([^$]+?)\$|\*\*(?=\S)(.+?)(?<=\S)\*\*|\*(?=\S)(.+?)(?<=\S)\*|`([^`]+)`", re.S
)
INLINE_TAGS = {3: "b", 4: "i", 5: "c"}
ZOOMS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
TABS = ["Content", "Structure", "Metadata"]
BOX_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#00a8c6", "#d81b9b", "#8a8a00", "#008080", "#9a6324"]


def box_color(k):
    return BOX_COLORS[k % len(BOX_COLORS)]


def parse(value, default):
    return json.loads(value) if isinstance(value, str) else default


def as_str(value):
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def load_review():
    return json.loads(REVIEW_FILE.read_text(encoding="utf-8")) if REVIEW_FILE.exists() else {}


def split_sections(content):
    parts = {"context": "", "question": ""}
    found = list(SECTION_RE.finditer(content))
    if not found:
        parts["question"] = content
        return parts
    parts["context"] = content[: found[0].start()]
    for k, m in enumerate(found):
        end = found[k + 1].start() if k + 1 < len(found) else len(content)
        key = "context" if m.group(1).lower() in ("texto", "text") else "question"
        parts[key] += "\n" + content[m.end():end]
    return parts


@lru_cache(maxsize=None)
def image_size(path):
    with Image.open(path) as im:
        return im.size


CASES_RE = re.compile(r"^(.*?)\\begin\{cases\}(.*?)\\end\{cases\}(.*)$", re.S)


def tex_fix(expr):
    expr = re.sub(r"\\(?:text|textrm|textbf|mbox)\{([^}]*)\}",
                  lambda m: "\\mathrm{" + m.group(1).replace(" ", "\\ ") + "}", expr)
    expr = re.sub(r"\\(ge|le|ne)(?![a-zA-Z])", r"\\\1q", expr)
    return expr.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")


def render_tex(expr, size, color):
    if not expr.strip():
        return None
    buf = io.BytesIO()
    mathtext.math_to_image(f"${tex_fix(expr)}$", buf, prop=FontProperties(size=size), dpi=96, format="png", color=color)
    return Image.open(buf).convert("RGBA")


def stack(images, gap, horizontal, align="center"):
    images = [im for im in images if im]
    if not images:
        return None
    w = sum(i.width for i in images) + gap * (len(images) - 1) if horizontal else max(i.width for i in images)
    h = max(i.height for i in images) if horizontal else sum(i.height for i in images) + gap * (len(images) - 1)
    out, pos = Image.new("RGBA", (w, h), (0, 0, 0, 0)), 0
    for im in images:
        x, y = (pos, (h - im.height) // 2) if horizontal else (0, pos)
        out.paste(im, (x, y), im)
        pos += (im.width if horizontal else im.height) + gap
    return out


def render_cases(pre, body, post, size, color):
    rows = [stack([render_tex(c, size, color) for c in r.split("&")], 14, True)
            for r in body.split("\\\\") if r.strip()]
    rows = stack(rows, 6, False)
    brace = render_tex(r"\left\{\right.", size * 4, color) or render_tex(r"\{", size * 4, color)
    brace = brace.resize((max(1, round(brace.width * rows.height / brace.height)), rows.height), Image.LANCZOS)
    parts = [render_tex(pre, size, color), brace, rows, render_tex(post, size, color)]
    return stack(parts, 6, True)


@lru_cache(maxsize=None)
def math_image(expr, size, color):
    if mathtext is None:
        return None
    try:
        m = CASES_RE.match(expr)
        return render_cases(*m.groups(), size, color) if m else render_tex(expr, size, color)
    except Exception:
        return None


def make_photo(path, width=None, boxes=()):
    img = Image.open(path).convert("RGB")
    if width is not None and width != img.width:
        img = img.resize((max(1, width), max(1, round(img.height * width / img.width))), Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    for k, b in enumerate(boxes):
        x0, y0, x1, y1 = b[0] * img.width, b[1] * img.height, b[2] * img.width, b[3] * img.height
        draw.rectangle([x0, y0, x1, y1], outline=box_color(k), width=3)
        draw.rectangle([x0, y0, x0 + 20, y0 + 16], fill=box_color(k))
        draw.text((x0 + 6, y0 + 2), str(k + 1), fill="white")
    return ImageTk.PhotoImage(img)


class Scrolled(tk.Frame):
    def __init__(self, master, hscroll=False):
        super().__init__(master, bg=PANEL)
        self.hscroll = hscroll
        self.cv = tk.Canvas(self, highlightthickness=0, bg=PANEL)
        sb = ttk.Scrollbar(self, command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        if hscroll:
            hb = ttk.Scrollbar(self, orient="horizontal", command=self.cv.xview)
            self.cv.configure(xscrollcommand=hb.set)
            hb.pack(side="bottom", fill="x")
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=PANEL)
        self.win = self.cv.create_window(0, 0, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        if not hscroll:
            self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))
        self.cv.bind("<Enter>", self.capture)
        self.cv.bind("<Leave>", self.release)

    def capture(self, _):
        def wheel(e):
            d = -3 if e.delta > 0 else 3
            if self.hscroll and e.state & 0x1:
                self.cv.xview_scroll(d, "units")
            else:
                self.cv.yview_scroll(d, "units")

        self.cv.bind_all("<MouseWheel>", wheel)
        self.cv.bind_all("<Button-4>", lambda e: self.cv.yview_scroll(-3, "units"))
        self.cv.bind_all("<Button-5>", lambda e: self.cv.yview_scroll(3, "units"))

    def release(self, _):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.cv.unbind_all(seq)

    def width(self):
        return max(self.cv.winfo_width() - 2 * PAD - 16, 100)

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.cv.yview_moveto(0)
        self.cv.xview_moveto(0)


def flat_button(parent, text, command):
    return tk.Button(
        parent, text=text, command=command, takefocus=0, relief="flat", bd=0, bg=BTN, fg=FG,
        activebackground=BORDER, activeforeground=FG, highlightthickness=0, padx=10, pady=4, cursor="hand2",
    )


def panel_header(parent, title):
    bar = tk.Frame(parent, bg=PANEL)
    bar.pack(side="top", fill="x")
    tk.Label(bar, text=title, bg=PANEL, fg=FG, font=H1).pack(side="left", padx=PAD, pady=8)
    tk.Frame(parent, bg=BORDER, height=1).pack(side="top", fill="x")
    return bar


class Viewer:
    def __init__(self, root, rows, review):
        self.root, self.rows, self.review = root, rows, review
        self.i, self.refs, self.left_refs, self.job, self.tab = 0, [], [], None, 0
        self.zoom_i = ZOOMS.index(1.0)
        self.ctx = None
        self.theme = "light"

        root.title("Question viewer")
        root.geometry("1500x900")
        root.bind("<Left>", lambda e: self.go(self.i - 1))
        root.bind("<Right>", lambda e: self.go(self.i + 1))
        root.bind("q", lambda e: root.destroy())

        self.build()

    def build(self):
        root = self.root
        root.configure(bg=BG)

        style = ttk.Style(root)
        style.theme_use("clam")
        for orient in ("Vertical", "Horizontal"):
            name = f"{orient}.TScrollbar"
            style.configure(name, background=BORDER, troughcolor=PANEL, bordercolor=PANEL,
                            lightcolor=BORDER, darkcolor=BORDER, arrowcolor=MUTED)
            style.map(name, background=[("active", MUTED), ("pressed", MUTED)],
                      lightcolor=[("active", MUTED), ("pressed", MUTED)],
                      darkcolor=[("active", MUTED), ("pressed", MUTED)])

        bar = tk.Frame(root, bg=BG)
        bar.pack(side="top", fill="x", padx=6, pady=4)
        flat_button(bar, "◀ Prev", lambda: self.go(self.i - 1)).pack(side="left", padx=2)
        flat_button(bar, "Skip ▶", lambda: self.go(self.i + 1)).pack(side="left", padx=2)
        self.goto = tk.Entry(bar, width=24, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                             highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.goto.pack(side="left", padx=8, ipady=3)
        self.goto.bind("<Return>", self.jump)
        tk.Label(bar, text="number or filename, Enter to jump", bg=BG, fg=MUTED, font=SMALL).pack(side="left")

        self.info = tk.Label(root, anchor="w", font=SMALL, bg=BG, fg=MUTED)
        self.info.pack(side="bottom", fill="x", padx=8, pady=3)

        self.paned = tk.PanedWindow(root, orient="horizontal", sashwidth=6, bg=BG, bd=0, sashrelief="flat")
        self.paned.pack(fill="both", expand=True)

        left_box = tk.Frame(self.paned, bg=PANEL)
        hdr = panel_header(left_box, "Screenshot")
        flat_button(hdr, "Fit", lambda: self.set_zoom(ZOOMS.index(1.0))).pack(side="right", padx=PAD, pady=4)
        flat_button(hdr, "+", lambda: self.set_zoom(self.zoom_i + 1)).pack(side="right", padx=2)
        self.zoom_lbl = tk.Label(hdr, text="100%", width=6, bg=BTN, fg=FG, font=BOLD)
        self.zoom_lbl.pack(side="right", padx=2, ipady=4)
        flat_button(hdr, "-", lambda: self.set_zoom(self.zoom_i - 1)).pack(side="right", padx=2)
        self.left = Scrolled(left_box, hscroll=True)
        self.left.pack(fill="both", expand=True)

        right_box = tk.Frame(self.paned, bg=PANEL)
        hdr = panel_header(right_box, "Extracted")
        label = "☀  Light mode" if self.theme == "dark" else "☾  Dark mode"
        flat_button(hdr, label, self.toggle_theme).pack(side="right", padx=PAD, pady=4)
        tabbar = tk.Frame(right_box, bg=PANEL)
        tabbar.pack(side="top", fill="x")
        self.tab_lbls = []
        for k, name in enumerate(TABS):
            cell = tk.Frame(tabbar, bg=PANEL)
            cell.pack(side="left", padx=(PAD if k == 0 else 4, 4))
            lb = tk.Label(cell, text=name, bg=PANEL, font=BOLD, cursor="hand2", padx=6, pady=6)
            lb.pack()
            line = tk.Frame(cell, height=2, bg=PANEL)
            line.pack(fill="x")
            lb.bind("<Button-1>", lambda e, k=k: self.set_tab(k))
            self.tab_lbls.append((lb, line))
        tk.Frame(right_box, bg=BORDER, height=1).pack(side="top", fill="x")
        self.right = Scrolled(right_box)
        self.right.pack(fill="both", expand=True)

        self.paned.add(left_box, stretch="always")
        self.paned.add(right_box, stretch="always")
        self.paned.bind("<Configure>", self.schedule)

    def toggle_theme(self):
        self.root.after_idle(self._apply_theme)

    def _apply_theme(self):
        if self.job:
            self.paned.after_cancel(self.job)
            self.job = None
        typed = self.goto.get()
        self.theme = "light" if self.theme == "dark" else "dark"
        apply_theme(self.theme)
        for w in self.root.winfo_children():
            w.destroy()
        self.build()
        self.goto.insert(0, typed)
        self.root.update_idletasks()
        self.render()

    def schedule(self, _=None):
        if self.job:
            self.paned.after_cancel(self.job)
        self.job = self.paned.after(150, self.render)

    def go(self, i):
        if 0 <= i < len(self.rows):
            self.i = i
            self.render()

    def jump(self, _):
        q = self.goto.get().strip().lower()
        if q.isdigit():
            self.go(int(q) - 1)
        else:
            k = next((k for k, r in enumerate(self.rows) if q in r["screenshot"].lower()), None)
            if k is not None:
                self.go(k)
        self.paned.focus_set()

    def set_tab(self, k):
        self.tab = k
        self.render_left()
        self.render_right()

    def set_zoom(self, zi):
        if 0 <= zi < len(ZOOMS):
            self.zoom_i = zi
            self.render_left()

    @property
    def row(self):
        return self.rows[self.i]

    def boxes(self):
        return self.review.get(self.row["screenshot"], {}).get("boxes", [])

    def shot_size(self):
        p = PNGS_DIR / self.row["screenshot"]
        return image_size(p) if p.exists() else None

    def render(self):
        self.job = None
        self.info.config(text=f"[{self.i + 1}/{len(self.rows)}]  {self.row['screenshot']}")
        self.render_left()
        self.render_right()

    def render_left(self):
        self.left.clear()
        z = ZOOMS[self.zoom_i]
        self.zoom_lbl.config(text=f"{round(z * 100)}%")
        path = PNGS_DIR / self.row["screenshot"]
        if not path.exists():
            tk.Label(self.left.inner, text=f"(missing: {path.name})", fg=ERR_FG, bg=PANEL).pack(padx=PAD, pady=PAD)
            return
        photo = make_photo(path, round(self.left.width() * z), self.boxes() if self.tab == 1 else ())
        self.left_refs = [photo]
        tk.Label(self.left.inner, image=photo, bg=PANEL, bd=0).pack(anchor="nw", padx=PAD, pady=PAD)

    def render_right(self):
        self.refs = []
        self.right.clear()
        row = self.row
        content = as_str(row.get("content"))
        for k, (lb, line) in enumerate(self.tab_lbls):
            lb.config(fg=ACCENT if k == self.tab else MUTED)
            line.config(bg=ACCENT if k == self.tab else PANEL)
        images = parse(row.get("images"), [])
        self.ctx = {"names": images, "boxes": self.boxes(), "used": set()}
        [self.tab_content, self.tab_structure, self.tab_meta][self.tab](row, content, images)

    def card(self, title):
        outer = tk.Frame(self.right.inner, bg=PANEL)
        outer.pack(fill="x", padx=PAD, pady=(PAD, 0))
        card = tk.Frame(outer, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=CARD_PAD, pady=(CARD_PAD - 2, 4))
        tk.Frame(head, bg=ACCENT, width=4, height=18).pack(side="left")
        tk.Label(head, text=title, bg=CARD, fg=FG, font=H1).pack(side="left", padx=8)
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", padx=CARD_PAD, pady=(2, CARD_PAD))
        return body

    def width(self):
        return self.right.width() - 2 * (PAD + CARD_PAD) - 2

    def note(self, parent, txt, fg=None, font=TEXT, **pack):
        tk.Label(parent, text=txt, bg=CARD, fg=fg or FG, font=font, anchor="w", justify="left",
                 wraplength=self.width()).pack(fill="x", **({"pady": 2} | pack))

    def resolve(self, target):
        names = self.ctx["names"]
        name = Path(target.strip()).name
        if name in names:
            idx = names.index(name)
        else:
            idx = next((k for k, n in enumerate(names) if Path(n).stem == Path(name).stem), None)
            if idx is None:
                idx = next((k for k in range(len(names)) if k not in self.ctx["used"]), None)
        if idx is not None:
            self.ctx["used"].add(idx)
        return idx

    def picture(self, parent, idx, max_w):
        names, boxes = self.ctx["names"], self.ctx["boxes"]
        if idx is None or idx >= len(names):
            self.note(parent, "(image not found)", ERR_FG)
            return
        path = IMGS_DIR / names[idx]
        if not path.exists():
            self.note(parent, f"(missing: {names[idx]})", ERR_FG)
            return
        shot = self.shot_size()
        if shot and idx < len(boxes):
            width = max(1, round((boxes[idx][2] - boxes[idx][0]) * shot[0]))
        else:
            width = image_size(path)[0]
        photo = make_photo(path, min(width, max_w))
        self.refs.append(photo)
        tk.Label(parent, image=photo, bg=CARD, bd=0).pack(anchor="center", pady=6)

    def block(self, parent, txt, font=None):
        size, *style = font or (BODY_SIZE, BODY_WEIGHT)
        box = tk.Frame(parent, bg=CARD, height=1)
        box.pack_propagate(False)
        box.pack(fill="x", pady=2)
        t = tk.Text(box, bg=CARD, fg=BODY, font=serif(size, *style), relief="flat", bd=0, highlightthickness=0,
                    wrap="word", height=1, width=1, padx=0, pady=0, cursor="arrow", spacing2=LINE_GAP)
        t.pack(fill="both", expand=True)
        t.tag_configure("b", font=serif(size, "bold"))
        t.tag_configure("i", font=serif(size, "italic"))
        t.tag_configure("c", font=("Courier", -round((size - 1) * PX_PER_PT)), foreground=MUTED)
        pos = 0
        for m in INLINE_RE.finditer(txt):
            t.insert("end", txt[pos:m.start()])
            idx = m.lastindex
            img = math_image(m.group(idx).strip(), size, BODY) if idx <= 2 else None
            if img:
                photo = ImageTk.PhotoImage(img)
                self.refs.append(photo)
                t.image_create("end", image=photo, align="center")
            elif idx <= 2:
                t.insert("end", m.group(0))
            else:
                t.insert("end", m.group(idx), INLINE_TAGS[idx])
            pos = m.end()
        t.insert("end", txt[pos:])

        def fit(_=None):
            if not t.winfo_exists():
                return
            px = t.count("1.0", "end", "update", "ypixels")
            px = px[0] if isinstance(px, tuple) else px or 0
            if int(box.cget("height")) != px + 2:
                box.config(height=px + 2)
            t.yview_moveto(0)

        t.bind("<Configure>", fit)
        t.config(state="disabled")
        for delay in (0, 50, 200):
            t.after(delay, fit)
        return t

    def bullet(self, parent, marker, txt):
        line = tk.Frame(parent, bg=CARD)
        line.pack(fill="x")
        tk.Label(line, text="•" if marker in "-*+" else marker, bg=CARD, fg=ACCENT, font=serif(BODY_SIZE)).pack(side="left", anchor="n")
        sub = tk.Frame(line, bg=CARD)
        sub.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.block(sub, txt)

    def md_text(self, parent, txt):
        para = []

        def flush():
            if para:
                self.block(parent, "\n".join(para))
                para.clear()

        for line in txt.split("\n"):
            h, li = HEADING_RE.match(line), LIST_RE.match(line)
            if not line.strip():
                flush()
            elif h:
                flush()
                self.block(parent, h.group(2), (HEADING_SIZES.get(len(h.group(1)), 12) + 5, "bold"))
            elif li:
                flush()
                self.bullet(parent, li.group(1), li.group(2))
            else:
                para.append(line)
        flush()

    def md(self, parent, txt, w):
        pos = 0
        for m in IMG_RE.finditer(txt):
            self.md_text(parent, txt[pos:m.start()])
            self.picture(parent, self.resolve(m.group(1)), w)
            pos = m.end()
        self.md_text(parent, txt[pos:])

    def tab_content(self, row, content, images):
        w = self.width()
        parts = split_sections(content)
        options = parse(row.get("options"), {})
        if parts["context"].strip():
            self.md(self.card("Context"), parts["context"], w)
        self.md(self.card("Question"), parts["question"], w)
        if options:
            body = self.card(f"Options ({len(options)})")
            for key, txt in options.items():
                self.md(body, f"{key.lower()}) {as_str(txt)}", w)
        unused = [k for k in range(len(images)) if k not in self.ctx["used"]]
        if unused:
            body = self.card("Images")
            for k in unused:
                self.note(body, images[k], ACCENT, BOLD)
                self.picture(body, k, w)

    def kv(self, parent, key, value):
        line = tk.Frame(parent, bg=CARD)
        line.pack(fill="x", pady=2)
        tk.Label(line, text=str(key), bg=CARD, fg=MUTED, font=BOLD, anchor="w", width=26).pack(side="left", anchor="n")
        tk.Label(line, text=str(value), bg=CARD, fg=FG, font=TEXT, anchor="w", justify="left",
                 wraplength=max(self.width() - 200, 100)).pack(side="left", fill="x", expand=True)

    def tab_structure(self, row, content, images):
        options = parse(row.get("options"), {})
        boxes, shot = self.boxes(), self.shot_size()
        body = self.card("Summary")
        for label, value in [
            ("file", row["screenshot"]),
            ("characters", len(content)),
            ("image markers", len(IMG_RE.findall(content))),
            ("images", len(images)),
            ("bounding boxes", len(boxes)),
            ("Options", len(options)),
        ]:
            self.kv(body, label, value)
        body = self.card(f"Images ({len(images)})")
        for k, name in enumerate(images):
            head = tk.Frame(body, bg=CARD)
            head.pack(fill="x", pady=(8, 0))
            tk.Frame(head, bg=box_color(k), width=14, height=14).pack(side="left", padx=(0, 8))
            tk.Label(head, text=f"{k + 1}. {name}", bg=CARD, fg=FG, font=BOLD, anchor="w", justify="left",
                     wraplength=self.width() - 30).pack(side="left", fill="x", expand=True)
            if k < len(boxes) and shot:
                b = boxes[k]
                px = [round(b[0] * shot[0]), round(b[1] * shot[1]), round(b[2] * shot[0]), round(b[3] * shot[1])]
                self.note(body, f"bbox: {b}\npx: {px}  size: {px[2] - px[0]}x{px[3] - px[1]}", MUTED, SMALL)
            else:
                self.note(body, "no bounding box", MUTED, SMALL)
        if len(images) != len(boxes):
            self.note(body, f"{len(images)} images vs {len(boxes)} boxes", ERR_FG, SMALL, pady=(8, 0))

    def tab_meta(self, row, content, images):
        body = self.card("Metadata")
        for k, v in row.items():
            if k not in ("content", "options", "images"):
                self.kv(body, k, as_str(v) or "-")
        review = self.review.get(row["screenshot"])
        if review:
            self.kv(body, "review status", review.get("status", "-"))
        shot = self.shot_size()
        if shot:
            self.kv(body, "screenshot size", f"{shot[0]}x{shot[1]}")


def main():
    global SERIF
    df = pd.read_csv(CSV_FILE).drop_duplicates("screenshot")
    rows = df.to_dict("records")
    print(f"{len(rows)} questions")
    root = tk.Tk()
    global PX_PER_PT
    PX_PER_PT = root.winfo_fpixels("1i") / 72
    available = set(tkfont.families())
    SERIF = next((f for f in SERIF_CANDIDATES if f in available), SERIF)
    Viewer(root, rows, load_review())
    root.mainloop()


if __name__ == "__main__":
    main()
