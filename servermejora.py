from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import random
from pymongo import MongoClient
import ollama
import uvicorn
from uuid import uuid4

# --- Configuración MongoDB ---
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbotdb"]
historial_col = db["historial"]
estado_col = db["estado_conversacion"]
asesores_col = db["asesores"]

# --- Datos iniciales ---
asistentes_admin = ["Marcela", "Carlos", "Sofía", "Javier", "Luisa"]

saludos_iniciales_naturales = [
    "¡Hola! Muchas gracias por comunicarte, te atiende {asistente}, asistente administrativo. ¿Con quién tengo el gusto?",
    "¡Hola! Me llamo {asistente} y estoy aquí para ayudarte. ¿Me pudieras compartir tu nombre, por favor?",
    "¡Hola que tal! Soy {asistente} y estoy aquí para ayudarte. ¿Pudieras compartirme tu nombre, por favor?",
    "¡Hola! Gracias por comunicarte, soy {asistente}. ¿Con quien tengo el gusto?",
]

disculpas_naturales = [
    "Estoy aquí para ayudarte y me gustaría saber tu nombre, ¿me lo puedes compartir, por favor?",
    "Me gustaría dirigirme a ti por tu nombre para darle más formalidad, ¿me lo puedes compartir, por favor?",
    "Quiero asegurarme de dirigirme a ti correctamente, ¿me dices tu nombre, por favor?",
    "Para ofrecerte un mejor servicio, ¿me podrías compartir tu nombre, por favor?",
    "¿me puedes compartir tu nombre para que pueda atenderte mejor?",
]

frases_pedir_area = [
    "¿Podrías decirme en qué área te gustaría que te atienda? Tenemos Ventas, Servicios y Refacciones.",
    "Para ayudarte mejor, ¿me dices si tu interés es en Ventas, Servicios o Refacciones?",
    "¿En qué área necesitas asistencia? Tenemos Ventas, Servicios y Refacciones.",
    "¿En qué área te gustaría recibir apoyo? Tenemos Ventas, Servicios y Refacciones.",
    "¿En qué área podemos ayudarte hoy? (Ventas, Servicios, Refacciones)",
    "¿En cuál de estas áreas quieres que te apoye? Ventas, Servicios o Refacciones.",
    "¿Me podrías indicar si buscas ayuda en Ventas, Servicios o Refacciones?",
]

frases_no_area = [
    "No estoy seguro de haber entendido el área. ¿Podrías repetirlo, por favor?",
    "Disculpa, ¿podrías decirme si es Ventas, Servicios o Refacciones?",
    "¿Podrías aclararme si es Ventas, Servicios o Refacciones a lo que te refieres?",
]

frases_pedir_contacto = [
    "Para que un asesor se comunique contigo por WhatsApp o llamada, ¿me podrías compartir tu número de teléfono?",
    "¿Me puedes proporcionar tu número de teléfono para que un asesor te contacte por WhatsApp o llamada?",
    "Para brindarte un mejor servicio, ¿me compartes tu número de teléfono, por favor?",
]

frases_canalizacion = [
    "Perfecto, {nombre}. Un asesor especializado en {area}, {asesor}, te contactará pronto por WhatsApp o llamada al {telefono}. Mientras, puedo ayudarte con más información si lo deseas.",
    "Entendido, {nombre}. Te estoy canalizando con {asesor} del área de {area}. Te contactará al {telefono} por WhatsApp o llamada. ¿Quieres que te ayude con algo más mientras tanto?",
]

frases_reasignacion = [
    "Disculpa, {nombre}, parece que {asesor} no está disponible ahora. Te estoy conectando con {nuevo_asesor}, especialista en {area}, quien te contactará al {telefono} por WhatsApp o llamada.",
    "Lo siento, {nombre}, {asesor} no ha respondido a tiempo. Te canalizo con {nuevo_asesor} de {area}, quien te contactará al {telefono}.",
]

