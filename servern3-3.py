from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timedelta
import random
import requests
import logging
import ollama

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbot_db"]
cache_col = db["cache"]
sesiones_col = db["sesiones"]
bitacora_col = db["bitacora"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"
EJECUTIVOS = ["ejecutivo1", "ejecutivo2", "ejecutivo3"]

class Mensaje(BaseModel):
    cliente_id: str
    texto: str

# ------------------------------
# Funciones para autos
# ------------------------------
def obtener_autos_nuevos(force_refresh=False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_nuevos"})
        if cache and (not force_refresh) and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])
        url = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload = {"r": "cargaAutosTodos", "x": str(random.random())}
        res = requests.post(url, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        modelos = list({auto.get("modelo") for auto in data if auto.get("modelo")})
        cache_col.update_one({"_id": "autos_nuevos"}, {"$set": {"data": modelos, "ts": ahora}}, upsert=True)
        return modelos
    except Exception as e:
        logger.error(f"Error autos nuevos: {e}")
        return []

def obtener_autos_usados(force_refresh=False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_usados"})
        if cache and (not force_refresh) and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])
        url = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers = {"User-Agent": "Mozilla/5.0"}
        payload = {"r": "CheckDist"}
        res = requests.post(url, headers=headers, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        modelos = list({f"{auto.get('Modelo')} ({auto.get('Anio')})" for auto in data.get("LiAutos", []) if auto.get("Modelo")})
        cache_col.update_one({"_id": "autos_usados"}, {"$set": {"data": modelos, "ts": ahora}}, upsert=True)
        return modelos
    except Exception as e:
        logger.error(f"Error autos usados: {e}")
        return []

# ------------------------------
# Sesiones y bitácora
# ------------------------------
def obtener_sesion(cliente_id):
    return sesiones_col.find_one({"cliente_id": cliente_id}) or {}

def guardar_sesion(cliente_id, sesion):
    sesion["cliente_id"] = cliente_id
    sesion["ts"] = datetime.utcnow()
    sesiones_col.update_one({"cliente_id": cliente_id}, {"$set": sesion}, upsert=True)

def guardar_bitacora(registro):
    registro["fecha"] = datetime.utcnow()
    bitacora_col.insert_one(registro)

# ------------------------------
# Asignación de ejecutivo
# ------------------------------
def asignar_ejecutivo(cliente_id, info_cliente):
    for ejecutivo in EJECUTIVOS:
        guardar_bitacora({
            "cliente_id": cliente_id,
            "ejecutivo": ejecutivo,
            "info_cliente": info_cliente,
            "estatus": "asignado"
        })
        return ejecutivo
    return None

# ------------------------------
# Generación de respuesta con Ollama
# ------------------------------
def generar_respuesta_ollama(prompt):
    try:
        resp = ollama.generate(model="llama3", prompt=prompt)
        return resp["response"].strip()
    except Exception as e:
        logger.error(f"Error Ollama: {e}")
        return "Disculpa, tuve un problema procesando tu mensaje."

# ------------------------------
# Webhook
# ------------------------------
@app.post("/webhook")
async def webhook(req: Mensaje):
    cliente_id = req.cliente_id
    texto = req.texto.lower()
    sesion = obtener_sesion(cliente_id)

    # 1️⃣ Nombre
    if "nombre" not in sesion:
        palabras = [p for p in texto.split() if p.isalpha()]
        if palabras:
            sesion["nombre"] = palabras[0].title()
        else:
            guardar_sesion(cliente_id, sesion)
            prompt = "Inicia conversación amigable para obtener el nombre del cliente."
            return {"texto": generar_respuesta_ollama(prompt), "botones": []}

    # 2️⃣ Tipo de auto
    if "tipo_auto" not in sesion:
        if "nuevo" in texto:
            sesion["tipo_auto"] = "nuevo"
        elif "usado" in texto:
            sesion["tipo_auto"] = "usado"
        else:
            guardar_sesion(cliente_id, sesion)
            prompt = f"{sesion['nombre']}, pregunta de manera natural si busca auto nuevo o usado."
            return {"texto": generar_respuesta_ollama(prompt), "botones": ["Nuevo", "Usado"]}

    # 3️⃣ Modelos según tipo
    tipo = sesion["tipo_auto"]
    modelos = obtener_autos_nuevos() if tipo=="nuevo" else obtener_autos_usados()
    sesion["modelos"] = modelos

    # 4️⃣ Detectar modelo mencionado
    modelo_seleccionado = next((m for m in modelos if m.lower() in texto), None)

    # 5️⃣ Confirmación modelo
    if "modelo_confirmado" not in sesion:
        if modelo_seleccionado:
            sesion["modelo"] = modelo_seleccionado
            guardar_sesion(cliente_id, sesion)
            prompt = f"Confirma de forma amable con {sesion['nombre']} que quiere el modelo {modelo_seleccionado} y permite cambiarlo."
            return {"texto": generar_respuesta_ollama(prompt), "botones": ["Sí", "Cambiar modelo"]}
        else:
            guardar_sesion(cliente_id, sesion)
            prompt = f"{sesion['nombre']}, muestra primeros 5 modelos disponibles: {', '.join(modelos[:5])}."
            return {"texto": generar_respuesta_ollama(prompt), "botones": modelos[:5]}

    # 6️⃣ Confirmación final y asignación ejecutivo
    if texto in ["sí", "si"]:
        info_cliente = {
            "nombre": sesion["nombre"],
            "tipo_auto": tipo,
            "modelo": sesion["modelo"],
            "whatsapp": cliente_id
        }
        ejecutivo = asignar_ejecutivo(cliente_id, info_cliente)
        guardar_sesion(cliente_id, sesion)
        prompt = f"{sesion['nombre']}, un ejecutivo ({ejecutivo}) te contactará pronto para ayudarte con tu {sesion['modelo']}."
        return {"texto": generar_respuesta_ollama(prompt), "botones": []}
    elif texto in ["cambiar modelo"]:
        sesion.pop("modelo", None)
        guardar_sesion(cliente_id, sesion)
        prompt = f"{sesion['nombre']}, muestra los primeros 5 modelos: {', '.join(modelos[:5])}."
        return {"texto": generar_respuesta_ollama(prompt), "botones": modelos[:5]}

    # 7️⃣ Continuar conversación
    guardar_sesion(cliente_id, sesion)
    prompt = f"{sesion['nombre']} continúa la conversación. Guía a elegir modelo, tono cercano y persuasivo."
    return {"texto": generar_respuesta_ollama(prompt), "botones": modelos[:5]}

# =========================
# Scheduler refresco cache
# =========================
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)), "interval", hours=3)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
