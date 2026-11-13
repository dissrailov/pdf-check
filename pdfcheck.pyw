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

_UNIT = r"(?:(мм|см|м|mm|cm|m)(?![а-яёa-z]))?"
SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*" + _UNIT + r"(?:\s*[xх×*]\s*|\s+на\s+)(\d+(?:[.,]\d+)?)\s*" + _UNIT, re.I)
COUNT_RE = re.compile(r"(\d+)\s*шт\.?", re.I)
MULT_RE = re.compile(r"^\s*[xх×*]\s*(\d+)\s*$", re.I)
MAX_PPI = 150.0  # выше для широкоформатной печати разрешение не нужно
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
        u1 = (m.group(2) or m.group(4) or default_unit).lower()
        u2 = (m.group(4) or m.group(2) or default_unit).lower()
        w = float(m.group(1).replace(",", ".")) * UNIT_MM[u1]
        h = float(m.group(3).replace(",", ".")) * UNIT_MM[u2]
        before, rest = line[:m.start()], line[m.end():]
        count = 1
        mult = MULT_RE.match(rest)
        found = COUNT_RE.search(rest) or COUNT_RE.search(before)
        if mult:
            count, rest = int(mult.group(1)), ""
        elif found:
            count = int(found.group(1))
            before, rest = COUNT_RE.sub("", before), COUNT_RE.sub("", rest)
        count = max(1, min(count, 999))
        strip = " \t:;,.-–—="
        name = before.strip(strip) or rest.strip(strip)
        if w <= 0 or h <= 0:
            bad.append(line)
            continue
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


def _cs_from_text(doc, text, depth=0):
    """Разбор значения /ColorSpace из словаря картинки."""
    text = text.strip()
    if depth > 4 or not text:
        return None
    m = re.match(r"^(\d+)\s+\d+\s+R$", text)
    if m:
        return _cs_from_text(doc, doc.xref_object(int(m.group(1)), compressed=True), depth + 1)
    body = text.lstrip("[ ").strip()
    if body.startswith(("/DeviceCMYK", "/CalCMYK")):
        return "CMYK"
    if body.startswith(("/DeviceRGB", "/CalRGB")):
        return "RGB"
    if body.startswith(("/DeviceGray", "/CalGray")):
        return "Gray"
    if body.startswith("/Lab"):
        return "Lab"
    if body.startswith(("/Separation", "/DeviceN")):
        return "Spot"
    if body.startswith("/ICCBased"):
        m = re.match(r"/ICCBased\s+(\d+)\s+\d+\s+R", body)
        if m:
            n = doc.xref_get_key(int(m.group(1)), "N")[1]
            return {"4": "CMYK", "3": "RGB", "1": "Gray"}.get(n.strip())
        return None
    if body.startswith("/Indexed"):
        rest = body[len("/Indexed"):].strip()
        m = re.match(r"^(\d+\s+\d+\s+R|\[.*?\]|/\w+)", rest, re.S)
        return _cs_from_text(doc, m.group(1), depth + 1) if m else None
    return None


def image_cs(doc, info):
    """Цветовая модель картинки так, как она записана в файле."""
    xref = info.get("xref") or 0
    if xref > 0:
        try:
            if doc.xref_get_key(xref, "ImageMask")[1] == "true":
                return "Mask"
            kind, value = doc.xref_get_key(xref, "ColorSpace")
            if kind != "null":
                found = _cs_from_text(doc, value)
                if found:
                    return found
        except Exception:
            pass
    return cs_label(info)


NAMED_CS_USE = re.compile(rb"/([^\s/\[\]<>()]+)\s+(?:cs|CS)(?![\w])")
NAMED_CS_DEF = re.compile(r"/([^\s/\[\]<>()]+)\s*(\d+\s+\d+\s+R|\[[^\]]*\]|/\w+)")


def named_rgb_spaces(doc, xref):
    """Имена цветовых пространств из ресурсов объекта, которые являются RGB."""
    try:
        kind, value = doc.xref_get_key(xref, "Resources/ColorSpace")
        if kind == "xref":
            value = doc.xref_object(int(value.split()[0]), compressed=True)
        elif kind != "dict":
            return set()
        return {m.group(1) for m in NAMED_CS_DEF.finditer(value)
                if _cs_from_text(doc, m.group(2)) == "RGB"}
    except Exception:
        return set()


SHADING_USE = re.compile(rb"/([^\s/\[\]<>()]+)\s+sh(?![\w])")
SHADING_DEF = re.compile(r"/([^\s/\[\]<>()]+)\s*(\d+)\s+\d+\s+R")
INLINE_IMAGE = re.compile(rb"(?:^|\s)BI\s.{0,400}?\sID\s", re.S)


def rgb_shadings(doc, xref):
    """Имена градиентов (shading) из ресурсов объекта, заданных в RGB."""
    try:
        kind, value = doc.xref_get_key(xref, "Resources/Shading")
        if kind == "xref":
            value = doc.xref_object(int(value.split()[0]), compressed=True)
        elif kind != "dict":
            return set()
        names = set()
        for m in SHADING_DEF.finditer(value):
            cs_kind, cs_value = doc.xref_get_key(int(m.group(2)), "ColorSpace")
            if cs_kind != "null" and _cs_from_text(doc, cs_value) == "RGB":
                names.add(m.group(1))
        return names
    except Exception:
        return set()


