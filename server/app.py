"""Serveur local de l'éditeur PDF."""

from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pymupdf
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import fonts as F
from .export import export_pdf
from .extract import DocFonts, analyze

ROOT = Path(__file__).resolve().parent.parent
DOCS = F.CACHE / "docs"
DOCS.mkdir(parents=True, exist_ok=True)
STATIC = ROOT / "static"

app = FastAPI(title="PDF Edit")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_lock = threading.RLock()
_open_docs: dict = {}
_renders: OrderedDict = OrderedDict()


def doc_dir(doc_id: str) -> Path:
    d = DOCS / doc_id
    if not doc_id.isalnum() or not d.is_dir():
        raise HTTPException(404, "Document introuvable, recharge le PDF.")
    return d


def load_analysis(doc_id: str) -> dict:
    return json.loads((doc_dir(doc_id) / "analysis.json").read_text())


def save_analysis(doc_id: str, a: dict):
    (doc_dir(doc_id) / "analysis.json").write_text(json.dumps(a))


def client_payload(doc_id: str, a: dict, name: str) -> dict:
    return {"id": doc_id, "name": name, "pages": a["pages"], "fonts": list(a["fonts"]["fonts"].values()),
            "extraFamilies": F.EXTRA_FAMILIES}


def get_layer_doc(doc_id: str, layer: str) -> pymupdf.Document:
    """'full' = PDF d'origine, 'notext' = sans texte, 'noimg' = sans images (pour masquer l'original)."""
    key = (doc_id, layer)
    with _lock:
        if key in _open_docs:
            return _open_docs[key]
        d = doc_dir(doc_id)
        path = d / f"{layer}.pdf"
        if layer == "full":
            path = d / "source.pdf"
        elif not path.exists():
            doc = pymupdf.open(d / "source.pdf")
            for page in doc:
                page.add_redact_annot(page.rect, fill=False)
                if layer == "notext":
                    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
                else:
                    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                          text=pymupdf.PDF_REDACT_TEXT_NONE)
            doc.save(path)
            doc.close()
        doc = pymupdf.open(path)
        _open_docs[key] = doc
        return doc


def _sample_colors(page: pymupdf.Page, blocks: list):
    """Pour le texte invisible d'un scan déjà océrisé : couleur du texte et du papier."""
    from .ocr import _measure
    scale = 2.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, colorspace=pymupdf.csRGB)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    gray = (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114).astype(np.uint8)
    for b in blocks:
        x0, y0, x1, y1 = b["bbox"]
        text = "".join(r["t"] for p in b["paras"] for r in p)
        m = _measure(gray, rgb, (x0 * scale, y0 * scale, x1 * scale, y1 * scale), scale, text)
        b["bg"] = m["bg"] if m else "#ffffff"
        if m:
            for p in b["paras"]:
                for r in p:
                    r["c"] = m["color"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect x="5" y="2" width="22" height="28" rx="3" fill="#2563eb"/>'
           '<path d="M10 11h12M10 16h12M10 21h7" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>')
    return Response(svg, media_type="image/svg+xml")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(400, "Ce fichier n'est pas un PDF valide.")
    if doc.needs_pass:
        raise HTTPException(400, "Ce PDF est protégé par un mot de passe.")
    if doc.page_count == 0:
        raise HTTPException(400, "Ce PDF ne contient aucune page.")
    for page in doc:
        if page.rotation:
            page.remove_rotation()
        # Page recadrée : on aligne la MediaBox sur la CropBox pour que toutes les
        # coordonnées (lecture et écriture) partent du même coin
        cb = doc.xref_get_key(page.xref, "CropBox")
        if cb[0] == "array" and cb[1] != doc.xref_get_key(page.xref, "MediaBox")[1]:
            doc.xref_set_key(page.xref, "MediaBox", cb[1])
            doc.xref_set_key(page.xref, "CropBox", cb[1])
    doc_id = uuid.uuid4().hex[:12]
    d = DOCS / doc_id
    d.mkdir(parents=True)
    src = d / "source.pdf"
    doc.save(src, encryption=pymupdf.PDF_ENCRYPT_NONE)
    doc.close()
    doc = pymupdf.open(src)
    a = analyze(doc)
    for pno, p in enumerate(a["pages"]):
        inv = [b for b in p["blocks"] if b["kind"] == "ocr"]
        if inv:
            _sample_colors(doc[pno], inv)
            for b in inv:
                a["server"][b["id"]]["bg"] = b["bg"]
    doc.close()
    name = file.filename or "document.pdf"
    (d / "meta.json").write_text(json.dumps({"name": name}))
    save_analysis(doc_id, a)
    return client_payload(doc_id, a, name)


