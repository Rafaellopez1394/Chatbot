from fastapi import FastAPI, Request
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup
import re
import random
import ollama

# =====================
# Configuración general
# =====================
app = FastAPI()
client = MongoClient("mongodb://localhost:27017/")
db = client["autos_db"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacán"

# =====================
# Funciones de scraping
# =====================
def scrape_autos(urls, selector):
    autos = []
    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            for car in soup.select(selector):
                texto = car.get_text(strip=True)
                if texto and texto not in autos:
                    autos.append(texto)
        except Exception as e:
            print(f"⚠️ Error al scrapear {url}: {e}")
    return autos

def actualizar_cache(tipo, urls, selector):
    modelos = scrape_autos(urls, selector)
    if modelos:
        db.cache.update_one(
            {"tipo": tipo},
            {"$set": {"modelos": modelos, "fecha": datetime.now()}},
            upsert=True
        )
        print(f"[{tipo}] ✅ Cache actualizado con {len(modelos)} modelos.")
    return modelos

def obtener_cache(tipo):
    cache = db.cache.find_one({"tipo": tipo})
    if cache and cache["fecha"] > datetime.now() - timedelta(hours=6):
        return cache["modelos"]
    return None

# =====================
# Funciones de sesión
# =====================
def guardar_sesion(cliente_id, estado):
    estado["cliente_id"] = cliente_id
    estado["fecha"] = datetime.now()
    db.sesiones.update_one(
        {"cliente_id": cliente_id},
        {"$set": estado},
        upsert=True
    )

def obtener_sesion(cliente_id):
    sesion = db.sesiones.find_one({"cliente_id": cliente_id})
    if sesion:
        return sesion
    return {}

# =====================
# Scheduler automático
# =====================
scheduler = BackgroundScheduler()

def refrescar_todos():
    print("♻️ Refrescando cache de autos...")
    actualizar_cache(
        "autos_nuevos",
        [
            "https://www.autocosmos.com.mx/vweurocity/autos",
            "https://www.autocosmos.com.mx/vweurocity/autos?pidx=2"
        ],
        ".anuncio h3"
    )
    actualizar_cache(
        "autos_usados",
        [
            "https://vw-eurocity.com.mx/seminuevos"
        ],
        ".anuncio h3"
    )

scheduler.add_job(refrescar_todos, "interval", hours=6)
scheduler.start()

# =====================
# Parser de nombre y tipo auto
# =====================
NOMBRE_REGEXES = [r"(?:^|\b)(?:mi nombre es|me llamo|soy)\s+([a-záéíóúñ]+)"]

def parsear_nombre_tipo(texto: str):
    texto = texto.lower().strip()
    nombre = None
    tipo_auto = None

    # Extraer nombre si el usuario lo indica
    for patron in NOMBRE_REGEXES:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            candidato = m.group(1).strip()
            if candidato and candidato not in ("soy", "me", "llamo"):
                nombre = candidato.title()
                break

    # Detectar tipo de auto
    if "nuevo" in texto:
        tipo_auto = "nuevo"
    elif "usado" in texto:
        tipo_auto = "usado"

    return nombre, tipo_auto

# =====================
# Generación de respuesta dinámica con LLM
# =====================
def generar_respuesta_llm(nombre, tipo_auto, modelos, modelo_elegido, texto_usuario):
    prompt = f"""
Eres {BOT_NOMBRE}, un asistente humano y amistoso de {AGENCIA}. 
Responde de forma natural, humana y cercana al usuario. 
Incluye emojis si es apropiado. 
No repitas frases exactas, varía tu estilo. 
Usa el nombre del usuario si lo sabes: {nombre if nombre else 'amigo'}.

Información disponible:
- Tipo de auto que busca el usuario: {tipo_auto if tipo_auto else 'no especificado'}
- Modelos disponibles: {', '.join(modelos[:10]) if modelos else 'no hay información'}
- Modelo elegido: {modelo_elegido if modelo_elegido else 'no ha elegido'}

Usuario escribió: "{texto_usuario}"

Genera un mensaje amable, natural y humano para el usuario.
"""
    response = ollama.generate(model="llama3", prompt=prompt)
    return response["response"].strip()

# =====================
# Webhook principal
# =====================
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    cliente_id = data.get("cliente_id")
    texto_usuario = data.get("texto", "").strip()
    sesion = obtener_sesion(cliente_id)

    # Manejo de "Cambiar modelo"
    if texto_usuario.lower() in ["cambiar modelo", "cambiar modelos"]:
        tipo = sesion.get("tipo_auto")
        modelos = sesion.get("modelos")
        if not modelos:
            if tipo == "nuevo":
                modelos = obtener_cache("autos_nuevos") or actualizar_cache(
                    "autos_nuevos",
                    [
                        "https://www.autocosmos.com.mx/vweurocity/autos",
                        "https://www.autocosmos.com.mx/vweurocity/autos?pidx=2"
                    ],
                    ".anuncio h3"
                )
            else:
                modelos = obtener_cache("autos_usados") or actualizar_cache(
                    "autos_usados",
                    [
                        "https://vw-eurocity.com.mx/seminuevos"
                    ],
                    ".anuncio h3"
                )
            sesion["modelos"] = modelos
        sesion.pop("modelo_elegido", None)
        guardar_sesion(cliente_id, sesion)
        return {
            "texto": f"🚘 {sesion.get('nombre','amigo')}, elige nuevamente tu modelo de {tipo}:",
            "botones": modelos[:5]
        }

    # Parsear nombre y tipo de auto
    nombre_parseado, tipo_parseado = parsear_nombre_tipo(texto_usuario)
    if nombre_parseado:
        sesion["nombre"] = nombre_parseado
    if tipo_parseado:
        sesion["tipo_auto"] = tipo_parseado

    # Preguntar tipo de auto si falta
    if "tipo_auto" not in sesion:
        guardar_sesion(cliente_id, sesion)
        return {
            "texto": f"👋 ¡Hola {sesion.get('nombre','amigo')}! ¿Buscas un auto *nuevo* o *usado*?",
            "botones": ["Autos nuevos", "Autos usados"]
        }

    # Obtener modelos disponibles
    tipo = sesion["tipo_auto"]
    if tipo == "nuevo":
        modelos = obtener_cache("autos_nuevos") or actualizar_cache(
            "autos_nuevos",
            [
                "https://www.autocosmos.com.mx/vweurocity/autos",
                "https://www.autocosmos.com.mx/vweurocity/autos?pidx=2"
            ],
            ".anuncio h3"
        )
    else:
        modelos = obtener_cache("autos_usados") or actualizar_cache(
            "autos_usados",
            [
                "https://vw-eurocity.com.mx/seminuevos"
            ],
            ".anuncio h3"
        )
    sesion["modelos"] = modelos

    # Detectar modelo elegido
    modelo_elegido = None
    for m in modelos:
        if m.lower() in texto_usuario.lower():
            modelo_elegido = m
            sesion["modelo_elegido"] = modelo_elegido
            break

    guardar_sesion(cliente_id, sesion)

    # Generar respuesta humana dinámica
    texto_respuesta = generar_respuesta_llm(
        sesion.get("nombre"),
        sesion.get("tipo_auto"),
        modelos,
        sesion.get("modelo_elegido"),
        texto_usuario
    )

    return {
        "texto": texto_respuesta,
        "botones": modelos[:5] if not modelo_elegido else ["Cambiar modelo", "Explorar más modelos"]
    }

# =====================
# Main
# =====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
