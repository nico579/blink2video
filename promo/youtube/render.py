"""Build the French or English presentation using repository screenshots only.

Requires Pillow and numpy. Uses the bundled ffmpeg; no camera or API access.
Run: python promo/youtube/render.py
     python promo/youtube/render.py --language en
"""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import textwrap
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / 'export-v2'
OUT.mkdir(exist_ok=True)
LANGUAGE = 'fr'
TRANSLATIONS = {}
SCREENSHOTS = {}
W, H, FPS = 1920, 1080, 30
BG, PANEL, WHITE, MUTED, GREEN = '#0b1420', '#152334', '#f4f7fb', '#a6b8cc', '#6ee7b7'
FONTS = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
def font(size, bold=False):
    return ImageFont.truetype(str(FONTS / ('segoeuib.ttf' if bold else 'segoeui.ttf')), size)

SCENES = [
    (12, 'Pourquoi blink2video ?', 'Blink est pensé pour le téléphone. Mais comment garder un historique sur ordinateur ?', 'why'),
    (12, 'Ne perdez pas le fil.', 'Récupérez les clips avant leur effacement et rassemblez-les dans une archive.', 'why_archive'),
    (7, 'Vos caméras Blink.\nSur votre ordinateur.', 'Le direct et vos archives, avec blink2video.', 'intro'),
    (9, 'Retrouvez vos clips.', 'Les enregistrements réunis dans une interface locale.', 'clips'),
    (9, 'Le bon moment.\nLa bonne caméra.', 'Filtrez par caméra et par période.', 'filter'),
    (9, 'Gardez aussi le direct.', 'Lancez un direct et enregistrez-le à la demande.', 'live'),
    (10, 'Récupérez les nouveaux clips.', 'Stockage du module et cloud : deux sources prises en charge.', 'sources'),
    (10, 'Des clips à une archive.', 'Une vidéo par caméra, par jour, par semaine et par mois.', 'archive'),
    (10, 'Vos vidéos, sur votre machine.', 'Date et heure incrustées. Clips sans intérêt mis de côté.', 'local'),
    (15, '1. Télécharger et installer.', 'Sur GitHub, ouvrez la dernière version publiée et choisissez votre système.', 'install'),
    (15, '2. Lancer et se connecter.', 'Ouvrez blink2video, puis connectez-vous à votre compte Blink dans le navigateur.', 'launch'),
    (14, '3. Valider le premier démarrage.', 'Choisissez le dossier des vidéos et le fuseau horaire, puis cliquez sur Appliquer.', 'first'),
    (16, 'Réglages : au quotidien.', 'Démarrage automatique, actualisation de la page, téléchargement et cadence de lecture.', 'settings_daily'),
    (16, 'Réglages : vos vidéos.', 'Horodatage, fuseau horaire, protocole du direct et archives par jour, semaine ou mois.', 'settings_video'),
    (16, 'Réglages : garder la main.', 'Alertes par caméra, suppression à la source après téléchargement et arrêt de la surveillance.', 'settings_control'),
    (10, 'À vous de jouer.', 'Windows · Linux · macOS', 'end'),
]

