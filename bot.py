"""
BOT DE MEDICINAS CUBA - VERSIÓN PROFESIONAL
100% por botones | Proveedores | Catálogos | Links personalizados
Compatible con Python 3.14 y python-telegram-bot v21+
"""

import logging
import json
import os
import re
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ===== CONFIGURACIÓN =====
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8685939368:AAESfgUVeQG0qA8521Qx5LO_7Qm3LY27Qq0"
ADMIN_ID = 814338625  # Tu Telegram ID

# Estados para conversaciones
PROVINCIA, ESPERANDO_MEDICINA, ESPERANDO_LISTADO, ESPERANDO_CONTACTO, ESPERANDO_TELEFONO, ESPERANDO_TELEGRAM = range(6)

# Lock para proteger datos
datos_lock = asyncio.Lock()

# ===== BASE DE DATOS =====
ARCHIVO_DATOS = "medicinas_cuba.json"

DATOS_POR_DEFECTO = {
    "proveedores": {},  # telegram_id: {nombre, contacto, catalogo, destacado_hasta, link_token}
    "clientes": {},     # telegram_id: {provincia, ultima_busqueda}
    "administradores": [ADMIN_ID],
    "medicinas": [],    # {nombre, mg, precio, proveedor_id, provincia, zona, fecha}
    "provincias": ["Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas", "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila", "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba", "Guantánamo", "Isla de la Juventud"],
    "proveedores_destacados": []
}

