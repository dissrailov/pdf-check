"""Автотесты логики проверки. Запуск: python tests/test_core.py"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile

import pymupdf as fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
loader = importlib.machinery.SourceFileLoader("pdfcheck", os.path.join(ROOT, "pdfcheck.pyw"))
spec = importlib.util.spec_from_loader("pdfcheck", loader)
pc = importlib.util.module_from_spec(spec)
loader.exec_module(pc)

MM = 72 / 25.4
TMP = tempfile.mkdtemp()
ST = {"unit": "см", "distance": 1.0, "min_ppi": None, "scale": None, "tol": 2.0, "allow": 0.0,
      "rgb_warn": True}


def pix(w, h, cs=fitz.csCMYK):
    p = fitz.Pixmap(cs, fitz.IRect(0, 0, w, h))
    p.clear_with(128)
    return p


def make(name, w_mm, h_mm, image=None, cs=fitz.csCMYK, text=False, extra=None):
    doc = fitz.open()
    page = doc.new_page(width=w_mm * MM, height=h_mm * MM)
    if image:
        page.insert_image(page.rect, pixmap=pix(*image, cs), keep_proportion=False)
    else:
        page.draw_rect(page.rect, fill=(0, 1, 1, 0))
    if text:
        page.insert_text((20, 50), "Hi", fontsize=30, color=(0, 0, 0, 1))
    if extra:
        extra(doc, page)
    path = os.path.join(TMP, name)
    doc.save(path)
    return pc.analyze_pdf(path)


def run(docs, tz, **over):
    specs, bad = pc.parse_specs(tz, "см")
    rows, summary = pc.evaluate(docs, specs, dict(ST, **over))
    return rows, summary


def check(row, label):
    return next(c for c in row["checks"] if c[1] == label)


def test_parse():
    cases = {
        "Л1 (верх) 110 × 110 см": ("Л1 (верх)", 1100, 1100, 1),
        "Л1 низ 110x97": ("Л1 низ", 1100, 970, 1),
        "П2 1100х2180 мм": ("П2", 1100, 2180, 1),
        "1,1 м x 218 см": ("", 1100, 2180, 1),
        "Баннер 110 на 218": ("Баннер", 1100, 2180, 1),
        "Баннер 110*218 6 шт": ("Баннер", 1100, 2180, 6),
        "2 шт 110x218": ("", 1100, 2180, 2),
        "110x218x3": ("", 1100, 2180, 3),
        "З1: 16 x 218": ("З1", 160, 2180, 1),
    }
    for line, want in cases.items():
        specs, bad = pc.parse_specs(line, "см")
        assert not bad and len(specs) == 1, line
        sp = specs[0]
        got = (sp.name, round(sp.w), round(sp.h), sp.count)
        assert got == want, (line, got, want)
    specs, bad = pc.parse_specs("просто текст\n0x10\n\n# комментарий", "см")
    assert not specs and bad == ["просто текст", "0x10"], bad


def test_size_and_names():
    docs = [make("Л1_верх.pdf", 1100, 1100), make("Л1_низ.pdf", 1100, 1000),
            make("З1.pdf", 160, 2180), make("random.pdf", 500, 700), make("П2.pdf", 2180, 1100),
            make("scaled.pdf", 110, 218)]
    rows, summary = run(docs, "Л1 верх 110x110\nЛ1 низ 110x97\n31 16x218\nП2 110x218\nЛ3 110x218\nЛ9 50x50")
    by = {r["title"]: r for r in rows}
    assert by["Л1_верх.pdf"]["status"] == pc.OK
    assert by["Л1_низ.pdf"]["status"] == pc.FAIL and "97" in check(by["Л1_низ.pdf"], "Размер")[2]
    assert by["З1.pdf"]["status"] == pc.OK, "З1 и 31 должны считаться одним названием"
    assert by["random.pdf"]["status"] == pc.FAIL
    assert by["П2.pdf"]["status"] == pc.WARN and "90" in check(by["П2.pdf"], "Размер")[2]
    assert by["scaled.pdf"]["scale"] == 10 and "1:10" in check(by["scaled.pdf"], "Размер")[2]
    assert summary["missing"] == ["Л9 (50 × 50 см)"], summary["missing"]


def test_duplicates_and_counts():
    docs = [make("a.pdf", 1100, 2180), make("b.pdf", 1100, 2180), make("c.pdf", 1100, 2180)]
    rows, summary = run(docs, "Баннер 110x218 2 шт")
    assert [r["status"] for r in rows] == [pc.OK, pc.OK, pc.WARN]
    assert check(rows[2], "Лишний макет")
    assert not summary["missing"]
    rows, summary = run(docs[:1], "Баннер 110x218 3 шт")
    assert summary["missing"] == ["Баннер (110 × 218 см) ×2"]


def test_resolution():
    low = make("low.pdf", 1000, 1000, image=(1000, 1000))      # 25 ppi
    edge = make("edge.pdf", 1000, 1000, image=(2800, 2800))    # 71 ppi
    good = make("good.pdf", 1000, 1000, image=(4000, 4000))    # 102 ppi
    rows, summary = run([low, edge, good], "")
    assert round(summary["target"]) == 87
    assert [check(r, "Разрешение")[0] for r in rows] == [pc.FAIL, pc.WARN, pc.OK]
    # макет 1:10: в натуре картинка в 10 раз крупнее
    small = make("s.pdf", 100, 100, image=(1000, 1000))
    rows, _ = run([small], "", scale=10.0)
    assert check(rows[0], "Разрешение")[0] == pc.FAIL
    # вплотную порог не выше 150 ppi
    _, summary = run([good], "", distance=0.2)
    assert summary["target"] == pc.MAX_PPI
    _, summary = run([good], "", min_ppi=300.0)
    assert summary["target"] == 300


def test_gradient_strip_ignored():
    def strip(doc, page):
        page.insert_image(page.rect, pixmap=pix(256, 1), keep_proportion=False)
    d = make("grad.pdf", 1000, 1000, image=(4000, 4000), extra=strip)
    assert [(i["px_w"], i["px_h"]) for i in d["pages"][0]["images"]] == [(4000, 4000)]


def test_colour():
    rgb = make("rgb.pdf", 500, 500, image=(2000, 2000), cs=fitz.csRGB)
    cmyk = make("cmyk.pdf", 500, 500, image=(2000, 2000))
    assert rgb["pages"][0]["images"][0]["cs"] == "RGB"
    assert cmyk["pages"][0]["images"][0]["cs"] == "CMYK"
    rows, _ = run([rgb, cmyk], "")
    assert check(rows[0], "Цвет")[0] == pc.WARN and check(rows[1], "Цвет")[0] == pc.OK
    rows, _ = run([rgb], "", rgb_warn=False)
    assert rows[0]["status"] == pc.OK

    def icc_image(doc, page):
        xref = page.get_images()[0][0]
        icc = doc.get_new_xref()
        doc.update_object(icc, "<</N 4>>")
        doc.update_stream(icc, b"profile")
        doc.xref_set_key(xref, "ColorSpace", "[/ICCBased %d 0 R]" % icc)
    d = make("icc.pdf", 500, 500, image=(2000, 2000), extra=icc_image)
    assert d["pages"][0]["images"][0]["cs"] == "CMYK"

    def vec_rgb(doc, page):
        page.draw_rect(fitz.Rect(10, 10, 50, 50), fill=(1, 0, 0))
    assert make("vrgb.pdf", 500, 500, extra=vec_rgb)["pages"][0]["vec_rgb"]
    assert not make("vcmyk.pdf", 500, 500)["pages"][0]["vec_rgb"]

    def named_rgb(doc, page):
        icc = doc.get_new_xref()
        doc.update_object(icc, "<</N 3>>")
        doc.update_stream(icc, b"profile")
        kind, value = doc.xref_get_key(page.xref, "Resources")
        target, key = (int(value.split()[0]), "ColorSpace") if kind == "xref" else (page.xref, "Resources/ColorSpace")
        doc.xref_set_key(target, key, "<</CS0 [/ICCBased %d 0 R]>>" % icc)
        extra = doc.get_new_xref()
        doc.update_object(extra, "<<>>")
        doc.update_stream(extra, b"q /CS0 cs 1 0 0 scn 10 10 200 200 re f Q")
        old = doc.xref_get_key(page.xref, "Contents")[1].strip("[]")
        doc.xref_set_key(page.xref, "Contents", "[%s %d 0 R]" % (old, extra))
    assert make("named.pdf", 500, 500, extra=named_rgb)["pages"][0]["vec_rgb"]
    for text, want in {"/DeviceCMYK": "CMYK", "[/Indexed /DeviceRGB 255 <00>]": "RGB",
                       "[/Separation /Gold /DeviceCMYK 5 0 R]": "Spot", "/DeviceGray": "Gray"}.items():
        assert pc._cs_from_text(fitz.open(), text) == want, text


def test_fonts():
    rows, _ = run([make("font.pdf", 500, 500, text=True)], "")
    assert check(rows[0], "Шрифты")[0] == pc.FAIL


def test_user_unit_and_boxes():
    def uu(doc, page):
        doc.xref_set_key(page.xref, "UserUnit", "10")
    d = make("uu.pdf", 600, 300, image=(6000, 3000), extra=uu)
    page = d["pages"][0]
    assert round(page["w_mm"]) == 6000 and round(page["images"][0]["w_mm"]) == 6000

    def trim(doc, page):
        r = page.rect
        page.set_trimbox(fitz.Rect(r.x0 + 10 * MM, r.y0 + 10 * MM, r.x1 - 10 * MM, r.y1 - 10 * MM))
    d = make("trim.pdf", 1120, 1120, extra=trim)
    assert round(d["pages"][0]["w_mm"]) == 1100 and round(d["pages"][0]["bleed_mm"]) == 10
    rows, _ = run([d], "Л1 110x110")
    assert rows[0]["status"] == pc.OK

    def rotate(doc, page):
        page.set_rotation(90)
    d = make("rot.pdf", 1100, 2180, extra=rotate)
    assert round(d["pages"][0]["w_mm"]) == 2180


def test_allowance_and_tolerance():
    d = make("allow.pdf", 1200, 2280)
    rows, _ = run([d], "Б 110x218")
    assert rows[0]["status"] == pc.FAIL
    rows, _ = run([d], "Б 110x218", allow=50.0)
    assert rows[0]["status"] == pc.OK
    d = make("tol.pdf", 1103, 2180)
    assert run([d], "Б 110x218")[0][0]["status"] == pc.FAIL
    assert run([d], "Б 110x218", tol=5.0)[0][0]["status"] == pc.OK


def test_multipage_and_broken():
    doc = fitz.open()
    doc.new_page(width=1100 * MM, height=1100 * MM)
    doc.new_page(width=1100 * MM, height=970 * MM)
    path = os.path.join(TMP, "multi.pdf")
    doc.save(path)
    with open(os.path.join(TMP, "broken.pdf"), "wb") as fh:
        fh.write(b"not a pdf")
    docs = [pc.analyze_pdf(path), pc.analyze_pdf(os.path.join(TMP, "broken.pdf"))]
    rows, summary = run(docs, "Л1 верх 110x110\nЛ1 низ 110x97")
    assert [r["status"] for r in rows] == [pc.OK, pc.OK, pc.FAIL]
    assert rows[0]["title"].endswith("стр. 1") and not summary["missing"]
    assert pc.build_report(rows, summary)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   ", name)
            except Exception:
                import traceback
                failed += 1
                print("FAIL ", name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
