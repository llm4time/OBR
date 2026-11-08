#!/usr/bin/env python3
import json
from pathlib import Path

import pymupdf
from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"

# Inputs
PDF_DIR = DATA_DIR / "raw" / "provas"
GEMINI_DIR = DATA_DIR / "interim" / "bboxes" / "gemini"

# Outputs
OUT_DIR = DATA_DIR / "processed" / "screenshots"
REVIEW_FILE = PROJECT_DIR / "annotations" / "screenshots_review.json"

for d in (OUT_DIR, REVIEW_FILE.parent):
    d.mkdir(parents=True, exist_ok=True)

SCALE = 2.0
VIEW_SCALE = 1.6
REEXPORT = False
EXAMS = None
NORM = 1000
GAP = 12
NAME = "{exam}.p{page}_q{number}.png"
LABEL_COLORS = {"question": "#1e6bff", "context": "#8e24aa", "continuation": "#ef6c00"}
DEFAULT_COLOR = "#00897b"
LABELS = ["question", "context", "continuation"]


def read_json(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_json(p, d):
    rows = [f"{json.dumps(k, ensure_ascii=False)}:{json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
            for k, v in d.items()]
    p.write_text("{\n" + ",\n".join(rows) + "\n}", encoding="utf-8")


def sort_boxes(boxes):
    return sorted(boxes, key=lambda b: (b["page"], b["bbox"][1], b["bbox"][0]))


def copy_boxes(boxes):
    return [{**b, "bbox": list(b["bbox"])} for b in boxes]


def overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return w * h / area if w > 0 and h > 0 and area > 0 else 0.0


def inherit_labels(boxes, ref):
    for b in boxes:
        best = max((r for r in ref if r["page"] == b["page"]), key=lambda r: overlap(b["bbox"], r["bbox"]), default=None)
        if best and overlap(b["bbox"], best["bbox"]) > 0.5:
            b["label"] = best["label"]
        elif not b.get("label"):
            b["label"] = "question"


def union_box(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def pieces_to_boxes(pieces):
    return [{"page": p["page"], "bbox": [v / NORM for v in p["bbox"]], "label": p.get("label")} for p in pieces]


def boxes_to_pieces(boxes):
    return [{"page": b["page"], "bbox": [round(v * NORM, 1) for v in b["bbox"]],
             **({"label": b["label"]} if b.get("label") else {})} for b in boxes]


def question_blocks(blocks, number):
    qi = next((i for i, b in enumerate(blocks) if b["type"] == "question" and b["number"] == number), None)
    if qi is None:
        return []

    def continuations(i, stop):
        out = []
        for b in blocks[i + 1:stop]:
            if b["type"] != "continuation":
                break
            out.append(b)
        return out

    ctx_of = {c: i for i, b in enumerate(blocks) if b["type"] == "context" for c in b["questions"]}
    ci = ctx_of.get(blocks[qi]["start"])
    before = [blocks[ci], *continuations(ci, qi)] if ci is not None else []
    return before + [blocks[qi]] + continuations(qi, len(blocks))


class Pdfs:
    def __init__(self):
        self.docs = {}

    def get(self, exam):
        if exam not in self.docs:
            pdf = PDF_DIR / f"{exam}.pdf"
            self.docs[exam] = pymupdf.open(pdf) if pdf.exists() else None
        return self.docs[exam]


def render(page, scale, clip=None):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def save_screenshot(doc, exam, number, boxes):
    imgs, pages = [], []
    for b in sort_boxes(boxes):
        page = doc[b["page"]]
        w, h = page.rect.width, page.rect.height
        x0, y0, x1, y1 = b["bbox"]
        rect = pymupdf.Rect(x0 * w, y0 * h, x1 * w, y1 * h)
        if rect.width < 1 or rect.height < 1:
            continue
        imgs.append(render(page, SCALE, rect))
        pages.append(b["page"])
    if not imgs:
        return None
    canvas = Image.new("RGB", (max(i.width for i in imgs), sum(i.height for i in imgs)), "white")
    y = 0
    for i in imgs:
        canvas.paste(i, (0, y))
        y += i.height
    for old in OUT_DIR.glob(f"{exam}.p*_q{number}.png"):
        old.unlink()
    out = OUT_DIR / NAME.format(exam=exam, page=min(pages) + 1, number=number)
    canvas.save(out)
    return out


def load_items(pdfs):
    paths = sorted(GEMINI_DIR.glob("*.json"))
    if EXAMS:
        paths = [p for p in paths if p.stem in EXAMS]
    items = []
    for path in paths:
        exam = path.stem
        if pdfs.get(exam) is None:
            print(f"[SKIP] {exam}: missing {PDF_DIR / (exam + '.pdf')}")
            continue
        blocks = json.loads(path.read_text(encoding="utf-8"))
        for q in (b for b in blocks if b["type"] == "question"):
            pieces = [{**pc, "label": blk["type"]} for blk in question_blocks(blocks, q["number"]) for pc in blk["pieces"]
                      if pc["bbox"][2] > pc["bbox"][0] and pc["bbox"][3] > pc["bbox"][1]]
            if not pieces:
                print(f"[WARN] {exam} q{q['number']}: no boxes detected")
            items.append({"key": f"{exam}.q{q['number']}", "exam": exam, "number": q["number"],
                          "boxes": sort_boxes(pieces_to_boxes(pieces))})
    return items


def review(items, pdfs):
    import tkinter as tk
    from tkinter import messagebox
    from PIL import ImageTk

    rev = read_json(REVIEW_FILE)

    class App:
        def __init__(self, root):
            root.title("Screenshot review")
            root.geometry("1150x850")
            frame = tk.Frame(root)
            self.cv = tk.Canvas(frame, bg="#222", highlightthickness=0)
            sb = tk.Scrollbar(frame, orient="vertical", command=self.cv.yview)
            self.cv.configure(yscrollcommand=sb.set)
            bar = tk.Frame(root)
            self.info = tk.Label(root, anchor="w", font=("Helvetica", 11))
            self.info.pack(side="bottom", fill="x")
            bar.pack(side="bottom", fill="x")
            frame.pack(fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.cv.pack(side="left", fill="both", expand=True)
            for txt, cmd in [("◀ Prev", self.prev), ("Group (G)", self.group), ("Delete (D)", self.delete),
                             ("Label (L)", self.cycle_label), ("Select all (A)", self.select_all), ("Undo (U)", self.undo),
                             ("Reset (R)", self.reset), ("Skip ▶", self.next), ("APPROVE (Enter)", self.approve)]:
                tk.Button(bar, text=txt, command=cmd, takefocus=0).pack(side="left", padx=2, pady=3)
            for key, cmd in [("<Return>", self.approve), ("g", self.group), ("d", self.delete),
                             ("<Delete>", self.delete), ("a", self.select_all), ("u", self.undo),
                             ("r", self.reset), ("l", self.cycle_label), ("1", lambda: self.set_label("question")),
                             ("2", lambda: self.set_label("context")), ("3", lambda: self.set_label("continuation")),
                             ("<Escape>", self.clear_sel), ("<Left>", self.prev),
                             ("<Right>", self.next), ("q", root.destroy)]:
                root.bind(key, lambda e, c=cmd: c())
            self.cv.bind("<ButtonPress-1>", self.press)
            self.cv.bind("<B1-Motion>", self.drag)
            self.cv.bind("<ButtonRelease-1>", self.release)
            self.cv.bind("<MouseWheel>", lambda e: self.cv.yview_scroll(-3 if e.delta > 0 else 3, "units"))
            self.cv.bind("<Button-4>", lambda e: self.cv.yview_scroll(-3, "units"))
            self.cv.bind("<Button-5>", lambda e: self.cv.yview_scroll(3, "units"))
            self.load(next((k for k, it in enumerate(items) if it["key"] not in rev), 0))

        def load(self, i):
            self.i = i
            item = items[i]
            doc = pdfs.get(item["exam"])
            self.orig = copy_boxes(item["boxes"])
            saved = rev.get(item["key"])
            start = pieces_to_boxes(saved["pieces"]) if saved else self.orig
            self.boxes = sort_boxes(copy_boxes(start))
            if not (saved and saved.get("labeled")):
                inherit_labels(self.boxes, self.orig)
            self.draw_label = "question"
            self.sel, self.hist = set(), []
            used = [b["page"] for b in self.orig + self.boxes] or [0]
            lo, hi = max(0, min(used) - 1), min(len(doc) - 1, max(used) + 1)
            self.views, y = [], 0
            for p in range(lo, hi + 1):
                img = render(doc[p], VIEW_SCALE)
                self.views.append({"page": p, "y0": y, "w": img.width, "h": img.height,
                                   "tk": ImageTk.PhotoImage(img)})
                y += img.height + GAP
            self.cv.configure(scrollregion=(0, 0, max(v["w"] for v in self.views), y))
            self.cv.delete("all")
            for v in self.views:
                self.cv.create_image(0, v["y0"], anchor="nw", image=v["tk"])
                self.cv.create_text(6, v["y0"] + 4, anchor="nw", text=f"page {v['page'] + 1}",
                                    fill="#888", font=("Helvetica", 9))
            self.overlay()
            first = next((self.view_of(b["page"]) for b in self.boxes if self.view_of(b["page"])), None)
            if first:
                top = first["y0"] + min(b["bbox"][1] for b in self.boxes if b["page"] == first["page"]) * first["h"]
                self.cv.yview_moveto(max(0, top - 40) / y)
            else:
                self.cv.yview_moveto(0)

        def view_of(self, page):
            return next((v for v in self.views if v["page"] == page), None)

        def locate(self, cx, cy):
            return next((v for v in self.views if v["y0"] <= cy < v["y0"] + v["h"] and 0 <= cx < v["w"]), None)

        def overlay(self):
            self.cv.delete("ov")
            for k, b in enumerate(self.boxes):
                v = self.view_of(b["page"])
                if v is None:
                    continue
                x0, y0, x1, y1 = b["bbox"]
                px0, py0, px1, py1 = x0 * v["w"], v["y0"] + y0 * v["h"], x1 * v["w"], v["y0"] + y1 * v["h"]
                label = b.get("label") or "question"
                color = LABEL_COLORS.get(label, DEFAULT_COLOR)
                self.cv.create_rectangle(px0, py0, px1, py1, outline="#ff2d2d" if k in self.sel else color,
                                         width=4 if k in self.sel else 2, tags="ov")
                ty = py0 - 20 if py0 >= 20 else py0
                txt = self.cv.create_text(px0 + 5, ty + 10, anchor="w", text=f"{k + 1} · {label}", fill="white",
                                          font=("Helvetica", 10, "bold"), tags="ov")
                tag = self.cv.create_rectangle(px0, ty, self.cv.bbox(txt)[2] + 5, ty + 20, fill=color,
                                               outline=color, tags="ov")
                self.cv.tag_lower(tag, txt)
            item = items[self.i]
            status = rev.get(item["key"], {}).get("status", "pending")
            self.info.config(text=f"[{self.i + 1}/{len(items)}] {item['key']} — {status} — "
                                  f"{len(self.boxes)} box(es) | selected: {len(self.sel)} | "
                                  f"new box: {self.draw_label} (1 question, 2 context, 3 continuation, L cycles)")

        def snap(self):
            self.hist.append(copy_boxes(self.boxes))

        def set_boxes(self, boxes):
            self.boxes, self.sel = sort_boxes(boxes), set()
            self.overlay()

        def press(self, e):
            cx, cy = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
            self.p0, self.v0, self.dragged = (cx, cy), self.locate(cx, cy), False

        def drag(self, e):
            cx, cy = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
            if abs(cx - self.p0[0]) + abs(cy - self.p0[1]) > 6:
                self.cv.delete("tmp")
                self.cv.create_rectangle(*self.p0, cx, cy, outline="#00c853", width=2, dash=(4, 3), tags="tmp")
                self.dragged = True

        def release(self, e):
            self.cv.delete("tmp")
            cx, cy = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
            v = self.v0
            if v is None:
                return
            if self.dragged:
                xa, xb = sorted((self.p0[0], cx))
                ya, yb = sorted((self.p0[1], cy))
                x0, x1 = (min(max(x / v["w"], 0.0), 1.0) for x in (xa, xb))
                y0, y1 = (min(max((y - v["y0"]) / v["h"], 0.0), 1.0) for y in (ya, yb))
                if x1 - x0 > 0.005 and y1 - y0 > 0.005:
                    self.snap()
                    self.set_boxes(self.boxes + [{"page": v["page"], "bbox": [x0, y0, x1, y1], "label": self.draw_label}])
                return
            nx, ny = cx / v["w"], (cy - v["y0"]) / v["h"]
            hit = [k for k, b in enumerate(self.boxes)
                   if b["page"] == v["page"] and b["bbox"][0] <= nx <= b["bbox"][2] and b["bbox"][1] <= ny <= b["bbox"][3]]
            if hit:
                area = lambda k: (self.boxes[k]["bbox"][2] - self.boxes[k]["bbox"][0]) * \
                                 (self.boxes[k]["bbox"][3] - self.boxes[k]["bbox"][1])
                self.sel ^= {min(hit, key=area)}
                self.overlay()

        def group(self):
            chosen = [self.boxes[k] for k in sorted(self.sel)]
            pages = sorted({b["page"] for b in chosen})
            if len(chosen) < 2 or len(pages) == len(chosen):
                return
            merged = []
            for p in pages:
                same = [b for b in chosen if b["page"] == p]
                merged.append({"page": p, "bbox": union_box([b["bbox"] for b in same]), "label": same[0].get("label") or "question"})
            self.snap()
            self.set_boxes([b for k, b in enumerate(self.boxes) if k not in self.sel] + merged)

        def set_label(self, label):
            self.draw_label = label
            if self.sel:
                self.snap()
                for k in self.sel:
                    self.boxes[k]["label"] = label
            self.overlay()

        def cycle_label(self):
            cur = self.boxes[min(self.sel)].get("label") if self.sel else self.draw_label
            self.set_label(LABELS[(LABELS.index(cur) + 1) % len(LABELS)] if cur in LABELS else LABELS[0])

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
            self.set_boxes(copy_boxes(self.orig))

        def approve(self):
            item = items[self.i]
            if not self.boxes:
                messagebox.showwarning("No boxes", "Draw at least one box before approving.")
                return
            out = save_screenshot(pdfs.get(item["exam"]), item["exam"], item["number"], self.boxes)
            if out is None:
                messagebox.showwarning("Empty crop", "The boxes are too small to render.")
                return
            rev[item["key"]] = {"status": "approved", "labeled": True, "pieces": boxes_to_pieces(self.boxes)}
            write_json(REVIEW_FILE, rev)
            self.next()

        def next(self):
            if self.i + 1 < len(items):
                self.load(self.i + 1)
            else:
                self.overlay()
                messagebox.showinfo("Done", f"Last question. Approved: {len(rev)}/{len(items)}.")

        def prev(self):
            if self.i > 0:
                self.load(self.i - 1)

    root = tk.Tk()
    App(root)
    root.mainloop()


def main():
    pdfs = Pdfs()
    items = load_items(pdfs)
    print(f"{len(items)} questions")

    if REEXPORT:
        rev = read_json(REVIEW_FILE)
        done = 0
        for it in items:
            r = rev.get(it["key"])
            if r and r.get("status") == "approved":
                if save_screenshot(pdfs.get(it["exam"]), it["exam"], it["number"], pieces_to_boxes(r["pieces"])):
                    done += 1
        print(f"{done} approved questions re-cropped into '{OUT_DIR}'")
        return
    review(items, pdfs)


if __name__ == "__main__":
    main()
