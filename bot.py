"""
BOT DE MEDICINAS CUBA - VERSIÓN PROFESIONAL MONGODB
v2.8.0 | Fuzzy Matching | Carrusel Anuncios | Admin Enumerados
Optimizado para conexiones lentas (Cuba) | Auto-reconexión
"""

import logging
import json
import os
import re
import html
import asyncio
import hashlib
import threading
import signal
import time
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
VERSION = "v2.8.0"
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 814338625
ADMIN_USERNAME = "TuUsuarioAqui"

if not TOKEN:
    logger.error("❌ FATAL: BOT_TOKEN no configurado.")

MAX_LINEAS_CATALOGO = 80
MAX_CATALOGOS_PROVEEDOR = 2
DIAS_EXPIRACION_ADMIN = 10
UMBRAL_FUZZY = 70
BOT_LINK = "https://t.me/MediCubaBot"

BLACKLIST = ["zapatos", "ropa", "joyas", "comida", "pollo", "arroz", "telefono", "casa", "carro", "zapatillas", "frutas", "viveres"]

# ===== CONEXIÓN MONGODB =====
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    logger.error("❌ FATAL: MONGODB_URI no configurado.")

client = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000, retryWrites=True)
db = client.medicubadb
coleccion_clientes = db.clientes
coleccion_proveedores = db.proveedores
coleccion_catalogos = db.catalogos

PROVINCIAS = ["Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas", "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila", "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba", "Guantánamo", "Isla de la Juventud"]

