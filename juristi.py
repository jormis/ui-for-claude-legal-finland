"""Kyselyn ajo Claude Coden juristi-skillillä (claude -p, stream-json)."""
import html
import json
import os
import re
import shutil
import subprocess
import threading
from email import policy
from email.parser import BytesParser
from pathlib import Path

import storage

CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or str(Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")))
TIMEOUT_SECONDS = int(os.environ.get("JURISTI_TIMEOUT", 30 * 60))
MODEL = os.environ.get("JURISTI_MODEL")  # tyhjä = Claude Coden oletusmalli

SUPPORTED_EXTENSIONS = {".txt", ".md", ".csv", ".eml", ".pdf", ".png", ".jpg", ".jpeg"}

# Vain lukevat työkalut ja oikeuslähteiden MCP-palvelimet; ei komentojen ajoa eikä tiedostojen muokkausta.
ALLOWED_TOOLS = [
    "Skill",
    "Read",
    "Glob",
    "Grep",
    "mcp__plugin_juristi_oik-ai",
    "mcp__claude_ai_Laki_ai",
    "WebFetch(domain:finlex.fi)",
]
DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit"]

SYSTEM_PROMPT = """Toimit suomalaisen oikeuden neuvojana selainkäyttöliittymän takana.
Käyttäjä ei näe työvaiheitasi, vain lopullisen vastauksesi, joten:
- Vastaa suomeksi ja Markdown-muodossa. Aloita lyhyellä vastauksella ydinkysymykseen, perustele sitten.
- Tarkista säädökset ja oikeuskäytäntö oikeuslähdetyökaluilla äläkä lainaa muistista; merkitse viittaukset.
- Tarkista aina, onko sovellettavaa säännöstä muutettu, ja lue muutoslakien voimaantulo- ja
  siirtymäsäännökset. Ratkaise ajallisesti sovellettava sanamuoto asian kannalta olennaisten päivämäärien
  (esim. sopimuksen tekeminen, ilmoituksen antaminen, vahingon sattuminen) perusteella ja kerro
  vastauksessa, mitä sanamuotoa sovelsit ja miksi. Tee tämä myös silloin, kun vastaus pyydetään lyhyenä.
- Lue kaikki liitetiedostot ennen vastaamista. Jos jokin liite ei aukea, kerro se vastauksessa.
- Et voi kysyä käyttäjältä lisätietoja. Jos tiedot ovat puutteelliset, tee perustellut oletukset,
  kerro ne ja kerro mitä lisätietoja tarvittaisiin.
- Nosta esiin määräajat (valitus-, reklamaatio-, vanhentumisajat), jos aineistosta ilmenee sellaisia.
- Lopuksi lyhyt huomautus, ettei vastaus korvaa asianajajan henkilökohtaista neuvontaa."""


# --- Liitteiden esikäsittely ---------------------------------------------------

def _html_to_text(source):
    source = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", source)
    source = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", source)
    source = re.sub(r"<[^>]+>", "", source)
    source = html.unescape(source)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", source).strip()


def _unpack_email(path, out_dir, depth=0):
    """Purkaa .eml-viestin tekstiksi ja tallentaa sen liitteet. Palauttaa luodut polut."""
    with open(path, "rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)
    out_dir.mkdir(parents=True, exist_ok=True)
    created = []

    lines = [f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Date", "Subject") if msg[h]]
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, ValueError):
            text = body.get_payload(decode=True).decode("utf-8", "replace")
        if body.get_content_type() == "text/html":
            text = _html_to_text(text)

    attachments_dir = out_dir / f"{path.stem[:40]}_liitteet"  # lyhyt, Windowsin 260 merkin polkuraja
    attachment_names = []
    for part in msg.iter_attachments():
        name = storage.safe_filename(part.get_filename() or "liite")
        if part.get_content_type() == "message/rfc822":
            data = part.get_payload(0).as_bytes() if part.is_multipart() else part.get_payload(decode=True)
            if not name.lower().endswith(".eml"):
                name += ".eml"
        else:
            data = part.get_payload(decode=True)
        if not data:
            continue
        attachments_dir.mkdir(exist_ok=True)
        target = attachments_dir / name
        target.write_bytes(data)
        attachment_names.append(name)
        if name.lower().endswith(".eml") and depth < 2:
            created += _unpack_email(target, attachments_dir, depth + 1)
        else:
            created.append(target)

    if attachment_names:
        lines.append("Liitteet: " + ", ".join(attachment_names))
    text_file = out_dir / f"{path.stem}.txt"
    text_file.write_text("\n".join(lines) + "\n\n" + text, encoding="utf-8")
    return [text_file] + created


def prepare_files(query_id):
    """Palauttaa listan (polku, kuvaus) Claudelle luettavaksi."""
    folder = storage.query_dir(query_id)
    result = []
    for f in storage.get_query(query_id)["files"]:
        path = folder / f["name"]
        if path.suffix.lower() == ".eml":
            try:
                for p in _unpack_email(path, folder / "_purettu"):
                    result.append((p, f"purettu sähköpostista {f['name']}"))
            except Exception as exc:  # viallinen viesti: annetaan Clauden yrittää raakatiedostoa
                result.append((path, f"sähköposti, purku epäonnistui: {exc}"))
        else:
            result.append((path, "käyttäjän lataama"))
    return result


# --- Edistymisviestit ------------------------------------------------------------

_TOOL_LABELS = {
    "search_law": "Haetaan lainsäädäntöä",
    "search_case_law": "Haetaan oikeuskäytäntöä",
    "search_preparatory_works": "Haetaan lainvalmisteluaineistoa",
    "search_guidance": "Haetaan viranomaisohjeita",
    "search_legal_sources": "Haetaan oikeuslähteitä",
    "search_within_documents": "Haetaan asiakirjojen sisältä",
    "read_document": "Luetaan oikeuslähdettä",
    "get_statute_section_history": "Tarkistetaan pykälän historia",
    "get_table_of_contents": "Luetaan sisällysluetteloa",
    "resolve_reference": "Tunnistetaan viittaus",
    "list_related": "Haetaan liittyviä asiakirjoja",
    "lookup_company": "Haetaan yritystietoja",
    "begin_research": "Aloitetaan oikeuslähdetutkimus",
    "finalize_research_graph": "Kootaan lähteet",
}


def _describe_tool(name, args):
    if name == "Skill":
        return f"Otetaan käyttöön ohje: {args.get('skill', '')}"
    if name == "Read":
        return f"Luetaan tiedosto: {Path(args.get('file_path', '')).name}"
    if name in ("Glob", "Grep"):
        return "Etsitään tiedostoista"
    if name == "WebFetch":
        return f"Haetaan verkkosivu: {args.get('url', '')}"
    label = _TOOL_LABELS.get(name.split("__")[-1], name)
    detail = args.get("query") or args.get("id") or args.get("reference") or ""
    if isinstance(detail, str) and detail:
        label += f": {detail[:120]}"
    return label


# --- Ajo ----------------------------------------------------------------------

def _build_prompt(query, files):
    prompt = "/juristi:juristi\n\nKäyttäjän kysymys:\n" + query.strip() + "\n"
    if files:
        prompt += "\nLiitetiedostot (lue Read-työkalulla, PDF:t sivuittain tarvittaessa):\n"
        prompt += "".join(f"- {p} ({desc})\n" for p, desc in files)
    return prompt


def _run(query_id):
    item = storage.get_query(query_id)
    folder = storage.query_dir(query_id)
    try:
        files = prepare_files(query_id)
    except Exception as exc:
        storage.finish_query(query_id, error=f"Liitteiden käsittely epäonnistui: {exc}")
        return

    # Kysymys annetaan stdinin ja järjestelmäkehote tiedoston kautta: komentoriville ei päädy
    # rivinvaihtoja eikä pitkää tekstiä (Windowsin .cmd-käynnistimet ja 32 767 merkin raja).
    system_prompt_file = storage.DATA_DIR / "jarjestelmakehote.txt"
    system_prompt_file.write_text(SYSTEM_PROMPT, encoding="utf-8")
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", ",".join(DISALLOWED_TOOLS),
        "--append-system-prompt-file", str(system_prompt_file),
        "--add-dir", str(folder),
        "--no-session-persistence",
    ]
    if MODEL:
        cmd += ["--model", MODEL]

    storage.append_progress(query_id, "Käynnistetään juristi")
    try:
        proc = subprocess.Popen(
            cmd, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        storage.finish_query(query_id, error=f"Claude Codea ei voitu käynnistää ({CLAUDE_BIN}): {exc}")
        return
    try:
        proc.stdin.write(_build_prompt(item["query"], files))
        proc.stdin.close()
    except OSError:
        pass  # prosessi kaatui heti; virhe näkyy stderr:ssä

    timed_out = threading.Event()

    def _kill():
        # Erillinen lippu, koska tapetun prosessin paluukoodi on eri macOS:llä ja Windowsilla.
        timed_out.set()
        proc.kill()

    timer = threading.Timer(TIMEOUT_SECONDS, _kill)
    timer.start()
    stderr_chunks = []
    stderr_thread = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    stderr_thread.start()

    result = None
    last_text = ""
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") != "ToolSearch":
                        storage.append_progress(query_id, _describe_tool(block["name"], block.get("input") or {}))
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        last_text = block["text"]
            elif event.get("type") == "result":
                result = event
        proc.wait()
    finally:
        timer.cancel()
        stderr_thread.join(timeout=5)

    if result and not result.get("is_error") and (result.get("result") or last_text):
        storage.finish_query(query_id, answer=result.get("result") or last_text,
                             cost_usd=result.get("total_cost_usd"))
    elif timed_out.is_set():
        storage.finish_query(query_id, error=f"Aikaraja ({TIMEOUT_SECONDS // 60} min) ylittyi.")
    else:
        detail = (result or {}).get("result") or "".join(stderr_chunks).strip()[-2000:] or f"paluukoodi {proc.returncode}"
        storage.finish_query(query_id, error=f"Neuvon laatiminen epäonnistui: {detail}")


def start(query_id):
    threading.Thread(target=_run, args=(query_id,), daemon=True).start()
