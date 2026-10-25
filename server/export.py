"""Applique les modifications de l'éditeur sur le PDF d'origine et produit le PDF final."""

from __future__ import annotations

import base64
import io
import re

import pymupdf
from fontTools import subset as ft_subset
from fontTools.ttLib import TTFont

from . import fonts as F


def rgb(hexstr: str | None):
    if not hexstr or hexstr in ("none", "transparent"):
        return None
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _remove_image_instances(doc: pymupdf.Document, page: pymupdf.Page, items: list):
    """Supprime l'affichage de certaines images (opérateurs 'Do') du contenu de la page."""
    page.clean_contents()
    names = {it[0]: it[7] for it in page.get_images(full=True)}
    cont = page.get_contents()
    if not cont:
        return
    cx = cont[0]
    data = doc.xref_stream(cx)
    by_name: dict = {}
    for it in items:
        name = names.get(it["xref"])
        if name:
            by_name.setdefault(name, set()).add(it["k"])
    cuts = []
    for name, ks in by_name.items():
        pat = re.compile(rb"/" + re.escape(name.encode()) + rb"\s+Do\b")
        for idx, m in enumerate(pat.finditer(data)):
            if idx in ks:
                cuts.append(m.span())
    for a, b in sorted(cuts, reverse=True):
        data = data[:a] + data[b:]
    doc.update_stream(cx, data)