# ===== CONFIG LOCAL =====
ARCHIVO_CONFIG = "config_bot.json"
def cargar_config():
    d = {"administradores": [ADMIN_ID], "anuncios": [], "siguiente_num_admin": 1}
    if os.path.exists(ARCHIVO_CONFIG):
        try:
            with open(ARCHIVO_CONFIG, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k in d:
                    if k not in data: data[k] = d[k]
                return data
        except: pass
    return d
def guardar_config(c):
    with open(ARCHIVO_CONFIG, 'w', encoding='utf-8') as f: json.dump(c, f, ensure_ascii=False, indent=2)
datos = cargar_config()

# ===== AUXILIARES =====
def esc(t):
    if t is None: return ""
    return html.escape(str(t))
def normalizar_texto(t):
    t = t.lower()
    for a, b in {'á':'a','é':'e','í':'i','ó':'o','ú':'u','ü':'u'}.items(): t = t.replace(a, b)
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', t)).strip()
def es_admin(uid): return int(uid) in datos.get("administradores", [ADMIN_ID])
async def es_destacado(p):
    if not p or not p.get("destacado_hasta"): return False
    try: return datetime.now() < p["destacado_hasta"]
    except: return False
def contiene_no_medicos(t):
    tn = normalizar_texto(t)
    for p in BLACKLIST:
        if re.search(r'\b'+re.escape(p)+r'\b', tn): return True
    return False
def generar_hash(t): return hashlib.md5(t.encode('utf-8')).hexdigest()
async def limpiar_expirados():
    try:
        r = await coleccion_catalogos.delete_many({"es_admin": True, "fecha_expiracion": {"$lt": datetime.now()}})
        if r.deleted_count: logger.info(f"🗑️ Purgados {r.deleted_count} expirados")
    except: pass

# ===== MENÚS =====
def menu_principal(uid, prov, anuncio_idx=0):
    a_list = datos.get("anuncios", [])
    msg_anuncio = ""
    tk_anuncio = []
    
    if a_list:
        msg_anuncio = f"📢 <b>AVISO:</b> {a_list[anuncio_idx]}\n\n"
        if len(a_list) > 1:
            prev_idx = (anuncio_idx - 1) % len(a_list)
            next_idx = (anuncio_idx + 1) % len(a_list)
            tk_anuncio.append([
                InlineKeyboardButton("⬅️", callback_data=f"anuncio_{prev_idx}"),
                InlineKeyboardButton(f"{anuncio_idx+1}/{len(a_list)}", callback_data="ignore"),
                InlineKeyboardButton("➡️", callback_data=f"anuncio_{next_idx}")
            ])

    tk = tk_anuncio + [
        [InlineKeyboardButton("🔍 Buscar Medicina", callback_data="buscar")],
        [InlineKeyboardButton("📝 Publicar Catálogo", callback_data="publicar")],
        [InlineKeyboardButton("📍 Cambiar Provincia", callback_data="cambiar_provincia")],
        [InlineKeyboardButton("👤 Mi Perfil", callback_data="mi_perfil")],
        [InlineKeyboardButton("⭐ Destacados", callback_data="destacados")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ]
    if es_admin(uid): tk.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    
    t = f"{msg_anuncio}🏥 <b>MediCuba</b>\n🩺 Tu salud, nuestra prioridad\n\n📍 <b>Provincia:</b> {esc(prov)}\n\n🔗 <code>t.me/MediCubaBot</code>\n\n¿Qué deseas hacer?\n\n<i>MediCuba {VERSION}</i>"
    return t, InlineKeyboardMarkup(tk)

def menu_post_busqueda():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")]])

async def enviar_menu_cb(q, uid, anuncio_idx=0):
    doc = await coleccion_clientes.find_one({"_id": uid})
    p = doc.get("provincia", "No seleccionada") if doc else "No seleccionada"
    t, tk = menu_principal(uid, p, anuncio_idx)
    try: await q.edit_message_text(t, reply_markup=tk, parse_mode="HTML")
    except: await q.message.reply_text(t, reply_markup=tk, parse_mode="HTML")

async def enviar_menu_msg(upd, uid, anuncio_idx=0):
    doc = await coleccion_clientes.find_one({"_id": uid})
    p = doc.get("provincia", "No seleccionada") if doc else "No seleccionada"
    t, tk = menu_principal(uid, p, anuncio_idx)
    await upd.message.reply_text(t, reply_markup=tk, parse_mode="HTML")

# ===== COMANDOS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        if context.args and context.args[0].startswith("proveedor_"):
            pid = context.args[0].replace("proveedor_", "")
            await mostrar_cat_prov(update, pid); return
        await coleccion_clientes.update_one({"_id": uid}, {"$setOnInsert": {"provincia": None, "busquedas": 0}}, upsert=True)
        c = await coleccion_clientes.find_one({"_id": uid})
        if not c.get("provincia"): return await forzar_prov(update, context)
        await enviar_menu_msg(update, uid)
    except Exception as e:
        logger.error(f"Start error: {e}")
        await update.message.reply_text("⚠️ Error. Escribe /start")

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["estado"] = None
    await update.message.reply_text("↩️ Cancelado.", reply_markup=ReplyKeyboardRemove())
    await enviar_menu_msg(update, str(update.effective_user.id))

async def forzar_prov(update, context):
    context.user_data["estado"] = "cambiando_provincia"
    lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
    await update.message.reply_text(f"👋 ¡Bienvenido!\n\n📍 Selecciona:\n\n{lista}\n\nNÚMERO:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))

async def mostrar_cat_prov(update, pid):
    prov = await coleccion_proveedores.find_one({"_id": pid})
    if not prov: return await update.message.reply_text("❌ No encontrado.")
    cats = await coleccion_catalogos.find({"proveedor_id": pid, "es_admin": False}).to_list(None)
    if not cats: return await update.message.reply_text("📭 Sin catálogos.")
    msg = f"🏥 <b>{esc(prov.get('nombre'))}</b>\n"
    if await es_destacado(prov): msg += "⭐ <b>Destacado</b> ⭐\n"
    msg += f"📞 {esc(prov.get('contacto_mostrar', 'N/A'))}\n" + "─"*20 + "\n\n"
    for i, c in enumerate(cats, 1):
        msg += f"<b>Catálogo {i}:</b>\n"
        for l in c["lineas_originales"][:30]: msg += f"• {esc(l)}\n"
        msg += "\n"
    msg += "─"*20 + "\n🩺 MediCuba"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Ir al Bot", callback_data="volver")]]), parse_mode="HTML")

# ===== CALLBACKS =====
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = str(q.from_user.id); d = q.data
    tv = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)
    
    if d == "ignore": return
    elif d.startswith("anuncio_"):
        idx = int(d.split("_")[1])
        await enviar_menu_cb(q, uid, anuncio_idx=idx)
    elif d == "volver": await enviar_menu_cb(q, uid)
    elif d == "buscar":
        c = await coleccion_clientes.find_one({"_id": uid})
        if not c or not c.get("provincia"):
            return await q.edit_message_text("❌ Configura provincia.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📍 Configurar", callback_data="cambiar_provincia")]]))
        context.user_data["estado"] = "esperando_medicina"
        await q.edit_message_text("🔍 <b>Buscar Medicina</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "Escribe el nombre:\n\n<i>Ej: gravinol</i>", reply_markup=tv, parse_mode="HTML")
    elif d == "publicar":
        context.user_data["estado"] = "esperando_listado"
        await q.edit_message_text("📝 <b>Publicar Catálogo</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "📋 Pega tu listado (máx 80 líneas, 2 catálogos, solo medicinas).", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"], ["❌ Cancelar"]], resize_keyboard=True), parse_mode="HTML")
    elif d == "cambiar_provincia":
        context.user_data["estado"] = "cambiando_provincia"
        lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
        await q.edit_message_text("📍 <b>Cambiar Provincia</b>", parse_mode="HTML")
        await context.bot.send_message(uid, f"{lista}\n\nNÚMERO:", reply_markup=tv)
    elif d == "mi_perfil": await _perfil(q, uid)
    elif d == "editar_contacto":
        context.user_data["editando_contacto"] = True
        tk = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp"), InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram"), InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")], [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]
        await q.edit_message_text("✏️ <b>Editar Contacto</b>", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")
    elif d == "ver_mi_catalogo": await _mi_cat(q, uid)
    elif d == "destacados": await _destacados(q)
    elif d == "ayuda": await _ayuda(q)
    elif d.startswith("ayuda_"): await _ayuda_det(q, d)
    elif d.startswith("contacto_"): await _contacto_cb(q, context, d)
    elif d == "admin_panel" and es_admin(uid): await _admin_panel(q)
    elif d.startswith("admin_") and es_admin(uid): await _admin_acc(q, context, uid, d)

# ===== VISTAS =====
async def _perfil(q, uid):
    prov = await coleccion_proveedores.find_one({"_id": uid})
    if prov:
        cc = await coleccion_catalogos.count_documents({"proveedor_id": uid, "es_admin": False})
        msg = f"👤 <b>Perfil Proveedor</b>\n\n📛 {esc(prov.get('nombre'))}\n📞 {esc(prov.get('contacto_mostrar'))}\n📋 Catálogos: {cc}/2\n"
        if prov.get('link_token'): msg += f"🔗 <code>t.me/MediCubaBot?start=proveedor_{uid}</code>\n"
        tk = [[InlineKeyboardButton("✏️ Editar Contacto", callback_data="editar_contacto")], [InlineKeyboardButton("📋 Ver Catálogo", callback_data="ver_mi_catalogo")], [InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    else:
        c = await coleccion_clientes.find_one({"_id": uid})
        msg = f"👤 <b>Perfil Cliente</b>\n\n📍 {esc(c.get('provincia', 'N/A'))}\n📊 Búsquedas: {c.get('busquedas', 0)}\n"
        tk = [[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _mi_cat(q, uid):
    cats = await coleccion_catalogos.find({"proveedor_id": uid, "es_admin": False}).sort("fecha_creacion", 1).to_list(None)
    if not cats: return await q.edit_message_text("📭 Sin catálogos.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Publicar", callback_data="publicar")], [InlineKeyboardButton("🔙", callback_data="mi_perfil")]]), parse_mode="HTML")
    msg = f"📋 <b>Tus Catálogos</b> ({len(cats)}/2)\n\n"
    for i, c in enumerate(cats, 1):
        msg += f"<b>Catálogo {i}:</b>\n"
        for l in c["lineas_originales"][:20]: msg += f"• {esc(l)}\n"
        if len(c["lineas_originales"]) > 20: msg += f"... +{len(c['lineas_originales'])-20}\n"
        msg += "\n"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="mi_perfil")]]), parse_mode="HTML")

