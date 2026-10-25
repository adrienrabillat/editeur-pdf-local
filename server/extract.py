"""Analyse d'un PDF : polices, blocs de texte (paragraphes) et images de chaque page."""

from __future__ import annotations

import re
import statistics

import pymupdf

from . import fonts as F

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP


def hex_color(c) -> str:
    if isinstance(c, int):
        return "#%06x" % c
    r, g, b = (list(c) + [0, 0, 0])[:3]
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


class DocFonts:
    """Polices utilisées par un document, identifiées par un id court ("d0", "d1"...)."""

    def __init__(self, doc: pymupdf.Document, store: dict | None = None):
        self.doc = doc
        self.fonts: dict = (store or {}).get("fonts", {})
        self.by_key: dict = (store or {}).get("by_key", {})
        self._xrefs: dict = {}
        self._uni2gid: dict = {}

    # -- collecte --------------------------------------------------------
    def scan_page(self, page: pymupdf.Page, trace: list):
        for f in page.get_fonts(full=True):
            xref, ext, ftype, basefont = f[0], f[1], f[2], f[3]
            if ftype == "Type3":
                key = f"Type3 ({xref} 0 R)"
                real = self._type3_name(xref)
            else:
                key = F.strip_subset(basefont)
                real = basefont
            lst = self._xrefs.setdefault(key, [])
            if not any(x[0] == xref for x in lst):
                lst.append((xref, ext, ftype, real))
        for s in trace:
            m = self._uni2gid.setdefault(s["font"], {})
            for ch in s["chars"]:
                u, gid = ch[0], ch[1]
                if u >= 32 and gid > 0 and u != 0xFFFD:
                    m.setdefault(u, gid)

    def _type3_name(self, xref: int) -> str:
        try:
            fd = self.doc.xref_get_key(xref, "FontDescriptor")
            if fd[0] == "xref":
                n = self.doc.xref_get_key(int(fd[1].split()[0]), "FontName")
                if n[0] == "name":
                    return n[1].lstrip("/")
        except Exception:
            pass
        return ""

    # -- résolution --------------------------------------------------------
    def get(self, span_font: str, flags: int) -> str:
        key = f"{span_font}|{flags & (16 | 2 | 4 | 8)}"
        if key in self.by_key:
            return self.by_key[key]
        cands = self._xrefs.get(span_font, [])
        real = cands[0][3] if cands else span_font
        if real.startswith("Type3 ("):
            real = ""
        info = F.parse_font_name(real or "Unknown", flags)
        if not real:
            info["family"] = "Sans" if not info["serif"] else "Serif"

        # Même police déjà connue (même nom, même style) ?
        for fid, df in self.fonts.items():
            if df["ps"] == info["ps"] and real and df["weight"] == info["weight"] and df["italic"] == info["italic"]:
                self.by_key[key] = fid
                return fid

        primary, source = None, "substitute"
        if real:
            primary = F.resolve_full(info["family"], info["weight"], info["italic"], ps=info["ps"])
            if primary:
                source = F.font_info(primary).get("source", "system")
        if not primary:
            emb = self._embedded(span_font, info["family"])
            if emb:
                primary, source = emb, "embedded"
        if not primary:
            # Sous-ensemble inexploitable : on reprend une variante intégrée de la même famille
            sib = self._embedded_sibling(info)
            if sib:
                primary, source = sib, "embedded"
        fallback = F.resolve_substitute(info)
        if not primary:
            primary = fallback
        did = self.add(info, primary, fallback, source)
        self.by_key[key] = did
        return did

    def _embedded_sibling(self, info: dict) -> str | None:
        nf = F.norm(info["family"])
        same = [df for df in self.fonts.values() if df["source"] == "embedded" and F.norm(df["family"]) == nf]
        if not same:
            return None
        best = min(same, key=lambda df: abs(df["weight"] - info["weight"]) + (0 if df["italic"] == info["italic"] else 500))
        return best["primary"]

    def _embedded(self, span_font: str, family: str) -> str | None:
        cands = [c for c in self._xrefs.get(span_font, []) if c[1] in ("ttf", "otf", "cff")]
        if not cands:
            return None
        # Plusieurs sous-ensembles du même nom : les numéros de glyphes ne sont pas fiables
        uni2gid = self._uni2gid.get(span_font, {}) if len(cands) == 1 else {}
        best = None
        for xref, ext, _t, _r in cands:
            try:
                _n, ext2, _st, buf = self.doc.extract_font(xref)
            except Exception:
                continue
            if buf and (best is None or len(buf) > len(best[1])):
                best = (ext2 or ext, buf)
        if not best:
            return None
        data = F.repair_embedded(best[1], best[0], uni2gid, family)
        if not data or not F.font_has_glyphs(data):
            return None
        return F.register_font(data, {"source": "embedded", "family": family})

    def add(self, info: dict, primary: str, fallback: str, source: str) -> str:
        did = f"d{len(self.fonts)}"
        self.fonts[did] = {
            "id": did, "label": F.label_for(info), "family": info["family"], "ps": info.get("ps", ""),
            "weight": info["weight"], "italic": info["italic"], "serif": info.get("serif", False),
            "primary": primary, "fallback": fallback, "source": source,
        }
        return did

    def variant(self, family: str, weight: int, italic: bool) -> str:
        """Police d'une famille donnée dans un style donné (gras / italique / autre police).
        Si le style exact n'existe pas, on prend le plus proche dans la même famille."""
        nf = F.norm(family)
        tries = [(weight, italic), (weight, False), (400, italic), (400, False), (700, False)]
        tries = list(dict.fromkeys(tries))
        same = [df for df in self.fonts.values() if F.norm(df["family"]) == nf]
        serif = any(df.get("serif") for df in same)
        for w, it in tries:
            for df in same:
                if df["weight"] == w and df["italic"] == it and (df["source"] != "substitute" or (w, it) == tries[0]):
                    return df["id"]
            primary = F.resolve_full(family, w, it)
            if primary:
                info = {"family": family, "weight": w, "italic": it, "ps": "", "serif": serif}
                return self.add(info, primary, F.resolve_substitute(info), F.font_info(primary).get("source", "system"))
        if same:
            # Police connue seulement par sa version embarquée : on garde la même
            return min(same, key=lambda df: abs(df["weight"] - weight) + (0 if df["italic"] == italic else 500))["id"]
        info = {"family": family, "weight": weight, "italic": italic, "ps": "", "serif": serif}
        sub = F.resolve_substitute(info)
        return self.add(info, sub, sub, "substitute")

    def to_json(self) -> dict:
        return {"fonts": self.fonts, "by_key": self.by_key}