# Additional scenes explain the purpose, then give a readable getting-started guide.
GUIDE = {
    'why': [
        ('Le point de départ', 'Vous regardez vos caméras Blink sur votre téléphone.'),
        ('Le besoin', 'Voir le direct et retrouver vos enregistrements sur ordinateur.'),
        ('L’idée de blink2video', 'Réunir vos caméras, vos clips et vos archives au même endroit.'),
    ],
    'why_archive': [
        ('Des clips dispersés', 'Un événement peut être réparti sur plusieurs enregistrements.'),
        ('Une conservation limitée', 'Le stockage local se remplit ; le cloud garde les clips un temps limité.'),
        ('Un historique chez vous', 'Télécharger les clips et les assembler pour les revoir facilement.'),
    ],
    'install': [
        ('Ouvrir le lien dans la description', 'github.com/nico579/blink2video/releases/latest'),
        ('Choisir l’archive de votre système', 'Windows : .zip  ·  Linux : .tar.gz  ·  macOS Apple Silicon : .zip'),
        ('Décompresser dans un dossier', 'Conservez tous les fichiers ensemble. Guide détaillé sur GitHub.'),
    ],
    'launch': [
        ('Windows', 'Double-cliquez sur blink2video.exe dans le dossier décompressé.'),
        ('Linux et macOS', 'Dans un terminal : ./blink2video — préparation expliquée sur GitHub.'),
        ('Connexion dans le navigateur', 'Adresse e-mail, mot de passe Blink, puis code de vérification Blink.'),
    ],
    'first': [
        ('Au tout premier lancement', 'Le panneau Réglages s’ouvre automatiquement après la connexion.'),
        ('Dossier des données et fuseau horaire', 'Choisissez où conserver les vidéos et quelle heure afficher.'),
        ('Cliquer sur « Appliquer »', 'Le téléchargement et l’assemblage peuvent alors démarrer.'),
    ],
    'settings_daily': [
        ('Démarrage et affichage', 'Lancer à l’ouverture de session et actualiser automatiquement la page.'),
        ('Téléchargement automatique', 'Activer la récupération ; choisir une cadence locale et une cadence cloud.'),
        ('Accès et stockage', 'Modifier le dossier des données ou, si nécessaire, le port du serveur.'),
    ],
    'settings_video': [
        ('Date et heure dans l’image', 'Activer l’horodatage et choisir votre fuseau horaire.'),
        ('Direct : WebRTC ou MSE', 'Choisir le protocole de lecture du direct dans le navigateur.'),
        ('Vidéos assemblées', 'Activer séparément les archives quotidiennes, hebdomadaires et mensuelles.'),
    ],
    'settings_control': [
        ('Alertes par caméra', 'Mettre en sourdine les alertes des caméras de votre choix.'),
        ('Suppression automatique après téléchargement', 'Option par caméra : supprime les clips à la source après récupération.'),
        ('Appliquer ou arrêter', 'Valider les changements ; le bouton rouge arrête la surveillance.'),
    ],
}

NOTES = {
    'why_archive': 'L’ordinateur doit rester allumé pour récupérer et assembler les clips.',
    'install': 'Aucune installation de Python nécessaire avec les archives publiées.',
    'launch': 'Un jeton de session est conservé ; jamais votre mot de passe.',
    'first': 'Aucun clip n’est téléchargé avant cette première validation.',
    'settings_daily': 'Pour rouvrir les réglages : cliquez sur l’engrenage en haut à droite.',
    'settings_video': 'Les trois périodicités d’archives sont indépendantes.',
    'settings_control': 'La suppression à la source est facultative : choisissez-la en connaissance de cause.',
}

def translate(value):
    return TRANSLATIONS.get(value, value)


def configure_language(language):
    global LANGUAGE, TRANSLATIONS, SCREENSHOTS, OUT
    LANGUAGE = language
    if language == 'en':
        from english import COPY, SCREENSHOTS as english_shots
        TRANSLATIONS, SCREENSHOTS = COPY, english_shots
    else:
        TRANSLATIONS, SCREENSHOTS = {}, {}
    OUT = Path(__file__).resolve().parent / ('export-en' if language == 'en' else 'export-v2')
    OUT.mkdir(exist_ok=True)


def text(draw, xy, value, size=40, color=WHITE, bold=False):
    value = translate(value)
    draw.multiline_text(xy, value, font=font(size, bold), fill=color, spacing=12)

def pill(draw, box, label, size=28):
    draw.rounded_rectangle(box, radius=18, fill=PANEL, outline='#31465d', width=2)
    text(draw, (box[0]+24, box[1]+18), label, size, GREEN, True)

def shot(canvas, name, box, crop=None):
    name = SCREENSHOTS.get(name, name)
    if name == 'direct_record_en.PNG' and crop:
        # English capture has a different player position; exclude the whole
        # camera information block, including its hardware identifier.
        crop = (9, 66, 655, 429)
    im = Image.open(ROOT / 'Screenshots' / name).convert('RGB')
    if crop:
        im = im.crop(crop)
    im.thumbnail((box[2]-box[0], box[3]-box[1]), Image.Resampling.LANCZOS)
    # Enlarge screenshots for readable video presentation.
    scale = min((box[2]-box[0])/im.width, (box[3]-box[1])/im.height)
    im = im.resize((round(im.width*scale), round(im.height*scale)), Image.Resampling.LANCZOS)
    x, y = box[0]+(box[2]-box[0]-im.width)//2, box[1]+(box[3]-box[1]-im.height)//2
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x-12,y-12,x+im.width+12,y+im.height+12), radius=18, fill='#26384a')
    canvas.paste(im, (x,y))

