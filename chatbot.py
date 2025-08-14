import random
from pymongo import MongoClient
import ollama

# --- Configuración MongoDB ---
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbotdb"]
historial_col = db["historial"]
estado_col = db["estado_conversacion"]

# --- Asistentes ---
asistentes = ["Marcela", "Carlos", "Sofía", "Javier", "Luisa"]

# --- Frases ---
saludos_iniciales_naturales = [
    "¡Hola! Muchas gracias por comunicarte, te atiende {asistente}, asistente administrativo. ¿Con quién tengo el gusto?",
    "¡Hola! Me llamo {asistente} y estoy aquí para ayudarte. ¿Me pudiras compartirme tu nombre, por favor?",
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
    "¿En qué área podemos ayudarte hoy? (Ventas, Servicios, Refacciones)"
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

# --- Funciones ---
def asignar_asistente():
    return random.choice(asistentes)

def guardar_mensaje(cliente_id, rol, contenido):
    historial_col.insert_one({
        "cliente_id": cliente_id,
        "rol": rol,
        "contenido": contenido
    })

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
        guardar_mensaje(cliente_id, "user", nombre)

        if es_nombre_valido(nombre):
            actualizar_estado(cliente_id, {"nombre": nombre, "intentos_nombre": 0})
            return nombre
        else:
            intentos += 1
            actualizar_estado(cliente_id, {"intentos_nombre": intentos})

def pedir_area(cliente_id):
    intentos = 0
    while True:
        if intentos == 0:
            mensaje = random.choice(frases_pedir_area)
        else:
            mensaje = random.choice(frases_no_area)
        print(mensaje)
        area = input("> ").strip().lower()
        guardar_mensaje(cliente_id, "user", area)

        if any(k in area for k in ["venta", "servicio", "refacción", "refacciones"]):
            if "venta" in area:
                return "Ventas"
            elif "servicio" in area:
                return "Servicios"
            else:
                return "Refacciones"
        intentos += 1

def pedir_contacto(cliente_id):
    while True:
        mensaje = random.choice(frases_pedir_contacto)
        print(mensaje)
        contacto = input("> ").strip()
        guardar_mensaje(cliente_id, "user", contacto)
        if es_contacto_valido(contacto):
            return contacto
        else:
            print("Disculpa, el dato que ingresaste no parece válido. ¿Me lo puedes proporcionar otra vez?")

def generar_respuesta_ollama(cliente_id):
    historial = obtener_historial(cliente_id)
    mensajes = [{"role": h["rol"], "content": h["contenido"]} for h in historial]
    respuesta = ollama.chat(model="llama3", messages=mensajes)
    texto = respuesta['message']['content']
    guardar_mensaje(cliente_id, "assistant", texto)
    return texto

def main():
    print("Bienvenido a la Agencia Automotriz. Para comenzar, dime tu identificador o número de cliente.")
    cliente_id = input("> ").strip()
    asistente = asignar_asistente()

    nombre = pedir_nombre(cliente_id, asistente)
    print(f"¡Perfecto, {nombre}! encantado de atenderte.")

    area = pedir_area(cliente_id)
    print(f"Entendido, te canalizo al área de {area}.")

    contacto = pedir_contacto(cliente_id)
    print(f"Gracias, {nombre}. Un asesor del área de {area} se pondrá en contacto contigo pronto al {contacto}.")

    print("\nSi quieres puedes hacerme preguntas adicionales")
    while True:
        pregunta = input("> ").strip()
        if pregunta.lower() == "salir":
            print("Gracias por contactarnos. ¡Que tengas un excelente día!")
            break
        guardar_mensaje(cliente_id, "user", pregunta)
        respuesta = generar_respuesta_ollama(cliente_id)
        print("Bot:", respuesta)

if __name__ == "__main__":
    main()