# ---------------------------------------------------------------------------
# Blocs de texte
# ---------------------------------------------------------------------------

class Piece:
    """Morceau de ligne (une ligne, ou une partie de ligne séparée par un grand espace)."""

    def __init__(self, chars: list, bi: int, invisible: bool):
        self.chars = chars  # [(char_dict, span_dict)]
        self.bi = bi
        self.invisible = invisible
        self.x0 = chars[0][0]["bbox"][0]
        self.x1 = chars[-1][0]["bbox"][2]
        self.y0 = min(c[0]["bbox"][1] for c in chars)
        self.y1 = max(c[0]["bbox"][3] for c in chars)
        big = max(chars, key=lambda c: c[1]["size"])
        self.size = big[1]["size"]
        self.baseline = big[0]["origin"][1]

    @property
    def width(self):
        return self.x1 - self.x0

    def first_word_width(self) -> float:
        x_end = self.x1
        for ch, _s in self.chars:
            if ch["c"].isspace():
                x_end = ch["bbox"][0]
                break
        return x_end - self.x0


def _split_line(line: dict) -> list:
    chars = [(ch, s) for s in line["spans"] for ch in s["chars"]]
    pieces, cur = [], []
    for ch, s in chars:
        if cur:
            pch, ps = cur[-1]
            gap = ch["bbox"][0] - pch["bbox"][2]
            if gap > 1.8 * max(s["size"], ps["size"]) or gap < -2 * s["size"]:
                pieces.append(cur)
                cur = []
        cur.append((ch, s))
    if cur:
        pieces.append(cur)
    out = []
    for p in pieces:
        while p and p[0][0]["c"].isspace():
            p = p[1:]
        while p and p[-1][0]["c"].isspace():
            p = p[:-1]
        if p:
            out.append(p)
    return out


def _letter_spacing(chars: list, ffont) -> float:
    vals = []
    for (a, sa), (b, _sb) in zip(chars, chars[1:]):
        if a["c"].isspace() or b["c"].isspace():
            continue
        try:
            adv = ffont.glyph_advance(ord(a["c"])) * sa["size"]
        except Exception:
            return 0.0
        vals.append(b["origin"][0] - a["origin"][0] - adv)
    if len(vals) < 3:
        return 0.0
    ls = statistics.fmean(vals)
    return round(ls, 3) if abs(ls) > 0.02 else 0.0