def slide(index):
    duration, title, subtitle, kind = SCENES[index]
    canvas = Image.new('RGB', (W,H), BG)
    d = ImageDraw.Draw(canvas)
    d.rectangle((0,0,12,H), fill=GREEN)
    text(d, (104,55), 'blink2video', 34, WHITE, True)
    text(d, (1485,65), 'VOS CAMÉRAS. VOS ARCHIVES.', 19, MUTED)
    text(d, (104,1010), 'github.com/nico579/blink2video', 25, MUTED)
    text(d, (1700,1010), f'{index+1:02} / {len(SCENES):02}', 25, MUTED)
    if kind in GUIDE:
        text(d,(104,165),title,70,WHITE,True)
        text(d,(108,275),subtitle,30,MUTED)
        for j,(label,detail) in enumerate(GUIDE[kind]):
            y=377+j*177
            text(d,(110,y),f'{j+1:02}',51,GREEN,True)
            text(d,(235,y),label,41,WHITE,True)
            text(d,(235,y+66),detail,32,MUTED)
        if kind in NOTES:
            text(d,(112,935),NOTES[kind],27,GREEN)
    elif kind == 'intro':
        pill(d, (104,223,540,300), 'LE DIRECT ET VOS ARCHIVES', 23)
        text(d, (100,338), title, 104, WHITE, True)
        text(d, (106,620), subtitle, 43, MUTED)
        for x, label in [(104,'DIRECT'),(405,'CLIPS'),(706,'ARCHIVES')]:
            pill(d,(x,758,x+265,842),label,30)
    elif kind == 'clips':
        text(d,(104,161),title,76,WHITE,True)
        text(d,(108,267),subtitle,37,MUTED)
        shot(canvas,'serve0.fr.PNG',(108,380,1810,925))
    elif kind == 'filter':
        text(d,(104,255),title,76,WHITE,True)
        text(d,(108,492),subtitle,35,MUTED)
        pill(d,(108,626,590,710),'Aujourd’hui · Semaine · Mois',28)
        shot(canvas,'filtre.fr.PNG',(1190,175,1750,915))
    elif kind == 'live':
        text(d,(104,166),title,76,WHITE,True)
        text(d,(108,269),subtitle,37,MUTED)
        # Frame the existing public capture around the player and recording control.
        # The bottom hardware identifiers are outside the video frame.
        shot(canvas,'direct_record_fr.PNG',(160,376,1075,939),(14,72,659,436))
        pill(d,(1175,470,1760,555),'1. Ouvrir le direct',34)
        pill(d,(1175,588,1760,673),'2. Enregistrer',34)
        text(d,(1185,725),'À conserver dans\nvos enregistrements.',36,MUTED)
    elif kind == 'sources':
        text(d,(104,165),title,72,WHITE,True)
        text(d,(108,271),subtitle,35,MUTED)
        for x, label, detail in [(108,'STOCKAGE LOCAL','Sync Module 2 : USB\nSync Module XR : microSD'),(988,'CLOUD BLINK','Clips de votre abonnement\nBlink')]:
            d.rounded_rectangle((x,410,x+800,726),radius=28,fill=PANEL)
            text(d,(x+44,450),label,37,GREEN,True)
            text(d,(x+44,538),detail,36)
        text(d,(110,827),'Seuls les nouveaux clips sont téléchargés.',45,WHITE,True)
    elif kind == 'archive':
        text(d,(104,165),title,76,WHITE,True)
        text(d,(108,273),subtitle,35,MUTED)
        for i,(label,detail) in enumerate([('01 JOUR','Une journée réunie'),('01 SEMAINE','Une vue plus large'),('01 MOIS','Votre historique')]):
            x=108+i*575
            d.rounded_rectangle((x,445,x+535,752),radius=26,fill=PANEL)
            text(d,(x+36,491),label,44,GREEN,True)
            text(d,(x+36,620),detail,32)
        text(d,(110,851),'Assemblage automatique, caméra par caméra.',42,WHITE,True)
    elif kind == 'local':
        text(d,(104,165),title,71,WHITE,True)
        for y,num,label,detail in [(350,'01','Horodater','La date et l’heure restent visibles dans l’image.'),(535,'02','Faire le tri','Écartez un clip sans supprimer son original.'),(720,'03','Conserver','Retrouvez vos fichiers vidéo sur votre ordinateur.')]:
            text(d,(112,y),num,58,GREEN,True)
            text(d,(260,y),label,48,WHITE,True)
            text(d,(260,y+73),detail,35,MUTED)
    else:
        text(d,(104,248),title,98,WHITE,True)
        text(d,(110,406),subtitle,47,GREEN)
        text(d,(110,515),'Gratuit et open source',49,WHITE,True)
        pill(d,(108,648,1770,762),'github.com/nico579/blink2video',58)
        text(d,(112,807),'Téléchargement et installation : lien dans la description.',34,MUTED)
        text(d,(112,887),'Un projet indépendant, sans affiliation avec Blink ou Amazon.',26,MUTED)
    path = OUT / f'scene-{index+1:02}.png'
    canvas.save(path)
    return path

