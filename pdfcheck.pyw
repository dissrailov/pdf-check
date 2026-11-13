#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF Check: проверка макетов перед печатью по размерам из ТЗ.

  - сверяет размер каждой страницы PDF с размерами из ТЗ (см / мм),
    сам определяет масштаб макета (1:1, 1:2, 1:10 ...);
  - считает эффективное разрешение каждой картинки в натуральном размере;
  - проверяет, встроены ли шрифты, и ищет RGB;
  - показывает, каких позиций из ТЗ не хватает.

Зависимости: PyMuPDF, customtkinter; tkinterdnd2 (перетаскивание файлов, по желанию).
"""
import json
import math
import os
import queue
import re
import sys
import threading

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

APP_NAME = "PDF Check"
PT_TO_MM = 25.4 / 72.0
INFO, OK, WARN, FAIL = -1, 0, 1, 2
STATUS_TEXT = {OK: "Готов", WARN: "Проверить", FAIL: "Ошибка"}
TAG = {INFO: "ИНФО", OK: "OK", WARN: "ВНИМАНИЕ", FAIL: "ПЛОХО"}
ICON = {INFO: "•", OK: "✓", WARN: "!", FAIL: "✕"}
AUTO_SCALES = [1, 2, 4, 5, 10, 20, 25, 50, 100]
UNIT_MM = {"мм": 1.0, "mm": 1.0, "см": 10.0, "cm": 10.0, "м": 1000.0, "m": 1000.0}

SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:(мм|см|м|mm|cm|m)(?![а-яёa-z]))?\s*[xх×*]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:(мм|см|м|mm|cm|m)(?![а-яёa-z]))?", re.I)
COUNT_RE = re.compile(r"(\d+)\s*шт", re.I)
RGB_OP = re.compile(rb"(?<![\w/.])(?:-?\d*\.?\d+\s+){3}(?:rg|RG)(?![\w])")
# похожие буквы: «З1» и «31», кириллическая и латинская «А» считаются одним и тем же
LOOKALIKE = str.maketrans("авекмнорстухз", "abekmhopctyx3")


# ----------------------------------------------------------------------------
# ТЗ
# ----------------------------------------------------------------------------
class Spec:
    def __init__(self, name, w, h, count):
        self.name, self.w, self.h, self.count = name, w, h, count
        self.tokens = tokenize(name)
        self.used = 0


def tokenize(text):
    text = text.lower().translate(LOOKALIKE)
    return [t for t in re.split(r"[^0-9a-zа-яё]+", text) if t]


def parse_specs(text, default_unit):
    """Строки вида 'Л1 верх 110x110', '110 × 218 см', 'П2 1100х2180 мм 2 шт'."""
    specs, bad = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = SIZE_RE.search(line)
        if not m:
            bad.append(line)
            continue
        unit = (m.group(4) or m.group(2) or default_unit).lower()
        k = UNIT_MM.get(unit, UNIT_MM[default_unit])
        w = float(m.group(1).replace(",", ".")) * k
        h = float(m.group(3).replace(",", ".")) * k
        rest = line[m.end():]
        cm = COUNT_RE.search(rest)
        count = max(1, int(cm.group(1))) if cm else 1
        name = line[:m.start()].strip(" \t:;,-–—=")
        if not name:
            name = COUNT_RE.sub("", rest).strip(" \t:;,-–—=")
        specs.append(Spec(name, w, h, count))
    return specs, bad


def fmt_num(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


def fmt_size(w_mm, h_mm, unit):
    k = UNIT_MM[unit]
    return "%s × %s %s" % (fmt_num(w_mm / k), fmt_num(h_mm / k), unit)


def spec_label(spec, unit):
    size = fmt_size(spec.w, spec.h, unit)
    return "%s (%s)" % (spec.name, size) if spec.name else size


# ----------------------------------------------------------------------------
# Разбор PDF (не зависит от настроек)
# ----------------------------------------------------------------------------
def cs_label(info):
    name = (info.get("cs-name") or "").lower()
    n = info.get("colorspace", 0)
    if "cmyk" in name or n == 4:
        return "CMYK"
    if "rgb" in name or n == 3:
        return "RGB"
    if "gray" in name or n == 1:
        return "Gray"
    return info.get("cs-name") or "?"


def vector_has_rgb(doc, page):
    chunks = []
    try:
        chunks.append(page.read_contents())
        for xo in page.get_xobjects():
            try:
                data = doc.xref_stream(xo[0])
                if data:
                    chunks.append(data)
            except Exception:
                pass
    except Exception:
        pass
    return bool(RGB_OP.search(b"\n".join(chunks)))


def analyze_pdf(path):
    res = {"path": path, "name": os.path.basename(path), "error": None, "pages": []}
    try:
        doc = fitz.open(path)
        if doc.needs_pass:
            res["error"] = "файл защищён паролем"
            return res
        for pno in range(doc.page_count):
            page = doc[pno]
            media, trim = page.mediabox, page.trimbox
            w, h = trim.width, trim.height
            bleed = max(media.width - trim.width, media.height - trim.height) / 2.0
            if page.rotation in (90, 270):
                w, h = h, w
            images = []
            for info in page.get_image_info(xrefs=True):
                pw, ph = info.get("width", 0), info.get("height", 0)
                if pw <= 2 and ph <= 2:
                    continue
                a, b, c, d = info["transform"][:4]
                dw, dh = math.hypot(a, b), math.hypot(c, d)
                if dw <= 0 or dh <= 0:
                    continue
                images.append({"px_w": pw, "px_h": ph, "w_mm": dw * PT_TO_MM,
                               "h_mm": dh * PT_TO_MM, "cs": cs_label(info)})
            missing = sorted({f[3] for f in doc.get_page_fonts(pno, full=True)
                              if f[1] == "n/a" and f[2] != "Type3"})
            has_text = bool(page.get_text("text").strip())
            res["pages"].append({
                "pno": pno + 1, "w_mm": w * PT_TO_MM, "h_mm": h * PT_TO_MM,
                "bleed_mm": bleed * PT_TO_MM if bleed > 0.5 else 0.0,
                "images": images, "missing_fonts": missing, "has_text": has_text,
                "vec_rgb": vector_has_rgb(doc, page),
            })
        doc.close()
    except Exception as e:
        res["error"] = str(e)
    return res


# ----------------------------------------------------------------------------
# Оценка по настройкам и ТЗ
# ----------------------------------------------------------------------------
def needed_ppi(distance_m):
    return 3438.0 / (max(distance_m, 0.05) * 100.0 / 2.54)


def match_size(pw, ph, spec, tol, allow, scales):
    for s in scales:
        t = tol + 0.2 * (s - 1)
        W, H = pw * s, ph * s
        variants = [("ok", spec.w, spec.h)]
        if allow > 0:
            variants.append(("allow", spec.w + 2 * allow, spec.h + 2 * allow))
        variants.append(("rot", spec.h, spec.w))
        if allow > 0:
            variants.append(("rot", spec.h + 2 * allow, spec.w + 2 * allow))
        for kind, sw, sh in variants:
            if abs(W - sw) <= t and abs(H - sh) <= t:
                return s, kind
    return None


def evaluate(docs, specs, st):
    """Возвращает список результатов (по одному на страницу) и сводку."""
    unit = st["unit"]
    target = st["min_ppi"] if st["min_ppi"] else needed_ppi(st["distance"])
    soft = target * 0.7
    scales = AUTO_SCALES if st["scale"] is None else [st["scale"]]
    default_scale = 1.0 if st["scale"] is None else st["scale"]
    for sp in specs:
        sp.used = 0

    rows = []
    for d in docs:
        if d["error"]:
            rows.append({"doc": d, "page": None, "status": FAIL, "title": d["name"], "meta": [],
                         "checks": [(FAIL, "Файл", "не удалось открыть: %s" % d["error"], [])]})
            continue
        ftokens = set(tokenize(os.path.splitext(d["name"])[0]))
        single = len(d["pages"]) == 1
        for p in d["pages"]:
            rows.append({"doc": d, "page": p, "ftokens": ftokens if single else set(),
                         "title": d["name"] if single else "%s, стр. %d" % (d["name"], p["pno"])})

    # 1) привязка к ТЗ по имени файла, 2) по размеру
    page_rows = [r for r in rows if r.get("page")]
    for r in page_rows:
        r["spec"], r["match"] = None, None
        best = None
        for sp in specs:
            if sp.tokens and all(t in r["ftokens"] for t in sp.tokens):
                if best is None or len(sp.tokens) > len(best.tokens):
                    best = sp
        if best:
            r["spec"], r["by_name"] = best, True
            r["match"] = match_size(r["page"]["w_mm"], r["page"]["h_mm"], best,
                                    st["tol"], st["allow"], scales)
            best.used += 1
    for r in page_rows:
        if r["spec"] is not None:
            continue
        r["by_name"] = False
        cands = []
        for i, sp in enumerate(specs):
            m = match_size(r["page"]["w_mm"], r["page"]["h_mm"], sp, st["tol"], st["allow"], scales)
            if m:
                cands.append((m[1] == "rot", sp.used >= sp.count, m[0], i, sp, m))
        if cands:
            cands.sort(key=lambda c: c[:4])
            r["spec"], r["match"] = cands[0][4], cands[0][5]
            r["spec"].used += 1

    for r in page_rows:
        p, sp, m = r["page"], r["spec"], r["match"]
        checks = []
        scale = m[0] if m else default_scale
        rw, rh = p["w_mm"] * scale, p["h_mm"] * scale
        real = fmt_size(rw, rh, unit)
        meta = [real]

        # размер
        if not specs:
            checks.append((INFO, "Размер", "%s. ТЗ не задано, размер не сверялся." % real, []))
        elif sp is None:
            near = min(specs, key=lambda s: abs(s.w - rw) + abs(s.h - rh))
            checks.append((FAIL, "Размер", "%s не совпадает ни с одной позицией ТЗ. Ближайшая: %s."
                           % (real, spec_label(near, unit)), []))
            meta.append("нет в ТЗ")
        elif m is None:
            checks.append((FAIL, "Размер", "по ТЗ «%s» должен быть %s, в макете %s."
                           % (sp.name, fmt_size(sp.w, sp.h, unit), real), []))
            meta.append(sp.name or "ТЗ")
        else:
            how = "по имени файла" if r["by_name"] else "по размеру"
            if m[1] == "rot":
                checks.append((WARN, "Размер", "совпадает с %s, но макет повёрнут на 90°."
                               % spec_label(sp, unit), []))
            elif m[1] == "allow":
                checks.append((OK, "Размер", "%s + припуск %s мм на сторону (%s)."
                               % (spec_label(sp, unit), fmt_num(st["allow"]), how), []))
            else:
                checks.append((OK, "Размер", "совпадает с %s (%s)." % (spec_label(sp, unit), how), []))
            meta.append("ТЗ: " + (sp.name or fmt_size(sp.w, sp.h, unit)))
        if scale != 1:
            meta.append("масштаб 1:%g" % scale)

        # картинки
        imgs = []
        for im in p["images"]:
            ppi = min(im["px_w"] / (im["w_mm"] * scale / 25.4), im["px_h"] / (im["h_mm"] * scale / 25.4))
            s = OK if ppi >= target else (WARN if ppi >= soft else FAIL)
            imgs.append((ppi, s, im))
        imgs.sort(key=lambda x: x[0])
        if not imgs:
            checks.append((OK, "Разрешение", "растра нет, всё в векторе.", []))
            meta.append("вектор")
        else:
            worst = max(i[1] for i in imgs)
            sub = ["%-8s %4.0f ppi   %d × %d px   на баннере %s   %s"
                   % (TAG[s], ppi, im["px_w"], im["px_h"],
                      fmt_size(im["w_mm"] * scale, im["h_mm"] * scale, unit), im["cs"])
                   for ppi, s, im in imgs[:20]]
            if len(imgs) > 20:
                sub.append("... и ещё %d картинок с разрешением выше" % (len(imgs) - 20))
            big = max(p["images"], key=lambda i: i["w_mm"] * i["h_mm"])
            if big["w_mm"] * big["h_mm"] >= 0.9 * p["w_mm"] * p["h_mm"] and not p["has_text"]:
                sub.append("Страница целиком растровая: если на ней есть текст, он тоже картинкой.")
            text = "минимум в макете %.0f ppi, нужно от %.0f ppi." % (imgs[0][0], target)
            if worst == FAIL:
                text += " Будут видны пиксели."
            elif worst == WARN:
                text += " Вблизи будет мягковато."
            checks.append((worst, "Разрешение", text, sub))
            meta.append("мин. %.0f ppi" % imgs[0][0])

        # шрифты
        if p["missing_fonts"]:
            checks.append((FAIL, "Шрифты", "не встроены: %s. Встройте или переведите в кривые."
                           % ", ".join(p["missing_fonts"]), []))
        else:
            checks.append((OK, "Шрифты", "встроены или в кривых.", []))

        # цвет
        rgb_imgs = sum(1 for im in p["images"] if im["cs"] == "RGB")
        if rgb_imgs or p["vec_rgb"]:
            parts = []
            if rgb_imgs:
                parts.append("картинок в RGB: %d" % rgb_imgs)
            if p["vec_rgb"]:
                parts.append("RGB в векторе или тексте")
            checks.append((WARN if st["rgb_warn"] else INFO, "Цвет",
                           "%s. В CMYK-печати яркие цвета могут потускнеть." % "; ".join(parts), []))
        else:
            checks.append((OK, "Цвет", "RGB не найден.", []))

        if p["bleed_mm"]:
            checks.append((INFO, "Вылеты", "размечены, около %s мм на сторону (в размер не входят)."
                           % fmt_num(p["bleed_mm"] * scale), []))

        r.update(status=max(max(c[0] for c in checks), OK), scale=scale, real=(rw, rh),
                 checks=checks, meta=meta)

    missing = []
    total = sum(sp.count for sp in specs)
    covered = sum(min(sp.used, sp.count) for sp in specs)
    for sp in specs:
        if sp.used < sp.count:
            n = sp.count - sp.used
            missing.append(spec_label(sp, unit) + (" ×%d" % n if n > 1 else ""))
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in (OK, WARN, FAIL)}
    return rows, {"counts": counts, "missing": missing, "target": target,
                  "spec_total": total, "spec_covered": covered}


def build_report(rows, summary, bad_lines=()):
    c = summary["counts"]
    out = ["ПРОВЕРКА МАКЕТОВ ПЕРЕД ПЕЧАТЬЮ",
           "Готово: %d | проверить: %d | с ошибками: %d | порог разрешения: %.0f ppi"
           % (c[OK], c[WARN], c[FAIL], summary["target"])]
    if summary["missing"]:
        out.append("Нет макетов для позиций ТЗ: " + "; ".join(summary["missing"]))
    if bad_lines:
        out.append("Не разобраны строки ТЗ: " + "; ".join(bad_lines))
    for r in rows:
        out.append("")
        out.append("=== %s  [%s] ===" % (r["title"], STATUS_TEXT[r["status"]].upper()))
        if r["meta"]:
            out.append("  ·  ".join(r["meta"]))
        for level, label, text, sub in r["checks"]:
            out.append("[%s] %s: %s" % (TAG[level], label, text))
            out.extend("      " + s for s in sub)
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Интерфейс
# ----------------------------------------------------------------------------
BG = ("#f1f3f7", "#12151c")
CARD = ("#ffffff", "#1b202b")
SOFT = ("#f6f7fa", "#232936")
BORDER = ("#e2e6ee", "#2b3242")
TEXT = ("#131722", "#e9ecf2")
MUTED = ("#667085", "#98a2b3")
ACCENT = ("#2f5bea", "#4c7dff")
ACCENT_HOVER = ("#2448c4", "#3a68e6")
SEG = ("#e4e7ee", "#262c3a")
SEG_SEL = ("#ffffff", "#4a5878")
S_FG = {INFO: MUTED, OK: ("#15803d", "#4ade80"), WARN: ("#b45309", "#fbbf24"), FAIL: ("#c22525", "#f87171")}
S_BG = {OK: ("#dcfce7", "#15301f"), WARN: ("#fef3c7", "#3a2e10"), FAIL: ("#fee2e2", "#3f1a1a")}
S_BAR = {OK: ("#22c55e", "#22c55e"), WARN: ("#f59e0b", "#f59e0b"), FAIL: ("#ef4444", "#ef4444")}
DISTANCES = ["0,5 м", "1 м", "2 м", "3 м", "5 м"]


def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "pdfcheck_settings.json")


def to_float(text, default=None):
    try:
        v = float(str(text).strip().replace(",", ".").replace("м", ""))
        return v if v > 0 else default
    except ValueError:
        return default


def fatal(message):
    try:
        import tkinter
        from tkinter import messagebox
        r = tkinter.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, message)
    except Exception:
        print(message)


def run_gui(initial_files):
    if fitz is None:
        fatal("Не установлена библиотека PyMuPDF.\n\nЗапустите start.bat или выполните:\n  pip install pymupdf")
        return None
    try:
        import customtkinter as ctk
    except ImportError:
        fatal("Не установлена библиотека customtkinter.\n\nЗапустите start.bat или выполните:\n"
              "  pip install customtkinter")
        return None
    import tkinter as tk
    from tkinter import filedialog

    saved = {}
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            saved = json.load(fh)
    except Exception:
        pass

    ctk.set_appearance_mode(saved.get("theme", "light"))
    dnd = None
    try:
        import tkinterdnd2

        class Root(ctk.CTk, tkinterdnd2.TkinterDnD.DnDWrapper):
            def __init__(self):
                super().__init__()
                self.TkdndVersion = tkinterdnd2.TkinterDnD._require(self)
        root = Root()
        dnd = tkinterdnd2
    except Exception:
        root = ctk.CTk()
    root.title(APP_NAME + ": проверка макетов перед печатью")
    root.geometry("1280x800")
    root.minsize(1040, 640)
    root.configure(fg_color=BG)

    family = "Segoe UI" if sys.platform == "win32" else None
    mono = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"

    def F(size, weight="normal"):
        return ctk.CTkFont(family=family, size=size, weight=weight)

    v_unit = tk.StringVar(value=saved.get("unit", "см"))
    v_dist = tk.StringVar(value=saved.get("distance", "1 м") if saved.get("distance") in DISTANCES else "1 м")
    v_ppi = tk.StringVar(value=saved.get("min_ppi", ""))
    v_scale = tk.StringVar(value=saved.get("scale", "Авто"))
    v_tol = tk.StringVar(value=saved.get("tol", "2"))
    v_allow = tk.StringVar(value=saved.get("allow", "0"))
    v_rgb = tk.BooleanVar(value=saved.get("rgb_warn", True))
    v_filter = tk.StringVar(value="Все")

    state = {"docs": [], "rows": [], "summary": None, "bad": [], "job": None, "busy": 0,
             "open": set(), "adv": bool(saved.get("adv", False)), "wrap": [], "filters": {}}
    q = queue.Queue()

    def button(parent, text, command, primary=False, width=0, **kw):
        if primary:
            return ctk.CTkButton(parent, text=text, command=command, width=width, height=36,
                                 corner_radius=9, font=F(13, "bold"), fg_color=ACCENT,
                                 hover_color=ACCENT_HOVER, text_color="#ffffff", **kw)
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=36,
                             corner_radius=9, font=F(13), fg_color=CARD, hover_color=SOFT,
                             text_color=TEXT, border_width=1, border_color=BORDER, **kw)

    def card(parent, **kw):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1,
                            border_color=BORDER, **kw)

    # === боковая панель ======================================================
    side = card(root, width=350)
    side.pack(side="left", fill="y", padx=(16, 0), pady=16)
    side.pack_propagate(False)

    brand = ctk.CTkFrame(side, fg_color="transparent")
    brand.pack(fill="x", padx=18, pady=(16, 4))
    ctk.CTkLabel(brand, text=APP_NAME, font=F(20, "bold"), text_color=TEXT).pack(side="left")
    theme_btn = ctk.CTkButton(brand, text="", width=92, height=28, corner_radius=8, font=F(12),
                              fg_color=SOFT, hover_color=BORDER, text_color=MUTED)
    theme_btn.pack(side="right")

    ctk.CTkLabel(side, text="Размеры из ТЗ", font=F(14, "bold"), text_color=TEXT).pack(
        anchor="w", padx=18, pady=(12, 0))
    ctk.CTkLabel(side, text="Одна позиция на строку, например:  Л1 верх 110x110",
                 font=F(12), text_color=MUTED).pack(anchor="w", padx=18)

    # нижняя часть панели пакуется первой, чтобы поле ТЗ занимало всё остальное
    bottom = ctk.CTkFrame(side, fg_color="transparent")
    bottom.pack(side="bottom", fill="x", padx=18, pady=(0, 16))

    tz = ctk.CTkTextbox(side, font=ctk.CTkFont(family=mono, size=13), wrap="none", corner_radius=10,
                        fg_color=SOFT, border_width=1, border_color=BORDER, text_color=TEXT)
    tz.pack(fill="both", expand=True, padx=18, pady=(8, 8))
    tz.insert("1.0", saved.get("tz", ""))

    tz_info = ctk.CTkLabel(bottom, text="", font=F(12), text_color=MUTED, justify="left",
                           anchor="w", wraplength=310)
    tz_info.pack(fill="x")

    urow = ctk.CTkFrame(bottom, fg_color="transparent")
    urow.pack(fill="x", pady=(8, 0))
    ctk.CTkLabel(urow, text="Единицы в ТЗ", font=F(13), text_color=TEXT).pack(side="left")
    ctk.CTkSegmentedButton(urow, values=["см", "мм"], variable=v_unit, font=F(13), height=30,
                           fg_color=SEG, unselected_color=SEG,
                           unselected_hover_color=BORDER, selected_color=SEG_SEL,
                           selected_hover_color=SEG_SEL, text_color=TEXT).pack(side="right")

    ctk.CTkLabel(bottom, text="С какого расстояния смотрят", font=F(13), text_color=TEXT).pack(
        anchor="w", pady=(14, 4))
    ctk.CTkSegmentedButton(bottom, values=DISTANCES, variable=v_dist, font=F(13), height=32,
                           fg_color=SEG, unselected_color=SEG,
                           unselected_hover_color=BORDER, selected_color=SEG_SEL,
                           selected_hover_color=SEG_SEL, text_color=TEXT).pack(fill="x")
    ppi_info = ctk.CTkLabel(bottom, text="", font=F(12), text_color=MUTED, anchor="w")
    ppi_info.pack(fill="x", pady=(4, 0))

    adv_btn = ctk.CTkButton(bottom, text="", anchor="w", height=30, corner_radius=8, font=F(13),
                            fg_color="transparent", hover_color=SOFT, text_color=ACCENT)
    adv_btn.pack(fill="x", pady=(10, 0))
    adv = ctk.CTkFrame(bottom, fg_color=SOFT, corner_radius=10)

    def adv_row(row, label, widget_factory, hint):
        ctk.CTkLabel(adv, text=label, font=F(12), text_color=TEXT).grid(
            row=row, column=0, sticky="w", padx=(12, 8), pady=4)
        widget_factory().grid(row=row, column=1, sticky="w", pady=4)
        ctk.CTkLabel(adv, text=hint, font=F(12), text_color=MUTED).grid(
            row=row, column=2, sticky="w", padx=(6, 10))

    def entry(var):
        return lambda: ctk.CTkEntry(adv, textvariable=var, width=70, height=28, corner_radius=7,
                                    font=F(12), border_color=BORDER, fg_color=CARD)

    adv_row(0, "Свой порог", entry(v_ppi), "ppi")
    adv_row(1, "Масштаб 1:", lambda: ctk.CTkComboBox(
        adv, variable=v_scale, width=70, height=28, corner_radius=7, font=F(12),
        values=["Авто", "1", "2", "5", "10", "20", "100"], border_color=BORDER, fg_color=CARD,
        button_color=BORDER, command=lambda _v: schedule()), "")
    adv_row(2, "Допуск размера", entry(v_tol), "мм")
    adv_row(3, "Припуск на сторону", entry(v_allow), "мм")
    ctk.CTkSwitch(adv, text="RGB считать предупреждением", variable=v_rgb, font=F(12),
                  progress_color=ACCENT, text_color=TEXT).grid(
        row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(6, 10))

    def render_adv():
        adv_btn.configure(text=("▾  " if state["adv"] else "▸  ") + "Дополнительные настройки")
        if state["adv"]:
            adv.pack(fill="x", pady=(4, 0))
        else:
            adv.pack_forget()

    def toggle_adv():
        state["adv"] = not state["adv"]
        render_adv()
        save_settings()

    adv_btn.configure(command=toggle_adv)

    # === основная область ====================================================
    main = ctk.CTkFrame(root, fg_color="transparent")
    main.pack(side="left", fill="both", expand=True, padx=16, pady=16)

    top = ctk.CTkFrame(main, fg_color="transparent")
    top.pack(fill="x")
    title = ctk.CTkLabel(top, text="Макеты", font=F(22, "bold"), text_color=TEXT, anchor="w")
    title.pack(side="left")
    button(top, "Сохранить отчёт", lambda: save_report()).pack(side="right")
    button(top, "Копировать отчёт", lambda: copy_report()).pack(side="right", padx=8)

    tools = ctk.CTkFrame(main, fg_color="transparent")
    tools.pack(fill="x", pady=(12, 0))
    button(tools, "+  Добавить PDF", lambda: add_files(), primary=True).pack(side="left")
    button(tools, "Папка", lambda: add_folder()).pack(side="left", padx=8)
    button(tools, "Перечитать", lambda: recheck()).pack(side="left")
    button(tools, "Очистить", lambda: clear()).pack(side="left", padx=8)
    filt = ctk.CTkSegmentedButton(tools, values=["Все"], variable=v_filter, font=F(13), height=34,
                                  fg_color=SEG, unselected_color=SEG,
                           unselected_hover_color=BORDER, selected_color=SEG_SEL,
                           selected_hover_color=SEG_SEL, text_color=TEXT,
                                  command=lambda _v: render())
    filt.pack(side="right")

    banner = ctk.CTkFrame(main, fg_color=S_BG[WARN], corner_radius=10)
    banner_text = ctk.CTkLabel(banner, text="", font=F(13), text_color=S_FG[WARN], justify="left",
                               anchor="w", wraplength=780)
    banner_text.pack(fill="x", padx=14, pady=10)

    body = ctk.CTkFrame(main, fg_color="transparent")
    body.pack(fill="both", expand=True, pady=(12, 0))

    # пустое состояние
    empty = ctk.CTkFrame(body, fg_color=CARD, corner_radius=16, border_width=2, border_color=BORDER)
    empty_in = ctk.CTkFrame(empty, fg_color="transparent")
    empty_in.place(relx=0.5, rely=0.5, anchor="center")
    ctk.CTkLabel(empty_in, text="PDF", font=F(30, "bold"), text_color=ACCENT, fg_color=SOFT,
                 corner_radius=18, width=96, height=96).pack()
    empty_title = ctk.CTkLabel(empty_in, text="", font=F(18, "bold"), text_color=TEXT)
    empty_title.pack(pady=(18, 4))
    empty_hint = ctk.CTkLabel(empty_in, text="", font=F(13), text_color=MUTED, justify="center")
    empty_hint.pack()
    button(empty_in, "Выбрать файлы", lambda: add_files(), primary=True, width=180).pack(pady=(18, 0))

    lst = ctk.CTkScrollableFrame(body, fg_color="transparent", corner_radius=0)

    # === логика ==============================================================
    def current_settings():
        scale = None if v_scale.get().strip().lower() in ("", "авто", "auto") else to_float(v_scale.get())
        tol = 0.0 if v_tol.get().strip() in ("0", "0.0", "0,0") else to_float(v_tol.get(), 2.0)
        return {"unit": v_unit.get(), "distance": to_float(v_dist.get(), 1.0),
                "min_ppi": to_float(v_ppi.get()), "scale": scale, "tol": tol,
                "allow": to_float(v_allow.get(), 0.0) or 0.0, "rgb_warn": v_rgb.get()}

    def save_settings():
        data = {"unit": v_unit.get(), "distance": v_dist.get(), "min_ppi": v_ppi.get(),
                "scale": v_scale.get(), "tol": v_tol.get(), "allow": v_allow.get(),
                "rgb_warn": v_rgb.get(), "tz": tz.get("1.0", "end").strip(),
                "theme": ctk.get_appearance_mode().lower(), "adv": state["adv"]}
        try:
            with open(settings_path(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def render_theme():
        theme_btn.configure(text="Тёмная тема" if ctk.get_appearance_mode() == "Light" else "Светлая тема")

    def toggle_theme():
        ctk.set_appearance_mode("dark" if ctk.get_appearance_mode() == "Light" else "light")
        render_theme()
        save_settings()

    theme_btn.configure(command=toggle_theme)

    def clickable(widget, callback):
        widget.bind("<Button-1>", callback, add="+")
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass
        for child in widget.winfo_children():
            if not isinstance(child, ctk.CTkButton):
                clickable(child, callback)

    def row_key(r):
        return (r["doc"]["path"], r["page"]["pno"] if r.get("page") else 0)

    def make_card(r):
        key = row_key(r)
        is_open = key in state["open"]
        status = r["status"]
        box = card(lst)
        box.pack(fill="x", pady=(0, 10), padx=(0, 6))
        ctk.CTkFrame(box, width=5, height=10, fg_color=S_BAR[status], corner_radius=3).pack(
            side="left", fill="y", padx=(8, 0), pady=10)
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=14, pady=12)

        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=STATUS_TEXT[status], font=F(12, "bold"), text_color=S_FG[status],
                     fg_color=S_BG[status], corner_radius=8, width=92, height=26).pack(side="left")
        ctk.CTkLabel(head, text=r["title"], font=F(15, "bold"), text_color=TEXT, anchor="w").pack(
            side="left", padx=12)
        ctk.CTkButton(head, text="✕", width=30, height=28, corner_radius=8, font=F(13),
                      fg_color="transparent", hover_color=SOFT, text_color=MUTED,
                      command=lambda: remove_doc(r["doc"])).pack(side="right")
        ctk.CTkLabel(head, text="Скрыть ▴" if is_open else "Подробнее ▾", font=F(12),
                     text_color=MUTED).pack(side="right", padx=8)

        if r["meta"]:
            ctk.CTkLabel(inner, text="   ·   ".join(r["meta"]), font=F(13), text_color=MUTED,
                         anchor="w").pack(fill="x", pady=(8, 0))

        shown = r["checks"] if is_open else [c for c in r["checks"] if c[0] >= WARN]
        if shown:
            ctk.CTkFrame(inner, height=1, fg_color=BORDER).pack(fill="x", pady=(10, 4))
        for level, label, text, sub in shown:
            line = ctk.CTkFrame(inner, fg_color="transparent")
            line.pack(fill="x", pady=(4, 0))
            ctk.CTkLabel(line, text=ICON[level], width=20, font=F(13, "bold"),
                         text_color=S_FG[level]).pack(side="left", anchor="n")
            lab = ctk.CTkLabel(line, text="%s: %s" % (label, text), font=F(13), justify="left",
                               anchor="w", text_color=TEXT if level >= WARN or is_open else MUTED,
                               wraplength=state.get("wrap_px", 700))
            lab.pack(side="left", fill="x", expand=True, padx=(6, 0))
            state["wrap"].append(lab)
            if is_open and sub:
                ctk.CTkLabel(inner, text="\n".join(sub), font=ctk.CTkFont(family=mono, size=12),
                             text_color=MUTED, justify="left", anchor="w", fg_color=SOFT,
                             corner_radius=8).pack(fill="x", padx=(26, 0), pady=(6, 2), ipadx=8, ipady=6)

        def toggle(_e=None):
            state["open"].symmetric_difference_update({key})
            render()
        clickable(box, toggle)

    def render():
        rows, summary = state["rows"], state["summary"]
        for w in lst.winfo_children():
            w.destroy()
        state["wrap"] = []
        if not rows:
            lst.pack_forget()
            banner.pack_forget()
            empty.pack(fill="both", expand=True)
            if state["busy"] > 0:
                empty_title.configure(text="Читаю файлы...")
                empty_hint.configure(text="Осталось: %d" % state["busy"])
            else:
                empty_title.configure(text="Перетащите PDF или папку сюда" if dnd else "Добавьте PDF-файлы")
                empty_hint.configure(text="Программа сверит размеры с ТЗ и проверит\n"
                                          "разрешение, шрифты и цвет")
            title.configure(text="Макеты")
            filt.pack_forget()
            state["filters"] = {}
            return
        empty.pack_forget()
        c = summary["counts"]
        title.configure(text="Макеты: %d" % len(rows))
        names = {"Все %d" % len(rows): None, "Ошибки %d" % c[FAIL]: FAIL,
                 "Проверить %d" % c[WARN]: WARN, "Готовы %d" % c[OK]: OK}
        current = state["filters"].get(v_filter.get())
        state["filters"] = names
        filt.configure(values=list(names))
        filt.pack(side="right")
        v_filter.set(next(k for k, v in names.items() if v == current))

        notes = []
        if summary["missing"]:
            notes.append("Нет макетов для позиций ТЗ: " + "; ".join(summary["missing"]))
        if state["bad"]:
            notes.append("Не понял строки ТЗ (нет размера): " + "; ".join(state["bad"]))
        if notes:
            banner_text.configure(text="\n".join(notes))
            banner.pack(fill="x", pady=(12, 0), before=body)
        else:
            banner.pack_forget()

        lst.pack(fill="both", expand=True)
        visible = [r for r in rows if current is None or r["status"] == current]
        visible.sort(key=lambda r: -r["status"])
        for r in visible:
            make_card(r)
        if not visible:
            ctk.CTkLabel(lst, text="В этой категории пусто", font=F(13), text_color=MUTED).pack(pady=40)

    def refresh(*_):
        state["job"] = None
        st = current_settings()
        specs, bad = parse_specs(tz.get("1.0", "end"), st["unit"])
        rows, summary = evaluate(state["docs"], specs, st)
        state.update(rows=rows, summary=summary, bad=bad)
        if st["min_ppi"]:
            ppi_info.configure(text="Задан свой порог: от %.0f ppi" % summary["target"])
        else:
            ppi_info.configure(text="Нужно разрешение от %.0f ppi" % summary["target"])
        if not specs:
            tz_info.configure(text="Без ТЗ размер не сверяется: проверяются только\n"
                                   "разрешение, шрифты и цвет.", text_color=MUTED)
        elif not state["docs"]:
            tz_info.configure(text="Позиций в ТЗ: %d" % summary["spec_total"], text_color=MUTED)
        else:
            done = summary["spec_covered"] == summary["spec_total"]
            tz_info.configure(text="Макеты есть для %d из %d позиций ТЗ"
                              % (summary["spec_covered"], summary["spec_total"]),
                              text_color=S_FG[OK] if done else S_FG[WARN])
        render()
        save_settings()

    def schedule(*_):
        if state["job"]:
            root.after_cancel(state["job"])
        state["job"] = root.after(450, refresh)

    def add_paths(paths):
        files = []
        for p in paths:
            if os.path.isdir(p):
                for dirpath, _dirs, names in os.walk(p):
                    files += [os.path.join(dirpath, n) for n in sorted(names) if n.lower().endswith(".pdf")]
            elif p.lower().endswith(".pdf") and os.path.isfile(p):
                files.append(p)
        known = {os.path.normcase(os.path.abspath(d["path"])) for d in state["docs"]}
        files = [f for f in files if os.path.normcase(os.path.abspath(f)) not in known]
        if not files:
            return
        state["busy"] += len(files)
        title.configure(text="Читаю файлы: %d..." % state["busy"])
        if not state["rows"]:
            render()

        def work():
            for f in files:
                q.put(analyze_pdf(f))
        threading.Thread(target=work, daemon=True).start()

    def poll():
        got = False
        try:
            while True:
                state["docs"].append(q.get_nowait())
                state["busy"] -= 1
                got = True
        except queue.Empty:
            pass
        if got:
            if state["busy"] > 0:
                title.configure(text="Читаю файлы: осталось %d..." % state["busy"])
            else:
                state["docs"].sort(key=lambda d: d["name"].lower())
                refresh()
        root.after(120, poll)

    def add_files():
        add_paths(filedialog.askopenfilenames(title="Выберите PDF", filetypes=[("PDF", "*.pdf")]))

    def add_folder():
        folder = filedialog.askdirectory(title="Папка с PDF")
        if folder:
            add_paths([folder])

    def recheck():
        paths = [d["path"] for d in state["docs"]]
        state["docs"] = []
        if paths:
            add_paths(paths)
        else:
            refresh()

    def remove_doc(doc):
        state["docs"] = [d for d in state["docs"] if d is not doc]
        refresh()

    def clear():
        state["docs"] = []
        state["open"].clear()
        refresh()

    def report_text():
        return build_report(state["rows"], state["summary"], state["bad"])

    def save_report():
        if not state["rows"]:
            return
        first = os.path.dirname(state["docs"][0]["path"]) if state["docs"] else ""
        path = filedialog.asksaveasfilename(title="Сохранить отчёт", defaultextension=".txt",
                                            initialdir=first, initialfile="проверка_макетов.txt",
                                            filetypes=[("Текст", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write(report_text())

    def copy_report():
        if state["rows"]:
            root.clipboard_clear()
            root.clipboard_append(report_text())

    def on_resize(event):
        px = max(300, int(event.width / ctk.ScalingTracker.get_window_scaling(root)) - 120)
        if abs(px - state.get("wrap_px", 0)) > 20:
            state["wrap_px"] = px
            banner_text.configure(wraplength=px + 60)
            for lab in state["wrap"]:
                try:
                    lab.configure(wraplength=px)
                except Exception:
                    pass

    body.bind("<Configure>", on_resize)
    tz.bind("<KeyRelease>", schedule)
    for var in (v_unit, v_dist, v_ppi, v_scale, v_tol, v_allow, v_rgb):
        var.trace_add("write", schedule)
    if dnd:
        root.drop_target_register(dnd.DND_FILES)
        root.dnd_bind("<<Drop>>", lambda e: add_paths(root.tk.splitlist(e.data)))
    clickable(empty, lambda _e: add_files())

    root.protocol("WM_DELETE_WINDOW", lambda: (save_settings(), root.destroy()))
    render_theme()
    render_adv()
    refresh()
    add_paths(initial_files)
    poll()
    return root


if __name__ == "__main__":
    app = run_gui(sys.argv[1:])
    if app is not None:
        app.mainloop()
