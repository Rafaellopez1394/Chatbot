from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
import ollama
import re
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import uvicorn
import random
import logging
import json

# =========================
# Configuración / Logging
# =========================
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', filename='chatbot.log')
logger = logging.getLogger(__name__)
BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"

# =========================
# App / DB
# =========================
app = FastAPI()

try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logger.info("Conexión a MongoDB exitosa")
except Exception as e:
    logger.error(f"Fallo en la conexión a MongoDB: {str(e)}")
    raise

db = client["chatbotdb"]
historial_col = db["historial"]
estado_conversacion = db["estado_conversacion"]
asignaciones = db["asignaciones"]
asesores_col = db["asesores"]
sends = db["sends"]
memoria_col = db["memoria_clientes"]
cache_col = db["cache_sitio"]  # para cachear modelos

# =========================
# Datos / Inicialización
# =========================
CATALOGO_MODELOS_KNOWN = [
    "Jetta", "Jetta GLI", "Virtus", "Vento", "Polo",
    "Nivus", "T-Cross", "Taos", "Tiguan", "Teramont",
    "Saveiro", "Amarok"
]

def inicializar_asesores():
    try:
        existing = list(asesores_col.find())
        if not existing or not all("telefono" in doc and "activo" in doc for doc in existing):
            asesores_col.delete_many({})
            asesores_data = [
                {"nombre": "Ana", "area": "Ventas", "activo": True, "telefono": "526879388889"},
            ]
            asesores_col.insert_many(asesores_data)
            logger.info(f"Asesores inicializados: {asesores_data}")
        else:
            logger.info("Asesores ya poblados")
    except Exception as e:
        logger.error(f"Error al inicializar asesores: {str(e)}")

inicializar_asesores()

# =========================
# Scheduler
# =========================
scheduler = BackgroundScheduler()
def reasignar_pendientes():
    try:
        limite = datetime.now() - timedelta(minutes=5)
        pendientes = asignaciones.find({"respuesta": None, "fecha": {"$lt": limite}})
        for p in pendientes:
            asignar_asesor_humano(p["cliente_id"])
    except Exception as e:
        logger.error(f"Error al reasignar pendientes: {str(e)}")

try:
    scheduler.start()
    scheduler.add_job(reasignar_pendientes, 'interval', minutes=1)
    logger.info("Scheduler iniciado")
except Exception as e:
    logger.error(f"Error al iniciar scheduler: {str(e)}")
    raise

# =========================
# Modelos Pydantic
# =========================
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

class AdvisorResponse(BaseModel):
    cliente_id: str
    respuesta: str
    asesor_phone: str

def obtener_catalogo_modelos_cache(force_refresh: bool = False) -> dict:
    """
    Devuelve los modelos catalogados por tipo: 'nuevo' y 'usado', cargados desde cache MongoDB.
    """
    try:
        ahora = datetime.utcnow()
        catalogo_cache = cache_col.find_one({"_id": "catalogo_modelos"})

        if catalogo_cache and not force_refresh and (ahora - catalogo_cache.get("ts", ahora) < timedelta(hours=3)):
            return catalogo_cache.get("data", {"nuevo": [], "usado": []})

        # Obtener autos nuevos y usados desde cache individual
        autos_nuevos = cache_col.find_one({"_id": "autos_nuevos"})
        autos_usados = cache_col.find_one({"_id": "autos_usados"})

        modelos_nuevos = sorted(list(set(autos_nuevos.get("data", [])))) if autos_nuevos else []
        modelos_usados = sorted(list(set(auto["modelo"] for auto in autos_usados.get("data", []) if "modelo" in auto))) if autos_usados else []

        catalogo = {
            "nuevo": modelos_nuevos,
            "usado": modelos_usados
        }

        # Guardar en cache general
        cache_col.update_one(
            {"_id": "catalogo_modelos"},
            {"$set": {"data": catalogo, "ts": ahora}},
            upsert=True
        )

        return catalogo

    except Exception as e:
        logger.error(f"Error obteniendo catálogo de modelos desde cache: {e}")
        # fallback mínimo
        return {"nuevo": [], "usado": []}


