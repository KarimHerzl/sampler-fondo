# coding: utf-8
# Sampler del fondo - legge le ortofoto regionali/nazionali (WMS) e classifica
# ogni punto come asfalto / sterrato / coperto, per risolvere i tratti "grigi".
# Struttura a REGISTRO SORGENTI: aggiungere una regione o nazione = una riga.

import io, math, os, re
from flask import Flask, request, jsonify
import requests
from PIL import Image

app = Flask(__name__)

# ======================= REGISTRO SORGENTI =======================
# bbox = [lon_min, lat_min, lon_max, lat_max]. L'ordine conta: prima le
# regionali ad alta risoluzione, poi i fallback nazionali.
# NB: i "layer" con nota (confermare) vanno verificati con /caps?url=... al 1o giro.
SOURCES = [
    {
        "name": "Piemonte AGEA 2024",
        "bbox": [6.62, 44.06, 9.21, 46.46],
        "url":  "https://opengis.csi.it/mp/regp_agea_2024",
        "layer": "regp_agea_2024",                      # (confermare con /caps)
        "crs":  "EPSG:3857", "res_cm": 30,
        "attr": "Ortofoto AGEA 2024 - Regione Piemonte",
    },
    {
        "name": "Emilia-Romagna AGEA 2023 RGB",
        "bbox": [9.15, 43.70, 12.85, 45.15],
        "url":  "https://servizigis.regione.emilia-romagna.it/wms/agea2023_rgb",
        "layer": "Agea2023_RGB",                        # (confermare con /caps)
        "crs":  "EPSG:3857", "res_cm": 20,
        "attr": "Ortofoto AGEA 2023 - Regione Emilia-Romagna",
    },
    {
        "name": "Veneto AGEA 2024",
        "bbox": [10.60, 44.75, 13.10, 46.70],
        "url":  "https://idt2-geoserver.regione.veneto.it/geoserver/ows",
        "layer": "rv:ortofoto_agea_2024",               # confermato via /caps
        "crs":  "EPSG:3857", "res_cm": 20,
        "attr": "Ortofoto AGEA 2024 - Regione del Veneto (CC-BY/IODL)",
    },
    {
        "name": "Friuli Venezia Giulia ortofoto",
        "bbox": [12.30, 45.55, 13.95, 46.65],
        "url":  "https://irdat-ortofoto.regione.fvg.it/geoserver/ortofoto/ows",
        "layer": "trueorto_FVG_1720",                   # confermato via /caps (2017-2020)
        "crs":  "EPSG:3857", "res_cm": 20,
        "attr": "True ortofoto 2017-2020 - Regione FVG (IRDAT)",
    },
    {
        "name": "Lombardia AGEA (ortofoto)",
        "bbox": [8.45, 44.65, 11.45, 46.65],
        "url":  "https://www.cartografia.servizirl.it/arcgis2/services/BaseMap/ortofoto2012UTM/ImageServer/WMSServer",
        "layer": "0",                                   # (confermare con /caps)
        "crs":  "EPSG:3857", "res_cm": 30,
        "attr": "Ortofoto AGEA - Regione Lombardia (uso: consultazione pubblica)",
    },
    {
        "name": "Toscana ortofoto (GEOscopio)",
        "bbox": [9.60, 42.20, 12.45, 44.50],
        "url":  "http://www502.regione.toscana.it/wmsraster/com.rt.wms.RTmap/wms?map=wmsofc",
        "layer": "rt_ofc.10k13",                        # confermato via /caps (2013, 10k)
        "crs":  "EPSG:3857", "res_cm": 20,
        "attr": "Ortofoto - Regione Toscana (GEOscopio)",
    },
    {
        "name": "France IGN BD ORTHO",
        "bbox": [-5.5, 41.0, 9.8, 51.6],
        "url":  "https://data.geopf.fr/wms-r/wms",
        "layer": "ORTHOIMAGERY.ORTHOPHOTOS",
        "crs":  "EPSG:3857", "res_cm": 20,
        "attr": "BD ORTHO - IGN France",
    },
    # --- pronte per il futuro (endpoint gia' noti) ---
    # Marche:  http://wms.cartografia.marche.it/geoserver/Ortofoto/wms
    # Liguria: https://geoservizi.regione.liguria.it/geoserver/...
    # Lazio / Puglia / Abruzzo / ... ; Spagna PNOA; Italia nazionale (PCN)
]

