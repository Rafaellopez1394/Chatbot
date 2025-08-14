import random
from pymongo import MongoClient
import ollama
from datetime import datetime, timedelta

# --- Configuración MongoDB ---
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbotdb"]
historial_col = db["historial"]
estado_col = db["estado_conversacion"]
asesores_col = db["asesores"]

# --- Asistentes Administrativos Iniciales ---
asistentes_admin = ["Marcela", "Carlos", "Sofía", "Javier", "Luisa"]

# --- Inicializar Asesores (Ejemplo) ---
def inicializar_asesores():
    if asesores_col.count_documents({}) == 0:
        asesores_ejemplo = [
            {"nombre": "Ana", "area": "Ventas", "activo": True},
            {"nombre": "Pedro", "area": "Ventas", "activo": True},
            {"nombre": "Laura", "area": "Ventas", "activo": False},
            {"nombre": "Miguel", "area": "Servicios", "activo": True},
            {"nombre": "Elena", "area": "Servicios", "activo": True},
            {"nombre": "Roberto", "area": "Servicios", "activo": False},
            {"nombre": "Gabriela", "area": "Refacciones", "activo": True},
            {"nombre": "Diego", "area": "Refacciones", "activo": True},
            {"nombre": "Valeria", "area": "Refacciones", "activo": False},
        ]
        asesores_col.insert_many(asesores_ejemplo)

inicializar_asesores()

# --- Frases ---
saludos_iniciales_naturales = [
    "¡Hola! Muchas gracias por comunicarte, te atiende {asistente}, asistente administrativo. ¿Con quién tengo el gusto?",
    "¡Hola! Me llamo {asistente} y estoy aquí para ayudarte. ¿Me pudieras compartirme tu nombre, por favor?",
    "¡Hola que tal! Soy {asistente} y estoy aquí para ayudarte. ¿Pudieras compartirme tu nombre, por favor?",
    "¡Hola! Gracias por comunicarte, soy {asistente}. ¿Con quien tengo el gusto?",
]

disculpas_naturales = [
    "Estoy aquí para ayudarte y me gustaría saber tu nombre, ¿me lo puedes compartir, por favor?",
    "Me gustaría dirigirme a ti por tu nombre para darle más formalidad, ¿me lo puedes compartir, por favor?",
    "Quiero asegurarme de dirigirme a ti correctamente, ¿me dices tu nombre, por favor?",
    "Para ofrecerte un mejor servicio, ¿me podrías compartir tu nombre, por favor?",
    "Si no te importa, ¿me dices tu nombre para que pueda atenderte mejor?",
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
    "Para que un asesor se comunique contigo, ¿me podrías compartir tu teléfono o correo electrónico?",
    "¿Me puedes proporcionar tu teléfono o correo para que un asesor te contacte?",
    "Para brindarte un mejor servicio, ¿me compartes tu teléfono o correo, por favor?",
]

frases_canalizacion = [
    "Perfecto, te estoy canalizando con {asesor}, un asesor especializado en {area}. ¡Hola {nombre}, soy {asesor} de {area}! ¿En qué puedo ayudarte?",
    "Entendido, ahora te conecto con {asesor} del área de {area}. ¡Hola {nombre}, {asesor} aquí, especialista en {area}. ¿Cómo puedo asistirte?",
]

frases_reasignacion = [
    "Disculpa, {nombre}, parece que {asesor} no está disponible ahora. Te estoy conectando con {nuevo_asesor}, especialista en {area}. ¡Hola, soy {nuevo_asesor}! ¿En qué puedo ayudarte?",
    "Lo siento, {nombre}, {asesor} no ha respondido a tiempo. Te canalizo con {nuevo_asesor} de {area}. ¡Hola, soy {nuevo_asesor}! ¿Cómo te puedo ayudar?",
]

# --- Funciones ---
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
        raise ValueError(f"No hay asesores activos disponibles en el área de {area}.")
    return random.choice(activos)["nombre"]

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
    estado_col.update_one(
        {"cliente_id": cliente_id},
        {"$set": nuevo_estado},
        upsert=True
    )

def obtener_estado(cliente_id):
    return estado_col.find_one({"cliente_id": cliente_id}) or {}

def verificar_respuesta_asesor(cliente_id, asesor, tiempo_limite_minutos=7):
    ultimo_mensaje = historial_col.find_one(
        {"cliente_id": cliente_id, "rol": "user"},
        sort=[("timestamp", -1)]
    )
    if not ultimo_mensaje:
        return False
    tiempo_transcurrido = datetime.now() - ultimo_mensaje["timestamp"]
    return tiempo_transcurrido > timedelta(minutes=tiempo_limite_minutos)

def es_nombre_valido(nombre):
    nombre = nombre.strip().lower()
    palabras_no_validas = [
        "no", "nada", "salir", "precio", "me interesa",
        "informacion", "más", "mas", "hola", ""
    ]
    if len(nombre) < 3 or nombre in palabras_no_validas:
        return False
    if not any(c.isalpha() for c in nombre):
        return False
    return True

