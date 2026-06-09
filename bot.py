"""
BOT DE MEDICINAS CUBA - VERSIÓN PROFESIONAL MONGODB
v3.0.1 | Fix Ayuda | Fix Referidos Perfil | Notificaciones Admin
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
from zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient
from rapidfuzz import fuzz as rfuzz

# ===== CONFIGURACIÓN =====
VERSION = "v3.0.1"
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = "814338625" 
ADMIN_USERNAME = "TuUsuarioAqui"

if not TOKEN:
    logger.error("❌ FATAL: BOT_TOKEN no configurado.")

MAX_LINEAS_CATALOGO = 80
MAX_CATALOGOS_PROVEEDOR = 2
DIAS_EXPIRACION = 20 
UMBRAL_FUZZY = 70
BOT_LINK = "https://t.me/MediCubaBot"
TZ_CUBA = ZoneInfo("America/Havana")

BLACKLIST = ["zapatos", "ropa", "joyas", "comida", "pollo", "arroz", "telefono", "casa", "carro", "zapatillas", "frutas", "viveres"]

# ===== CONEXIÓN MONGODB =====
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    logger.error("❌ FATAL: MONGODB_URI no configurado.")

try:
    client = AsyncIOMotorClient(MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000, retryWrites=True)
    db = client.medicubadb
    coleccion_clientes = db.clientes
    coleccion_proveedores = db.proveedores
    coleccion_catalogos = db.catalogos
    coleccion_config = db.config
except Exception as e:
    logger.error(f"❌ Error conectando MongoDB: {e}")

PROVINCIAS = ["Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas", "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila", "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba", "Guantánamo", "Isla de la Juventud"]

# ===== CONFIG EN BD =====
datos = {}

TEXTOS_INICIALES = {
    "reglas": "1. Solo medicinas y productos médicos.\n2. Medicinas de procedencia lícita.\n3. No publicar si no eres proveedor real (estanca).\n4. Respetar a los clientes.\n5. Si detectas un listado falso o proveedor que no responde, repórtalo usando el botón 🚨 en los resultados de búsqueda.",
    "referidos": "🎁 PROMO REFERIDOS (Hasta 10/07/2026 o 500 refs)\n\nGana dinero refiriendo usuarios nuevos que configuren su provincia y hagan 1 búsqueda.\n\n💰 10 refs = 50 CUP\n💰 20 refs = 100 CUP\n💰 30 refs = 150 CUP\n💰 40 refs = 200 CUP\n💰 50 refs = 250 CUP\n\n⚠️ Límite: 50 referidos por persona. 500 total en la promo."
}

async def init_config():
    global datos
    try:
        doc = await coleccion_config.find_one({"_id": "main_config"})
        if not doc:
            doc = {"_id": "main_config", "administradores": [int(ADMIN_ID)], "anuncios": [], "siguiente_num_admin": 1, "promo_activa": True, "referidos_global": 0, "textos": TEXTOS_INICIALES}
            await coleccion_config.insert_one(doc)
        defaults = {"administradores": [int(ADMIN_ID)], "anuncios": [], "siguiente_num_admin": 1, "promo_activa": True, "referidos_global": 0, "textos": TEXTOS_INICIALES}
        for k, v in defaults.items():
            if k not in doc: doc[k] = v
        if "reglas" not in doc.get("textos", {}) or "referidos" not in doc.get("textos", {}):
            doc.setdefault("textos", {}).update(TEXTOS_INICIALES)
        datos = doc
    except Exception as e:
        logger.error(f"❌ Error cargando config: {e}. Usando defaults.")
        datos = {"_id": "main_config", "administradores": [int(ADMIN_ID)], "anuncios": [], "siguiente_num_admin": 1, "promo_activa": True, "referidos_global": 0, "textos": TEXTOS_INICIALES}

async def guardar_config(c):
    global datos
    datos = c
    save_data = {k: v for k, v in c.items() if k != "_id"}
    try:
        await coleccion_config.update_one({"_id": "main_config"}, {"$set": save_data}, upsert=True)
    except Exception as e:
        logger.error(f"❌ Error guardando config: {e}")

# ===== AUXILIARES =====
def esc(t):
    if t is None: return ""
    return html.escape(str(t))

def normalizar_texto(t):
    t = t.lower()
    for a, b in {'á':'a','é':'e','í':'i','ó':'o','ú':'u','ü':'u'}.items(): t = t.replace(a, b)
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', t)).strip()

def eliminar_emojis(t):
    patron_emoji = re.compile("[" u"\U0001F600-\U0001F64F" u"\U0001F300-\U0001F5FF" u"\U0001F680-\U0001F6FF" u"\U0001F1E0-\U0001F1FF" u"\U00002702-\U000027B0" u"\U000024C2-\U0001F251" u"\U0001f926-\U0001f937" u"\U00010000-\U0010ffff" u"\u2640-\u2642" u"\u2600-\u2B55" u"\u200d" u"\u23cf" u"\u23e9" u"\u231a" u"\ufe0f" u"\u3030" "]+", flags=re.UNICODE)
    return patron_emoji.sub('', t)

def es_admin(uid): return int(uid) in datos.get("administradores", [int(ADMIN_ID)])

async def esta_baneado(uid):
    try:
        doc = await coleccion_clientes.find_one({"_id": uid}, {"baneado": 1})
        return doc and doc.get("baneado", False)
    except:
        return False

async def es_destacado(p):
    if not p or not p.get("destacado_hasta"): return False
    try: return datetime.now(TZ_CUBA) < p["destacado_hasta"]
    except: return False

def contiene_no_medicos(t):
    tn = normalizar_texto(t)
    for p in BLACKLIST:
        if re.search(r'\b'+re.escape(p)+r'\b', tn): return True
    return False

def generar_hash(t): return hashlib.md5(t.encode('utf-8')).hexdigest()

async def limpiar_expirados():
    try:
        r = await coleccion_catalogos.delete_many({"fecha_expiracion": {"$lt": datetime.now(TZ_CUBA)}})
        if r.deleted_count: logger.info(f"🗑️ Purgados {r.deleted_count} expirados")
    except: pass

async def get_lineas_por_provincia():
    try:
        pipeline = [{"$match": {"lineas_originales": {"$exists": True}}}, {"$project": {"provincia": 1, "count": {"$size": "$lineas_originales"}}}, {"$group": {"_id": "$provincia", "total": {"$sum": "$count"}}}]
        results = {}
        async for doc in coleccion_catalogos.aggregate(pipeline):
            if doc["_id"]: results[doc["_id"]] = doc["total"]
        return results
    except: return {}

async def get_ultima_actualizacion():
    try:
        cat = await coleccion_catalogos.find_one(sort=[("fecha_creacion", -1)])
        if cat and cat.get("fecha_creacion"):
            fecha_local = cat["fecha_creacion"].astimezone(TZ_CUBA)
            if fecha_local.date() == datetime.now(TZ_CUBA).date(): return f"🕐 Actualizado: {fecha_local.strftime('%d/%b %H:%M')}\n"
        return ""
    except: return ""

# ===== REFERIDOS =====
async def validar_referido(uid, context):
    try:
        user = await coleccion_clientes.find_one({"_id": uid})
        if not user or user.get("referido_validado") or not user.get("referido_por"): return
        
        if user.get("provincia") and user.get("busquedas", 0) >= 1:
            ref_id = user["referido_por"]
            await coleccion_clientes.update_one({"_id": uid}, {"$set": {"referido_validado": True}})
            
            if not datos.get("promo_activa", False): return
            
            await coleccion_clientes.update_one({"_id": ref_id}, {"$inc": {"referidos_count": 1}})
            referrer = await coleccion_clientes.find_one({"_id": ref_id})
            count = referrer.get("referidos_count", 0) + 1
            
            if count in [10, 20, 30, 40, 50]:
                try: await context.bot.send_message(ref_id, "🎉 ¡Felicidades! Has ganado un premio por tus referidos. Contacta a Soporte para reclamarlo.", parse_mode="HTML")
                except: pass
                
            datos["referidos_global"] = datos.get("referidos_global", 0) + 1
            global_count = datos["referidos_global"]
            await guardar_config(datos)
            
            if global_count in [300, 400]:
                try: await context.bot.send_message(ADMIN_ID, f"📢 <b>Promo Referidos</b>\n\nSe han alcanzado {global_count} referidos globales.", parse_mode="HTML")
                except: pass
            elif global_count >= 500:
                await desactivar_promo(context)
    except Exception as e:
        logger.error(f"Error validando referido: {e}")

async def desactivar_promo(context):
    datos["promo_activa"] = False
    await guardar_config(datos)
    try:
        await coleccion_clientes.update_many({}, {"$set": {"referidos_count": 0}})
        await context.bot.send_message(ADMIN_ID, "🏁 <b>Promo de Referidos Desactivada</b>\n\nSe alcanzaron los 500 referidos globales. Los contadores han sido reseteados.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error desactivando promo: {e}")

# ===== MENÚS =====
def menu_principal(uid, prov, anuncio_idx=0, ultima_act=""):
    a_list = datos.get("anuncios", [])
    msg_anuncio = ""; tk_anuncio = []
    if a_list:
        msg_anuncio = f"\n\n📢 {a_list[anuncio_idx]}"
        if len(a_list) > 1:
            prev_idx = (anuncio_idx - 1) % len(a_list); next_idx = (anuncio_idx + 1) % len(a_list)
            tk_anuncio.append([InlineKeyboardButton("⬅️", callback_data=f"anuncio_{prev_idx}"), InlineKeyboardButton(f"{anuncio_idx+1}/{len(a_list)}", callback_data="ignore"), InlineKeyboardButton("➡️", callback_data=f"anuncio_{next_idx}")])

    tk = [
        [InlineKeyboardButton("🔍 Buscar", callback_data="buscar"), InlineKeyboardButton("📝 Publicar", callback_data="publicar")],
        [InlineKeyboardButton("📍 Provincia", callback_data="cambiar_provincia"), InlineKeyboardButton("👤 Perfil", callback_data="mi_perfil")],
        [InlineKeyboardButton("⭐ Destacados", callback_data="destacados"), InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")],
        [InlineKeyboardButton("📋 Compartir", callback_data="compartir"), InlineKeyboardButton("📞 Soporte", callback_data="soporte")]
    ]
    if es_admin(uid): tk.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    final_tk = tk_anuncio + tk
    ver = VERSION.replace('v', ''); title_str = f"MediCuba{ ' ' * (22 - len('MediCuba') - len(ver)) }{ver}"
    t = f"<code>{title_str}</code>\n🩺 Tu salud, nostra prioridad\n\n{ultima_act}📍 <b>Provincia:</b> {esc(prov)}{msg_anuncio}"
    return t, InlineKeyboardMarkup(final_tk)

def menu_post_busqueda():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")]])

async def enviar_menu_cb(q, uid, anuncio_idx=0):
    doc = await coleccion_clientes.find_one({"_id": uid})
    p = doc.get("provincia", "No seleccionada") if doc else "No seleccionada"
    ult = await get_ultima_actualizacion(); t, tk = menu_principal(uid, p, anuncio_idx, ult)
    try: await q.edit_message_text(t, reply_markup=tk, parse_mode="HTML")
    except: await q.message.reply_text(t, reply_markup=tk, parse_mode="HTML")

async def enviar_menu_msg(upd, uid, anuncio_idx=0):
    doc = await coleccion_clientes.find_one({"_id": uid})
    p = doc.get("provincia", "No seleccionada") if doc else "No seleccionada"
    ult = await get_ultima_actualizacion(); t, tk = menu_principal(uid, p, anuncio_idx, ult)
    await upd.message.reply_text(t, reply_markup=tk, parse_mode="HTML")

# ===== COMANDOS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    user = update.effective_user
    referido_por = None
    
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            ref_id = arg.replace("ref_", "")
            if ref_id != uid: referido_por = ref_id
        elif arg.startswith("proveedor_"):
            pid = arg.replace("proveedor_", "")
            await mostrar_cat_prov(update, pid); return

    try:
        update_fields = {"nombre": user.first_name, "username": user.username, "ultima_actividad": datetime.now(TZ_CUBA)}
        set_on_insert = {"provincia": None, "busquedas": 0, "fecha_registro": datetime.now(TZ_CUBA), "baneado": False, "referido_validado": False}
        if referido_por: set_on_insert["referido_por"] = referido_por
        
        result = await coleccion_clientes.update_one({"_id": uid}, {"$set": update_fields, "$setOnInsert": set_on_insert}, upsert=True)
        
        if result.upserted_id:
            try: await context.bot.send_message(ADMIN_ID, f"👋 <b>Nuevo usuario registrado</b>\n👤 {esc(user.first_name)}\n🆔 <code>{uid}</code>", parse_mode="HTML")
            except: pass
            
        if referido_por and not result.upserted_id:
            await coleccion_clientes.update_one({"_id": uid, "referido_por": {"$exists": False}}, {"$set": {"referido_por": referido_por}})
            
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
    conteos = await get_lineas_por_provincia()
    lista = "\n".join([f"{i+1}. {p} ({conteos.get(p, 0)})" for i, p in enumerate(PROVINCIAS)])
    tk = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_forzado")]]
    await update.message.reply_text(f"👋 ¡Bienvenido!\n\n📍 Selecciona:\n\n{lista}\n\nNÚMERO:", reply_markup=InlineKeyboardMarkup(tk))

async def mostrar_cat_prov(update, pid):
    prov = await coleccion_proveedores.find_one({"_id": pid})
    if not prov: return await update.message.reply_text("❌ No encontrado.")
    cats = await coleccion_catalogos.find({"proveedor_id": pid}).to_list(None)
    if not cats: return await update.message.reply_text("📭 Sin catálogos.")
    msg = f"🏥 <b>{esc(prov.get('nombre'))}</b>\n📞 {esc(prov.get('contacto_mostrar', 'N/A'))}\n" + "─"*20 + "\n\n"
    for i, c in enumerate(cats, 1):
        msg += f"<b>Catálogo {i}:</b>\n"
        for l in c["lineas_originales"][:30]: msg += f"• {esc(l)}\n"
        msg += "\n"
    msg += "─"*20 + "\n🩺 MediCuba"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Ir al Bot", url="https://t.me/MediCubaBot")]]), parse_mode="HTML")

# ===== CALLBACKS =====
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = str(q.from_user.id); d = q.data
    
    if await esta_baneado(uid):
        await q.answer("🚫 Estás baneado", show_alert=True); return
        
    if d == "ignore": return
    elif d.startswith("anuncio_"):
        idx = int(d.split("_")[1]); await enviar_menu_cb(q, uid, anuncio_idx=idx)
    elif d == "volver" or d == "volver_forzado": 
        context.user_data["estado"] = None; await enviar_menu_cb(q, uid)
    elif d == "compartir":
        tk_back = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver al Menú", callback_data="volver")]])
        await q.message.reply_text("📋 <b>Copia el enlace para compartir MediCuba:</b>\n\n<code>t.me/MediCubaBot</code>", reply_markup=tk_back, parse_mode="HTML")
    elif d == "soporte":
        context.user_data["estado"] = "soporte_esperando_msg"
        await q.edit_message_text("📞 <b>Soporte MediCuba</b>\n\nEscribe tu duda o propuesta. El equipo te responderá aquí.", parse_mode="HTML")
        await context.bot.send_message(uid, "Escribe tu mensaje:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="volver")]]))
    elif d.startswith("reply_") and es_admin(uid):
        target_uid = d.split("_")[1]; context.user_data["reply_to"] = target_uid; context.user_data["estado"] = "admin_esperando_reply"
        await q.edit_message_text("✉️ <b>Responder</b>\n\nEscribe la respuesta para el usuario:", parse_mode="HTML")
    elif d.startswith("reportar_"):
        pid = d.split("_")[1]
        try: await context.bot.send_message(ADMIN_ID, f"🚨 <b>Reporte de Proveedor</b>\n🆔 ID: <code>{pid}</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔨 Banear", callback_data=f"ban_{pid}")], [InlineKeyboardButton("✅ Ignorar", callback_data="ignore")]]), parse_mode="HTML")
        except: pass
        await q.answer("✅ Reporte enviado al Admin", show_alert=True)
    elif d.startswith("ban_") and es_admin(uid):
        pid = d.split("_")[1]
        await coleccion_clientes.update_one({"_id": pid}, {"$set": {"baneado": True}}, upsert=True)
        await coleccion_proveedores.update_one({"_id": pid}, {"$set": {"baneado": True}}, upsert=True)
        await q.answer("✅ Usuario baneado", show_alert=True)
        await q.edit_message_text(f"✅ <code>{pid}</code> ha sido baneado.", parse_mode="HTML")
    elif d.startswith("sel_"):
        idx = int(d.split("_")[1]) - 1; sugs = context.user_data.get("sugs", [])
        if 0 <= idx < len(sugs):
            o = sugs[idx]; prv = o["p"]; dest = "⭐ " if await es_destacado(prv) else ""; nombre_prov = prv.get('nombre', 'Prov')
            msg = f"🏥 {dest}<b>{esc(nombre_prov)}</b>\n📞 {esc(prv.get('contacto_mostrar'))}\n\n💊 {esc(o['l'])}\n"
            botones = []; contacto = prv.get("contacto", {}); tel_wa = contacto.get("whatsapp", "").replace("+", "").replace(" ", ""); tel_tg = contacto.get("telegram", ""); search_term = context.user_data.get("last_search", "esto")
            if tel_wa:
                wa_msg = f"🏥 MediCuba (t.me/MediCubaBot)\n\nHola, ¿Tienes disponible {search_term}?"; wa_url = f"https://wa.me/{tel_wa}?text={wa_msg.replace(' ', '%20')}"
                botones.append([InlineKeyboardButton(f"💬 Ir WhatsApp: {esc(nombre_prov)}", url=wa_url)])
            if tel_tg:
                tg_url = f"https://t.me/{tel_tg.replace('@','')}"; botones.append([InlineKeyboardButton(f"✈️ Ir Telegram: {esc(nombre_prov)}", url=tg_url)])
            botones.append([InlineKeyboardButton("🚨 Reportar", callback_data=f"reportar_{prv['_id']}")])
            botones.append([InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
            await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        else: await q.answer("Selección inválida", show_alert=True)
    elif d == "buscar":
        c = await coleccion_clientes.find_one({"_id": uid})
        if not c or not c.get("provincia"): return await q.edit_message_text("❌ Configura provincia.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📍 Configurar", callback_data="cambiar_provincia")]]))
        context.user_data["estado"] = "esperando_medicina"; await q.edit_message_text("🔍 <b>Buscar Medicina</b>", parse_mode="HTML")
        tk_back = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver al Menú", callback_data="volver")]])
        await context.bot.send_message(uid, "Escribe el nombre:\n\n<i>Ej: gravinol</i>", reply_markup=tk_back, parse_mode="HTML")
    elif d == "publicar":
        context.user_data["estado"] = "esperando_listado"; await q.edit_message_text("📝 <b>Publicar Catálogo</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "📋 Pega tu listado (máx 80 líneas, solo medicinas).", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"], ["❌ Cancelar"]], resize_keyboard=True), parse_mode="HTML")
    elif d == "cambiar_provincia":
        context.user_data["estado"] = "cambiando_provincia"; conteos = await get_lineas_por_provincia()
        lista = "\n".join([f"{i+1}. {p} ({conteos.get(p, 0)})" for i, p in enumerate(PROVINCIAS)])
        await q.edit_message_text("📍 <b>Cambiar Provincia</b>", parse_mode="HTML")
        tk_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        await context.bot.send_message(uid, f"{lista}\n\nNÚMERO:", reply_markup=tk_back)
    elif d == "mi_perfil": await _perfil(q, uid)
    elif d == "editar_contacto":
        context.user_data["editando_contacto"] = True; tk = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp"), InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram"), InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")], [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]
        await q.edit_message_text("✏️ <b>Editar Contacto</b>", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")
    elif d == "ver_mi_catalogo": await _mi_cat(q, uid)
    elif d == "destacados": await _destacados(q)
    elif d == "ayuda": await _ayuda(q)
    elif d.startswith("ayuda_"): await _ayuda_det(q, d)
    elif d.startswith("contacto_"): await _contacto_cb(q, context, d)
    elif d == "perfil_referidos":
        link = f"https://t.me/MediCubaBot?start=ref_{uid}"
        msg = f"🎁 <b>Invita y Gana</b>\n\nComparte este enlace:\n<code>{link}</code>\n\nCuando tus amigos configuren su provincia y hagan su primera búsqueda, sumarás referidos."
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="mi_perfil")]]), parse_mode="HTML")
    elif d == "admin_panel" and es_admin(uid): await _admin_panel(q)
    elif d.startswith("admin_") and es_admin(uid): await _admin_acc(q, context, uid, d)

# ===== VISTAS =====
async def _perfil(q, uid):
    prov = await coleccion_proveedores.find_one({"_id": uid})
    if prov:
        cc = await coleccion_catalogos.count_documents({"proveedor_id": uid})
        msg = f"👤 <b>Perfil Proveedor</b>\n\n📛 {esc(prov.get('nombre'))}\n📞 {esc(prov.get('contacto_mostrar'))}\n📋 Catálogos: {cc}/2\n"
        if prov.get('link_token'): msg += f"🔗 <code>t.me/MediCubaBot?start=proveedor_{uid}</code>\n"
        tk = [[InlineKeyboardButton("✏️ Editar Contacto", callback_data="editar_contacto")]]
    else:
        c = await coleccion_clientes.find_one({"_id": uid})
        msg = f"👤 <b>Perfil Cliente</b>\n\n📍 {esc(c.get('provincia', 'N/A'))}\n📊 Búsquedas: {c.get('busquedas', 0)}\n"
        tk = []
        
    if datos.get("promo_activa", False):
        tk.append([InlineKeyboardButton("🎁 Invitar y Ganar", callback_data="perfil_referidos")])
    tk.append([InlineKeyboardButton("🏠 Volver", callback_data="volver")])
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _mi_cat(q, uid):
    cats = await coleccion_catalogos.find({"proveedor_id": uid}).sort("fecha_creacion", 1).to_list(None)
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
    provs = await coleccion_proveedores.find({"destacado_hasta": {"$gt": datetime.now(TZ_CUBA)}}).to_list(None)
    if not provs: return await q.edit_message_text("⭐ Sin destacados.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]), parse_mode="HTML")
    msg = "⭐ <b>DESTACADOS</b> ⭐\n\n"
    for p in provs[:5]:
        l = f"t.me/MediCubaBot?start=proveedor_{p['_id']}"; msg += f"🏥 <b>{esc(p.get('nombre'))}</b>\n📞 {esc(p.get('contacto_mostrar'))}\n🔗 <a href='{l}'>Ver</a>\n\n"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Volver", callback_data="volver")]]), parse_mode="HTML", disable_web_page_preview=True)

async def _ayuda(q):
    tk = [
        [InlineKeyboardButton("👨‍💼 Proveedores", callback_data="ayuda_prov")], 
        [InlineKeyboardButton("🛒 Clientes", callback_data="ayuda_cli")], 
        [InlineKeyboardButton("⚙️ General", callback_data="ayuda_gen")], 
        [InlineKeyboardButton("📜 Reglas", callback_data="ayuda_reglas")]
    ]
    if datos.get("promo_activa", False): 
        tk.append([InlineKeyboardButton("🎁 Referidos", callback_data="ayuda_referidos")])
    tk.append([InlineKeyboardButton("📞 Soporte", callback_data="soporte")])
    tk.append([InlineKeyboardButton("🏠 Volver", callback_data="volver")])
    await q.edit_message_text("❓ <b>Ayuda</b>", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _ayuda_det(q, d):
    lnk = f"\n\n🔗 Comparte: <code>t.me/MediCubaBot</code>"
    ts = {
        "ayuda_prov": f"👨‍💼 <b>Proveedores</b>\n\nSube hasta 2 listados (80 líneas) con copiar y pegar.\n\n⭐ <b>Estrellas:</b> Destacados aparecen PRIMERO y generan 3x más contactos.\n\n📞 Registra WhatsApp, Telegram o ambos.{lnk}", 
        "ayuda_cli": f"🛒 <b>Clientes</b>\n\nBuscador inteligente que acepta errores.\n\n⭐ <b>Estrellas:</b> Los ⭐ son los más confiables.\n\n📱 Contacta por WhatsApp o Telegram directo.{lnk}", 
        "ayuda_gen": f"⚙️ <b>General</b>\n\n🩺 MediCuba conecta pacientes con proveedores directo.\n\nConfigura provincia una vez, cámbiala cuando viajes.{lnk}",
        "ayuda_reglas": datos.get("textos", {}).get("reglas", "No configurado."),
        "ayuda_referidos": datos.get("textos", {}).get("referidos", "No configurado.")
    }
    await q.edit_message_text(ts.get(d, ""), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📞 Soporte", callback_data="soporte")], [InlineKeyboardButton("🔙", callback_data="ayuda")]]), parse_mode="HTML")

async def _contacto_cb(q, context, d):
    tipo = d.replace("contacto_", ""); context.user_data["tipo_contacto"] = tipo
    if tipo in ["whatsapp", "ambos"]:
        context.user_data["estado"] = "esperando_telefono"; await q.edit_message_text("📱 <b>WhatsApp</b>", parse_mode="HTML")
        await context.bot.send_message(q.from_user.id, "Escribe tu número (ej: <code>+53 5 1234567</code>):", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True), parse_mode="HTML")
    else:
        context.user_data["estado"] = "esperando_telegram"; await q.edit_message_text("✈️ <b>Telegram</b>", parse_mode="HTML")
        await context.bot.send_message(q.from_user.id, "Escribe tu @usuario:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))

async def _admin_panel(q):
    tk = [
        [InlineKeyboardButton("📥 Cargar", callback_data="admin_cargar"), InlineKeyboardButton("➕ Añadir Admin", callback_data="admin_add_admin")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats"), InlineKeyboardButton("👥 Provs", callback_data="admin_provs")],
        [InlineKeyboardButton("⭐ Destacar", callback_data="admin_dest"), InlineKeyboardButton("📢 Anuncios", callback_data="admin_anuncios")],
        [InlineKeyboardButton("✉️ Enviar Mensaje", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🎁 Promo Referidos", callback_data="admin_promo"), InlineKeyboardButton("📢 Textos", callback_data="admin_textos")],
        [InlineKeyboardButton("🔨 Banear", callback_data="admin_ban"), InlineKeyboardButton("🔓 Desbanear", callback_data="admin_unban")],
        [InlineKeyboardButton("🏠 Volver", callback_data="volver")]
    ]
    await q.edit_message_text("🔧 <b>Admin</b>", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _admin_acc(q, context, uid, d):
    if d == "admin_cargar":
        context.user_data["estado"] = "admin_esperando_tel"; await q.edit_message_text("📥 <b>Cargar Listado</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "WhatsApp del listado (ej: <code>+5351234567</code>):", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True), parse_mode="HTML")
    elif d == "admin_add_admin":
        context.user_data["estado"] = "admin_esperando_id_nuevo"; await q.edit_message_text("➕ <b>Añadir Admin</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "Envía el ID numérico del nuevo administrador:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))
    elif d == "admin_stats": await _admin_stats(q)
    elif d == "admin_provs": await _admin_provs(q)
    elif d == "admin_dest": await _admin_dest(q)
    elif d == "admin_anuncios": await _admin_anun(q)
    elif d == "admin_broadcast": await _admin_broadcast_ini(q)
    elif d == "admin_promo": await _admin_promo(q)
    elif d == "admin_textos": await _admin_textos(q)
    elif d == "admin_ban":
        context.user_data["estado"] = "admin_esperando_ban_id"; await q.edit_message_text("🔨 <b>Banear Usuario</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "Envía el ID del usuario a banear:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))
    elif d == "admin_unban":
        context.user_data["estado"] = "admin_esperando_unban_id"; await q.edit_message_text("🔓 <b>Desbanear Usuario</b>", parse_mode="HTML")
        await context.bot.send_message(uid, "Envía el ID del usuario a desbanear:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))

async def _admin_stats(q):
    await limpiar_expirados()
    cp = await coleccion_proveedores.count_documents({}); cc = await coleccion_clientes.count_documents({}); cca = await coleccion_catalogos.count_documents({})
    msg = f"📊 Clientes: {cc}\n🏥 Proveedores: {cp}\n📋 Catálogos: {cca}\n🛡️ Admins: {len(datos.get('administradores',[]))}"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_provs(q):
    ps = await coleccion_proveedores.find({}).to_list(None); msg = "👥 <b>Proveedores</b>\n\n"
    for p in ps[:10]:
        c = await coleccion_catalogos.count_documents({"proveedor_id": p["_id"]}); msg += f"• <code>{p['_id']}</code> - {esc(p.get('nombre'))} ({c})\n"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_dest(q):
    ps = await coleccion_proveedores.find({}).to_list(None); msg = "⭐ <code>/destacar ID DIAS</code>\n\n"
    for p in ps[:15]: msg += f"• <code>{p['_id']}</code> - {esc(p.get('nombre'))}\n"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_anun(q):
    ans = datos.get("anuncios", []); msg = "📢 <b>Anuncios</b> (rotan con flechas en menú):\n\n"
    if not ans: msg += "<i>Vacío</i>"
    else: 
        for i, a in enumerate(ans, 1): msg += f"{i}. {esc(a)}\n"
    msg += "\n<code>/anuncio add texto</code>\n<code>/anuncio del N</code>"
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

async def _admin_broadcast_ini(q):
    tk = [[InlineKeyboardButton("🏥 A Proveedores", callback_data="broadcast_prov")], [InlineKeyboardButton("🛒 A Clientes", callback_data="broadcast_cli")], [InlineKeyboardButton("🔙", callback_data="admin_panel")]]
    await q.edit_message_text("✉️ <b>Enviar Mensaje</b>\n\n¿A quién deseas enviarle un mensaje?", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _admin_promo(q):
    estado = "✅ Activa" if datos.get("promo_activa", False) else "❌ Inactiva"
    total = datos.get("referidos_global", 0)
    msg = f"🎁 <b>Promo Referidos</b>\n\nEstado: {estado}\nGlobales: {total}/500\n\n<b>Top 5 Referidores:</b>\n"
    try:
        top5 = await coleccion_clientes.find({"referidos_count": {"$gt": 0}}).sort("referidos_count", -1).limit(5).to_list(None)
        for i, u in enumerate(top5, 1): msg += f"{i}. <code>{u['_id']}</code> ({u.get('referidos_count', 0)} refs)\n"
    except: msg += "Error al cargar top."
    
    tk = [[InlineKeyboardButton("🛑 Detener y Resetear", callback_data="admin_stop_promo")], [InlineKeyboardButton("🔙", callback_data="admin_panel")]]
    if not datos.get("promo_activa"): tk.pop(0)
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

async def _admin_textos(q):
    tk = [[InlineKeyboardButton("📜 Editar Reglas", callback_data="admin_edit_reglas")], [InlineKeyboardButton("🎁 Editar Referidos", callback_data="admin_edit_referidos")], [InlineKeyboardButton("🔙", callback_data="admin_panel")]]
    await q.edit_message_text("📢 <b>Textos Dinámicos</b>", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

# ===== MENSAJES =====
async def proc_msgs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); txt = update.message.text
    
    if await esta_baneado(uid):
        await update.message.reply_text("🚫 Estás baneado"); return
        
    if txt in ["🔙 Volver al Menú", "❌ Cancelar", "/cancelar", "/cancel"]:
        context.user_data["estado"] = None; await update.message.reply_text("↩️", reply_markup=ReplyKeyboardRemove())
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
        elif est == "admin_esperando_broadcast_msg": await _admin_broadcast_msg(update, context, uid, txt)
        elif est == "admin_esperando_reply": await _admin_reply(update, context, uid, txt)
        elif est == "soporte_esperando_msg": await _soporte_msg(update, context, uid, txt)
        elif est == "esperando_seleccion": await _seleccion(update, context, uid, txt)
        elif est == "admin_esperando_ban_id": await _admin_ban_msg(update, context, uid, txt)
        elif est == "admin_esperando_unban_id": await _admin_unban_msg(update, context, uid, txt)
        elif est == "admin_esperando_texto_reglas": await _admin_edit_text(update, context, uid, txt, "reglas")
        elif est == "admin_esperando_texto_referidos": await _admin_edit_text(update, context, uid, txt, "referidos")
        else: context.user_data["estado"] = None
    except Exception as e:
        logger.error(f"Error: {e}"); context.user_data["estado"] = None
        await update.message.reply_text("⚠️ Error.", reply_markup=ReplyKeyboardRemove())

async def _cambiar_prov(update, context, uid, txt):
    try:
        n = int(txt.strip())
        if 1 <= n <= len(PROVINCIAS):
            pr = PROVINCIAS[n-1]
            user_doc = await coleccion_clientes.find_one({"_id": uid})
            first_time = user_doc.get("provincia") is None
            
            await coleccion_clientes.update_one({"_id": uid}, {"$set": {"provincia": pr}})
            await update.message.reply_text(f"✅ {esc(pr)}", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
            
            if first_time:
                try: await context.bot.send_message(ADMIN_ID, f"📍 <b>Nuevo usuario configuró provincia</b>\n👤 {esc(user_doc.get('nombre', 'N/A'))}\n📍 {esc(pr)}\n🆔 <code>{uid}</code>", parse_mode="HTML")
                except: pass
                await validar_referido(uid, context)
                
            context.user_data["estado"] = None; return await enviar_menu_msg(update, uid)
        else: raise ValueError
    except ValueError: await update.message.reply_text("Número inválido.")

async def _admin_add_id_msg(update, context, uid, txt):
    try:
        na = int(txt.strip())
        if na not in datos.get("administradores", [int(ADMIN_ID)]):
            datos.setdefault("administradores", [int(ADMIN_ID)]).append(na); await guardar_config(datos)
            await update.message.reply_text(f"✅ Admin {na} añadido.", reply_markup=ReplyKeyboardRemove())
        else: await update.message.reply_text("Ese ID ya es administrador.", reply_markup=ReplyKeyboardRemove())
    except ValueError: await update.message.reply_text("ID inválido. Debe ser numérico.", reply_markup=ReplyKeyboardRemove())
    context.user_data["estado"] = None; await enviar_menu_msg(update, uid)

# ===== SOPORTE =====
async def _soporte_msg(update, context, uid, txt):
    user = update.effective_user; msg = f"📩 <b>Mensaje de Soporte</b>\n👤 {esc(user.first_name)} (<code>{uid}</code>)\n\n{esc(txt)}"
    tk = [[InlineKeyboardButton("✉️ Responder", callback_data=f"reply_{uid}")]]
    await context.bot.send_message(ADMIN_ID, msg, reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")
    await update.message.reply_text("✅ Mensaje enviado. Te responderemos pronto.", reply_markup=ReplyKeyboardRemove())
    context.user_data["estado"] = None; await enviar_menu_msg(update, uid)

async def _admin_reply(update, context, uid, txt):
    target_uid = context.user_data.get("reply_to")
    if not target_uid: return
    try:
        await context.bot.send_message(target_uid, f"📩 <b>Respuesta de MediCuba:</b>\n\n{esc(txt)}", parse_mode="HTML")
        await update.message.reply_text("✅ Respuesta enviada.", reply_markup=ReplyKeyboardRemove())
    except Exception as e: await update.message.reply_text(f"❌ Error al enviar: {e}", reply_markup=ReplyKeyboardRemove())
    context.user_data["estado"] = None; context.user_data["reply_to"] = None

# ===== BÚSQUEDA =====
async def _busqueda(update, context, uid, txt):
    mb = normalizar_texto(txt); context.user_data["last_search"] = txt
    c = await coleccion_clientes.find_one({"_id": uid}); prov = c.get("provincia") if c else None
    if not prov:
        await update.message.reply_text("❌ Configura provincia.", reply_markup=ReplyKeyboardRemove()); return await enviar_menu_msg(update, uid)
    
    await coleccion_clientes.update_one({"_id": uid}, {"$inc": {"busquedas": 1}, "$set": {"ultima_actividad": datetime.now(TZ_CUBA)}})
    await validar_referido(uid, context)
    await limpiar_expirados()
    try: cats = await coleccion_catalogos.find({"provincia": prov}).to_list(None)
    except: return await update.message.reply_text("⚠️ Error BD.", reply_markup=menu_post_busqueda())
    
    ops = []
    for cat in cats:
        pid = cat["proveedor_id"]; prv = await coleccion_proveedores.find_one({"_id": pid})
        if not prv or prv.get("baneado"): continue
        for i, ln in enumerate(cat["lineas_normalizadas"]):
            sc = rfuzz.WRatio(mb, ln)
            if sc >= UMBRAL_FUZZY: ops.append({"s": sc, "l": cat["lineas_originales"][i], "p": prv})
    
    if not ops:
        await update.message.reply_text(f"❌ No encontré '<b>{esc(txt)}</b>' en {esc(prov)}.", reply_markup=menu_post_busqueda(), parse_mode="HTML")
        context.user_data["estado"] = None; return
    
    now_cuba = datetime.now(TZ_CUBA)
    def sort_key(x):
        is_dest = False
        if x["p"].get("destacado_hasta"):
            try: is_dest = now_cuba < x["p"]["destacado_hasta"]
            except: pass
        return (is_dest, x["s"])
    ops.sort(key=sort_key, reverse=True)
    
    por_prov = {}
    for o in ops:
        pid = o["p"]["_id"]
        if pid not in por_prov: por_prov[pid] = {"prov": o["p"], "items": []}
        por_prov[pid]["items"].append(o["l"])
    
    if len(por_prov) <= 3:
        msg = f"🔍 <b>{esc(txt.upper())}</b> en {esc(prov)}\n\n"; botones = []
        for pid, data in por_prov.items():
            prv = data["prov"]; dest = "⭐ " if await es_destacado(prv) else ""; nombre_prov = prv.get('nombre', 'Prov')
            msg += f"{dest}<b>{esc(nombre_prov)}</b>\n📞 {esc(prv.get('contacto_mostrar'))}\n"
            for item in data["items"][:3]: msg += f"   • {esc(item)}\n"
            msg += "\n"; contacto = prv.get("contacto", {}); tel_wa = contacto.get("whatsapp", "").replace("+", "").replace(" ", ""); tel_tg = contacto.get("telegram", "")
            if tel_wa:
                wa_msg = f"🏥 MediCuba (t.me/MediCubaBot)\n\nHola, ¿Tienes disponible {txt}?"; wa_url = f"https://wa.me/{tel_wa}?text={wa_msg.replace(' ', '%20')}"
                botones.append([InlineKeyboardButton(f"💬 Ir WhatsApp: {esc(nombre_prov)}", url=wa_url)])
            if tel_tg:
                tg_url = f"https://t.me/{tel_tg.replace('@','')}"; botones.append([InlineKeyboardButton(f"✈️ Ir Telegram: {esc(nombre_prov)}", url=tg_url)])
            botones.append([InlineKeyboardButton("🚨 Reportar", callback_data=f"reportar_{pid}")])
            
        botones.append([InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML"); context.user_data["estado"] = None
    else:
        msg = f"🔍 <b>{esc(txt.upper())}</b> - Sugerencias:\n\nToca el número del proveedor con el que deseas contactar:\n\n"
        sugs = ops[:15]; context.user_data["sugs"] = sugs
        for i, o in enumerate(sugs, 1): 
            dest_hasta = o["p"].get("destacado_hasta"); is_dest = False
            if dest_hasta:
                try: is_dest = now_cuba < dest_hasta
                except: pass
            dest = "⭐ " if is_dest else ""; msg += f"{i}. {dest}{esc(o['l'])}\n"
        botones_num = []; fila = []
        for i in range(1, len(sugs)+1):
            fila.append(InlineKeyboardButton(str(i), callback_data=f"sel_{i}"))
            if i % 5 == 0 or i == len(sugs): botones_num.append(fila); fila = []
        botones_num.append([InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(botones_num), parse_mode="HTML"); context.user_data["estado"] = "esperando_seleccion"

async def _seleccion(update, context, uid, txt):
    sugs = context.user_data.get("sugs", [])
    try:
        n = int(txt.strip())
        if 1 <= n <= len(sugs):
            o = sugs[n-1]; prv = o["p"]; dest = "⭐ " if await es_destacado(prv) else ""; nombre_prov = prv.get('nombre', 'Prov')
            msg = f"🏥 {dest}<b>{esc(nombre_prov)}</b>\n📞 {esc(prv.get('contacto_mostrar'))}\n\n💊 {esc(o['l'])}\n"
            botones = []; contacto = prv.get("contacto", {}); tel_wa = contacto.get("whatsapp", "").replace("+", "").replace(" ", ""); tel_tg = contacto.get("telegram", ""); search_term = context.user_data.get("last_search", "esto")
            if tel_wa:
                wa_msg = f"🏥 MediCuba (t.me/MediCubaBot)\n\nHola, ¿Tienes disponible {search_term}?"; wa_url = f"https://wa.me/{tel_wa}?text={wa_msg.replace(' ', '%20')}"
                botones.append([InlineKeyboardButton(f"💬 Ir WhatsApp: {esc(nombre_prov)}", url=wa_url)])
            if tel_tg:
                tg_url = f"https://t.me/{tel_tg.replace('@','')}"; botones.append([InlineKeyboardButton(f"✈️ Ir Telegram: {esc(nombre_prov)}", url=tg_url)])
            botones.append([InlineKeyboardButton("🚨 Reportar", callback_data=f"reportar_{prv['_id']}")])
            botones.append([InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML"); context.user_data["estado"] = None
        else: await _busqueda(update, context, uid, txt)
    except ValueError: await _busqueda(update, context, uid, txt)

# ===== LISTADO =====
async def _listado(update, context, uid, txt):
    txt_limpio = eliminar_emojis(txt)
    if contiene_no_medicos(txt_limpio):
        context.user_data["estado"] = None; await update.message.reply_text("❌ Productos no médicos.", reply_markup=ReplyKeyboardRemove()); return await enviar_menu_msg(update, uid)
    lns = [l.strip() for l in txt_limpio.split('\n') if l.strip()]; tr = len(lns) > MAX_LINEAS_CATALOGO; lns = lns[:MAX_LINEAS_CATALOGO]; lns_n = [normalizar_texto(l) for l in lns]
    cc = await coleccion_catalogos.count_documents({"proveedor_id": uid})
    if cc >= MAX_CATALOGOS_PROVEEDOR:
        ol = await coleccion_catalogos.find_one({"proveedor_id": uid}, sort=[("fecha_creacion", 1)])
        if ol: await coleccion_catalogos.delete_one({"_id": ol["_id"]})
    cd = await coleccion_clientes.find_one({"_id": uid}); pr = cd.get("provincia", "Santiago de Cuba") if cd else "Santiago de Cuba"
    await coleccion_proveedores.update_one({"_id": uid}, {"$set": {"nombre": update.effective_user.first_name or "Prov", "provincia": pr, "link_token": uid}}, upsert=True)
    expiracion = datetime.now(TZ_CUBA) + timedelta(days=DIAS_EXPIRACION)
    await coleccion_catalogos.insert_one({"proveedor_id": uid, "lineas_originales": lns, "lineas_normalizadas": lns_n, "fecha_creacion": datetime.now(TZ_CUBA), "fecha_expiracion": expiracion, "provincia": pr, "hash": generar_hash(txt_limpio)})
    av = f"\n⚠️ Solo {MAX_LINEAS_CATALOGO} líneas." if tr else ""
    context.user_data["estado"] = "esperando_contacto"; context.user_data["mc"] = len(lns)
    tk = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp"), InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram"), InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")]]
    await update.message.reply_text(f"✅ {len(lns)} líneas.{av}\n\n¿Cómo te contactarán?", reply_markup=InlineKeyboardMarkup(tk), parse_mode="HTML")

# ===== CONTACTO PROVEEDOR =====
async def _telefono(update, context, uid, txt):
    tel = txt.strip(); tipo = context.user_data.get("tipo_contacto", "whatsapp")
    existing_prov = await coleccion_proveedores.find_one({"contacto.whatsapp": tel, "_id": {"$ne": uid}})
    if existing_prov:
        old_pid = existing_prov["_id"]; await coleccion_catalogos.update_many({"proveedor_id": old_pid}, {"$set": {"proveedor_id": uid}})
        await coleccion_proveedores.delete_one({"_id": old_pid})
        await coleccion_proveedores.update_one({"_id": uid}, {"$set": {"contacto.tipo": tipo, "contacto.whatsapp": tel, "contacto_mostrar": tel, "nombre": update.effective_user.first_name or existing_prov.get('nombre'), "provincia": existing_prov.get('provincia'), "link_token": uid}}, upsert=True)
    else: await coleccion_proveedores.update_one({"_id": uid}, {"$set": {"contacto.tipo": tipo, "contacto.whatsapp": tel, "contacto_mostrar": tel, "nombre": update.effective_user.first_name or "Prov"}})
    if tipo == "ambos":
        context.user_data["estado"] = "esperando_telegram"; return await update.message.reply_text("✈️ Ahora tu @usuario de Telegram:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))
    await _fin_reg(update, context, uid)

async def _telegram(update, context, uid, txt):
    tg = txt.strip()
    if not tg.startswith("@"): tg = "@" + tg
    tipo = context.user_data.get("tipo_contacto", "telegram"); prv = await coleccion_proveedores.find_one({"_id": uid}); wa = prv.get("contacto", {}).get("whatsapp", "") if prv else ""
    mos = tg if tipo == "telegram" else f"{wa} / {tg}"
    await coleccion_proveedores.update_one({"_id": uid}, {"$set": {"contacto.telegram": tg, "contacto_mostrar": mos, "nombre": update.effective_user.first_name or "Prov"}})
    await _fin_reg(update, context, uid)

async def _fin_reg(update, context, uid):
    ed = context.user_data.get("editando_contacto", False); prv = await coleccion_proveedores.find_one({"_id": uid})
    if ed: msg = f"✅ Contacto actualizado:\n📞 {esc(prv.get('contacto_mostrar'))}"; context.user_data["editando_contacto"] = False
    else: lk = f"t.me/MediCubaBot?start=proveedor_{uid}"; msg = f"✅ <b>¡Publicado!</b>\n\n📋 {context.user_data.get('mc',0)} líneas\n📞 {esc(prv.get('contacto_mostrar'))}\n\n🔗 <code>{lk}</code>"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú", callback_data="volver")]]), parse_mode="HTML"); context.user_data["estado"] = None

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
    txt_limpio = eliminar_emojis(txt); h = generar_hash(txt_limpio)
    if await coleccion_catalogos.find_one({"hash": h}): context.user_data["estado"] = None; await update.message.reply_text("⚠️ Duplicado.", reply_markup=ReplyKeyboardRemove()); return await enviar_menu_msg(update, uid)
    if contiene_no_medicos(txt_limpio): context.user_data["estado"] = None; await update.message.reply_text("❌ No médicos.", reply_markup=ReplyKeyboardRemove()); return await enviar_menu_msg(update, uid)
    lns = [l.strip() for l in txt_limpio.split('\n') if l.strip()][:MAX_LINEAS_CATALOGO]; lns_n = [normalizar_texto(l) for l in lns]
    pid = f"admin_{uid}_{int(datetime.now(TZ_CUBA).timestamp())}"
    existing_prov = await coleccion_proveedores.find_one({"contacto.whatsapp": tel})
    if existing_prov: pid = existing_prov["_id"]
    else:
        num_admin = datos.get("siguiente_num_admin", 1); nombre_admin = f"{num_admin} Admin MediCuba"
        await coleccion_proveedores.update_one({"_id": pid}, {"$set": {"nombre": nombre_admin, "contacto": {"tipo": "whatsapp", "whatsapp": tel}, "contacto_mostrar": tel, "provincia": "Santiago de Cuba"}}, upsert=True)
        datos["siguiente_num_admin"] = num_admin + 1; await guardar_config(datos)
    cc = await coleccion_catalogos.count_documents({"proveedor_id": pid})
    if cc >= MAX_CATALOGOS_PROVEEDOR:
        ol = await coleccion_catalogos.find_one({"proveedor_id": pid}, sort=[("fecha_creacion", 1)])
        if ol: await coleccion_catalogos.delete_one({"_id": ol["_id"]})
    expiracion = datetime.now(TZ_CUBA) + timedelta(days=DIAS_EXPIRACION)
    await coleccion_catalogos.insert_one({"proveedor_id": pid, "lineas_originales": lns, "lineas_normalizadas": lns_n, "fecha_creacion": datetime.now(TZ_CUBA), "fecha_expiracion": expiracion, "provincia": "Santiago de Cuba", "hash": h})
    prv_data = await coleccion_proveedores.find_one({"_id": pid})
    await update.message.reply_text(f"✅ {len(lns)} líneas.\n📞 {esc(tel)}\n📛 Publicado como: {esc(prv_data.get('nombre'))}", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await enviar_menu_msg(update, uid); context.user_data["estado"] = None; context.user_data["admin_tel"] = None

# ===== BROADCAST =====
async def _admin_broadcast_msg(update, context, uid, txt):
    tipo = context.user_data.get("broadcast_tipo")
    if not tipo: return
    enviado = 0; error = 0
    if tipo == "prov": users = await coleccion_proveedores.find({}).to_list(None)
    else: users = await coleccion_clientes.find({}).to_list(None)
    for u in users:
        try: await context.bot.send_message(u["_id"], f"📢 <b>Mensaje de MediCuba:</b>\n\n{esc(txt)}", parse_mode="HTML"); enviado += 1
        except: error += 1
    await update.message.reply_text(f"✅ Mensaje enviado a {enviado} usuarios. ({error} errores/bloqueados)", reply_markup=ReplyKeyboardRemove())
    context.user_data["estado"] = None; context.user_data["broadcast_tipo"] = None; await enviar_menu_msg(update, uid)

# ===== BAN / UNBAN / TEXTS =====
async def _admin_ban_msg(update, context, uid, txt):
    if not es_admin(uid): return
    bid = txt.strip()
    await coleccion_clientes.update_one({"_id": bid}, {"$set": {"baneado": True}}, upsert=True)
    await coleccion_proveedores.update_one({"_id": bid}, {"$set": {"baneado": True}}, upsert=True)
    await update.message.reply_text(f"✅ Usuario <code>{bid}</code> baneado.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    context.user_data["estado"] = None; await enviar_menu_msg(update, uid)

async def _admin_unban_msg(update, context, uid, txt):
    if not es_admin(uid): return
    bid = txt.strip()
    await coleccion_clientes.update_one({"_id": bid}, {"$set": {"baneado": False}}, upsert=True)
    await coleccion_proveedores.update_one({"_id": bid}, {"$set": {"baneado": False}}, upsert=True)
    await update.message.reply_text(f"✅ Usuario <code>{bid}</code> desbaneado.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    context.user_data["estado"] = None; await enviar_menu_msg(update, uid)

async def _admin_edit_text(update, context, uid, txt, key):
    if not es_admin(uid): return
    datos.setdefault("textos", {})[key] = txt
    await guardar_config(datos)
    await update.message.reply_text(f"✅ Texto de {key} actualizado.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    context.user_data["estado"] = None; await enviar_menu_msg(update, uid)

async def destacar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id): return
    if not context.args or len(context.args) < 2: return await update.message.reply_text("<code>/destacar ID DIAS</code>", parse_mode="HTML")
    pid = context.args[0]
    try: d = int(context.args[1])
    except: return await update.message.reply_text("❌ Días inválidos.")
    if not await coleccion_proveedores.find_one({"_id": pid}): return await update.message.reply_text("❌ No encontrado.")
    await coleccion_proveedores.update_one({"_id": pid}, {"$set": {"destacado_hasta": datetime.now(TZ_CUBA) + timedelta(days=d)}})
    await update.message.reply_text(f"✅ Destacado {d} días.", parse_mode="HTML")

async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id) != int(ADMIN_ID): return
    if not context.args: return await update.message.reply_text("<code>/addadmin ID</code>", parse_mode="HTML")
    try: na = int(context.args[0])
    except: return await update.message.reply_text("ID numérico.")
    if na not in datos.get("administradores", [int(ADMIN_ID)]): datos.setdefault("administradores", [int(ADMIN_ID)]).append(na); await guardar_config(datos); await update.message.reply_text(f"✅ Admin {na}")
    else: await update.message.reply_text("Ya lo es.")

async def del_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id) != int(ADMIN_ID): return
    if not context.args: return await update.message.reply_text("<code>/deladmin ID</code>", parse_mode="HTML")
    try: da = int(context.args[0])
    except: return await update.message.reply_text("ID numérico.")
    if da == int(ADMIN_ID): return await update.message.reply_text("No puedes eliminarte.")
    if da in datos.get("administradores", []): datos["administradores"].remove(da); await guardar_config(datos); await update.message.reply_text(f"✅ Eliminado {da}")

async def anuncio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id): return
    if not context.args or len(context.args) < 2: return await update.message.reply_text("<code>/anuncio add texto</code>\n<code>/anuncio del N</code>", parse_mode="HTML")
    a = context.args[0].lower(); t = " ".join(context.args[1:]); datos.setdefault("anuncios", [])
    if a == "add": datos["anuncios"].append(t); await guardar_config(datos); await update.message.reply_text("✅ Añadido.")
    elif a == "del":
        try: datos["anuncios"].pop(int(t) - 1); await guardar_config(datos); await update.message.reply_text("✅ Eliminado.")
        except: await update.message.reply_text("N inválido.")

# ===== BROADCAST & PROMO CALLBACK =====
async def callbacks_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer(); uid = str(q.from_user.id); d = q.data
    if d in ["broadcast_prov", "broadcast_cli"] and es_admin(uid):
        context.user_data["broadcast_tipo"] = "prov" if d == "broadcast_prov" else "cli"; context.user_data["estado"] = "admin_esperando_broadcast_msg"
        await q.edit_message_text("✉️ <b>Enviar Mensaje</b>\n\nEscribe el mensaje a enviar:", parse_mode="HTML")
    elif d.startswith("admin_edit_") and es_admin(uid):
        key = d.replace("admin_edit_", ""); context.user_data["estado"] = f"admin_esperando_texto_{key}"
        current_text = datos.get("textos", {}).get(key, "")
        await q.edit_message_text(f"✏️ <b>Editar {key.capitalize()}</b>\n\nTexto actual:\n<code>{esc(current_text)}</code>\n\nEnvía el nuevo texto:", parse_mode="HTML")
        await context.bot.send_message(uid, "Nuevo texto:", reply_markup=ReplyKeyboardMarkup([["🔙 Volver al Menú"]], resize_keyboard=True))
    elif d == "admin_stop_promo" and es_admin(uid):
        await desactivar_promo(context)
        await q.edit_message_text("🏁 Promo desactivada y contadores reseteados.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]), parse_mode="HTML")

# ===== HEALTH CHECK =====
class HCH(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.send_header('Content-type', 'text/plain'); self.end_headers(); self.wfile.write(b'OK')
    def log_message(self, fmt, *a): pass

def iniciar_hc():
    p = int(os.environ.get('PORT', 10000))
    try: s = HTTPServer(('0.0.0.0', p), HCH); threading.Thread(target=s.serve_forever, daemon=True).start(); logger.info(f"✅ HC puerto {p}")
    except Exception as e: logger.error(f"HC error: {e}")

# ===== MAIN =====
async def post_init(app):
    await init_config()
    try: await app.bot.delete_webhook(drop_pending_updates=True)
    except: pass
    try:
        await coleccion_catalogos.create_index([("provincia", 1)]); await coleccion_catalogos.create_index([("proveedor_id", 1)]); await coleccion_catalogos.create_index([("fecha_expiracion", 1)])
    except: pass

def main():
    if not TOKEN or not MONGODB_URI: logger.error("🛑 Faltan vars."); return
    iniciar_hc()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancelar", cancelar)); app.add_handler(CommandHandler("cancel", cancelar))
    app.add_handler(CommandHandler("admin_cargar_listado", admin_cargar)); app.add_handler(CommandHandler("destacar", destacar_cmd))
    app.add_handler(CommandHandler("addadmin", add_admin_cmd)); app.add_handler(CommandHandler("deladmin", del_admin_cmd))
    app.add_handler(CommandHandler("anuncio", anuncio_cmd))
    
    app.add_handler(CallbackQueryHandler(callbacks_broadcast, pattern=r'^(broadcast_|admin_edit_|admin_stop_promo)'))
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