def _runs(chars: list, fonts: DocFonts) -> list:
    runs = []
    for ch, s in chars:
        fid = fonts.get(s["font"], s["flags"])
        col = hex_color(s["color"])
        size = round(s["size"], 2)
        c = ch["c"]
        if c in (" ",):
            c = " "
        if runs and runs[-1]["f"] == fid and runs[-1]["s"] == size and runs[-1]["c"] == col:
            runs[-1]["t"] += c
            runs[-1]["_chars"].append((ch, s))
        else:
            runs.append({"t": c, "f": fid, "s": size, "c": col, "_chars": [(ch, s)]})
    for r in runs:
        df = fonts.fonts[r["f"]]
        ls = 0.0
        if df["source"] != "substitute":
            try:
                ls = _letter_spacing(r["_chars"], F.fitz_font(df["primary"]))
            except Exception:
                ls = 0.0
        if ls:
            r["ls"] = ls
        del r["_chars"]
    return runs


def _natural(runs: list, fonts: DocFonts, ws: float = 0.0) -> float:
    """Largeur du texte composé avec nos polices (celle que l'éditeur affichera)."""
    w = 0.0
    for r in runs:
        df = fonts.fonts[r["f"]]
        pf, ff = F.fitz_font(df["primary"]), F.fitz_font(df["fallback"])
        ls = r.get("ls") or 0
        for ch in r["t"]:
            f = pf if pf.has_glyph(ord(ch)) or not ff.has_glyph(ord(ch)) else ff
            w += f.glyph_advance(ord(ch)) * r["s"] + ls + (ws if ch == " " else 0)
    return w


def _word_spacing(lines: list, line_runs: list, fonts: DocFonts) -> float:
    """Écart moyen des espaces par rapport à leur largeur normale (Word, LaTeX... les ajustent)."""
    vals = []
    for p, runs in zip(lines, line_runs):
        ls = statistics.fmean([r.get("ls") or 0 for r in runs]) if runs else 0
        for (a, sa), (b, _sb) in zip(p.chars, p.chars[1:]):
            if a["c"] != " " or b["c"].isspace():
                continue
            fid = fonts.get(sa["font"], sa["flags"])
            df = fonts.fonts[fid]
            if df["source"] == "substitute":
                return 0.0
            adv = F.fitz_font(df["primary"]).glyph_advance(32) * sa["size"]
            vals.append(b["origin"][0] - a["origin"][0] - adv - ls)
    if len(vals) < 2:
        return 0.0
    ws = statistics.fmean(vals)
    return round(ws, 3) if 0.03 < abs(ws) < lines[0].size * 0.5 else 0.0


def _first_word(runs: list) -> list:
    out = []
    for r in runs:
        i = r["t"].find(" ")
        if i < 0:
            out.append(r)
        else:
            out.append({**r, "t": r["t"][:i]})
            break
    return out


def _group(pieces: list) -> list:
    pieces.sort(key=lambda p: (p.baseline, p.x0))
    groups = []
    for p in pieces:
        target = None
        for g in reversed(groups):
            last = g["lines"][-1]
            if g["bi"] != p.bi or g["inv"] != p.invisible:
                continue
            dy = p.baseline - last.baseline
            big = max(p.size, last.size)
            if dy <= 0.3 * big or dy > 1.75 * big:
                continue
            if abs(p.size - last.size) > 0.2 * big:
                continue
            if g["lh"] and abs(dy - g["lh"]) > 0.25 * big:
                continue
            ov = min(p.x1, g["x1"]) - max(p.x0, g["x0"])
            if ov < 0.3 * min(p.width, g["x1"] - g["x0"]):
                continue
            target = g
            break
        if target:
            if not target["lh"]:
                target["lh"] = p.baseline - target["lines"][-1].baseline
            target["lines"].append(p)
            target["x0"] = min(target["x0"], p.x0)
            target["x1"] = max(target["x1"], p.x1)
        else:
            groups.append({"bi": p.bi, "inv": p.invisible, "lines": [p], "x0": p.x0, "x1": p.x1, "lh": None})
    return groups