NIR_SOURCES = [
    {
        "name": "Piemonte ICE NIR 2009-2011",
        "bbox": [6.62, 44.06, 9.21, 46.46],
        "url":  "https://geomap.reteunitaria.piemonte.it/mapproxy/service",
        "layer": "regp_ortofoto_ice_nir_2010",
        "crs":  "EPSG:3857", "res_cm": 45,
        "attr": "Ortofoto ICE NIR 2009-2011 - Regione Piemonte (CC-BY)",
    },
    # future: Emilia agea..._nir ; Toscana rt_ofc...4R1G2B (NIR nel canale rosso)
]

def pick_nir(lon, lat):
    for s in NIR_SOURCES:
        b = s["bbox"]
        if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            return s
    return None

def pick_source(lon, lat):
    c = candidates(lon, lat)
    return c[0] if c else None

def candidates(lon, lat):
    out = []
    for s in SOURCES:
        b = s["bbox"]
        if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            out.append(s)
    return out

def is_blank(img):
    # immagine vuota/nera/uniforme = la sorgente non copre davvero questo punto
    w, h = img.size
    px = img.load()
    mn, mx = 255, 0
    for yy in range(0, h, 4):
        for xx in range(0, w, 4):
            v = max(px[xx, yy])
            if v < mn: mn = v
            if v > mx: mx = v
    return mx < 12 or (mx - mn) < 3

def fetch_first_good(lon, lat, half_m=0.4, px=64):
    # prova le sorgenti in ordine e usa la prima che restituisce un'immagine vera
    last = None
    for src in candidates(lon, lat):
        try:
            img = fetch_image(src, lon, lat, half_m=half_m, px=px)
            if not is_blank(img):
                return src, img
            last = (src, img)
        except Exception:
            continue
    return last if last else (None, None)

# ======================= WMS GetMap =======================
def _merc(lon, lat):
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return x, y * 20037508.34 / 180.0

