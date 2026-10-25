"""Gestion des polices.

Pour chaque police trouvée dans un PDF, on cherche à obtenir un fichier de police
COMPLET et utilisable à la fois par le navigateur (affichage dans l'éditeur) et par
PyMuPDF (écriture dans le PDF final). Ordre de préférence :

1. la même police installée sur le Mac (y compris les polices Office : Calibri, Aptos...)
2. la même police téléchargée depuis Google Fonts
3. la police embarquée dans le PDF, "réparée" (souvent un sous-ensemble : seules les
   lettres utilisées sont présentes)
4. une police de substitution proche (mêmes métriques quand c'est possible)

Chaque police du document a toujours une police "fallback" pour les caractères absents
de la police principale.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
from pathlib import Path

import logging

import pymupdf
import requests
from fontTools import agl
from fontTools.fontBuilder import FontBuilder
from fontTools.misc import psCharStrings
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

logging.getLogger("fontTools").setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
FONT_DIR = CACHE / "fonts"
GF_DIR = CACHE / "gfonts"
for _d in (FONT_DIR, GF_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SYSTEM_FONT_DIRS = [
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/Applications/Microsoft Word.app/Contents/Resources/DFonts",
    "/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts",
    "/Applications/Microsoft Excel.app/Contents/Resources/DFonts",
]

WEIGHT_WORDS = [
    ("extralight", 200), ("ultralight", 200), ("hairline", 100), ("thin", 100),
    ("semibold", 600), ("demibold", 600), ("extrabold", 800), ("ultrabold", 800),
    ("light", 300), ("book", 400), ("regular", 400), ("normal", 400), ("roman", 400),
    ("medium", 500), ("demi", 600), ("bold", 700), ("black", 900), ("heavy", 900),
]

# Substituts ayant (presque) les mêmes métriques que des polices courantes.
METRIC_ALIASES = {
    "arial": ["Arial", "Arimo"],
    "helvetica": ["Helvetica", "Arial", "Arimo"],
    "helveticaneue": ["Helvetica Neue", "Helvetica", "Arimo"],
    "timesnewroman": ["Times New Roman", "Tinos"],
    "times": ["Times", "Times New Roman", "Tinos"],
    "timesroman": ["Times New Roman", "Tinos"],
    "courier": ["Courier", "Courier New", "Cousine"],
    "couriernew": ["Courier New", "Cousine"],
    "calibri": ["Calibri", "Carlito"],
    "cambria": ["Cambria", "Caladea"],
    "segoeui": ["Segoe UI", "Open Sans"],
    "tahoma": ["Tahoma", "Verdana"],
    "symbol": ["Symbol"],
    "zapfdingbats": ["Zapf Dingbats"],
}

GENERIC = {"sans": ["Arial", "Helvetica", "Arimo"], "serif": ["Times New Roman", "Times", "Tinos"], "mono": ["Courier New", "Courier", "Cousine"]}

# Polices proposées dans la liste "Autres polices" de l'éditeur.
EXTRA_FAMILIES = [
    "Arial", "Helvetica", "Times New Roman", "Georgia", "Verdana", "Courier New",
    "Roboto", "Open Sans", "Lato", "Montserrat", "Poppins", "Inter", "Raleway",
    "Nunito", "Source Sans 3", "Merriweather", "Playfair Display", "PT Serif",
    "Libre Baskerville", "Oswald", "Dancing Script", "Great Vibes",
]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def strip_subset(name: str) -> str:
    return name.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", name or "") else (name or "")


def split_camel(s: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return s.strip()


def parse_font_name(raw: str, flags: int = 0) -> dict:
    """'BCDEEE+TimesNewRomanPS-BoldItalicMT' -> family 'Times New Roman', 700, italic."""
    name = strip_subset(raw).strip()
    parts = re.split(r"[-,]", name, maxsplit=1)
    fam = parts[0]
    style = parts[1] if len(parts) > 1 else ""
    fam = re.sub(r"(PSMT|PS|MT)$", "", fam)
    style_l = re.sub(r"(PSMT|PS|MT)$", "", style).lower()
    weight = None
    italic = bool(re.search(r"italic|oblique|(^|[^a-z])it$", style_l))
    for w, v in WEIGHT_WORDS:
        if w in style_l:
            weight = v
            break
    # Familles du type "ArialBold" sans séparateur
    if not style:
        m = re.match(r"^(.*?)(Bold|Italic|BoldItalic|Light|Medium|Black|Semibold|SemiBold)$", fam)
        if m and m.group(1):
            fam = m.group(1)
            st = m.group(2).lower()
            italic = italic or "italic" in st
            for w, v in WEIGHT_WORDS:
                if w in st:
                    weight = v
                    break
    if weight is None:
        weight = 700 if flags & 16 else 400
    if flags & 2:
        italic = True
    family = split_camel(fam) if " " not in fam else fam
    if family.endswith(" Roman") and norm(family) not in ("timesroman", "timesnewroman"):
        family = family[:-6]
    return {
        "ps": name,
        "family": family,
        "weight": weight,
        "italic": italic,
        "serif": bool(flags & 4) and not is_sans_name(family),
        "mono": bool(flags & 8),
    }


# Le bit "serif" des polices PDF est peu fiable (souvent posé sur des linéales) :
# on le contredit pour les familles linéales connues.
SANS_HINTS = ("sans", "myriad", "helvetica", "arial", "aptos", "calibri", "segoe", "verdana", "tahoma",
              "roboto", "inter", "futura", "gill", "frutiger", "univers", "avenir", "lato", "montserrat",
              "open", "source sans", "noto sans", "trebuchet", "century gothic", "gotham", "proxima",
              "sf pro", "san francisco", "lucida grande", "candara", "corbel", "nunito", "poppins")


def is_sans_name(family: str) -> bool:
    f = family.lower()
    return any(h in f for h in SANS_HINTS)


# ---------------------------------------------------------------------------
# Registre des fichiers de police (cache disque)
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_REG_PATH = FONT_DIR / "registry.json"
try:
    _registry: dict = json.loads(_REG_PATH.read_text())
except Exception:
    _registry = {}


def _save_registry():
    _REG_PATH.write_text(json.dumps(_registry))


def register_font(data: bytes, info: dict) -> str:
    """Stocke un fichier de police, retourne son identifiant."""
    fid = hashlib.sha1(data).hexdigest()[:16]
    with _lock:
        path = FONT_DIR / f"{fid}.bin"
        if not path.exists():
            path.write_bytes(data)
        if fid not in _registry:
            _registry[fid] = info
            _save_registry()
    return fid


def font_bytes(fid: str) -> bytes:
    return (FONT_DIR / f"{fid}.bin").read_bytes()


def font_info(fid: str) -> dict:
    return _registry.get(fid, {})


_fitz_cache: dict = {}


def fitz_font(fid: str) -> pymupdf.Font:
    f = _fitz_cache.get(fid)
    if f is None:
        f = pymupdf.Font(fontbuffer=font_bytes(fid))
        _fitz_cache[fid] = f
    return f


# ---------------------------------------------------------------------------
# Index des polices installées sur le Mac
# ---------------------------------------------------------------------------

_sys_index = None


def _scan_system_fonts() -> list:
    entries = []
    for d in SYSTEM_FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            ext = fn.lower().rsplit(".", 1)[-1]
            if ext not in ("ttf", "otf", "ttc"):
                continue
            path = os.path.join(d, fn)
            try:
                count = 1
                if ext == "ttc":
                    from fontTools.ttLib import TTCollection
                    count = len(TTCollection(path, lazy=True).fonts)
                for i in range(count):
                    f = TTFont(path, fontNumber=i, lazy=True)
                    name = f["name"]
                    ps = name.getDebugName(6) or ""
                    fam = name.getDebugName(16) or name.getDebugName(1) or ""
                    sub = name.getDebugName(17) or name.getDebugName(2) or ""
                    weight, italic = 400, False
                    if "OS/2" in f:
                        os2 = f["OS/2"]
                        weight = os2.usWeightClass
                        italic = bool(os2.fsSelection & 1)
                    if "italic" in sub.lower() or "oblique" in sub.lower():
                        italic = True
                    if "fvar" in f:
                        # Police variable : on ne retient que l'instance par défaut
                        pass
                    entries.append({"path": path, "index": i, "ps": ps, "family": fam,
                                    "sub": sub, "weight": weight, "italic": italic})
                    f.close()
            except Exception:
                continue
    return entries


def system_index() -> list:
    global _sys_index
    with _lock:
        if _sys_index is not None:
            return _sys_index
        cache = CACHE / "system_fonts.json"
        sig = "|".join(f"{d}:{os.path.getmtime(d) if os.path.isdir(d) else 0}" for d in SYSTEM_FONT_DIRS)
        try:
            data = json.loads(cache.read_text())
            if data.get("sig") == sig:
                _sys_index = data["entries"]
                return _sys_index
        except Exception:
            pass
        _sys_index = _scan_system_fonts()
        cache.write_text(json.dumps({"sig": sig, "entries": _sys_index}))
        return _sys_index


def _load_system_font(e: dict) -> bytes | None:
    try:
        if e["path"].lower().endswith(".ttc"):
            f = TTFont(e["path"], fontNumber=e["index"])
            buf = io.BytesIO()
            f.save(buf)
            return buf.getvalue()
        return Path(e["path"]).read_bytes()
    except Exception:
        return None


def _best_by_style(cands: list, weight: int, italic: bool):
    if not cands:
        return None
    def score(e):
        return abs(e["weight"] - weight) + (0 if e["italic"] == italic else 1000)
    best = min(cands, key=score)
    if score(best) >= 1000 or abs(best["weight"] - weight) > 150:
        return None
    return best


def find_system_font(family: str, weight: int, italic: bool, ps: str | None = None):
    idx = system_index()
    if ps:
        p = norm(ps)
        for e in idx:
            if norm(e["ps"]) == p:
                return e
    f = norm(family)
    cands = [e for e in idx if norm(e["family"]) == f]
    return _best_by_style(cands, weight, italic)


# ---------------------------------------------------------------------------
# Google Fonts
# ---------------------------------------------------------------------------

def find_google_font(family: str, weight: int, italic: bool) -> bytes | None:
    key = f"{norm(family)}_{weight}_{int(italic)}"
    path = GF_DIR / f"{key}.ttf"
    miss = GF_DIR / f"{key}.miss"
    if path.exists():
        return path.read_bytes()
    if miss.exists():
        return None
    fam_q = family.strip().replace(" ", "+")
    url = f"https://fonts.googleapis.com/css2?family={fam_q}:ital,wght@{int(italic)},{weight}"
    data = None
    try:
        r = requests.get(url, timeout=8)
        m = re.search(r"url\((https://[^)]+\.ttf)\)", r.text) if r.status_code == 200 else None
        if m:
            data = requests.get(m.group(1), timeout=15).content
    except Exception:
        return None  # pas de réseau : on n'enregistre pas d'échec définitif
    if data:
        path.write_bytes(data)
    else:
        miss.write_text("")
    return data


# ---------------------------------------------------------------------------
# Réparation des polices embarquées
# ---------------------------------------------------------------------------

def _cmap_table(mapping: dict) -> object:
    c = newTable("cmap")
    c.tableVersion = 0
    c.tables = []
    bmp = {u: g for u, g in mapping.items() if u < 0x10000}
    for plat, enc in ((0, 3), (3, 1)):
        st = CmapSubtable.newSubtable(4)
        st.platformID, st.platEncID, st.language = plat, enc, 0
        st.cmap = dict(bmp)
        c.tables.append(st)
    if any(u >= 0x10000 for u in mapping):
        st = CmapSubtable.newSubtable(12)
        st.platformID, st.platEncID, st.language = 3, 10, 0
        st.cmap = dict(mapping)
        c.tables.append(st)
    return c


def _finish_tables(font: TTFont, family: str, upem: int):
    """Ajoute les tables obligatoires manquantes pour que le navigateur accepte la police."""
    fb = FontBuilder(font=font)
    hhea = font["hhea"] if "hhea" in font else None
    asc = hhea.ascent if hhea else int(upem * 0.8)
    desc = hhea.descent if hhea else -int(upem * 0.2)
    if hhea is None:
        fb.setupHorizontalHeader(ascent=asc, descent=desc)
    psname = re.sub(r"[^A-Za-z0-9-]", "", family)[:60] or "EmbeddedFont"
    fb.setupNameTable({"familyName": family[:60] or "Embedded", "styleName": "Regular", "psName": psname,
                       "uniqueFontIdentifier": psname, "fullName": family[:60] or "Embedded"})
    if "OS/2" not in font:
        head = font["head"]
        fb.setupOS2(sTypoAscender=asc, sTypoDescender=desc, sTypoLineGap=0,
                    usWinAscent=max(asc, head.yMax, 0), usWinDescent=max(-desc, -head.yMin, 0),
                    sxHeight=int(upem * 0.5), sCapHeight=int(upem * 0.7), achVendID="PDFE")
    else:
        os2 = font["OS/2"]
        if os2.version < 1:
            os2.version = 1
            os2.ulCodePageRange1 = 1
            os2.ulCodePageRange2 = 0
    font["post"] = newTable("post")
    post = font["post"]
    post.formatType = 3.0
    post.italicAngle = 0
    post.underlinePosition = -int(upem * 0.1)
    post.underlineThickness = int(upem * 0.05)
    post.isFixedPitch = 0
    post.minMemType42 = post.maxMemType42 = post.minMemType1 = post.maxMemType1 = 0


def _repair_ttf(buf: bytes, uni2gid: dict, family: str) -> bytes | None:
    font = TTFont(io.BytesIO(buf))
    order = font.getGlyphOrder()
    mapping = {}
    if "cmap" in font:
        for t in font["cmap"].tables:
            if t.isUnicode():
                mapping.update(t.cmap)
    for u, g in uni2gid.items():
        if 0 < g < len(order):
            mapping[u] = order[g]
    # Ne garder que les caractères dont le dessin existe vraiment (sous-ensembles "vidés")
    if "glyf" in font:
        glyf = font["glyf"]
        mapping = {u: g for u, g in mapping.items()
                   if chr(u).isspace() or u in (0xA0, 0x200B) or glyf[g].numberOfContours != 0}
    if not mapping:
        return None
    for tag in ("fpgm", "prep", "cvt ", "hdmx", "LTSH", "VDMX", "kern", "gasp", "DSIG", "name", "cmap"):
        if tag in font:
            del font[tag]
    font["cmap"] = _cmap_table(mapping)
    _finish_tables(font, family, font["head"].unitsPerEm)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def _repair_cff(buf: bytes, uni2gid: dict, family: str) -> bytes | None:
    from fontTools.cffLib import CFFFontSet
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(buf), None)
    top = cff[cff.fontNames[0]]
    order = list(top.charset)
    fm = getattr(top, "FontMatrix", [0.001])
    upem = int(round(1 / fm[0])) if fm and fm[0] else 1000
    mapping = {}
    if not hasattr(top, "ROS"):
        for g in order:
            u = agl.toUnicode(g)
            if len(u) == 1:
                mapping[ord(u)] = g
    for u, g in uni2gid.items():
        if 0 < g < len(order):
            mapping[u] = order[g]
    if not mapping:
        return None
    metrics = {}
    cs_all = top.CharStrings
    for g in order:
        cs = cs_all[g]
        priv = cs.private
        ext = psCharStrings.T2WidthExtractor(getattr(priv, "Subrs", []), cs.globalSubrs,
                                             getattr(priv, "nominalWidthX", 0), getattr(priv, "defaultWidthX", 0))
        try:
            ext.execute(cs)
            w = int(round(ext.width))
        except Exception:
            w = 0
        metrics[g] = (w, 0)
    fb = FontBuilder(upem, isTTF=False)
    fb.font.sfntVersion = "OTTO"
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(mapping)
    t = newTable("CFF ")
    t.cff = cff
    fb.font["CFF "] = t
    fb.setupHorizontalMetrics(metrics)
    bbox = getattr(top, "FontBBox", [0, -200, 1000, 800])
    asc = int(bbox[3]) if bbox[3] > 0 else int(upem * 0.8)
    desc = int(bbox[1]) if bbox[1] < 0 else -int(upem * 0.2)
    fb.setupHorizontalHeader(ascent=asc, descent=desc)
    fb.setupMaxp()
    fb.setupPost()
    fb.setupOS2(sTypoAscender=asc, sTypoDescender=desc, usWinAscent=asc, usWinDescent=-desc, achVendID="PDFE")
    psname = re.sub(r"[^A-Za-z0-9-]", "", family)[:60] or "EmbeddedFont"
    fb.setupNameTable({"familyName": family[:60] or "Embedded", "styleName": "Regular", "psName": psname})
    out = io.BytesIO()
    fb.font.save(out)
    return out.getvalue()


def repair_embedded(buf: bytes, ext: str, uni2gid: dict, family: str) -> bytes | None:
    try:
        if ext in ("ttf", "otf"):
            # Un "otf" embarqué peut contenir du CFF dans une enveloppe OpenType
            f = TTFont(io.BytesIO(buf))
            if "CFF " in f and "glyf" not in f:
                return _repair_otf(f, uni2gid, family)
            return _repair_ttf(buf, uni2gid, family)
        if ext == "cff":
            return _repair_cff(buf, uni2gid, family)
    except Exception as e:  # police embarquée inutilisable
        print("repair_embedded failed:", family, ext, e)
    return None


def _repair_otf(font: TTFont, uni2gid: dict, family: str) -> bytes | None:
    order = font.getGlyphOrder()
    mapping = {}
    if "cmap" in font:
        for t in font["cmap"].tables:
            if t.isUnicode():
                mapping.update(t.cmap)
    for u, g in uni2gid.items():
        if 0 < g < len(order):
            mapping[u] = order[g]
    if not mapping:
        return None
    for tag in ("name", "cmap", "DSIG"):
        if tag in font:
            del font[tag]
    font["cmap"] = _cmap_table(mapping)
    _finish_tables(font, family, font["head"].unitsPerEm)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def font_has_glyphs(data: bytes) -> bool:
    try:
        f = pymupdf.Font(fontbuffer=data)
        return f.glyph_count > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Résolution
# ---------------------------------------------------------------------------

def _register_system(e: dict) -> str | None:
    data = _load_system_font(e)
    if not data:
        return None
    return register_font(data, {"source": "system", "family": e["family"], "ps": e["ps"],
                                "weight": e["weight"], "italic": e["italic"]})


def resolve_full(family: str, weight: int, italic: bool, ps: str | None = None) -> str | None:
    """Police complète portant ce nom (Mac puis Google Fonts)."""
    e = find_system_font(family, weight, italic, ps)
    if e:
        fid = _register_system(e)
        if fid:
            return fid
    data = find_google_font(family, weight, italic)
    if data:
        return register_font(data, {"source": "google", "family": family, "weight": weight, "italic": italic})
    # "MontserratRoman", "Inter Display"... : on retente sans le dernier mot
    words = family.split()
    if len(words) > 1 and words[-1].lower() in ("roman", "regular", "std", "pro", "lt", "display", "text"):
        return resolve_full(" ".join(words[:-1]), weight, italic)
    return None


def resolve_substitute(info: dict) -> str:
    """Police de substitution, toujours disponible."""
    fam = norm(info.get("family", ""))
    names = list(METRIC_ALIASES.get(fam, []))
    if info.get("mono") or "mono" in fam or "courier" in fam:
        names += GENERIC["mono"]
    elif "sans" not in fam and (info.get("serif") or "serif" in fam or fam in ("georgia", "garamond", "baskerville")):
        names += GENERIC["serif"]
    else:
        names += GENERIC["sans"]
    w, it = info.get("weight", 400), info.get("italic", False)
    for n in names:
        fid = resolve_full(n, w, it)
        if fid:
            return fid
    for n in names:
        fid = resolve_full(n, 700 if w >= 600 else 400, it) or resolve_full(n, 400, False)
        if fid:
            return fid
    # Dernier recours : police intégrée à PyMuPDF
    key = "hebo" if w >= 600 else "helv"
    data = pymupdf.Font(key).buffer
    return register_font(data, {"source": "builtin", "family": "Helvetica", "weight": w, "italic": False})


def label_for(info: dict) -> str:
    w = info.get("weight", 400)
    style = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "", 500: "Medium", 600: "SemiBold",
             700: "Bold", 800: "ExtraBold", 900: "Black"}.get(int(round(w / 100) * 100), "")
    if info.get("italic"):
        style = (style + " Italic").strip()
    return (info.get("family", "Police") + (" " + style if style else "")).strip()