async def _destacados(q):
    await limpiar_expirados()
    provs = await coleccion_proveedores.find({"destacado_hasta": {"$gt": datetime.now()}}).to_list(None)
    if not provs: return await q.edit_message_text("⭐ Sin destacados.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]), parse_mode="HTML")
    msg = "⭐ <b>DESTACADOS</b> ⭐\n\n"
    for p in provs[:5]:
        l = f"t.me/MediCubaBot?start=proveedor_{p['_id']}"
        msg += f"🏥 <b>{esc(p.get('nombre'))}</b>\n📞 {esc(p.get('contacto_mostrar'))}\n🔗 <a href='{l}'>Ver</a>\n\n"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]), parse_mode="HTML", disable_web_page_preview=True)

async def _ayuda(q):
    tk = [[InlineKeyboardButton("👨‍💼 Proveedores", callback_data="ayuda_prov")], [InlineKeyboardButton("🛒 Clientes", callback_data="ayuda_cli")], [InlineKeyboardButton("⚙️ General", callback_data="ayuda_gen")], [InlineKeyboardButton("💬 Admin", url=f"https://t.me/{ADMIN_USERNAME}")], [InlineKeyboardButton("🏠 Volver", callback_data="volver")]]
    await q.edit_message_text("❓ <b>Ayuda</b>", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _ayuda_det(q, d):
    lnk = f"\n\n🔗 Comparte: <code>t.me/MediCubaBot</code>"
    ts = {"ayuda_prov": f"👨‍💼 <b>Proveedores</b>\n\nSube hasta 2 listados (80 líneas) con copiar y pegar.\n\n⭐ <b>Estrellas:</b> Destacados aparecen PRIMERO y generan 3x más contactos.\n\n📞 Registra WhatsApp, Telegram o ambos.{lnk}", "ayuda_cli": f"🛒 <b>Clientes</b>\n\nBuscador inteligente que acepta errores.\n\n⭐ <b>Estrellas:</b> Los ⭐ son los más confiables.\n\n📱 Contacta por WhatsApp o Telegram directo.{lnk}", "ayuda_gen": f"⚙️ <b>General</b>\n\n🩺 MediCuba conecta pacientes con proveedores directo.\n\nConfigura provincia una vez, cámbiala cuando viajes.{lnk}"}
    await q.edit_message_text(ts.get(d, ""), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Admin", url=f"https://t.me/{ADMIN_USERNAME}")], [InlineKeyboardButton("🔙", callback_data="ayuda")]]), parse_mode="HTML")

async def _contacto_cb(q, context, d):
    tipo = d.replace("contacto_", "")
    context.user_data["tipo_contacto"] = tipo
    tv = ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True)
    if tipo in ["whatsapp", "ambos"]:
        context.user_data["estado"] = "esperando_telefono"
        await q.edit_message_text("📱 <b>WhatsApp</b>", parse_mode="HTML")
        await context.bot.send_message(q.from_user.id, "Escribe tu número (ej: <code>+53 5 1234567</code>):", reply_markup=tv, parse_mode="HTML")
    else:
        context.user_data["estado"] = "esperando_telegram"
        await q.edit_message_text("✈️ <b>Telegram</b>", parse_mode="HTML")
        await context.bot.send_message(q.from_user.id, "Escribe tu @usuario:", reply_markup=tv)

async def _admin_panel(q):
    tk = [
        [InlineKeyboardButton("📥 Cargar", callback_data="admin_cargar"), InlineKeyboardButton("➕ Añadir Admin", callback_data="admin_add_admin")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats"), InlineKeyboardButton("👥 Provs", callback_data="admin_provs")],
        [InlineKeyboardButton("⭐ Destacar", callback_data="admin_dest"), InlineKeyboardButton("📢 Anuncios", callback_data="admin_anuncios")],
        [InlineKeyboardButton("🏠 Volver", callback_data="volver")]
    ]
    await q.edit_message_text("🔧 <b>Admin</b>", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _admin_acc(q, context, uid, d):
    if d == "admin_cargar":
        context.user_data["estado"] = "admin_esperando_tel"
        await q.edit_message_text("📥 <b>Cargar Listado</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "WhatsApp del listado (ej: <code>+5351234567</code>):", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True), parse_mode="HTML")
    elif d == "admin_add_admin":
        context.user_data["estado"] = "admin_esperando_id_nuevo"
        await q.edit_message_text("➕ <b>Añadir Admin</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "Envía el ID numérico del nuevo administrador:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))
    elif d == "admin_stats": await _admin_stats(q)
    elif d == "admin_provs": await _admin_provs(q)
    elif d == "admin_dest": await _admin_dest(q)
    elif d == "admin_anuncios": await _admin_anun(q)

async def _admin_stats(q):
    await limpiar_expirados()
    cp = await coleccion_proveedores.count_documents({})
    cc = await coleccion_clientes.count_documents({})
    cca = await coleccion_catalogos.count_documents({})
    msg = f"📊 Clientes: {cc}\n🏥 Proveedores: {cp}\n📋 Catálogos: {cca}\n🛡️ Admins: {len(datos.get('administradores',[]))}"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_provs(q):
    ps = await coleccion_proveedores.find({}).to_list(None)
    msg = "👥 <b>Proveedores</b>\n\n"
    for p in ps[:10]:
        c = await coleccion_catalogos.count_documents({"proveedor_id": p["_id"]})
        msg += f"• <code>{p['_id']}</code> - {esc(p.get('nombre'))} ({c})\n"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_dest(q):
    ps = await coleccion_proveedores.find({}).to_list(None)
    msg = "⭐ <code>/destacar ID DIAS</code>\n\n"
    for p in ps[:15]: msg += f"• <code>{p['_id']}</code> - {esc(p.get('nombre'))}\n"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_anun(q):
    ans = datos.get("anuncios", [])
    msg = "📢 <b>Anuncios</b> (rotan con flechas en menú):\n\n"
    if not ans: msg += "<i>Vacío</i>"
    else:
        for i, a in enumerate(ans, 1): msg += f"{i}. {esc(a)}\n"
    msg += "\n<code>/anuncio add texto</code>\n<code>/anuncio del N</code>"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

# ===== MENSAJES =====
async def proc_msgs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    txt = update.message.text
    if txt in ["🔙 Volver al Menú", "❌ Cancelar", "/cancelar", "/cancel"]:
        context.user_data["estado"] = None
        await update.message.reply_text("↩️", reply_markup=ReplyKeyboardRemove())
        return await enviar_menu_msg(update, uid)
    est = context.user_data.get("estado")
    if est is None:
        c = await coleccion_clientes.find_one({"_id": uid})
        if c and c.get("provincia"): est = "esperando_medicina"; context.user_data["estado"] = est
        else: return await update.message.reply_text("⚠️ Usa /start")
    try:
        if est == "esperando_medicina": await _busqueda(update, context, uid, txt)
        elif est == "esperando_listado": await _listado(update, context, uid, txt)
        elif est == "cambiando_provincia": await _cambiar_prov(update, context, uid, txt)
        elif est == "esperando_telefono": await _telefono(update, context, uid, txt)
        elif est == "esperando_telegram": await _telegram(update, context, uid, txt)
        elif est == "admin_esperando_tel": await _admin_tel(update, context, uid, txt)
        elif est == "admin_esperando_listado": await _admin_list(update, context, uid, txt)
        elif est == "admin_esperando_id_nuevo": await _admin_add_id_msg(update, context, uid, txt)
        elif est == "esperando_seleccion": await _seleccion(update, context, uid, txt)
        else: context.user_data["estado"] = None
    except Exception as e:
        logger.error(f"Error: {e}")
        context.user_data["estado"] = None
        await update.message.reply_text("⚠️ Error.", reply_markup=ReplyKeyboardRemove())

async def _cambiar_prov(update, context, uid, txt):
    try:
        n = int(txt.strip())
        if 1 <= n <= len(PROVINCIAS):
            pr = PROVINCIAS[n-1]
            await coleccion_clientes.update_one({"_id": uid}, {"$set": {"provincia": pr}})
            await update.message.reply_text(f"✅ {esc(pr)}", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
            context.user_data["estado"] = None
            return await enviar_menu_msg(update, uid)
        else: raise ValueError
    except ValueError: await update.message.reply_text("Número inválido.")

async def _admin_add_id_msg(update, context, uid, txt):
    try:
        na = int(txt.strip())
        if na not in datos.get("administradores", [ADMIN_ID]):
            datos.setdefault("administradores", [ADMIN_ID]).append(na)
            guardar_config(datos)
            await update.message.reply_text(f"✅ Admin {na} añadido.", reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text("Ese ID ya es administrador.", reply_markup=ReplyKeyboardRemove())
    except ValueError:
        await update.message.reply_text("ID inválido. Debe ser numérico.", reply_markup=ReplyKeyboardRemove())
    context.user_data["estado"] = None
    await enviar_menu_msg(update, uid)

# ===== BÚSQUEDA (Botones diferenciados por nombre de proveedor) =====
async def _busqueda(update, context, uid, txt):
    mb = normalizar_texto(txt)
    c = await coleccion_clientes.find_one({"_id": uid})
    prov = c.get("provincia") if c else None
    if not prov:
        await update.message.reply_text("❌ Configura provincia.", reply_markup=ReplyKeyboardRemove())
        return await enviar_menu_msg(update, uid)
    await coleccion_clientes.update_one({"_id": uid}, {"$inc": {"busquedas": 1}})
    await limpiar_expirados()
    try: cats = await coleccion_catalogos.find({"provincia": prov}).to_list(None)
    except: return await update.message.reply_text("⚠️ Error BD.", reply_markup=menu_post_busqueda())
    
    ops = []
    for cat in cats:
        pid = cat["proveedor_id"]
        prv = await coleccion_proveedores.find_one({"_id": pid})
        if not prv: continue
        for i, ln in enumerate(cat["lineas_normalizadas"]):
            sc = rfuzz.WRatio(mb, ln)
            if sc >= UMBRAL_FUZZY: ops.append({"s": sc, "l": cat["lineas_originales"][i], "p": prv})
    
    if not ops:
        await update.message.reply_text(f"❌ No encontré '<b>{esc(txt)}</b>' en {esc(prov)}.", reply_markup=menu_post_busqueda(), parse_mode="HTML")
        context.user_data["estado"] = None; return
    
    ops.sort(key=lambda x: x["s"], reverse=True)
    
    por_prov = {}
    for o in ops:
        pid = o["p"]["_id"]
        if pid not in por_prov: por_prov[pid] = {"prov": o["p"], "items": []}
        por_prov[pid]["items"].append(o["l"])
    
    if len(por_prov) <= 3:
        msg = f"🔍 <b>{esc(txt.upper())}</b> en {esc(prov)}\n\n"
        botones = []
        
        for pid, data in por_prov.items():
            prv = data["prov"]
            dest = "⭐ " if await es_destacado(prv) else ""
            nombre_prov = prv.get('nombre', 'Prov')
            msg += f"{dest}<b>{esc(nombre_prov)}</b>\n📞 {esc(prv.get('contacto_mostrar'))}\n"
            for item in data["items"][:3]:
                msg += f"   • {esc(item)}\n"
            msg += "\n"
            
            contacto = prv.get("contacto", {})
            tel_wa = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
            tel_tg = contacto.get("telegram", "")
            
            if tel_wa:
                wa_msg = f"Hola, te contacto desde MediCuba ({BOT_LINK}). ¿Tienes disponible {txt}?"
                wa_url = f"https://wa.me/{tel_wa}?text={wa_msg.replace(' ', '%20')}"
                botones.append([InlineKeyboardButton(f"📞 WA: {esc(nombre_prov)}", url=wa_url)])
            if tel_tg:
                tg_url = f"https://t.me/{tel_tg.replace('@','')}"
                botones.append([InlineKeyboardButton(f"✈️ TG: {esc(nombre_prov)}", url=tg_url)])
        
        botones.append([InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        context.user_data["estado"] = None
    else:
        msg = f"🔍 <b>{esc(txt.upper())}</b> - Sugerencias:\n\n"
        sugs = ops[:10]; context.user_data["sugs"] = sugs
        for i, o in enumerate(sugs, 1): msg += f"{i}. {esc(o['l'])}\n"
        msg += "\nResponde el NÚMERO."
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True), parse_mode="HTML")
        context.user_data["estado"] = "esperando_seleccion"

async def _seleccion(update, context, uid, txt):
    sugs = context.user_data.get("sugs", [])
    try:
        n = int(txt.strip())
        if 1 <= n <= len(sugs):
            o = sugs[n-1]; prv = o["p"]
            dest = "⭐ " if await es_destacado(prv) else ""
            nombre_prov = prv.get('nombre', 'Prov')
            msg = f"🏥 {dest}<b>{esc(nombre_prov)}</b>\n📞 {esc(prv.get('contacto_mostrar'))}\n\n💊 {esc(o['l'])}\n"
            
            botones = []
            contacto = prv.get("contacto", {})
            tel_wa = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
            tel_tg = contacto.get("telegram", "")
            if tel_wa:
                wa_msg = f"Hola, te contacto desde MediCuba ({BOT_LINK}). ¿Tienes disponible esto?"
                wa_url = f"https://wa.me/{tel_wa}?text={wa_msg.replace(' ', '%20')}"
                botones.append([InlineKeyboardButton(f"📞 WA: {esc(nombre_prov)}", url=wa_url)])
            if tel_tg:
                tg_url = f"https://t.me/{tel_tg.replace('@','')}"
                botones.append([InlineKeyboardButton(f"✈️ TG: {esc(nombre_prov)}", url=tg_url)])
            botones.append([InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        else: raise ValueError
    except ValueError: await update.message.reply_text("Número inválido.")
    context.user_data["estado"] = None

# ===== LISTADO =====
async def _listado(update, context, uid, txt):
    if contiene_no_medicos(txt):
        context.user_data["estado"] = None
        await update.message.reply_text("❌ Productos no médicos.", reply_markup=ReplyKeyboardRemove())
        return await enviar_menu_msg(update, uid)
    lns = [l.strip() for l in txt.split('\n') if l.strip()]
    tr = len(lns) > MAX_LINEAS_CATALOGO
    lns = lns[:MAX_LINEAS_CATALOGO]
    lns_n = [normalizar_texto(l) for l in lns]
    cc = await coleccion_catalogos.count_documents({"proveedor_id": uid, "es_admin": False})
    if cc >= MAX_CATALOGOS_PROVEEDOR:
        ol = await coleccion_catalogos.find_one({"proveedor_id": uid, "es_admin": False}, sort=[("fecha_creacion", 1)])
        if ol: await coleccion_catalogos.delete_one({"_id": ol["_id"]})
    cd = await coleccion_clientes.find_one({"_id": uid})
    pr = cd.get("provincia", "Santiago de Cuba") if cd else "Santiago de Cuba"
    await coleccion_proveedores.update_one({"_id": uid}, {"$set": {"nombre": update.effective_user.first_name or "Prov", "provincia": pr, "link_token": uid}}, upsert=True)
    await coleccion_catalogos.insert_one({"proveedor_id": uid, "lineas_originales": lns, "lineas_normalizadas": lns_n, "es_admin": False, "fecha_creacion": datetime.now(), "fecha_expiracion": None, "provincia": pr, "hash": generar_hash(txt)})
    av = f"\n⚠️ Solo {MAX_LINEAS_CATALOGO} líneas." if tr else ""
    context.user_data["estado"] = "esperando_contacto"; context.user_data["mc"] = len(lns)
    tk = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp"), InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram"), InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")]]
    await update.message.reply_text(f"✅ {len(lns)} líneas.{av}\n\n¿Cómo te contactarán?", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

# ===== CONTACTO PROVEEDOR =====
async def _telefono(update, context, uid, txt):
    tel = txt.strip(); tipo = context.user_data.get("tipo_contacto", "whatsapp")
    await coleccion_proveedores.update_one({"_id": uid}, {"$set": {"contacto.tipo": tipo, "contacto.whatsapp": tel, "contacto_mostrar": tel, "nombre": update.effective_user.first_name or "Prov"}})
    if tipo == "ambos":
        context.user_data["estado"] = "esperando_telegram"
        return await update.message.reply_text("✈️ Ahora tu @usuario de Telegram:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))
    await _fin_reg(update, context, uid)

async def _telegram(update, context, uid, txt):
    tg = txt.strip()
    if not tg.startswith("@"): tg = "@" + tg
    tipo = context.user_data.get("tipo_contacto", "telegram")
    prv = await coleccion_proveedores.find_one({"_id": uid})
    wa = prv.get("contacto", {}).get("whatsapp", "") if prv else ""
    mos = tg if tipo == "telegram" else f"{wa} / {tg}"
    await coleccion_proveedores.update_one({"_id": uid}, {"$set": {"contacto.telegram": tg, "contacto_mostrar": mos, "nombre": update.effective_user.first_name or "Prov"}})
    await _fin_reg(update, context, uid)

async def _fin_reg(update, context, uid):
    ed = context.user_data.get("editando_contacto", False)
    prv = await coleccion_proveedores.find_one({"_id": uid})
    if ed:
        msg = f"✅ Contacto actualizado:\n📞 {esc(prv.get('contacto_mostrar'))}"; context.user_data["editando_contacto"] = False
    else:
        lk = f"t.me/MediCubaBot?start=proveedor_{uid}"
        msg = f"✅ <b>¡Publicado!</b>\n\n📋 {context.user_data.get('mc',0)} líneas\n📞 {esc(prv.get('contacto_mostrar'))}\n\n🔗 <code>{lk}</code>"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú", callback_data="volver")]]), parse_mode="HTML")
    context.user_data["estado"] = None

# ===== ADMIN CMDS =====
async def admin_cargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("<code>/admin_cargar_listado +5351234567</code>", parse_mode="HTML")
    context.user_data["admin_tel"] = context.args[0]; context.user_data["estado"] = "admin_esperando_listado"
    await update.message.reply_text("📥 Pega el listado:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))

async def _admin_tel(update, context, uid, txt):
    context.user_data["admin_tel"] = txt.strip(); context.user_data["estado"] = "admin_esperando_listado"
    await update.message.reply_text(f"✅ Tel: <code>{esc(txt.strip())}</code>\n\nPega el listado:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True), parse_mode="HTML")

async def _admin_list(update, context, uid, txt):
    if not es_admin(uid): return
    tel = context.user_data.get("admin_tel")
    if not tel: context.user_data["estado"] = None; await update.message.reply_text("❌ Falta teléfono.", reply_markup=ReplyKeyboardRemove()); return await enviar_menu_msg(update, uid)
    h = generar_hash(txt)
    if await coleccion_catalogos.find_one({"hash": h}): context.user_data["estado"] = None; await update.message.reply_text("⚠️ Duplicado.", reply_markup=ReplyKeyboardRemove()); return await enviar_menu_msg(update, uid)
    if contiene_no_medicos(txt): context.user_data["estado"] = None; await update.message.reply_text("❌ No médicos.", reply_markup=ReplyKeyboardRemove()); return await enviar_menu_msg(update, uid)
    lns = [l.strip() for l in txt.split('\n') if l.strip()][:MAX_LINEAS_CATALOGO]
    lns_n = [normalizar_texto(l) for l in lns]
    
    # Lógica de enumeración de Admin
    num_admin = datos.get("siguiente_num_admin", 1)
    nombre_admin = f"{num_admin} Admin MediCuba"
    
    apid = f"admin_{uid}_{int(datetime.now().timestamp())}"
    await coleccion_proveedores.update_one({"_id": apid}, {"$set": {"nombre": nombre_admin, "contacto": {"tipo": "whatsapp", "whatsapp": tel}, "contacto_mostrar": tel, "provincia": "Santiago de Cuba"}}, upsert=True)
    exp = datetime.now() + timedelta(days=DIAS_EXPIRACION_ADMIN)
    await coleccion_catalogos.insert_one({"proveedor_id": apid, "lineas_originales": lns, "lineas_normalizadas": lns_n, "es_admin": True, "fecha_creacion": datetime.now(), "fecha_expiracion": exp, "provincia": "Santiago de Cuba", "hash": h})
    
    # Actualizar el contador para el próximo admin
    datos["siguiente_num_admin"] = num_admin + 1
    guardar_config(datos)
    
    await update.message.reply_text(f"✅ {len(lns)} líneas.\n📞 {esc(tel)}\n📛 Publicado como: {esc(nombre_admin)}", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await enviar_menu_msg(update, uid); context.user_data["estado"] = None; context.user_data["admin_tel"] = None

async def destacar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id): return
    if not context.args or len(context.args) < 2: return await update.message.reply_text("<code>/destacar ID DIAS</code>", parse_mode="HTML")
    pid = context.args[0]
    try: d = int(context.args[1])
    except: return await update.message.reply_text("❌ Días inválidos.")
    if not await coleccion_proveedores.find_one({"_id": pid}): return await update.message.reply_text("❌ No encontrado.")
    await coleccion_proveedores.update_one({"_id": pid}, {"$set": {"destacado_hasta": datetime.now() + timedelta(days=d)}})
    await update.message.reply_text(f"✅ Destacado {d} días.", parse_mode="HTML")

async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id) != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("<code>/addadmin ID</code>", parse_mode="HTML")
    try: na = int(context.args[0])
    except: return await update.message.reply_text("ID numérico.")
    if na not in datos.get("administradores", [ADMIN_ID]): datos.setdefault("administradores", [ADMIN_ID]).append(na); guardar_config(datos); await update.message.reply_text(f"✅ Admin {na}")
    else: await update.message.reply_text("Ya lo es.")

async def del_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id) != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("<code>/deladmin ID</code>", parse_mode="HTML")
    try: da = int(context.args[0])
    except: return await update.message.reply_text("ID numérico.")
    if da == ADMIN_ID: return await update.message.reply_text("No puedes eliminarte.")
    if da in datos.get("administradores", []): datos["administradores"].remove(da); guardar_config(datos); await update.message.reply_text(f"✅ Eliminado {da}")

async def anuncio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id): return
    if not context.args or len(context.args) < 2: return await update.message.reply_text("<code>/anuncio add texto</code>\n<code>/anuncio del N</code>", parse_mode="HTML")
    a = context.args[0].lower(); t = " ".join(context.args[1:])
    datos.setdefault("anuncios", [])
    if a == "add": datos["anuncios"].append(t); guardar_config(datos); await update.message.reply_text("✅ Añadido.")
    elif a == "del":
        try: datos["anuncios"].pop(int(t) - 1); guardar_config(datos); await update.message.reply_text("✅ Eliminado.")
        except: await update.message.reply_text("N inválido.")

# ===== HEALTH CHECK =====
class HCH(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-type', 'text/plain'); self.end_headers(); self.wfile.write(b'OK')
    def log_message(self, fmt, *a): pass
def iniciar_hc():
    p = int(os.environ.get('PORT', 10000))
    try:
        s = HTTPServer(('0.0.0.0', p), HCH); threading.Thread(target=s.serve_forever, daemon=True).start(); logger.info(f"✅ HC puerto {p}")
    except Exception as e: logger.error(f"HC error: {e}")

# ===== MAIN =====
async def post_init(app):
    try: await app.bot.delete_webhook(drop_pending_updates=True)
    except: pass
    try:
        await coleccion_catalogos.create_index([("provincia", 1)])
        await coleccion_catalogos.create_index([("proveedor_id", 1)])
    except: pass

def main():
    if not TOKEN or not MONGODB_URI: logger.error("🛑 Faltan vars."); return
    iniciar_hc()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CommandHandler("cancel", cancelar))
    app.add_handler(CommandHandler("admin_cargar_listado", admin_cargar))
    app.add_handler(CommandHandler("destacar", destacar_cmd))
    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("deladmin", del_admin_cmd))
    app.add_handler(CommandHandler("anuncio", anuncio_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, proc_msgs))
    logger.info(f"🤖 MediCuba {VERSION}")
    le = 0
    while True:
        try: app.run_polling(drop_pending_updates=True)
        except Exception as e:
            n = time.time()
            if n - le > 30: logger.error(f"Polling: {e}"); le = n
            time.sleep(10)

if __name__ == "__main__":
    main()
