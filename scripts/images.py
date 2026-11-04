#!/usr/bin/env python3
import ast
import bisect
import json
import re
from pathlib import Path

import pandas as pd
from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parents[1]
PDF_DIR, JSON_DIR, TXT_DIR, PNGS_DIR = (PROJECT_DIR / d for d in ("provas", "bboxes/mineru", "bboxes/gemini", "screenshots"))
for d in (JSON_DIR, TXT_DIR, PNGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

CSV_FILE = Path("questions.csv")
OUT_DIR = Path("images")
SCALE = 6.0
REEXPORT = False

IMAGE_TYPES = {"image", "figure"}
REVIEW_FILE = Path("review.json")
MARGIN_PX = 0
NORM, Y_TOL = 1000, 20
A4 = (595.0, 842.0)
QUESTION_RE = re.compile(r"_q(\d+)", re.I)

try:
    import pymupdf
except ImportError:
    pymupdf = None


def read_json(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_json(p, d):
    rows = [f"{json.dumps(k, ensure_ascii=False)}:{json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
            for k, v in d.items()]
    p.write_text("{\n" + ",\n".join(rows) + "\n}", encoding="utf-8")


def sort_boxes(boxes, tol=0.02):
    rows = []
    for b in sorted(boxes, key=lambda b: b[1]):
        for r in rows:
            if abs(b[1] - r[0]) <= tol:
                r[1].append(b)
                break
        else:
            rows.append([b[1], [b]])
    return [b for _, r in rows for b in sorted(r, key=lambda b: b[0])]


def union_box(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def render_from_pdf(sources, layout, b, scale):
    doc = sources.doc(layout["stem"])
    if doc is None:
        return None
    W, H = layout["W"], layout["H"]
    bx0, by0, bx1, by1 = b[0] * W, b[1] * H, b[2] * W, b[3] * H
    parts = []
    for p in layout["pieces"]:
        iy0, iy1 = max(by0, p["y_off"]), min(by1, p["y_off"] + p["h"])
        ix0, ix1 = max(bx0, 0.0), min(bx1, p["w"])
        if iy1 - iy0 <= 0.5 or ix1 - ix0 <= 0.5:
            continue
        ox, oy = p["x0n"] * p["kx"], p["y0n"] * p["ky"]
        rect = pymupdf.Rect(ox + ix0, oy + iy0 - p["y_off"], ox + ix1, oy + iy1 - p["y_off"])
        pix = doc[p["page"]].get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False)
        parts.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    canvas = Image.new("RGB", (max(i.width for i in parts), sum(i.height for i in parts)), "white")
    y = 0
    for i in parts:
        canvas.paste(i, (0, y))
        y += i.height
    return canvas


def save_crops(path, boxes, out_dir, layout=None, sources=None, scale=6):
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    for old in out_dir.glob(f"{stem}_img*.png"):
        old.unlink()
    img = Image.open(path).convert("RGB")
    W, H = img.size
    n = 0
    for b in boxes:
        crop = None
        if layout and sources:
            try:
                crop = render_from_pdf(sources, layout, b, scale)
            except Exception as ex:
                print(f"Warning: PDF render failed ({ex}); using screenshot.")
        if crop is None:
            x0, y0 = max(0, int(b[0] * W) - MARGIN_PX), max(0, int(b[1] * H) - MARGIN_PX)
            x1, y1 = min(W, round(b[2] * W) + MARGIN_PX), min(H, round(b[3] * H) + MARGIN_PX)
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            crop = img.crop((x0, y0, x1, y1))
        elif crop.width < 2 or crop.height < 2:
            continue
        n += 1
        crop.save(out_dir / f"{stem}_img{n}.png")
    return n


def reading_order(pages, tol=Y_TOL):
    out = []
    for blocks in pages:
        rows = []
        for b in sorted(blocks or [], key=lambda b: b["bbox"][1]):
            r = next((r for r in rows if abs(b["bbox"][1] - r[0]) <= tol), None)
            if r:
                r[1].append(b)
            else:
                rows.append([b["bbox"][1], [b]])
        out.append([b for _, r in sorted(rows, key=lambda x: x[0]) for b in sorted(r, key=lambda b: b["bbox"][0])])
    for i, b in enumerate(b for p in out for b in p):
        b["id"] = i
    return out


class Sources:
    def __init__(self):
        self.cache, self.docs, self.warned = {}, {}, False
        self.stems = [p.stem for p in JSON_DIR.glob("*.json")]

    def exam(self, stem):
        if stem not in self.cache:
            jp, tp = JSON_DIR / f"{stem}.json", TXT_DIR / f"{stem}.txt"
            if not jp.exists() or not tp.exists():
                self.cache[stem] = None
            else:
                pages = reading_order(json.loads(jp.read_text(encoding="utf-8")))
                blocks = ast.literal_eval(tp.read_text(encoding="utf-8").strip())
                els = sorted((e for p in pages for e in p), key=lambda e: e["id"])
                self.cache[stem] = (els, [e["id"] for e in els], blocks)
        return self.cache[stem]

    def doc(self, stem):
        if stem not in self.docs:
            self.docs[stem] = None
            pdf = PDF_DIR / f"{stem}.pdf"
            if pdf.exists() and pymupdf:
                try:
                    self.docs[stem] = pymupdf.open(pdf)
                except Exception as ex:
                    print(f"Warning: cannot open {pdf}: {ex}")
            elif not self.warned:
                print("Warning: PDFs or pymupdf unavailable; assuming A4 pages and cropping from screenshots.")
                self.warned = True
        return self.docs[stem]

    def page_size(self, stem, p):
        d = self.doc(stem)
        if d is None:
            return A4
        r = d[p].rect
        return r.width, r.height


def identify(name, sources):
    base = Path(name).stem
    stems = [s for s in sources.stems if base.lower().startswith(s.lower())]
    q = QUESTION_RE.search(base)
    if not stems or not q:
        return None
    return max(stems, key=len), int(q.group(1))


def question_strips(blocks, qnum):
    ctx_of = {c: i for i, b in enumerate(blocks) if b[0] == "context" for c in b[3]}
    n = 0
    for i, q in enumerate(blocks):
        if q[0] != "question":
            continue
        n += 1
        if n != qnum:
            continue
        strips = []
        if q[1] in ctx_of:
            ci = ctx_of[q[1]]
            strips.append((blocks[ci][1], blocks[ci][2]))
            j = ci + 1
            while j < i and blocks[j][0] == "continuation":
                strips.append((blocks[j][1], blocks[j][2]))
                j += 1
        strips.append((q[1], q[2]))
        j = i + 1
        while j < len(blocks) and blocks[j][0] == "continuation":
            strips.append((blocks[j][1], blocks[j][2]))
            j += 1
        return strips
    return []


def screenshot_layout(name, sources):
    ident = identify(name, sources)
    if not ident:
        return None
    stem, qnum = ident
    exam = sources.exam(stem)
    if exam is None:
        return None
    els, ids, blocks = exam
    pieces, y_off = [], 0.0
    for a, z in question_strips(blocks, qnum):
        qs = els[bisect.bisect_left(ids, a):bisect.bisect_right(ids, z)]
        for p in sorted({e["page"] for e in qs}):
            es = [e for e in qs if e["page"] == p]
            pw, ph = sources.page_size(stem, p)
            x0, y0 = min(e["bbox"][0] for e in es), min(e["bbox"][1] for e in es)
            x1, y1 = max(e["bbox"][2] for e in es), max(e["bbox"][3] for e in es)
            if x1 <= x0 or y1 <= y0:
                continue
            kx, ky = pw / NORM, ph / NORM
            w, h = (x1 - x0) * kx, (y1 - y0) * ky
            imgs = [((e["bbox"][0] - x0) * kx, (e["bbox"][1] - y0) * ky,
                     (e["bbox"][2] - x0) * kx, (e["bbox"][3] - y0) * ky)
                    for e in es if e["category"] in IMAGE_TYPES]
            pieces.append({"page": p, "x0n": x0, "y0n": y0, "kx": kx, "ky": ky,
                           "w": w, "h": h, "y_off": y_off, "imgs": imgs})
            y_off += h
    if not pieces:
        return {"stem": stem, "W": 1.0, "H": 1.0, "pieces": []}
    return {"stem": stem, "W": max(p["w"] for p in pieces), "H": y_off, "pieces": pieces}


def detect_from_layout(layout):
    if not layout:
        return None
    W, H, out = layout["W"], layout["H"], []
    for p in layout["pieces"]:
        for ix0, iy0, ix1, iy1 in p["imgs"]:
            box = [ix0 / W, (p["y_off"] + iy0) / H, ix1 / W, (p["y_off"] + iy1) / H]
            out.append({"type": "image", "bbox": [round(min(max(v, 0.0), 1.0), 4) for v in box]})
    return out


def empty_reason(name, sources):
    ident = identify(name, sources)
    if not ident:
        return f"name does not start with a json stem in '{JSON_DIR}' or lacks '_q<n>'"
    stem, q = ident
    exam = sources.exam(stem)
    if exam is None:
        return f"missing json/txt for {stem}"
    if not question_strips(exam[2], q):
        return f"question q{q} not found in txt of {stem}"
    return "no image/figure block inside the question bbox"


def build_detections(names, paths, sources):
    det, layouts, reasons, mismatched = {}, {}, {}, []
    for n in names:
        lay = screenshot_layout(n, sources)
        det[n] = detect_from_layout(lay) or []
        if not det[n]:
            reasons.setdefault(empty_reason(n, sources), []).append(n)
        if lay and lay["pieces"] and sources.doc(lay["stem"]) is not None:
            with Image.open(paths[n]) as im:
                sw, sh = im.size
            if abs((lay["H"] / lay["W"]) / (sh / sw) - 1) > 0.03:
                mismatched.append(n)
            else:
                layouts[n] = lay
    for reason, items in reasons.items():
        print(f"No boxes ({len(items)}): {reason}  e.g. {items[:2]}")
    if mismatched:
        print(f"Warning: {len(mismatched)} screenshots with mismatched aspect ratio "
              f"(e.g. {mismatched[:2]}); cropping from screenshot.")
    print(f"{len(layouts)} screenshots will be cropped from the PDF")
    return det, layouts


def review(names, paths, out_dir, det, layouts, sources, scale):
    import tkinter as tk
    from tkinter import messagebox
    from PIL import ImageTk

    rev = read_json(REVIEW_FILE)

    class App:
        def __init__(self, root):
            root.title("Image review")
            root.geometry("1300x800")
            self.cv = tk.Canvas(root, bg="#222", highlightthickness=0)
            bar = tk.Frame(root)
            self.info = tk.Label(root, anchor="w", font=("Helvetica", 11))
            self.info.pack(side="bottom", fill="x")
            bar.pack(side="bottom", fill="x")
            self.cv.pack(fill="both", expand=True)
            for txt, cmd in [("◀ Prev", self.prev), ("Group (G)", self.group), ("Delete (D)", self.delete),
                             ("Select all (A)", self.select_all), ("Undo (U)", self.undo),
                             ("Reset (R)", self.reset), ("Skip ▶", self.next), ("APPROVE (Enter)", self.approve)]:
                tk.Button(bar, text=txt, command=cmd, takefocus=0).pack(side="left", padx=2, pady=3)
            for key, cmd in [("<Return>", self.approve), ("g", self.group), ("d", self.delete),
                             ("<Delete>", self.delete), ("a", self.select_all), ("u", self.undo),
                             ("r", self.reset), ("<Escape>", self.clear_sel), ("<Left>", self.prev),
                             ("<Right>", self.next), ("q", root.destroy)]:
                root.bind(key, lambda e, c=cmd: c())
            self.cv.bind("<Configure>", lambda e: self.draw())
            self.cv.bind("<ButtonPress-1>", self.press)
            self.cv.bind("<B1-Motion>", self.drag)
            self.cv.bind("<ButtonRelease-1>", self.release)
            self.load(next((k for k, n in enumerate(names) if n not in rev), 0))

        def load(self, i):
            self.i = i
            name = names[i]
            self.img = Image.open(paths[name]).convert("RGB")
            self.orig = [list(b["bbox"]) for b in det.get(name, []) if b["type"] in IMAGE_TYPES]
            self.boxes = sort_boxes([list(b) for b in (rev[name]["boxes"] if name in rev else self.orig)])
            self.sel, self.hist = set(), []
            self.draw()

        def snap(self):
            self.hist.append([list(b) for b in self.boxes])

        def set_boxes(self, boxes):
            self.boxes, self.sel = sort_boxes(boxes), set()
            self.overlay()

        def draw(self):
            cw, ch = self.cv.winfo_width(), self.cv.winfo_height()
            if cw < 20 or ch < 20:
                return
            W, H = self.img.size
            s = min(cw / W, ch / H)
            self.dw, self.dh = max(1, int(W * s)), max(1, int(H * s))
            self.tk_img = ImageTk.PhotoImage(self.img.resize((self.dw, self.dh), Image.LANCZOS))
            self.cv.delete("all")
            self.cv.create_image(0, 0, anchor="nw", image=self.tk_img)
            self.overlay()

        def overlay(self):
            self.cv.delete("ov")
            for k, (x0, y0, x1, y1) in enumerate(self.boxes):
                sel = k in self.sel
                color = "#ff2d2d" if sel else "#1e6bff"
                self.cv.create_rectangle(x0 * self.dw, y0 * self.dh, x1 * self.dw, y1 * self.dh,
                                         outline=color, width=4 if sel else 2, tags="ov")
                self.cv.create_rectangle(x0 * self.dw, y0 * self.dh, x0 * self.dw + 26, y0 * self.dh + 20,
                                         fill=color, outline=color, tags="ov")
                self.cv.create_text(x0 * self.dw + 13, y0 * self.dh + 10, text=str(k + 1), fill="white",
                                    font=("Helvetica", 11, "bold"), tags="ov")
            name = names[self.i]
            status = rev.get(name, {}).get("status", "pending")
            self.info.config(text=f"[{self.i + 1}/{len(names)}] {name} — {status} — "
                                  f"{len(self.boxes)} image(s) | selected: {len(self.sel)}")

        def press(self, e):
            self.p0, self.dragged = (e.x, e.y), False

        def drag(self, e):
            if abs(e.x - self.p0[0]) + abs(e.y - self.p0[1]) > 6:
                self.cv.delete("tmp")
                self.cv.create_rectangle(*self.p0, e.x, e.y, outline="#00c853", width=2, dash=(4, 3), tags="tmp")
                self.dragged = True

        def release(self, e):
            self.cv.delete("tmp")
            if self.dragged:
                x0, x1 = sorted((self.p0[0], e.x))
                y0, y1 = sorted((self.p0[1], e.y))
                new = [min(max(v, 0.0), 1.0) for v in (x0 / self.dw, y0 / self.dh, x1 / self.dw, y1 / self.dh)]
                if new[2] - new[0] > 0.005 and new[3] - new[1] > 0.005:
                    self.snap()
                    self.set_boxes(self.boxes + [new])
                return
            nx, ny = e.x / self.dw, e.y / self.dh
            hit = [k for k, b in enumerate(self.boxes) if b[0] <= nx <= b[2] and b[1] <= ny <= b[3]]
            if hit:
                k = min(hit, key=lambda k: (self.boxes[k][2] - self.boxes[k][0]) * (self.boxes[k][3] - self.boxes[k][1]))
                self.sel ^= {k}
                self.overlay()

        def group(self):
            if len(self.sel) < 2:
                return
            self.snap()
            u = union_box([self.boxes[k] for k in self.sel])
            self.set_boxes([b for k, b in enumerate(self.boxes) if k not in self.sel] + [u])

        def delete(self):
            if self.sel:
                self.snap()
                self.set_boxes([b for k, b in enumerate(self.boxes) if k not in self.sel])

        def select_all(self):
            self.sel = set(range(len(self.boxes)))
            self.overlay()

        def clear_sel(self):
            self.sel = set()
            self.overlay()

        def undo(self):
            if self.hist:
                self.set_boxes(self.hist.pop())

        def reset(self):
            self.snap()
            self.set_boxes([list(b) for b in self.orig])

        def approve(self):
            name = names[self.i]
            save_crops(paths[name], self.boxes, out_dir, layouts.get(name), sources, scale)
            rev[name] = {"status": "approved", "boxes": self.boxes}
            write_json(REVIEW_FILE, rev)
            self.next()

        def next(self):
            if self.i + 1 < len(names):
                self.load(self.i + 1)
            else:
                self.overlay()
                messagebox.showinfo("Done", f"Last screenshot. Approved: {len(rev)}/{len(names)}.")

        def prev(self):
            if self.i > 0:
                self.load(self.i - 1)

    root = tk.Tk()
    App(root)
    root.mainloop()


def main():
    df = pd.read_csv(CSV_FILE)
    names = df.loc[df["has_image"] == 1, "screenshot"].astype(str).str.strip().drop_duplicates().tolist()
    index = {p.name: p for p in PNGS_DIR.rglob("*") if p.is_file()}
    missing = [n for n in names if n not in index]
    if missing:
        print(f"Warning: {len(missing)} screenshots not found in '{PNGS_DIR}' (e.g. {missing[:3]})")
    names = [n for n in names if n in index]
    paths = {n: index[n] for n in names}
    print(f"{len(names)} screenshots with has_image == 1")

    sources = Sources()
    det, layouts = build_detections(names, paths, sources)
    print(f"{sum(1 for v in det.values() if v)} screenshots with detected images")

    if REEXPORT:
        rev = read_json(REVIEW_FILE)
        done = 0
        for n in names:
            if rev.get(n, {}).get("status") == "approved":
                save_crops(paths[n], rev[n]["boxes"], OUT_DIR, layouts.get(n), sources, SCALE)
                done += 1
        print(f"{done} approved screenshots re-cropped into '{OUT_DIR}'")
        return
    review(names, paths, OUT_DIR, det, layouts, sources, SCALE)


if __name__ == "__main__":
    main()
