"""
BOT DE MEDICINAS CUBA - VERSIÓN INICIAL
Fase 1: Búsqueda básica por provincia
Compatible con Python 3.14 y python-telegram-bot v21+
"""

import logging
import json
import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# Configuración
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Estados para la conversación
PROVINCIA, BUSQUEDA = range(2)

# Lock para proteger acceso concurrente a los datos
datos_lock = asyncio.Lock()

# ===== BASE DE DATOS SIMPLE (archivo JSON) =====

ARCHIVO_DATOS = "medicinas_cuba.json"

DATOS_POR_DEFECTO = {
    "usuarios": {},
    "medicamentos": {
        "paracetamol": {
            "contactos": [
                {"telefono": "+53 5 1234567", "provincia": "Santiago de Cuba", "zona": "Centro", "fecha": "2024-01-15"},
                {"telefono": "+53 5 7654321", "provincia": "Santiago de Cuba", "zona": "Reparto", "fecha": "2024-01-14"}
            ]
        },
        "ibuprofeno": {
            "contactos": [
                {"telefono": "+53 5 9876543", "provincia": "Santiago de Cuba", "zona": "Alto", "fecha": "2024-01-15"}
            ]
        },
        "amoxicilina": {
            "contactos": [
                {"telefono": "+53 5 4567890", "provincia": "Santiago de Cuba", "zona": "Centro", "fecha": "2024-01-13"}
            ]
        }
    },
    "reportes": [],
    "provincias": [
        "Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas",
        "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila",
        "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba",
        "Guantánamo", "Isla de la Juventud"
    ]
}