def fetch_image(src, lon, lat, half_m=0.4, px=64):
    crs = src.get("crs", "EPSG:3857")
    if crs == "EPSG:3857":
        x, y = _merc(lon, lat)
        bbox = "%f,%f,%f,%f" % (x - half_m, y - half_m, x + half_m, y + half_m)
    else:
        dlat = half_m / 111320.0
        dlon = half_m / (111320.0 * math.cos(math.radians(lat)))
        if crs == "CRS:84":
            bbox = "%f,%f,%f,%f" % (lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        else:  # EPSG:4326 in WMS 1.3.0 -> ordine lat,lon
            bbox = "%f,%f,%f,%f" % (lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": src["layer"], "STYLES": "",
        "CRS": crs, "BBOX": bbox,
        "WIDTH": px, "HEIGHT": px, "FORMAT": "image/jpeg",
    }
    r = requests.get(src["url"], params=params, timeout=25,
                     headers={"User-Agent": "TracciatoriCarbonari/1.0"})
    ct = r.headers.get("content-type", "")
    if "image" not in ct:
        raise RuntimeError("il WMS non ha restituito un'immagine (%s): %s"
                           % (ct, r.text[:180]))
    return Image.open(io.BytesIO(r.content)).convert("RGB")

# ======================= FEATURE + CLASSIFICAZIONE =======================
def features(img):
    w, h = img.size
    px = img.load()
    m0, m1 = int(w*0.20), int(w*0.80)
    x0, x1, y0, y1 = m0, m1, m0, m1
    Ls, exg, warm, sat = [], [], [], []
    for yy in range(y0, y1):
        for xx in range(x0, x1):
            R, G, B = px[xx, yy]
            s = R + G + B + 1e-6
            Ls.append((0.299 * R + 0.587 * G + 0.114 * B) / 255.0)
            exg.append((2.0 * G - R - B) / s)
            warm.append((R - B) / 255.0)
            mx = max(R, G, B); mn = min(R, G, B)
            sat.append((mx - mn) / (mx + 1e-6))          # 0=grigio, alto=colorato
    n = len(Ls)
    mean = lambda a: sum(a) / len(a)
    L, ExG, WARM, SAT = mean(Ls), mean(exg), mean(warm), mean(sat)
    TEX = (sum((v - L) ** 2 for v in Ls) / n) ** 0.5
    return {"L": round(L, 3), "ExG": round(ExG, 3), "WARM": round(WARM, 3),
            "SAT": round(SAT, 3), "TEX": round(TEX, 3)}

def uniformity(img):
    # deviazione standard di luminosita' e tono su una finestra piu' larga:
    # bassa = superficie uniforme (asfalto) ; alta = screziata (terra/ghiaia)
    w, h = img.size
    px = img.load()
    x0, x1, y0, y1 = int(w*0.15), int(w*0.85), int(h*0.15), int(h*0.85)
    Ls, Ws = [], []
    for yy in range(y0, y1):
        for xx in range(x0, x1):
            R, G, B = px[xx, yy]
            Ls.append((0.299*R + 0.587*G + 0.114*B) / 255.0)
            Ws.append((R - B) / 255.0)
    def std(a):
        m = sum(a)/len(a)
        return (sum((v-m)**2 for v in a)/len(a)) ** 0.5
    return {"UNIF_L": round(std(Ls), 3), "UNIF_W": round(std(Ws), 3)}

def classify(f):
    # REGOLA v12 - tarata su 118 punti etichettati dal campionatore (Piemonte).
    # Grid search con obiettivo: ZERO errori pericolosi (asfalto->verde e viceversa).
    # Risultato sul dataset: 0 verdi falsi, 0 rossi falsi; decide sul ~20% dei punti,
    # con accuratezza 100% dove decide. Il resto e' onestamente "incerto".
    w = f.get("WARM", 0); L = f.get("L", 0)
    if L < 0.25 or w < -0.02:   return "coperto"    # ombra / non leggibile
    if f["ExG"] > 0.15:         return "coperto"    # vegetazione
    if w >= 0.13 and L >= 0.70: return "sterrato"   # terroso netto e chiaro
    if w <= 0.06:               return "asfalto"    # grigio neutro netto
    return "incerto"

def edges(img):
    # Nitidezza dei bordi su finestra larga: misura diagnostica (non usata dalla regola)
    w, h = img.size
    px = img.load()
    lum = []
    for yy in range(h):
        row = []
        for xx in range(w):
            R, G, B = px[xx, yy]
            row.append((0.299*R + 0.587*G + 0.114*B) / 255.0)
        lum.append(row)
    mags = []
    for yy in range(1, h-1):
        for xx in range(1, w-1):
            gx = lum[yy][xx+1] - lum[yy][xx-1]
            gy = lum[yy+1][xx] - lum[yy-1][xx]
            mags.append((gx*gx + gy*gy) ** 0.5)
    if not mags:
        return {}
    mags.sort()
    n = len(mags)
    p50 = mags[int(n*0.50)]; p95 = mags[int(n*0.95)]; mx = mags[-1]
    mean = sum(mags)/n
    return {"EDGE_P95": round(p95, 3), "EDGE_MAX": round(mx, 3),
            "EDGE_MED": round(p50, 3), "SHARP": round(p95/(mean + 1e-6), 2)}


def classify_smart(src, lon, lat, half=0.4):
    # lettura centrale (con salto automatico delle sorgenti vuote)
    src2, img = fetch_first_good(lon, lat, half_m=half)
    if src2 is not None:
        src = src2
    f = features(img if img is not None else fetch_image(src, lon, lat, half_m=half))
    g = classify(f)
    if g in ("sterrato", "asfalto"):
        return g, f
    # traccia a due solchi: il centro e' erboso/ambiguo -> provo i lati (~1.2 m)
    d = 1.2
    dlat = d / 111320.0
    dlon = d / (111320.0 * math.cos(math.radians(lat)))
    for (ala, alo) in ((dlat, 0), (-dlat, 0), (0, dlon), (0, -dlon)):
        try:
            f2 = features(fetch_image(src, lon + alo, lat + ala, half_m=half))
            if classify(f2) == "sterrato":
                f2["nota"] = "solco laterale"
                return "sterrato", f2
        except Exception:
            pass
    return g, f


# ======================= ENDPOINT =======================
@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route("/")
def home():
    return "Sampler fondo v15 (Veneto e FVG confermati). /sources | /caps | /surface/test?lat=45.09&lon=8.48"

@app.route("/sources")
def sources():
    return jsonify([{"name": s["name"], "res_cm": s["res_cm"],
                     "bbox": s["bbox"], "layer": s["layer"]} for s in SOURCES])

@app.route("/caps")
def caps():
    # scopre i nomi dei layer di un WMS (o di tutte le sorgenti se senza ?url=)
    url = request.args.get("url")
    if not url:
        return jsonify([{"name": s["name"], "url": s["url"]} for s in SOURCES])
    try:
        full = url + ("&" if "?" in url else "?") + "SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0"
        r = requests.get(full, timeout=25, headers={"User-Agent": "TracciatoriCarbonari/1.0"})
        names = re.findall(r"<Name>\s*([^<]+?)\s*</Name>", r.text)
        return jsonify({"status": r.status_code, "n": len(names), "layers": names[:80]})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/surface/test")
def surface_test():
    try:
        lon = float(request.args["lon"]); lat = float(request.args["lat"])
    except Exception:
        return jsonify({"error": "usa ?lat=..&lon=.."}), 400
    src = pick_source(lon, lat)
    if not src:
        return jsonify({"error": "nessuna sorgente copre questo punto"}), 404
    try:
        half = float(request.args.get("half", 0.4))
        src2, img = fetch_first_good(lon, lat, half_m=half)
        if src2 is not None:
            src = src2
        f = features(img if img is not None else fetch_image(src, lon, lat, half_m=half))
        try:
            f.update(uniformity(fetch_image(src, lon, lat, half_m=1.5, px=64)))
        except Exception:
            pass
        try:
            f.update(edges(fetch_image(src, lon, lat, half_m=4.0, px=64)))
        except Exception:
            pass
        g = classify(f)
        if g not in ("sterrato", "asfalto"):
            g, f2 = classify_smart(src, lon, lat, half=half)
            f2.update({k: v for k, v in f.items() if k.startswith("EDGE") or k in ("SHARP", "UNIF_L", "UNIF_W")})
            f = f2
        return jsonify({"source": src["name"], "res_cm": src["res_cm"],
                        "finestra_m": round(2 * half, 1),
                        "features": f, "guess": g})
    except Exception as e:
        return jsonify({"source": src["name"], "error": str(e)}), 502

@app.route("/surface", methods=["POST", "OPTIONS"])
def surface():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True, silent=True) or {}
    pts = data.get("points", [])
    out = []
    warms = []
    for p in pts:
        try:
            lon, lat = float(p[0]), float(p[1])
        except Exception:
            out.append({"guess": "input-non-valido"}); continue
        src = pick_source(lon, lat)
        if not src:
            out.append({"guess": "nessuna-sorgente"}); continue
        try:
            g, f = classify_smart(src, lon, lat)
            warms.append(f.get("WARM"))
            out.append({"guess": g, "features": f})
        except Exception as e:
            out.append({"guess": "errore", "err": str(e)[:100]})
    disp = None
    ws = [w for w in warms if w is not None]
    if len(ws) >= 3:
        m = sum(ws)/len(ws)
        disp = round((sum((w-m)**2 for w in ws)/len(ws)) ** 0.5, 4)
    return jsonify({"results": out, "DISP_W": disp,
                    "nota_disp": "dispersione WARM sui punti inviati: bassa = monotono (asfalto?), alta = variegato (sterrato?)"})

