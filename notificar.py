#!/usr/bin/env python3
"""
Notificaciones del panel FWI / Basugix (euskera y castellano).

  * Correo: informe del día y previsión a 3 días, una vez al día, al cerrarse el dato
    (hora del dato + 60 min: 15:00 en verano, 14:00 en invierno).
  * Telegram: avisos de riesgo alto (Basugix >= 30 o FWI >= 21,3), subidas bruscas,
    viento sur con riesgo alto, y avisos técnicos (estaciones sin datos, fallo del proceso).

Lee docs/data/fwi.json (lo escribe actualizar_fwi.py) y guarda en datos/notificaciones.json lo ya
enviado, para no repetir: el correo, una vez al día; los avisos, solo cuando empieza o termina la
situación (no cada día que se mantiene).

Configuración (secretos de GitHub, como variables de entorno); si faltan, ese canal se omite:
  SMTP_USER, SMTP_PASS (contraseña de aplicación), EMAIL_TO (varios, separados por comas);
  opcionales: SMTP_HOST (por defecto smtp.gmail.com), SMTP_PORT (465), EMAIL_FROM
  TELEGRAM_TOKEN, TELEGRAM_CHAT (grupo o canal de avisos), TELEGRAM_CHAT_TEC (avisos técnicos; si
  falta, se usa TELEGRAM_CHAT)

Uso:
  python notificar.py                  # lo normal, tras actualizar_fwi.py
  python notificar.py --prueba         # no envía nada: escribe prueba_correo.html y muestra los mensajes
  python notificar.py --fallo URL      # aviso técnico de que el proceso ha fallado
"""
import argparse, html, json, math, os, smtplib, ssl, sys, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("Europe/Madrid")
DATOS = Path("docs/data/fwi.json")
ESTADO = Path("datos/notificaciones.json")
WEB = "https://irratiamendiak.github.io/panel-fwi/"
UMBRAL_BGX = 30.0      # Basugix "Handia"
UMBRAL_FWI = 21.3      # FWI "Handia" (EFFIS)
SALTO_NIVELES = 2      # subida brusca: 2 niveles o más de un día para otro
MARGEN_CIERRE = 60     # minutos tras la hora del dato

CLASES = ["Muy bajo", "Bajo", "Moderado", "Alto", "Muy alto", "Extremo"]
NOM = {"eu": ["Oso baxua", "Baxua", "Ertaina", "Handia", "Oso handia", "Muturrekoa"],
       "es": ["Muy bajo", "Bajo", "Moderado", "Alto", "Muy alto", "Extremo"]}
COL = ["#2f8f5b", "#8fbf3f", "#f0c53b", "#ee8a2e", "#d9382b", "#7a1f4d"]
FG = ["#fff", "#14231c", "#14231c", "#14231c", "#fff", "#fff"]
EGUN = ["astelehena", "asteartea", "asteazkena", "osteguna", "ostirala", "larunbata", "igandea"]
HIL = ["urtarrilaren", "otsailaren", "martxoaren", "apirilaren", "maiatzaren", "ekainaren", "uztailaren",
       "abuztuaren", "irailaren", "urriaren", "azaroaren", "abenduaren"]
DIA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
       "noviembre", "diciembre"]


# ---------------------------------------------------------------- utilidades
IFG = (-7.335, 1.122, 1.156, 0.479, 497 / 63837)   # mismos coeficientes que actualizar_fwi.py


def ifg_de(u):
    """Multiplicador IFG del día; si no viene en los datos, se calcula a partir del FWI, la época y el viento sur."""
    if u.get("ifg") is not None:
        return u["ifg"]
    if u.get("fwi") is None or not u.get("fecha"):
        return None
    b0, b1, be, bs, media = IFG
    x = b0 + b1 * math.log1p(max(u["fwi"], 0)) + (be if int(u["fecha"][5:7]) in (12, 1, 2, 3, 4) else 0) + (bs if hay_sur(u) else 0)
    return math.exp(x) / media


def bgx(ifg):
    return None if ifg is None else max(0.0, 20 + 10 * math.log2(max(ifg, 1e-6)))


def idx_bgx(v):
    v = round(v, 1)
    return 0 if v < 10 else 1 if v < 20 else 2 if v < 30 else 3 if v < 40 else 4 if v < 50 else 5


def idx_fwi(v):
    return 0 if v <= 5.2 else 1 if v <= 11.2 else 2 if v <= 21.3 else 3 if v <= 38 else 4 if v <= 50 else 5