def page_text_blocks(page: pymupdf.Page, pno: int, fonts: DocFonts, trace: list) -> tuple[list, dict]:
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    invisible = [pymupdf.Rect(s["bbox"]) for s in trace if s.get("type") == 3 or s.get("opacity", 1) == 0]

    def is_invisible(bbox):
        r = pymupdf.Rect(bbox)
        c = (r.tl + r.br) / 2
        return any(c in ir for ir in invisible)

    pieces = []
    for bi, b in enumerate(raw["blocks"]):
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            d = line["dir"]
            if abs(d[1]) > 0.01 or d[0] < 0:
                continue  # texte vertical / tourné : non éditable
            for chars in _split_line(line):
                pieces.append(Piece(chars, bi, is_invisible(line["bbox"])))
    if not pieces:
        return [], {}

    vis = [p for p in pieces if not p.invisible] or pieces
    lm = min(p.x0 for p in vis)
    rm = max(p.x1 for p in vis)
    pw = page.rect.width

    groups = _group(pieces)

    def right_limit(g):
        """Abscisse jusqu'où le texte du bloc pouvait s'étendre (prochain texte à droite, sinon la marge)."""
        y0 = min(p.y0 for p in g["lines"])
        y1 = max(p.y1 for p in g["lines"])
        xs = [p.x0 for h in groups if h is not g for p in h["lines"]
              if p.x0 >= g["x1"] - 1 and p.y1 > y0 + 1 and p.y0 < y1 - 1]
        return (min(xs) - 4) if xs else max(rm, g["x1"])

    blocks, server = [], {}
    for gi, g in enumerate(groups):
        lines = g["lines"]
        x0, x1 = g["x0"], g["x1"]
        n = len(lines)
        limit = right_limit(g)
        tol = max(1.5, 0.15 * lines[0].size)

        # Retours à la ligne "souples" (le mot suivant ne tenait pas) ou "durs".
        # La largeur de composition est x1 si au moins deux lignes l'atteignent, sinon la limite à droite.
        soft = []
        tol_w = max(tol, 0.12 * (x1 - x0))
        confirmed = sum(1 for p in lines if abs(p.x1 - x1) < tol_w) >= 2
        width_end = x1 if confirmed else limit
        for a, b in zip(lines, lines[1:]):
            space = a.size * 0.28
            need = a.x1 + space + b.first_word_width()
            is_soft = need > width_end + 0.5 or a.chars[-1][0]["c"] in "-\u00ad"
            soft.append(is_soft)

        left_al = all(abs(p.x0 - x0) < tol for p in lines[1:]) if n > 1 else True
        right_al = all(abs(p.x1 - x1) < tol for p in lines) if n > 1 else False
        cx = (x0 + x1) / 2
        centered = all(abs((p.x0 + p.x1) / 2 - cx) < tol for p in lines) if n > 1 else False
        indent = 0.0
        if n > 1 and left_al and lines[0].x0 - x0 > tol and lines[0].x0 - x0 < 10 * lines[0].size:
            indent = round(lines[0].x0 - x0, 2)

        align = "left"
        if n > 1:
            body = lines[:-1]
            if len(body) >= 1 and all(abs(p.x1 - x1) < tol for p in body) and left_al and any(soft):
                align = "justify"
            elif left_al:
                align = "left"
            elif right_al:
                align = "right"
            elif centered:
                align = "center"
        else:
            p = lines[0]
            if abs(p.x1 - rm) < 2 and p.x0 - lm > 0.3 * (rm - lm):
                align = "right"
            elif (abs((p.x0 + p.x1) / 2 - pw / 2) < 3 or abs((p.x0 + p.x1) / 2 - (lm + rm) / 2) < 3) and p.x0 - lm > 20:
                align = "center"

        # Boîte de mise en page. Pour un paragraphe, on cherche la largeur qui reproduit exactement
        # les mêmes retours à la ligne avec nos polices : chaque ligne doit tenir, et le premier mot
        # de la ligne suivante ne doit pas tenir.
        line_runs = [_runs(p.chars, fonts) for p in lines]
        # Espacement des mots mesuré sur les lignes non étirées par la justification
        natural_lines = [i for i in range(n) if align != "justify" or i == n - 1 or not soft[i]]
        ws = _word_spacing([lines[i] for i in natural_lines], [line_runs[i] for i in natural_lines], fonts)
        nat = [_natural(r, fonts, ws) + (indent if i == 0 else 0) for i, r in enumerate(line_runs)]
        if any(soft):
            lo = max(nat) + 0.15
            his = []
            for i, is_soft in enumerate(soft):
                if not is_soft or not line_runs[i] or not line_runs[i + 1]:
                    continue
                last = line_runs[i][-1]
                sp = 0.0 if last["t"].endswith("-") else _natural([{**last, "t": " "}], fonts, ws)
                his.append(nat[i] + sp + _natural(_first_word(line_runs[i + 1]), fonts) - 0.15)
            w = x1 - x0
            hi = min(his) if his else w + 2
            w = min(max(w, lo), hi) if lo <= hi else lo
            w = round(w, 2)
            box = {"anchor": "left", "x": x0, "w": w, "maxW": None}
            if align == "center":
                box = {"anchor": "center", "x": cx, "w": w, "maxW": None}
            elif align == "right":
                box = {"anchor": "right", "x": x1, "w": w, "maxW": None}
        else:
            need = max(max(nat), x1 - x0) + 1
            if align == "right":
                box = {"anchor": "right", "x": x1, "w": None, "maxW": round(max(x1 - lm, need), 2)}
            elif align == "center":
                half = max(min(cx - lm, rm - cx), need / 2)
                box = {"anchor": "center", "x": cx, "w": None, "maxW": round(2 * half, 2)}
            else:
                box = {"anchor": "left", "x": x0, "w": None, "maxW": round(max(limit - x0, need), 2)}

        paras = [[]]
        for i, p in enumerate(lines):
            runs = [dict(r) for r in line_runs[i]]
            if i > 0:
                if soft[i - 1]:
                    prev = paras[-1][-1] if paras[-1] else None
                    if prev and not prev["t"].endswith("-"):
                        prev["t"] += " "
                else:
                    paras.append([])
            for r in runs:
                cur = paras[-1]
                if cur and cur[-1]["f"] == r["f"] and cur[-1]["s"] == r["s"] and cur[-1]["c"] == r["c"] and cur[-1].get("ls") == r.get("ls"):
                    cur[-1]["t"] += r["t"]
                else:
                    cur.append(r)

        sizes = [r["s"] for para in paras for r in para for _ in range(len(r["t"]))]
        dom_size = statistics.mode(sizes) if sizes else lines[0].size
        bid = f"p{pno}b{gi}"
        bbox = [min(p.x0 for p in lines), min(p.y0 for p in lines), max(p.x1 for p in lines), max(p.y1 for p in lines)]
        kind = "ocr" if g["inv"] else "text"
        blk = {
            "id": bid, "page": pno, "kind": kind, "bbox": [round(v, 2) for v in bbox],
            "base": round(lines[0].baseline, 2), "align": align, "indent": indent,
            "lh": round((lines[-1].baseline - lines[0].baseline) / (n - 1), 3) if n > 1 else None,
            "ws": ws, "size": dom_size, "box": box, "paras": paras,
        }
        server[bid] = {"page": pno, "kind": kind, "rects": [[p.x0, p.y0, p.x1, p.y1] for p in lines]}
        blocks.append(blk)
    return blocks, server


