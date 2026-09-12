"""DOM-only steps in the dedicated browser, connecting to a single tab."""
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import quote
import json
import base64
import sys
import time
import websocket

BASE=Path(__file__).resolve().parent
lang=sys.argv[2] if len(sys.argv)>2 else 'en'
video={'fr':'V2E4m_SLgb4','en':'6rNqLI9K8Tc'}[lang]
targets=json.load(urlopen('http://127.0.0.1:9222/json/list',timeout=5))
target_store=BASE/'export-v2/cdp-targets.json'
target_ids=json.loads(target_store.read_text()) if target_store.exists() else {}
candidates=[t for t in targets if t['type']=='page' and t['url'].startswith('https://www.youtube.com/watch?v='+video)]
target=next((t for t in candidates if t['id']==target_ids.get(lang)),None) or next(iter(candidates),None)
if target is None:
    target=json.load(urlopen(Request('http://127.0.0.1:9222/json/new?'+quote('https://www.youtube.com/watch?v='+video,safe=''),method='PUT'),timeout=5))
target_ids[lang]=target['id']
target_store.write_text(json.dumps(target_ids))
ws=websocket.create_connection(target['webSocketDebuggerUrl'],timeout=12,suppress_origin=True)
counter=0
def call(method, params=None):
    global counter
    counter+=1
    ws.send(json.dumps({'id':counter,'method':method,'params':params or {}}))
    while True:
        result=json.loads(ws.recv())
        if result.get('id')==counter:
            if 'error' in result: raise RuntimeError(result['error'])
            return result.get('result')
def evaluate(expression):
    result=call('Runtime.evaluate',{'expression':expression,'returnByValue':True})
    if 'exceptionDetails' in result: raise RuntimeError(result['exceptionDetails'])
    return result.get('result',{}).get('value')
action=sys.argv[1]
if action=='inspect':
    print(evaluate('document.querySelector("ytd-comments")?.innerText'))
    print(evaluate('document.querySelector("ytd-popup-container")?.innerText'))
    print(evaluate('[...document.querySelectorAll("ytd-comments [contenteditable]")].map(e=>({id:e.id,text:e.innerText}))'))
elif action=='body':
    print(evaluate('document.body.innerText.substring(0,6500)'))
    print(evaluate('({href:location.href,title:document.title,state:document.readyState,full:!!document.fullscreenElement,body:document.body.outerHTML.substring(0,1000)})'))
elif action=='screen':
    data=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
    (BASE/f'export-v2/youtube-{lang}-screen.png').write_bytes(base64.b64decode(data['data']))
    print(evaluate('({width:innerWidth,height:innerHeight,scrollY,visible:document.visibilityState})'))
elif action=='window':
    result=call('Browser.getWindowForTarget',{'targetId':target['id']})
    print(result)
    call('Browser.setWindowBounds',{'windowId':result['windowId'],'bounds':{'windowState':'normal'}})
    call('Page.bringToFront')
elif action=='account':
    call('Page.bringToFront')
    evaluate('document.querySelector("#avatar-btn").click()')
    print(evaluate('document.querySelector("#avatar-btn").outerHTML'))
    print(evaluate('document.querySelector("ytd-comment-simplebox-renderer")?.outerHTML.substring(0,3000)'))
elif action=='editor':
    # The avatar on the comment composer identifies the posting channel.
    account=evaluate('document.querySelector("ytd-comment-simplebox-renderer #author-thumbnail img")?.alt')
    assert account=='Nico — Projets logiciels', account
    call('Input.dispatchKeyEvent',{'type':'keyDown','key':'Escape','windowsVirtualKeyCode':27})
    call('Input.dispatchKeyEvent',{'type':'keyUp','key':'Escape','windowsVirtualKeyCode':27})
    meta=json.loads((BASE/f'publication-ready/publication-{lang}.json').read_text(encoding='utf-8'))
    assert meta['comment'].splitlines()[0] not in (evaluate('document.querySelector("ytd-comments #contents")?.innerText') or '')
    evaluate('document.querySelector("ytd-comment-simplebox-renderer #placeholder-area")?.click()')
    for _ in range(24):
        if evaluate('Boolean(document.querySelector("ytd-comments #contenteditable-root"))'): break
        time.sleep(.25)
    evaluate('document.querySelector("ytd-comments #contenteditable-root").focus()')
    evaluate('document.querySelector("ytd-comments #contenteditable-root").innerText=""')
    call('Input.insertText',{'text':meta['comment']})
    print('Prepared',lang)
elif action=='publish':
    meta=json.loads((BASE/f'publication-ready/publication-{lang}.json').read_text(encoding='utf-8'))
    actual=evaluate('document.querySelector("ytd-comments #contenteditable-root").innerText')
    assert actual.split()==meta['comment'].split()
    evaluate('document.querySelector("ytd-comments #submit-button button").click()')
    print('Submitted',lang)
elif action=='reload':
    call('Page.reload')
elif action=='scroll':
    call('Page.bringToFront')
    evaluate('document.querySelector("video")?.pause();window.scrollTo({top:950,behavior:"instant"})')
    print(evaluate('({scrollY,visible:document.visibilityState,comments:document.querySelector("ytd-comments")?.getBoundingClientRect().top})'))
elif action=='links':
    print(evaluate('[...document.querySelectorAll("ytd-comments a")].filter(e=>e.href.includes("lc=")).map(e=>({text:e.innerText,href:e.href}))'))
elif action=='verify':
    meta=json.loads((BASE/f'publication-ready/publication-{lang}.json').read_text(encoding='utf-8'))
    comments=evaluate('[...document.querySelectorAll("ytd-comment-thread-renderer")].map(e=>({text:e.querySelector("#content-text")?.textContent,author:e.querySelector("#author-text")?.textContent,url:[...e.querySelectorAll("a")].find(a=>a.href.includes("lc="))?.href}))')
    matches=[c for c in comments if c.get('text') and c['text'].split()==meta['comment'].split()]
    assert len(matches)==1, {'matches':len(matches),'comments':comments}
    assert '@NicoProjetsLogiciels' in matches[0]['author']
    result_path=BASE/'comment-results.json'
    results=json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
    results[lang]={'url':matches[0]['url'],'author':'@NicoProjetsLogiciels','verified':True}
    result_path.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(results[lang])
elif action=='menu':
    evaluate('document.querySelector("ytd-comment-thread-renderer #action-menu button").click()')
    print('Menu opened')
else: raise ValueError(action)
ws.close()