def cargar_datos():
    if os.path.exists(ARCHIVO_DATOS):
        try:
            with open(ARCHIVO_DATOS, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return DATOS_POR_DEFECTO
    return DATOS_POR_DEFECTO

def guardar_datos(datos):
    with open(ARCHIVO_DATOS, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

datos = cargar_datos()

# ===== FUNCIONES AUXILIARES =====

def normalizar_texto(texto):
    """Elimina acentos y convierte a minúsculas"""
    texto = texto.lower()
    acentos = {'á':'a','é':'e','í':'i','ó':'o','ú':'u','ü':'u'}
    for a, b in acentos.items():
        texto = texto.replace(a, b)
    return texto

def extraer_medicinas_desde_texto(texto):
    """Extrae medicinas, mg y precios de un texto pegado"""
    lineas = texto.split('\n')
    medicinas = []
    
    for linea in lineas:
        linea = linea.strip()
        if not linea or len(linea) < 2:
            continue
        
        # Detectar patrones
        nombre = None
        mg = None
        precio = None
        
        # Patrón: "Medicina(mg)-precio" o "Medicina(mg) - precio"
        match = re.search(r'([A-Za-záéíóúüñ]+[A-Za-záéíóúüñ\s\(\)\-]+?)(?:\((\d+\s?mg)\))?\s*[-:]\s*(\d+)', linea, re.IGNORECASE)
        if match:
            nombre = match.group(1).strip()
            mg = match.group(2) if match.group(2) else None
            precio = match.group(3)
        else:
            # Patrón: "📌 Medicina: precio"
            match = re.search(r'📌\s*([A-Za-záéíóúüñ\s]+)[:：]\s*(\d+)', linea)
            if match:
                nombre = match.group(1).strip()
                precio = match.group(2)
            else:
                # Patrón: "*Medicina*💊" o "Medicina💊"
                match = re.search(r'\*?([A-Za-záéíóúüñ\s]+?)\*?[💊]', linea)
                if match:
                    nombre = match.group(1).strip()
        
        if nombre and len(nombre) > 1:
            medicinas.append({
                "nombre": normalizar_texto(nombre),
                "nombre_original": nombre,
                "mg": mg,
                "precio": precio
            })
    
    return medicinas

async def menu_principal(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id=None):
    """Muestra el menú principal con botones"""
    if user_id is None:
        user_id = str(update.effective_user.id)
    
    # Obtener provincia del cliente
    provincia = datos["clientes"].get(user_id, {}).get("provincia", "No seleccionada")
    es_admin = int(user_id) in datos["administradores"]
    
    teclado = [
        [InlineKeyboardButton("🔍 Buscar Medicina", callback_data="buscar")],
        [InlineKeyboardButton("📝 Publicar Catálogo", callback_data="publicar")],
        [InlineKeyboardButton("📍 Cambiar Provincia", callback_data="cambiar_provincia")],
        [InlineKeyboardButton("👤 Mi Perfil", callback_data="mi_perfil")],
        [InlineKeyboardButton("⭐ Proveedores Destacados", callback_data="destacados")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ]
    
    if es_admin:
        teclado.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    
    mensaje = f"🏥 **MediCuba**\n🩺 Tu salud, nuestra prioridad\n\n📍 **Tu provincia:** {provincia}\n\n¿Qué deseas hacer?"
    
    await update.callback_query.edit_message_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicio del bot - maneja links personalizados"""
    user_id = str(update.effective_user.id)
    
    # Verificar si es un link personalizado de proveedor
    if context.args and context.args[0].startswith("proveedor_"):
        proveedor_id = context.args[0].replace("proveedor_", "")
        if proveedor_id in datos["proveedores"]:
            # Mostrar catálogo del proveedor
            await mostrar_catalogo_proveedor(update, proveedor_id)
            return
    
    # Registrar cliente si no existe
    if user_id not in datos["clientes"]:
        datos["clientes"][user_id] = {}
        guardar_datos(datos)
    
    # Mostrar menú principal
    teclado = []
    provincia = datos["clientes"].get(user_id, {}).get("provincia", "No seleccionada")
    es_admin = int(user_id) in datos["administradores"]
    
    teclado = [
        [InlineKeyboardButton("🔍 Buscar Medicina", callback_data="buscar")],
        [InlineKeyboardButton("📝 Publicar Catálogo", callback_data="publicar")],
        [InlineKeyboardButton("📍 Cambiar Provincia", callback_data="cambiar_provincia")],
        [InlineKeyboardButton("👤 Mi Perfil", callback_data="mi_perfil")],
        [InlineKeyboardButton("⭐ Proveedores Destacados", callback_data="destacados")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ]
    
    if es_admin:
        teclado.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])
    
    mensaje = f"🏥 **MediCuba**\n🩺 Tu salud, nuestra prioridad\n\n📍 **Tu provincia:** {provincia}\n\n¿Qué deseas hacer?"
    
    await update.message.reply_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )

async def mostrar_catalogo_proveedor(update: Update, proveedor_id):
    """Muestra el catálogo de un proveedor específico"""
    proveedor = datos["proveedores"].get(proveedor_id)
    if not proveedor:
        await update.message.reply_text("❌ Proveedor no encontrado.")
        return
    
    medicinas = [m for m in datos["medicinas"] if m["proveedor_id"] == proveedor_id]
    
    if not medicinas:
        await update.message.reply_text(f"📭 {proveedor.get('nombre', 'Proveedor')} no tiene medicinas disponibles.")
        return
    
    mensaje = f"🏥 **{proveedor.get('nombre', 'Proveedor')}**\n"
    if proveedor.get("destacado_hasta"):
        mensaje += "⭐ **Proveedor Destacado** ⭐\n"
    mensaje += f"📞 **Contacto:** {proveedor.get('contacto_mostrar', 'No especificado')}\n"
    mensaje += "─" * 20 + "\n\n**📋 Catálogo:**\n"
    
    for m in medicinas[:20]:
        mg = f" ({m['mg']})" if m.get('mg') else ""
        precio = f" - {m['precio']} CUP" if m.get('precio') else ""
        mensaje += f"• {m['nombre_original']}{mg}{precio}\n"
    
    mensaje += "\n─" * 20 + "\n🩺 **MediCuba** - Encuentra más proveedores en @MediCubaBot"
    
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def botones_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones del menú"""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    
    if query.data == "buscar":
        context.user_data["estado"] = "esperando_medicina"
        await query.edit_message_text(
            "🔍 **Buscar Medicina**\n\nEscribe el nombre de la medicina que buscas:\n\n*Ejemplo:* `paracetamol`",
            parse_mode="Markdown"
        )
    
    elif query.data == "publicar":
        context.user_data["estado"] = "esperando_listado"
        await query.edit_message_text(
            "📝 **Publicar Catálogo**\n\n📋 **Instrucciones:**\n"
            "1. Copia tu listado de WhatsApp\n"
            "2. Pégalo aquí\n\n"
            "El bot extraerá automáticamente todas las medicinas.\n\n"
            "⚠️ *Si ya tienes un catálogo, este lo reemplazará.*",
            parse_mode="Markdown"
        )
    
    elif query.data == "cambiar_provincia":
        context.user_data["estado"] = "cambiando_provincia"
        lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(datos["provincias"])])
        await query.edit_message_text(
            f"📍 **Cambiar Provincia**\n\n{lista}\n\nResponde con el NÚMERO de tu provincia:",
            parse_mode="Markdown"
        )
    
    elif query.data == "mi_perfil":
        await mi_perfil(query, user_id)
    
    elif query.data == "destacados":
        await mostrar_destacados(query)
    
    elif query.data == "ayuda":
        await mostrar_ayuda(query)
    
    elif query.data == "admin_panel":
        if int(user_id) in datos["administradores"]:
            await admin_panel(query)
        else:
            await query.edit_message_text("❌ No tienes permisos de administrador.")

async def mi_perfil(query, user_id):
    """Muestra el perfil del usuario"""
    es_proveedor = user_id in datos["proveedores"]
    
    if es_proveedor:
        proveedor = datos["proveedores"][user_id]
        catalogo_count = len([m for m in datos["medicinas"] if m["proveedor_id"] == user_id])
        mensaje = f"👤 **Mi Perfil (Proveedor)**\n\n"
        mensaje += f"📛 **Nombre:** {proveedor.get('nombre', 'No especificado')}\n"
        mensaje += f"📞 **Contacto:** {proveedor.get('contacto_mostrar', 'No especificado')}\n"
        mensaje += f"📋 **Catálogo:** {catalogo_count} medicinas\n"
        if proveedor.get('link_token'):
            mensaje += f"🔗 **Link personalizado:** `t.me/MediCubaBot?start=proveedor_{user_id}`\n"
        mensaje += "\n¿Qué deseas hacer?\n\n"
        teclado = [
            [InlineKeyboardButton("✏️ Editar Contacto", callback_data="editar_contacto")],
            [InlineKeyboardButton("📋 Ver Mi Catálogo", callback_data="ver_mi_catalogo")],
            [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
        ]
        await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown")
    else:
        cliente = datos["clientes"].get(user_id, {})
        mensaje = f"👤 **Mi Perfil (Cliente)**\n\n"
        mensaje += f"📍 **Provincia:** {cliente.get('provincia', 'No seleccionada')}\n"
        mensaje += f"📊 **Búsquedas realizadas:** {cliente.get('busquedas', 0)}\n\n"
        teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]
        await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown")

async def mostrar_destacados(query):
    """Muestra proveedores destacados"""
    destacados = [p_id for p_id, p in datos["proveedores"].items() if p.get("destacado_hasta") and datetime.now() < datetime.fromisoformat(p["destacado_hasta"])]
    
    if not destacados:
        await query.edit_message_text(
            "⭐ **Proveedores Destacados**\n\nPor el momento no hay proveedores destacados.\n\n¿Quieres aparecer aquí? Contacta al administrador.",
            parse_mode="Markdown"
        )
        return
    
    mensaje = "⭐ **PROVEEDORES DESTACADOS** ⭐\n\n"
    for p_id in destacados[:5]:
        p = datos["proveedores"][p_id]
        mensaje += f"🏥 **{p.get('nombre', 'Proveedor')}**\n📞 {p.get('contacto_mostrar', '')}\n🔗 [Ver catálogo](t.me/MediCubaBot?start=proveedor_{p_id})\n\n"
    
    teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]
    await query.edit_message_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown", disable_web_page_preview=True)

