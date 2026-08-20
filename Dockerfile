FROM python:3.12-slim

# Le ffmpeg livre par imageio-ffmpeg pour Linux est compile sans libfreetype,
# donc sans le filtre drawtext qui incruste l'heure dans l'image (voir
# find_ffmpeg() dans merge_daily.py, et le meme choix dans .github/ci.yml).
# Celui de la distribution l'a.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Comptes, reglages et clips vivent ici, separes du code : une image mise a
# jour ne doit jamais se retrouver masquee par un volume nomme qui garde
# l'ancienne version au meme chemin. runtime.app_dir() sait deja deplacer
# tout le dossier de donnees via BLINK_HOME, rien d'autre a cabler.
ENV BLINK_HOME=/data
VOLUME /data

# 127.0.0.1 par defaut (voir serve.py) designerait la boucle locale du
# conteneur, injoignable depuis l'hote meme avec le port publie : le reseau
# en pont route vers l'interface du conteneur, pas sa boucle locale. La
# frontiere de securite reste posee par la publication du port elle-meme
# (docker-compose.yml : 127.0.0.1: en dur), pas par cette adresse d'ecoute.
ENV BLINK_BIND=0.0.0.0

EXPOSE 8765
CMD ["python", "blink2video.py", "start"]