# --- Funciones auxiliares ---
def inicializar_asesores():
    if asesores_col.count_documents({}) == 0:
        asesores_col.insert_many([
            {"nombre": "Ana", "area": "Ventas", "activo": True, "telefono": "+1234567890"},
            {"nombre": "Pedro", "area": "Ventas", "activo": True, "telefono": "+1234567891"},
            {"nombre": "Laura", "area": "Ventas", "activo": False, "telefono": "+1234567892"},
            {"nombre": "Miguel", "area": "Servicios", "activo": True, "telefono": "+1234567893"},
            {"nombre": "Elena", "area": "Servicios", "activo": True, "telefono": "+1234567894"},
            {"nombre": "Roberto", "area": "Servicios", "activo": False, "telefono": "+1234567895"},
            {"nombre": "Gabriela", "area": "Refacciones", "activo": True, "telefono": "+1234567896"},
            {"nombre": "Diego", "area": "Refacciones", "activo": True, "telefono": "+1234567897"},
            {"nombre": "Valeria", "area": "Refacciones", "activo": False, "telefono": "+1234567898"},
        ])
inicializar_asesores()

def asignar_asistente_admin():
    return random.choice(asistentes_admin)

def obtener_asesores_activos(area, exclude_asesor=None):
    query = {"area": area, "activo": True}
    if exclude_asesor:
        query["nombre"] = {"$ne": exclude_asesor}
    return list(asesores_col.find(query))

def asignar_asesor_activo(area, exclude_asesor=None):
    activos = obtener_asesores_activos(area, exclude_asesor)
    if not activos:
        return None
    return random.choice(activos)

def guardar_mensaje(cliente_id, rol, contenido, asistente=None):
    doc = {
        "cliente_id": cliente_id,
        "rol": rol,
        "contenido": contenido,
        "timestamp": datetime.now()
    }
    if asistente:
        doc["asistente"] = asistente
    historial_col.insert_one(doc)

def obtener_historial(cliente_id):
    return list(historial_col.find({"cliente_id": cliente_id}))

def actualizar_estado(cliente_id, nuevo_estado):
    estado_col.update_one({"cliente_id": cliente_id}, {"$set": nuevo_estado}, upsert=True)

def obtener_estado(cliente_id):
    return estado_col.find_one({"cliente_id": cliente_id}) or {}

def verificar_respuesta_asesor(cliente_id, tiempo_limite_minutos=7):
    ultimo_mensaje = historial_col.find_one({"cliente_id": cliente_id, "rol": "user"}, sort=[("timestamp", -1)])
    if not ultimo_mensaje:
        return False
    return (datetime.now() - ultimo_mensaje["timestamp"]) > timedelta(minutes=tiempo_limite_minutos)

def es_nombre_valido(nombre):
    nombre = nombre.strip().lower()
    palabras_no_validas = ["no", "nada", "salir", "precio", "me interesa", "informacion", "más", "mas", "hola", ""]
    return len(nombre) >= 3 and nombre not in palabras_no_validas and any(c.isalpha() for c in nombre)

def es_contacto_valido(contacto):
    contacto = contacto.strip()
    return len(contacto) >= 10 and contacto.replace("+", "").replace(" ", "").isdigit()

def generar_respuesta_ollama(cliente_id, asesor, area):
    historial = obtener_historial(cliente_id)
    mensajes = [{"role": h["rol"], "content": h["contenido"]} for h in historial]
    system_prompt = {"role": "system", "content": f"Eres {asesor}, un asesor experto en {area}. Responde de manera profesional y amigable."}
    mensajes.insert(0, system_prompt)
    respuesta = ollama.chat(model="llama3", messages=mensajes)
    texto = respuesta['message']['content']
    guardar_mensaje(cliente_id, "assistant", texto, asesor)
    return texto

# --- Generar token de confirmación ---
def generar_token():
    return str(uuid4())

# --- FastAPI ---
app = FastAPI()

class Mensaje(BaseModel):
    cliente_id: str
    texto: str

# Endpoint para confirmar al cliente
@app.get("/confirmar/{token}")
def confirmar_cliente(token: str):
    estado = estado_col.find_one({"token_confirmacion": token, "confirmado": False})
    if not estado:
        raise HTTPException(status_code=404, detail="Token inválido o ya confirmado")
    
    estado_col.update_one(
        {"_id": estado["_id"]},
        {"$set": {"confirmado": True, "fecha_confirmacion": datetime.now()}}
    )
    return {"mensaje": f"Cliente {estado.get('nombre','')} confirmado como atendido por {estado.get('asesor','')}"}