def idx_fwi_de(u):
    """Nivel del FWI tal como lo calcula el proceso (con el valor sin redondear); si no viene, por umbrales."""
    return CLASES.index(u["clase"]) if u.get("clase") in CLASES else idx_fwi(u["fwi"])


def fnum(v, dec=1):
    return "—" if v is None else f"{v:.{dec}f}".replace(".", ",")


def ffecha(iso, l, larga=True):
    d = date.fromisoformat(iso)
    if l == "eu":
        return f"{EGUN[d.weekday()]}, {d.year}ko {HIL[d.month-1]} {d.day}a" if larga else f"{HIL[d.month-1].replace('aren','a').replace('ren','a')} {d.day}"
    return f"{DIA[d.weekday()]}, {d.day} de {MES[d.month-1]} de {d.year}" if larga else f"{DIA[d.weekday()][:3]}. {d.day}"


def sector(g):
    if g is None:
        return ""
    return ["N", "NE", "E", "SE", "S", "SO", "O", "NO"][int(((g % 360) + 22.5) // 45) % 8]


def hay_sur(d):
    return d.get("dir") is not None and -math.cos(math.radians(d["dir"])) >= 0.5 and (d.get("W") or 0) >= 3


def dias_sin_lluvia(dias):
    """Días seguidos (hasta el último, incluido) con lluvia de 24 h < 1 mm. None si no se sabe."""
    n = 0
    ant = None
    for d in reversed(dias):
        if ant and (date.fromisoformat(ant) - date.fromisoformat(d["fecha"])).days != 1:
            return None
        if d.get("R") is None:
            return None
        if d["R"] >= 1:
            return n
        n += 1
        ant = d["fecha"]
    return None


def cargar_estado():
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_estado(e):
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- envío
def enviar_telegram(texto, chat=None, prueba=False):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), chat or os.environ.get("TELEGRAM_CHAT")
    if prueba or not tok or not chat:
        print(("[prueba] " if prueba else "[Telegram sin configurar] ") + "mensaje:\n" + texto + "\n")
        return
    datos = urllib.parse.urlencode({"chat_id": chat, "text": texto, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", datos, timeout=30) as r:
        r.read()


def enviar_correo(asunto, cuerpo_html, prueba=False):
    # Gmail por defecto: basta con SMTP_USER, SMTP_PASS (contraseña de aplicación) y EMAIL_TO
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    usu, pw, dest = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    pw = (pw or "").replace(" ", "")
    if prueba or not (host and usu and pw and dest):
        Path("prueba_correo.html").write_text(cuerpo_html, encoding="utf-8")
        print(("[prueba] " if prueba else "[correo sin configurar] ") + f"asunto: {asunto} -> prueba_correo.html")
        return
    m = MIMEMultipart("alternative")
    m["Subject"], m["From"] = asunto, os.environ.get("EMAIL_FROM") or f"Basugix <{usu}>"
    lista = [x.strip() for x in dest.split(",") if x.strip()]
    m["To"] = m["From"]            # destinatarios en copia oculta, para no mostrar las direcciones
    m.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT") or 465), context=ssl.create_default_context()) as s:
        s.login(usu, pw)
        s.sendmail(usu, lista, m.as_string())


# ---------------------------------------------------------------- correo: informe del día y previsión
T = {
    "tit": {"eu": "Baso sute arriskua Gipuzkoan", "es": "Riesgo de incendio forestal en Gipuzkoa"},
    "sub": {"eu": "Eguzki-eguerdiko datuak", "es": "Datos del mediodía solar"},
    "est": {"eu": "Estazioa", "es": "Estación"}, "niv": {"eu": "Maila", "es": "Nivel"},
    "sinll": {"eu": "Euririk gabe", "es": "Sin lluvia"}, "dias": {"eu": "egun", "es": "días"},
    "viento": {"eu": "Haizea", "es": "Viento"}, "lluvia": {"eu": "Euria 24 h", "es": "Lluvia 24 h"},
    "prev": {"eu": "Hurrengo egunetako iragarpena", "es": "Previsión de los próximos días"},
    "sur": {"eu": "💨 hego haizea", "es": "💨 viento sur"}, "calc": {"eu": "kalkulatua", "es": "calculado"},
    "leyenda": {"eu": "FWI: nazioarteko indizea (EFFIS mailak). Basugix: Gipuzkoarako egokitua (20 = egun ertaina; 10 puntu gehiago = sute-probabilitatea bikoitza).",
                "es": "FWI: índice internacional (niveles del EFFIS). Basugix: adaptado a Gipuzkoa (20 = día medio; 10 puntos más = el doble de probabilidad de incendio)."},
    "web": {"eu": "Ikusi webgunean", "es": "Ver en la web"},
    "pie": {"eu": "Tresna orientagarria: ez ditu ordezkatzen abisu ofizialak. Datuak: Euskalmet; iragarpena: Open-Meteo.",
            "es": "Herramienta orientativa: no sustituye a los avisos oficiales. Datos: Euskalmet; previsión: Open-Meteo."},
}


def celda(v, i, extra=""):
    return (f'<td style="padding:4px 6px;text-align:center"><span style="display:inline-block;min-width:44px;padding:2px 8px;'
            f'border-radius:999px;background:{COL[i]};color:{FG[i]};font-weight:700">{fnum(v)}</span>{extra}</td>')


def bloque_correo(d, l, hoy):
    ests = d["estaciones"]
    th = 'style="padding:4px 6px;background:#eef1f4;font-size:12px;text-align:left"'
    h = [f'<h2 style="margin:0 0 2px;font:700 20px system-ui">{T["tit"][l]}</h2>',
         f'<p style="margin:0 0 10px;color:#444">{ffecha(hoy, l)} · {T["sub"][l]} ({d["hora_dato"]}:00)</p>',
         f'<table style="border-collapse:collapse;font:13px system-ui;width:100%"><tr><th {th}>{T["est"][l]}</th>'
         f'<th {th}>FWI</th><th {th}>Basugix</th><th {th}>T · HR</th><th {th}>{T["viento"][l]}</th><th {th}>{T["lluvia"][l]}</th><th {th}>{T["sinll"][l]}</th></tr>']
    for e in ests:
        u = e["dias"][-1] if e.get("dias") else None
        if not u or u["fecha"] != hoy:
            u = next((p for p in e.get("prevision", []) if p["fecha"] == hoy), None)   # aún sin medir: calculado
            calc = u is not None
        else:
            calc = False
        nom = "<b>GIPUZKOA</b>" if e.get("resumen") else html.escape(e["nombre"])
        if not u:
            h.append(f'<tr><td style="padding:4px 6px">{nom}</td><td colspan="6" style="color:#777">—</td></tr>')
            continue
        b = bgx(ifg_de(u))
        meteo = "" if e.get("resumen") else f'{fnum(u.get("T"))} °C · {fnum(u.get("H"),0)} %'
        vto = "" if e.get("resumen") or u.get("W") is None else f'{fnum(u["W"])} km/h {sector(u.get("dir"))}' + (f' {T["sur"][l]}' if hay_sur(u) else "")
        ll = "" if e.get("resumen") else f'{fnum(u.get("R"))} mm'
        sl = "" if e.get("resumen") else dias_sin_lluvia(e["dias"])
        sl = "" if sl in ("", None) else ("🌧️" if sl == 0 else f"{sl} {T['dias'][l]}")
        h.append(f'<tr style="border-bottom:1px solid #ddd"><td style="padding:4px 6px">{nom}'
                 f'{" <i style=color:#777>(" + T["calc"][l] + ")</i>" if calc else ""}</td>'
                 + celda(u["fwi"], idx_fwi_de(u)) + (celda(b, idx_bgx(b)) if b is not None else "<td>—</td>")
                 + f'<td style="padding:4px 6px">{meteo}</td><td style="padding:4px 6px">{vto}</td>'
                 f'<td style="padding:4px 6px">{ll}</td><td style="padding:4px 6px">{sl}</td></tr>')
    h.append("</table>")
    fechas = sorted({p["fecha"] for e in ests for p in e.get("prevision", []) if p["fecha"] > hoy})[:3]
    if fechas:
        h.append(f'<h3 style="margin:16px 0 4px;font:700 16px system-ui">{T["prev"][l]}</h3>'
                 f'<table style="border-collapse:collapse;font:13px system-ui;width:100%"><tr><th {th}>{T["est"][l]}</th>'
                 + "".join(f'<th {th} colspan="2">{ffecha(f, l, False)}<br><span style="font-weight:400">FWI · Basugix</span></th>' for f in fechas) + "</tr>")
        for e in ests:
            pv = {p["fecha"]: p for p in e.get("prevision", [])}
            nom = "<b>GIPUZKOA</b>" if e.get("resumen") else html.escape(e["nombre"])
            fila = f'<tr style="border-bottom:1px solid #ddd"><td style="padding:4px 6px">{nom}</td>'
            for f in fechas:
                p = pv.get(f)
                if not p:
                    fila += '<td colspan="2" style="text-align:center;color:#777">—</td>'
                    continue
                b = bgx(ifg_de(p))
                fila += celda(p["fwi"], idx_fwi_de(p)) + (celda(b, idx_bgx(b), " 💨" if hay_sur(p) else "") if b is not None else "<td>—</td>")
            h.append(fila + "</tr>")
        h.append("</table>")
    h.append(f'<p style="font:12px system-ui;color:#555;margin:10px 0 2px">{T["leyenda"][l]}</p>'
             f'<p style="font:12px system-ui;color:#555;margin:2px 0"><a href="{WEB}{"?lang=es" if l == "es" else ""}">{T["web"][l]}</a> · {T["pie"][l]}</p>')
    return "\n".join(h)


def correo_diario(d, hoy, prueba):
    cuerpo = ('<div style="max-width:760px;margin:0 auto;font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#14231c">'
              + bloque_correo(d, "eu", hoy) + '<hr style="margin:22px 0;border:0;border-top:2px solid #1f5f46">'
              + bloque_correo(d, "es", hoy) + "</div>")
    g = next((e for e in d["estaciones"] if e.get("resumen")), None)
    u = g["dias"][-1] if g and g.get("dias") else None
    extra = ""
    if u and u["fecha"] == hoy:
        b = bgx(ifg_de(u))
        extra = f" · GIPUZKOA FWI {fnum(u['fwi'])}" + (f" / Basugix {fnum(b)}" if b is not None else "")
    enviar_correo(f"Sute arriskua / Riesgo de incendio · {hoy}{extra}", cuerpo, prueba)


# ---------------------------------------------------------------- Telegram: avisos
def valor_dia(e, f):
    u = next((x for x in e.get("dias", []) if x["fecha"] == f), None)
    if u:
        return u, False
    p = next((x for x in e.get("prevision", []) if x["fecha"] == f), None)
    return p, True


def alto(u):
    b = bgx(ifg_de(u))
    return (b is not None and round(b, 1) >= UMBRAL_BGX) or idx_fwi_de(u) >= 3   # FWI "Handia" o más


def txt_valor(u, l):
    b = bgx(ifg_de(u))
    s = f"FWI {fnum(u['fwi'])} ({NOM[l][idx_fwi_de(u)]})"
    if b is not None:
        s += f" · Basugix {fnum(b)} ({NOM[l][idx_bgx(b)]})"
    if hay_sur(u):
        s += " · 💨"
    return s


def avisos(d, hoy, est, prueba):
    activos = est.setdefault("activos", {})       # estación -> fecha en que empezó el riesgo alto
    prev_av = est.setdefault("preavisos", {})     # "estación|fecha" ya preavisados
    empieza, termina, subidas, preav = [], [], [], []
    ayer = (date.fromisoformat(hoy) - timedelta(days=1)).isoformat()
    for e in d["estaciones"]:
        n = "GIPUZKOA" if e.get("resumen") else e["nombre"]
        u, calc = valor_dia(e, hoy)
        if not u:
            continue
        if alto(u) and n not in activos:
            activos[n] = hoy
            empieza.append((n, u))
        elif not alto(u) and n in activos:
            del activos[n]
            termina.append((n, u))
        y, _ = valor_dia(e, ayer)
        if y and ifg_de(y) is not None and ifg_de(u) is not None:
            s = idx_bgx(bgx(ifg_de(u))) - idx_bgx(bgx(ifg_de(y)))
            if s >= SALTO_NIVELES:
                subidas.append((n, y, u))
        if not alto(u):                               # preaviso: riesgo alto previsto en los próximos días
            for p in e.get("prevision", []):
                if p["fecha"] > hoy and alto(p) and f"{n}|{p['fecha']}" not in prev_av:
                    prev_av[f"{n}|{p['fecha']}"] = hoy
                    preav.append((n, p))
    # limpiar preavisos viejos
    for k in [k for k in prev_av if k.split("|")[1] < hoy]:
        del prev_av[k]
    if not (empieza or termina or subidas or preav):
        return
    partes = []
    for l, cab in (("eu", "🔥 <b>Baso sute arriskua — Gipuzkoa</b>"), ("es", "🔥 <b>Riesgo de incendio forestal — Gipuzkoa</b>")):
        t = [cab, ffecha(hoy, l)]
        if empieza:
            t.append(("\n⚠️ <b>Arrisku handia hasi da:</b>" if l == "eu" else "\n⚠️ <b>Comienza riesgo alto:</b>"))
            t += [f"• {n}: {txt_valor(u, l)}" for n, u in empieza]
        if subidas:
            t.append(("\n📈 <b>Igoera bizkorra (atzotik):</b>" if l == "eu" else "\n📈 <b>Subida brusca (desde ayer):</b>"))
            t += [f"• {n}: Basugix {fnum(bgx(ifg_de(y)))} → {fnum(bgx(ifg_de(u)))}" for n, y, u in subidas]
        if preav:
            t.append(("\n🔮 <b>Aurreabisua (iragarpena):</b>" if l == "eu" else "\n🔮 <b>Preaviso (previsión):</b>"))
            por_est = {}
            for n, p in preav:
                por_est.setdefault(n, []).append(p)
            for n, ps in por_est.items():
                peor = max(ps, key=lambda p: bgx(ifg_de(p)) or 0)
                dias = ", ".join(ffecha(p["fecha"], l, False) for p in ps)
                t.append(f"• {n} ({dias}) — {'gehienez' if l == 'eu' else 'máximo'}: {txt_valor(peor, l)}")
        if termina:
            t.append(("\n✅ <b>Arrisku handia amaitu da:</b>" if l == "eu" else "\n✅ <b>Termina el riesgo alto:</b>"))
            t += [f"• {n}: {txt_valor(u, l)}" for n, u in termina]
        t.append(f'\n<a href="{WEB}{"?lang=es" if l == "es" else ""}">{"Webgunea" if l == "eu" else "Web"}</a>')
        partes.append("\n".join(t))
    enviar_telegram("\n\n— — —\n\n".join(partes), prueba=prueba)


def aviso_tecnico(texto_eu, texto_es, prueba):
    enviar_telegram(f"🛠️ {texto_eu}\n\n🛠️ {texto_es}", os.environ.get("TELEGRAM_CHAT_TEC"), prueba)


def estaciones_sin_datos(d, hoy, est, prueba):
    lim = (date.fromisoformat(hoy) - timedelta(days=1)).isoformat()
    sin = [e["nombre"] for e in d["estaciones"] if not e.get("resumen") and not e.get("modelo")
           and (not e.get("dias") or e["dias"][-1]["fecha"] < lim)]
    if sin and est.get("tec_sin_datos") != hoy:
        est["tec_sin_datos"] = hoy
        aviso_tecnico("Egun bat baino gehiago daturik gabe: " + ", ".join(sin),
                      "Más de un día sin datos: " + ", ".join(sin), prueba)


# ---------------------------------------------------------------- principal
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prueba", action="store_true", help="No envía nada; muestra y guarda lo que enviaría")
    p.add_argument("--forzar", action="store_true", help="Envía aunque ya se haya enviado hoy o no sea la hora")
    p.add_argument("--fallo", metavar="URL", help="Aviso técnico: el proceso ha fallado")
    a = p.parse_args()
    if a.fallo:
        aviso_tecnico(f"Eguneratze-prozesuak huts egin du. Ikusi: {a.fallo}",
                      f"El proceso de actualización ha fallado. Ver: {a.fallo}", a.prueba)
        return 0
    d = json.loads(DATOS.read_text(encoding="utf-8"))
    ahora = datetime.now(ZONA)
    hoy = ahora.date().isoformat()
    cierre = ahora.replace(hour=int(d.get("hora_dato", 14)), minute=0, second=0, microsecond=0) + timedelta(minutes=MARGEN_CIERRE)
    est = cargar_estado()
    if ahora < cierre and not a.forzar:
        print("Aún no se ha cerrado el dato del día: no se notifica nada.")
        return 0
    if est.get("correo") != hoy or a.forzar:
        try:
            correo_diario(d, hoy, a.prueba)
            if not a.prueba:
                est["correo"] = hoy
        except Exception as e:
            print("No se ha podido enviar el correo:", e)
    if est.get("avisos") != hoy or a.forzar:
        try:
            avisos(d, hoy, est, a.prueba)
            estaciones_sin_datos(d, hoy, est, a.prueba)
            if not a.prueba:
                est["avisos"] = hoy
        except Exception as e:
            print("No se han podido enviar los avisos:", e)
    if not a.prueba:
        guardar_estado(est)
    return 0


if __name__ == "__main__":
    sys.exit(main())