def _dedupe_font_names(doc: pymupdf.Document, page: pymupdf.Page):
    """Une entrée de ressource par police (TextWriter en crée une par mot écrit)."""
    kind, val = doc.xref_get_key(page.xref, "Resources/Font")
    if kind != "dict":
        return
    pairs = re.findall(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R", val)
    canon, rename = {}, {}
    for name, x in pairs:
        if x in canon:
            rename[name] = canon[x]
        else:
            canon[x] = name
    cont = page.get_contents()
    if not rename or len(cont) != 1:
        return
    data = doc.xref_stream(cont[0])
    data = re.sub(rb"/([^\s/<>\[\]()]+)(\s+[-\d.]+\s+Tf)",
                  lambda m: b"/" + rename.get(m.group(1).decode("latin-1"), m.group(1).decode("latin-1")).encode("latin-1") + m.group(2), data)
    doc.update_stream(cont[0], data)
    doc.xref_set_key(page.xref, "Resources/Font", "<<" + "".join(f"/{n} {x} 0 R" for x, n in canon.items()) + ">>")


def _strip_metadata(doc: pymupdf.Document):
    """Efface les métadonnées et traces d'édition : le PDF ressort comme un fichier neuf."""
    now = pymupdf.get_pdf_now()
    doc.set_metadata({"title": "", "author": "", "subject": "", "keywords": "", "creator": "", "producer": "",
                      "creationDate": now, "modDate": now, "trapped": ""})
    doc.del_xml_metadata()
    cat = doc.pdf_catalog()
    for key in ("PieceInfo", "Metadata", "SpiderInfo", "Legal"):
        if doc.xref_get_key(cat, key)[0] != "null":
            doc.xref_set_key(cat, key, "null")
    # Métadonnées XMP / données privées d'applications attachées aux pages, images, polices...
    for xref in range(1, doc.xref_length()):
        try:
            for key in ("Metadata", "PieceInfo", "LastModified"):
                if doc.xref_get_key(xref, key)[0] != "null":
                    doc.xref_set_key(xref, key, "null")
        except Exception:
            continue


class FontPlan:
    """Choix police principale / secours caractère par caractère, puis sous-ensemble des polices."""

    def __init__(self, docfonts: dict):
        self.docfonts = docfonts
        self.chars: dict = {}
        self.subsets: dict = {}

    def pieces(self, seg: dict) -> list:
        df = self.docfonts.get(seg["f"]) or next(iter(self.docfonts.values()))
        prim = df["fallback"] if seg.get("fb") else df["primary"]
        fb = df["fallback"]
        pf, ff = F.fitz_font(prim), F.fitz_font(fb)
        out = []
        for c in seg["t"]:
            fid = prim if pf.has_glyph(ord(c)) or not ff.has_glyph(ord(c)) else fb
            if out and out[-1][0] == fid:
                out[-1][1] += c
            else:
                out.append([fid, c])
        for fid, txt in out:
            self.chars.setdefault(fid, set()).update(txt)
        return out

    def font(self, fid: str) -> pymupdf.Font:
        f = self.subsets.get(fid)
        if f is None:
            data = F.font_bytes(fid)
            try:
                tt = TTFont(io.BytesIO(data))
                opts = ft_subset.Options()
                opts.layout_features = []
                opts.notdef_outline = True
                opts.name_IDs = ["*"]
                opts.drop_tables += ["DSIG", "GSUB", "GPOS", "morx", "kerx", "feat"]
                sub = ft_subset.Subsetter(opts)
                sub.populate(unicodes=[ord(c) for c in self.chars.get(fid, set())] + [32])
                sub.subset(tt)
                buf = io.BytesIO()
                tt.save(buf)
                f = pymupdf.Font(fontbuffer=buf.getvalue())
            except Exception as e:
                print("subset failed", fid, e)
                f = F.fitz_font(fid)
            self.subsets[fid] = f
        return f


def _write_text(page: pymupdf.Page, segs: list, plan: FontPlan, prepared: list):
    """Chaque mot est écrit à sa position absolue (un TextWriter par mot) : sinon MuPDF regroupe la
    ligne en un seul TJ dont les écarts arrondis s'accumulent jusqu'à ~0,5 pt en fin de ligne."""
    for seg, pieces in zip(segs, prepared):
        tw = pymupdf.TextWriter(page.rect, color=rgb(seg.get("c", "#000000")))
        pos = pymupdf.Point(seg["x"], seg["y"])
        size = float(seg["s"])
        ls = float(seg.get("ls") or 0)
        for fid, txt in pieces:
            font = plan.font(fid)
            if ls:
                for c in txt:
                    _r, last = tw.append(pos, c, font=font, fontsize=size)
                    pos = pymupdf.Point(last.x + ls, pos.y)
            else:
                _r, last = tw.append(pos, txt, font=font, fontsize=size)
                pos = pymupdf.Point(last.x, pos.y)
        tw.write_text(page)


def _image_bytes(src: str) -> bytes:
    if src.startswith("data:"):
        return base64.b64decode(src.split(",", 1)[1])
    raise ValueError("source d'image inconnue")


def _draw_shape(page: pymupdf.Page, it: dict):
    kind = it["shape"]
    x0, y0, x1, y1 = it["rect"]
    r = pymupdf.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    stroke = rgb(it.get("stroke"))
    fill = rgb(it.get("fill"))
    width = float(it.get("sw", 1)) if stroke else 0
    op = float(it.get("opacity", 1))
    if kind == "line":
        page.draw_line(pymupdf.Point(x0, y0), pymupdf.Point(x1, y1), color=stroke or (0, 0, 0),
                       width=float(it.get("sw", 1)), lineCap=1, stroke_opacity=op)
    elif kind == "ellipse":
        page.draw_oval(r, color=stroke, fill=fill, width=width, stroke_opacity=op, fill_opacity=op)
    elif kind == "highlight":
        page.draw_rect(r, color=None, fill=fill or (1, 0.92, 0.23), width=0, fill_opacity=op)
    elif kind == "whiteout":
        page.draw_rect(r, color=None, fill=fill or (1, 1, 1), width=0)
    else:
        page.draw_rect(r, color=stroke, fill=fill, width=width, stroke_opacity=op, fill_opacity=op)


def export_pdf(src_path: str, analysis: dict, ops: dict) -> bytes:
    doc = pymupdf.open(src_path)
    server = analysis["server"]
    plan = FontPlan(analysis["fonts"]["fonts"])

    # Préparation des textes (choix des polices caractère par caractère) avant de sous-ensembler
    prepared_pages = []
    for pg in ops.get("pages", []):
        prep = []
        for it in pg.get("items", []):
            prep.append([plan.pieces(s) for s in it.get("segments", [])] if it["type"] == "text" else None)
        prepared_pages.append(prep)

    for pg, prep in zip(ops.get("pages", []), prepared_pages):
        page = doc[pg["n"]]
        # Isoler le contenu d'origine (q ... Q) : certains PDF laissent une transformation ouverte
        page.wrap_contents()

        # 1. Effacer le texte d'origine des blocs modifiés / déplacés / supprimés
        covers = []
        redact = False
        for bid in pg.get("removeBlocks", []):
            sb = server.get(bid)
            if not sb:
                continue
            for r in sb["rects"]:
                R = pymupdf.Rect(r)
                h = R.height
                page.add_redact_annot(pymupdf.Rect(R.x0 + 0.3, R.y0 + h * 0.3, R.x1 - 0.3, R.y1 - h * 0.3), fill=False)
                redact = True
                if sb.get("bg"):
                    covers.append((R, sb["bg"]))
        if redact:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)

        # 2. Retirer les images déplacées / supprimées
        if pg.get("removeImages"):
            _remove_image_instances(doc, page, pg["removeImages"])

        # 3. Pages scannées : masquer l'ancienne ligne avec la couleur du papier
        for R, bg in covers:
            pad = R.height * 0.12
            page.draw_rect(pymupdf.Rect(R.x0 - 1, R.y0 - pad, R.x1 + 1, R.y1 + pad), color=None, fill=rgb(bg), width=0)

        # 4. Nouveaux éléments, dans l'ordre d'empilement
        for it, pieces in zip(pg.get("items", []), prep):
            t = it["type"]
            if t == "text":
                for cover in it.get("covers", []):
                    page.draw_rect(pymupdf.Rect(cover["rect"]), color=None, fill=rgb(cover["color"]), width=0)
                _write_text(page, it["segments"], plan, pieces)
            elif t == "image":
                rect = pymupdf.Rect(it["rect"])
                if it.get("xref"):
                    page.insert_image(rect, xref=int(it["xref"]), keep_proportion=False)
                else:
                    page.insert_image(rect, stream=_image_bytes(it["src"]), keep_proportion=False)
            elif t == "shape":
                _draw_shape(page, it)

        # Regrouper les nombreux petits flux ajoutés en un seul
        page.clean_contents()
        _dedupe_font_names(doc, page)

    _strip_metadata(doc)
    # Réécriture complète (aucune révision incrémentale conservée) avec un nouvel identifiant
    out = doc.tobytes(garbage=4, deflate=True, clean=True, no_new_id=False)
    # Commentaire "% Written by MuPDF x.y" en tête : effacé à longueur égale (les positions internes restent valides)
    head = out[:200]
    i = head.find(b"% Written by")
    if i >= 0:
        j = head.find(b"\n", i)
        out = out[:i] + b"%" + b" " * (j - i - 1) + out[j:]
    return out
