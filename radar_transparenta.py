#!/usr/bin/env python3
"""
Radar — supraveghere transparență decizională, mai multe ministere.

Ce face, la fiecare rulare:
  1. citește paginile de transparență decizională ale instituțiilor urmărite
  2. compară cu ce e deja arhivat
  3. descarcă arhivele/documentele proiectelor noi
  4. le dezarhivează și le convertește în text
  5. le încadrează pe domenii (cele 20 din Radar)
  6. caută dacă vreun proiect vechi a devenit act publicat
  7. actualizează index.json și afișează ce e nou

Rulare:  python3 radar_transparenta.py
Dependințe:  pip install requests beautifulsoup4
Opțional (pentru .doc vechi):  LibreOffice instalat
"""

import json
import re
import subprocess
import time
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Lipsesc dependințele. Rulează:  pip install requests beautifulsoup4")

# ─────────────────────────────────────────────────────────────
# SURSE — proiecte în transparență decizională
# ─────────────────────────────────────────────────────────────

# instituție, adresă, zile termen observații, domenii implicite, confirmată, pas_doi
#
# „confirmată" = am verificat că adresa răspunde și întoarce proiecte reale.
# Sursele NEconfirmate rămân în listă intenționat, ca să apară zilnic în raport
# la „DE REPARAT". Nu le ștergem — o sursă ștearsă e o gaură pe care o uiți.
#
# „pas_doi" = pagina-listă NU ține documentele; ele stau pe pagina fiecărui
# proiect. Fără pasul doi, radarul găsește zero și raportează liniște falsă.
#
# Diferența contează în raport:
#   sursă confirmată care pică  = ALARMĂ  (s-a stricat ceva; poate ai ratat acte)
#   sursă neconfirmată care pică = TODO   (n-a mers niciodată; de găsit adresa)

SURSE = [
    ("ANAF", "https://www.anaf.ro/anaf/internet/ANAF/transparenta_decizionala/", 10, ["1", "3"], True, False),
    ("Ministerul Finanțelor", "https://mfinante.gov.ro/acasa/transparenta/proiecte-acte-normative", 10, ["1", "3"], True, False),

    # Adresă corectă și pagină vie (verificat 17.09.2026, în browser, 13 documente
    # direct pe pagina-listă). Dacă pică aici, e blocaj de rețea împotriva
    # runnerului, NU adresă moartă. Verifică în browser înainte să cauți alta.
    ("Ministerul Economiei", "https://economie.gov.ro/proiecte-de-acte-normative-aflate-in-consultare-publica/", 30, ["11"], True, False),

    # Verificat 17.09.2026: pagina-listă are ZERO documente. Titlurile trimit la
    # pagina fiecărui proiect, unde stau fișierele. De aici pas_doi=True.
    ("Ministerul Muncii", "https://mmuncii.gov.ro/transparenta-decizionala/", 30, ["2"], True, True),

    # Pagină separată, ține proiectele de fond (ordine + HG), nu inventarele de
    # bunuri care umplu pagina de transparență.
    ("Ministerul Muncii — dezbateri", "https://mmuncii.gov.ro/dezbateri-publice/", 30, ["2"], True, True),

    # Plasa transversală: HG de la TOATE ministerele, inclusiv Muncă și Economie.
    # Târzie (zile înainte de adoptare, nu în fereastra de observații) și doar HG,
    # fără ordine de ministru. Nu înlocuiește sursele ministeriale, le dublează.
    # Neconfirmată: pagina e vie, dar structura paginilor-item nu a fost verificată.
    ("SGG — ședința Guvernului",
     "https://sgg.gov.ro/1/category/proiecte-de-acte-normative-care-ar-putea-fi-incluse-in-sedinta-guvernului-romaniei/",
     0, [], False, True),

    # ── neconfirmate: adresa veche a murit, cea nouă nu e încă găsită ──
    ("Ministerul Transporturilor", "https://www.mt.ro/web14/transparenta-decizionala/consultare-publica/acte-normative-in-avizare", 30, ["4"], False, False),
    ("Vama (AVR)", "https://www.customs.ro/info-publice/transparenta-decizionala", 10, ["14", "8"], False, False),
    ("Ministerul Mediului", "https://www.mmediu.ro/categorie/transparenta-decizionala/1", 30, ["15"], False, False),
    ("Ministerul Agriculturii", "https://www.madr.ro/transparenta-decizionala.html", 30, ["9"], False, False),

    # e-consultare: NU e un substitut pentru sursele ministeriale.
    # Două motive, ambele verificate pe 17.09.2026:
    #   1. fluxul e dominat de hotărâri de consiliu local (Comuna Epureni,
    #      Municipiul Petroșani). Ministerele apar rar.
    #   2. e aplicație cu randare în browser — requests + BeautifulSoup nu văd
    #      nimic, indiferent de adresă. Vechiul URL dădea 500.
    ("e-consultare (agregator)", "https://e-consultare.gov.ro/Consultare-publică", 10, [], False, False),
]

# ─────────────────────────────────────────────────────────────
# SURSE — acte deja publicate (pentru potrivirea proiect → act)
# ─────────────────────────────────────────────────────────────

SURSE_ACTE_PUBLICATE = [
    ("ANAF — alte acte normative",
     "https://www.anaf.ro/anaf/internet/ANAF/asistenta_contribuabili/legislatie/alte_acte_normative/"),
    ("Monitorul Oficial — sumar",
     "https://monitoruloficial.ro/"),
]

# ─────────────────────────────────────────────────────────────
# DOMENII — încadrare pe cuvinte-cheie
# ─────────────────────────────────────────────────────────────

