"""
BOT DE MEDICINAS CUBA - VERSIÓN PROFESIONAL MONGODB
v2.5.1 | Fuzzy Matching | 2 Catálogos | Carrusel Anuncios | Sub-Admins
Optimizado para conexiones lentas (Cuba)
"""

import logging
import os
import re
import html
import asyncio
import hashlib
import threading
import signal
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient
from rapidfuzz import fuzz as rfuzz

# ===== CONFIGURACIÓN =====
VERSION = "v2.5.1"
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 814338625
ADMIN_USERNAME = "TuUsuarioAqui"  # Cambia por tu usuario (sin @)

if not TOKEN:
    logger.error("❌ FATAL: La variable de entorno BOT_TOKEN no está configurada.")

MAX_LINEAS_CATALOGO = 80
MAX_CATALOGOS_PROVEEDOR = 2
DIAS_EXPIRACION_ADMIN = 10
UMBRAL_FUZZY = 70

BLACKLIST = ["zapatos", "ropa", "joyas", "comida", "pollo", "arroz", "telefono", "casa", "carro", "zapatillas", "frutas", "viveres"]

# ===== CONEXIÓN A MONGODB =====
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    logger.error("❌ FATAL: La variable de entorno MONGODB_URI no está configurada.")

client = AsyncIOMotorClient(MONGODB_URI)
db = client.medicubadb

coleccion_clientes = db.clientes
coleccion_proveedores = db.proveedores
coleccion_catalogos = db.catalogos

PROVINCIAS = [
    "Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas",
    "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila",
    "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba",
    "Guantánamo", "Isla de la Juventud"
]

# ===== CONFIGURACIÓN LOCAL (Admins y Anuncios) =====
ARCHIVO_CONFIG = "config_bot.json"