def content_streams(doc, page):
    """Потоки содержимого страницы и её форм вместе с xref объекта, чьи ресурсы они используют."""
    streams = []
    try:
        streams.append((page.read_contents(), page.xref))
        for xo in page.get_xobjects():
            try:
                data = doc.xref_stream(xo[0])
                if data:
                    streams.append((data, xo[0]))
            except Exception:
                pass
    except Exception:
        pass
    return streams


def has_inline_images(doc, page):
    return any(INLINE_IMAGE.search(data) for data, _x in content_streams(doc, page))


def vector_has_rgb(doc, page):
    """RGB в векторе и тексте: операторы rg/RG, именованные RGB-пространства и RGB-градиенты."""
    try:
        page_cs, page_sh = named_rgb_spaces(doc, page.xref), rgb_shadings(doc, page.xref)
        for data, xref in content_streams(doc, page):
            if RGB_OP.search(data):
                return True
            names = page_cs if xref == page.xref else named_rgb_spaces(doc, xref) | page_cs
            if names and any(n.decode("latin-1") in names for n in NAMED_CS_USE.findall(data)):
                return True
            shades = page_sh if xref == page.xref else rgb_shadings(doc, xref) | page_sh
            if shades and any(n.decode("latin-1") in shades for n in SHADING_USE.findall(data)):
                return True
    except Exception:
        pass
    return False