DOMENII = {
    "1":  ("Fiscal general",        ["cod fiscal", "tva", "impozit pe profit", "microîntreprinder",
                                      "cod de procedură fiscală", "taxa pe valoarea adăugată",
                                      "rambursare", "decont", "inspecție fiscală", "antifraud"]),
    "2":  ("Salarizare și muncă",   ["contribuți", "salari", "contract individual de muncă", "revisal",
                                      "inspecția muncii", "securitate în muncă", "concediu",
                                      "pensie", "pensii", "pensionar", "pensionare"]),
    "3":  ("Raportări și declarații", ["declaraț", "formular", "saf-t", "d406", "e-factura", "e-transport",
                                      "e-tva", "raportare", "spv", "e-case de marcat"]),
    "4":  ("Transport rutier",      ["transport rutier", "a.d.r.", "adr", "licență de transport",
                                      "tahograf", "mărfuri periculoase", "vehicul"]),
    "5":  ("Comerț cu amănuntul",   ["casă de marcat", "aparate de marcat", "bon fiscal",
                                      "protecția consumator", "etichetare"]),
    "6":  ("Construcții",           ["construcț", "autorizație de construire", "recepți",
                                      "inspectoratul de stat în construcții", "urbanism"]),
    "7":  ("HoReCa",                ["alimentație publică", "restaurant", "turism gastronomic",
                                      "unități de alimentație"]),
    "8":  ("Accize și carburanți",  ["acciz", "carburant", "motorin", "benzin", "antrepozit fiscal",
                                      "produse energetice", "alcool", "tutun"]),
    "9":  ("Agricultură",           ["agricol", "subvenț", "apia", "fermier", "cereale", "zootehni"]),
    "10": ("Imobiliare",            ["imobil", "locuinț", "cadastru", "carte funciar", "teren"]),
    "11": ("Producție și industrie",["iscir", "sudor", "prescripți tehnic", "instalații sub presiune",
                                      "autorizare tehnic", "industrie"]),
    "12": ("Farma și medical",      ["medicament", "farmac", "dispozitiv medical", "sanitar"]),
    "13": ("IT și servicii",        ["software", "digitalizare", "servicii informatic", "cloud"]),
    "14": ("Import-export",         ["vamal", "vamă", "vama", "antidumping", "taric",
                                      "importul", "importuri", "importator", "export",
                                      "tarif vamal", "declarație vamală"]),
    "15": ("Deșeuri și mediu",      ["deșeu", "mediu", "ambalaj", "reciclare", "emisii", "poluare"]),
    "16": ("Energie și utilități",  ["anre", "energie electric", "gaze natural", "furnizare energie"]),
    "17": ("Turism",                ["agenți de turism", "agenție de turism", "de primire turistic",
                                      "voucher de vacanț", "structur de primire"]),
    "18": ("Finanțări, ajutor de stat", ["ajutor de stat", "schemă de finanțare", "minimis",
                                      "apel de proiecte", "grant", "fonduri europene"]),
    "19": ("Comerț cu ridicata",    ["taxare inversă", "comerț cu ridicata", "distribuți"]),
}

IMPLICITE = {"1", "2", "3"}

# ─────────────────────────────────────────────────────────────
# CONFIGURARE
# ─────────────────────────────────────────────────────────────

ARHIVA = Path("arhiva")
INDEX = ARHIVA / "index.json"
MO_ARHIVA = ARHIVA / "monitorul-oficial"
MO_ZILE_LA_PRIMA_RULARE = 7
MO_ZILE_MAXIM = 21
TIMEOUT = 60
EXTENSII = (".zip", ".pdf", ".doc", ".docx", ".rtf")

# ── Praguri de siguranță ─────────────────────────────────────
# NU sunt filtre de mărime. Un act normativ mare (ordonanță-trenuleț cu 40 de
# anexe) trece fără probleme — cea mai mare arhivă ANAF de până acum are 142 KB.
# Pragurile astea există doar contra unei arhive-bombă: un .zip mic care,
# desfăcut, umflă zeci de gigaocteți și blochează rularea.
#
# REGULA: nimic nu dispare în tăcere. Ce depășește pragul se PĂSTREAZĂ și se
# RAPORTEAZĂ, ca să te uiți tu. Un fals negativ tăcut e mai rău decât o alarmă.
MAX_ARHIVA_DESFACUTA = 400 * 1024 * 1024   # 400 MB desfăcut, total
MAX_FISIER_CONVERSIE = 60 * 1024 * 1024    # peste atât: se arhivează, nu se convertește
MAX_RAPORT_COMPRESIE = 200                 # zip de 1 MB care dă 200 MB = suspect

AVERTISMENTE: list[str] = []               # se afișează la final, vizibil

def avertizeaza(mesaj: str) -> None:
    """Zgomotos și inofensiv, nu tăcut și periculos."""
    AVERTISMENTE.append(mesaj)
    print(f"    !! ATENȚIE: {mesaj}")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# Al doilea set de antete, folosit doar la reincercare. Unele site-uri publice
# refuza un client care arata prea „automat"; altele refuza plaje intregi de IP,
# caz in care nicio schimbare de antet nu ajuta. Distinctia o face rezultatul
# reincercarii, iar raportul o consemneaza.
HEADERS_ALT = dict(HEADERS, **{
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Sec-Fetch-Site": "same-origin",
})

# Coduri care merita reincercate: refuz temporar sau filtru anti-robot.
# 404 NU se reincearca — o pagina mutata ramane mutata.
CODURI_DE_REINCERCAT = {403, 429, 500, 502, 503, 504}