def obtener_modelos_disponibles(tipo_auto: str) -> list:
    """
    Retorna la lista de modelos disponibles según el tipo de vehículo
    desde la cache en MongoDB.
    """
    catalogo = obtener_catalogo_modelos_cache()  # devuelve algo como {"nuevo": [...], "usado": [...]}
    modelos = catalogo.get(tipo_auto.lower(), [])
    return list(set(modelos))  # elimina duplicados por seguridad

def validar_modelo_usuario(tipo_auto: str, modelo_usuario: str) -> bool:
    """
    Retorna True si el modelo ingresado por el usuario está disponible
    según la cache y el tipo de auto.
    """
    modelos_disponibles = obtener_modelos_disponibles(tipo_auto)
    return modelo_usuario.strip().lower() in [m.lower() for m in modelos_disponibles]

# =========================
# Utilidades / Estado
# =========================
def actualizar_estado(cliente_id: str, nuevo_estado: dict):
    try:
        estado_conversacion.update_one({"_id": cliente_id}, {"$set": nuevo_estado}, upsert=True)
        logger.debug(f"Estado actualizado {cliente_id}: {nuevo_estado}")
    except Exception as e:
        logger.error(f"Error actualizar estado {cliente_id}: {str(e)}")

def obtener_estado(cliente_id: str) -> dict:
    try:
        estado = estado_conversacion.find_one({"_id": cliente_id})
        return estado if estado else {}
    except Exception as e:
        logger.error(f"Error obtener estado {cliente_id}: {str(e)}")
        return {}

def guardar_mensaje(cliente_id: str, mensaje: str, role: str):
    try:
        historial_col.insert_one({"cliente_id": cliente_id, "mensaje": mensaje, "role": role, "fecha": datetime.now()})
    except Exception as e:
        logger.error(f"Error guardar mensaje {cliente_id}: {str(e)}")

def es_contacto_valido(telefono: str) -> bool:
    return bool(re.match(r'^\d{10,}$', telefono))

def extraer_telefono_de_jid(jid: str) -> str:
    # Ej: "5216674680997@s.whatsapp.net" -> "5216674680997"
    if "@s.whatsapp.net" in jid:
        return jid.split("@")[0]
    return jid

# =========================
# Memoria
# =========================
def actualizar_memoria_avanzada(cliente_id: str, nuevo_dato: dict):
    try:
        memoria_col.update_one({"_id": cliente_id}, {"$set": nuevo_dato}, upsert=True)
    except Exception as e:
        logger.error(f"Error actualizar memoria {cliente_id}: {str(e)}")

def obtener_memoria_avanzada(cliente_id: str) -> dict:
    try:
        memoria = memoria_col.find_one({"_id": cliente_id})
        if not memoria:
            memoria = {"modelos_favoritos": [], "tipo_auto_preferido": None, "emociones": [], "ultima_pregunta": ""}
        return memoria
    except Exception:
        return {"modelos_favoritos": [], "tipo_auto_preferido": None, "emociones": [], "ultima_pregunta": ""}

def detectar_emocion(texto: str) -> str:
    t = texto.lower()
    if any(p in t for p in ["emocionado", "genial", "excelente", "perfecto", "okey", "claro", "si esta bien"]):
        return "positivo"
    if any(p in t for p in ["no sé", "no estoy seguro", "tal vez", "quizás", "dudoso", "no tengo alguno en mente", "no tengo un modelo aun"]):
        return "indeciso"
    if any(p in t for p in ["triste", "no me gusta", "malo", "difícil"]):
        return "negativo"
    return "neutral"

# =========================
# Parser de entrada (nombre / tipo)
# =========================
NOMBRE_REGEXES = [
    r"(?:^|\b)(?:mi nombre es|me llamo|soy)\s+([a-záéíóúñ]+)(?:\b|$)"
]

IGNORAR_PALABRAS = {
    "hola","me","interesa","un","auto","no","se","no","sé","okey","hey","si","sí",
    "si","esta","bien","claro","ok","no","tengo","alguno","en","mente","modelo","aun",
    "y","busco","carro","vehículo","vehiculo","auto"
}