async def mostrar_ayuda(query):
    """Muestra ayuda dividida por perfiles"""
    teclado = [
        [InlineKeyboardButton("👨‍💼 Para Proveedores", callback_data="ayuda_proveedores")],
        [InlineKeyboardButton("🛒 Para Clientes", callback_data="ayuda_clientes")],
        [InlineKeyboardButton("⚙️ General", callback_data="ayuda_general")],
        [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
    ]
    await query.edit_message_text(
        "❓ **Centro de Ayuda**\n\n¿Qué tipo de ayuda necesitas?",
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )

async def admin_panel(query):
    """Panel de administración"""
    teclado = [
        [InlineKeyboardButton("📥 Cargar Listado (Admin)", callback_data="admin_cargar")],
        [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_estadisticas")],
        [InlineKeyboardButton("👥 Ver Proveedores", callback_data="admin_proveedores")],
        [InlineKeyboardButton("⭐ Destacar Proveedor", callback_data="admin_destacar")],
        [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
    ]
    await query.edit_message_text(
        "🔧 **Panel de Administración**\n\n¿Qué deseas gestionar?",
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="Markdown"
    )

# ===== MANEJADORES DE MENSAJES =====

async def procesar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa mensajes de texto según el estado"""
    user_id = str(update.effective_user.id)
    texto = update.message.text
    estado = context.user_data.get("estado")
    
    if estado == "esperando_medicina":
        # Buscar medicina
        medicina_buscar = normalizar_texto(texto)
        provincia = datos["clientes"].get(user_id, {}).get("provincia")
        
        if not provincia:
            await update.message.reply_text("❌ Primero configura tu provincia con /start")
            return
        
        resultados = [m for m in datos["medicinas"] if medicina_buscar in m["nombre"] and m["provincia"] == provincia]
        
        if not resultados:
            await update.message.reply_text(
                f"❌ No encontré '{texto}' en {provincia}.\n\n💡 Sugerencias:\n• Revisa la ortografía\n• Prueba con otro nombre\n• Los proveedores pueden publicar su catálogo con /start"
            )
        else:
            # Agrupar por proveedor
            por_proveedor = {}
            for r in resultados:
                if r["proveedor_id"] not in por_proveedor:
                    por_proveedor[r["proveedor_id"]] = []
                por_proveedor[r["proveedor_id"]].append(r)
            
            mensaje = f"🔍 **{texto.upper()}** en {provincia}\n\n✅ Encontré {len(resultados)} coincidencias:\n\n"
            
            for p_id, items in list(por_proveedor.items())[:5]:
                p = datos["proveedores"].get(p_id, {})
                destacado = "⭐ " if p.get("destacado_hasta") and datetime.now() < datetime.fromisoformat(p["destacado_hasta"]) else ""
                mensaje += f"{destacado}**Proveedor:** {p.get('nombre', 'Anónimo')}\n"
                mensaje += f"📞 {p.get('contacto_mostrar', 'No disponible')}\n"
                for item in items[:3]:
                    mg = f" ({item['mg']})" if item.get('mg') else ""
                    precio = f" - {item['precio']} CUP" if item.get('precio') else ""
                    mensaje += f"   • {item['nombre_original']}{mg}{precio}\n"
                mensaje += "\n"
            
            # Botón de contacto con mensaje pre-escrito
            primer_proveedor = list(por_proveedor.keys())[0]
            contacto = datos["proveedores"].get(primer_proveedor, {}).get("contacto")
            if contacto and contacto.get("tipo") in ["whatsapp", "ambos"]:
                telefono = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
                mensaje_pre = f"🩺 Hola, te contacto a través de MediCuba (t.me/MediCubaBot). ¿Tienes disponible {texto}?"
                enlace = f"https://wa.me/{telefono}?text={mensaje_pre.replace(' ', '%20')}"
                teclado = [[InlineKeyboardButton("📞 Contactar Proveedor", url=enlace)]]
                await update.message.reply_text(mensaje, reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown")
            else:
                await update.message.reply_text(mensaje, parse_mode="Markdown")
        
        context.user_data["estado"] = None
    
    elif estado == "esperando_listado":
        # Procesar listado de medicinas
        medicinas = extraer_medicinas_desde_texto(texto)
        
        if not medicinas:
            await update.message.reply_text("❌ No pude extraer medicinas de ese texto. Asegúrate de que tenga nombres como en los ejemplos.")
            return
        
        # Guardar o reemplazar catálogo del proveedor
        if user_id not in datos["proveedores"]:
            datos["proveedores"][user_id] = {
                "nombre": update.effective_user.first_name,
                "contacto": {},
                "catalogo_activo": True,
                "fecha_actualizacion": datetime.now().isoformat()
            }
        
        # Eliminar catálogo anterior
        datos["medicinas"] = [m for m in datos["medicinas"] if m["proveedor_id"] != user_id]
        
        # Guardar nuevo catálogo
        provincia = datos["clientes"].get(user_id, {}).get("provincia", "Santiago de Cuba")
        for m in medicinas:
            datos["medicinas"].append({
                "nombre": m["nombre"],
                "nombre_original": m["nombre_original"],
                "mg": m.get("mg"),
                "precio": m.get("precio"),
                "proveedor_id": user_id,
                "provincia": provincia,
                "fecha": datetime.now().isoformat()
            })
        
        # Generar link personalizado
        datos["proveedores"][user_id]["link_token"] = user_id
        
        guardar_datos(datos)
        
        # Preguntar forma de contacto
        context.user_data["estado"] = "esperando_contacto"
        context.user_data["medicinas_count"] = len(medicinas)
        
        teclado = [
            [InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")],
            [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")],
            [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")]
        ]
        await update.message.reply_text(
            f"✅ Se extrajeron {len(medicinas)} medicinas.\n\nAhora elige cómo prefieres que te contacten:",
            reply_markup=InlineKeyboardMarkup(teclado)
        )
    
    elif estado == "cambiando_provincia":
        try:
            num = int(texto)
            if 1 <= num <= len(datos["provincias"]):
                provincia = datos["provincias"][num - 1]
                if user_id not in datos["clientes"]:
                    datos["clientes"][user_id] = {}
                datos["clientes"][user_id]["provincia"] = provincia
                guardar_datos(datos)
                await update.message.reply_text(f"✅ Provincia cambiada a: {provincia}")
            else:
                await update.message.reply_text(f"Número inválido. Elige entre 1 y {len(datos['provincias'])}")
        except ValueError:
            await update.message.reply_text("Envía solo el NÚMERO de la provincia.")
        
        context.user_data["estado"] = None

async def procesar_contacto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja la selección de tipo de contacto"""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    
    tipo = query.data.replace("contacto_", "")
    context.user_data["tipo_contacto"] = tipo
    
    if tipo in ["whatsapp", "ambos"]:
        context.user_data["estado"] = "esperando_telefono"
        await query.edit_message_text("📱 Escribe tu número de WhatsApp (ej: +53 5 1234567):")
    elif tipo == "telegram":
        context.user_data["estado"] = "esperando_telegram"
        await query.edit_message_text("✈️ Escribe tu @usuario de Telegram:")

async def procesar_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda el teléfono del proveedor"""
    user_id = str(update.effective_user.id)
    telefono = update.message.text.strip()
    tipo = context.user_data.get("tipo_contacto", "whatsapp")
    
    if user_id not in datos["proveedores"]:
        datos["proveedores"][user_id] = {}
    
    if "contacto" not in datos["proveedores"][user_id]:
        datos["proveedores"][user_id]["contacto"] = {}
    
    datos["proveedores"][user_id]["contacto"]["tipo"] = tipo
    if tipo in ["whatsapp", "ambos"]:
        datos["proveedores"][user_id]["contacto"]["whatsapp"] = telefono
    
    if tipo == "ambos":
        context.user_data["estado"] = "esperando_telegram"
        await update.message.reply_text("✈️ Ahora escribe tu @usuario de Telegram:")
    else:
        # Generar mensaje de contacto
        contacto_mostrar = telefono if tipo == "whatsapp" else f"@{datos['proveedores'][user_id]['contacto'].get('telegram', '')}"
        datos["proveedores"][user_id]["contacto_mostrar"] = contacto_mostrar
        datos["proveedores"][user_id]["nombre"] = update.effective_user.first_name
        guardar_datos(datos)
        
        medicinas_count = context.user_data.get("medicinas_count", 0)
        link = f"t.me/MediCubaBot?start=proveedor_{user_id}"
        
        await update.message.reply_text(
            f"✅ **¡Catálogo publicado!**\n\n"
            f"📊 Se registraron {medicinas_count} medicinas.\n"
            f"📞 Contacto: {contacto_mostrar}\n\n"
            f"🔗 **Tu link personalizado:**\n`{link}`\n\n"
            f"⭐ ¿Quieres aparecer como Proveedor Destacado? Contacta al administrador.\n\n"
            f"🩺 **MediCuba** - Conectando pacientes con proveedores",
            parse_mode="Markdown"
        )
        context.user_data["estado"] = None

async def procesar_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda el @usuario de Telegram"""
    user_id = str(update.effective_user.id)
    telegram_user = update.message.text.strip()
    
    if not telegram_user.startswith("@"):
        telegram_user = "@" + telegram_user
    
    datos["proveedores"][user_id]["contacto"]["telegram"] = telegram_user
    
    tipo = context.user_data.get("tipo_contacto", "telegram")
    if tipo == "ambos":
        contacto_mostrar = f"{datos['proveedores'][user_id]['contacto'].get('whatsapp', '')} / {telegram_user}"
    else:
        contacto_mostrar = telegram_user
    
    datos["proveedores"][user_id]["contacto_mostrar"] = contacto_mostrar
    datos["proveedores"][user_id]["nombre"] = update.effective_user.first_name
    guardar_datos(datos)
    
    medicinas_count = context.user_data.get("medicinas_count", 0)
    link = f"t.me/MediCubaBot?start=proveedor_{user_id}"
    
    await update.message.reply_text(
        f"✅ **¡Catálogo publicado!**\n\n"
        f"📊 Se registraron {medicinas_count} medicinas.\n"
        f"📞 Contacto: {contacto_mostrar}\n\n"
        f"🔗 **Tu link personalizado:**\n`{link}`\n\n"
        f"⭐ ¿Quieres aparecer como Proveedor Destacado? Contacta al administrador.\n\n"
        f"🩺 **MediCuba** - Conectando pacientes con proveedores",
        parse_mode="Markdown"
    )
    context.user_data["estado"] = None

# ===== COMANDO ADMIN =====

async def admin_cargar_listado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para que el admin cargue listados manualmente"""
    user_id = str(update.effective_user.id)
    
    if int(user_id) not in datos["administradores"]:
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "📥 **Modo Administrador - Cargar Listado**\n\n"
            "Uso: `/admin_cargar_listado +5351234567`\n\n"
            "Luego pega el listado de medicinas.",
            parse_mode="Markdown"
        )
        return
    
    telefono = context.args[0]
    context.user_data["admin_telefono"] = telefono
    context.user_data["estado"] = "admin_esperando_listado"
    
    await update.message.reply_text(
        f"📥 Teléfono asignado: `{telefono}`\n\nAhora pega el listado de medicinas (como en los ejemplos de WhatsApp):",
        parse_mode="Markdown"
    )

async def procesar_admin_listado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa listado cargado por admin"""
    user_id = str(update.effective_user.id)
    
    if int(user_id) not in datos["administradores"]:
        return
    
    texto = update.message.text
    telefono = context.user_data.get("admin_telefono")
    estado = context.user_data.get("estado")
    
    if estado != "admin_esperando_listado":
        return
    
    medicinas = extraer_medicinas_desde_texto(texto)
    
    if not medicinas:
        await update.message.reply_text("❌ No pude extraer medicinas de ese texto.")
        return
    
    # Crear proveedor admin
    admin_proveedor_id = f"admin_{user_id}_{int(datetime.now().timestamp())}"
    datos["proveedores"][admin_proveedor_id] = {
        "nombre": "Administrador MediCuba",
        "contacto": {"tipo": "whatsapp", "whatsapp": telefono},
        "contacto_mostrar": telefono,
        "catalogo_activo": True,
        "fecha_actualizacion": datetime.now().isoformat()
    }
    
    # Guardar medicinas
    for m in medicinas:
        datos["medicinas"].append({
            "nombre": m["nombre"],
            "nombre_original": m["nombre_original"],
            "mg": m.get("mg"),
            "precio": m.get("precio"),
            "proveedor_id": admin_proveedor_id,
            "provincia": "Santiago de Cuba",
            "fecha": datetime.now().isoformat()
        })
    
    guardar_datos(datos)
    
    await update.message.reply_text(
        f"✅ **Listado cargado por Administrador**\n\n"
        f"📊 Se registraron {len(medicinas)} medicinas.\n"
        f"📞 Teléfono asignado: {telefono}\n"
        f"📍 Provincia: Santiago de Cuba\n\n"
        f"Ya están disponibles en las búsquedas.",
        parse_mode="Markdown"
    )
    
    context.user_data["estado"] = None
    context.user_data["admin_telefono"] = None

# ===== MAIN =====

def main():
    application = Application.builder().token(TOKEN).build()
    
    # Manejadores de comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin_cargar_listado", admin_cargar_listado))
    
    # Manejadores de callbacks (botones)
    application.add_handler(CallbackQueryHandler(botones_callback))
    application.add_handler(CallbackQueryHandler(procesar_contacto_callback, pattern="^contacto_"))
    
    # Manejadores de mensajes
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_texto))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_telefono, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_telegram, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_admin_listado, block=False))
    
    print("🤖 MediCuba Bot iniciado...")
    application.run_polling()

if __name__ == "__main__":
    main()