def cere(url: str, *, incercari: int = 3, **kw):
    """O cerere HTTP care nu renunta la primul refuz.

    Reincearca doar la coduri care pot fi trecatoare sau anti-robot, cu pauze
    crescatoare si cu antete schimbate de la a doua incercare. La 404 sau la o
    eroare de retea reala, ridica imediat — nu are rost sa insiste.
    """
    ultima = None
    for i in range(incercari):
        antete = HEADERS if i == 0 else HEADERS_ALT
        if "Referer" not in antete:
            antete = dict(antete, Referer=f"https://{urlparse(url).netloc}/")
        try:
            r = requests.get(url, headers=antete, timeout=TIMEOUT, **kw)
            if r.status_code not in CODURI_DE_REINCERCAT:
                r.raise_for_status()
                return r
            ultima = requests.HTTPError(
                f"{r.status_code} la {url} (incercarea {i + 1} din {incercari})",
                response=r)
        except requests.RequestException as e:
            ultima = e
            if i >= 1:
                raise
        if i < incercari - 1:
            time.sleep(5 * (i + 1))
    raise ultima

# Gazde care apar în subsolul fiecărei pagini guvernamentale și NU sunt proiecte.
# Fără filtrul ăsta, Programul de Guvernare (PDF pe gov.ro, prezent în footer pe
# mmuncii.gov.ro și sgg.gov.ro) intră în arhivă la fiecare rulare ca „proiect nou".
GAZDE_IGNORATE = {"gov.ro", "www.gov.ro"}

LUNI = {"ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4, "mai": 5, "iunie": 6,
        "iulie": 7, "august": 8, "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12}

# „Ordinul ... nr. 352/2022", „O.p.A.N.A.F. nr. 1757/2019", „HG nr. 1175/2007"
TIPAR_ACT = re.compile(r"nr\.?\s*(\d{1,5})\s*/\s*(\d{4})", re.IGNORECASE)

# ─────────────────────────────────────────────────────────────
# UTILITARE
# ─────────────────────────────────────────────────────────────

def incarca_index() -> dict:
    if INDEX.exists():
        d = json.loads(INDEX.read_text(encoding="utf-8"))
        d.setdefault("proiecte", {})
        d.setdefault("acte_vazute", [])
        return d
    return {"proiecte": {}, "acte_vazute": []}

def salveaza_index(index: dict) -> None:
    ARHIVA.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

def parseaza_data(text: str):
    t = text.lower()
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", t)
    if m:
        zi, luna, an = (int(x) for x in m.groups())
        try:
            return datetime(an, luna, zi).date()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})\s+([a-zăâîșțş]+)\s+(\d{4})", t)
    if m:
        zi, luna, an = m.groups()
        if luna in LUNI:
            try:
                return datetime(int(an), LUNI[luna], int(zi)).date()
            except ValueError:
                pass
    return None

def acte_mentionate(text: str) -> set:
    """{'352/2022', '1757/2019'} — actele la care trimite un titlu."""
    return {f"{a}/{b}" for a, b in TIPAR_ACT.findall(text or "")}

def normalizeaza(text: str) -> str:
    t = (text or "").lower()
    for a, b in [("ă", "a"), ("â", "a"), ("î", "i"), ("ș", "s"), ("ş", "s"), ("ț", "t"), ("ţ", "t")]:
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9 ]+", " ", t)

def asemanare(a: str, b: str) -> float:
    return SequenceMatcher(None, normalizeaza(a), normalizeaza(b)).ratio()

_CACHE_TIPARE: dict[str, re.Pattern] = {}

def _tipar_cuvant(cuvant: str) -> re.Pattern:
    """Cuvântul-cheie, căutat DOAR la început de cuvânt.

    Fără asta, „adr" se potrivea în „cadrul", iar un ordin despre doctorate
    ajungea la Transport rutier. Cuvintele rămân trunchiate intenționat
    („contribuți", „construcț"), ca să prindă toate formele — dar trunchierea
    e la coadă, nu la cap.
    """
    if cuvant not in _CACHE_TIPARE:
        _CACHE_TIPARE[cuvant] = re.compile(r"(?<![a-z0-9])" + re.escape(normalizeaza(cuvant)))
    return _CACHE_TIPARE[cuvant]

def incadreaza(titlu: str, implicite: list) -> list:
    """Domeniile în care intră un proiect, după cuvinte-cheie din titlu."""
    t = normalizeaza(titlu)
    gasite = set(implicite)
    for cod, (_, cuvinte) in DOMENII.items():
        if any(_tipar_cuvant(c).search(t) for c in cuvinte):
            gasite.add(cod)
    return sorted(gasite, key=lambda x: int(x))

# ─────────────────────────────────────────────────────────────
# CITIRE PAGINI DE TRANSPARENȚĂ
# ─────────────────────────────────────────────────────────────

def _documente_din_pagina(url: str, nume: str, zile: int, domenii_implicite: list) -> list[dict]:
    """Documentele de pe O pagină. Pasul unu și pasul doi folosesc aceeași logică."""
    r = cere(url)
    sup = BeautifulSoup(r.text, "html.parser")

    gasite, vazute = [], set()
    for a in sup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if not href.lower().split("?")[0].endswith(EXTENSII):
            continue
        # subsolul fiecărei pagini conține Programul de Guvernare. Nu e proiect.
        if urlparse(href).netloc.lower() in GAZDE_IGNORATE:
            continue
        if href in vazute:
            continue
        vazute.add(href)

        bloc = a.find_parent(["li", "tr", "div", "p", "article"]) or a.parent
        context = bloc.get_text(" ", strip=True) if bloc else a.get_text(strip=True)
        data = parseaza_data(context)

        titlu = re.sub(r"\s*\d{1,2}[.\-/ ]\w+[.\-/ ]\d{4}\s*\|?\s*", " ", context)
        for gunoi in ("Detalii proiect", "Descarcă", "Download", "citeste mai mult",
                      "CITEŞTE MAI MULT", "continuă lectura"):
            titlu = titlu.replace(gunoi, "")
        titlu = " ".join(titlu.split())[:400]

        if len(titlu) < 25:          # linkuri de navigare, nu proiecte
            continue

        gasite.append({
            "institutie": nume,
            "url": href,
            "data": data.isoformat() if data else None,
            "titlu": titlu,
            "id": re.sub(r"[^A-Za-z0-9._-]", "_", href.rsplit("/", 1)[-1])[:120],
            "zile_observatii": zile,
            "domenii": incadreaza(titlu, domenii_implicite),
            "acte_modificate": sorted(acte_mentionate(titlu)),
        })
    return gasite


