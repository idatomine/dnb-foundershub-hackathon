# Satselaget: Atech-hackathon

Du trenger Python 3.10 eller nyere. Oppsettet er testet med 3.12. Kjør dette fra rota av repoet.

Windows (PowerShell). `py -0p` viser hvilke versjoner du har. Bytt ut `3.12` hvis du bruker en annen:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
```

macOS eller Linux (ikke testet):

```bash
python3 -m venv .venv
source .venv/bin/activate
python --version
python -m pip install -r requirements.txt
```

`python --version` skal vise 3.10 eller nyere. Viser den en eldre versjon, får du feilen `No matching distribution found for atech`. Slett da `.venv` og lag miljøet på nytt med en nyere Python.

Aktiver miljøet i hver nye terminal.

## Test oppsettet

```bash
python demo/host.py --sim
atech validate demo
atech build demo
python demo/full_sim_test.py
```

Du er klar når `host.py` skriver `Simulering fullført.`, `validate` skriver `ok`, `build` skriver `SUCCESS` og testen skriver `20 av 20 bestått`. Den første `atech build` laster ned kompilatoren på ca. 2,4 GB.

Testene bruker et simulert kort og kjører ikke firmwaren. Opplasting og modulene må testes på kortet.

## Bygg

Åpne Claude Code i rota av repoet og beskriv hva dere vil bygge. Atechs prompt ligger i `CLAUDE.md`. Andre agenter kan få den limt inn.

Sett API-nøkkelen som miljøvariabelen `ANTHROPIC_API_KEY`. Ikke legg den i koden eller i git.

## Hva som kommer fra Atech

| Fil | Kilde |
|---|---|
| `CLAUDE.md` | Prompten fra atech.dev/docs |
| `demo/AGENTS.md`, `demo/CLAUDE.md`, `demo/README.md` | Generert av `atech new` |
| `docs/llms.txt` | atech.dev/llms.txt |
| `.mcp.json` | Adressen til Atechs MCP-kobling. Ikke prøvd ennå. |
| `demo/project.yaml` | Laget selv i Atechs format. Portene er antatt, så sjekk dem før opplasting. |
| `demo/host.py` | Laget selv. Tester Python-siden med SDK-ens `MockTransport`, og kjører ikke firmwaren. |
| `demo/full_sim_test.py` | Laget selv. Tester SDK-ens USB-kode mot en falsk port, og sammenligner med generert firmware. |
| `README.md`, `requirements.txt`, `.gitignore` | Laget selv |

Hjelp: `atech guide`, [atech.dev/docs](https://atech.dev/docs) og [Discord](https://discord.gg/B6kuhUW27S).