def page_images(page: pymupdf.Page, pno: int) -> list:
    area = page.rect.width * page.rect.height
    names = {}
    for it in page.get_images(full=True):
        names[it[0]] = (it[7], it[9])
    out, count = [], {}
    for info in page.get_image_info(xrefs=True):
        xref = info.get("xref", 0)
        k = count.get(xref, 0)
        count[xref] = k + 1
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if r.is_empty or r.width < 4 or r.height < 4:
            continue
        t = info["transform"]
        axis = abs(t[1]) < 1e-3 and abs(t[2]) < 1e-3 and t[0] > 0 and t[3] > 0
        ref = names.get(xref, (None, -1))
        editable = xref > 0 and ref[1] == 0 and axis
        background = r.width * r.height > 0.85 * area
        out.append({"id": f"p{pno}i{len(out)}", "xref": xref, "k": k, "rect": [round(v, 2) for v in info["bbox"]],
                    "editable": bool(editable and not background), "background": background})
    return out


def analyze(doc: pymupdf.Document) -> dict:
    fonts = DocFonts(doc)
    traces = []
    for page in doc:
        tr = page.get_texttrace()
        traces.append(tr)
        fonts.scan_page(page, tr)
    pages, server = [], {}
    for pno, page in enumerate(doc):
        blocks, srv = page_text_blocks(page, pno, fonts, traces[pno])
        server.update(srv)
        images = page_images(page, pno)
        visible_chars = sum(len(s["chars"]) for s in traces[pno] if s.get("type") != 3)
        scanned = visible_chars < 15 and any(im["background"] for im in images)
        pages.append({"w": round(page.rect.width, 2), "h": round(page.rect.height, 2),
                      "blocks": blocks, "images": images, "scanned": scanned,
                      "ocrDone": any(b["kind"] == "ocr" for b in blocks)})
    return {"pages": pages, "server": server, "fonts": fonts.to_json()}