def _linkuri_proiecte(url: str, limita: int = 25) -> list[str]:
    """Linkurile către paginile individuale de proiect, de pe o pagină-listă."""
    r = cere(url)
    sup = BeautifulSoup(r.text, "html.parser")
    gazda = urlparse(url).netloc.lower()

    linkuri, vazute = [], set()
    for a in sup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.netloc.lower() != gazda:
            continue
        if href in vazute or href.rstrip("/") == url.rstrip("/"):
            continue
        # paginile de proiect au slug lung; meniurile au slug scurt
        slug = p.path.rstrip("/").rsplit("/", 1)[-1]
        if len(slug) < 30 or "category" in p.path:
            continue
        vazute.add(href)
        linkuri.append(href)
        if len(linkuri) >= limita:
            break
    return linkuri


def citeste_sursa(nume: str, url: str, zile: int, domenii_implicite: list,
                  pas_doi: bool = False) -> list[dict]:
    gasite = _documente_din_pagina(url, nume, zile, domenii_implicite)
    if gasite or not pas_doi:
        return gasite

    # Pagina-listă nu ține documente. Le ține pagina fiecărui proiect.
    for link in _linkuri_proiecte(url):
        try:
            gasite.extend(_documente_din_pagina(link, nume, zile, domenii_implicite))
        except Exception:
            continue          # o pagină-item ratată nu oprește sursa
    return gasite

# ─────────────────────────────────────────────────────────────
# DESCĂRCARE ȘI EXTRAGERE
# ─────────────────────────────────────────────────────────────

def descarca(url: str, destinatie: Path) -> Path:
    destinatie.parent.mkdir(parents=True, exist_ok=True)
    with cere(url, stream=True) as r:
        with open(destinatie, "wb") as f:
            for bucata in r.iter_content(65536):
                f.write(bucata)
    return destinatie

def desface(cale: Path, unde: Path) -> list[Path]:
    """Dezarhivează, dacă e arhivă. Altfel întoarce fișierul ca atare.

    Verifică ÎNAINTE de desfacere cât declară arhiva că ocupă desfăcută.
    Un act normativ real, oricât de mare, trece. O bombă e oprită și semnalată.
    """
    unde.mkdir(parents=True, exist_ok=True)
    if cale.suffix.lower() != ".zip":
        tinta = unde / cale.name
        tinta.write_bytes(cale.read_bytes())
        return [tinta]

    with zipfile.ZipFile(cale) as z:
        intrari = [i for i in z.infolist() if not i.is_dir()]
        desfacut = sum(i.file_size for i in intrari)
        comprimat = max(sum(i.compress_size for i in intrari), 1)
        raport = desfacut / comprimat

        if desfacut > MAX_ARHIVA_DESFACUTA or raport > MAX_RAPORT_COMPRESIE:
            avertizeaza(
                f"arhiva {cale.name} declară {desfacut / 1048576:.0f} MB desfăcuți "
                f"(raport de compresie {raport:.0f}:1). NU am desfăcut-o. "
                f"Arhiva brută e păstrată la {cale}. Verific-o manual."
            )
            return []                              # nu se desface, dar nici nu se șterge

        fisiere = []
        for i in intrari:
            tinta = unde / Path(i.filename).name   # nume sigur, fără ../
            with z.open(i) as sursa, open(tinta, "wb") as dest:
                dest.write(sursa.read())
            fisiere.append(tinta)
    return fisiere

def in_text(fisier: Path) -> str | None:
    ext = fisier.suffix.lower()

    if fisier.stat().st_size > MAX_FISIER_CONVERSIE:
        avertizeaza(
            f"{fisier.name} are {fisier.stat().st_size / 1048576:.0f} MB — "
            f"e arhivat, dar nu l-am convertit în text (ar bloca rularea). "
            f"Dacă e un act care te interesează, deschide-l direct."
        )
        return None

    if ext == ".docx":
        try:
            import docx
            d = docx.Document(str(fisier))
            parti = [p.text for p in d.paragraphs]
            for t in d.tables:
                for rand in t.rows:
                    parti.append(" | ".join(c.text.strip() for c in rand.cells))
            return "\n".join(parti)
        except Exception:
            pass

    if ext == ".xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(fisier), data_only=True)
            out = []
            for ws in wb.worksheets:
                out.append(f"--- {ws.title} ---")
                for rand in ws.iter_rows(values_only=True):
                    if any(c is not None for c in rand):
                        out.append(" | ".join("" if c is None else str(c) for c in rand))
            return "\n".join(out)
        except Exception:
            pass

    if ext == ".xls":
        try:
            import xlrd
            wb = xlrd.open_workbook(str(fisier))
            out = []
            for ws in wb.sheets():
                out.append(f"--- {ws.name} ---")
                for i in range(ws.nrows):
                    out.append(" | ".join(str(v) for v in ws.row_values(i)))
            return "\n".join(out)
        except Exception:
            pass

    try:  # .doc vechi, .pdf, .rtf — prin LibreOffice
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "txt:Text",
             "--outdir", str(fisier.parent), str(fisier)],
            check=True, capture_output=True, timeout=180,
        )
        txt = fisier.with_suffix(".txt")
        if txt.exists():
            return txt.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass

    return None

