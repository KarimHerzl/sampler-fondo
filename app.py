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
        "name": "Lombardia AGEA (ortofoto)",
        "bbox": [8.45, 44.65, 11.45, 46.65],
        "url":  "https://www.cartografia.servizirl.it/arcgis2/services/BaseMap/ortofoto2012UTM/ImageServer/WMSServer",
        "layer": "0",                                   # (confermare con /caps)
        "crs":  "EPSG:3857", "res_cm": 30,
        "attr": "Ortofoto AGEA - Regione Lombardia (uso: consultazione pubblica)",
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
        "url":  "https://opengis.csi.it/mp/regp_ortofoto_ice_nir_2010",
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
    for s in SOURCES:
        b = s["bbox"]
        if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            return s
    return None

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
    # Tarato su 8 punti reali (Piemonte, AGEA 30cm, finestra 80cm).
    # SCOPERTA: a distinguere e' il TONO TERROSO (WARM = R-B), non la luminosita'.
    #   sterrato/terra = caldo/colorato ; asfalto = grigio neutro.
    #   (uno sterrato scuro puo' avere L bassa come un asfalto, ma WARM alto)
    w = f.get("WARM", 0); sat = f.get("SAT", 0)
    if f["ExG"] > 0.15:            return "coperto"    # vegetazione / ombra verde
    if w >= 0.09 or sat >= 0.20:   return "sterrato"   # terroso / colorato
    if w <= 0.06:                  return "asfalto"    # grigio neutro
    return "incerto"                                   # fascia di confine 0.06-0.09

# ======================= ENDPOINT =======================
@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route("/")
def home():
    return "Sampler fondo v4 (NIR Piemonte). /sources | /caps | /surface/test?lat=45.09&lon=8.48"

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
        f = features(fetch_image(src, lon, lat, half_m=half))
        try:
            f.update(uniformity(fetch_image(src, lon, lat, half_m=1.5, px=64)))
        except Exception:
            pass
        return jsonify({"source": src["name"], "res_cm": src["res_cm"],
                        "finestra_m": round(2 * half, 1),
                        "features": f, "guess": classify(f)})
    except Exception as e:
        return jsonify({"source": src["name"], "error": str(e)}), 502

@app.route("/surface", methods=["POST", "OPTIONS"])
def surface():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True, silent=True) or {}
    pts = data.get("points", [])
    out = []
    for p in pts:
        try:
            lon, lat = float(p[0]), float(p[1])
        except Exception:
            out.append({"guess": "input-non-valido"}); continue
        src = pick_source(lon, lat)
        if not src:
            out.append({"guess": "nessuna-sorgente"}); continue
        try:
            f = features(fetch_image(src, lon, lat))
            out.append({"guess": classify(f), "features": f})
        except Exception as e:
            out.append({"guess": "errore", "err": str(e)[:100]})
    return jsonify({"results": out})

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
