from fastapi import FastAPI, Request
from pydantic import BaseModel
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import requests
import random
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
estado_col = db["estado_conversacion"]
historial_col = db["historial"]

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"

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
        res = requests.post(url, data=payload, headers=headers, timeout=10)
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

def obtener_autos_usados(force_refresh: bool = False) -> list[dict]:
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_usados"})
        if (not force_refresh) and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

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
                autos.append({"modelo": modelo, "anio": anio})

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
# Funciones de estado / memoria
# =========================
def obtener_estado(cliente_id: str) -> dict:
    estado = estado_col.find_one({"_id": cliente_id})
    return estado if estado else {}

def actualizar_estado(cliente_id: str, nuevo_estado: dict):
    estado_col.update_one({"_id": cliente_id}, {"$set": nuevo_estado}, upsert=True)

def guardar_historial(cliente_id: str, mensaje: str, role: str):
    historial_col.insert_one({
        "cliente_id": cliente_id,
        "mensaje": mensaje,
        "role": role,
        "fecha": datetime.now()
    })

# =========================
# Funciones de conversación
# =========================
def detectar_emocion(texto: str) -> str:
    t = texto.lower()
    if any(p in t for p in ["perfecto", "genial", "ok", "claro", "sí", "si"]):
        return "positivo"
    if any(p in t for p in ["no sé", "quizás", "tal vez", "indeciso"]):
        return "indeciso"
    if any(p in t for p in ["malo", "no me gusta", "triste"]):
        return "negativo"
    return "neutral"

def generar_respuesta(cliente_id: str, texto_usuario: str) -> dict:
    estado = obtener_estado(cliente_id)
    emocion_actual = detectar_emocion(texto_usuario)

    # Actualizar emociones
    if "emociones" not in estado:
        estado["emociones"] = []
    if emocion_actual not in estado["emociones"]:
        estado["emociones"].append(emocion_actual)
        actualizar_estado(cliente_id, {"emociones": estado["emociones"]})

    # Falta nombre
    if "nombre" not in estado:
        return {"texto": f"¡Hola! Bienvenido a {AGENCIA}. Soy {BOT_NOMBRE}. ¿Cuál es tu nombre? Además, ¿buscas un vehículo nuevo o usado?", "botones": []}

    # Falta tipo de auto
    if "tipo_auto" not in estado:
        return {"texto": f"{BOT_NOMBRE}: ¿Buscas un auto nuevo o usado?", "botones": ["Autos nuevos", "Autos usados"]}

    # Falta modelo
    if "modelo" not in estado:
        tipo = estado["tipo_auto"].lower()
        if tipo == "nuevo":
            modelos = obtener_autos_nuevos()
        else:
            autos_usados = obtener_autos_usados()
            modelos = [f"{a['modelo']} ({a['anio']})" for a in autos_usados]
        if not modelos:
            modelos = ["Jetta", "Tiguan", "Taos"]
        return {"texto": f"{BOT_NOMBRE}: Estos son los modelos disponibles de {tipo}:\n- " + "\n- ".join(modelos), "botones": modelos[:5]}

    # Confirmación final
    return {"texto": f"{BOT_NOMBRE}: Excelente {estado['nombre']}, he registrado que buscas un {estado['tipo_auto']} modelo {estado['modelo']}.", "botones": ["Agendar cita", "Ver más modelos"]}

# =========================
# Webhook principal
# =========================
@app.post("/webhook")
async def webhook(req: Mensaje):
    cliente_id = req.cliente_id
    texto = req.texto.lower()

    guardar_historial(cliente_id, texto, "usuario")
    estado = obtener_estado(cliente_id)

    # Detectar nombre
    if "nombre" not in estado:
        palabras = texto.split()
        for palabra in palabras:
            if palabra.isalpha():
                estado["nombre"] = palabra.title()
                break

    # Detectar tipo de auto
    if "nuevo" in texto:
        estado["tipo_auto"] = "nuevo"
    elif "usado" in texto:
        estado["tipo_auto"] = "usado"

    # Detectar modelo si menciona alguno
    autos_nuevos = obtener_autos_nuevos()
    autos_usados = [f"{a['modelo']} ({a['anio']})" for a in obtener_autos_usados()]
    posibles_modelos = autos_nuevos + autos_usados
    for modelo in posibles_modelos:
        if modelo.lower() in texto:
            estado["modelo"] = modelo
            break

    actualizar_estado(cliente_id, estado)

    respuesta_data = generar_respuesta(cliente_id, texto)
    guardar_historial(cliente_id, respuesta_data["texto"], "bot")
    return respuesta_data

# =========================
# Scheduler refresco cache
# =========================
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)), "interval", hours=3)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("servern3-2:app", host="0.0.0.0", port=5000, reload=True)

