from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import uvicorn
import random
from datetime import datetime, timedelta
from pymongo import MongoClient
import ollama

# --- Configuración MongoDB ---
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbotdb"]
historial_col = db["historial"]
estado_col = db["estado_conversacion"]
asesores_col = db["asesores"]

# --- Inicializar asesores si no existen ---
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

# --- Clases de request ---
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

# --- Funciones de historial ---
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

# --- Función de generación Ollama ---
def generar_respuesta_ollama(cliente_id, asesor, area):
    historial = obtener_historial(cliente_id)
    mensajes = [{"role": h["rol"], "content": h["contenido"]} for h in historial]
    system_prompt = {
        "role": "system",
        "content": f"Eres {asesor}, un asesor experto en {area}. Responde de manera profesional y amigable."
    }
    mensajes.insert(0, system_prompt)
    respuesta = ollama.chat(model="llama3", messages=mensajes)
    texto = respuesta['message']['content']
    guardar_mensaje(cliente_id, "assistant", texto, asesor)
    return texto

# --- Endpoint principal ---
app = FastAPI()

@app.post("/webhook")
def webhook(msg: Mensaje):
    # Por simplicidad, asignamos un asesor random del área "Ventas" si no hay estado previo
    estado = estado_col.find_one({"cliente_id": msg.cliente_id}) or {}
    if "asesor" not in estado:
        asesor_doc = random.choice(list(asesores_col.find({"activo": True})))
        asesor = asesor_doc["nombre"]
        area = asesor_doc["area"]
        estado_col.update_one({"cliente_id": msg.cliente_id}, {"$set": {"asesor": asesor, "area": area}}, upsert=True)
    else:
        asesor = estado["asesor"]
        area = estado["area"]

    guardar_mensaje(msg.cliente_id, "user", msg.texto, asesor)
    respuesta = generar_respuesta_ollama(msg.cliente_id, asesor, area)
    return {"respuesta": respuesta}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
