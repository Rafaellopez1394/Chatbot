from fastapi import FastAPI, Request
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import random
import requests
import logging

# =========================
# Configuración / Logging
# =========================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =========================
# App / DB
# =========================
app = FastAPI()
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbot_db"]
cache_col = db["cache"]
sesiones_col = db["sesiones"]
bitacora_col = db["bitacora"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"

# Lista de ejecutivos disponibles
EJECUTIVOS = ["ejecutivo1", "ejecutivo2", "ejecutivo3"]

# =========================
# Modelos Pydantic
# =========================
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

# =========================
# Funciones de obtención de autos
# =========================
def obtener_autos_nuevos(force_refresh: bool = False) -> list[str]:
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_nuevos"})
        if (not force_refresh) and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

        url = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload = {"r": "cargaAutosTodos", "x": str(random.random())}
        res = requests.post(url, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()

        modelos_unicos = set()
        autos = []
        for auto in data:
            modelo = auto.get("modelo")
            if modelo and modelo not in modelos_unicos:
                modelos_unicos.add(modelo)
                autos.append(modelo)

        cache_col.update_one(
            {"_id": "autos_nuevos"},
            {"$set": {"data": autos, "ts": ahora}},
            upsert=True
        )
        return autos
    except Exception as e:
        logger.error(f"Error obteniendo autos nuevos: {e}")
        return []

def obtener_autos_usados(force_refresh: bool = False) -> list[str]:
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_usados"})
        if (not force_refresh) and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

        url = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers_usados = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://vw-eurocity.com.mx",
            "Referer": "https://vw-eurocity.com.mx/Seminuevos/",
        }
        payload = {"r": "CheckDist"}
        res = requests.post(url, headers=headers_usados, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()

        autos = []
        vistos = set()
        for auto in data.get("LiAutos", []):
            modelo = auto.get("Modelo")
            anio = auto.get("Anio")
            clave = f"{modelo}-{anio}"
            if modelo and anio and clave not in vistos:
                vistos.add(clave)
                autos.append(f"{modelo} ({anio})")

        cache_col.update_one(
            {"_id": "autos_usados"},
            {"$set": {"data": autos, "ts": ahora}},
            upsert=True
        )
        return autos
    except Exception as e:
        logger.error(f"Error obteniendo autos usados: {e}")
        return []

# =========================
# Funciones de sesión y bitácora
# =========================
def obtener_sesion(cliente_id):
    sesion = sesiones_col.find_one({"cliente_id": cliente_id})
    return sesion if sesion else {}

def guardar_sesion(cliente_id, sesion):
    sesion["cliente_id"] = cliente_id
    sesion["ts"] = datetime.utcnow()
    sesiones_col.update_one({"cliente_id": cliente_id}, {"$set": sesion}, upsert=True)

def guardar_bitacora(registro):
    registro["fecha"] = datetime.utcnow()
    bitacora_col.insert_one(registro)

# =========================
# Función para asignar ejecutivo
# =========================
def asignar_ejecutivo(cliente_id, info_cliente):
    for ejecutivo in EJECUTIVOS:
        # Simulación de disponibilidad: se puede reemplazar por API real
        disponible = True
        if disponible:
            # Guardamos en bitácora la asignación
            guardar_bitacora({
                "cliente_id": cliente_id,
                "ejecutivo": ejecutivo,
                "info_cliente": info_cliente,
                "estatus": "asignado"
            })
            return ejecutivo
    return None

# =========================
# Webhook principal
# =========================
@app.post("/webhook")
async def webhook(req: Mensaje):
    cliente_id = req.cliente_id
    texto = req.texto.lower()
    sesion = obtener_sesion(cliente_id)

    # 1️⃣ Obtener nombre si no está
    if "nombre" not in sesion:
        palabras = [p for p in texto.split() if p.isalpha()]
        if palabras:
            sesion["nombre"] = palabras[0].title()
        else:
            return {"texto": f"👋 ¡Hola! Bienvenido a {AGENCIA}. ¿Cómo te llamas?", "botones": []}

    # 2️⃣ Preguntar tipo de auto si falta
    if "tipo_auto" not in sesion:
        if "nuevo" in texto:
            sesion["tipo_auto"] = "nuevo"
        elif "usado" in texto:
            sesion["tipo_auto"] = "usado"
        else:
            guardar_sesion(cliente_id, sesion)
            return {"texto": f"Hola {sesion['nombre']}, ¿buscas un auto nuevo o usado?", "botones": ["Nuevo", "Usado"]}

    # 3️⃣ Obtener lista de modelos según tipo
    tipo = sesion["tipo_auto"]
    if tipo == "nuevo":
        modelos = obtener_autos_nuevos()
    else:
        modelos = obtener_autos_usados()

    sesion["modelos"] = modelos

    # 4️⃣ Detectar modelo mencionado
    modelo_seleccionado = None
    for m in modelos:
        if m.lower() in texto:
            modelo_seleccionado = m
            break

    # 5️⃣ Confirmar modelo con cliente
    if "modelo_confirmado" not in sesion:
        if modelo_seleccionado:
            sesion["modelo"] = modelo_seleccionado
            guardar_sesion(cliente_id, sesion)
            return {"texto": f"Perfecto {sesion['nombre']}, confirmas que el modelo que quieres es {modelo_seleccionado}? 🤔", "botones": ["Sí", "Cambiar modelo"]}
        else:
            guardar_sesion(cliente_id, sesion)
            return {"texto": f"{sesion['nombre']}, estos son los modelos de {tipo} disponibles:\n- " + "\n- ".join(modelos[:10]), "botones": modelos[:5]}

    # 6️⃣ Confirmación final y asignación a ejecutivo
    if texto in ["sí", "si"]:
        info_cliente = {
            "nombre": sesion["nombre"],
            "tipo_auto": tipo,
            "modelo": sesion["modelo"],
            "whatsapp": cliente_id
        }
        ejecutivo = asignar_ejecutivo(cliente_id, info_cliente)
        guardar_sesion(cliente_id, sesion)
        return {
            "texto": f"✅ Gracias {sesion['nombre']}! Un ejecutivo de ventas te contactará en breve. {ejecutivo} se hará cargo de tu solicitud.",
            "botones": []
        }
    elif texto in ["cambiar modelo"]:
        sesion.pop("modelo", None)
        guardar_sesion(cliente_id, sesion)
        return {"texto": "De acuerdo, elige otro modelo de la lista:", "botones": modelos[:5]}

    guardar_sesion(cliente_id, sesion)
    return {"texto": "Estoy aquí para ayudarte a elegir el modelo perfecto. 😉", "botones": modelos[:5]}

# =========================
# Scheduler refresco cache
# =========================
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)), "interval", hours=3)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