# ─────────────────────────────────────────────────────────────
# POTRIVIREA PROIECT → ACT PUBLICAT
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# MONITORUL OFICIAL — sursă de detecție primară
# ─────────────────────────────────────────────────────────────
#
# Portalul Legislativ al Ministerului Justiției (legislatie.just.ro) publică
# integral Partea I a Monitorului Oficial și permite căutare după data
# publicării. E gratuit, oficial și complet — nu e o listă de noutăți
# alcătuită de cineva, ci registrul însuși.
#
# DE CE CONTEAZĂ: până acum actele publicate le aflam dintr-un buletin
# comercial. De aici le aflăm de la sursă. Buletinul rămâne pentru ce face
# el mai bine — forma consolidată și comparația „până acum / de acum".
#
# FORMATUL DATEI: aaaa-ll-zz. NU zz.ll.aaaa — în formatul acela situl
# acceptă valoarea, o afișează înapoi, și o ignoră în tăcere, întorcând
# acte din 1837. Verificat pe 19.09.2026. Dacă schimbi formatul, verifici
# întâi că primele rezultate au anul curent.

MO_CAUTARE = ("https://legislatie.just.ro/Public/RezultateCautare"
              "?page={pag}&op2=AND&op3=AND&op4=AND"
              "&publicatinceputtext={de}&publicatsfarsittext={la}")
MO_DOCUMENT = "https://legislatie.just.ro/Public/DetaliiDocument/{id}"
MO_PAGINI_MAXIM = 40          # 10 rezultate pe pagină; 40 de pagini = 400 de acte
MO_TEXT_MAXIM = 40            # atâtea texte integrale descărcăm într-o rulare
MO_TRECERI_MAXIM = 6          # parcurgeri repetate ale aceleiași zile, până se închide numărul


def _sesiune_mo():
    """Portalul cere o vizită pe prima pagină înainte de căutare."""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.headers["Referer"] = "https://legislatie.just.ro/"
    s.get("https://legislatie.just.ro/", timeout=TIMEOUT)
    return s


def _act_din_bloc(bloc) -> dict | None:
    a = bloc.find("a", href=re.compile("DetaliiDocument"))
    if not a:
        return None
    den = bloc.select_one("span.S_DEN")
    par = bloc.select_one("span.S_PAR")
    emt = bloc.select_one("span.S_EMT_BDY")
    pub = bloc.select_one("span.S_PUB_BDY")
    vig = re.search(r"Data intrarii in vigoare:\s*(.+)", bloc.get_text("\n"))

    def curat(el):
        return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""

    publicat = curat(pub)
    mo_nr = mo_data = None
    m = re.search(r"nr\.\s*([\d.]+)\s+din\s+(\d{1,2}\s+\w+\s+\d{4})", publicat)
    if m:
        mo_nr = m.group(1)
        mo_data = (parseaza_data(m.group(2)) or "").__str__() or None

    titlu = curat(par)
    return {
        "id": a["href"].rsplit("/", 1)[-1],
        "denumire": curat(den),
        "titlu": titlu,
        "emitent": curat(emt),
        "publicat_in": publicat,
        "mo_numar": mo_nr,
        "mo_data": mo_data,
        "vigoare": re.sub(r"\s+", " ", vig.group(1)).strip() if vig else "",
        "url": MO_DOCUMENT.format(id=a["href"].rsplit("/", 1)[-1]),
        "acte_modificate": sorted(acte_mentionate(titlu)),
        "domenii": incadreaza(f"{curat(den)} {titlu}", []),
    }


def _o_trecere(s, zi: str) -> tuple[dict[str, dict], int]:
    """O parcurgere completă a rezultatelor pentru o zi. (acte, total_anunțat)"""
    gasite: dict[str, dict] = {}
    total = None
    servite = 0
    for pag in range(1, MO_PAGINI_MAXIM + 1):
        r = s.get(MO_CAUTARE.format(pag=pag, de=zi, la=zi), timeout=TIMEOUT)
        r.raise_for_status()
        sup = BeautifulSoup(r.text, "html.parser")
        if total is None:
            m = re.search(r"(\d+)\s*document\(e\)",
                          re.sub(r"\s+", " ", sup.get_text(" ", strip=True)))
            total = int(m.group(1)) if m else 0
            if total == 0:
                return {}, 0
        blocuri = sup.select("div.search_result_item")
        if not blocuri:
            break
        servite += len(blocuri)
        for b in blocuri:
            act = _act_din_bloc(b)
            if act:
                gasite.setdefault(act["id"], act)
        if servite >= total:
            break
        time.sleep(0.5)
    return gasite, total


def citeste_o_zi(s, zi: str) -> tuple[list[dict], int, int]:
    """Toate actele publicate în M.Of. Partea I într-o zi. (acte, total, treceri)

    DE CE TRECERI REPETATE: rezultatele nu au o sortare stabilă pe server, așa
    că paginarea sare rânduri — o singură parcurgere poate da 46 din 65 de acte,
    fără niciun semn că lipsește ceva. Repetăm până când numărul de acte
    distincte ajunge la totalul anunțat de sit. Atunci avem dovada că e complet;
    dacă nu ajunge, se raportează, nu se trece cu vederea.
    Verificat pe 19.09.2026: zilele obișnuite se închid din prima trecere,
    cele încărcate din a doua sau a treia.
    """
    strans: dict[str, dict] = {}
    total = 0
    for trecere in range(1, MO_TRECERI_MAXIM + 1):
        acte, total = _o_trecere(s, zi)
        if total == 0:
            return [], 0, trecere
        strans.update({k: v for k, v in acte.items() if k not in strans})
        if len(strans) >= total:
            return list(strans.values()), total, trecere
        time.sleep(0.6)
    return list(strans.values()), total, MO_TRECERI_MAXIM


