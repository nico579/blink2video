"""Run an explicitly selected browser step against the dedicated YouTube tab."""
from pathlib import Path
import json
import sys
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

BASE=Path(__file__).resolve().parent
def inspect(page):
    print('URL', page.url)
    dialogs=page.locator('ytcp-uploads-dialog')
    region=dialogs if dialogs.count() else page.locator('body')
    body=region.inner_text()
    print(body if len(body)<5000 else body[:700]+'\n[...]\n'+body[-2200:])

with sync_playwright() as p:
    browser=p.chromium.connect_over_cdp('http://127.0.0.1:9222')
    pages=[page for ctx in browser.contexts for page in ctx.pages if urlsplit(page.url).hostname in ('www.youtube.com','studio.youtube.com')]
    page=pages[0]
    page.set_default_timeout(12000)
    action=sys.argv[1]
    if action=='inspect':
        inspect(page)
    elif action=='screen':
        page.screenshot(path=str(BASE/'export-v2/youtube-ui.png'))
        print(page.locator('body').inner_text()[:12000])
    elif action=='tail':
        print(page.locator('body').inner_text()[-5000:])
    elif action=='channel_form':
        page.get_by_text('Créer une chaîne',exact=True).click()
        page.wait_for_timeout(1500)
        inspect(page)
    elif action=='channel_fill':
        fields=page.locator('input[placeholder=""]')
        assert fields.count()==2, 'Unexpected channel form'
        fields.nth(0).fill('Nico — Projets logiciels')
        fields.nth(1).fill('NicoProjetsLogiciels')
        page.wait_for_timeout(1800)
        inspect(page)
    elif action=='channel_create':
        fields=page.locator('input[placeholder=""]')
        assert fields.nth(0).input_value()=='Nico — Projets logiciels'
        assert fields.nth(1).input_value().lstrip('@')=='NicoProjetsLogiciels'
        button=page.get_by_role('button',name='Créer une chaîne',exact=True)
        assert button.count()==1
        button.click()
        page.wait_for_timeout(3500)
        inspect(page)
    elif action=='studio':
        page.goto('https://studio.youtube.com/channel/UCyW61N0ueQmkU1MkczXRsJw',wait_until='domcontentloaded')
        page.wait_for_timeout(2200)
        inspect(page)
    elif action=='channel_details':
        item=page.get_by_text('Personnalisation',exact=True)
        link=item.evaluate('(e)=>e.closest("a").href')
        print('NAV',link)
        page.goto(link,wait_until='domcontentloaded')
        page.wait_for_timeout(1600)
        inspect(page)
    elif action=='channel_save':
        source=(BASE/'chaine.md').read_text(encoding='utf-8')
        description=source.split('## Description française\n')[1].split('## English')[0].strip()
        page.locator('[contenteditable="true"]').fill(description)
        page.get_by_role('button',name='Publier',exact=True).click()
        page.wait_for_timeout(1200)
        inspect(page)
    elif action=='upload_dialog':
        assert 'UCyW61N0ueQmkU1MkczXRsJw' in page.url
        page.get_by_role('button',name='Créer',exact=True).click()
        page.wait_for_timeout(300)
        inspect(page)
    elif action=='upload_open':
        page.get_by_text('Importer des vidéos',exact=True).click()
        page.wait_for_timeout(900)
        inspect(page)
    elif action=='upload_file':
        lang=sys.argv[2]
        assert lang in ('fr','en')
        page.locator('input[type="file"]').last.set_input_files(str(BASE/f'publication-ready/blink2video-{lang}.mp4'))
        page.wait_for_timeout(3500)
        inspect(page)
    elif action=='video_details':
        lang=sys.argv[2]
        meta=json.loads((BASE/f'publication-ready/publication-{lang}.json').read_text(encoding='utf-8'))
        page.get_by_label('Ajoutez un titre pour décrire votre vidéo (saisissez @ pour mentionner une chaîne)',exact=True).fill(meta['title'])
        page.get_by_label('Présentez votre vidéo à vos spectateurs (saisissez @ pour mentionner une chaîne)',exact=True).fill(meta['description'])
        page.get_by_text("Non, elle n'est pas conçue pour les enfants",exact=True).click()
        page.get_by_text('Plus',exact=True).click()
        page.wait_for_timeout(750)
        inspect(page)
    elif action=='thumbnail':
        lang=sys.argv[2]
        page.locator('#file-loader').set_input_files(str(BASE/f'publication-ready/miniature-{lang}.jpg'))
        page.wait_for_timeout(800)
        inspect(page)
    elif action=='controls':
        print(page.locator('[role="radio"], ytcp-dropdown-trigger, [role="listbox"], [role="combobox"]').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,id:e.id,role:e.getAttribute("role"),label:e.getAttribute("aria-label"),text:e.innerText?.substring(0,150),outer:e.outerHTML.substring(0,500)}))'))
    elif action=='video_options':
        lang=sys.argv[2]
        meta=json.loads((BASE/f'publication-ready/publication-{lang}.json').read_text(encoding='utf-8'))
        page.locator('[name="VIDEO_PAID_PRODUCT_PLACEMENT_NO"]').click()
        page.locator('[name="VIDEO_HAS_ALTERED_CONTENT_NO"]').click()
        page.get_by_label('Tags',exact=True).fill(','.join(meta['tags']))
        page.get_by_label('Tags',exact=True).press('Enter')
        page.locator('ytcp-dropdown-trigger').filter(has_text='Langue de la vidéo').click()
        page.wait_for_timeout(400)
        print(page.locator('tp-yt-paper-listbox:visible').inner_text()[:10000])
    elif action=='language_select':
        lang=sys.argv[2]
        page.get_by_text('Français' if lang=='fr' else 'Anglais',exact=True).click()
        page.locator('ytcp-dropdown-trigger').filter(has_text='People et blogs').click()
        page.wait_for_timeout(300)
        print(page.locator('tp-yt-paper-listbox:visible').inner_text())
    elif action=='category_select':
        page.get_by_text('Science et technologie',exact=True).click()
        page.get_by_role('button',name='Suivant',exact=True).click()
        page.wait_for_timeout(650)
        inspect(page)
    elif action=='captions_open':
        page.get_by_role('button',name='Ajouter',exact=True).first.click()
        page.wait_for_timeout(1000)
        print(page.locator('body').inner_text()[-10000:])
    elif action=='captions_inspect':
        print(page.locator('body').inner_text()[-6500:])
        print('FRAMES', [(f.name,f.url) for f in page.frames])
        print('BUTTONS',page.get_by_role('button').evaluate_all('(els)=>els.filter(e=>e.getBoundingClientRect().width).map(e=>({tag:e.tagName,label:e.getAttribute("aria-label"),text:e.innerText,id:e.id}))'))
    elif action=='captions_upload':
        page.locator('#choose-upload-file').click()
        page.wait_for_timeout(400)
        print(page.locator('body').inner_text()[-2200:])
        print(page.locator('input[type="file"]').evaluate_all('(els)=>els.map(e=>({id:e.id,accept:e.accept}))'))
    elif action=='captions_file':
        lang=sys.argv[2]
        page.locator('#captions-file-loader').set_input_files(str(BASE/f'publication-ready/blink2video.{lang}.srt'))
        page.wait_for_timeout(800)
        print(page.locator('body').inner_text()[-4500:])
    elif action=='captions_done':
        page.get_by_role('button',name='OK',exact=True).click()
        page.wait_for_timeout(700)
        inspect(page)
    elif action=='next':
        page.get_by_role('button',name='Suivant',exact=True).click()
        page.wait_for_timeout(600)
        inspect(page)
    elif action=='escape':
        page.keyboard.press('Escape')
        print(page.locator('body').inner_text()[-1400:])
    elif action=='publish':
        dialog=page.locator('ytcp-uploads-dialog')
        assert 'Aucun problème détecté' in dialog.inner_text()
        dialog.get_by_role('radio',name='Publique',exact=True).click()
        dialog.get_by_role('button',name='Publier',exact=True).click()
        page.wait_for_timeout(1500)
        print(page.locator('body').inner_text()[-5500:])
    elif action=='close_upload':
        page.get_by_role('button',name='Fermer',exact=True).last.click()
        page.wait_for_timeout(500)
        page.get_by_role('button',name='Créer',exact=True).click()
        page.get_by_text('Importer des vidéos',exact=True).click()
        page.wait_for_timeout(500)
        print(page.locator('body').inner_text()[-1200:])
    elif action=='content_list':
        close=page.get_by_role('button',name='Fermer',exact=True)
        if close.count():
            close.last.click()
        link=page.get_by_text('Contenus',exact=True).evaluate('(e)=>e.closest("a").href')
        page.goto(link,wait_until='domcontentloaded')
        page.wait_for_timeout(1700)
        inspect(page)
    elif action=='video_verify':
        lang=sys.argv[2]
        video_id={'fr':'V2E4m_SLgb4','en':'6rNqLI9K8Tc'}[lang]
        page.goto(f'https://studio.youtube.com/video/{video_id}/edit',wait_until='domcontentloaded')
        page.wait_for_timeout(1800)
        inspect(page)
    elif action=='assets_verify':
        print('THUMBNAIL',page.locator('img').evaluate_all('(els)=>els.filter(e=>e.src.includes("ytimg")||e.alt.toLowerCase().includes("miniature")).map(i=>({alt:i.alt,src:i.src,parent:i.parentElement.outerHTML.substring(0,1200)}))'))
        print('NAVLINKS',page.locator('a').evaluate_all('(els)=>els.filter(e=>e.innerText.trim()==="Sous-titres").map(e=>({text:e.innerText,href:e.href}))'))
    elif action=='thumbnail_verify':
        lang=sys.argv[2]
        video_id={'fr':'V2E4m_SLgb4','en':'6rNqLI9K8Tc'}[lang]
        assert f'/video/{video_id}/edit' in page.url
        assert not page.get_by_role('button',name='Enregistrer',exact=True).is_enabled()
        src=page.locator('img').evaluate_all('(els)=>els.find(e=>e.src.includes("ytimg") && e.alt.startsWith("Miniature de la vidéo")).src')
        response=page.request.get(src)
        assert response.ok
        target=BASE/f'export-v2/thumbnail-verified-{lang}.webp'
        target.write_bytes(response.body())
        print(json.dumps({'language':lang,'saved':True,'src':src,'download':str(target)}))
    elif action=='subtitles_page':
        link=page.get_by_text('Sous-titres',exact=True).first.evaluate('(e)=>e.closest("a").href')
        page.goto(link,wait_until='domcontentloaded')
        page.wait_for_timeout(1300)
        inspect(page)
    elif action=='thumbnail_check':
        welcome=page.get_by_role('button',name='Continuer',exact=True)
        if welcome.count() and welcome.is_visible():
            welcome.click()
        page.get_by_text('Importer un fichier',exact=True).click()
        page.wait_for_timeout(1200)
        print(page.locator('body').inner_text()[-2500:])
    elif action=='save_details':
        button=page.get_by_role('button',name='Enregistrer',exact=True)
        print('Save enabled',button.is_enabled(),button.get_attribute('aria-disabled'))
        button.click()
        page.wait_for_timeout(2000)
        print(page.locator('body').inner_text()[-1000:])
    elif action=='thumbnail_choose':
        lang=sys.argv[2]
        with page.expect_file_chooser() as chooser:
            page.get_by_text('Importer un fichier',exact=True).click()
        chooser.value.set_files(str(BASE/f'publication-ready/miniature-{lang}.jpg'))
        page.wait_for_timeout(1800)
        print('Save enabled',page.get_by_role('button',name='Enregistrer',exact=True).is_enabled())
    else:
        raise ValueError(action)
