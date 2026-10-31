# Juristi – paikallinen käyttöliittymä suomalaisiin oikeudellisiin kysymyksiin

Selainkäyttöliittymä, joka välittää kysymyksen ja liitetiedostot Claude Codelle ja
käyttää vastaamiseen `juristi`-skilliä (plugin `juristi@claude-for-legal-finland`)
sekä oik.ai- ja Laki.ai-oikeuslähdetyökaluja.

## Käynnistys

    python3 server.py            # avaa selaimen osoitteeseen http://127.0.0.1:8765/
    python3 server.py --port 9000 --no-browser

Vaatimukset: Python 3.9+ (ei lisäkirjastoja) ja kirjautunut `claude`-komentorivityökalu,
johon juristi-plugin on asennettu.

### Windows

    py server.py

Kysymys välitetään Claudelle stdinin kautta, joten pitkät kysymykset ja rivinvaihdot toimivat myös
npm-asennuksen `claude.cmd`-käynnistimellä. Jos `claude` ei ole PATHissa, aseta `CLAUDE_BIN`
(esim. `set CLAUDE_BIN=C:\Users\nimi\.local\bin\claude.exe`). Hyvin syvälle sisäkkäisten
sähköpostiliitteiden kanssa kannattaa sijoittaa sovellus lyhyeen polkuun tai ottaa Windowsin
pitkät polut käyttöön (LongPathsEnabled).

## Toiminta

- **Kysymys** kirjoitetaan tekstikenttään, liitteet pudotetaan latausalueelle.
  Tuetut tiedostot: `.txt`, `.md`, `.csv`, `.eml`, `.pdf`, `.png`, `.jpg` (enintään 25 Mt).
- **Anna neuvo** käynnistää taustalla ajon `claude -p "/juristi:juristi …"`.
  Edistyminen (haut, luetut lähteet) näkyy näytöllä, ja valmis vastaus näytetään Markdown-muotoiltuna.
- Sähköpostit (`.eml`) puretaan tekstiksi, ja niiden liitteet (esim. PDF:t) annetaan myös luettaviksi.
  PDF-tiedostot Claude lukee itse.
- **Aiemmat kyselyt**: haku kohdistuu kysymyksiin, vastauksiin ja tiedostonimiin, ja tulokset voi järjestää kyselyajan mukaan.

## Tallennus

Kaikki tallennetaan paikallisesti hakemistoon `data/` (muutettavissa ympäristömuuttujalla `JURISTI_DATA`):
`data/kyselyt.db` (SQLite) ja `data/tiedostot/<kyselyn id>/` (liitteet ja puretut sähköpostit).
Palvelin kuuntelee vain osoitetta 127.0.0.1.

## Turvarajaukset

Claude saa käyttää vain lukevia työkaluja (Read, Glob, Grep, Skill), oikeuslähde-MCP:itä ja
Finlexin verkkosivuja. Komentojen ajo ja tiedostojen muokkaus on estetty.

Muut asetukset: `JURISTI_MODEL` (esim. `opus`), `JURISTI_TIMEOUT` (sekunteina, oletus 1800), `CLAUDE_BIN`.