def es_contacto_valido(contacto):
    contacto = contacto.strip()
    if len(contacto) < 5:
        return False
    return True

def pedir_nombre(cliente_id, asistente):
    estado = obtener_estado(cliente_id)
    intentos = estado.get("intentos_nombre", 0)

    while True:
        if intentos == 0:
            mensaje = random.choice(saludos_iniciales_naturales).format(asistente=asistente)
        else:
            mensaje = random.choice(disculpas_naturales)
        print(mensaje)
        nombre = input("> ").strip()
        guardar_mensaje(cliente_id, "user", nombre, asistente)

        if es_nombre_valido(nombre):
            actualizar_estado(cliente_id, {"nombre": nombre, "intentos_nombre": 0})
            return nombre
        else:
            intentos += 1
            actualizar_estado(cliente_id, {"intentos_nombre": intentos})

def pedir_area(cliente_id, asistente):
    intentos = 0
    while True:
        if intentos == 0:
            mensaje = random.choice(frases_pedir_area)
        else:
            mensaje = random.choice(frases_no_area)
        print(mensaje)
        area = input("> ").strip().lower()
        guardar_mensaje(cliente_id, "user", area, asistente)

        if any(k in area for k in ["venta", "servicio", "refacción", "refacciones"]):
            if "venta" in area:
                return "Ventas"
            elif "servicio" in area:
                return "Servicios"
            else:
                return "Refacciones"
        intentos += 1

def pedir_contacto(cliente_id, asistente):
    while True:
        mensaje = random.choice(frases_pedir_contacto)
        print(mensaje)
        contacto = input("> ").strip()
        guardar_mensaje(cliente_id, "user", contacto, asistente)
        if es_contacto_valido(contacto):
            return contacto
        else:
            print("Disculpa, el dato que ingresaste no parece válido. ¿Me lo puedes proporcionar otra vez?")

def generar_respuesta_ollama(cliente_id, asesor, area):
    historial = obtener_historial(cliente_id)
    mensajes = [{"role": h["rol"], "content": h["contenido"]} for h in historial]
    system_prompt = {
        "role": "system",
        "content": f"Eres {asesor}, un asesor experto en el área de {area} de una agencia automotriz. Responde de manera profesional, útil y amigable. Usa el nombre del cliente si lo sabes."
    }
    mensajes.insert(0, system_prompt)
    respuesta = ollama.chat(model="llama3", messages=mensajes)
    texto = respuesta['message']['content']
    guardar_mensaje(cliente_id, "assistant", texto, asesor)
    return texto

def main():
    print("Bienvenido a la Agencia Automotriz. Para comenzar, dime tu identificador o número de cliente.")
    cliente_id = input("> ").strip()
    asistente_admin = asignar_asistente_admin()

    nombre = pedir_nombre(cliente_id, asistente_admin)
    print(f"¡Perfecto, {nombre}! Encantado de atenderte.")

    area = pedir_area(cliente_id, asistente_admin)
    
    try:
        asesor = asignar_asesor_activo(area)
        mensaje_canalizacion = random.choice(frases_canalizacion).format(asesor=asesor, area=area, nombre=nombre)
        print(mensaje_canalizacion)
        guardar_mensaje(cliente_id, "assistant", mensaje_canalizacion, asesor)
        actualizar_estado(cliente_id, {"nombre": nombre, "area": area, "asesor": asesor})
    except ValueError as e:
        print(f"Lo siento, actualmente no hay asesores disponibles en {area}. Por favor, proporciona tu contacto para que te llamemos.")
        contacto = pedir_contacto(cliente_id, asistente_admin)
        print(f"Gracias, {nombre}. Te contactaremos pronto al {contacto}.")
        return

    while True:
        pregunta = input("> ").strip()
        if pregunta.lower() == "salir":
            print("Gracias por contactarnos. ¡Que tengas un excelente día!")
            break
        guardar_mensaje(cliente_id, "user", pregunta, asesor)
        
        # Verificar si el asesor no ha respondido
        if verificar_respuesta_asesor(cliente_id, asesor):
            try:
                nuevo_asesor = asignar_asesor_activo(area, exclude_asesor=asesor)
                mensaje_reasignacion = random.choice(frases_reasignacion).format(
                    nombre=nombre, asesor=asesor, nuevo_asesor=nuevo_asesor, area=area
                )
                print(mensaje_reasignacion)
                guardar_mensaje(cliente_id, "assistant", mensaje_reasignacion, nuevo_asesor)
                asesor = nuevo_asesor
                actualizar_estado(cliente_id, {"nombre": nombre, "area": area, "asesor": asesor})
            except ValueError as e:
                print(f"Lo siento, no hay más asesores disponibles en {area}. Por favor, proporciona tu contacto para que te llamemos.")
                contacto = pedir_contacto(cliente_id, asistente_admin)
                print(f"Gracias, {nombre}. Te contactaremos pronto al {contacto}.")
                return
        else:
            respuesta = generar_respuesta_ollama(cliente_id, asesor, area)
            print(f"{asesor}: {respuesta}")

if __name__ == "__main__":
    main()