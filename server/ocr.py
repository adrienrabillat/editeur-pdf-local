"""Reconnaissance de texte sur les pages scannées, avec le moteur OCR intégré à macOS (Apple Vision).

Chaque ligne reconnue devient un bloc éditable : on estime la police (avec ou sans empattements),
la taille, la couleur du texte et celle du papier, pour masquer proprement l'original et réécrire
par-dessus.
"""

from __future__ import annotations

import io
import math

import numpy as np
import pymupdf
from fontTools.ttLib import TTFont

from . import fonts as F

OCR_DPI = 220


def _vision_lines(png: bytes) -> list:
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(png, len(png))
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["fr-FR", "en-US", "de-DE", "es-ES", "it-IT"])
    req.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    handler.performRequests_error_([req], None)
    out = []
    for o in (req.results() or []):
        cand = o.topCandidates_(1)
        if not cand:
            continue
        c = cand[0]
        bb = o.boundingBox()
        out.append((str(c.string()), float(c.confidence()),
                    (bb.origin.x, bb.origin.y, bb.size.width, bb.size.height)))
    return out


def _measure(gray: np.ndarray, rgb: np.ndarray, box: tuple, scale: float, text: str):
    """Ligne de base, hauteur des lettres, largeur des mots, couleurs, à partir des pixels."""
    x0, y0, x1, y1 = box
    H, W = gray.shape
    pad = int((y1 - y0) * 0.15)
    xa, xb = max(0, int(x0) - 2), min(W, int(x1) + 3)
    ya, yb = max(0, int(y0) - pad), min(H, int(y1) + pad + 1)
    g = gray[ya:yb, xa:xb].astype(np.float32)
    if g.size == 0:
        return None
    bg = float(np.percentile(g, 80))
    ink = float(np.percentile(g, 3))
    if bg - ink < 30:
        return None
    mask = g < (bg + ink) / 2
    rows = mask.sum(axis=1)
    if rows.max() == 0:
        return None
    mx = rows.max()
    top = np.where(rows > 0.06 * mx)[0][0]
    base = np.where(rows > 0.3 * mx)[0][-1] + 1
    tall = any(ch.isupper() or ch.isdigit() or ch in "bdfhkl" for ch in text)

    # Mots : colonnes d'encre séparées par des espaces assez larges
    cols = mask[top:base].any(axis=0)
    xs = np.where(cols)[0]
    words_px = []
    if len(xs):
        min_gap = max(2, (base - top) * 0.22)
        start = prev = xs[0]
        for x in xs[1:]:
            if x - prev > min_gap:
                words_px.append((start, prev + 1))
                start = x
            prev = x
        words_px.append((start, prev + 1))
    words = text.split()
    word_widths = []
    if len(words) == len(words_px):
        word_widths = [(w, (b - a) / scale) for w, (a, b) in zip(words, words_px)]

    # Épaisseur des traits (pour détecter le gras) : longueur médiane des segments d'encre horizontaux
    runs = []
    for row in mask[top + (base - top) // 3: base - (base - top) // 6]:
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        runs.extend((ends - starts).tolist())
    stroke = float(np.median(runs)) / scale if runs else 0.0

    sub = rgb[ya:yb, xa:xb]
    # Couleur du texte : le cœur des lettres (les bords lissés tirent vers le papier)
    core = mask & (g <= np.percentile(g[mask], 35)) if mask.any() else mask
    ink_px = sub[core]
    bg_px = sub[~mask & (g > bg - 15)] if (~mask).any() else sub.reshape(-1, 3)
    col = np.median(ink_px, axis=0) if len(ink_px) else np.array([0, 0, 0])
    bgc = np.median(bg_px, axis=0) if len(bg_px) else np.array([255, 255, 255])
    return {
        "baseline": (ya + base) / scale,
        "ink_x0": (xa + xs[0]) / scale if len(xs) else x0 / scale,
        "height": (base - top) / scale,
        "tall": tall,
        "words": word_widths,
        "stroke": stroke,
        "color": "#%02x%02x%02x" % tuple(int(v) for v in col[:3]),
        "bg": "#%02x%02x%02x" % tuple(int(v) for v in bgc[:3]),
    }


def _ratios(fid: str) -> tuple[float, float]:
    """Hauteur des majuscules et hauteur d'x, en fraction de la taille du corps."""
    try:
        f = TTFont(io.BytesIO(F.font_bytes(fid)), lazy=True)
        upem = f["head"].unitsPerEm
        os2 = f["OS/2"]
        cap = getattr(os2, "sCapHeight", 0) or 0.7 * upem
        xh = getattr(os2, "sxHeight", 0) or 0.5 * upem
        # Les lettres montantes (d, l, h) dépassent un peu les capitales
        return cap / upem * 1.03, xh / upem
    except Exception:
        return 0.72, 0.5


def ocr_page(page: pymupdf.Page, pno: int, candidates: list) -> tuple[list, dict]:
    """candidates : [(police normale, fichier, police grasse, fichier)], ex. Arial et Times New Roman."""
    scale = OCR_DPI / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, colorspace=pymupdf.csRGB)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    gray = (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114).astype(np.uint8)
    lines = _vision_lines(pix.tobytes("png"))
    W, H = pix.width, pix.height
    pw = page.rect.width

    found = []
    for i, (text, conf, (bx, by, bw, bh)) in enumerate(lines):
        text = text.strip()
        if not text or conf < 0.3:
            continue
        x0, x1 = bx * W, (bx + bw) * W
        y0, y1 = (1 - by - bh) * H, (1 - by) * H
        m = _measure(gray, rgb, (x0, y0, x1, y1), scale, text)
        if not m or m["height"] < 2:
            continue
        found.append((i, text, m, [x0 / scale, y0 / scale, x1 / scale, y1 / scale]))
        found[-1][3][0] = min(found[-1][3][0], m["ink_x0"])

    def size_for(m, ratios):
        cap, xh = ratios
        return m["height"] / (cap if m["tall"] else xh)

    # Police la plus proche : celle dont la largeur des mots colle le mieux à l'image
    best, best_err = None, None
    for cand in candidates:
        ff = F.fitz_font(cand[1])
        ratios = _ratios(cand[1])
        err, n = 0.0, 0
        for _i, _text, m, _rect in found:
            size = size_for(m, ratios)
            for word, w in m["words"]:
                if len(word) >= 4 and w > 0:
                    err += abs(math.log(ff.text_length(word, fontsize=size) / w))
                    n += 1
        err = err / n if n else 0
        if best_err is None or err < best_err:
            best, best_err = cand, err
    ratios = _ratios(best[1])

    blocks, server = [], {}
    for i, text, m, rect in found:
        size = round(size_for(m, ratios) * 2) / 2
        if size < 4:
            continue
        bold = m["stroke"] / size > 0.125
        font_id, ffont = (best[2], F.fitz_font(best[3])) if bold else (best[0], F.fitz_font(best[1]))
        width = rect[2] - rect[0]
        # Espacement des lettres pour que la ligne garde sa longueur d'origine
        natural = ffont.text_length(text, fontsize=size)
        ls = 0.0
        if len(text) > 1 and natural > 0:
            ls = round((width - natural) / (len(text) - 1), 2)
            if abs(ls) < 0.1 or abs(ls) > size * 0.3:
                ls = 0.0
        run = {"t": text, "f": font_id, "s": size, "c": m["color"]}
        if ls:
            run["ls"] = ls
        bid = f"p{pno}o{i}"
        blocks.append({
            "id": bid, "page": pno, "kind": "ocr", "bbox": [round(v, 2) for v in rect],
            "base": round(m["baseline"], 2), "align": "left", "indent": 0, "lh": None, "size": size,
            "box": {"anchor": "left", "x": m["ink_x0"], "w": None, "maxW": round(max(pw - 30 - rect[0], width + 2), 2)},
            "paras": [[run]], "bg": m["bg"],
        })
        server[bid] = {"page": pno, "kind": "ocr", "rects": [rect], "bg": m["bg"]}
    return blocks, server