# Cron job simulado: reasignar clientes no confirmados
def reasignar_pendientes(minutos=5):
    tiempo_limite = datetime.now() - timedelta(minutes=minutos)
    pendientes = estado_col.find({"confirmado": False, "fecha_asignacion": {"$lt": tiempo_limite}})
    
    for lead in pendientes:
        nuevo_asesor_doc = asignar_asesor_activo(lead["area"], exclude_asesor=lead["asesor"])
        if nuevo_asesor_doc:
            nuevo_token = generar_token()
            estado_col.update_one(
                {"_id": lead["_id"]},
                {"$set": {
                    "asesor": nuevo_asesor_doc["nombre"],
                    "fecha_asignacion": datetime.now(),
                    "token_confirmacion": nuevo_token
                }}
            )
            # Aquí envías el mensaje al nuevo asesor con el nuevo link
            print(f"Cliente {lead['nombre']} reasignado a {nuevo_asesor_doc['nombre']} con link /confirmar/{nuevo_token}")

@app.post("/webhook")
def webhook(msg: Mensaje):
    estado = obtener_estado(msg.cliente_id)

    # Si es nuevo cliente
    if "asistente_admin" not in estado:
        asistente = asignar_asistente_admin()
        actualizar_estado(msg.cliente_id, {"asistente_admin": asistente, "paso": "nombre"})
        return {"respuesta": random.choice(saludos_iniciales_naturales).format(asistente=asistente)}

    paso = estado.get("paso", "nombre")
    asistente = estado["asistente_admin"]

    # Flujo por pasos
    if paso == "nombre":
        if es_nombre_valido(msg.texto):
            actualizar_estado(msg.cliente_id, {"nombre": msg.texto, "paso": "contacto"})
            return {"respuesta": random.choice(frases_pedir_contacto)}
        else:
            return {"respuesta": random.choice(disculpas_naturales)}

    if paso == "contacto":
        if es_contacto_valido(msg.texto):
            actualizar_estado(msg.cliente_id, {"telefono": msg.texto, "paso": "area"})
            return {"respuesta": random.choice(frases_pedir_area)}
        else:
            return {"respuesta": "El número no es válido, por favor compártelo con formato +1234567890."}

    if paso == "area":
        if "venta" in msg.texto.lower():
            area = "Ventas"
        elif "servicio" in msg.texto.lower():
            area = "Servicios"
        elif "refaccion" in msg.texto.lower():
            area = "Refacciones"
        else:
            return {"respuesta": random.choice(frases_no_area)}

        asesor_doc = asignar_asesor_activo(area)
        if not asesor_doc:
            return {"respuesta": f"No hay asesores disponibles en {area} en este momento."}

        # Generar token de confirmación para el asesor
        token = generar_token()
        actualizar_estado(msg.cliente_id, {
            "area": area,
            "asesor": asesor_doc["nombre"],
            "paso": "chat",
            "fecha_asignacion": datetime.now(),
            "token_confirmacion": token,
            "confirmado": False
        })

        # Aquí envías mensaje al asesor con el link
        link_confirmacion = f"https://midominio.com/confirmar/{token}"
        print(f"Envía al asesor {asesor_doc['nombre']} el link: {link_confirmacion}")

        return {"respuesta": random.choice(frases_canalizacion).format(
            nombre=estado.get("nombre", ""),
            area=area,
            asesor=asesor_doc["nombre"],
            telefono=estado.get("telefono", "")
        )}

    if paso == "chat":
        # Reasignar si el asesor no confirmó
        tiempo_limite = datetime.now() - timedelta(minutes=5)
        if not estado.get("confirmado") and estado.get("fecha_asignacion") < tiempo_limite:
            nuevo_asesor_doc = asignar_asesor_activo(estado["area"], exclude_asesor=estado["asesor"])
            if nuevo_asesor_doc:
                nuevo_token = generar_token()
                actualizar_estado(msg.cliente_id, {
                    "asesor": nuevo_asesor_doc["nombre"],
                    "fecha_asignacion": datetime.now(),
                    "token_confirmacion": nuevo_token
                })
                link_confirmacion = f"https://midominio.com/confirmar/{nuevo_token}"
                print(f"Reasignando al cliente {estado.get('nombre')} al asesor {nuevo_asesor_doc['nombre']} con link {link_confirmacion}")
                return {"respuesta": random.choice(frases_reasignacion).format(
                    nombre=estado["nombre"],
                    asesor=estado["asesor"],
                    nuevo_asesor=nuevo_asesor_doc["nombre"],
                    area=estado["area"],
                    telefono=estado["telefono"]
                )}
            else:
                return {"respuesta": f"No hay más asesores disponibles en {estado['area']} en este momento."}

        # Generar respuesta con Ollama
        return {"respuesta": generar_respuesta_ollama(msg.cliente_id, estado["asesor"], estado["area"])}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
