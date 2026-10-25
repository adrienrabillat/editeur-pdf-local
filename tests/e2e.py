"""Test de bout en bout dans Chrome : ouvre un PDF, modifie, télécharge, rend le résultat en PNG.

Usage : .venv/bin/python tests/e2e.py <pdf> <dossier_sortie> [scenario]
"""

import sys
from pathlib import Path

import pymupdf
from playwright.sync_api import sync_playwright

pdf, out = Path(sys.argv[1]), Path(sys.argv[2])
scenario = sys.argv[3] if len(sys.argv) > 3 else "basic"
out.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=1)
    logs = []
    pg.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
    pg.on("pageerror", lambda e: logs.append(f"PAGEERROR: {e}"))
    pg.goto("http://127.0.0.1:8765/")
    pg.evaluate("localStorage.clear()")
    pg.reload()
    pg.set_input_files("#file", str(pdf))
    pg.wait_for_selector("#editor:not([hidden])", timeout=60000)
    pg.wait_for_timeout(1500)
    pg.screenshot(path=str(out / "1_loaded.png"))

    def click_block(bid, dx=0.5, dy=0.5):
        box = pg.evaluate(f"""(() => {{ const el = document.querySelector('[data-id="{bid}"]'); el.scrollIntoView({{block: 'center'}}); const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }})()""")
        pg.mouse.click(box[0] + box[2] * dx, box[1] + box[3] * dy)
        pg.wait_for_timeout(200)

    if scenario == "basic":
        # 1. clic dans le paragraphe justifié, on tape un mot au milieu
        click_block("p0b2", 0.3, 0.1)
        pg.keyboard.type(" AJOUT éèàç")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / "2_editing.png"))
        pg.keyboard.press("Escape")
        # 2. remplacer le total
        click_block("p0b16", 0.5, 0.5)
        pg.keyboard.press("Meta+a")
        pg.keyboard.type("Total : 9 999,00 € HT")
        pg.keyboard.press("Escape")
        # 3. supprimer "Merci pour votre confiance."
        click_block("p0b17")
        pg.keyboard.press("Escape")
        pg.keyboard.press("Delete")
        # 4. déplacer le logo
        box = pg.evaluate("""(() => { const el = document.querySelector('.hit.img'); if (!el) return null; el.scrollIntoView({block: 'center'}); const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()""")
        if box:
            pg.mouse.move(box[0] + 20, box[1] + 20)
            pg.mouse.down()
            pg.mouse.move(box[0] - 100, box[1] + 60, steps=5)
            pg.mouse.up()
        # 5. ajouter un texte
        pg.click('[data-tool="text"]')
        z = pg.evaluate("S.zoom")
        pg.evaluate(f"document.querySelector('#viewer').scrollTop = 600 * {z}")
        page_box = pg.evaluate("(() => { const r = document.querySelector('.page').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
        pg.mouse.click(page_box[0] + 60 * z, page_box[1] + 700 * z)
        pg.keyboard.type("Nouveau texte ajouté")
        pg.keyboard.press("Escape")
        # 6. surligner
        pg.click('[data-tool="highlight"]')
        pg.evaluate("document.querySelector('#viewer').scrollTop = 0")
        page_box = pg.evaluate("(() => { const r = document.querySelector('.page').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
        pg.mouse.move(page_box[0] + 57 * z, page_box[1] + 238 * z)
        pg.mouse.down()
        pg.mouse.move(page_box[0] + 300 * z, page_box[1] + 252 * z, steps=4)
        pg.mouse.up()
    elif scenario == "word":
        click_block("p0b2", 0.6, 0.05)
        pg.keyboard.type(" (texte inséré pour tester le retour à la ligne automatique)")
        pg.keyboard.press("Escape")
        click_block("p0b6")
        pg.keyboard.press("End")
        pg.keyboard.type("-Martin")
        pg.keyboard.press("Escape")
    elif scenario == "scan":
        pg.wait_for_selector('[data-id^="p0o"]', timeout=60000)
        pg.wait_for_timeout(500)
        ids = pg.evaluate("Object.keys(S.blocks)")
        print("ocr blocks", ids[:5], len(ids))
        click_block(ids[1])
        pg.keyboard.press("Meta+a")
        pg.keyboard.type("Chère Madame,")
        pg.keyboard.press("Escape")
    elif scenario == "styles":
        click_block("p0b2", 0.2, 0.1)
        # sélectionner le mot "paragraphe" (double-clic)
        pos = pg.evaluate("""(() => { const c = document.querySelector('[data-id="p0b2"] .tcontent');
            const w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT); const n = w.nextNode(); const i = n.data.indexOf('paragraphe');
            const r = document.createRange(); r.setStart(n, i+2); r.setEnd(n, i+3); const b = r.getBoundingClientRect(); return [b.left, b.top + b.height/2]; })()""")
        pg.mouse.dblclick(pos[0], pos[1])
        pg.keyboard.press("Meta+b")
        pg.wait_for_timeout(300)
        pg.fill("#f-size", "16")
        pg.press("#f-size", "Enter")
        pg.wait_for_timeout(300)
        pg.evaluate("document.querySelector('#f-color').value = '#dc2626'; document.querySelector('#f-color').dispatchEvent(new Event('change'))")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / "2_styled.png"))
        # police Google pour tout le bloc "Merci..." : on sélectionne le bloc sans l'éditer
        click_block("p0b17")
        pg.keyboard.press("Escape")
        pg.select_option("#f-family", "Dancing Script")
        pg.wait_for_timeout(2500)
        pg.fill("#f-size", "18")
        pg.press("#f-size", "Enter")
        pg.wait_for_timeout(300)
        # titre : on change le texte puis on annule
        click_block("p0b0")
        pg.keyboard.press("Meta+a")
        pg.keyboard.type("XXXX")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
        pg.keyboard.press("Meta+z")
        pg.wait_for_timeout(300)
        print("undo title:", pg.evaluate("byId('p0b0').state"))
    elif scenario == "rewrite_all":
        pg.evaluate("""(() => {
            for (const o of S.objects.filter(o => o.type === 'block')) {
                activateBlock(o); o.pristine = false;
            }
            commit();
        })()""")
    elif scenario == "noop":
        for bid in pg.evaluate("Object.keys(S.blocks)")[:3]:
            click_block(bid)
            pg.keyboard.press("Escape")

    pg.wait_for_timeout(500)
    pg.screenshot(path=str(out / "3_before_download.png"))
    with pg.expect_download(timeout=60000) as dl:
        pg.click("#btn-download")
    path = out / "result.pdf"
    dl.value.save_as(str(path))
    ops = pg.evaluate("JSON.stringify(buildOps())")
    (out / "ops.json").write_text(ops)
    b.close()

doc = pymupdf.open(path)
src = pymupdf.open(pdf)
import numpy as np
for i, page in enumerate(doc):
    a = page.get_pixmap(dpi=110)
    a.save(str(out / f"result_p{i}.png"))
    b2 = src[i].get_pixmap(dpi=110)
    b2.save(str(out / f"source_p{i}.png"))
    A = np.frombuffer(a.samples, np.uint8).reshape(a.height, a.width, a.n).astype(int)
    B = np.frombuffer(b2.samples, np.uint8).reshape(b2.height, b2.width, b2.n).astype(int)
    diff = np.abs(A - B).max(axis=2)
    print(f"page {i}: pixels differents (>64) = {(diff > 64).sum()} / {diff.size}")
    D = pymupdf.Pixmap(pymupdf.csGRAY, a.width, a.height, bytes((255 - np.clip(diff * 3, 0, 255)).astype(np.uint8).tobytes()), 0)
    D.save(str(out / f"diff_p{i}.png"))
print("\n".join(logs[-20:]))
print("OK", path, path.stat().st_size)