def citeste_monitorul_oficial(de: str, la: str) -> tuple[list[dict], list[str]]:
    """Actele publicate între cele două date (aaaa-ll-zz), interogate zi cu zi.

    Zi cu zi, nu pe interval: pe interval situl pierde rânduri și nu se poate
    dovedi că am luat tot. Pe zi, totalul anunțat e reperul față de care
    verificăm. Întoarce (acte, zile_incomplete).
    """
    s = _sesiune_mo()
    d0 = datetime.fromisoformat(de).date()
    d1 = datetime.fromisoformat(la).date()
    toate: dict[str, dict] = {}
    incomplete: list[str] = []
    zi = d0
    while zi <= d1:
        z = zi.isoformat()
        try:
            acte, total, treceri = citeste_o_zi(s, z)
        except Exception as e:
            incomplete.append(f"{z}: {str(e)[:80]}")
            zi += timedelta(days=1)
            continue
        if total and len(acte) < total:
            incomplete.append(
                f"{z}: situl anunță {total} acte, am strâns {len(acte)} "
                f"după {treceri} treceri"
            )
        if total:
            print(f"    {z}: {len(acte)}/{total} acte"
                  + (f"  ({treceri} treceri)" if treceri > 1 else ""))
        for a in acte:
            toate.setdefault(a["id"], a)
        zi += timedelta(days=1)
    return list(toate.values()), incomplete


def text_integral_mo(s, act: dict) -> str | None:
    """Textul actului, curățat de meniuri. None dacă nu se poate citi."""
    try:
        r = s.get(act["url"], timeout=TIMEOUT)
        r.raise_for_status()
        sup = BeautifulSoup(r.text, "html.parser")
        for x in sup(["script", "style", "nav", "header", "footer"]):
            x.decompose()
        t = re.sub(r"\n{3,}", "\n\n", sup.get_text("\n", strip=True))
        return t if len(t) > 400 else None
    except Exception:
        return None



def fereastra_mo(index: dict) -> tuple[str, str]:
    """De unde până unde citim. Continuă din ziua următoare ultimei acoperite.

    Dacă radarul n-a rulat câteva zile, fereastra se lărgește singură, ca să
    nu rămână o gaură. Se oprește la MO_ZILE_MAXIM — mai mult înseamnă că
    ceva a stat prea mult și vrem să vezi tu, nu să tragem o lună de acte.
    """
    azi = datetime.now().date()
    ultima = index.get("mo_ultima_zi")
    if ultima:
        de = datetime.fromisoformat(ultima).date() + timedelta(days=1)
    else:
        de = azi - timedelta(days=MO_ZILE_LA_PRIMA_RULARE)
    if (azi - de).days > MO_ZILE_MAXIM:
        de = azi - timedelta(days=MO_ZILE_MAXIM)
    if de > azi:
        de = azi
    return de.isoformat(), azi.isoformat()


def culege_monitorul_oficial(index: dict, alarme: list) -> list[dict]:
    """Culege actele publicate, le salvează în arhivă și le întoarce pe cele noi."""
    de, la = fereastra_mo(index)
    print("\nMONITORUL OFICIAL — acte publicate\n" + "─" * 60)
    print(f"Fereastră: {de} … {la}")

    try:
        acte, incomplete = citeste_monitorul_oficial(de, la)
    except Exception as e:
        print(f"  EROARE: {str(e)[:90]}")
        alarme.append(
            f"Monitorul Oficial (legislatie.just.ro): {str(e)[:110]}\n"
            f"      Asta e sursa de detecție a actelor publicate. Cât e căzută, "
            f"digestul nu are de unde ști ce a apărut.\n"
            f"      https://legislatie.just.ro/"
        )
        return []

    if not acte and not incomplete:
        print("  Niciun act publicat în intervalul acesta.")
        # Zero e un răspuns legitim (weekend, sărbătoare). Nu e alarmă, dar
        # nici nu avansăm ziua acoperită: dacă e o defecțiune tăcută, vrem
        # ca rularea următoare să reîncerce același interval.
        return []

    if incomplete:
        alarme.append(
            "Monitorul Oficial — zile citite incomplet:\n      "
            + "\n      ".join(incomplete)
            + "\n      Actele lipsă nu s-au pierdut, dar nu sunt în digest. "
              "Zilele acestea trebuie reluate.\n      https://legislatie.just.ro/"
        )
    print(f"  {len(acte)} acte în total")

    vazute = set(index.setdefault("acte_vazute", []))
    noi = [a for a in acte if a["id"] not in vazute]
    print(f"  {len(noi)} noi față de rulările anterioare")

    # Textul integral doar pentru actele care ating un domeniu urmărit.
    # Restul rămân cu titlu și legătură — se pot citi oricând, la nevoie.
    de_citit = [a for a in noi if a["domenii"]][:MO_TEXT_MAXIM]
    if de_citit:
        s = _sesiune_mo()
        citite = 0
        for a in de_citit:
            t = text_integral_mo(s, a)
            if t:
                zi = a["mo_data"] or la
                d = MO_ARHIVA / zi / "text"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{a['id']}.txt").write_text(t, encoding="utf-8")
                a["cale_text"] = str(d / f"{a['id']}.txt").replace("\\", "/")
                citite += 1
            else:
                avertizeaza(f"M.Of.: n-am putut citi textul pentru {a['denumire'][:70]} — {a['url']}")
            time.sleep(0.6)
        print(f"  {citite}/{len(de_citit)} texte integrale descărcate "
              f"(doar actele care ating un domeniu urmărit)")

    # Salvăm pe zile, ca să poată fi citite de digest fără să reia căutarea.
    pe_zile: dict[str, list] = {}
    for a in acte:
        pe_zile.setdefault(a["mo_data"] or la, []).append(a)
    MO_ARHIVA.mkdir(parents=True, exist_ok=True)
    for zi, lista in pe_zile.items():
        f = MO_ARHIVA / f"{zi}.json"
        vechi = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
        dupa_id = {x["id"]: x for x in vechi}
        dupa_id.update({x["id"]: x for x in lista})
        f.write_text(json.dumps(sorted(dupa_id.values(), key=lambda x: x["id"]),
                                ensure_ascii=False, indent=2), encoding="utf-8")

    index["acte_vazute"] = sorted(vazute | {a["id"] for a in acte})
    # Dacă o zi a rămas incompletă, nu avansăm dincolo de ea: rularea
    # următoare o reia. Mai bine repetăm o zi decât s-o pierdem.
    prima_stricata = min((r.split(":")[0] for r in incomplete), default=None)
    if prima_stricata:
        ieri = (datetime.fromisoformat(prima_stricata).date() - timedelta(days=1)).isoformat()
        index["mo_ultima_zi"] = min(la, ieri)
    else:
        index["mo_ultima_zi"] = la
    return noi