def parsear_entrada(texto: str) -> tuple[str | None, str | None]:
    t = texto.lower().strip()
    nombre = None
    tipo_auto = None

    # Nombre vía regex (captura "soy luis", "me llamo raúl", etc.)
    for patron in NOMBRE_REGEXES:
        m = re.search(patron, t, re.IGNORECASE)
        if m:
            candidato = m.group(1).strip()
            if candidato and candidato not in ("soy", "me", "llamo"):
                nombre = candidato.title()
                break

    # Fallback: primera palabra útil
    if not nombre:
        for palabra in re.findall(r"[a-záéíóúñ]+", t):
            if palabra not in IGNORAR_PALABRAS and palabra not in ("nuevo","usado"):
                nombre = palabra.title()
                break

    # Tipo de auto
    if "nuevo" in t:
        tipo_auto = "nuevo"
    elif "usado" in t:
        tipo_auto = "usado"

    logger.debug(f"parsear_entrada -> nombre={nombre}, tipo_auto={tipo_auto}")
    return nombre, tipo_auto

# =========================
# Scraper de modelos (dos sitios)
# =========================
VARIANTAS = [
    ("t cross", "T-Cross"),
    ("t-cross", "T-Cross"),
    ("t-cross", "T-Cross"),
    ("jetta gli", "Jetta GLI"),
    ("gli", "Jetta GLI"),
    ("atlas", "Teramont"),  # en MX se vende como Teramont
]
BASENAMES = {m.lower(): m for m in CATALOGO_MODELOS_KNOWN}

def limpiar_texto_modelo(txt: str) -> str | None:
    t = re.sub(r"\s+", " ", txt.lower())
    # Quitar ruido común
    if any(p in t for p in ["precio", "$", "culiac", "sinaloa", "lista", "desde", "version", "versión"]):
        # aún puede tener el modelo base, seguimos
        pass

    # Mapeos de variantes
    for needle, canon in VARIANTAS:
        if needle in t:
            return canon

    # Coincidencia por base conocida
    for base in BASENAMES.keys():
        if re.search(rf"\b{re.escape(base)}\b", t):
            return BASENAMES[base]

    return None

def scrap_urls_para_modelos(urls: list[str], timeout=10) -> list[str]:
    modelos = set()
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Buscar en elementos semánticos
            candidates = []
            candidates.extend(soup.find_all(["h1","h2","h3","h4","a","li","span","div"]))

            for tag in candidates:
                text = tag.get_text(" ", strip=True)
                if not text or len(text) > 80:
                    continue
                nombre = limpiar_texto_modelo(text)
                if nombre:
                    modelos.add(nombre)

                # También analizar hrefs relevantes
                if tag.name == "a":
                    href = tag.get("href") or ""
                    # Heurística: si el href contiene el modelo
                    nombre_href = limpiar_texto_modelo(href.replace("-", " ").replace("/", " "))
                    if nombre_href:
                        modelos.add(nombre_href)

        except Exception as e:
            logger.warning(f"Scraping fallo en {url}: {e}")

    # Filtrar por catálogo conocido pero conservar descubrimientos válidos
    # (si un sitio lista "ID.4" u otro, lo dejamos pasar)
    modelos_limpios = set()
    for m in modelos:
        if m in CATALOGO_MODELOS_KNOWN:
            modelos_limpios.add(m)
        else:
            # Aceptar modelos de 2 a 20 chars alfanum (p.ej. "ID.4")
            if 2 <= len(m) <= 20:
                modelos_limpios.add(m)

    # Ordenar por catálogo conocido primero, luego alfabético
    orden = {name: i for i, name in enumerate(CATALOGO_MODELOS_KNOWN)}
    return sorted(modelos_limpios, key=lambda x: (orden.get(x, 999), x))
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded"
}
def obtener_autos_nuevos(force_refresh: bool = False) -> list[str]:
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_nuevos"})

        if (not force_refresh) and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

        url = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload = {"r": "cargaAutosTodos", "x": "0.123456789"}  # x solo es random
        res = requests.post(url, data=payload, headers=headers, timeout=10)
        res.raise_for_status()

        data = res.json()  # respuesta en JSON

        modelos_unicos = set()   # aquí evitamos duplicados
        autos = []

        for auto in data:
            modelo = auto.get("modelo")
            if modelo and modelo not in modelos_unicos:
                modelos_unicos.add(modelo)
                autos.append(modelo)

        # Guardamos en cache sin duplicados
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
        vistos = set()  # para evitar duplicados

        for auto in data.get("LiAutos", []):
            modelo = auto.get("Modelo")
            anio = auto.get("Anio")

            # clave única: modelo+año
            clave = f"{modelo}-{anio}"

            if modelo and anio and clave not in vistos:
                vistos.add(clave)
                autos.append({
                    "modelo": modelo,
                    "anio": anio
                })

        cache_col.update_one(
            {"_id": "autos_usados"},
            {"$set": {"data": autos, "ts": ahora}},
            upsert=True
        )
        return autos

    except Exception as e:
        logger.error(f"Error obteniendo autos usados: {e}")
        return []

    