def cargar_datos():
    """Carga los datos desde el archivo JSON o crea datos por defecto"""
    if os.path.exists(ARCHIVO_DATOS):
        try:
            with open(ARCHIVO_DATOS, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error cargando datos: {e}. Usando datos por defecto.")
    return DATOS_POR_DEFECTO

def guardar_datos(datos_a_guardar):
    """Guarda los datos en el archivo JSON"""
    with open(ARCHIVO_DATOS, 'w', encoding='utf-8') as f:
        json.dump(datos_a_guardar, f, ensure_ascii=False, indent=2)

# Cargar datos al iniciar
datos = cargar_datos()

# ===== FUNCIONES DEL BOT =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Inicio del bot"""
    usuario_id = str(update.effective_user.id)
    
    if usuario_id in datos["usuarios"] and datos["usuarios"][usuario_id].get("provincia"):
        provincia = datos["usuarios"][usuario_id]["provincia"]
        mensaje = (
            f"✅ ¡Bienvenido de vuelta!\n"
            f"Tu provincia guardada es: {provincia}\n\n"
            f"Usa /buscar [medicina] para encontrar contactos.\n\n"
            f"Ejemplo: /buscar paracetamol"
        )
        await update.message.reply_text(mensaje)
        return ConversationHandler.END
    
    lista_provincias = "\n".join([f"{i+1}. {p}" for i, p in enumerate(datos["provincias"])])
    mensaje = (
        f"🌍 ¡Bienvenido a Medicinas Cuba Bot!\n\n"
        f"¿De qué provincia eres?\n\n{lista_provincias}\n\n"
        f"Responde con el NÚMERO de tu provincia:"
    )
    await update.message.reply_text(mensaje)
    return PROVINCIA

async def seleccionar_provincia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda la provincia seleccionada"""
    usuario_id = str(update.effective_user.id)
    texto = update.message.text.strip()
    
    try:
        numero = int(texto)
        if 1 <= numero <= len(datos["provincias"]):
            provincia = datos["provincias"][numero - 1]
            
            async with datos_lock:
                if usuario_id not in datos["usuarios"]:
                    datos["usuarios"][usuario_id] = {}
                datos["usuarios"][usuario_id]["provincia"] = provincia
                guardar_datos(datos)
            
            mensaje = (
                f"✅ Provincia guardada: {provincia}\n\n"
                f"Ahora puedes buscar medicinas con:\n"
                f"/buscar [nombre]\n\n"
                f"Ejemplo: /buscar paracetamol"
            )
            await update.message.reply_text(mensaje)
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                f"Número inválido. Elige entre 1 y {len(datos['provincias'])}"
            )
            return PROVINCIA
    except ValueError:
        await update.message.reply_text(
            "Por favor, envía solo el NÚMERO de tu provincia (ejemplo: 14 para Santiago de Cuba)"
        )
        return PROVINCIA

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela la conversación"""
    await update.message.reply_text("Operación cancelada. Usa /start cuando quieras comenzar.")
    return ConversationHandler.END

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /buscar - Busca una medicina"""
    usuario_id = str(update.effective_user.id)
    
    if usuario_id not in datos["usuarios"] or not datos["usuarios"][usuario_id].get("provincia"):
        await update.message.reply_text("❌ Primero debes configurar tu provincia. Usa /start")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Escribe el nombre de la medicina.\nEjemplo: /buscar paracetamol")
        return
    
    medicina = " ".join(context.args).lower().strip()
    provincia_usuario = datos["usuarios"][usuario_id]["provincia"]
    
    # Búsqueda flexible (coincidencia parcial)
    resultados = []
    for med_key in datos["medicamentos"]:
        if medicina == med_key or medicina in med_key or med_key in medicina:
            resultados.append(med_key)
    
    if not resultados:
        disponibles = ", ".join(datos["medicamentos"].keys())
        mensaje = (
            f"❌ No encontré '{medicina}' en mi base de datos.\n\n"
            f"💡 Medicinas disponibles: {disponibles}\n\n"
            f"Si conoces esta medicina, escribe /reportar {medicina}"
        )
        await update.message.reply_text(mensaje)
        return
    
    # Si hay múltiples coincidencias, priorizar la exacta
    medicina_key = resultados[0]
    if len(resultados) > 1:
        exactas = [r for r in resultados if r == medicina]
        if exactas:
            medicina_key = exactas[0]
    
    contactos = datos["medicamentos"][medicina_key]["contactos"]
    contactos_filtrados = [c for c in contactos if c["provincia"] == provincia_usuario]
    
    if not contactos_filtrados:
        await update.message.reply_text(
            f"❌ No hay contactos para '{medicina_key}' en {provincia_usuario}\n\n"
            f"Prueba otra provincia con /cambiar_provincia"
        )
        return
    
    mensaje = f"🔍 {medicina_key.upper()} en {provincia_usuario}\n\n"
    mensaje += f"✅ Encontré {len(contactos_filtrados)} contacto(s):\n\n"
    
    for i, c in enumerate(contactos_filtrados, 1):
        zona = c.get('zona', 'Sin zona')
        mensaje += f"{i}. 📱 {c['telefono']}\n   📍 {zona}\n   🕒 {c['fecha']}\n\n"
    
    mensaje += "---\n💬 Contacta directamente por WhatsApp o Telegram"
    
    await update.message.reply_text(mensaje)

async def cambiar_provincia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /cambiar_provincia - Cambia la provincia del usuario"""
    lista_provincias = "\n".join([f"{i+1}. {p}" for i, p in enumerate(datos["provincias"])])
    mensaje = f"📍 Cambiar provincia\n\n{lista_provincias}\n\nResponde con el NÚMERO de tu nueva provincia:"
    await update.message.reply_text(mensaje)
    return PROVINCIA

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /ayuda - Muestra ayuda"""
    mensaje = (
        "📚 AYUDA - Medicinas Cuba Bot\n\n"
        "Comandos disponibles:\n"
        "/start - Iniciar o configurar provincia\n"
        "/buscar [medicina] - Buscar contactos\n"
        "/cambiar_provincia - Cambiar tu provincia\n"
        "/reportar [medicina] - Reportar medicina faltante\n"
        "/ayuda - Mostrar esta ayuda\n\n"
        "Ejemplos:\n"
        "/buscar paracetamol\n"
        "/buscar ibuprofeno\n\n"
        "Nota: Este bot recopila información pública.\n"
        "Siempre verifica antes de comprar."
    )
    await update.message.reply_text(mensaje)

async def reportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /reportar - Reportar una medicina que falta"""
    if not context.args:
        await update.message.reply_text("Ejemplo: /reportar paracetamol 500mg\n\nAsí puedo agregar esta medicina a la base de datos.")
        return
    
    medicina = " ".join(context.args)
    usuario_id = str(update.effective_user.id)
    
    async with datos_lock:
        if "reportes" not in datos:
            datos["reportes"] = []
        datos["reportes"].append({
            "medicina": medicina,
            "usuario_id": usuario_id
        })
        guardar_datos(datos)
    
    await update.message.reply_text(f"✅ Gracias por reportar '{medicina}'. Lo revisaré para agregarlo próximamente.")

# ===== CONFIGURACIÓN DEL BOT =====

def main():
    """Función principal que inicia el bot"""
    # Token del bot
    TOKEN = "8685939368:AAESfgUVeQG0qA8521Qx5LO_7Qm3LY27Qq0"
    
    # Crear la aplicación
    application = Application.builder().token(TOKEN).build()
    
    # Conversación UNIFICADA para start y cambiar_provincia
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("cambiar_provincia", cambiar_provincia),
        ],
        states={
            PROVINCIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, seleccionar_provincia)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    
    # Registrar manejadores
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("buscar", buscar))
    application.add_handler(CommandHandler("ayuda", ayuda))
    application.add_handler(CommandHandler("reportar", reportar))
    
    # Iniciar el bot
    print("🤖 Bot iniciado... Esperando mensajes")
    application.run_polling()

if __name__ == "__main__":
    main()