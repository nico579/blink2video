"""Publish the authorized French comment using its own watch-page tab."""
from pathlib import Path
import json
import re
import sys
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE = Path(__file__).resolve().parent
LANG = sys.argv[2] if len(sys.argv) > 2 else 'fr'
assert LANG in ('fr', 'en')
VIDEO = {'fr': 'V2E4m_SLgb4', 'en': '6rNqLI9K8Tc'}[LANG]
CHANNEL = 'UCyW61N0ueQmkU1MkczXRsJw'

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
    context = browser.contexts[0]
    pages = [page for page in context.pages
             if urlsplit(page.url).hostname == 'www.youtube.com'
             and parse_qs(urlsplit(page.url).query).get('v') == [VIDEO]]
    if not pages:
        page = context.new_page()
        page.goto(f'https://www.youtube.com/watch?v={VIDEO}', wait_until='domcontentloaded')
        page.wait_for_timeout(2500)
    else:
        assert len(pages) == 1
        page = pages[0]
    page.set_default_timeout(12000)
    assert parse_qs(urlsplit(page.url).query).get('v') == [VIDEO]
    action = sys.argv[1]
    if action == 'inspect':
        print('URL', page.url)
        print(page.locator('body').inner_text()[:14000])
        print('AVATARS', page.locator('#avatar-btn').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,label:e.getAttribute("aria-label"),text:e.innerText,html:e.outerHTML.substring(0,1500)}))'))
    elif action == 'account':
        page.locator('#avatar-btn').click()
        page.wait_for_timeout(600)
        print(page.locator('ytd-popup-container').inner_text())
    elif action == 'account_list':
        page.get_by_text('Changer de compte', exact=True).click()
        page.wait_for_timeout(700)
        print(page.locator('ytd-popup-container').inner_text())
    elif action == 'account_select':
        popup = page.locator('ytd-popup-container')
        target = popup.get_by_text('Nico — Projets logiciels', exact=True)
        assert target.count() == 1
        target.click()
        page.wait_for_timeout(2200)
        page.locator('#avatar-btn').click()
        page.wait_for_timeout(600)
        print(page.locator('ytd-popup-container').inner_text())
    elif action == 'comments_inspect':
        page.bring_to_front()
        page.keyboard.press('Escape')
        page.locator('video').evaluate_all('(els)=>els.forEach(e=>e.pause())')
        page.locator('ytd-comments#comments').scroll_into_view_if_needed()
        page.wait_for_timeout(2500)
        print(page.locator('ytd-comments#comments').inner_text()[:9000])
        print('INPUTS', page.locator('ytd-comments input, ytd-comments textarea, ytd-comments [contenteditable]').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,id:e.id,label:e.getAttribute("aria-label"),placeholder:e.getAttribute("placeholder")}))'))
    elif action == 'prepare':
        page.bring_to_front()
        page.keyboard.press('Escape')
        page.locator('#avatar-btn').click()
        page.locator('ytd-active-account-header-renderer:visible').get_by_text('@NicoProjetsLogiciels', exact=True).wait_for(state='visible')
        assert 'Nico — Projets logiciels' in page.locator('ytd-popup-container').inner_text()
        page.keyboard.press('Escape')
        comments = page.locator('ytd-comments#comments')
        comments.scroll_into_view_if_needed()
        page.wait_for_timeout(1500)
        meta = json.loads((BASE / f'publication-ready/publication-{LANG}.json').read_text(encoding='utf-8'))
        assert not comments.get_by_text(meta['comment'], exact=True).count(), 'Comment already present'
        comments.get_by_text('Ajoutez un commentaire…', exact=True).click()
        editor = comments.locator('#contenteditable-root[contenteditable="true"]').first
        editor.fill(meta['comment'])
        assert re.sub(r'\n{2,}', '\n\n', editor.inner_text()).strip() == meta['comment'].strip()
        print('Ready', LANG, len(meta['comment']), 'characters; verified @NicoProjetsLogiciels')
        print('BUTTONS', comments.get_by_role('button').evaluate_all('(els)=>els.filter(e=>e.getBoundingClientRect().width).map(e=>({id:e.id,label:e.getAttribute("aria-label"),text:e.innerText}))'))
    elif action == 'publish':
        comments = page.locator('ytd-comments#comments')
        meta = json.loads((BASE / f'publication-ready/publication-{LANG}.json').read_text(encoding='utf-8'))
        editor = comments.locator('#contenteditable-root[contenteditable="true"]').first
        assert re.sub(r'\n{2,}', '\n\n', editor.inner_text()).strip() == meta['comment'].strip()
        page.keyboard.press('Escape')
        page.locator('#avatar-btn').click()
        page.locator('ytd-active-account-header-renderer:visible').get_by_text('@NicoProjetsLogiciels', exact=True).wait_for(state='visible')
        page.keyboard.press('Escape')
        comments.get_by_role('button', name='Ajouter un commentaire', exact=True).click()
        page.wait_for_timeout(2000)
        print(comments.inner_text()[:6500])
    elif action == 'editor_check':
        import difflib
        editor=page.locator('ytd-comments #contenteditable-root[contenteditable="true"]').first
        expected=json.loads((BASE/f'publication-ready/publication-{LANG}.json').read_text(encoding='utf-8'))['comment']
        actual=editor.inner_text()
        print(len(expected),len(actual))
        print(repr(actual[:450]))
        print('\n'.join(list(difflib.unified_diff(expected.splitlines(),actual.splitlines()))[:60]))
    else:
        raise ValueError(action)
