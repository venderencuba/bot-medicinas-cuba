"""
BOT DE MEDICINAS CUBA - VERSIÓN PROFESIONAL MONGODB
100% por botones | Fuzzy Search | 2 Catálogos | Auto-Limpieza
Compatible con Python 3.14 y python-telegram-bot v21+
"""

import logging
import os
import html
import hashlib
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient
from rapidfuzz import fuzz, process

# ===== CONFIGURACIÓN =====
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "8685939368:AAESfgUVeQG0qA8521Qx5LO_7Qm3LY27Qq0")
ADMIN_ID = 814338625

# Constantes del sistema
MAX_CATALOGOS_PROVEEDOR = 2
MAX_LINEAS_CATALOGO = 80
DIAS_EXPIRACION_ADMIN = 10
UMBRAL_FUZZY = 70
LINEAS_PAGINACION = 10

# Lista negra de productos no médicos
BLACKLIST = [
    "zapato", "zapatilla", "ropa", "camisa", "pantalon", "falda", "joya", "anillo", 
    "collar", "comida", "pollo", "arroz", "frijol", "cafe", "azucar", "aceite", 
    "telefono", "celular", "carro", "auto", "casa", "departamento", "perfume", 
    "maquillaje", "cosmetico", "mascota", "perro", "gato"
]

# ===== CONEXIÓN MONGODB =====
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    logger.error("❌ MONGODB_URI no configurada.")

client = AsyncIOMotorClient(MONGODB_URI)
db = client.medicuba_db

coleccion_clientes = db.clientes
coleccion_proveedores = db.proveedores
coleccion_catalogos = db.catalogos

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
    try: return datetime.now() < datetime.fromisoformat(proveedor["destacado_hasta"])
    except: return False

def contiene_productos_no_medicos(texto):
    texto_norm = normalizar_texto(texto)
    for palabra in BLACKLIST:
        if palabra in texto_norm:
            return palabra
    return None

async def limpiar_expirados():
    """Elimina catálogos de admin expirados y destacados vencidos"""
    ahora = datetime.now()
    await coleccion_catalogos.delete_many({"es_admin": True, "fecha_expiracion": {"$lt": ahora}})
    await coleccion_proveedores.update_many(
        {"destacado_hasta": {"$lt": agora.isoformat()}},
        {"$unset": {"destacado_hasta": ""}}
    )

# ===== GENERADORES DE INTERFAZ =====