def cargar_config():
    """Carga configuración local (admins y anuncios)"""
    config_por_defecto = {
        "administradores": [ADMIN_ID],
        "anuncios": []
    }
    if os.path.exists(ARCHIVO_CONFIG):
        try:
            with open(ARCHIVO_CONFIG, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for key in config_por_defecto:
                    if key not in data:
                        data[key] = config_por_defecto[key]
                return data
        except (json.JSONDecodeError, IOError):
            return config_por_defecto
    return config_por_defecto

def guardar_config(config):
    """Guarda configuración local"""
    with open(ARCHIVO_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# Cargar configuración al iniciar
datos = cargar_config()

# ===== FUNCIONES AUXILIARES =====
def esc(texto):
    if texto is None: return ""
    return html.escape(str(texto))

def normalizar_texto(texto):
    texto = texto.lower()
    acentos = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ü': 'u'}
    for a, b in acentos.items(): texto = texto.replace(a, b)
    texto = re.sub(r'[^\w\s]', ' ', texto)
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()

def es_admin(user_id):
    admins = datos.get("administradores", [ADMIN_ID])
    return int(user_id) in admins

async def es_destacado_activo(proveedor):
    if not proveedor or not proveedor.get("destacado_hasta"): return False
    try: return datetime.now() < proveedor["destacado_hasta"]
    except: return False

def contiene_productos_no_medicos(texto):
    texto_norm = normalizar_texto(texto)
    for palabra in BLACKLIST:
        patron = r'\b' + re.escape(palabra) + r'\b'
        if re.search(patron, texto_norm): return True
    return False

def generar_hash(texto):
    return hashlib.md5(texto.encode('utf-8')).hexdigest()

async def limpiar_expirados():
    ahora = datetime.now()
    resultado = await coleccion_catalogos.delete_many({"es_admin": True, "fecha_expiracion": {"$lt": agora}})
    if resultado.deleted_count > 0:
        logger.info(f"🗑️ Purgados {resultado.deleted_count} listados expirados.")

def get_anuncio_actual():
    """Obtiene el anuncio rotativo actual (cambia cada 4 horas)"""
    anuncios = datos.get("anuncios", [])
    if not anuncios: return ""
    index = (datetime.now().hour // 4) % len(anuncios)
    return f"📢 <b>AVISO:</b> {anuncios[index]}\n\n"

def generar_menu_principal(user_id, provincia):
    anuncio = get_anuncio_actual()
    
    teclado = [
        [InlineKeyboardButton("🔍 Buscar Medicina", callback_data="buscar")],
        [InlineKeyboardButton("📝 Publicar Catálogo", callback_data="publicar")],
        [InlineKeyboardButton("📍 Cambiar Provincia", callback_data="cambiar_provincia")],
        [InlineKeyboardButton("👤 Mi Perfil", callback_data="mi_perfil")],
        [InlineKeyboardButton("⭐ Proveedores Destacados", callback_data="destacados")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ]
    if es_admin(user_id):
        teclado.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    
    texto = (f"{anuncio}"
             f"🏥 <b>MediCuba</b>\n🩺 Tu salud, nuestra prioridad\n\n"
             f"📍 <b>Tu provincia:</b> {esc(provincia)}\n\n"
             f"🔗 <code>t.me/MediCubaBot</code> (Comparte el bot)\n\n"
             f"¿Qué deseas hacer?\n\n"
             f"<i>MediCuba {VERSION}</i>")
    return texto, InlineKeyboardMarkup(teclado)

async def enviar_menu_callback(query, user_id):
    provincia_doc = await coleccion_clientes.find_one({"_id": user_id})
    provincia = provincia_doc.get("provincia", "No seleccionada") if provincia_doc else "No seleccionada"
    texto, teclado = generar_menu_principal(user_id, provincia)
    try: await query.edit_message_text(texto, reply_markup=teclado, parse_mode="HTML")
    except: await query.message.reply_text(texto, reply_markup=teclado, parse_mode="HTML")

async def enviar_menu_mensaje(update, user_id):
    provincia_doc = await coleccion_clientes.find_one({"_id": user_id})
    provincia = provincia_doc.get("provincia", "No seleccionada") if provincia_doc else "No seleccionada"
    texto, teclado = generar_menu_principal(user_id, provincia)
    await update.message.reply_text(texto, reply_markup=teclado, parse_mode="HTML")

# ===== COMANDOS BÁSICOS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    try:
        if context.args and context.args[0].startswith("proveedor_"):
            prov_id = context.args[0].replace("proveedor_", "")
            await mostrar_catalogo_proveedor_msg(update, prov_id)
            return
        await coleccion_clientes.update_one({"_id": user_id}, {"$setOnInsert": {"provincia": None, "busquedas": 0}}, upsert=True)
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        if not cliente.get("provincia"):
            return await forzar_provincia(update, context)
        await enviar_menu_mensaje(update, user_id)
    except Exception as e:
        logger.error(f"❌ Error en /start: {e}")
        await update.message.reply_text("⚠️ Error de conexión. Intenta de nuevo.")

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["estado"] = None
    user_id = str(update.effective_user.id)
    await update.message.reply_text("↩️ Cancelado.", reply_markup=ReplyKeyboardRemove())
    await enviar_menu_mensaje(update, user_id)

async def forzar_provincia(update, context):
    context.user_data["estado"] = "cambiando_provincia"
    lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
    teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)
    await update.message.reply_text(f"👋 ¡Bienvenido a MediCuba!\n\n📍 Selecciona tu provincia:\n\n{lista}\n\nResponde con el NÚMERO:", reply_markup=teclado_volver)

async def mostrar_catalogo_proveedor_msg(update, proveedor_id):
    proveedor = await coleccion_proveedores.find_one({"_id": proveedor_id})
    if not proveedor: return await update.message.reply_text("❌ Proveedor no encontrado.")
    catalogos = await coleccion_catalogos.find({"proveedor_id": proveedor_id, "es_admin": False}).to_list(None)
    if not catalogos: return await update.message.reply_text("📭 Este proveedor no tiene catálogos activos.")
    mensaje = f"🏥 <b>{esc(proveedor.get('nombre'))}</b>\n"
    if await es_destacado_activo(proveedor): mensaje += "⭐ <b>Proveedor Destacado</b> ⭐\n"
    mensaje += f"📞 {esc(proveedor.get('contacto_mostrar', 'No especificado'))}\n" + "─"*20 + "\n\n"
    for idx, cat in enumerate(catalogos, 1):
        mensaje += f"<b>📋 Catálogo {idx}:</b>\n"
        for linea in cat["lineas_originales"][:30]: mensaje += f"• {esc(linea)}\n"
        if len(cat["lineas_originales"]) > 30: mensaje += f"... y {len(cat['lineas_originales'])-30} más.\n"
        mensaje += "\n"
    mensaje += "─"*20 + "\n🩺 <b>MediCuba</b>"
    teclado = [[InlineKeyboardButton("🏠 Ir al Bot", callback_data="volver")]]
    await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

# ===== HANDLER ÚNICO DE CALLBACKS =====
async def manejador_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data = query.data
    teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)

    if data == "volver": await enviar_menu_callback(query, user_id)      
    elif data == "buscar":
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        if not cliente or not cliente.get("provincia"):
            return await query.edit_message_text("❌ Primero configura tu provincia.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📍 Configurar", callback_data="cambiar_provincia")]]))
        context.user_data["estado"] = "esperando_medicina"
        await query.edit_message_text("🔍 <b>Buscar Medicina</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=user_id, text="Escribe el nombre (acepta errores):\n\n<i>Ej: gravinol</i>", reply_markup=teclado_volver, parse_mode="HTML")  
    elif data == "publicar":
        context.user_data["estado"] = "esperando_listado"
        await query.edit_message_text("📝 <b>Publicar Catálogo</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=user_id, text="📋 Pega tu listado.\n\n⚠️ Máximo 80 líneas. Puedes tener 2 catálogos (el 3ro borra el 1ro). Solo medicinas.", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"], ["❌ Cancelar"]], resize_keyboard=True), parse_mode="HTML")   
    elif data == "cambiar_provincia":
        context.user_data["estado"] = "cambiando_provincia"
        lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
        await query.edit_message_text("📍 <b>Cambiar Provincia</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=user_id, text=f"{lista}\n\nResponde con el NÚMERO:", reply_markup=teclado_volver)    
    elif data == "mi_perfil": await _mostrar_perfil(query, user_id)     
    elif data == "editar_contacto":
        context.user_data["editando_contacto"] = True
        teclado = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")], [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")], [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")], [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]
        await query.edit_message_text("✏️ <b>Editar Contacto</b>", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")   
    elif data == "ver_mi_catalogo": await _mostrar_mi_catalogo(query, user_id)     
    elif data == "destacados": await _mostrar_destacados(query)     
    elif data == "ayuda": await _mostrar_ayuda(query)     
    elif data.startswith("ayuda_"): await _mostrar_ayuda_detalle(query, data)     
    elif data.startswith("contacto_"): await _procesar_contacto_callback(query, context, data)     
    elif data == "admin_panel":
        if es_admin(user_id): await _admin_panel(query)     
    elif data.startswith("admin_"): 
        if es_admin(user_id): await _admin_acciones(query, context, user_id, data)

# ===== FUNCIONES DE VISTAS =====
async def _mostrar_perfil(query, user_id):
    proveedor = await coleccion_proveedores.find_one({"_id": user_id})
    if proveedor:
        cat_count = await coleccion_catalogos.count_documents({"proveedor_id": user_id, "es_admin": False})
        mensaje = f"👤 <b>Perfil Proveedor</b>\n\n📛 {esc(proveedor.get('nombre'))}\n📞 {esc(proveedor.get('contacto_mostrar'))}\n📋 Catálogos: {cat_count}/2\n"
        if proveedor.get('link_token'): mensaje += f"🔗 <code>t.me/MediCubaBot?start=proveedor_{user_id}</code>\n"
        teclado = [[InlineKeyboardButton("✏️ Editar Contacto", callback_data="editar_contacto")], [InlineKeyboardButton("📋 Ver Catálogo", callback_data="ver_mi_catalogo")], [InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    else:
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        mensaje = f"👤 <b>Perfil Cliente</b>\n\n📍 {esc(cliente.get('provincia', 'N/A'))}\n📊 Búsquedas: {cliente.get('busquedas', 0)}\n"
        teclado = [[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _mostrar_mi_catalogo(query, user_id):
    catalogos = await coleccion_catalogos.find({"proveedor_id": user_id, "es_admin": False}).sort("fecha_creacion", 1).to_list(None)
    if not catalogos: 
        return await query.edit_message_text("📭 No tienes catálogos.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Publicar", callback_data="publicar")], [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]), parse_mode="HTML")
    mensaje = f"📋 <b>Tus Catálogos</b> ({len(catalogos)}/2)\n\n"
    for idx, cat in enumerate(catalogos, 1):
        mensaje += f"<b>Catálogo {idx}:</b>\n"
        for linea in cat["lineas_originales"][:20]: mensaje += f"• {esc(linea)}\n"
        if len(cat["lineas_originales"]) > 20: mensaje += f"... y {len(cat['lineas_originales'])-20} más.\n"
        mensaje += "\n"
    teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _mostrar_destacados(query):
    await limpiar_expirados()
    proveedores = await coleccion_proveedores.find({"destacado_hasta": {"$gt": datetime.now()}}).to_list(None)
    if not proveedores: 
        return await query.edit_message_text("⭐ No hay destacados actualmente.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]), parse_mode="HTML")
    mensaje = "⭐ <b>PROVEEDORES DESTACADOS</b> ⭐\n\nLos mejores y más confiables:\n\n"
    for p in proveedores[:5]:
        link = f"t.me/MediCubaBot?start=proveedor_{p['_id']}"
        mensaje += f"🏥 <b>{esc(p.get('nombre'))}</b>\n📞 {esc(p.get('contacto_mostrar'))}\n🔗 <a href='{link}'>Ver catálogo</a>\n\n"
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]), parse_mode="HTML", disable_web_page_preview=True)

async def _mostrar_ayuda(query):
    teclado = [
        [InlineKeyboardButton("👨‍💼 Proveedores", callback_data="ayuda_prov")], 
        [InlineKeyboardButton("🛒 Clientes", callback_data="ayuda_cli")], 
        [InlineKeyboardButton("⚙️ General", callback_data="ayuda_gen")],
        [InlineKeyboardButton("💬 Contactar Admin", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton("🏠 Volver", callback_data="volver")]
    ]
    await query.edit_message_text("❓ <b>Centro de Ayuda</b>", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _mostrar_ayuda_detalle(query, data):
    link = "\n\n🔗 Comparte el bot y salva vidas: <code>t.me/MediCubaBot</code>"
    textos = {
        "ayuda_prov": (
            "👨‍💼 <b>Para Proveedores</b>\n\n"
            "¿Vendes medicinas? Este es tu lugar. Sube hasta 2 listados de medicamentos (80 líneas cada uno) con un simple copiar y pegar.\n\n"
            "⭐ <b>Sistema de Estrellas:</b> Los proveedores destacados aparecen PRIMERO en las búsquedas y llevan la insignia ⭐. "
            "Esto genera 3x más contactos. ¡Pregunta al Admin cómo destacar tu catálogo!\n\n"
            "📞 Tú eliges cómo te contactan: WhatsApp, Telegram o ambos. Recibirás mensajes pre-escritos listos para responder."
            f"{link}"
        ), 
        "ayuda_cli": (
            "🛒 <b>Para Clientes</b>\n\n"
            "Encontrar tu medicina nunca fue tan fácil. Nuestro buscador es inteligente: si escribes 'parasetamol' o 'gravinor', él entiende a qué te refieres.\n\n"
            "⭐ <b>Proveedores Destacados:</b> En tus resultados, los proveedores ⭐ son los más confiables y rápidos. "
            "Identificarlos es tu garantía de un mejor servicio.\n\n"
            "📱 Un solo clic en 'Contactar' abrirá WhatsApp con el mensaje listo para enviar."
            f"{link}"
        ), 
        "ayuda_gen": (
            "⚙️ <b>General</b>\n\n"
            "🩺 <b>MediCuba</b> conecta a quien necesita medicinas con quien las tiene, directo y sin intermediarios.\n\n"
            "Tu provincia se configura una vez y tus búsquedas serán siempre locales. "
            "Si viajas, cámbiala en un clic desde el menú."
            f"{link}"
        )
    }
    teclado = [
        [InlineKeyboardButton("💬 Consultar al Admin", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton("🔙 Atrás", callback_data="ayuda")]
    ]
    await query.edit_message_text(textos.get(data, ""), reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _procesar_contacto_callback(query, context, data):
    tipo = data.replace("contacto_", "")
    context.user_data["tipo_contacto"] = tipo
    teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)
    if tipo in ["whatsapp", "ambos"]:
        context.user_data["estado"] = "esperando_telefono"
        await query.edit_message_text("📱 <b>Contacto WhatsApp</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=query.from_user.id, text="Escribe tu número (ej: <code>+53 5 1234567</code>):", reply_markup=teclado_volver, parse_mode="HTML")
    else:
        context.user_data["estado"] = "esperando_telegram"
        await query.edit_message_text("✈️ <b>Contacto Telegram</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=query.from_user.id, text="Escribe tu @usuario:", reply_markup=teclado_volver)

async def _admin_panel(query):
    teclado = [
        [InlineKeyboardButton("📥 Cargar Listado", callback_data="admin_cargar")], 
        [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_stats")], 
        [InlineKeyboardButton("👥 Proveedores", callback_data="admin_provs")], 
        [InlineKeyboardButton("⭐ Destacar", callback_data="admin_dest")],
        [InlineKeyboardButton("📢 Config Anuncios", callback_data="admin_anuncios")],
        [InlineKeyboardButton("🏠 Volver", callback_data="volver")]
    ]
    await query.edit_message_text("🔧 <b>Panel Admin</b>", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _admin_acciones(query, context, user_id, data):
    if data == "admin_cargar":
        context.user_data["estado"] = "admin_esperando_telefono_listado"
        await query.edit_message_text("📥 <b>Cargar Listado Admin</b>", parse_mode="HTML")
        teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)
        await context.bot.send_message(chat_id=user_id, text="Envía el WhatsApp para este listado (ej: <code>+5351234567</code>):", reply_markup=teclado_volver, parse_mode="HTML")
    elif data == "admin_stats": await _admin_stats(query)
    elif data == "admin_provs": await _admin_provs(query)
    elif data == "admin_dest": await _admin_dest(query)
    elif data == "admin_anuncios": await _admin_anuncios(query)

async def _admin_stats(query):
    await limpiar_expirados()
    c_prov = await coleccion_proveedores.count_documents({})
    c_cli = await coleccion_clientes.count_documents({})
    c_cat = await coleccion_catalogos.count_documents({})
    c_adm = len(datos.get("administradores", []))
    mensaje = f"📊 <b>Estadísticas</b>\n\n👥 Clientes: {c_cli}\n🏥 Proveedores: {c_prov}\n📋 Catálogos: {c_cat}\n🛡️ Admins: {c_adm}"
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_provs(query):
    provs = await coleccion_proveedores.find({}).to_list(None)
    mensaje = "👥 <b>Proveedores</b>\n\n"
    for p in provs[:10]:
        c_cat = await coleccion_catalogos.count_documents({"proveedor_id": p["_id"]})
        mensaje += f"• <code>{p['_id']}</code> - {esc(p.get('nombre'))} ({c_cat} cat.)\n"
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_dest(query):
    provs = await coleccion_proveedores.find({}).to_list(None)
    mensaje = "⭐ <b>Destacar</b>\n\nUsa: <code>/destacar ID DIAS</code>\n\n"
    for p in provs[:15]: mensaje += f"• <code>{p['_id']}</code> - {esc(p.get('nombre'))}\n"
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_anuncios(query):
    anuncios = datos.get("anuncios", [])
    mensaje = "📢 <b>Configurar Anuncios</b>\n\nAnuncios actuales (rotan cada 4h):\n\n"
    if not anuncios:
        mensaje += "<i>Vacío</i>\n"
    else:
        for i, a in enumerate(anuncios, 1): mensaje += f"{i}. {esc(a)}\n"
    mensaje += "\nComandos:\n/anuncio add <code>texto</code>\n/anuncio del <code>numero</code>"
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]), parse_mode="HTML")

# ===== HANDLER ÚNICO DE MENSAJES =====
async def procesar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    texto = update.message.text

    if texto in ["🔙 Volver al Menú", "❌ Cancelar", "/cancelar", "/cancel"]:
        context.user_data["estado"] = None
        await update.message.reply_text("↩️ Cancelado.", reply_markup=ReplyKeyboardRemove())
        return await enviar_menu_mensaje(update, user_id)

    estado = context.user_data.get("estado")

    if estado is None:
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        if cliente and cliente.get("provincia"):
            estado = "esperando_medicina"
            context.user_data["estado"] = estado
        else:
            await update.message.reply_text("⚠️ Usa /start para comenzar.")
            return

    try:
        if estado == "esperando_medicina": await _busqueda(update, context, user_id, texto)
        elif estado == "esperando_listado": await _listado(update, context, user_id, texto)
        elif estado == "cambiando_provincia": await _cambio_provincia(update, context, user_id, texto)
        elif estado == "esperando_telefono": await _telefono(update, context, user_id, texto)
        elif estado == "esperando_telegram": await _telegram_user(update, context, user_id, texto)
        elif estado == "admin_esperando_telefono_listado": await _admin_esperando_telefono(update, context, user_id, texto)
        elif estado == "admin_esperando_listado": await _admin_listado(update, context, user_id, texto)
        elif estado == "esperando_seleccion": await _seleccion_sugerencia(update, context, user_id, texto)
        else:
            context.user_data["estado"] = None
            await enviar_menu_mensaje(update, user_id)
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        context.user_data["estado"] = None
        await update.message.reply_text("⚠️ Error. Volviendo al menú.", reply_markup=ReplyKeyboardRemove())
        await enviar_menu_mensaje(update, user_id)

async def _cambio_provincia(update, context, user_id, texto):
    try:
        num = int(texto.strip())
        if 1 <= num <= len(PROVINCIAS):
            prov = PROVINCIAS[num-1]
            await coleccion_clientes.update_one({"_id": user_id}, {"$set": {"provincia": prov}})
            await update.message.reply_text(f"✅ Provincia: <b>{esc(prov)}</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
            context.user_data["estado"] = None
            return await enviar_menu_mensaje(update, user_id)
        else: raise ValueError
    except ValueError:
        await update.message.reply_text("Número inválido. Intenta de nuevo.")

async def _busqueda(update, context, user_id, texto):
    medicina_buscar = normalizar_texto(texto)
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    provincia = cliente.get("provincia") if cliente else None
    if not provincia: 
        await update.message.reply_text("❌ Configura provincia primero.", reply_markup=ReplyKeyboardRemove())
        return await enviar_menu_mensaje(update, user_id)
    await coleccion_clientes.update_one({"_id": user_id}, {"$inc": {"busquedas": 1}})
    await limpiar_expirados()
    catalogos_prov = await coleccion_catalogos.find({"provincia": provincia}).to_list(None)
    opciones = []
    for cat in catalogos_prov:
        prov_id = cat["proveedor_id"]
        proveedor = await coleccion_proveedores.find_one({"_id": prov_id})
        if not proveedor: continue
        for i, linea_norm in enumerate(cat["lineas_normalizadas"]):
            score = rfuzz.WRatio(medicina_buscar, linea_norm)
            if score >= UMBRAL_FUZZY:
                opciones.append({"score": score, "linea_original": cat["lineas_originales"][i], "proveedor": proveedor})
    if not opciones:
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Buscar Otra", callback_data="buscar")], [InlineKeyboardButton("🏠 Menú", callback_data="volver")]])
        await update.message.reply_text(f"❌ No hay '{esc(texto)}' en {esc(provincia)}.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        await update.message.reply_text("¿Qué deseas hacer?", reply_markup=teclado)
        context.user_data["estado"] = None
        return
    opciones.sort(key=lambda x: x["score"], reverse=True)
    if len(opciones) <= 5:
        mensaje = f"🔍 <b>{esc(texto.upper())}</b> en {esc(provincia)}\n\n✅ {len(opciones)} resultado(s):\n\n"
        enlace_wa = None
        prov_ya_mostrados = set()
        for op in opciones:
            p = op["proveedor"]
            dest = "⭐ " if await es_destacado_activo(p) else ""
            if p["_id"] not in prov_ya_mostrados:
                mensaje += f"{dest}<b>{esc(p.get('nombre'))}</b>\n📞 {esc(p.get('contacto_mostrar'))}\n"
                prov_ya_mostrados.add(p["_id"])
                contacto = p.get("contacto", {})
                if contacto.get("tipo") in ["whatsapp", "ambos"] and not enlace_wa:
                    tel = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
                    if tel: enlace_wa = f"https://wa.me/{tel}?text=Hola%2C%20te%20contacto%20desde%20MediCuba%20(https%3A%2F%2Ft.me%2FMediCubaBot).%20Tienes%20{texto}%3F"
            mensaje += f"   • {esc(op['linea_original'])}\n"
        botones = []
        if enlace_wa: botones.append([InlineKeyboardButton("📞 Contactar WhatsApp", url=enlace_wa)])
        botones.append([InlineKeyboardButton("🔍 Buscar Otra", callback_data="buscar")])
        botones.append([InlineKeyboardButton("🏠 Menú", callback_data="volver")])
        await update.message.reply_text(mensaje, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        await update.message.reply_text("¿Qué deseas hacer?", reply_markup=InlineKeyboardMarkup(botones))
        context.user_data["estado"] = None
    else:
        mensaje = f"🔍 <b>{esc(texto.upper())}</b> - Sugerencias:\n\n"
        sugerencias = opciones[:10]
        context.user_data["sugerencias"] = sugerencias
        for i, op in enumerate(sugerencias, 1): mensaje += f"{i}. {esc(op['linea_original'])}\n"
        mensaje += "\nResponde el NÚMERO."
        teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)
        await update.message.reply_text(mensaje, reply_markup=teclado_volver, parse_mode="HTML")
        context.user_data["estado"] = "esperando_seleccion"

async def _seleccion_sugerencia(update, context, user_id, texto):
    sugerencias = context.user_data.get("sugerencias", [])
    try:
        num = int(texto.strip())
        if 1 <= num <= len(sugerencias):
            op = sugerencias[num-1]
            p = op["proveedor"]
            dest = "⭐ " if await es_destacado_activo(p) else ""
            mensaje = f"🏥 {dest}<b>{esc(p.get('nombre'))}</b>\n📞 {esc(p.get('contacto_mostrar'))}\n\n💊 {esc(op['linea_original'])}\n"
            enlace_wa = None
            contacto = p.get("contacto", {})
            if contacto.get("tipo") in ["whatsapp", "ambos"]:
                tel = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
                if tel: enlace_wa = f"https://wa.me/{tel}?text=Hola%2C%20te%20contacto%20desde%20MediCuba%20(https%3A%2F%2Ft.me%2FMediCubaBot).%20Tienes%20esto%3F"
            botones = [[InlineKeyboardButton("🔍 Buscar Otra", callback_data="buscar")], [InlineKeyboardButton("🏠 Menú", callback_data="volver")]]
            if enlace_wa: botones.insert(0, [InlineKeyboardButton("📞 WhatsApp", url=enlace_wa)])
            await update.message.reply_text(mensaje, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
            await update.message.reply_text("¿Qué deseas hacer?", reply_markup=InlineKeyboardMarkup(botones))
        else: raise ValueError
    except ValueError:
        await update.message.reply_text("Número inválido o Volver.")
    context.user_data["estado"] = None

async def _listado(update, context, user_id, texto):
    if contiene_productos_no_medicos(texto):
        context.user_data["estado"] = None 
        await update.message.reply_text("❌ <b>Rechazado.</b> Se detectaron productos no médicos.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        return await enviar_menu_mensaje(update, user_id)
    lineas = [l.strip() for l in texto.split('\n') if l.strip()]
    truncado = len(lineas) > MAX_LINEAS_CATALOGO
    lineas = lineas[:MAX_LINEAS_CATALOGO]
    lineas_norm = [normalizar_texto(l) for l in lineas]
    cat_count = await coleccion_catalogos.count_documents({"proveedor_id": user_id, "es_admin": False})
    if cat_count >= MAX_CATALOGOS_PROVEEDOR:
        oldest = await coleccion_catalogos.find_one({"proveedor_id": user_id, "es_admin": False}, sort=[("fecha_creacion", 1)])
        if oldest: await coleccion_catalogos.delete_one({"_id": oldest["_id"]})
    provincia_doc = await coleccion_clientes.find_one({"_id": user_id})
    provincia = provincia_doc.get("provincia", "Santiago de Cuba") if provincia_doc else "Santiago de Cuba"
    await coleccion_proveedores.update_one({"_id": user_id}, {"$set": {"nombre": update.effective_user.first_name or "Proveedor", "provincia": provincia, "link_token": user_id}}, upsert=True)
    await coleccion_catalogos.insert_one({
        "proveedor_id": user_id, "lineas_originales": lineas, "lineas_normalizadas": lineas_norm,
        "es_admin": False, "fecha_creacion": datetime.now(), "fecha_expiracion": None,
        "provincia": provincia, "hash": generar_hash(texto)
    })
    aviso = ""
    if truncado: aviso = f"\n⚠️ Solo se guardaron {MAX_LINEAS_CATALOGO} líneas."
    context.user_data["estado"] = "esperando_contacto"
    context.user_data["medicinas_count"] = len(lineas)
    teclado = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")], [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")], [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")]]
    await update.message.reply_text(f"✅ {len(lineas)} líneas extraídas.{aviso}\n\n¿Cómo te contactarán?", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _telefono(update, context, user_id, texto):
    telefono = texto.strip()
    tipo = context.user_data.get("tipo_contacto", "whatsapp")
    await coleccion_proveedores.update_one({"_id": user_id}, {"$set": {"contacto.tipo": tipo, "contacto.whatsapp": telefono, "contacto_mostrar": telefono, "nombre": update.effective_user.first_name or "Prov"}})
    if tipo == "ambos":
        context.user_data["estado"] = "esperando_telegram"
        teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)
        return await update.message.reply_text("✈️ Ahora tu @usuario de Telegram:", reply_markup=teclado_volver)
    await _finalizar_registro(update, context, user_id)

async def _telegram_user(update, context, user_id, texto):
    tg_user = texto.strip()
    if not tg_user.startswith("@"): tg_user = "@" + tg_user
    tipo = context.user_data.get("tipo_contacto", "telegram")
    proveedor = await coleccion_proveedores.find_one({"_id": user_id})
    wa = proveedor.get("contacto", {}).get("whatsapp", "") if proveedor else ""
    mostrar = tg_user if tipo == "telegram" else f"{wa} / {tg_user}"
    await coleccion_proveedores.update_one({"_id": user_id}, {"$set": {"contacto.telegram": tg_user, "contacto_mostrar": mostrar, "nombre": update.effective_user.first_name or "Prov"}})
    await _finalizar_registro(update, context, user_id)

async def _finalizar_registro(update, context, user_id):
    editando = context.user_data.get("editando_contacto", False)
    proveedor = await coleccion_proveedores.find_one({"_id": user_id})
    if editando:
        mensaje = f"✅ Contacto actualizado:\n📞 {esc(proveedor.get('contacto_mostrar'))}"
        context.user_data["editando_contacto"] = False
    else:
        link = f"t.me/MediCubaBot?start=proveedor_{user_id}"
        mensaje = f"✅ <b>¡Catálogo publicado!</b>\n\n📋 Líneas: {context.user_data.get('medicinas_count', 0)}\n📞 Contacto: {esc(proveedor.get('contacto_mostrar'))}\n\n🔗 <b>Tu link:</b>\n<code>{link}</code>"
    teclado = [[InlineKeyboardButton("🏠 Menú", callback_data="volver")]]
    await update.message.reply_text(mensaje, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await update.message.reply_text("¿Qué deseas hacer?", reply_markup=InlineKeyboardMarkup(teclado))
    context.user_data["estado"] = None

# ===== COMANDOS ADMIN =====
async def admin_cargar_listado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not es_admin(user_id): return
    if not context.args: return await update.message.reply_text("Uso: <code>/admin_cargar_listado +5351234567</code>", parse_mode="HTML")
    context.user_data["admin_telefono"] = context.args[0]
    context.user_data["estado"] = "admin_esperando_listado"
    teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"], ["❌ Cancelar"]], resize_keyboard=True)
    await update.message.reply_text("📥 Teléfono guardado. Pega el listado:", reply_markup=teclado_volver, parse_mode="HTML")

async def _admin_esperando_telefono(update, context, user_id, texto):
    telefono = texto.strip()
    context.user_data["admin_telefono"] = telefono
    context.user_data["estado"] = "admin_esperando_listado"
    teclado_volver = ReplyKeyboardMarkup([["🔙 Volver al Menú"], ["❌ Cancelar"]], resize_keyboard=True)
    await update.message.reply_text(f"✅ Teléfono: <code>{esc(telefono)}</code>\n\nPega el listado:", reply_markup=teclado_volver, parse_mode="HTML")

async def _admin_listado(update, context, user_id, texto):
    if not es_admin(user_id): return
    telefono = context.user_data.get("admin_telefono")
    if not telefono: 
        context.user_data["estado"] = None 
        await update.message.reply_text("❌ Falta teléfono.", reply_markup=ReplyKeyboardRemove())
        return await enviar_menu_mensaje(update, user_id)
    hash_txt = generar_hash(texto)
    if await coleccion_catalogos.find_one({"hash": hash_txt}):
        context.user_data["estado"] = None 
        await update.message.reply_text("⚠️ <b>Duplicado.</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        return await enviar_menu_mensaje(update, user_id)
    if contiene_productos_no_medicos(texto):
        context.user_data["estado"] = None 
        await update.message.reply_text("❌ Productos no médicos.", reply_markup=ReplyKeyboardRemove())
        return await enviar_menu_mensaje(update, user_id)
    lineas = [l.strip() for l in texto.split('\n') if l.strip()][:MAX_LINEAS_CATALOGO]
    lineas_norm = [normalizar_texto(l) for l in lineas]
    admin_prov_id = f"admin_{user_id}_{int(datetime.now().timestamp())}"
    provincia = "Santiago de Cuba"
    await coleccion_proveedores.update_one({"_id": admin_prov_id}, {"$set": {"nombre": "Admin MediCuba", "contacto": {"tipo": "whatsapp", "whatsapp": telefono}, "contacto_mostrar": telefono, "provincia": provincia}}, upsert=True)
    expiracion = datetime.now() + timedelta(days=DIAS_EXPIRACION_ADMIN)
    await coleccion_catalogos.insert_one({
        "proveedor_id": admin_prov_id, "lineas_originales": lineas, "lineas_normalizadas": lineas_norm,
        "es_admin": True, "fecha_creacion": datetime.now(), "fecha_expiracion": expiracion,
        "provincia": provincia, "hash": hash_txt
    })
    await update.message.reply_text(f"✅ <b>Listado Cargado</b>\n\n📊 {len(lineas)} líneas.\n📞 {esc(telefono)}", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await enviar_menu_mensaje(update, user_id)
    context.user_data["estado"] = None
    context.user_data["admin_telefono"] = None

async def destacar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not es_admin(user_id): return
    if not context.args or len(context.args) < 2: return await update.message.reply_text("Uso: <code>/destacar ID DIAS</code>", parse_mode="HTML")
    prov_id = context.args[0]
    try: dias = int(context.args[1])
    except: return await update.message.reply_text("❌ Días inválidos.")
    if not await coleccion_proveedores.find_one({"_id": prov_id}): return await update.message.reply_text("❌ Proveedor no encontrado.")
    fecha_fin = datetime.now() + timedelta(days=dias)
    await coleccion_proveedores.update_one({"_id": prov_id}, {"$set": {"destacado_hasta": fecha_fin}})
    await update.message.reply_text(f"✅ Destacado por {dias} días.", parse_mode="HTML")

async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Uso: <code>/addadmin TELEGRAM_ID</code>", parse_mode="HTML")
    try: new_admin = int(context.args[0])
    except: return await update.message.reply_text("ID debe ser un número.")
    if new_admin not in datos.get("administradores", [ADMIN_ID]):
        datos.setdefault("administradores", [ADMIN_ID]).append(new_admin)
        guardar_config(datos)
        await update.message.reply_text(f"✅ Admin {new_admin} agregado.")
    else:
        await update.message.reply_text("Ya es admin.")

async def del_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Uso: <code>/deladmin TELEGRAM_ID</code>", parse_mode="HTML")
    try: del_admin = int(context.args[0])
    except: return await update.message.reply_text("ID debe ser un número.")
    if del_admin == ADMIN_ID: return await update.message.reply_text("No puedes eliminarte a ti mismo.")
    if del_admin in datos.get("administradores", []):
        datos["administradores"].remove(del_admin)
        guardar_config(datos)
        await update.message.reply_text(f"✅ Admin {del_admin} eliminado.")
    else:
        await update.message.reply_text("No estaba en la lista.")

async def anuncio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not es_admin(user_id): return
    if not context.args or len(context.args) < 2: 
        return await update.message.reply_text("Uso:\n<code>/anuncio add Texto del anuncio</code>\n<code>/anuncio del 1</code>", parse_mode="HTML")
    
    accion = context.args[0].lower()
    texto_anuncio = " ".join(context.args[1:])
    
    datos.setdefault("anuncios", [])
    if accion == "add":
        datos["anuncios"].append(texto_anuncio)
        guardar_config(datos)
        await update.message.reply_text("✅ Anuncio añadido.")
    elif accion == "del":
        try:
            idx = int(texto_anuncio) - 1
            datos["anuncios"].pop(idx)
            guardar_config(datos)
            await update.message.reply_text("✅ Anuncio eliminado.")
        except:
            await update.message.reply_text("Número inválido.")

# ===== SERVIDOR HEALTH CHECK =====
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, format, *args): pass

def iniciar_health_check():
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

# ===== MAIN =====
async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    await coleccion_catalogos.create_index([("provincia", 1)])
    await coleccion_catalogos.create_index([("proveedor_id", 1)])

def main():
    if not TOKEN or not MONGODB_URI:
        logger.error("🛑 BOT DETENIDO: Faltan variables.")
        return
    iniciar_health_check()
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancelar", cancelar))
    application.add_handler(CommandHandler("cancel", cancelar))
    application.add_handler(CommandHandler("admin_cargar_listado", admin_cargar_listado))
    application.add_handler(CommandHandler("destacar", destacar_cmd))
    application.add_handler(CommandHandler("addadmin", add_admin_cmd))
    application.add_handler(CommandHandler("deladmin", del_admin_cmd))
    application.add_handler(CommandHandler("anuncio", anuncio_cmd))
    
    application.add_handler(CallbackQueryHandler(manejador_callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensajes))
    
    logger.info(f"🤖 MediCuba {VERSION} iniciado...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