def citeste_acte_publicate() -> list[dict]:
    """Titluri de acte publicate recent, din sursele configurate."""
    acte = []
    for nume, url in SURSE_ACTE_PUBLICATE:
        try:
            r = cere(url)
            sup = BeautifulSoup(r.text, "html.parser")
            for el in sup.find_all(["li", "tr", "p", "h2", "h3", "a"]):
                text = " ".join(el.get_text(" ", strip=True).split())
                if len(text) < 40 or len(text) > 600:
                    continue
                if not re.search(r"ordin|hot[ăa]r[âa]re|lege|ordonan[țt]", text, re.IGNORECASE):
                    continue
                acte.append({"sursa": nume, "titlu": text[:400],
                             "acte": sorted(acte_mentionate(text)),
                             "data": (parseaza_data(text) or "").__str__() or None})
        except Exception as e:
            print(f"  ! {nume}: {e}")
    return acte

def cauta_potriviri(index: dict) -> list[dict]:
    """Pentru fiecare proiect încă „in_consultare", caută actul publicat."""
    deschise = [p for p in index["proiecte"].values() if p.get("stare") == "in_consultare"]
    if not deschise:
        return []

    print("\nCaut acte publicate care să corespundă proiectelor deschise…")
    # Întâi actele culese din Monitorul Oficial — au denumire oficială,
    # emitent și numărul actelor modificate, deci potrivirea e mult mai sigură
    # decât din titluri răzuite de pe pagini de noutăți.
    acte = []
    for f in sorted(MO_ARHIVA.glob("*.json"), reverse=True)[:60]:
        try:
            for a in json.loads(f.read_text(encoding="utf-8")):
                acte.append({"sursa": f"M.Of. {a.get('mo_numar') or '?'}",
                             "titlu": f"{a['denumire']} {a['titlu']}".strip()[:400],
                             "acte": a.get("acte_modificate", []),
                             "data": a.get("mo_data"),
                             "url": a.get("url")})
        except Exception as e:
            avertizeaza(f"Nu pot citi arhiva M.Of. {f.name}: {e}")
    acte += citeste_acte_publicate()
    if not acte:
        print("  (nicio sursă de acte publicate n-a răspuns)")
        return []
    print(f"  {len(acte)} titluri de acte citite.")

    potriviri = []
    for p in deschise:
        candidati = []
        for act in acte:
            scor, motiv = 0.0, ""

            # TREAPTA 1 — ancoră exactă: același act modificat
            comune = set(p.get("acte_modificate", [])) & set(act["acte"])
            if comune:
                scor = 0.90
                motiv = f"modifică același act: {', '.join(sorted(comune))}"

            # TREAPTA 2 — asemănare de titlu
            s = asemanare(p["titlu"], act["titlu"])
            if s > scor:
                scor, motiv = s, f"titluri asemănătoare ({s:.0%})"
            elif comune and s > 0.5:
                scor = min(0.98, scor + 0.05)
                motiv += f" + titluri asemănătoare ({s:.0%})"

            if scor >= 0.62:
                candidati.append({"scor": round(scor, 2), "motiv": motiv, **act})

        if candidati:
            candidati.sort(key=lambda c: -c["scor"])
            potriviri.append({"proiect": p, "candidati": candidati[:3]})
    return potriviri

# ─────────────────────────────────────────────────────────────
# PRINCIPAL
# ─────────────────────────────────────────────────────────────