def generar_menu_principal(user_id, provincia):
    teclado = [
        [InlineKeyboardButton("🔍 Buscar Medicina", callback_data="buscar")],
        [InlineKeyboardButton("📝 Publicar Catálogo", callback_data="publicar")],
        [InlineKeyboardButton(f"📍 Provincia: {provincia or 'Seleccionar'}", callback_data="cambiar_provincia")],
        [InlineKeyboardButton("👤 Mi Perfil", callback_data="mi_perfil")],
        [InlineKeyboardButton("⭐ Proveedores Destacados", callback_data="destacados")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ]
    if es_admin(user_id):
        teclado.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    texto = (f"🏥 <b>MediCuba</b>\n🩺 Tu salud, nuestra prioridad\n\n"
             f"📍 <b>Tu provincia:</b> {esc(provincia or 'No seleccionada')}\n\n"
             f"¿Qué deseas hacer?")
    return texto, InlineKeyboardMarkup(teclado)

async def enviar_menu_callback(query, user_id):
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    provincia = cliente.get("provincia") if cliente else None
    texto, teclado = generar_menu_principal(user_id, provincia)
    try: await query.edit_message_text(texto, reply_markup=teclado, parse_mode="HTML")
    except: await query.message.reply_text(texto, reply_markup=teclado, parse_mode="HTML")

async def enviar_menu_mensaje(update, user_id):
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    provincia = cliente.get("provincia") if cliente else None
    texto, teclado = generar_menu_principal(user_id, provincia)
    await update.message.reply_text(texto, reply_markup=teclado, parse_mode="HTML")

# ===== COMANDO /START =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if context.args and context.args[0].startswith("proveedor_"):
        prov_id = context.args[0].replace("proveedor_", "")
        await mostrar_catalogo_proveedor_msg(update, prov_id)
        return
    
    async with await client.start_session() as session:
        await coleccion_clientes.update_one(
            {"_id": user_id}, 
            {"$setOnInsert": {"provincia": None, "busquedas": 0}}, 
            upsert=True, session=session
        )
    
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    if not cliente.get("provincia"):
        await forzar_seleccion_provincia(update)
    else:
        await enviar_menu_mensaje(update, user_id)

async def forzar_seleccion_provincia(update):
    PROVINCIAS = ["Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas", "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila", "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba", "Guantánamo", "Isla de la Juventud"]
    lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
    await update.message.reply_text(
        f"👋 ¡Bienvenido a MediCuba!\n\nPara buscar medicinas, primero dinos de qué provincia eres:\n\n{lista}\n\n"
        f"Responde con el NÚMERO:", parse_mode="HTML"
    )

async def mostrar_catalogo_proveedor_msg(update, prov_id):
    proveedor = await coleccion_proveedores.find_one({"_id": prov_id})
    if not proveedor:
        await update.message.reply_text("❌ Proveedor no encontrado.")
        return
    
    catalogos_cursor = coleccion_catalogos.find({"proveedor_id": prov_id})
    catalogos = await catalogos_cursor.to_list(None)
    
    if not catalogos:
        await update.message.reply_text("📭 Este proveedor no tiene catálogo activo.")
        return

    mensaje = f"🏥 <b>{esc(proveedor.get('nombre', 'Proveedor'))}</b>\n"
    if await es_destacado_activo(proveedor): mensaje += "⭐ <b>Proveedor Destacado</b> ⭐\n"
    mensaje += f"📞 {esc(proveedor.get('contacto_mostrar', ''))}\n" + "─"*20 + "\n\n"
    
    for cat in catalogos:
        mensaje += "<b>📋 Catálogo:</b>\n"
        for linea in cat.get("lineas", [])[:30]:
            mensaje += f"• {esc(linea['original'])}\n"
        if len(cat.get("lineas", [])) > 30: mensaje += f"... y {len(cat['lineas'])-30} más.\n"
        mensaje += "\n"

    mensaje += "─"*20 + "\n🩺 <b>MediCuba</b> - @MediCubaBot"
    teclado = [[InlineKeyboardButton("🏠 Ir al Bot", callback_data="volver")]]
    await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")


# ===== HANDLER ÚNICO DE CALLBACKS =====

async def manejador_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data = query.data
    
    await limpiar_expirados() # Micro-limpieza en cada interacción

    if data == "volver":
        await enviar_menu_callback(query, user_id)
    elif data == "buscar":
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        if not cliente or not cliente.get("provincia"):
            await query.edit_message_text("❌ Primero configura tu provincia.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📍 Seleccionar Provincia", callback_data="cambiar_provincia")]]))
            return
        context.user_data["estado"] = "esperando_medicina"
        await query.edit_message_text("🔍 <b>Buscar Medicina</b>\n\nEscribe el nombre (puedes cometer errores de ortografía):\n\n<i>Ejemplo:</i> <code>parasetamol</code>", parse_mode="HTML")
    elif data == "publicar":
        context.user_data["estado"] = "esperando_listado"
        await query.edit_message_text(
            "📝 <b>Publicar Catálogo</b>\n\n"
            f"📋 Pega aquí tu listado de medicinas.\n\n"
            f"⚠️ <b>Reglas:</b>\n"
            f"• Máximo {MAX_LINEAS_CATALOGO} líneas por catálogo\n"
            f"• Puedes tener hasta <b>{MAX_CATALOGOS_PROVEEDOR} catálogos</b> activos\n"
            f"• Si subes un {MAX_CATALOGOS_PROVEEDOR+1}º, se borrará el más antiguo\n"
            f"• Solo se permiten medicinas (no ropa, comida, etc.)\n\n"
            f"<i>Pega tu texto de WhatsApp/Telegram:</i>", parse_mode="HTML"
        )
    elif data == "cambiar_provincia":
        PROVINCIAS = ["Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas", "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila", "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba", "Guantánamo", "Isla de la Juventud"]
        lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(PROVINCIAS)])
        context.user_data["estado"] = "cambiando_provincia"
        await query.edit_message_text(f"📍 <b>Cambiar Provincia</b>\n\n{lista}\n\nResponde con el NÚMERO:", parse_mode="HTML")
    elif data == "mi_perfil":
        await _mostrar_perfil(query, user_id)
    elif data == "editar_contacto":
        context.user_data["editando_contacto"] = True
        teclado = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")], [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")], [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")], [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]
        await query.edit_message_text("✏️ <b>Editar Contacto</b>\n\n¿Cómo prefieres que te contacten?", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")
    elif data == "ver_mi_catalogo":
        await _mostrar_mi_catalogo(query, user_id, pagina=0)
    elif data.startswith("pag_cat_"):
        pagina = int(data.split("_")[-1])
        await _mostrar_mi_catalogo(query, user_id, pagina)
    elif data == "destacados":
        await _mostrar_destacados(query)
    elif data == "ayuda":
        teclado = [[InlineKeyboardButton("👨‍💼 Proveedores", callback_data="ayuda_prov")], [InlineKeyboardButton("🛒 Clientes", callback_data="ayuda_cli")], [InlineKeyboardButton("⚙️ General", callback_data="ayuda_gen")], [InlineKeyboardButton("🔙 Volver", callback_data="volver")]]
        await query.edit_message_text("❓ <b>Centro de Ayuda</b>", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")
    elif data == "ayuda_prov":
        await query.edit_message_text("👨‍💼 Pega tu listado. El bot lo guarda exactamente como lo escribes. Los clientes lo verán con búsqueda inteligente.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="ayuda")]]), parse_mode="HTML")
    elif data == "ayuda_cli":
        await query.edit_message_text("🛒 Escribe el nombre de la medicina. Nuestro motor tolera errores ortográficos.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="ayuda")]]), parse_mode="HTML")
    elif data == "ayuda_gen":
        await query.edit_message_text("⚙️ MediCuba conecta pacientes y proveedores en Cuba.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="ayuda")]]), parse_mode="HTML")
    elif data.startswith("contacto_"):
        await _procesar_contacto_callback(query, context, user_id, data)
    elif data.startswith("sel_"): # Selección de sugerencia fuzzy
        partes = data.split("_")
        cat_id = partes[1]
        linea_idx = int(partes[2])
        await _mostrar_detalle_sugerencia(query, user_id, cat_id, linea_idx)
    elif data.startswith("pag_res_"): # Paginación resultados
        pagina = int(data.split("_")[-1])
        termino = context.user_data.get("ultimo_termino", "")
        await _busqueda(update, context, user_id, termino, pagina)
    elif data == "admin_panel":
        if es_admin(user_id):
            teclado = [[InlineKeyboardButton("📥 Cargar Listado", callback_data="admin_cargar")], [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_stats")], [InlineKeyboardButton("👥 Proveedores", callback_data="admin_provs")], [InlineKeyboardButton("📋 Listados Admin Activos", callback_data="admin_listados")], [InlineKeyboardButton("🔙 Volver", callback_data="volver")]]
            await query.edit_message_text("🔧 <b>Panel Admin</b>", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")
    elif data == "admin_cargar":
        if es_admin(user_id): await query.edit_message_text("📥 Usa: <code>/admin_cargar_listado +5351234567</code>\n\nLuego pega el texto.", parse_mode="HTML")
    elif data == "admin_stats":
        if es_admin(user_id): await _mostrar_estadisticas(query)
    elif data == "admin_provs":
        if es_admin(user_id): await _mostrar_proveedores_admin(query)
    elif data == "admin_listados":
        if es_admin(user_id): await _mostrar_listados_admin(query)
    else:
        logger.warning(f"Callback desconocido: {data}")


# ===== HANDLER ÚNICO DE MENSAJES =====

async def procesar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    texto = update.message.text
    estado = context.user_data.get("estado")

    if estado == "esperando_medicina": await _busqueda(update, context, user_id, texto, pagina=0)
    elif estado == "esperando_listado": await _listado(update, context, user_id, texto)
    elif estado == "cambiando_provincia": await _cambio_provincia(update, context, user_id, texto)
    elif estado == "esperando_telefono": await _telefono(update, context, user_id, texto)
    elif estado == "esperando_telegram": await _telegram_user(update, context, user_id, texto)
    elif estado == "admin_esperando_listado": await _admin_listado(update, context, user_id, texto)


# ===== LÓGICA DE BÚSQUEDA (FUZZY MATCHING) =====

async def _busqueda(update, context, user_id, texto, pagina=0):
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    provincia = cliente.get("provincia") if cliente else None
    if not provincia:
        await update.message.reply_text("❌ Configura tu provincia primero.")
        context.user_data["estado"] = None
        return

    await coleccion_clientes.update_one({"_id": user_id}, {"$inc": {"busquedas": 1}})
    termino_norm = normalizar_texto(texto)
    context.user_data["ultimo_termino"] = texto # Guardar para paginación

    # 1. Buscar catálogos en la provincia del usuario
    catalogos_cursor = coleccion_catalogos.find({"provincia": provincia, "es_admin": {"$ne": True}})
    catalogos_prov = await catalogos_cursor.to_list(None)
    
    # Unir todos los admin que no tienen provincia o es la misma
    catalogos_admin_cursor = coleccion_catalogos.find({"es_admin": True, "provincia": provincia})
    catalogos_prov.extend(await catalogos_admin_cursor.to_list(None))

    # 2. Búsqueda Exacta / Parcial inicial
    resultados_exactos = []
    for cat in catalogos_prov:
        for idx, linea in enumerate(cat.get("lineas", [])):
            if termino_norm in linea["normalizada"]:
                resultados_exactos.append({"cat_id": cat["_id"], "linea_idx": idx, "linea": linea["original"], "prov_id": cat["proveedor_id"], "score": 100})

    # 3. Si no hay exactos, lanzar Fuzzy
    resultados_fuzzy = []
    if not resultados_exactos:
        opciones_fuzzy = []
        for cat in catalogos_prov:
            for idx, linea in enumerate(cat.get("lineas", [])):
                opciones_fuzzy.append({
                    "texto": linea["normalizada"], 
                    "original": linea["original"], 
                    "cat_id": cat["_id"], 
                    "linea_idx": idx, 
                    "prov_id": cat["proveedor_id"]
                })
        
        textos_buscar = [op["texto"] for op in opciones_fuzzy]
        matches = process.extract(termino_norm, textos_buscar, scorer=fuzz.WRatio, limit=10, score_cutoff=UMBRAL_FUZZY)
        
        for match_texto, score, idx_lista in matches:
            op = opciones_fuzzy[idx_lista]
            resultados_fuzzy.append({**op, "score": score})

    # 4. Mostrar Resultados
    if resultados_exactos:
        # Ordenar por proveedores destacados primero
        proveedores_cache = {}
        for res in resultados_exactos:
            if res["prov_id"] not in proveedores_cache:
                prov = await coleccion_proveedores.find_one({"_id": res["prov_id"]})
                proveedores_cache[res["prov_id"]] = prov
        
        resultados_exactos.sort(key=lambda x: es_destacado_activo_sync(proveedores_cache.get(x["prov_id"])), reverse=True)
        
        # Paginar
        inicio = pagina * LINEAS_PAGINACION
        fin = inicio + LINEAS_PAGINACION
        pag_actual = resultados_exactos[inicio:fin]
        
        mensaje = f"🔍 <b>{esc(texto.upper())}</b> en {esc(provincia)}\n✅ {len(resultados_exactos)} coincidencias:\n\n"
        for res in pag_actual:
            prov = proveedores_cache.get(res["prov_id"], {})
            dest = "⭐ " if await es_destacado_activo(prov) else ""
            mensaje += f"{dest}<b>{esc(prov.get('nombre', 'Anónimo'))}</b>\n  📞 {esc(prov.get('contacto_mostrar', ''))}\n  💊 {esc(res['linea'])}\n\n"
        
        botones = []
        if fin < len(resultados_exactos): botones.append([InlineKeyboardButton("➡️ Ver más", callback_data=f"pag_res_{pagina+1}")])
        botones.append([InlineKeyboardButton("🏠 Menú", callback_data="volver")])
        
        target = update.message if pagina == 0 else update.callback_query
        if pagina == 0: await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        else: await update.callback_query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        context.user_data["estado"] = None

    elif resultados_fuzzy:
        mensaje = f"🤔 No encontré <b>{esc(texto)}</b> exactamente, pero esto es muy parecido:\n\n"
        botones = []
        for i, res in enumerate(resultados_fuzzy[:5], 1):
            mensaje += f"{i}. {esc(res['original'])}\n"
            botones.append([InlineKeyboardButton(f"{i}. {res['original'][:30]}...", callback_data=f"sel_{res['cat_id']}_{res['linea_idx']}")])
        
        botones.append([InlineKeyboardButton("🔍 Nueva búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
        await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")
        context.user_data["estado"] = None
    else:
        await update.message.reply_text(f"❌ No encontré nada parecido a '{esc(texto)}'.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Reintentar", callback_data="buscar")], [InlineKeyboardButton("🏠 Menú", callback_data="volver")]]), parse_mode="HTML")
        context.user_data["estado"] = None

def es_destacado_activo_sync(prov):
    if not prov or not prov.get("destacado_hasta"): return False
    try: return datetime.now() < datetime.fromisoformat(prov["destacado_hasta"])
    except: return False

async def _mostrar_detalle_sugerencia(query, user_id, cat_id, linea_idx):
    from bson import ObjectId
    cat = await coleccion_catalogos.find_one({"_id": ObjectId(cat_id)})
    if not cat: await query.answer("Catálogo no encontrado", show_alert=True); return
    
    proveedor = await coleccion_proveedores.find_one({"_id": cat["proveedor_id"]}) or {}
    linea_original = cat["lineas"][linea_idx]["original"]
    
    mensaje = f"💊 <b>{esc(linea_original)}</b>\n\n🏥 <b>Proveedor:</b> {esc(proveedor.get('nombre', 'Anónimo'))}\n📞 {esc(proveedor.get('contacto_mostrar', ''))}\n"
    
    botones = []
    contacto = proveedor.get("contacto", {})
    if contacto.get("tipo") in ["whatsapp", "ambos"]:
        tel = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
        if tel:
            msg_wa = f"Hola, te contacto desde MediCuba por: {linea_original}. ¿Tienes disponible?"
            botones.append([InlineKeyboardButton("📞 Contactar WhatsApp", url=f"https://wa.me/{tel}?text={msg_wa.replace(' ', '%20')}")])
    botones.append([InlineKeyboardButton("🔍 Nueva búsqueda", callback_data="buscar"), InlineKeyboardButton("🏠 Menú", callback_data="volver")])
    
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")


# ===== LÓGICA DE CATÁLOGOS Y PROVEEDORES =====

async def _listado(update, context, user_id, texto):
    # 1. Blacklist check
    palabra_prohibida = contiene_productos_no_medicos(texto)
    if palabra_prohibida:
        await update.message.reply_text(f"❌ <b>Listado rechazado.</b>\nSe detectó un producto no médico: <i>{esc(palabra_prohibida)}</i>.\n\nSolo se permiten medicinas e insumos médicos.", parse_mode="HTML")
        return

    # 2. Dividir líneas y limitar a 80
    lineas_raw = [l.strip() for l in texto.split('\n') if l.strip()]
    advertencia = ""
    if len(lineas_raw) > MAX_LINEAS_CATALOGO:
        lineas_raw = lineas_raw[:MAX_LINEAS_CATALOGO]
        advertencia = f"\n\n⚠️ Solo se guardaron las primeras {MAX_LINEAS_CATALOGO} líneas."

    # 3. Hash para duplicados
    hash_md5 = hashlib.md5(texto.strip().encode('utf-8')).hexdigest()
    existe = await coleccion_catalogos.find_one({"proveedor_id": user_id, "hash": hash_md5})
    if existe:
        await update.message.reply_text("❌ Este listado es idéntico a uno que ya tienes publicado.")
        return

    # 4. Normalizar líneas para búsqueda
    lineas_db = [{"original": l, "normalizada": normalizar_texto(l)} for l in lineas_raw]

    # 5. Control de cantidad de catálogos (Máximo 2)
    catalogos_previos = await coleccion_catalogos.find({"proveedor_id": user_id, "es_admin": {"$ne": True}}).sort("fecha_creacion", 1).to_list(None)
    if len(catalogos_previos) >= MAX_CATALOGOS_PROVEEDOR:
        # Eliminar el más viejo (FIFO)
        await coleccion_catalogos.delete_one({"_id": catalogos_previos[0]["_id"]})
        advertencia += f"\n🗑️ Se eliminó tu catálogo más antiguo para guardar este nuevo (Máximo {MAX_CATALOGOS_PROVEEDOR})."

    # 6. Obtener provincia
    cliente = await coleccion_clientes.find_one({"_id": user_id})
    provincia = cliente.get("provincia") if cliente else None
    if not provincia:
        await update.message.reply_text("❌ Debes configurar tu provincia primero con /start.")
        context.user_data["estado"] = None; return

    # 7. Guardar en BD
    await coleccion_catalogos.insert_one({
        "proveedor_id": user_id,
        "provincia": provincia,
        "es_admin": False,
        "fecha_creacion": datetime.now(),
        "fecha_expiracion": None,
        "hash": hash_md5,
        "lineas": lineas_db
    })

    # 8. Asegurar que el proveedor exista
    await coleccion_proveedores.update_one(
        {"_id": user_id},
        {"$setOnInsert": {"nombre": update.effective_user.first_name or "Proveedor", "contacto": {}, "contacto_mostrar": ""}},
        upsert=True
    )

    context.user_data["estado"] = "esperando_contacto"
    context.user_data["medicinas_count"] = len(lineas_db)

    teclado = [[InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")], [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")], [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")]]
    await update.message.reply_text(f"✅ Se procesaron <b>{len(lineas_db)}</b> líneas.{advertencia}\n\nAhora elige cómo te contactarán:", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _mostrar_mi_catalogo(query, user_id, pagina=0):
    catalogos = await coleccion_catalogos.find({"proveedor_id": user_id, "es_admin": {"$ne": True}}).to_list(None)
    if not catalogos:
        await query.edit_message_text("📭 No tienes catálogos activos.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Publicar", callback_data="publicar")], [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]), parse_mode="HTML")
        return

    mensaje = f"📋 <b>Tus Catálogos</b> ({len(catalogos)}/{MAX_CATALOGOS_PROVEEDOR})\n\n"
    todas_las_lineas = []
    for i, cat in enumerate(catalogos, 1):
        for l in cat["lineas"]: todas_las_lineas.append(f"[C{i}] {l['original']}")

    inicio = pagina * LINEAS_PAGINACION
    fin = inicio + LINEAS_PAGINACION
    for l in todas_las_lineas[inicio:fin]:
        mensaje += f"• {esc(l)}\n"

    botones = []
    if fin < len(todas_las_lineas): botones.append([InlineKeyboardButton("➡️ Ver más", callback_data=f"pag_cat_{pagina+1}")])
    if pagina > 0: botones.append([InlineKeyboardButton("🔙 Atrás", callback_data=f"pag_cat_{pagina-1}")])
    botones.append([InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")])

    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(botones), parse_mode="HTML")


# ===== PERFIL Y CONTACTO =====

async def _mostrar_perfil(query, user_id):
    proveedor = await coleccion_proveedores.find_one({"_id": user_id})
    if proveedor:
        total_cats = await coleccion_catalogos.count_documents({"proveedor_id": user_id, "es_admin": {"$ne": True}})
        total_lineas = sum([len(c["lineas"]) for c in await coleccion_catalogos.find({"proveedor_id": user_id, "es_admin": {"$ne": True}}).to_list(None)])
        mensaje = (f"👤 <b>Perfil Proveedor</b>\n\n📛 {esc(proveedor.get('nombre'))}\n📞 {esc(proveedor.get('contacto_mostrar'))}\n"
                   f"📋 Catálogos: {total_cats}/{MAX_CATALOGOS_PROVEEDOR} ({total_lineas} líneas)\n")
        if await es_destacado_activo(proveedor): mensaje += "⭐ <b>Destacado</b>\n"
        link = f"t.me/MediCubaBot?start=proveedor_{user_id}"
        mensaje += f"🔗 <code>{link}</code>"
        teclado = [[InlineKeyboardButton("✏️ Editar Contacto", callback_data="editar_contacto")], [InlineKeyboardButton("📋 Ver Catálogos", callback_data="ver_mi_catalogo")], [InlineKeyboardButton("🔙 Volver", callback_data="volver")]]
    else:
        cliente = await coleccion_clientes.find_one({"_id": user_id})
        mensaje = f"👤 <b>Perfil Cliente</b>\n\n📍 {esc(cliente.get('provincia', 'N/A'))}\n📊 Búsquedas: {cliente.get('busquedas', 0)}"
        teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")

async def _procesar_contacto_callback(query, context, user_id, data):
    tipo = data.replace("contacto_", "")
    context.user_data["tipo_contacto"] = tipo
    if tipo in ["whatsapp", "ambos"]:
        context.user_data["estado"] = "esperando_telefono"
        await query.edit_message_text("📱 Escribe tu WhatsApp (ej: <code>+53 5 1234567</code>):", parse_mode="HTML")
    else:
        context.user_data["estado"] = "esperando_telegram"
        await query.edit_message_text("✈️ Escribe tu @usuario de Telegram:")

async def _telefono(update, context, user_id, texto):
    telefono = texto.strip()
    tipo = context.user_data.get("tipo_contacto", "whatsapp")
    async with await client.start_session() as session:
        await coleccion_proveedores.update_one({"_id": user_id}, {"$set": {"contacto.tipo": tipo, "contacto.whatsapp": telefono, "contacto_mostrar": telefono, "nombre": update.effective_user.first_name or "Proveedor"}}, session=session)
    if tipo == "ambos":
        context.user_data["estado"] = "esperando_telegram"
        await update.message.reply_text("✈️ Ahora tu @usuario de Telegram:")
    else:
        await _finalizar_registro(update, context, user_id)

async def _telegram_user(update, context, user_id, texto):
    tg_user = texto.strip()
    if not tg_user.startswith("@"): tg_user = "@" + tg_user
    tipo = context.user_data.get("tipo_contacto", "telegram")
    contacto_mostrar = f"{datos['proveedores'][user_id]['contacto'].get('whatsapp', '')} / {tg_user}" if tipo == "ambos" else tg_user
    await coleccion_proveedores.update_one({"_id": user_id}, {"$set": {"contacto.telegram": tg_user, "contacto_mostrar": contacto_mostrar, "nombre": update.effective_user.first_name or "Proveedor"}})
    await _finalizar_registro(update, context, user_id)

async def _finalizar_registro(update, context, user_id):
    editando = context.user_data.get("editando_contacto", False)
    proveedor = await coleccion_proveedores.find_one({"_id": user_id})
    if editando:
        mensaje = f"✅ Contacto actualizado a: {esc(proveedor.get('contacto_mostrar'))}"
        context.user_data["editando_contacto"] = False
    else:
        link = f"t.me/MediCubaBot?start=proveedor_{user_id}"
        mensaje = (f"✅ <b>¡Catálogo publicado!</b>\n\n📞 Contacto: {esc(proveedor.get('contacto_mostrar'))}\n"
                   f"🔗 Link: <code>{link}</code>\n\n⭐ ¿Quieres destacarte? Contacta al admin.")
    teclado = [[InlineKeyboardButton("🏠 Menú", callback_data="volver")]]
    await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="HTML")
    context.user_data["estado"] = None

async def _cambio_provincia(update, context, user_id, texto):
    PROVINCIAS = ["Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas", "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila", "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba", "Guantánamo", "Isla de la Juventud"]
    try:
        num = int(texto.strip())
        if 1 <= num <= len(PROVINCIAS):
            prov = PROVINCIAS[num-1]
            await coleccion_clientes.update_one({"_id": user_id}, {"$set": {"provincia": prov}})
            # Actualizar provincia en sus catálogos también
            await coleccion_catalogos.update_many({"proveedor_id": user_id, "es_admin": {"$ne": True}}, {"$set": {"provincia": prov}})
            await update.message.reply_text(f"✅ Provincia: <b>{esc(prov)}</b>", parse_mode="HTML")
        else: await update.message.reply_text("Número inválido."); return
    except ValueError: await update.message.reply_text("Envía solo el NÚMERO."); return
    context.user_data["estado"] = None


# ===== COMANDOS ADMIN =====

async def admin_cargar_listado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not es_admin(user_id): return
    if not context.args: await update.message.reply_text("Uso: <code>/admin_cargar_listado +5351234567</code>", parse_mode="HTML"); return
    context.user_data["admin_telefono"] = context.args[0]
    context.user_data["estado"] = "admin_esperando_listado"
    await update.message.reply_text("📥 Pega el listado de admin:")

async def _admin_listado(update, context, user_id, texto):
    if not es_admin(user_id): return
    palabra_prohibida = contiene_productos_no_medicos(texto)
    if palabra_prohibida: await update.message.reply_text(f"❌ Rechazado: {palabra_prohibida}"); return

    hash_md5 = hashlib.md5(texto.strip().encode('utf-8')).hexdigest()
    if await coleccion_catalogos.find_one({"hash": hash_md5}): await update.message.reply_text("❌ Duplicado."); return

    lineas_raw = [l.strip() for l in texto.split('\n') if l.strip()][:MAX_LINEAS_CATALOGO]
    lineas_db = [{"original": l, "normalizada": normalizar_texto(l)} for l in lineas_raw]
    telefono = context.user_data.get("admin_telefono")
    provincia = "Santiago de Cuba" # Default admin
    
    admin_prov_id = f"admin_{user_id}_{int(datetime.now().timestamp())}"
    await coleccion_proveedores.update_one({"_id": admin_prov_id}, {"$set": {"nombre": "Admin MediCuba", "contacto_mostrar": telefono, "contacto": {"tipo": "whatsapp", "whatsapp": telefono}}}, upsert=True)
    
    await coleccion_catalogos.insert_one({
        "proveedor_id": admin_prov_id, "provincia": provincia, "es_admin": True,
        "fecha_creacion": datetime.now(), "fecha_expiracion": datetime.now() + timedelta(days=DIAS_EXPIRACION_ADMIN),
        "hash": hash_md5, "lineas": lineas_db
    })
    await update.message.reply_text(f"✅ Cargado ({len(lineas_db)} líneas). Expira en {DIAS_EXPIRACION_ADMIN} días.", parse_mode="HTML")
    context.user_data["estado"] = None

async def destacar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not es_admin(user_id) or not context.args or len(context.args) < 2: return
    dias = int(context.args[1])
    fecha_fin = (datetime.now() + timedelta(days=dias)).isoformat()
    await coleccion_proveedores.update_one({"_id": context.args[0]}, {"$set": {"destacado_hasta": fecha_fin}})
    await update.message.reply_text(f"⭐ Destacado por {dias} días.", parse_mode="HTML")

async def _mostrar_estadisticas(query):
    c = await coleccion_clientes.count_documents({})
    p = await coleccion_proveedores.count_documents({})
    m = await coleccion_catalogos.count_documents({})
    d = sum(1 async for prov in coleccion_proveedores.find() if await es_destacado_activo(prov))
    await query.edit_message_text(f"📊 <b>Stats</b>\n\n👥 Clientes: {c}\n🏥 Proveedores: {p}\n📋 Catálogos: {m}\n⭐ Destacados: {d}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]), parse_mode="HTML")

async def _mostrar_proveedores_admin(query):
    provs = await coleccion_proveedores.find().to_list(10)
    msg = "👥 <b>Proveedores</b>\n\n"
    for p in provs: msg += f"• <code>{p['_id']}</code> - {esc(p.get('nombre'))}\n"
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]), parse_mode="HTML")

async def _mostrar_listados_admin(query):
    cats = await coleccion_catalogos.find({"es_admin": True}).to_list(10)
    msg = "📋 <b>Listados Admin</b>\n\n"
    for c in cats: msg += f"• ID: <code>{c['_id']}</code> | Expira: {c.get('fecha_expiracion','').strftime('%Y-%m-%d') if isinstance(c.get('fecha_expiracion'), datetime) else 'N/A'}\n"
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]), parse_mode="HTML")

async def _mostrar_destacados(query):
    dest = []
    async for p in coleccion_proveedores.find():
        if await es_destacado_activo(p): dest.append(p)
    if not dest: await query.edit_message_text("⭐ No hay destacados.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]), parse_mode="HTML"); return
    msg = "⭐ <b>Destacados</b>\n\n"
    for p in dest: msg += f"🏥 {esc(p.get('nombre'))} - 📞 {esc(p.get('contacto_mostrar'))}\n"
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]), parse_mode="HTML")


# ===== MAIN =====

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin_cargar_listado", admin_cargar_listado))
    application.add_handler(CommandHandler("destacar", destacar_cmd))
    application.add_handler(CallbackQueryHandler(manejador_callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensajes))
    
    print("🤖 MediCuba Bot (MongoDB + Fuzzy) iniciado...")
    
    import signal
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling())
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)
        loop.run_until_complete(stop_event.wait())
    except (KeyboardInterrupt, SystemExit): pass
    finally:
        loop.run_until_complete(application.updater.stop())
        loop.run_until_complete(application.stop())
        loop.run_until_complete(application.shutdown())
        loop.close()

if __name__ == "__main__":
    main()