def obtener_modelos_oficiales(force_refresh: bool = False) -> list[str]:
    try:
        # --- CACHE por 3 horas ---
        cache = cache_col.find_one({"_id": "modelos"})
        ahora = datetime.utcnow()
        if (not force_refresh) and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

        modelos = set()

        # -------- AUTOS NUEVOS --------
        url_nuevos = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload_nuevos = {"r": "cargaAutosTodos", "x": "0.4167698852081686"}
        res_nuevos = requests.post(url_nuevos, data=payload_nuevos, headers=headers, timeout=10)

        if res_nuevos.status_code == 200:
            for auto in res_nuevos.json():
                modelo = auto.get("Modelo") or auto.get("modelo")
                if modelo:
                    modelos.add(modelo.strip())

        # -------- AUTOS USADOS --------
        url_usados = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers_usados = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://vw-eurocity.com.mx",
            "Referer": "https://vw-eurocity.com.mx/Seminuevos/",
        }
        payload_usados = {"r": "CheckDist"}

        res_usados = requests.post(url_usados, headers=headers_usados, data=payload_usados, timeout=10)
        try:
            data_usados = res_usados.json()
            for auto in data_usados.get("LiAutos", []):
                modelo = auto.get("Modelo")
                if modelo:
                    modelos.add(modelo.strip())
        except Exception as e:
            logger.warning(f"No se pudo parsear autos usados: {e}")

        # --- Fallback mínimo si no encontró nada ---
        if not modelos:
            modelos = {"Jetta", "Tiguan", "Taos", "Teramont", "T-Cross", "Virtus", "Polo", "Vento"}

        # Guardar en cache
        encontrados = sorted(modelos)
        cache_col.update_one(
            {"_id": "modelos"},
            {"$set": {"data": encontrados, "ts": ahora}},
            upsert=True
        )

        logger.debug(f"Modelos oficiales: {encontrados}")
        return encontrados

    except Exception as e:
        logger.error(f"Error obtener modelos oficiales: {str(e)}")
        return ["Jetta", "Tiguan", "Taos"] 

