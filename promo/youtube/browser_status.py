"""Read only the state of the dedicated YouTube browser, without credentials."""
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
    for context in browser.contexts:
        for page in context.pages:
            address = urlsplit(page.url)
            print(page.title(), address.netloc + address.path)
            if address.netloc.endswith('youtube.com'):
                for label in ['Sign in', 'Se connecter', 'Create a channel', 'Créer une chaîne']:
                    count = page.get_by_text(label, exact=True).count()
                    if count:
                        print(label, count)