@app.get("/api/doc/{doc_id}")
def get_doc(doc_id: str):
    d = doc_dir(doc_id)
    name = json.loads((d / "meta.json").read_text()).get("name", "document.pdf")
    return client_payload(doc_id, load_analysis(doc_id), name)


@app.get("/api/doc/{doc_id}/page/{n}.png")
def page_png(doc_id: str, n: int, layer: str = "full", scale: float = 2.0):
    if layer not in ("full", "notext", "noimg"):
        raise HTTPException(400, "layer")
    scale = max(0.5, min(4.0, round(scale * 2) / 2))
    key = (doc_id, layer, n, scale)
    with _lock:
        png = _renders.get(key)
        if png is None:
            doc = get_layer_doc(doc_id, layer)
            if not 0 <= n < doc.page_count:
                raise HTTPException(404, "page")
            pix = doc[n].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=(layer == "full"))
            png = pix.tobytes("png")
            _renders[key] = png
            while len(_renders) > 150:
                _renders.popitem(last=False)
        else:
            _renders.move_to_end(key)
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/doc/{doc_id}/image/{xref}.png")
def image_png(doc_id: str, xref: int):
    with _lock:
        doc = get_layer_doc(doc_id, "full")
        try:
            pix = pymupdf.Pixmap(doc, xref)
            smask = doc.xref_get_key(xref, "SMask")
            if smask[0] == "xref":
                mask = pymupdf.Pixmap(doc, int(smask[1].split()[0]))
                if pix.alpha:
                    pix = pymupdf.Pixmap(pix, 0)
                if pix.colorspace and pix.colorspace.n != 3:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                pix = pymupdf.Pixmap(pix, mask)
            elif pix.colorspace and pix.colorspace.n not in (1, 3):
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            png = pix.tobytes("png")
        except Exception as e:
            raise HTTPException(404, f"image: {e}")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/font/{fid}")
def font_file(fid: str):
    if not fid.isalnum():
        raise HTTPException(404)
    try:
        data = F.font_bytes(fid)
    except FileNotFoundError:
        raise HTTPException(404)
    mt = "font/otf" if data[:4] == b"OTTO" else "font/ttf"
    return Response(data, media_type=mt, headers={"Cache-Control": "max-age=86400"})


@app.post("/api/doc/{doc_id}/ocr/{n}")
def ocr(doc_id: str, n: int):
    from .ocr import ocr_page
    with _lock:
        a = load_analysis(doc_id)
        fonts = DocFonts(None, a["fonts"])
        cands = []
        for fam in ("Arial", "Times New Roman"):
            reg = fonts.variant(fam, 400, False)
            bold = fonts.variant(fam, 700, False)
            cands.append((reg, fonts.fonts[reg]["primary"], bold, fonts.fonts[bold]["primary"]))
        a["fonts"] = fonts.to_json()
        doc = get_layer_doc(doc_id, "full")
        blocks, server = ocr_page(doc[n], n, cands)
        a["pages"][n]["blocks"] = [b for b in a["pages"][n]["blocks"] if b["kind"] != "ocr"] + blocks
        a["pages"][n]["ocrDone"] = True
        a["server"].update(server)
        save_analysis(doc_id, a)
    return {"blocks": blocks, "fonts": list(a["fonts"]["fonts"].values())}


@app.post("/api/doc/{doc_id}/font")
def add_font(doc_id: str, body: dict = Body(...)):
    with _lock:
        a = load_analysis(doc_id)
        fonts = DocFonts(None, a["fonts"])
        fid = fonts.variant(body["family"], int(body.get("weight", 400)), bool(body.get("italic", False)))
        a["fonts"] = fonts.to_json()
        save_analysis(doc_id, a)
    return {"id": fid, "fonts": list(a["fonts"]["fonts"].values())}


@app.post("/api/doc/{doc_id}/export")
def export(doc_id: str, ops: dict = Body(...)):
    d = doc_dir(doc_id)
    a = load_analysis(doc_id)
    try:
        pdf = export_pdf(str(d / "source.pdf"), a, ops)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Erreur pendant la création du PDF : {e}")
    return Response(pdf, media_type="application/pdf")


@app.exception_handler(HTTPException)
async def http_error(_req, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