def soundtrack(seconds):
    """Original quiet instrumental: synthesized pads and sparse arpeggios."""
    rate=44100
    sound=np.zeros((seconds*rate,2),dtype=np.float32)
    chords=[(50,57,62,65),(46,53,58,62),(53,60,65,69),(48,55,60,64)]
    for start in range(0,seconds,4):
        chord=chords[(start//4)%4]
        length=min(5,seconds-start)
        t=np.arange(length*rate)/rate
        envelope=np.minimum(t/1.0,1)*np.minimum((length-t)/1.4,1)
        for j,note in enumerate(chord):
            f=440*2**((note-69)/12)
            tone=(np.sin(2*np.pi*f*t)+0.18*np.sin(4*np.pi*f*t))*envelope*0.023
            sound[start*rate:start*rate+len(t),0]+=tone*(0.8+j*0.05)
            sound[start*rate:start*rate+len(t),1]+=tone*(0.95-j*0.05)
        for j,note in enumerate(chord):
            offset=start+j*0.75
            count=min(int(1.8*rate),len(sound)-int(offset*rate))
            if count<=0: continue
            t2=np.arange(count)/rate
            f=440*2**((note+12-69)/12)
            tone=np.sin(2*np.pi*f*t2)*np.exp(-3.5*t2)*np.minimum(t2/0.025,1)*0.045
            sound[int(offset*rate):int(offset*rate)+count,:]+=tone[:,None]
    fade=np.minimum(np.arange(len(sound))/rate/2,1)*np.minimum((len(sound)-np.arange(len(sound)))/rate/3,1)
    sound*=fade[:,None]
    path=OUT/'musique-originale.wav'
    with wave.open(str(path),'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((np.clip(sound,-1,1)*32767).astype('<i2').tobytes())
    return path

def stamp(seconds):
    milliseconds = round(seconds * 1000)
    return f'{milliseconds//3600000:02}:{milliseconds//60000%60:02}:{milliseconds//1000%60:02},{milliseconds%1000:03}'


def write_subtitles():
    """Caption the guide details, not just its headline, in the video language."""
    cues = []
    position = 0
    for duration, title, subtitle, kind in SCENES:
        if kind in GUIDE:
            paragraphs = [translate(detail) for _, detail in GUIDE[kind]]
            if kind == 'launch':
                # The platform label is essential to the terminal instruction.
                paragraphs[1] = translate(GUIDE[kind][1][0]) + ': ' + paragraphs[1]
            if kind == 'install':
                paragraphs[0] = translate(GUIDE[kind][0][0]) + ': ' + paragraphs[0]
            if kind == 'first':
                paragraphs[2] = translate(GUIDE[kind][2][0]) + '. ' + paragraphs[2]
            if kind == 'settings_video':
                paragraphs[1] = translate(GUIDE[kind][1][0]) + '. ' + paragraphs[1]
            if kind in NOTES:
                paragraphs.append(translate(NOTES[kind]))
        elif kind == 'sources':
            paragraphs = [translate(subtitle), translate('Sync Module 2 : USB\nSync Module XR : microSD'),
                          translate('Clips de votre abonnement\nBlink'), translate('Seuls les nouveaux clips sont téléchargés.')]
        elif kind == 'local':
            paragraphs = [translate('La date et l’heure restent visibles dans l’image.'),
                          translate('Écartez un clip sans supprimer son original.'),
                          translate('Retrouvez vos fichiers vidéo sur votre ordinateur.')]
        elif kind == 'end':
            paragraphs = [translate('Gratuit et open source') + '. Windows, Linux, macOS.',
                          translate('Téléchargement et installation : lien dans la description.'),
                          translate('Un projet indépendant, sans affiliation avec Blink ou Amazon.')]
        else:
            paragraphs = [translate(title), translate(subtitle)]
        # Longer instructions get proportionally more reading time.
        paragraphs = [p.replace('\n', ' ') for p in paragraphs]
        weights = [max(30, len(p)) for p in paragraphs]
        elapsed = 0
        for paragraph, weight in zip(paragraphs, weights):
            start = position + duration * elapsed / sum(weights)
            elapsed += weight
            end = position + duration * elapsed / sum(weights)
            wrapped = '\n'.join(textwrap.wrap(paragraph, width=48, break_long_words=False, break_on_hyphens=False))
            cues.append(f'{len(cues)+1}\n{stamp(start)} --> {stamp(end)}\n{wrapped}\n')
        position += duration
    path = OUT / f'blink2video.{LANGUAGE}.srt'
    path.write_text('\n'.join(cues), encoding='utf-8')
    return path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=('fr', 'en'), default='fr')
    args = parser.parse_args()
    configure_language(args.language)
    candidates=[ROOT/'dist/blink2video/_internal/ffmpeg-win-x86_64-v7.1.exe',ROOT/'build_venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe']
    ffmpeg=next((str(p) for p in candidates if p.exists()),None) or shutil.which('ffmpeg')
    if not ffmpeg: raise RuntimeError('ffmpeg introuvable')
    segments=[]; position=0
    for i,(duration,title,subtitle,kind) in enumerate(SCENES):
        png=slide(i)
        target=OUT/f'segment-{i+1:02}.mp4'
        vf=(f"zoompan=z='1+0.012*on/{duration*FPS}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={duration*FPS}:s={W}x{H}:fps={FPS},"
            f"fade=t=in:st=0:d=0.35,fade=t=out:st={duration-0.35}:d=0.35,format=yuv420p")
        subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(png),'-vf',vf,'-t',str(duration),'-c:v','libx264','-preset','fast','-crf','19','-threads','4',str(target)],check=True)
        segments.append(target)
        position+=duration
        print(f'Scène {i+1}/{len(SCENES)} exportée',flush=True)
    write_subtitles()
    listing=OUT/'segments.txt'
    listing.write_text('\n'.join(f"file '{p.name}'" for p in segments),encoding='utf-8')
    music=soundtrack(position)
    target=OUT/f'blink2video-youtube-{LANGUAGE}.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-i',str(music),'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart','-t',str(position),str(target)],check=True)
    # A matching, code-rendered thumbnail with no generated imagery.
    thumb=Image.new('RGB',(W,H),BG); d=ImageDraw.Draw(thumb)
    text(d,(100,100),'blink2video',60,GREEN,True)
    text(d,(100,245),'BLINK\nSUR VOTRE PC',125,WHITE,True)
    text(d,(106,628),'DIRECT + ARCHIVES',54,GREEN,True)
    shot(thumb,'serve0.fr.PNG',(1050,245,1810,850))
    thumb.resize((1280,720),Image.Resampling.LANCZOS).save(OUT/'miniature-youtube.jpg',quality=94)
    contact=Image.new('RGB',(960,270*((len(SCENES)+1)//2)),BG)
    for i in range(len(SCENES)):
        im=Image.open(OUT/f'scene-{i+1:02}.png').resize((480,270),Image.Resampling.LANCZOS)
        contact.paste(im,((i%2)*480,(i//2)*270))
    contact.save(OUT/'apercu.jpg',quality=92)
    chapters=[]
    for i,s in enumerate(SCENES):
        start=sum(previous[0] for previous in SCENES[:i])
        chapters.append(f'{start//60:02}:{start%60:02} {translate(s[1]).replace(chr(10), " ")}')
    (OUT/'chapitres.txt').write_text('\n'.join(chapters)+'\n',encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'language':LANGUAGE,'duration_seconds':position,'size':[W,H],'fps':FPS,'scenes':[{'start':sum(s[0] for s in SCENES[:i]),'duration':s[0],'title':translate(s[1])} for i,s in enumerate(SCENES)]},ensure_ascii=False,indent=2),encoding='utf-8')
    print(target,flush=True)

if __name__=='__main__': main()