def analyze_pdf(path):
    res = {"path": path, "name": os.path.basename(path), "error": None, "pages": []}
    try:
        doc = fitz.open(path)
        if doc.needs_pass:
            res["error"] = "файл защищён паролем"
            return res
        for pno in range(doc.page_count):
            page = doc[pno]
            media, trim, crop = page.mediabox, page.trimbox, page.cropbox
            # UserUnit: у макетов больше 5 м размеры в файле записаны с множителем
            uu = max(page.rect.width, page.rect.height) / max(crop.width, crop.height, 1e-6)
            if not 0.5 < uu < 1000 or abs(uu - 1) < 0.001:
                uu = 1.0
            w, h = trim.width * uu, trim.height * uu
            bleed = max(media.width - trim.width, media.height - trim.height) / 2.0 * uu
            if page.rotation in (90, 270):
                w, h = h, w
            images = []
            inline = has_inline_images(doc, page)
            for info in page.get_image_info(xrefs=True):
                if not inline and not info.get("xref"):
                    # Не картинка: так движок показывает векторные градиенты (всегда «72 ppi»).
                    continue
                pw, ph = info.get("width", 0), info.get("height", 0)
                if min(pw, ph) <= 4:
                    continue  # заливки и полоски градиентов, а не изображения
                a, b, c, d = info["transform"][:4]
                dw, dh = math.hypot(a, b), math.hypot(c, d)
                if dw <= 0 or dh <= 0:
                    continue
                box = fitz.Rect(info["bbox"]) & page.rect  # видимая на странице часть
                visible = 0.0 if box.is_empty else box.width * box.height / max(
                    page.rect.width * page.rect.height, 1e-6)
                images.append({"px_w": pw, "px_h": ph, "w_mm": dw * PT_TO_MM, "visible": visible,
                               "h_mm": dh * PT_TO_MM, "cs": image_cs(doc, info)})
            missing = sorted({f[3] for f in doc.get_page_fonts(pno, full=True)
                              if f[1] == "n/a" and f[2] != "Type3"})
            has_text = bool(page.get_text("text").strip())
            thumb = None
            try:
                z = 76.0 / max(page.rect.width, page.rect.height, 1.0)
                thumb = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("ppm")
            except Exception:
                pass
            res["pages"].append({
                "thumb": thumb,
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
def share(fraction):
    pct = min(fraction, 1.0) * 100
    return "<1%" if pct < 1 else "%.0f%%" % pct


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
    target = st["min_ppi"] if st["min_ppi"] else min(needed_ppi(st["distance"]), MAX_PPI)
    soft = target * 0.6  # ниже 60% от нормы пиксели уже заметны, между 60% и 100% «мягковато»
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
            r["extra"] = best.used > best.count
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
            r["extra"] = r["spec"].used > r["spec"].count

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
            if m[0] != 1:
                how += ", макет в масштабе 1:%g" % m[0]
            if m[1] == "rot":
                checks.append((WARN, "Размер", "совпадает с %s, но макет повёрнут на 90°."
                               % spec_label(sp, unit), []))
            elif m[1] == "allow":
                checks.append((OK, "Размер", "%s + припуск %s мм на сторону (%s)."
                               % (spec_label(sp, unit), fmt_num(st["allow"]), how), []))
            else:
                checks.append((OK, "Размер", "совпадает с %s (%s)." % (spec_label(sp, unit), how), []))
            meta.append("ТЗ: " + (sp.name or fmt_size(sp.w, sp.h, unit)))
        if sp is not None and r.get("extra"):
            checks.append((WARN, "Лишний макет", "для позиции «%s» в ТЗ нужно %d шт., макетов больше. "
                           "Возможно, это дубль или старая версия."
                           % (sp.name or fmt_size(sp.w, sp.h, unit), sp.count), []))
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
            page_area = max(p["w_mm"] * p["h_mm"], 1e-6)
            sub = ["%-8s %4.0f ppi   %d × %d px   на баннере %s   %s   %s площади"
                   % (TAG[s], ppi, im["px_w"], im["px_h"],
                      fmt_size(im["w_mm"] * scale, im["h_mm"] * scale, unit), im["cs"],
                      share(im.get("visible", im["w_mm"] * im["h_mm"] / page_area)))
                   for ppi, s, im in imgs[:20]]
            if len(imgs) > 20:
                sub.append("... и ещё %d картинок с разрешением выше" % (len(imgs) - 20))
            if max(i.get("visible", 0) for i in p["images"]) >= 0.97 and not p["has_text"]:
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
        rgb_list = [im for im in p["images"] if im["cs"] == "RGB"]
        rgb_imgs = len(rgb_list)
        rgb_sub = ["RGB   %d × %d px   на баннере %s" % (
            im["px_w"], im["px_h"], fmt_size(im["w_mm"] * scale, im["h_mm"] * scale, unit))
            for im in sorted(rgb_list, key=lambda i: -i["w_mm"] * i["h_mm"])[:10]]
        if rgb_imgs or p["vec_rgb"]:
            parts = []
            if rgb_imgs:
                parts.append("картинок в RGB: %d из %d" % (rgb_imgs, len(p["images"])))
            if p["vec_rgb"]:
                parts.append("RGB в векторе или тексте")
            checks.append((WARN if st["rgb_warn"] else INFO, "Цвет",
                           "%s. В CMYK-печати яркие цвета могут потускнеть."
                           % "; ".join(parts), rgb_sub))
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
    from tkinter import filedialog, messagebox

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
    root.geometry("1320x820")
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
             "open": set(), "thumbs": {}, "sig": None, "limit": 40, "gen": 0, "pending": set(), "adv": bool(saved.get("adv", False)), "wrap": [], "filters": {}}
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

    # --- подсказки ----------------------------------------------------------
    class Tip:
        """Всплывающая подсказка при наведении."""

        def __init__(self, widget, text):
            self.text, self.win, self.job = text, None, None
            for seq, fn in (("<Enter>", self.enter), ("<Leave>", self.hide), ("<ButtonPress>", self.hide)):
                try:
                    widget.bind(seq, fn, add="+")
                except Exception:
                    pass

        def enter(self, _e=None):
            self.hide()
            self.job = root.after(350, self.show)

        def show(self):
            self.job = None
            try:
                scale = ctk.ScalingTracker.get_window_scaling(root)
                dark = ctk.get_appearance_mode() == "Dark"
                win = tk.Toplevel(root)
                win.wm_overrideredirect(True)
                try:
                    win.attributes("-topmost", True)
                except Exception:
                    pass
                tk.Label(win, text=self.text, justify="left", wraplength=int(320 * scale),
                         bg="#2b3242" if not dark else "#e9ecf2", fg="#ffffff" if not dark else "#131722",
                         font=(family or "TkDefaultFont", 9), padx=10, pady=8, bd=0).pack()
                win.update_idletasks()
                x, y = root.winfo_pointerx() + 14, root.winfo_pointery() + 18
                x = max(4, min(x, root.winfo_screenwidth() - win.winfo_reqwidth() - 8))
                if y + win.winfo_reqheight() > root.winfo_screenheight() - 8:
                    y = root.winfo_pointery() - win.winfo_reqheight() - 12
                win.wm_geometry("+%d+%d" % (x, y))
                self.win = win
            except Exception:
                self.win = None

        def hide(self, _e=None):
            if self.job:
                root.after_cancel(self.job)
                self.job = None
            if self.win:
                try:
                    self.win.destroy()
                except Exception:
                    pass
                self.win = None

    def hint(parent, text):
        """Значок «?» с подсказкой."""
        lab = ctk.CTkLabel(parent, text="?", width=18, height=18, corner_radius=9, font=F(11, "bold"),
                           fg_color=SEG, text_color=MUTED)
        try:
            lab.configure(cursor="question_arrow")
        except Exception:
            pass
        Tip(lab, text)
        return lab

    HELP = [
        ("1. Впишите размеры из ТЗ",
         "Слева одна строка на каждый баннер: ширина и высота обязательны. Выберите, в чём они "
         "записаны: в сантиметрах или миллиметрах.\n"
         "Если ТЗ пришло текстом, нажмите «Вставить списком» и вставьте его целиком."),
        ("2. Назовите файлы как позиции в ТЗ",
         "Если в имени файла есть название позиции, программа сверит размер именно с ней: файл "
         "«Л1_верх.pdf» сверяется с позицией «Л1 верх».\n"
         "Если названия нет или в одном PDF несколько страниц, позиция подбирается по размеру страницы."),
        ("3. Выберите расстояние",
         "С какого расстояния люди будут смотреть на баннер. Детская зона, стойка, ролл-ап: 0,5–1 м. "
         "Стена в зале: 2–3 м. Фасад и улица: 5 м.\n"
         "Если типография назвала своё минимальное разрешение, впишите его в «Дополнительных "
         "настройках»: оно важнее расстояния."),
        ("4. Добавьте макеты",
         "Перетащите PDF-файлы или папку в окно либо нажмите «Добавить PDF». После правок в макете "
         "пересохраните PDF и нажмите «Перечитать»."),
        ("5. Прочитайте результат",
         "Зелёный «Готов»: можно отдавать в печать.\n"
         "Жёлтый «Проверить»: посмотрите причину и решите сами, например RGB или повёрнутый макет.\n"
         "Красный «Ошибка»: нужно исправить, например размер не по ТЗ, низкое разрешение или "
         "невстроенные шрифты.\n"
         "Клик по карточке раскрывает все проверки и список картинок с их разрешением."),
        ("Масштаб макета",
         "Макет может быть сделан в масштабе 1:2, 1:5, 1:10. Программа определяет это сама, сравнивая "
         "с ТЗ, и пишет масштаб на карточке. Без ТЗ масштаб нужно указать вручную в дополнительных "
         "настройках, иначе разрешение посчитается неверно."),
        ("Что значит разрешение",
         "Программа считает, сколько пикселей картинки приходится на дюйм баннера в натуральную "
         "величину. Если мало, вблизи будут видны пиксели. У вектора и текста в кривых разрешения "
         "нет: они чёткие при любом размере.\n"
         "Низкое разрешение у фона на весь макет критично, у мелкой детали на 1% площади обычно нет: "
         "доля площади указана в списке картинок."),
        ("Чего программа не видит",
         "Картинку, которую растянули из маленькой ещё до вставки в макет: разрешение будет "
         "хорошим, а изображение мыльным. Артефакты сжатия JPEG. Ошибки в тексте и содержании. "
         "Поэтому важные макеты всё равно просмотрите глазами в масштабе 100%."),
    ]

    def show_help():
        win = ctk.CTkToplevel(root)
        win.title("Как пользоваться PDF Check")
        win.geometry("600x680+%d+%d" % (root.winfo_rootx() + 120, root.winfo_rooty() + 40))
        win.configure(fg_color=BG)
        win.transient(root)
        area = ctk.CTkScrollableFrame(win, fg_color="transparent")
        area.pack(fill="both", expand=True, padx=10, pady=(12, 0))
        for head, body_text in HELP:
            box = card(area)
            box.pack(fill="x", padx=6, pady=(0, 10))
            ctk.CTkLabel(box, text=head, font=F(14, "bold"), text_color=TEXT, anchor="w").pack(
                fill="x", padx=16, pady=(12, 2))
            ctk.CTkLabel(box, text=body_text, font=F(13), text_color=TEXT, anchor="w", justify="left",
                         wraplength=500).pack(fill="x", padx=16, pady=(0, 12))
        button(win, "Понятно", win.destroy, primary=True, width=140).pack(pady=12)

        def front():
            try:
                win.lift()
                win.focus_set()
            except Exception:
                pass
        win.after(250, front)

    # === боковая панель ======================================================
    side = card(root, width=380)
    side.pack(side="left", fill="y", padx=(16, 0), pady=16)
    side.pack_propagate(False)

    brand = ctk.CTkFrame(side, fg_color="transparent")
    brand.pack(fill="x", padx=18, pady=(16, 4))
    ctk.CTkLabel(brand, text=APP_NAME, font=F(20, "bold"), text_color=TEXT).pack(side="left")
    theme_btn = ctk.CTkButton(brand, text="", width=64, height=28, corner_radius=8, font=F(12),
                              fg_color=SOFT, hover_color=BORDER, text_color=MUTED)
    theme_btn.pack(side="right")
    ctk.CTkButton(brand, text="Как пользоваться", width=120, height=28, corner_radius=8, font=F(12),
                  fg_color=SOFT, hover_color=BORDER, text_color=ACCENT,
                  command=lambda: show_help()).pack(side="right", padx=6)

    tz_head = ctk.CTkFrame(side, fg_color="transparent")
    tz_head.pack(fill="x", padx=18, pady=(12, 0))
    ctk.CTkLabel(tz_head, text="Размеры из ТЗ", font=F(14, "bold"), text_color=TEXT).pack(side="left")
    hint(tz_head, "Одна строка на каждый баннер из задания. Ширина и высота обязательны.\n\n"
                  "Название нужно, чтобы привязать файл по имени: файл «Л1_верх.pdf» сверится "
                  "с позицией «Л1 верх». Без названия позиция подбирается по размеру.\n\n"
                  "«шт» заполняйте, если одинаковых баннеров несколько.").pack(side="left", padx=8)
    ctk.CTkLabel(side, text="Размер обязателен, название и количество по желанию",
                 font=F(12), text_color=MUTED).pack(anchor="w", padx=18)

    # нижняя часть панели пакуется первой, чтобы список ТЗ занимал всё остальное
    bottom = ctk.CTkFrame(side, fg_color="transparent")
    bottom.pack(side="bottom", fill="x", padx=18, pady=(0, 16))

    tz_box = ctk.CTkScrollableFrame(side, fg_color=SOFT, corner_radius=10, border_width=1,
                                    border_color=BORDER)
    tz_box.pack(fill="both", expand=True, padx=18, pady=(8, 6))
    tz_rows = []

    def row_empty(row):
        return not any(row[k].get().strip() for k in ("name", "w", "h", "n"))

    def remove_row(row):
        row["frame"].destroy()
        tz_rows.remove(row)
        if not tz_rows:
            add_row()
        schedule()

    def add_row(values=("", "", "", ""), focus=False):
        fr = ctk.CTkFrame(tz_box, fg_color="transparent")
        fr.pack(fill="x", pady=2)
        row = {"frame": fr}
        ctk.CTkButton(fr, text="✕", width=24, height=30, corner_radius=7, font=F(12),
                      fg_color="transparent", hover_color=BORDER, text_color=MUTED,
                      command=lambda: remove_row(row)).pack(side="right")

        def ent(key, placeholder, width, value, expand=False):
            e = ctk.CTkEntry(fr, placeholder_text=placeholder, width=width, height=30, corner_radius=7,
                             font=F(12), border_width=1, border_color=BORDER, fg_color=CARD,
                             text_color=TEXT)
            if value:
                e.insert(0, value)
            e.bind("<KeyRelease>", lambda _e: schedule())
            e.bind("<Return>", lambda _e: add_row(focus=True))
            row[key] = e
            return e

        ent("n", "шт", 36, values[3]).pack(side="right", padx=(4, 0))
        ent("h", "Выс.", 52, values[2]).pack(side="right")
        ctk.CTkLabel(fr, text="×", font=F(12), text_color=MUTED, width=14).pack(side="right")
        ent("w", "Шир.", 52, values[1]).pack(side="right")
        ent("name", "Название", 90, values[0]).pack(side="left", fill="x", expand=True, padx=(0, 6))
        tz_rows.append(row)
        if focus:
            row["name"].focus_set()
        return row

    def add_specs(specs):
        k = UNIT_MM[v_unit.get()]
        for row in [r for r in tz_rows if row_empty(r)]:
            row["frame"].destroy()
            tz_rows.remove(row)
        for sp in specs:
            add_row((sp.name, fmt_num(sp.w / k), fmt_num(sp.h / k), str(sp.count) if sp.count > 1 else ""))
        if not tz_rows:
            add_row()

    def get_specs():
        k = UNIT_MM[v_unit.get()]
        specs, bad = [], []
        for i, row in enumerate(tz_rows, 1):
            name, ws, hs, ns = (row[key].get().strip() for key in ("name", "w", "h", "n"))
            if not (name or ws or hs or ns):
                for key in ("w", "h", "n"):
                    row[key].configure(border_color=BORDER)
                continue
            w, h = to_float(ws), to_float(hs)
            n = 1 if not ns else (int(ns) if re.fullmatch(r"[0-9]{1,3}", ns) and int(ns) >= 1 else None)
            row["w"].configure(border_color=BORDER if w else S_BAR[FAIL])
            row["h"].configure(border_color=BORDER if h else S_BAR[FAIL])
            row["n"].configure(border_color=BORDER if n else S_BAR[FAIL])
            if w and h and n:
                specs.append(Spec(name, w * k, h * k, n))
            else:
                bad.append(name or "строка %d" % i)
        return specs, bad

    def rows_data():
        return [[row[key].get().strip() for key in ("name", "w", "h", "n")]
                for row in tz_rows if not row_empty(row)]

    def clear_tz():
        for row in list(tz_rows):
            row["frame"].destroy()
        tz_rows.clear()
        for _ in range(4):
            add_row()
        schedule()

    def paste_dialog():
        win = ctk.CTkToplevel(root)
        win.title("Вставить размеры списком")
        win.geometry("480x440")
        win.configure(fg_color=BG)
        win.transient(root)
        ctk.CTkLabel(win, justify="left", anchor="w", font=F(13), text_color=TEXT,
                     text="Вставьте текст из ТЗ, по одной позиции на строку.\n"
                          "Подойдёт любой вид записи:").pack(fill="x", padx=18, pady=(16, 2))
        ctk.CTkLabel(win, justify="left", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(family=mono, size=12),
                     text="Л1 верх 110x110\nЛ3 (низ) 110 × 97 см\nБаннер 1100*2180 мм 6 шт").pack(
            fill="x", padx=18)
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family=mono, size=13), corner_radius=10,
                             fg_color=CARD, border_width=1, border_color=BORDER, text_color=TEXT)
        box.pack(fill="both", expand=True, padx=18, pady=10)
        msg = ctk.CTkLabel(win, text="", justify="left", anchor="w", font=F(12),
                           text_color=S_FG[WARN], wraplength=440)
        msg.pack(fill="x", padx=18)

        def apply():
            specs, bad = parse_specs(box.get("1.0", "end"), v_unit.get())
            add_specs(specs)
            schedule()
            if bad:
                box.delete("1.0", "end")
                box.insert("1.0", "\n".join(bad))
                msg.configure(text="Добавлено позиций: %d. В оставшихся строках не нашёл размер "
                                   "вида 110x218: поправьте их или закройте окно." % len(specs))
            else:
                win.destroy()

        button(win, "Добавить в ТЗ", apply, primary=True).pack(anchor="e", padx=18, pady=(8, 16))
        def show():
            try:
                win.lift()
                win.grab_set()
                box.focus_set()
            except Exception:
                pass
        win.after(250, show)

    tz_btns = ctk.CTkFrame(bottom, fg_color="transparent")
    tz_btns.pack(fill="x", pady=(0, 6))
    for text, cmd in (("+ Позиция", lambda: add_row(focus=True)), ("Вставить списком", paste_dialog),
                      ("Очистить", clear_tz)):
        ctk.CTkButton(tz_btns, text=text, command=cmd, height=28, width=10, corner_radius=8,
                      font=F(12), fg_color=SOFT, hover_color=BORDER, text_color=TEXT).pack(
            side="left", padx=(0, 6))

    tz_info = ctk.CTkLabel(bottom, text="", font=F(12), text_color=MUTED, justify="left",
                           anchor="w", wraplength=340)
    tz_info.pack(fill="x")

    urow = ctk.CTkFrame(bottom, fg_color="transparent")
    urow.pack(fill="x", pady=(8, 0))
    ctk.CTkLabel(urow, text="Единицы в ТЗ", font=F(13), text_color=TEXT).pack(side="left")
    hint(urow, "В чём записаны ширина и высота в списке выше. На сами макеты не влияет: "
               "они могут быть в любом масштабе.").pack(side="left", padx=8)
    ctk.CTkSegmentedButton(urow, values=["см", "мм"], variable=v_unit, font=F(13), height=30,
                           fg_color=SEG, unselected_color=SEG,
                           unselected_hover_color=BORDER, selected_color=SEG_SEL,
                           selected_hover_color=SEG_SEL, text_color=TEXT).pack(side="right")

    drow = ctk.CTkFrame(bottom, fg_color="transparent")
    drow.pack(fill="x", pady=(14, 4))
    ctk.CTkLabel(drow, text="С какого расстояния смотрят", font=F(13), text_color=TEXT).pack(side="left")
    hint(drow, "Чем ближе зритель, тем выше нужно разрешение картинок.\n\n"
               "0,5–1 м: детская зона, стойка, ролл-ап, меню.\n"
               "2–3 м: стена в зале, витрина.\n"
               "5 м: фасад, улица, баннер под потолком.").pack(side="left", padx=8)
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

    def adv_row(row, label, widget_factory, unit_text, tip):
        ctk.CTkLabel(adv, text=label, font=F(12), text_color=TEXT).grid(
            row=row, column=0, sticky="w", padx=(12, 8), pady=4)
        widget_factory().grid(row=row, column=1, sticky="w", pady=4)
        ctk.CTkLabel(adv, text=unit_text, font=F(12), text_color=MUTED, width=28, anchor="w").grid(
            row=row, column=2, sticky="w", padx=(6, 4))
        hint(adv, tip).grid(row=row, column=3, sticky="w", padx=(0, 10))

    def entry(var):
        return lambda: ctk.CTkEntry(adv, textvariable=var, width=70, height=28, corner_radius=7,
                                    font=F(12), border_color=BORDER, fg_color=CARD)

    adv_row(0, "Свой порог", entry(v_ppi), "ppi",
            "Если типография назвала минимальное разрешение, например 100 ppi, впишите его сюда: "
            "оно заменит расчёт по расстоянию. Пусто: считать по расстоянию.")
    adv_row(1, "Масштаб 1:", lambda: ctk.CTkComboBox(
        adv, variable=v_scale, width=70, height=28, corner_radius=7, font=F(12),
        values=["Авто", "1", "2", "5", "10", "20", "100"], border_color=BORDER, fg_color=CARD,
        button_color=BORDER, command=lambda _v: schedule()), "",
            "«Авто»: программа сама поймёт, что макет сделан 1:2, 1:5 или 1:10, сравнив его с ТЗ.\n\n"
            "Если ТЗ не заполнено, а макеты в масштабе, выберите его вручную, иначе разрешение "
            "посчитается неверно.")
    adv_row(2, "Допуск размера", entry(v_tol), "мм",
            "На сколько миллиметров размер макета может отличаться от ТЗ и всё ещё считаться верным.")
    adv_row(3, "Припуск на сторону", entry(v_allow), "мм",
            "Если типография просит добавить поля под подгиб, карманы или люверсы, впишите их размер "
            "на одну сторону. Тогда макет размером «ТЗ + припуск со всех сторон» тоже считается верным.")
    ctk.CTkSwitch(adv, text="RGB считать предупреждением", variable=v_rgb, font=F(12),
                  progress_color=ACCENT, text_color=TEXT).grid(
        row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(6, 10))
    hint(adv, "Печать идёт в CMYK, и яркие RGB-цвета могут потускнеть. Выключите, если типография "
              "сама принимает RGB: тогда RGB не будет менять статус макета.").grid(
        row=4, column=3, sticky="w", padx=(0, 10), pady=(6, 10))

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
    b_save = button(top, "Сохранить отчёт", lambda: save_report())
    b_save.pack(side="right")
    b_copy = button(top, "Копировать отчёт", lambda: copy_report())
    b_copy.pack(side="right", padx=8)
    Tip(b_save, "Сохранить результаты всех проверок в текстовый файл, например чтобы отправить дизайнеру.")
    Tip(b_copy, "Скопировать тот же отчёт в буфер обмена, чтобы вставить в мессенджер или письмо.")

    tools = ctk.CTkFrame(main, fg_color="transparent")
    tools.pack(fill="x", pady=(12, 0))
    button(tools, "+  Добавить PDF", lambda: add_files(), primary=True).pack(side="left")
    b_folder = button(tools, "Папка", lambda: add_folder())
    b_folder.pack(side="left", padx=8)
    b_reload = button(tools, "Перечитать", lambda: recheck())
    b_reload.pack(side="left")
    b_clear = button(tools, "Очистить", lambda: clear())
    b_clear.pack(side="left", padx=8)
    Tip(b_folder, "Добавить все PDF из папки вместе с вложенными папками.")
    Tip(b_reload, "Заново прочитать файлы с диска. Нажмите после того, как исправили и пересохранили макет.")
    Tip(b_clear, "Убрать все макеты из списка. Сами файлы и ТЗ не трогаются.")
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
                "rgb_warn": v_rgb.get(), "tz_rows": rows_data(),
                "theme": ctk.get_appearance_mode().lower(), "adv": state["adv"], "help_seen": True}
        try:
            with open(settings_path(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def render_theme():
        theme_btn.configure(text="Тёмная" if ctk.get_appearance_mode() == "Light" else "Светлая")

    def toggle_theme():
        ctk.set_appearance_mode("dark" if ctk.get_appearance_mode() == "Light" else "light")
        render_theme()
        render()
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
        thumb = r["page"].get("thumb") if r.get("page") else None
        if thumb:
            try:
                if key not in state["thumbs"]:
                    state["thumbs"][key] = tk.PhotoImage(data=thumb)
                dark = ctk.get_appearance_mode() == "Dark"
                tk.Label(box, image=state["thumbs"][key], bd=0, width=80, bg=CARD[1 if dark else 0],
                         highlightthickness=0).pack(
                    side="left", anchor="n", padx=(12, 0), pady=14)
            except Exception:
                pass
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
                      command=lambda: remove_row_card(r)).pack(side="right")
        ctk.CTkLabel(head, text="Скрыть ▴" if is_open else "Все проверки ▾", font=F(12),
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
        sig = (ctk.get_appearance_mode(), v_filter.get().split(" ")[0], state["busy"] > 0,
               tuple(sorted(state["open"])), tuple(state["bad"]),
               tuple(summary["missing"]) if summary else (), state["gen"], state["limit"],
               tuple((r["title"], r["status"], tuple(r["meta"]),
                      tuple((c[0], c[1], c[2], tuple(c[3])) for c in r["checks"])) for r in rows))
        if sig == state["sig"]:
            return
        state["sig"] = sig
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
                empty_hint.configure(text="1. Слева впишите размеры из ТЗ\n"
                                          "2. Добавьте PDF с макетами\n"
                                          "3. Красные карточки нужно исправить, зелёные готовы\n\n"
                                          "Подробнее: кнопка «Как пользоваться» слева вверху")
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
            notes.append("В ТЗ есть строки с ошибкой, они не учитываются (поля подсвечены красным): "
                         + "; ".join(state["bad"]))
        if notes:
            banner_text.configure(text="\n".join(notes))
            banner.pack(fill="x", pady=(12, 0), before=body)
        else:
            banner.pack_forget()

        lst.pack(fill="both", expand=True)
        visible = [r for r in rows if current is None or r["status"] == current]
        visible.sort(key=lambda r: -r["status"])
        for r in visible[:state["limit"]]:
            make_card(r)
        if len(visible) > state["limit"]:
            def more():
                state["limit"] += 40
                render()
            button(lst, "Показать ещё %d" % min(40, len(visible) - state["limit"]), more).pack(pady=(4, 16))
        if not visible:
            ctk.CTkLabel(lst, text="В этой категории пусто", font=F(13), text_color=MUTED).pack(pady=40)

    def refresh(*_):
        state["job"] = None
        st = current_settings()
        specs, bad = get_specs()
        rows, summary = evaluate(state["docs"], specs, st)
        state.update(rows=rows, summary=summary, bad=bad)
        if st["min_ppi"]:
            ppi_info.configure(text="Задан свой порог: от %.0f ppi" % summary["target"])
        else:
            capped = needed_ppi(st["distance"]) > MAX_PPI
            ppi_info.configure(text="Нужно разрешение от %.0f ppi%s" % (
                summary["target"], " (больше для печати не требуется)" if capped else ""))
        if not specs:
            tz_info.configure(text="Без размеров проверяются только разрешение,\n"
                                   "шрифты и цвет.", text_color=MUTED)
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
        norm = lambda f: os.path.normcase(os.path.abspath(f))
        known = {norm(d["path"]) for d in state["docs"]} | state["pending"]
        unique = []
        for f in files:
            if norm(f) not in known:
                known.add(norm(f))
                unique.append(f)
        files = unique
        if not files:
            return
        state["pending"].update(norm(f) for f in files)
        state["busy"] += len(files)
        title.configure(text="Читаю файлы: %d..." % state["busy"])
        if not state["rows"]:
            render()
        gen = state["gen"]

        def work():
            for f in files:
                q.put((gen, analyze_pdf(f)))
        threading.Thread(target=work, daemon=True).start()

    def poll():
        got = False
        try:
            while True:
                gen, doc = q.get_nowait()
                if gen != state["gen"]:
                    continue  # результат от уже сброшенного списка
                state["docs"].append(doc)
                state["pending"].discard(os.path.normcase(os.path.abspath(doc["path"])))
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

    def reset_list():
        state["gen"] += 1
        state["limit"] = 40
        state["docs"] = []
        state["busy"] = 0
        state["pending"].clear()
        state["thumbs"].clear()

    def add_files():
        add_paths(filedialog.askopenfilenames(title="Выберите PDF", filetypes=[("PDF", "*.pdf")]))

    def add_folder():
        folder = filedialog.askdirectory(title="Папка с PDF")
        if folder:
            add_paths([folder])

    def recheck():
        paths = [d["path"] for d in state["docs"]]
        reset_list()
        if paths:
            add_paths(paths)
        else:
            refresh()

    def remove_row_card(r):
        doc, page = r["doc"], r.get("page")
        if page is not None and len(doc["pages"]) > 1:
            doc["pages"].remove(page)  # у многостраничного файла убираем только эту страницу
        else:
            state["docs"] = [d for d in state["docs"] if d is not doc]
        refresh()

    def clear():
        reset_list()
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
            try:
                with open(path, "w", encoding="utf-8-sig") as fh:
                    fh.write(report_text())
            except OSError as e:
                messagebox.showerror(APP_NAME, "Не удалось сохранить отчёт:\n%s" % e)

    def copy_report():
        if state["rows"]:
            root.clipboard_clear()
            root.clipboard_append(report_text())

    def on_resize(event):
        px = max(300, int(event.width / ctk.ScalingTracker.get_window_scaling(root)) - 230)
        if abs(px - state.get("wrap_px", 0)) > 20:
            state["wrap_px"] = px
            banner_text.configure(wraplength=px + 170)
            for lab in state["wrap"]:
                try:
                    lab.configure(wraplength=px)
                except Exception:
                    pass

    body.bind("<Configure>", on_resize)
    for var in (v_unit, v_dist, v_ppi, v_scale, v_tol, v_allow, v_rgb):
        var.trace_add("write", schedule)
    if dnd:
        root.drop_target_register(dnd.DND_FILES)
        root.dnd_bind("<<Drop>>", lambda e: add_paths(root.tk.splitlist(e.data)))
    clickable(empty, lambda _e: add_files())

    def on_error(exc, val, tb):
        import traceback
        text = "".join(traceback.format_exception(exc, val, tb))
        if not state.get("error_shown"):
            state["error_shown"] = True
            messagebox.showerror(APP_NAME, "В программе произошла ошибка. Пришлите этот текст "
                                           "разработчику:\n\n" + text[-1500:])
            state["error_shown"] = False

    root.report_callback_exception = on_error

    def hotkey(event):
        # В русской раскладке Tk не узнаёт Ctrl+C / V / X / A, чиним по коду клавиши.
        if sys.platform != "win32" or event.keysym.lower() in ("c", "v", "x", "a"):
            return None
        action = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}.get(event.keycode)
        if action:
            event.widget.event_generate(action)
            return "break"
        return None

    root.bind_all("<Control-KeyPress>", hotkey, add="+")
    root.protocol("WM_DELETE_WINDOW", lambda: (save_settings(), root.destroy()))
    if saved.get("tz_rows"):
        for values in saved["tz_rows"]:
            add_row((list(values) + ["", "", "", ""])[:4])
    elif saved.get("tz"):
        add_specs(parse_specs(saved["tz"], v_unit.get())[0])
    while len(tz_rows) < 4:
        add_row()
    render_theme()
    render_adv()
    refresh()
    add_paths(initial_files)
    poll()
    if not saved.get("help_seen") and not initial_files:
        root.after(600, show_help)
    return root


if __name__ == "__main__":
    app = run_gui(sys.argv[1:])
    if app is not None:
        app.mainloop()