def main() -> None:
    index = incarca_index()
    cunoscute = index["proiecte"]
    noi = []

    alarme, de_reparat = [], []

    print("SURSE DE PROIECTE\n" + "─" * 60)
    for nume, url, zile, dom, confirmata, pas_doi in SURSE:
        try:
            gasite = citeste_sursa(nume, url, zile, dom, pas_doi)
            print(f"{nume:<32} {len(gasite):>3} proiecte")
            if not gasite:
                # zero proiecte nu e „e liniște". E ori chiar liniște, ori
                # s-a schimbat structura paginii și nu mai vedem nimic.
                (alarme if confirmata else de_reparat).append(
                    f"{nume}: pagina răspunde, dar n-am găsit niciun document. "
                    f"Ori chiar nu e nimic în consultare, ori s-a schimbat structura paginii."
                )
        except Exception as e:
            mesaj = str(e)
            blocaj = ("Max retries exceeded" in mesaj
                      or "ConnectionError" in type(e).__name__
                      or "SSL" in mesaj)
            print(f"{nume:<32}  EROARE: {mesaj[:70]}")
            if blocaj and confirmata:
                alarme.append(
                    f"{nume}: refuz la nivel de conexiune, nu 404. Adresa e probabil bună — "
                    f"verific-o în browser înainte să cauți alta. Cauza tipică: blocare "
                    f"a IP-urilor de GitHub Actions.\n      {url}"
                )
            else:
                (alarme if confirmata else de_reparat).append(f"{nume}: {mesaj[:110]}\n      {url}")
            continue

        for p in gasite:
            cheie = f"{nume}::{p['id']}"
            if cheie in cunoscute:
                continue

            folder = ARHIVA / (p["data"] or "fara-data") / re.sub(r"[^A-Za-z0-9._-]", "_", nume) / p["id"]
            try:
                fis = descarca(p["url"], folder / p["url"].rsplit("/", 1)[-1][:120])
                fisiere = desface(fis, folder / "continut")
            except Exception as e:
                print(f"    ! {p['titlu'][:60]} — {str(e)[:60]}")
                continue

            texte = folder / "text"
            texte.mkdir(exist_ok=True)
            convertite = []
            for f in fisiere:
                t = in_text(f)
                if t:
                    (texte / (f.stem + ".txt")).write_text(t, encoding="utf-8")
                    convertite.append(f.name)

            termen = None
            if p["data"]:
                d = datetime.fromisoformat(p["data"]).date()
                termen = datetime.fromordinal(d.toordinal() + p["zile_observatii"]).date().isoformat()

            cunoscute[cheie] = {
                **p,
                "termen_observatii": termen,
                "fisiere": [f.name for f in fisiere],
                "convertite": convertite,
                "arhivat_la": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "stare": "in_consultare",
                "act_publicat": None,
                "cale": str(folder).replace("\\", "/"),
            }
            noi.append(cunoscute[cheie])
            print(f"    NOU  {p['titlu'][:70]}  ({len(convertite)}/{len(fisiere)} în text)")

    acte_noi = culege_monitorul_oficial(index, alarme)

    potriviri = cauta_potriviri(index)
    salveaza_index(index)

    # ── RAPORT ──────────────────────────────────────────────
    azi = datetime.now().date()
    print("\n" + "═" * 60)

    if noi:
        print(f"\nPROIECTE NOI — {len(noi)}\n")
        for p in noi:
            dom = ", ".join(DOMENII[d][0] for d in p["domenii"] if d in DOMENII) or "neîncadrat"
            print(f"  {p['institutie']} · {p['data'] or '?'}")
            print(f"  {p['titlu'][:100]}")
            print(f"  domenii: {dom}")
            if p["termen_observatii"]:
                t = datetime.fromisoformat(p["termen_observatii"]).date()
                z = (t - azi).days
                print(f"  termen observații: {p['termen_observatii']}  "
                      f"({z} zile rămase)" if z >= 0 else
                      f"  termen observații: {p['termen_observatii']}  (EXPIRAT de {-z} zile)")
            print(f"  text: {p['cale']}/text/\n")
    else:
        print("\nNiciun proiect nou.\n")

    if acte_noi:
        print("═" * 60)
        cu_domeniu = [a for a in acte_noi if a["domenii"]]
        print(f"\nACTE PUBLICATE ÎN MONITORUL OFICIAL — {len(acte_noi)}, "
              f"din care {len(cu_domeniu)} ating un domeniu urmărit\n")
        for a in sorted(cu_domeniu, key=lambda x: (x["mo_data"] or "", x["denumire"])):
            dom = ", ".join(DOMENII[d][0] for d in a["domenii"] if d in DOMENII)
            print(f"  {a['denumire']}")
            print(f"  {a['titlu'][:110]}")
            print(f"  emitent: {a['emitent'][:70]}")
            print(f"  {a['publicat_in']}  ·  în vigoare: {a['vigoare'] or '?'}")
            print(f"  domenii: {dom}")
            if a.get("acte_modificate"):
                print(f"  modifică: {', '.join(a['acte_modificate'][:6])}")
            if a.get("cale_text"):
                print(f"  text: {a['cale_text']}")
            print(f"  {a['url']}\n")
        fara = len(acte_noi) - len(cu_domeniu)
        if fara:
            print(f"  (încă {fara} acte publicate, fără legătură cu domeniile urmărite —")
            print(f"   sunt în arhivă, la {MO_ARHIVA}/, dacă vrei să te uiți)\n")

    if potriviri:
        print("═" * 60)
        print("\nPOSIBILE POTRIVIRI — de confirmat de tine\n")
        for pot in potriviri:
            print(f"  PROIECT: {pot['proiect']['titlu'][:90]}")
            print(f"           publicat {pot['proiect']['data']}")
            for c in pot["candidati"]:
                print(f"    → {c['scor']:.0%}  {c['titlu'][:85]}")
                print(f"           ({c['motiv']}; sursa: {c['sursa']})")
            print("    Dacă e corect, în index.json pune:")
            print('      "stare": "adoptat",  "act_publicat": "<nr. și M.Of.>"\n')

    if not noi and not potriviri and not acte_noi:
        print("Nimic de raportat. Nu se trimite nimic.")

    if AVERTISMENTE:
        print("\n" + "═" * 60)
        print(f"\nDE VERIFICAT MANUAL — {len(AVERTISMENTE)}\n")
        for a in AVERTISMENTE:
            print(f"  • {a}")
        print("\n  Nimic nu s-a pierdut. Doar nu s-a procesat automat.\n")

    if alarme:
        print("═" * 60)
        print(f"\n /!\\  ALARMĂ — {len(alarme)} surse care mergeau au picat\n")
        for a in alarme:
            print(f"  • {a}")
        print("\n  Astea funcționau. Cât timp sunt căzute, s-ar putea să ratezi acte.\n")

    if de_reparat:
        print("═" * 60)
        print(f"\nDE REPARAT — {len(de_reparat)} surse care n-au mers niciodată\n")
        for a in de_reparat:
            print(f"  • {a}")
        print("\n  Nu e urgent, dar nici nu dispare. Când găsești adresa corectă,")
        print("  o schimbi în SURSE și pui True la sfârșit.\n")


if __name__ == "__main__":
    main()