def obtener_detalles_modelo(modelo: str) -> dict:
    # Mantener simple y no mostrar "no disponible"
    try:
        r = requests.get("https://vw-eurocity.com.mx/", timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(["h2","h3","p","li","div"]):
            text = tag.get_text(" ", strip=True).lower()
            if modelo.lower() in text and len(text) <= 200 and "precio" not in text:
                return {"descripcion": text.title()}
    except Exception:
        pass
    return {"descripcion": ""}

# =========================
# Generación de respuesta
# =========================
def resumir_historial_emociones(historial, estado, memoria, max_msgs=5):
    try:
        resumen = []
        for h in historial[-max_msgs:]:
            role = "Usuario" if h["role"] == "user" else "Asistente"
            mensaje = h["mensaje"]
            resumen.append(f"{role}: {mensaje}")
        estado_str = ", ".join([f"{k}: {v}" for k, v in estado.items() if k in ["nombre", "tipo_auto", "modelo", "confirmado"]])
        memoria_str = ", ".join([f"{k}: {v}" for k, v in memoria.items() if k != "emociones"])
        emociones_str = ", ".join(memoria.get("emociones", []))
        resumen.append(f"[Resumen de estado: {estado_str}]")
        resumen.append(f"[Memoria del cliente: {memoria_str}]")
        resumen.append(f"[Emociones detectadas: {emociones_str}]")
        return "\n".join(resumen)
    except Exception as e:
        logger.error(f"Error al resumir historial: {str(e)}")
        return ""

def generar_respuesta_premium(mensaje: str, historial: list, estado: dict) -> dict:
    try:
        cliente_id = estado.get("cliente_id", "")
        memoria = obtener_memoria_avanzada(cliente_id)

        # Detectar emoción y actualizar memoria
        emocion_actual = detectar_emocion(mensaje)
        if emocion_actual not in memoria.get("emociones", []):
            memoria.setdefault("emociones", []).append(emocion_actual)
            actualizar_memoria_avanzada(cliente_id, {"emociones": memoria["emociones"]})

        transiciones = {
            "positivo": ["¡Perfecto!", "¡Genial!"],
            "indeciso": ["Entiendo, vamos paso a paso.", "Muy bien, veamos opciones."],
            "negativo": ["No te preocupes, encontraremos la mejor opción.", "Vamos a ver alternativas que te convengan."],
            "neutral": ["Muy bien, continuemos.", "Perfecto, sigamos."]
        }
        transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))

        # 1) Falta nombre
        if not estado.get("nombre"):
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {
                "respuesta": f"¡Hola! Bienvenido a {AGENCIA}. Soy {BOT_NOMBRE}. ¿Cuál es tu nombre? Además, ¿buscas un vehículo nuevo o usado?",
                "enviar_a_asesor": False
            }

        # 2) Falta tipo_auto
        if "nombre" in estado and not estado.get("tipo_auto"):
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": f"{transicion} ¿Buscas un auto nuevo o usado?", "enviar_a_asesor": False}

        # 3) Falta modelo
        if "nombre" in estado and "tipo_auto" in estado and not estado.get("modelo"):
            # Obtener modelos desde la cache Mongo según tipo
            tipo = estado["tipo_auto"].lower()
            modelos_disponibles = obtener_modelos_disponibles(tipo)  # función que obtiene de cache
            if not modelos_disponibles:
                modelos_disponibles = ["Jetta", "Tiguan", "Taos"]  # fallback
            lista_modelos = "\n".join([f"- {m}" for m in modelos_disponibles])
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": f"{transicion} Ahora, ¿qué modelo te interesa?\n{lista_modelos}", "enviar_a_asesor": False}

        # 4) Validar modelo ingresado
        if "modelo" in estado and not estado.get("confirmado"):
            if not validar_modelo_usuario(estado["tipo_auto"], estado["modelo"]):
                modelos_disponibles = obtener_modelos_disponibles(estado["tipo_auto"])
                lista_modelos = "\n".join([f"- {m}" for m in modelos_disponibles])
                actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
                return {
                    "respuesta": f"Lo siento, el modelo '{estado['modelo']}' no está disponible en {estado['tipo_auto']}. Por favor elige uno de los siguientes:\n{lista_modelos}",
                    "enviar_a_asesor": False
                }

            # Confirmación de datos
            _ = obtener_detalles_modelo(estado["modelo"])  # opcional
            telefono = estado.get("telefono") or extraer_telefono_de_jid(cliente_id)
            respuesta = (
                f"Muy bien, continuemos. Confirmemos tus datos:\n"
                f"Nombre: {estado['nombre']}, "
                f"Contacto: {telefono}, "
                f"Modelo: {estado['tipo_auto']} {estado['modelo']}.\n"
                f"¿Es correcto?"
            )
            sends.insert_one({
                "jid": cliente_id,
                "message": {
                    "text": respuesta,
                    "buttons": [
                        {"buttonId": f"yes_{cliente_id}", "buttonText": {"displayText": "✅ Sí"}, "type": 1},
                        {"buttonId": f"no_{cliente_id}", "buttonText": {"displayText": "❌ No"}, "type": 1}
                    ]
                },
                "sent": False
            })
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}

        # 5) Confirmado
        if all(k in estado for k in ["nombre", "tipo_auto", "modelo", "confirmado"]):
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": f"Hola {estado.get('nombre','')}, un asesor te contactará pronto.", "enviar_a_asesor": True}

        # 6) Fallback con LLM
        historial_resumido = resumir_historial_emociones(historial, estado, memoria)
        prompt_base = (
            f"Eres {BOT_NOMBRE}, asistente de ventas de {AGENCIA}. "
            f"Estilo humano, cercano, profesional y amable. Usa memoria e historial.\n"
            f"Historial resumido:\n{historial_resumido}\n"
            f"Mensaje actual: {mensaje}"
        )
        try:
            response = ollama.generate(model="llama3", prompt=prompt_base)
            texto_respuesta = response["response"].strip()
        except Exception as e:
            logger.error(f"Ollama error: {e}")
            texto_respuesta = "Lo siento, hubo un error generando la respuesta. Por favor intenta de nuevo."
        return {"respuesta": texto_respuesta, "enviar_a_asesor": False}

    except Exception as e:
        logger.error(f"Error generar_respuesta_premium: {str(e)}")
        return {"respuesta": "Lo siento, hubo un error generando la respuesta. Por favor intenta de nuevo.", "enviar_a_asesor": False}

