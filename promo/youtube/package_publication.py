"""Collect final videos and publication copy into one local review folder."""
from pathlib import Path
import hashlib
import html
import json
import shutil

BASE = Path(__file__).resolve().parent
DEST = BASE / 'publication-ready'

def main():
    DEST.mkdir(exist_ok=True)
    sections = []
    manifest = []
    for lang, folder, label in [('fr', 'export-v2', 'Français'), ('en', 'export-en', 'English')]:
        meta = json.loads((BASE / f'publication-{lang}.json').read_text(encoding='utf-8'))
        sources = [
            (BASE / folder / f'blink2video-youtube-{lang}.mp4', f'blink2video-{lang}.mp4'),
            (BASE / folder / 'miniature-youtube.jpg', f'miniature-{lang}.jpg'),
            (BASE / folder / f'blink2video.{lang}.srt', f'blink2video.{lang}.srt'),
            (BASE / f'publication-{lang}.json', f'publication-{lang}.json'),
            (BASE / f'publication-{lang}.md', f'publication-{lang}.md'),
        ]
        for source, name in sources:
            if not source.is_file():
                raise FileNotFoundError(source)
            shutil.copy2(source, DEST / name)
            manifest.append({'file': name, 'bytes': source.stat().st_size,
                             'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
        fields=[]
        for key, title in [('title','Titre / Title'),('description','Description'),('comment','Commentaire / Comment')]:
            value=meta[key]
            (DEST / f'{key}-{lang}.txt').write_text(value+'\n',encoding='utf-8')
            fields.append(f'<label for="{key}-{lang}">{title}</label><textarea id="{key}-{lang}" rows="{2 if key == "title" else 10}" readonly>{html.escape(value)}</textarea><button data-copy="{key}-{lang}">Copier / Copy</button>')
        sections.append(f'<section><h2>{label}</h2><video controls preload="metadata" poster="miniature-{lang}.jpg" src="blink2video-{lang}.mp4"></video><p><a href="blink2video-{lang}.mp4">Vidéo MP4</a> · <a href="miniature-{lang}.jpg">Miniature</a> · <a href="blink2video.{lang}.srt">Sous-titres SRT</a></p>{"".join(fields)}</section>')
    page='''<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>blink2video — Publications YouTube</title><style>
body{margin:40px auto;padding:0 24px;max-width:1100px;background:#0b1420;color:#f4f7fb;font:18px/1.5 system-ui}h1,h2{color:#6ee7b7}section{margin:48px 0;padding:24px;background:#152334;border-radius:16px}video{width:100%;border-radius:8px}a{color:#6ee7b7}label{display:block;margin-top:24px}textarea{box-sizing:border-box;width:100%;padding:14px;background:#0b1420;color:white;border:1px solid #41546b;font:16px/1.5 system-ui}button{padding:10px 18px;background:#6ee7b7;border:0;border-radius:6px;margin:8px 0;cursor:pointer}#status{position:sticky;top:8px;background:#152334;padding:8px}
</style><h1>blink2video — Deux publications YouTube</h1><p>Versions française et anglaise, 3 min 10, sans voix off. Fichiers locaux prêts à publier.</p><p><a href="https://studio.youtube.com/" target="_blank" rel="noreferrer">Ouvrir YouTube Studio</a></p><p id="status" role="status">Chaque version possède son titre, sa description et son commentaire explicatif.</p>'''+''.join(sections)+'''<script>
document.addEventListener('click',async e=>{const id=e.target.dataset.copy;if(!id)return;const field=document.getElementById(id);try{await navigator.clipboard.writeText(field.value)}catch{field.focus();field.select();document.execCommand('copy')}document.getElementById('status').textContent='Texte copié / Text copied';});
</script></html>'''
    (DEST / 'index.html').write_text(page,encoding='utf-8')
    (DEST / 'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(DEST / 'index.html')

if __name__=='__main__':
    main()