@app.route("/image")
def image_crop():
    # Ritaglio d'ortofoto largo con mirino sul punto: serve al test di visione.
    # ?lat=..&lon=..&half=20 (metri di semi-finestra) &px=256
    try:
        lon = float(request.args["lon"]); lat = float(request.args["lat"])
    except Exception:
        return jsonify({"error": "usa ?lat=..&lon=.."}), 400
    half = float(request.args.get("half", 20))
    px = int(request.args.get("px", 256))
    src, img = fetch_first_good(lon, lat, half_m=half, px=px)
    if img is None:
        return jsonify({"error": "nessuna immagine per questo punto"}), 404
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    cx, cy = px // 2, px // 2
    r = max(8, px // 16)
    d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(255, 40, 40), width=3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    from flask import Response
    return Response(buf.read(), mimetype="image/jpeg")

@app.route("/epoch/test")
def epoch_test():
    # Confronto tra annate: l'asfalto resta identico nel tempo, lo sterrato cambia.
    # Legge il punto su AGEA 2024 e su ICE 2010 (Piemonte) e confronta le firme.
    try:
        lon = float(request.args["lon"]); lat = float(request.args["lat"])
    except Exception:
        return jsonify({"error": "usa ?lat=..&lon=.."}), 400
    src24 = pick_source(lon, lat)
    src10 = pick_nir(lon, lat)   # ICE 2010 (falso colore NIR, ma per il confronto basta)
    if not src24 or not src10:
        return jsonify({"error": "confronto epoche disponibile solo in Piemonte"}), 404
    try:
        half = float(request.args.get("half", 0.4))
        f24 = features(fetch_image(src24, lon, lat, half_m=half))
        f10 = features(fetch_image(src10, lon, lat, half_m=half))
        # differenza tra le firme (su L e WARM, i piu' stabili)
        dL = abs(f24["L"] - f10["L"])
        dW = abs(f24["WARM"] - f10["WARM"])
        return jsonify({"a2024": f24, "b2010": f10,
                        "DIFF_L": round(dL, 3), "DIFF_W": round(dW, 3),
                        "finestra_m": round(2*half, 1),
                        "nota": "DIFF bassi = stabile nel tempo (indizia asfalto); alti = cambiato (indizia sterrato)"})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/nir/test")
def nir_test():
    try:
        lon = float(request.args["lon"]); lat = float(request.args["lat"])
    except Exception:
        return jsonify({"error": "usa ?lat=..&lon=.."}), 400
    src = pick_nir(lon, lat)
    if not src:
        return jsonify({"error": "nessuna sorgente NIR per questo punto"}), 404
    try:
        half = float(request.args.get("half", 0.4))
        img = fetch_image(src, lon, lat, half_m=half)
        w, h = img.size; px = img.load()
        x0, x1, y0, y1 = int(w*0.2), int(w*0.8), int(h*0.2), int(h*0.8)
        Rs = Gs = Bs = Ls = 0; n = 0
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                R, G, B = px[xx, yy]
                Rs += R; Gs += G; Bs += B
                Ls += (0.299*R + 0.587*G + 0.114*B)
                n += 1
        return jsonify({"source": src["name"], "finestra_m": round(2*half, 1),
                        "NIR_L": round(Ls/n/255.0, 3),
                        "R": round(Rs/n/255.0, 3), "G": round(Gs/n/255.0, 3),
                        "B": round(Bs/n/255.0, 3)})
    except Exception as e:
        return jsonify({"source": src["name"], "error": str(e)}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