# =========================
# Asignación de asesor
# =========================
def asignar_asesor_humano(cliente_id: str):
    try:
        estado = obtener_estado(cliente_id)
        if not all(k in estado for k in ["telefono", "nombre", "tipo_auto", "modelo", "confirmado"]):
            logger.warning(f"Datos incompletos para asignar asesor a {cliente_id}: {estado}")
            return

        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        if not asesores:
            logger.warning("No hay asesores disponibles")
            return

        asesor = asesores[0]["telefono"]
        mensaje_asesor = (
            f"Hola 👋 {asesor},\n"
            f"Nuevo cliente: {estado['nombre']}.\n"
            f"Interesado en: {estado['tipo_auto']} {estado['modelo']}.\n"
            f"Tel: {estado['telefono']}\n"
            f"¿Estás disponible para contactarlo ahora?"
        )

        sends.insert_one({
            "jid": f"{asesor}@s.whatsapp.net",
            "message": {
                "text": mensaje_asesor,
                "buttons": [
                    {"buttonId": f"yes_{cliente_id}", "buttonText": {"displayText": "✅ Sí"}, "type": 1},
                    {"buttonId": f"no_{cliente_id}", "buttonText": {"displayText": "❌ No"}, "type": 1}
                ]
            },
            "sent": False
        })

        asignaciones.insert_one({
            "cliente_id": cliente_id,
            "asesor_phone": asesor,
            "respuesta": None,
            "fecha": datetime.now()
        })

        # Mensaje para el cliente
        sends.insert_one({
            "jid": cliente_id,
            "message": {"text": f"Hola {estado['nombre']} 👋, tu solicitud fue confirmada. Un asesor te contactará muy pronto."},
            "sent": False
        })

        logger.info(f"Asignado asesor {asesor} a {cliente_id}")
    except Exception as e:
        logger.error(f"Error asignar_asesor_humano: {str(e)}")

