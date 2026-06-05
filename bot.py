"""
BOT DE MEDICINAS CUBA - VERSIÓN PROFESIONAL MONGODB
100% por botones | Fuzzy Matching | 2 Catálogos | Auto-purga
Compatible con Python 3.14 y python-telegram-bot v21+
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient
from rapidfuzz import fuzz as rfuzz

# ===== CONFIGURACIÓN =====
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "8685939368:AAESfgUVeQG0qA8521Qx5LO_7Qm3LY27Qq0")
ADMIN_ID = 814338625

# Constantes del Bot
MAX_LINEAS_CATALOGO = 80
MAX_CATALOGOS_PROVEEDOR = 2
DIAS_EXPIRACION_ADMIN = 10
UMBRAL_FUZZY = 70

# Blacklist de productos no médicos
BLACKLIST = ["zapatos", "ropa", "joyas", "comida", "pollo", "arroz", "telefono", "casa", "carro", "zapatillas", "frutas", "viveres"]

# ===== CONEXIÓN A MONGODB =====
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    logger.error("❌ ERROR: La variable de entorno MONGODB_URI no está configurada.")

client = AsyncIOMotorClient(MONGODB_URI)
db = client.medicubadb

coleccion_clientes = db.clientes
coleccion_proveedores = db.proveedores
coleccion_catalogos = db.catalogos

# Lista estática de provincias
PROVINCIAS = [
    "Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas",
    "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila",
    "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba",
    "Guantánamo", "Isla de la Juventud"
]

# ===== FUNCIONES AUXILIARES =====

def esc(texto):
    if texto is None: return ""
    return html.escape(str(texto))

def normalizar_texto(texto):
    texto = texto.lower()
    acentos = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ü': 'u'}
    for a, b in acentos.items(): texto = texto.replace(a, b)
    return texto

def es_admin(user_id):
    return int(user_id) in [ADMIN_ID]

async def es_destacado_activo(proveedor):
    if not proveedor or not proveedor.get("destacado_hasta"): return False
    try: return datetime.now() < proveedor["destacado_hasta"]
    except: return False

def contiene_productos_no_medicos(texto):
    texto_norm = normalizar_texto(texto)
    for palabra in BLACKLIST:
        if palabra in texto_norm:
            return True
    return False

def generar_hash(texto):
    return hashlib.md5(texto.encode('utf-8')).hexdigest()

async def limpiar_expirados():
    """Auto-purga de catálogos de admin expirados"""
    ahora = datetime.now()
    resultado = await coleccion_catalogos.delete_many({"es_admin": True, "fecha_expiracion": {"$lt": ahora}})
    if resultado.deleted_count > 0:
        logger.info(f"🗑️ Purgados {resultado.deleted_count} listados de admin expirados.")

def generar_menu_principal(user_id, provincia):
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
        
    texto = (f"🏥 <b>MediCuba</b>\n🩺 Tu salud, nuestra prioridad\n\n"
             f"📍 <b>Tu provincia:</b> {esc(provincia)}\n\n¿Qué deseas hacer?")
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


# ===== COMANDO /START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Deep link de proveedor
    if context.args and context.args[0].startswith("proveedor_"):
        prov_id = context.args[0].replace("proveedor_", "")
        await mostrar_catalogo_proveedor_msg(update, prov_id)
        return

    # Registrar o cargar cliente
    await coleccion_clientes.update_one({"_id": user_id}, {"$setOnInsert": {"provincia": None, "busquedas": 0}}, upsert=True)
    
    # Forzar selección de provincia si no la tiene
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    if not cliente.get("provincia"):
        return await forzar_provincia(update)

    await enviar_menu_mensaje(update, user_id)

async def forzar_provincia(update):
    lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
    await update.message.reply_text(f"👋 ¡Bienvenido a MediCuba!\n\n📍 Primero debes seleccionar tu provincia:\n\n{lista}\n\nResponde con el NÚMERO:")

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

    if data == "volver":
        await enviar_menu_callback(query, user_id)
        
    elif data == "buscar":
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        if not cliente or not cliente.get("provincia"):
            return await query.edit_message_text("❌ Primero configura tu provincia.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📍 Configurar", callback_data="cambiar_provincia")]]))
        context.user_data["estado"] = "esperando_medicina"
        await query.edit_message_text("🔍 <b>Buscar Medicina</b>\n\nEscribe el nombre (acepta errores ortográficos):\n\n<i>Ej:</i> <code>parasetamol</code>", parse_mode="HTML")
        
    elif data == "publicar":
        context.user_data["estado"] = "esperando_listado"
        await query.edit_message_text("📝 <b>Publicar Catálogo</b>\n\n📋 Pega aquí tu listado.\n\n⚠️ <b>Reglas:</b>\n• Máximo 80 líneas por catálogo.\n• Puedes tener hasta <b>2 catálogos activos</b>.\n• Si subes un 3ro, el más antiguo se elimina.\n• Solo medicinas (sin ropa, comida, etc).", parse_mode="HTML")
        
    elif data == "cambiar_provincia":
        context.user_data["estado"] = "cambiando_provincia"
        lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
        await query.edit_message_text(f"📍 <b>Cambiar Provincia</b>\n\n{lista}\n\nResponde con el NÚMERO:", parse_mode="HTML")
        
    elif data == "mi_perfil": 
        await _mostrar_perfil(query, user_id)
        
    elif data == "editar_contacto":
        context.user_data["editando_contacto"] = True
        teclado = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")], [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")], [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")], [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]
        await query.edit_message_text("✏️ <b>Editar Contacto</b>\n\n¿Cómo prefieres que te contacten?", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")
        
    elif data == "ver_mi_catalogo": 
        await _mostrar_mi_catalogo(query, user_id)
        
    elif data == "destacados": 
        await _mostrar_destacados(query)
        
    elif data == "ayuda": 
        await _mostrar_ayuda(query)
        
    elif data.startswith("ayuda_"): 
        await _mostrar_ayuda_detalle(query, data)
        
    elif data.startswith("contacto_"): 
        await _procesar_contacto_callback(query, context, data)
        
    elif data == "admin_panel":
        if es_admin(user_id): await _admin_panel(query)
        
    elif data.startswith("admin_"): 
        if es_admin(user_id): await _admin_acciones(query, data)
        
    else: logger.warning(f"Callback desconocido: {data}")


# ===== FUNCIONES DE VISTAS =====
async def _mostrar_perfil(query, user_id):
    proveedor = await coleccion_proveedores.find_one({"_id": user_id})
    if proveedor:
        cat_count = await coleccion_catalogos.count_documents({"proveedor_id": user_id, "es_admin": False})
        mensaje = f"👤 <b>Mi Perfil (Proveedor)</b>\n\n📛 {esc(proveedor.get('nombre'))}\n📞 {esc(proveedor.get('contacto_mostrar'))}\n📋 Catálogos activos: {cat_count}/2\n"
        if proveedor.get('link_token'): mensaje += f"🔗 Link: <code>t.me/MediCubaBot?start=proveedor_{user_id}</code>\n"
        teclado = [[InlineKeyboardButton("✏️ Editar Contacto", callback_data="editar_contacto")], [InlineKeyboardButton("📋 Ver Mi Catálogo", callback_data="ver_mi_catalogo")], [InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    else:
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        mensaje = f"👤 <b>Mi Perfil (Cliente)</b>\n\n📍 {esc(cliente.get('provincia', 'N/A'))}\n📊 Búsquedas: {cliente.get('busquedas', 0)}\n"
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
    mensaje = "⭐ <b>PROVEEDORES DESTACADOS</b> ⭐\n\n"
    for p in proveedores[:5]:
        link = f"t.me/MediCubaBot?start=proveedor_{p['_id']}"
        mensaje += f"🏥 <b>{esc(p.get('nombre'))}</b>\n📞 {esc(p.get('contacto_mostrar'))}\n🔗 <a href='{link}'>Ver catálogo</a>\n\n"
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]), parse_mode="HTML", disable_web_page_preview=True)

async def _mostrar_ayuda(query):
    teclado = [[InlineKeyboardButton("👨‍💼 Proveedores", callback_data="ayuda_prov")], [InlineKeyboardButton("🛒 Clientes", callback_data="ayuda_cli")], [InlineKeyboardButton("⚙️ General", callback_data="ayuda_gen")], [InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    await query.edit_message_text("❓ <b>Centro de Ayuda</b>", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _mostrar_ayuda_detalle(query, data):
    textos = {
        "ayuda_prov": "👨‍💼 <b>Proveedores</b>\n\nPuedes tener hasta 2 catálogos (80 líneas máximo c/u).\nSi subes un 3ro, el más antiguo se borra.\nUsa el botón Publicar Catálogo.", 
        "ayuda_cli": "🛒 <b>Clientes</b>\n\nLa búsqueda es inteligente, acepta errores.\nSi no encuentra exacto, te sugiere similares.", 
        "ayuda_gen": "⚙️ <b>General</b>\n\nBot para conectar pacientes y proveedores en Cuba."
    }
    await query.edit_message_text(textos.get(data, ""), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Atrás", callback_data="ayuda")]]), parse_mode="HTML")

async def _procesar_contacto_callback(query, context, data):
    tipo = data.replace("contacto_", "")
    context.user_data["tipo_contacto"] = tipo
    if tipo in ["whatsapp", "ambos"]:
        context.user_data["estado"] = "esperando_telefono"
        await query.edit_message_text("📱 Escribe tu WhatsApp (ej: <code>+53 5 1234567</code>):", parse_mode="HTML")
    else:
        context.user_data["estado"] = "esperando_telegram"
        await query.edit_message_text("✈️ Escribe tu @usuario de Telegram:")

async def _admin_panel(query):
    teclado = [[InlineKeyboardButton("📥 Cargar Listado", callback_data="admin_cargar")], [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_stats")], [InlineKeyboardButton("👥 Proveedores", callback_data="admin_provs")], [InlineKeyboardButton("⭐ Destacar", callback_data="admin_dest")], [InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    await query.edit_message_text("🔧 <b>Panel Admin</b>", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _admin_acciones(query, data):
    if data == "admin_cargar": await query.edit_message_text("📥 Usa: <code>/admin_cargar_listado +5351234567</code>", parse_mode="HTML")
    elif data == "admin_stats": await _admin_stats(query)
    elif data == "admin_provs": await _admin_provs(query)
    elif data == "admin_dest": await _admin_dest(query)

async def _admin_stats(query):
    await limpiar_expirados()
    c_prov = await coleccion_proveedores.count_documents({})
    c_cli = await coleccion_clientes.count_documents({})
    c_cat = await coleccion_catalogos.count_documents({})
    c_dest = await coleccion_proveedores.count_documents({"destacado_hasta": {"$gt": datetime.now()}})
    mensaje = f"📊 <b>Estadísticas</b>\n\n👥 Clientes: {c_cli}\n🏥 Proveedores: {c_prov}\n📋 Catálogos: {c_cat}\n⭐ Destacados: {c_dest}"
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


# ===== HANDLER ÚNICO DE MENSAJES =====
async def procesar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    texto = update.message.text
    estado = context.user_data.get("estado")

    if estado == "esperando_medicina": await _busqueda(update, context, user_id, texto)
    elif estado == "esperando_listado": await _listado(update, context, user_id, texto)
    elif estado == "cambiando_provincia": await _cambio_provincia(update, context, user_id, texto)
    elif estado == "esperando_telefono": await _telefono(update, context, user_id, texto)
    elif estado == "esperando_telegram": await _telegram_user(update, context, user_id, texto)
    elif estado == "admin_esperando_listado": await _admin_listado(update, context, user_id, texto)
    elif estado == "esperando_seleccion": await _seleccion_sugerencia(update, context, user_id, texto)

async def _cambio_provincia(update, context, user_id, texto):
    try:
        num = int(texto.strip())
        if 1 <= num <= len(PROVINCIAS):
            prov = PROVINCIAS[num-1]
            await coleccion_clientes.update_one({"_id": user_id}, {"$set": {"provincia": prov}})
            await update.message.reply_text(f"✅ Provincia: <b>{esc(prov)}</b>", parse_mode="HTML")
            context.user_data["estado"] = None
            return await enviar_menu_mensaje(update, user_id)
        else: raise ValueError
    except ValueError:
        await update.message.reply_text("Número inválido. Intenta de nuevo.")

async def _busqueda(update, context, user_id, texto):
    medicina_buscar = normalizar_texto(texto)
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    provincia = cliente.get("provincia") if cliente else None

    if not provincia: return await update.message.reply_text("❌ Configura tu provincia primero.")

    await coleccion_clientes.update_one({"_id": user_id}, {"$inc": {"busquedas": 1}})
    await limpiar_expirados()

    # Búsqueda Fuzzy: Obtener catálogos de la provincia
    catalogos_prov = await coleccion_catalogos.find({"provincia": provincia}).to_list(None)
    
    opciones = []
    for cat in catalogos_prov:
        prov_id = cat["proveedor_id"]
        proveedor = await coleccion_proveedores.find_one({"_id": prov_id})
        if not proveedor: continue
        
        for i, linea_norm in enumerate(cat["lineas_normalizadas"]):
            score = rfuzz.WRatio(medicina_buscar, linea_norm)
            if score >= UMBRAL_FUZZY:
                opciones.append({
                    "score": score, 
                    "linea_original": cat["lineas_originales"][i], 
                    "proveedor": proveedor
                })

    if not opciones:
        await update.message.reply_text(f"❌ No encontré '{esc(texto)}' en {esc(provincia)}.\n\n💡 Revisa ortografía o prueba otro nombre.", parse_mode="HTML")
        context.user_data["estado"] = None
        return

    opciones.sort(key=lambda x: x["score"], reverse=True)

    if len(opciones) <= 5:
        mensaje = f"🔍 <b>{esc(texto.upper())}</b> en {esc(provincia)}\n\n✅ Encontré {len(opciones)} coincidencia(s):\n\n"
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
                    if tel: enlace_wa = f"https://wa.me/{tel}?text=Hola%20te%20contacto%20desde%20MediCuba.%20Tienes%20{texto}?"
            mensaje += f"   • {esc(op['linea_original'])}\n"
        
        botones = []
        if enlace_wa: botones.append([InlineKeyboardButton("📞 WhatsApp", url=enlace_wa)])
        botones.append([InlineKeyboardButton("🏠 Menú", callback_data="volver")])
        await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        context.user_data["estado"] = None
    else:
        # Mostrar sugerencias numeradas
        mensaje = f"🔍 <b>{esc(texto.upper())}</b> - Sugerencias:\n\n"
        sugerencias = opciones[:10]
        context.user_data["sugerencias"] = sugerencias
        
        for i, op in enumerate(sugerencias, 1):
            mensaje += f"{i}. {esc(op['linea_original'])}\n"
        mensaje += "\nResponde con el <b>NÚMERO</b> para ver el proveedor, o ignora para buscar otra cosa."
        await update.message.reply_text(mensaje, parse_mode="HTML")
        context.user_data["estado"] = "esperando_seleccion"

async def _seleccion_sugerencia(update, context, user_id, texto):
    sugerencias = context.user_data.get("sugerencias", [])
    try:
        num = int(texto.strip())
        if 1 <= num <= len(sugerencias):
            op = sugerencias[num-1]
            p = op["proveedor"]
            dest = "⭐ " if await es_destacado_activo(p) else ""
            mensaje = f"🏥 {dest}<b>{esc(p.get('nombre'))}</b>\n📞 {esc(p.get('contacto_mostrar'))}\n\n"
            mensaje += f"💊 {esc(op['linea_original'])}\n"
            
            enlace_wa = None
            contacto = p.get("contacto", {})
            if contacto.get("tipo") in ["whatsapp", "ambos"]:
                tel = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
                if tel: enlace_wa = f"https://wa.me/{tel}?text=Hola%20te%20contacto%20desde%20MediCuba.%20Tienes%20esto?"
            
            botones = [[InlineKeyboardButton("🏠 Menú", callback_data="volver")]]
            if enlace_wa: botones.insert(0, [InlineKeyboardButton("📞 WhatsApp", url=enlace_wa)])
            await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        else: raise ValueError
    except ValueError:
        await update.message.reply_text("Número inválido. Intenta de nuevo o usa el menú.")
    context.user_data["estado"] = None

async def _listado(update, context, user_id, texto):
    if contiene_productos_no_medicos(texto):
        return await update.message.reply_text("❌ <b>Listado rechazado.</b>\n\nSe detectaron productos no médicos (ropa, comida, etc). Solo medicinas.", parse_mode="HTML")

    lineas = [l.strip() for l in texto.split('\n') if l.strip()]
    truncado = len(lineas) > MAX_LINEAS_CATALOGO
    lineas = lineas[:MAX_LINEAS_CATALOGO]
    
    lineas_norm = [normalizar_texto(l) for l in lineas]

    # Lógica de 2 catálogos máximo (FIFO)
    cat_count = await coleccion_catalogos.count_documents({"proveedor_id": user_id, "es_admin": False})
    if cat_count >= MAX_CATALOGOS_PROVEEDOR:
        oldest = await coleccion_catalogos.find_one({"proveedor_id": user_id, "es_admin": False}, sort=[("fecha_creacion", 1)])
        if oldest: await coleccion_catalogos.delete_one({"_id": oldest["_id"]})

    # Asegurar que el proveedor existe
    provincia_doc = await coleccion_clientes.find_one({"_id": user_id})
    provincia = provincia_doc.get("provincia", "Santiago de Cuba") if provincia_doc else "Santiago de Cuba"
    
    await coleccion_proveedores.update_one({"_id": user_id}, {"$set": {"nombre": update.effective_user.first_name or "Proveedor", "provincia": provincia, "link_token": user_id}}, upsert=True)

    # Guardar catálogo
    await coleccion_catalogos.insert_one({
        "proveedor_id": user_id,
        "lineas_originales": lineas,
        "lineas_normalizadas": lineas_norm,
        "es_admin": False,
        "fecha_creacion": datetime.now(),
        "fecha_expiracion": None,
        "provincia": provincia,
        "hash": generar_hash(texto)
    })

    aviso = ""
    if truncado: aviso = f"\n⚠️ Solo se guardaron las primeras {MAX_LINEAS_CATALOGO} líneas."
    
    context.user_data["estado"] = "esperando_contacto"
    context.user_data["medicinas_count"] = len(lineas)
    
    teclado = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")], [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")], [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")]]
    await update.message.reply_text(f"✅ Se extrajeron <b>{len(lineas)}</b> líneas.{aviso}\n\nAhora elige cómo te contactarán:", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _telefono(update, context, user_id, texto):
    telefono = texto.strip()
    tipo = context.user_data.get("tipo_contacto", "whatsapp")
    await coleccion_proveedores.update_one({"_id": user_id}, {"$set": {"contacto.tipo": tipo, "contacto.whatsapp": telefono, "contacto_mostrar": telefono, "nombre": update.effective_user.first_name or "Prov"}})
    if tipo == "ambos":
        context.user_data["estado"] = "esperando_telegram"
        return await update.message.reply_text("✈️ Ahora escribe tu @usuario de Telegram:")
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
    await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")
    context.user_data["estado"] = None


# ===== COMANDOS ADMIN =====
async def admin_cargar_listado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not es_admin(user_id): return
    if not context.args: return await update.message.reply_text("Uso: <code>/admin_cargar_listado +5351234567</code>", parse_mode="HTML")
    context.user_data["admin_telefono"] = context.args[0]
    context.user_data["estado"] = "admin_esperando_listado"
    await update.message.reply_text("📥 Pega el listado de admin:", parse_mode="HTML")

async def _admin_listado(update, context, user_id, texto):
    if not es_admin(user_id): return
    telefono = context.user_data.get("admin_telefono")
    if not telefono: return await update.message.reply_text("❌ Error. Usa /admin_cargar_listado primero.")

    hash_txt = generar_hash(texto)
    if await coleccion_catalogos.find_one({"hash": hash_txt}):
        context.user_data["estado"] = None
        return await update.message.reply_text("⚠️ <b>Duplicado.</b> Este listado exacto ya existe.", parse_mode="HTML")

    if contiene_productos_no_medicos(texto):
        return await update.message.reply_text("❌ Listado contiene productos no médicos.", parse_mode="HTML")

    lineas = [l.strip() for l in texto.split('\n') if l.strip()][:MAX_LINEAS_CATALOGO]
    lineas_norm = [normalizar_texto(l) for l in lineas]
    
    admin_prov_id = f"admin_{user_id}_{int(datetime.now().timestamp())}"
    provincia = "Santiago de Cuba"

    await coleccion_proveedores.update_one({"_id": admin_prov_id}, {"$set": {"nombre": "Admin MediCuba", "contacto": {"tipo": "whatsapp", "whatsapp": telefono}, "contacto_mostrar": telefono, "provincia": provincia}}, upsert=True)
    
    expiracion = datetime.now() + timedelta(days=DIAS_EXPIRACION_ADMIN)
    await coleccion_catalogos.insert_one({
        "proveedor_id": admin_prov_id,
        "lineas_originales": lineas,
        "lineas_normalizadas": lineas_norm,
        "es_admin": True,
        "fecha_creacion": datetime.now(),
        "fecha_expiracion": expiracion,
        "provincia": provincia,
        "hash": hash_txt
    })

    await update.message.reply_text(f"✅ <b>Listado Admin cargado</b>\n\n📊 {len(lineas)} líneas.\n📞 {esc(telefono)}\n⏳ Expira: {expiracion.strftime('%Y-%m-%d')}", parse_mode="HTML")
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


# ===== SERVIDOR HEALTH CHECK PARA RENDER =====
class HealthCheckHandler(BaseHTTPRequestHandler):
    """Servidor HTTP mínimo para que Render no mate el proceso"""
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, format, *args):
        pass  # Silenciar logs del health check


def iniciar_health_check():
    """Inicia servidor HTTP en el puerto de Render"""
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"✅ Health check iniciado en puerto {port}")


# ===== MAIN =====
def main():
    # 1. Iniciar health check PRIMERO (para que Render no haga TimeOut)
    iniciar_health_check()

    # 2. Crear la aplicación del bot
    application = Application.builder().token(TOKEN).build()

    # 3. Registrar handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin_cargar_listado", admin_cargar_listado))
    application.add_handler(CommandHandler("destacar", destacar_cmd))
    application.add_handler(CallbackQueryHandler(manejador_callbacks))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensajes)
    )

    print("🤖 MediCuba Bot (MongoDB + Fuzzy) iniciado...")

    # 4. Ejecutar con drop_pending_updates para evitar conflictos
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
