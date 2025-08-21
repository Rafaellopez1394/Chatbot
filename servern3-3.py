from fastapi import FastAPI, Request
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import random
import requests
import logging
import ollama

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO)
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
EJECUTIVOS = ["ejecutivo1", "ejecutivo2", "ejecutivo3"]

# =========================
# Pydantic model
# =========================
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

# =========================
# Funciones de autos
# =========================
def obtener_autos_nuevos(force_refresh: bool = False):
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
        autos = list({auto.get("modelo") for auto in data if auto.get("modelo")})
        cache_col.update_one({"_id": "autos_nuevos"}, {"$set": {"data": autos, "ts": ahora}}, upsert=True)
        return autos
    except Exception as e:
        logger.error(f"Error autos nuevos: {e}")
        return []

def obtener_autos_usados(force_refresh: bool = False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_usados"})
        if (not force_refresh) and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

        url = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers = {"User-Agent": "Mozilla/5.0"}
        payload = {"r": "CheckDist"}
        res = requests.post(url, headers=headers, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        autos = list({f"{auto.get('Modelo')} ({auto.get('Anio')})" for auto in data.get("LiAutos", []) if auto.get("Modelo")})
        cache_col.update_one({"_id": "autos_usados"}, {"$set": {"data": autos, "ts": ahora}}, upsert=True)
        return autos
    except Exception as e:
        logger.error(f"Error autos usados: {e}")
        return []

# =========================
# Sesiones / bitácora
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
# Asignación ejecutivo
# =========================
def asignar_ejecutivo(cliente_id, info_cliente):
    for ejecutivo in EJECUTIVOS:
        disponible = True
        if disponible:
            guardar_bitacora({
                "cliente_id": cliente_id,
                "ejecutivo": ejecutivo,
                "info_cliente": info_cliente,
                "estatus": "asignado"
            })
            return ejecutivo
    return None

# =========================
# Generación de respuesta humana con Ollama
# =========================
def generar_respuesta_ollama(prompt_base: str) -> str:
    try:
        response = ollama.generate(model="llama3", prompt=prompt_base)
        return response["response"].strip()
    except Exception as e:
        logger.error(f"Error Ollama: {e}")
        return "Disculpa, tuve un problema procesando tu mensaje."

# =========================
# Webhook
# =========================
@app.post("/webhook")
async def webhook(req: Mensaje):
    cliente_id = req.cliente_id
    texto = req.texto.lower()
    sesion = obtener_sesion(cliente_id)

    # 1️⃣ Obtener nombre
    if "nombre" not in sesion:
        palabras = [p for p in texto.split() if p.isalpha()]
        if palabras:
            sesion["nombre"] = palabras[0].title()
        else:
            guardar_sesion(cliente_id, sesion)
            prompt = f"Hola, inicia una conversación amigable y natural para conseguir el nombre del cliente."
            return {"texto": generar_respuesta_ollama(prompt), "botones": []}

    # 2️⃣ Tipo de auto
    if "tipo_auto" not in sesion:
        if "nuevo" in texto:
            sesion["tipo_auto"] = "nuevo"
        elif "usado" in texto:
            sesion["tipo_auto"] = "usado"
        else:
            guardar_sesion(cliente_id, sesion)
            prompt = f"{sesion['nombre']} está interactuando. Pregunta de forma amistosa si busca un auto nuevo o usado."
            return {"texto": generar_respuesta_ollama(prompt), "botones": ["Nuevo", "Usado"]}

    # 3️⃣ Obtener modelos según tipo
    tipo = sesion["tipo_auto"]
    modelos = obtener_autos_nuevos() if tipo == "nuevo" else obtener_autos_usados()
    sesion["modelos"] = modelos

    # 4️⃣ Detectar modelo mencionado
    modelo_seleccionado = next((m for m in modelos if m.lower() in texto), None)

    # 5️⃣ Confirmar modelo con Ollama
    if "modelo_confirmado" not in sesion:
        if modelo_seleccionado:
            sesion["modelo"] = modelo_seleccionado
            guardar_sesion(cliente_id, sesion)
            prompt = f"Confirma amablemente con {sesion['nombre']} que quiere el modelo {modelo_seleccionado}, y ofrece opción de cambiarlo."
            return {"texto": generar_respuesta_ollama(prompt), "botones": ["Sí", "Cambiar modelo"]}
        else:
            guardar_sesion(cliente_id, sesion)
            prompt = f"{sesion['nombre']} necesita elegir un modelo. Muestra de forma natural los primeros 5 modelos de la lista: {', '.join(modelos[:5])}."
            return {"texto": generar_respuesta_ollama(prompt), "botones": modelos[:5]}

    # 6️⃣ Confirmación final y asignación ejecutivo
    if texto in ["sí", "si"]:
        info_cliente = {"nombre": sesion["nombre"], "tipo_auto": tipo, "modelo": sesion["modelo"], "whatsapp": cliente_id}
        ejecutivo = asignar_ejecutivo(cliente_id, info_cliente)
        guardar_sesion(cliente_id, sesion)
        prompt = f"Avisa de forma cordial a {sesion['nombre']} que un ejecutivo ({ejecutivo}) lo contactará en breve."
        return {"texto": generar_respuesta_ollama(prompt), "botones": []}
    elif texto in ["cambiar modelo"]:
        sesion.pop("modelo", None)
        guardar_sesion(cliente_id, sesion)
        prompt = f"{sesion['nombre']} quiere cambiar de modelo, muestra de forma natural los primeros 5 modelos: {', '.join(modelos[:5])}."
        return {"texto": generar_respuesta_ollama(prompt), "botones": modelos[:5]}

    guardar_sesion(cliente_id, sesion)
    prompt = f"{sesion['nombre']} continúa la conversación. Genera respuesta humana para guiarlo a seleccionar modelo."
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
