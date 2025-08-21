import requests
from bs4 import BeautifulSoup
import json
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded"
}

# -------- AUTOS NUEVOS --------
url_nuevos = "https://vw-eurocity.com.mx/info/consultas.ashx"
payload_nuevos = {"r": "cargaAutosTodos", "x": "0.4167698852081686"}

res_nuevos = requests.post(url_nuevos, data=payload_nuevos, headers=headers)
print("=== AUTOS NUEVOS ===")
if res_nuevos.status_code == 200:
    for auto in res_nuevos.json():
        print({
            "claveGen": auto.get("Clavegen") or auto.get("claveGen"),
            "marca": auto.get("Marca") or "VOLKSWAGEN",
            "anio": auto.get("Anio") or auto.get("anio"),
            "modelo": auto.get("Modelo") or auto.get("modelo"),
            "Precios": auto.get("Precios"),
            "TipCarr": auto.get("TipCarr"),
            "orden": auto.get("orden"),
            "version": auto.get("Titulo") or auto.get("version"),
            "Precio": auto.get("Precio")
        })

# -------- PROMOCIONES --------
payload_promos = {"r": "CargaPromociones", "x": "0.4226774843529951"}
res_promos = requests.post(url_nuevos, data=payload_promos, headers=headers)
print("\n=== PROMOCIONES ===")
if res_promos.status_code == 200:
    for promo in res_promos.json():
        print({
            "Nombre": promo.get("Nombre"),
            "Modelo": promo.get("Modelo"),
            "Anio": promo.get("Anio"),
            "Clavegen": promo.get("Clavegen"),
            "Titulo": promo.get("Titulo"),
            "Descripcion": promo.get("Descripcion"),
            "FechaVigencia": promo.get("FechaVigencia"),
            "Corporativa": promo.get("Corporativa")
        })

# -------- AUTOS USADOS --------
url_usados = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
headers = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://vw-eurocity.com.mx",
    "Referer": "https://vw-eurocity.com.mx/Seminuevos/",
}
payload = {"r": "CheckDist"}

resp = requests.post(url_usados, headers=headers, data=payload)

try:
    data = json.loads(resp.text)  # forzamos a interpretarlo como JSON
    print("=== AUTOS USADOS ===")
    for auto in data.get("LiAutos", []):
        print({
            "Marca": auto.get("Marca"),
            "Modelo": auto.get("Modelo"),
            "Anio": auto.get("Anio"),
            "Version": auto.get("Version"),
            "Precio": auto.get("Precio"),
            "Transmision": auto.get("Transmision"),
            "Kilometraje": auto.get("Kilometraje"),
        })
except Exception as e:
    print("Error procesando usados:", e)
    print("Contenido recibido:\n", resp.text[:500])

url_nuevos = "https://www.autocosmos.com.mx/vweurocity/autos"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

resp = requests.get(url_nuevos, headers=headers)

try:
    html = resp.text
    print(html)
except Exception as e:
    print("Error procesando:", e)
    print("Contenido recibido:\n", resp.text[:500])