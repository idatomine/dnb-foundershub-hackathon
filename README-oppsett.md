## Oppsett

Krever Python 3.10 eller nyere. Testet på Mac med Apple Silicon (kjører på GPU via `mps`), men fungerer også på CPU.

### 1. Hent koden og lag et virtuelt miljø

```bash
git clone https://github.com/idatomine/satselag-hacakthon.git
cd satselag-hacakthon
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

Har du allerede repoet, holder det med `git pull` før `pip install -r requirements.txt`.

### 2. Last ned bildemodellen (ca. 1,7 GB, én gang)

```bash
python -c "from transformers import pipeline; pipeline('zero-shot-image-classification', model='openai/clip-vit-large-patch14-336'); print('ferdig')"
```

Modellen lagres i Hugging Face-cachen i hjemmemappa, ikke i repoet:

```text
~/.cache/huggingface/hub/models--openai--clip-vit-large-patch14-336
```

Er nettet tregt, kan en på laget sende denne mappa med AirDrop. Legg den på samme sted, så slipper du nedlastingen.

### 3. Gi terminalen tilgang til kameraet (macOS)

```bash
python -c "import cv2; c=cv2.VideoCapture(0); print('kamera ok' if c.read()[0] else 'kamera FEIL'); c.release()"
```

Får du `kamera FEIL`: gå til Systeminnstillinger → Personvern og sikkerhet → Kamera, slå på terminalappen du bruker, start terminalen på nytt og prøv igjen.

## Bildemodellen: `vision_material.py`

Gir sannsynlighet for fem materialer ut fra ett bilde: **tekstil, metall, tre, plast og glass**. Bruker en ferdigtrent CLIP-modell uten egen trening (zero-shot).

### Kjør fra terminalen

```bash
python vision_material.py test.jpg     # fra en bildefil
python vision_material.py --webcam     # tar bilde med webkameraet
```

Eksempel på utdata:

```text
{"textile": 0.05, "metal": 0.86, "wood": 0.01, "plastic": 0.05, "glass": 0.03}
Inferens: 715 ms (mps)
METALL 86.0% | TEKSTIL 5.0% | PLAST 5.0% | GLASS 3.0% | TRE 1.0%
```

Med `--webcam` lagres bildet i `captures/`. Modellen lastes først (ca. 7–10 sekunder), deretter tas bildet. Hold objektet i ro til terminalen skriver `Lagret …`.

### Bruk fra kode

```python
from vision_material import predict_material, capture_webcam_image

bilde = capture_webcam_image()
p = predict_material(bilde)   # eller predict_material("test.jpg")
# {"textile": float, "metal": float, "wood": float, "plastic": float, "glass": float}, summerer til 1
```

Nøklene er alltid `textile`, `metal`, `wood`, `plastic` og `glass`. Lyd- og Bayes-delen må bruke de samme nøklene.

### For best resultat

- La objektet fylle mesteparten av bildet.
- Bruk ensfarget bakgrunn, for eksempel et hvitt ark. Et trebord i bakgrunnen kan få modellen til å svare «tre».
- Godt og jevnt lys. Bruk samme oppsett i demoen som når dere tester.

### Feilsøking

| Problem | Løsning |
|---|---|
| `kamera FEIL` | Gi terminalen kameratilgang (se steg 3). Lyser kameraet uten at det virker, prøv `--camera 1`. |
| `command not found: ❯` | Du har kopiert med `❯`-tegnet foran kommandoen. Skriv kommandoen uten. |
| `ModuleNotFoundError` | Venv-en er ikke aktiv. Kjør `source .venv/bin/activate`. |
| «Redirects are currently not supported in MacOS» | Ufarlig melding fra torch. Kan ignoreres. |
