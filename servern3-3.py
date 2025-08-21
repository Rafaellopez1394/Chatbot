from fastapi import FastAPI, Request
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import random
import logging
import ollama
import re

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
db = client["autos_db"]
cache_col = db["cache"]
sesiones_col = db["sesiones"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# =========================
# Funciones de autos
# =========================
def obtener_autos_nuevos(force_refresh: bool = False):
    ahora = datetime.utcnow()
    cache = cache_col.find_one({"_id": "autos_nuevos"})
    if not force_refresh and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
        return cache.get("data", [])

    try:
        url = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload = {"r": "cargaAutosTodos", "x": str(random.random())}
        res = requests.post(url, data=payload, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()

        autos_unicos = set()
        autos = []
        for auto in data:
            modelo = auto.get("modelo")
            if modelo and modelo not in autos_unicos:
                autos_unicos.add(modelo)
                autos.append(modelo)

        cache_col.update_one({"_id": "autos_nuevos"}, {"$set": {"data": autos, "ts": ahora}}, upsert=True)
        return autos
    except Exception as e:
        logger.error(f"Error obteniendo autos nuevos: {e}")
        return []

def obtener_autos_usados(force_refresh: bool = False):
    ahora = datetime.utcnow()
    cache = cache_col.find_one({"_id": "autos_usados"})
    if not force_refresh and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
        return cache.get("data", [])

    try:
        url = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers_usados = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": headers["User-Agent"],
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

        cache_col.update_one({"_id": "autos_usados"}, {"$set": {"data": autos, "ts": ahora}}, upsert=True)
        return autos
    except Exception as e:
        logger.error(f"Error obteniendo autos usados: {e}")
        return []

# =========================
# Funciones de sesión
# =========================
def guardar_sesion(cliente_id, estado):
    estado["cliente_id"] = cliente_id
    estado["fecha"] = datetime.now()
    sesiones_col.update_one({"cliente_id": cliente_id}, {"$set": estado}, upsert=True)

def obtener_sesion(cliente_id):
    sesion = sesiones_col.find_one({"cliente_id": cliente_id})
    return sesion if sesion else {}

# =========================
# Parser de nombre y tipo auto
# =========================
NOMBRE_REGEXES = [r"(?:^|\b)(?:mi nombre es|me llamo|soy)\s+([a-záéíóúñ]+)(?:\b|$)"]
IGNORAR_PALABRAS = {"hola","me","interesa","un","auto","no","se","no","sé","okey","hey","si","sí","esta","bien","claro","ok","tengo","alguno","en","mente","modelo","aun","y","busco","carro","vehículo","vehiculo","auto"}

def parsear_nombre_tipo(texto: str):
    texto = texto.lower().strip()
    nombre = None
    tipo_auto = None
    for patron in NOMBRE_REGEXES:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            candidato = m.group(1).strip()
            if candidato and candidato not in ("soy", "me", "llamo"):
                nombre = candidato.title()
                break
    if not nombre:
        for palabra in re.findall(r"[a-záéíóúñ]+", texto):
            if palabra not in IGNORAR_PALABRAS and palabra not in ("nuevo","usado"):
                nombre = palabra.title()
                break
    if "nuevo" in texto:
        tipo_auto = "nuevo"
    elif "usado" in texto:
        tipo_auto = "usado"
    return nombre, tipo_auto

# =========================
# Webhook humano y sin repeticiones
# =========================
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    cliente_id = data.get("cliente_id")
    texto_usuario = data.get("texto", "").strip()
    sesion = obtener_sesion(cliente_id)

    # Inicializar flags
    sesion.setdefault("saludo_dado", False)
    sesion.setdefault("modelos_mostrados", False)
    sesion.setdefault("historial_modelos", [])

    # Nombre y tipo de auto
    if "nombre" not in sesion or "tipo_auto" not in sesion:
        nombre_parseado, tipo_parseado = parsear_nombre_tipo(texto_usuario)
        if nombre_parseado:
            sesion["nombre"] = nombre_parseado
        else:
            sesion["nombre"] = "amigo"
        if tipo_parseado:
            sesion["tipo_auto"] = tipo_parseado
        guardar_sesion(cliente_id, sesion)

    # Preguntar tipo de auto si falta
    if "tipo_auto" not in sesion:
        return {"texto": f"👋 {sesion['nombre']}! ¿Buscas un auto *nuevo* o *usado*?", "botones": ["Autos nuevos","Autos usados"]}

    tipo = sesion["tipo_auto"]
    modelos = obtener_autos_nuevos() if tipo=="nuevo" else obtener_autos_usados()
    sesion["modelos"] = modelos

    # Detectar modelo mencionado
    for m in modelos:
        if m.lower() in texto_usuario.lower():
            sesion["modelo_seleccionado"] = m
            if m not in sesion["historial_modelos"]:
                sesion["historial_modelos"].append(m)
            break

    # Construir prompt según estado
    if not sesion["saludo_dado"]:
        prompt_base = f"""
Eres un asistente cordial y humano de Volkswagen Eurocity Culiacán llamado {BOT_NOMBRE}.
Saluda al usuario por su nombre {sesion['nombre']} y preséntale los modelos disponibles de manera breve y amigable.
Mensaje del usuario: "{texto_usuario}"
"""
        sesion["saludo_dado"] = True
    elif not sesion["modelos_mostrados"]:
        prompt_base = f"""
Eres un asistente cordial y humano de Volkswagen Eurocity Culiacán llamado {BOT_NOMBRE}.
Usuario: {sesion['nombre']}
Muestra algunos modelos de {tipo} de manera breve, solo los nombres y un detalle clave, sin repetir saludos.
Mensaje del usuario: "{texto_usuario}"
"""
        sesion["modelos_mostrados"] = True
    else:
        prompt_base = f"""
Eres un asistente cordial y humano de Volkswagen Eurocity Culiacán llamado {BOT_NOMBRE}.
Usuario: {sesion['nombre']}
Continúa la conversación de manera natural, cercana y amistosa, sin repetir introducciones ni saludos.
Mensaje del usuario: "{texto_usuario}"
"""

    # Generar respuesta con Ollama
    try:
        response = ollama.generate(model="llama3", prompt=prompt_base)
        texto_respuesta = response["response"].strip()
    except Exception as e:
        logger.error(f"Error en Ollama: {e}")
        texto_respuesta = f"{BOT_NOMBRE}: Lo siento, tuve un problema generando la respuesta. 😔"

    guardar_sesion(cliente_id, sesion)

    return {"texto": texto_respuesta, "botones": ["Cambiar modelo", "Explorar más modelos"]}

# =========================
# Scheduler refresco cache
# =========================
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)), "interval", hours=3)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