# =========================
# Rutas
# =========================
@app.post("/webhook")
async def webhook(mensaje: Mensaje):
    try:
        cliente_id = mensaje.cliente_id
        texto = mensaje.texto.strip()
        t_lower = texto.lower()
        logger.debug(f"Webhook: {cliente_id} -> {texto}")

        # Reset duro con "hola"
        if t_lower == "hola":
            estado_conversacion.delete_one({"_id": cliente_id})
            memoria_col.delete_one({"_id": cliente_id})
            asignaciones.delete_many({"cliente_id": cliente_id})  # FIX
            sends.delete_many({"jid": cliente_id})
            logger.info(f"Estado reseteado por 'hola' para {cliente_id}")

        estado = obtener_estado(cliente_id)
        guardar_mensaje(cliente_id, texto, "user")

        # Extraer teléfono desde JID
        if "telefono" not in estado and "@s.whatsapp.net" in cliente_id:
            phone = extraer_telefono_de_jid(cliente_id)
            if es_contacto_valido(phone):
                actualizar_estado(cliente_id, {"telefono": phone})
                estado["telefono"] = phone

        # Parsear entrada
        nombre, tipo_auto = parsear_entrada(t_lower)
        if "nombre" not in estado and nombre:
            actualizar_estado(cliente_id, {"nombre": nombre})
            estado["nombre"] = nombre
        if "nombre" in estado and "tipo_auto" not in estado and tipo_auto:
            actualizar_estado(cliente_id, {"tipo_auto": tipo_auto})
            estado["tipo_auto"] = tipo_auto

        # Detectar modelo si no está
        if "nombre" in estado and "tipo_auto" in estado and "modelo" not in estado:
            # Intentar mapear a base conocida
            for base in (m.lower() for m in CATALOGO_MODELOS_KNOWN):
                if re.search(rf"\b{re.escape(base)}\b", t_lower):
                    actualizar_estado(cliente_id, {"modelo": BASENAMES.get(base, base.title())})
                    estado["modelo"] = BASENAMES.get(base, base.title())
                    break
            # Variantes comunes
            if "modelo" not in estado:
                for needle, canon in VARIANTAS:
                    if needle in t_lower:
                        actualizar_estado(cliente_id, {"modelo": canon})
                        estado["modelo"] = canon
                        break

        # Confirmación manual escrita "sí/si/no" si ya se pidió confirmar
        if all(k in estado for k in ["nombre", "tipo_auto", "modelo"]) and not estado.get("confirmado"):
            if t_lower in ("si", "sí"):
                actualizar_estado(cliente_id, {"confirmado": True})
                estado["confirmado"] = True
            elif t_lower == "no":
                # Reiniciar solo el modelo
                actualizar_estado(cliente_id, {"modelo": None, "confirmado": None})
                estado.pop("modelo", None)
                estado.pop("confirmado", None)

        # Historial para LLM / contexto
        historial = list(historial_col.find({"cliente_id": cliente_id}))
        historial_texto = [{"role": h["role"], "mensaje": h["mensaje"]} for h in historial]

        result = generar_respuesta_premium(texto, historial_texto, {**estado, "cliente_id": cliente_id})
        respuesta = result["respuesta"]
        enviar_a_asesor = result["enviar_a_asesor"]

        # Guardar y enviar al usuario (texto base; los botones ya se insertaron aparte)
        guardar_mensaje(cliente_id, respuesta, "assistant")
        sends.insert_one({
            "jid": cliente_id,
            "message": {"text": respuesta},
            "sent": False
        })

        if enviar_a_asesor:
            asignar_asesor_humano(cliente_id)

        logger.info(f"Resp enviada a {cliente_id}: {respuesta}")
        return {"respuesta": respuesta}

    except Exception as e:
        logger.error(f"Error en webhook: {str(e)}")
        return {"respuesta": "Lo siento, hubo un error generando la respuesta. Por favor intenta de nuevo."}

@app.post("/advisor_response")
async def advisor_response(response: AdvisorResponse):
    try:
        cliente_id = response.cliente_id
        respuesta = response.respuesta.lower()
        asesor_phone = response.asesor_phone
        asignaciones.update_one(
            {"cliente_id": cliente_id, "asesor_phone": asesor_phone},
            {"$set": {"respuesta": respuesta, "fecha_respuesta": datetime.now()}}
        )
        estado = obtener_estado(cliente_id)
        if respuesta in ["yes", "sí", "si"]:
            mensaje = (
                f"Contacta a {estado.get('nombre','')} interesado en un "
                f"{estado.get('tipo_auto','')} {estado.get('modelo','')} "
                f"al número {estado.get('telefono','')}"
            )
            sends.insert_one({"jid": f"{asesor_phone}@s.whatsapp.net", "message": {"text": mensaje}, "sent": False})
            sends.insert_one({"jid": cliente_id, "message": {"text": f"Hola, {estado.get('nombre','')}. Un asesor te contactará pronto."}, "sent": False})
        elif respuesta == "no":
            asignar_asesor_humano(cliente_id)
        logger.info(f"advisor_response procesada para {cliente_id}: {respuesta}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error en advisor_response: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error del servidor: {str(e)}")

@app.get("/get_asesores")
def get_asesores():
    try:
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        return [a["telefono"] for a in asesores if "telefono" in a]
    except Exception as e:
        logger.error(f"Error en /get_asesores: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error del servidor: {str(e)}")

# =========================
# Main
# =========================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
