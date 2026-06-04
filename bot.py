"""
BOT DE MEDICINAS CUBA - VERSIÓN PROFESIONAL
100% por botones | Proveedores | Catálogos | Links personalizados
Compatible con Python 3.14 y python-telegram-bot v21+
"""

import logging
import json
import os
import re
import html
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ===== CONFIGURACIÓN =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = "8685939368:AAESfgUVeQG0qA8521Qx5LO_7Qm3LY27Qq0"
ADMIN_ID = 814338625

datos_lock = asyncio.Lock()

# ===== BASE DE DATOS =====
ARCHIVO_DATOS = "medicinas_cuba.json"

DATOS_POR_DEFECTO = {
    "proveedores": {},
    "clientes": {},
    "administradores": [ADMIN_ID],
    "medicinas": [],
    "provincias": [
        "Pinar del Río", "Artemisa", "La Habana", "Mayabeque", "Matanzas",
        "Cienfuegos", "Villa Clara", "Sancti Spíritus", "Ciego de Ávila",
        "Camagüey", "Las Tunas", "Granma", "Holguín", "Santiago de Cuba",
        "Guantánamo", "Isla de la Juventud"
    ]
}


def cargar_datos():
    if os.path.exists(ARCHIVO_DATOS):
        try:
            with open(ARCHIVO_DATOS, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for key in DATOS_POR_DEFECTO:
                    if key not in data:
                        data[key] = DATOS_POR_DEFECTO[key]
                return data
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error cargando datos: {e}. Usando datos por defecto.")
    return json.loads(json.dumps(DATOS_POR_DEFECTO))


def guardar_datos(datos_a_guardar):
    with open(ARCHIVO_DATOS, 'w', encoding='utf-8') as f:
        json.dump(datos_a_guardar, f, ensure_ascii=False, indent=2)


datos = cargar_datos()


# ===== FUNCIONES AUXILIARES =====

def esc(texto):
    """Escapa texto para HTML seguro en Telegram"""
    if texto is None:
        return ""
    return html.escape(str(texto))


def normalizar_texto(texto):
    """Elimina acentos y convierte a minúsculas"""
    texto = texto.lower()
    acentos = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ü': 'u'}
    for a, b in acentos.items():
        texto = texto.replace(a, b)
    return texto


def es_admin(user_id):
    return int(user_id) in datos["administradores"]


def es_destacado_activo(proveedor):
    """Verifica si un proveedor tiene destacado vigente"""
    if not proveedor.get("destacado_hasta"):
        return False
    try:
        return datetime.now() < datetime.fromisoformat(proveedor["destacado_hasta"])
    except (ValueError, TypeError):
        return False


def generar_menu_principal(user_id):
    """Genera texto y teclado del menú principal"""
    provincia = datos["clientes"].get(str(user_id), {}).get("provincia", "No seleccionada")

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

    texto = (
        f"🏥 <b>MediCuba</b>\n"
        f"🩺 Tu salud, nuestra prioridad\n\n"
        f"📍 <b>Tu provincia:</b> {esc(provincia)}\n\n"
        f"¿Qué deseas hacer?"
    )
    return texto, InlineKeyboardMarkup(teclado)


async def enviar_menu_callback(query, user_id):
    """Envía menú principal editando mensaje (para callbacks)"""
    texto, teclado = generar_menu_principal(user_id)
    try:
        await query.edit_message_text(texto, reply_markup=teclado, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, reply_markup=teclado, parse_mode="HTML")


async def enviar_menu_mensaje(update, user_id):
    """Envía menú principal como mensaje nuevo"""
    texto, teclado = generar_menu_principal(user_id)
    await update.message.reply_text(texto, reply_markup=teclado, parse_mode="HTML")


def extraer_medicinas_desde_texto(texto):
    """Extrae medicinas, mg y precios de un texto pegado"""
    lineas = texto.split('\n')
    medicinas = []

    for linea in lineas:
        linea = linea.strip()
        if not linea or len(linea) < 2:
            continue

        nombre = mg = precio = None

        # Patrón: "Medicina(mg)-precio" o "Medicina(mg) - precio"
        match = re.search(
            r'([A-Za-záéíóúüñ]+[A-Za-záéíóúüñ\s\(\)\-]+?)(?:\((\d+\s?mg)\))?\s*[-:]\s*(\d+)',
            linea, re.IGNORECASE
        )
        if match:
            nombre = match.group(1).strip()
            mg = match.group(2) if match.group(2) else None
            precio = match.group(3)
        else:
            match = re.search(r'📌\s*([A-Za-záéíóúüñ\s]+)[:：]\s*(\d+)', linea)
            if match:
                nombre = match.group(1).strip()
                precio = match.group(2)
            else:
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


# ===== COMANDO /START =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Link personalizado de proveedor
    if context.args and context.args[0].startswith("proveedor_"):
        proveedor_id = context.args[0].replace("proveedor_", "")
        await mostrar_catalogo_proveedor_msg(update, proveedor_id)
        return

    # Registrar cliente
    async with datos_lock:
        if user_id not in datos["clientes"]:
            datos["clientes"][user_id] = {}
            guardar_datos(datos)

    await enviar_menu_mensaje(update, user_id)


async def mostrar_catalogo_proveedor_msg(update, proveedor_id):
    """Muestra catálogo de proveedor (desde mensaje nuevo)"""
    proveedor = datos["proveedores"].get(proveedor_id)
    if not proveedor:
        await update.message.reply_text("❌ Proveedor no encontrado.")
        return

    medicinas = [m for m in datos["medicinas"] if m["proveedor_id"] == proveedor_id]

    if not medicinas:
        await update.message.reply_text(
            f"📭 {esc(proveedor.get('nombre', 'Proveedor'))} no tiene medicinas disponibles."
        )
        return

    mensaje = f"🏥 <b>{esc(proveedor.get('nombre', 'Proveedor'))}</b>\n"
    if es_destacado_activo(proveedor):
        mensaje += "⭐ <b>Proveedor Destacado</b> ⭐\n"
    mensaje += f"📞 <b>Contacto:</b> {esc(proveedor.get('contacto_mostrar', 'No especificado'))}\n"
    mensaje += "─" * 20 + "\n\n<b>📋 Catálogo:</b>\n"

    for m in medicinas[:30]:
        mg = f" ({esc(m['mg'])})" if m.get('mg') else ""
        precio = f" - {esc(m['precio'])} CUP" if m.get('precio') else ""
        mensaje += f"• {esc(m['nombre_original'])}{mg}{precio}\n"

    if len(medicinas) > 30:
        mensaje += f"\n... y {len(medicinas) - 30} más."

    mensaje += "\n" + "─" * 20 + "\n🩺 <b>MediCuba</b> - Encuentra más proveedores en @MediCubaBot"

    teclado = [[InlineKeyboardButton("🏠 Ir al Bot", callback_data="volver")]]
    await update.message.reply_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )


# ===== HANDLER ÚNICO DE CALLBACKS =====

async def manejador_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja TODOS los callbacks de botones"""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data = query.data

    # ──── Navegación ────
    if data == "volver":
        await enviar_menu_callback(query, user_id)

    # ──── Buscar ────
    elif data == "buscar":
        provincia = datos["clientes"].get(user_id, {}).get("provincia")
        if not provincia:
            await query.edit_message_text(
                "❌ Primero debes configurar tu provincia.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📍 Seleccionar Provincia", callback_data="cambiar_provincia")]
                ])
            )
            return
        context.user_data["estado"] = "esperando_medicina"
        await query.edit_message_text(
            "🔍 <b>Buscar Medicina</b>\n\n"
            "Escribe el nombre de la medicina que buscas:\n\n"
            "<i>Ejemplo:</i> <code>paracetamol</code>",
            parse_mode="HTML"
        )

    # ──── Publicar ────
    elif data == "publicar":
        context.user_data["estado"] = "esperando_listado"
        await query.edit_message_text(
            "📝 <b>Publicar Catálogo</b>\n\n"
            "📋 <b>Instrucciones:</b>\n"
            "1. Copia tu listado de WhatsApp\n"
            "2. Pégalo aquí\n\n"
            "El bot extraerá automáticamente todas las medicinas.\n\n"
            "⚠️ <i>Si ya tienes un catálogo, este lo reemplazará.</i>",
            parse_mode="HTML"
        )

    # ──── Cambiar provincia ────
    elif data == "cambiar_provincia":
        context.user_data["estado"] = "cambiando_provincia"
        lista = "\n".join([f"{i+1}. {p}" for i, p in enumerate(datos["provincias"])])
        await query.edit_message_text(
            f"📍 <b>Cambiar Provincia</b>\n\n{lista}\n\n"
            f"Responde con el NÚMERO de tu provincia:",
            parse_mode="HTML"
        )

    # ──── Mi perfil ────
    elif data == "mi_perfil":
        await _mostrar_perfil(query, user_id)

    # ──── Editar contacto ────
    elif data == "editar_contacto":
        context.user_data["editando_contacto"] = True
        teclado = [
            [InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")],
            [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")],
            [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")],
            [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]
        ]
        await query.edit_message_text(
            "✏️ <b>Editar Contacto</b>\n\n¿Cómo prefieres que te contacten?",
            reply_markup=InlineKeyboardMarkup(teclado),
            parse_mode="HTML"
        )

    # ──── Ver mi catálogo ────
    elif data == "ver_mi_catalogo":
        await _mostrar_mi_catalogo(query, user_id)

    # ──── Proveedores destacados ────
    elif data == "destacados":
        await _mostrar_destacados(query)

    # ──── Ayuda ────
    elif data == "ayuda":
        teclado = [
            [InlineKeyboardButton("👨‍💼 Para Proveedores", callback_data="ayuda_proveedores")],
            [InlineKeyboardButton("🛒 Para Clientes", callback_data="ayuda_clientes")],
            [InlineKeyboardButton("⚙️ General", callback_data="ayuda_general")],
            [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
        ]
        await query.edit_message_text(
            "❓ <b>Centro de Ayuda</b>\n\n¿Qué tipo de ayuda necesitas?",
            reply_markup=InlineKeyboardMarkup(teclado),
            parse_mode="HTML"
        )

    elif data == "ayuda_proveedores":
        await query.edit_message_text(
            "👨‍💼 <b>Ayuda para Proveedores</b>\n\n"
            "1. Pulsa <b>Publicar Catálogo</b>\n"
            "2. Pega tu listado de WhatsApp\n"
            "3. El bot extrae las medicinas automáticamente\n"
            "4. Elige tu forma de contacto\n"
            "5. Recibe un link personalizado para compartir\n\n"
            "<b>Tip:</b> Usa el formato <code>Medicina(mg) - precio</code> "
            "para mejores resultados.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver a Ayuda", callback_data="ayuda")]
            ]),
            parse_mode="HTML"
        )

    elif data == "ayuda_clientes":
        await query.edit_message_text(
            "🛒 <b>Ayuda para Clientes</b>\n\n"
            "1. Configura tu provincia al inicio\n"
            "2. Pulsa <b>Buscar Medicina</b>\n"
            "3. Escribe el nombre\n"
            "4. Contacta al proveedor directamente\n\n"
            "<b>Tip:</b> Usa nombres genéricos (paracetamol, no Panadol).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver a Ayuda", callback_data="ayuda")]
            ]),
            parse_mode="HTML"
        )

    elif data == "ayuda_general":
        await query.edit_message_text(
            "⚙️ <b>Información General</b>\n\n"
            "🤖 <b>MediCuba Bot</b> conecta pacientes con proveedores.\n\n"
            "• Los precios son informativos\n"
            "• Siempre verifica disponibilidad\n"
            "• Reporta problemas al administrador\n\n"
            "📧 Contacto: @MediCubaAdmin",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver a Ayuda", callback_data="ayuda")]
            ]),
            parse_mode="HTML"
        )

    # ──── Tipo de contacto ────
    elif data.startswith("contacto_"):
        await _procesar_contacto_callback(query, context, user_id, data)

    # ──── Admin ────
    elif data == "admin_panel":
        if es_admin(user_id):
            teclado = [
                [InlineKeyboardButton("📥 Cargar Listado", callback_data="admin_cargar")],
                [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_estadisticas")],
                [InlineKeyboardButton("👥 Ver Proveedores", callback_data="admin_proveedores")],
                [InlineKeyboardButton("⭐ Destacar Proveedor", callback_data="admin_destacar")],
                [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
            ]
            await query.edit_message_text(
                "🔧 <b>Panel de Administración</b>\n\n¿Qué deseas gestionar?",
                reply_markup=InlineKeyboardMarkup(teclado),
                parse_mode="HTML"
            )
        else:
            await query.edit_message_text("❌ No tienes permisos de administrador.")

    elif data == "admin_cargar":
        if es_admin(user_id):
            await query.edit_message_text(
                "📥 <b>Cargar Listado (Admin)</b>\n\n"
                "Usa el comando:\n"
                "<code>/admin_cargar_listado +5351234567</code>\n\n"
                "Luego pega el listado de medicinas.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]
                ]),
                parse_mode="HTML"
            )

    elif data == "admin_estadisticas":
        if es_admin(user_id):
            await _mostrar_estadisticas(query)

    elif data == "admin_proveedores":
        if es_admin(user_id):
            await _mostrar_proveedores_admin(query)

    elif data == "admin_destacar":
        if es_admin(user_id):
            await _destacar_proveedor_admin(query)

    else:
        logger.warning(f"Callback no reconocido: {data}")


# ===== FUNCIONES DE VISTA =====

async def _mostrar_perfil(query, user_id):
    es_prov = user_id in datos["proveedores"]

    if es_prov:
        proveedor = datos["proveedores"][user_id]
        catalogo_count = len([m for m in datos["medicinas"] if m["proveedor_id"] == user_id])
        mensaje = (
            f"👤 <b>Mi Perfil (Proveedor)</b>\n\n"
            f"📛 <b>Nombre:</b> {esc(proveedor.get('nombre', 'No especificado'))}\n"
            f"📞 <b>Contacto:</b> {esc(proveedor.get('contacto_mostrar', 'No especificado'))}\n"
            f"📋 <b>Catálogo:</b> {catalogo_count} medicinas\n"
        )
        if proveedor.get('link_token'):
            link = f"t.me/MediCubaBot?start=proveedor_{user_id}"
            mensaje += f"🔗 <b>Link personalizado:</b> <code>{link}</code>\n"
        if es_destacado_activo(proveedor):
            mensaje += "⭐ <b>Proveedor Destacado</b>\n"
        mensaje += "\n¿Qué deseas hacer?"

        teclado = [
            [InlineKeyboardButton("✏️ Editar Contacto", callback_data="editar_contacto")],
            [InlineKeyboardButton("📋 Ver Mi Catálogo", callback_data="ver_mi_catalogo")],
            [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
        ]
    else:
        cliente = datos["clientes"].get(user_id, {})
        mensaje = (
            f"👤 <b>Mi Perfil (Cliente)</b>\n\n"
            f"📍 <b>Provincia:</b> {esc(cliente.get('provincia', 'No seleccionada'))}\n"
            f"📊 <b>Búsquedas realizadas:</b> {cliente.get('busquedas', 0)}\n\n"
        )
        teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]

    await query.edit_message_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )


async def _mostrar_mi_catalogo(query, user_id):
    medicinas = [m for m in datos["medicinas"] if m["proveedor_id"] == user_id]

    if not medicinas:
        await query.edit_message_text(
            "📭 No tienes medicinas en tu catálogo.\n\n"
            "Usa <b>Publicar Catálogo</b> para agregar.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Publicar", callback_data="publicar")],
                [InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]
            ]),
            parse_mode="HTML"
        )
        return

    mensaje = f"📋 <b>Tu Catálogo</b> ({len(medicinas)} medicinas)\n\n"
    for m in medicinas[:30]:
        mg = f" ({esc(m['mg'])})" if m.get('mg') else ""
        precio = f" - {esc(m['precio'])} CUP" if m.get('precio') else ""
        mensaje += f"• {esc(m['nombre_original'])}{mg}{precio}\n"

    if len(medicinas) > 30:
        mensaje += f"\n... y {len(medicinas) - 30} más."

    teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="mi_perfil")]]
    await query.edit_message_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )


async def _mostrar_destacados(query):
    destacados = [
        p_id for p_id, p in datos["proveedores"].items()
        if es_destacado_activo(p)
    ]

    if not destacados:
        await query.edit_message_text(
            "⭐ <b>Proveedores Destacados</b>\n\n"
            "Por el momento no hay proveedores destacados.\n\n"
            "¿Quieres aparecer aquí? Contacta al administrador.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
            ]),
            parse_mode="HTML"
        )
        return

    mensaje = "⭐ <b>PROVEEDORES DESTACADOS</b> ⭐\n\n"
    for p_id in destacados[:5]:
        p = datos["proveedores"][p_id]
        link = f"t.me/MediCubaBot?start=proveedor_{p_id}"
        mensaje += (
            f"🏥 <b>{esc(p.get('nombre', 'Proveedor'))}</b>\n"
            f"📞 {esc(p.get('contacto_mostrar', ''))}\n"
            f'🔗 <a href="{link}">Ver catálogo</a>\n\n'
        )

    teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]
    await query.edit_message_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def _mostrar_estadisticas(query):
    total_prov = len(datos["proveedores"])
    total_clientes = len(datos["clientes"])
    total_med = len(datos["medicinas"])
    total_dest = sum(1 for p in datos["proveedores"].values() if es_destacado_activo(p))

    mensaje = (
        "📊 <b>Estadísticas</b>\n\n"
        f"👥 <b>Clientes:</b> {total_clientes}\n"
        f"🏥 <b>Proveedores:</b> {total_prov}\n"
        f"💊 <b>Medicinas:</b> {total_med}\n"
        f"⭐ <b>Destacados activos:</b> {total_dest}\n"
    )

    teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]
    await query.edit_message_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )


async def _mostrar_proveedores_admin(query):
    if not datos["proveedores"]:
        await query.edit_message_text(
            "👥 No hay proveedores registrados.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]
            ]),
            parse_mode="HTML"
        )
        return

    mensaje = "👥 <b>Proveedores Registrados</b>\n\n"
    for p_id, p in list(datos["proveedores"].items())[:10]:
        count = len([m for m in datos["medicinas"] if m["proveedor_id"] == p_id])
        dest = "⭐" if es_destacado_activo(p) else "  "
        mensaje += (
            f"{dest} <b>{esc(p.get('nombre', 'Sin nombre'))}</b> — "
            f"{count} medicinas | 📞 {esc(p.get('contacto_mostrar', 'N/A'))}\n"
            f"   ID: <code>{p_id}</code>\n"
        )

    if len(datos["proveedores"]) > 10:
        mensaje += f"\n... y {len(datos['proveedores']) - 10} más."

    teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]
    await query.edit_message_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )


async def _destacar_proveedor_admin(query):
    if not datos["proveedores"]:
        await query.edit_message_text(
            "👥 No hay proveedores para destacar.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]
            ]),
            parse_mode="HTML"
        )
        return

    mensaje = (
        "⭐ <b>Destacar Proveedor</b>\n\n"
        "Usa el comando:\n"
        "<code>/destacar PROVEEDOR_ID DIAS</code>\n\n"
        "<b>Proveedores:</b>\n"
    )
    for p_id, p in list(datos["proveedores"].items())[:15]:
        estado = "⭐" if es_destacado_activo(p) else "  "
        mensaje += f"{estado} <code>{p_id}</code> — {esc(p.get('nombre', 'Sin nombre'))}\n"

    teclado = [[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]
    await query.edit_message_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )


async def _procesar_contacto_callback(query, context, user_id, data):
    """Maneja selección de tipo de contacto (WhatsApp/Telegram/Ambos)"""
    tipo = data.replace("contacto_", "")
    context.user_data["tipo_contacto"] = tipo

    if tipo in ["whatsapp", "ambos"]:
        context.user_data["estado"] = "esperando_telefono"
        await query.edit_message_text(
            "📱 Escribe tu número de WhatsApp (ej: <code>+53 5 1234567</code>):",
            parse_mode="HTML"
        )
    elif tipo == "telegram":
        context.user_data["estado"] = "esperando_telegram"
        await query.edit_message_text(
            "✈️ Escribe tu @usuario de Telegram:"
        )


# ===== HANDLER ÚNICO DE MENSAJES =====

async def procesar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa TODOS los mensajes de texto según estado"""
    user_id = str(update.effective_user.id)
    texto = update.message.text
    estado = context.user_data.get("estado")

    if estado == "esperando_medicina":
        await _busqueda(update, context, user_id, texto)
    elif estado == "esperando_listado":
        await _listado(update, context, user_id, texto)
    elif estado == "cambiando_provincia":
        await _cambio_provincia(update, context, user_id, texto)
    elif estado == "esperando_telefono":
        await _telefono(update, context, user_id, texto)
    elif estado == "esperando_telegram":
        await _telegram_user(update, context, user_id, texto)
    elif estado == "admin_esperando_listado":
        await _admin_listado(update, context, user_id, texto)
    # else: ignorar mensaje sin estado activo


async def _busqueda(update, context, user_id, texto):
    medicina_buscar = normalizar_texto(texto)
    provincia = datos["clientes"].get(user_id, {}).get("provincia")

    if not provincia:
        await update.message.reply_text("❌ Primero configura tu provincia con /start")
        context.user_data["estado"] = None
        return

    # Incrementar contador
    async with datos_lock:
        datos["clientes"][user_id]["busquedas"] = datos["clientes"][user_id].get("busquedas", 0) + 1
        guardar_datos(datos)

    resultados = [
        m for m in datos["medicinas"]
        if medicina_buscar in m["nombre"] and m["provincia"] == provincia
    ]

    if not resultados:
        await update.message.reply_text(
            f"❌ No encontré '{esc(texto)}' en {esc(provincia)}.\n\n"
            f"💡 Sugerencias:\n"
            f"• Revisa la ortografía\n"
            f"• Prueba con otro nombre\n"
            f"• Los proveedores pueden publicar su catálogo",
            parse_mode="HTML"
        )
        context.user_data["estado"] = None
        return

    # Agrupar por proveedor
    por_proveedor = {}
    for r in resultados:
        pid = r["proveedor_id"]
        if pid not in por_proveedor:
            por_proveedor[pid] = []
        por_proveedor[pid].append(r)

    mensaje = (
        f"🔍 <b>{esc(texto.upper())}</b> en {esc(provincia)}\n\n"
        f"✅ Encontré {len(resultados)} coincidencia(s):\n\n"
    )

    enlace_wa = None

    for p_id, items in list(por_proveedor.items())[:5]:
        p = datos["proveedores"].get(p_id, {})
        dest = "⭐ " if es_destacado_activo(p) else ""

        mensaje += f"{dest}<b>Proveedor:</b> {esc(p.get('nombre', 'Anónimo'))}\n"
        mensaje += f"📞 {esc(p.get('contacto_mostrar', 'No disponible'))}\n"
        for item in items[:3]:
            mg = f" ({esc(item['mg'])})" if item.get('mg') else ""
            precio = f" - {esc(item['precio'])} CUP" if item.get('precio') else ""
            mensaje += f"   • {esc(item['nombre_original'])}{mg}{precio}\n"
        mensaje += "\n"

        # Enlace WhatsApp del primer proveedor que tenga
        if enlace_wa is None:
            contacto = p.get("contacto", {})
            if contacto.get("tipo") in ["whatsapp", "ambos"]:
                tel = contacto.get("whatsapp", "").replace("+", "").replace(" ", "")
                if tel:
                    msg_wa = f"Hola, te contacto desde MediCuba. ¿Tienes disponible {texto}?"
                    enlace_wa = f"https://wa.me/{tel}?text={msg_wa.replace(' ', '%20')}"

    botones = []
    if enlace_wa:
        botones.append([InlineKeyboardButton("📞 Contactar por WhatsApp", url=enlace_wa)])
    botones.append([InlineKeyboardButton("🏠 Menú", callback_data="volver")])

    await update.message.reply_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(botones),
        parse_mode="HTML"
    )
    context.user_data["estado"] = None


async def _listado(update, context, user_id, texto):
    medicinas = extraer_medicinas_desde_texto(texto)

    if not medicinas:
        await update.message.reply_text(
            "❌ No pude extraer medicinas de ese texto.\n\n"
            "Formatos que reconozco:\n"
            "• <code>Medicina(mg) - precio</code>\n"
            "• <code>Medicina: precio</code>\n"
            "• <code>Medicina💊</code>",
            parse_mode="HTML"
        )
        return

    provincia = datos["clientes"].get(user_id, {}).get("provincia", "Santiago de Cuba")

    async with datos_lock:
        # Crear o actualizar proveedor
        if user_id not in datos["proveedores"]:
            datos["proveedores"][user_id] = {
                "nombre": update.effective_user.first_name or "Proveedor",
                "contacto": {},
                "contacto_mostrar": "",
                "catalogo_activo": True,
                "link_token": user_id,
                "fecha_actualizacion": datetime.now().isoformat()
            }

        # Eliminar catálogo anterior
        datos["medicinas"] = [m for m in datos["medicinas"] if m["proveedor_id"] != user_id]

        # Guardar nuevo catálogo
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

        datos["proveedores"][user_id]["fecha_actualizacion"] = datetime.now().isoformat()
        guardar_datos(datos)

    context.user_data["estado"] = "esperando_contacto"
    context.user_data["medicinas_count"] = len(medicinas)

    teclado = [
        [InlineKeyboardButton("📱 WhatsApp", callback_data="contacto_whatsapp")],
        [InlineKeyboardButton("✈️ Telegram", callback_data="contacto_telegram")],
        [InlineKeyboardButton("📞 Ambos", callback_data="contacto_ambos")]
    ]
    await update.message.reply_text(
        f"✅ Se extrajeron <b>{len(medicinas)}</b> medicinas.\n\n"
        f"Ahora elige cómo prefieres que te contacten:",
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )


async def _cambio_provincia(update, context, user_id, texto):
    try:
        num = int(texto.strip())
        if 1 <= num <= len(datos["provincias"]):
            provincia = datos["provincias"][num - 1]
            async with datos_lock:
                if user_id not in datos["clientes"]:
                    datos["clientes"][user_id] = {}
                datos["clientes"][user_id]["provincia"] = provincia
                guardar_datos(datos)
            await update.message.reply_text(
                f"✅ Provincia cambiada a: <b>{esc(provincia)}</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"Número inválido. Elige entre 1 y {len(datos['provincias'])}"
            )
            return  # No limpiar estado
    except ValueError:
        await update.message.reply_text("Envía solo el NÚMERO de la provincia.")
        return  # No limpiar estado

    context.user_data["estado"] = None


async def _telefono(update, context, user_id, texto):
    telefono = texto.strip()
    tipo = context.user_data.get("tipo_contacto", "whatsapp")

    async with datos_lock:
        if user_id not in datos["proveedores"]:
            datos["proveedores"][user_id] = {}
        if "contacto" not in datos["proveedores"][user_id]:
            datos["proveedores"][user_id]["contacto"] = {}

        datos["proveedores"][user_id]["contacto"]["tipo"] = tipo
        datos["proveedores"][user_id]["contacto"]["whatsapp"] = telefono

        if tipo == "ambos":
            datos["proveedores"][user_id]["contacto_mostrar"] = telefono
            guardar_datos(datos)

            context.user_data["estado"] = "esperando_telegram"
            await update.message.reply_text("✈️ Ahora escribe tu @usuario de Telegram:")
            return

        # Solo WhatsApp — finalizar
        datos["proveedores"][user_id]["contacto_mostrar"] = telefono
        datos["proveedores"][user_id]["nombre"] = update.effective_user.first_name or "Proveedor"
        guardar_datos(datos)

    await _finalizar_registro(update, context, user_id)


async def _telegram_user(update, context, user_id, texto):
    telegram_user = texto.strip()
    if not telegram_user.startswith("@"):
        telegram_user = "@" + telegram_user

    tipo = context.user_data.get("tipo_contacto", "telegram")

    async with datos_lock:
        if user_id not in datos["proveedores"]:
            datos["proveedores"][user_id] = {}
        if "contacto" not in datos["proveedores"][user_id]:
            datos["proveedores"][user_id]["contacto"] = {}

        datos["proveedores"][user_id]["contacto"]["telegram"] = telegram_user

        if tipo == "ambos":
            whatsapp = datos["proveedores"][user_id]["contacto"].get("whatsapp", "")
            contacto_mostrar = f"{whatsapp} / {telegram_user}"
        else:
            contacto_mostrar = telegram_user

        datos["proveedores"][user_id]["contacto_mostrar"] = contacto_mostrar
        datos["proveedores"][user_id]["nombre"] = update.effective_user.first_name or "Proveedor"
        guardar_datos(datos)

    await _finalizar_registro(update, context, user_id)


async def _finalizar_registro(update, context, user_id):
    """Mensaje final tras registrar contacto de proveedor"""
    editando = context.user_data.get("editando_contacto", False)
    proveedor = datos["proveedores"].get(user_id, {})

    if editando:
        mensaje = (
            f"✅ <b>Contacto actualizado</b>\n\n"
            f"📞 Nuevo contacto: {esc(proveedor.get('contacto_mostrar', ''))}"
        )
        context.user_data["editando_contacto"] = False
    else:
        medicinas_count = context.user_data.get("medicinas_count", 0)
        link = f"t.me/MediCubaBot?start=proveedor_{user_id}"
        mensaje = (
            f"✅ <b>¡Catálogo publicado!</b>\n\n"
            f"📊 Se registraron {medicinas_count} medicinas.\n"
            f"📞 Contacto: {esc(proveedor.get('contacto_mostrar', ''))}\n\n"
            f"🔗 <b>Tu link personalizado:</b>\n<code>{link}</code>\n\n"
            f"⭐ ¿Quieres aparecer como Proveedor Destacado? Contacta al administrador.\n\n"
            f"🩺 <b>MediCuba</b> - Conectando pacientes con proveedores"
        )

    teclado = [[InlineKeyboardButton("🏠 Menú Principal", callback_data="volver")]]
    await update.message.reply_text(
        mensaje,
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode="HTML"
    )
    context.user_data["estado"] = None


# ===== COMANDOS ADMIN =====

async def admin_cargar_listado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not es_admin(user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    if not context.args:
        await update.message.reply_text(
            "📥 <b>Modo Administrador - Cargar Listado</b>\n\n"
            "Uso: <code>/admin_cargar_listado +5351234567</code>\n\n"
            "Luego pega el listado de medicinas.",
            parse_mode="HTML"
        )
        return

    telefono = context.args[0]
    context.user_data["admin_telefono"] = telefono
    context.user_data["estado"] = "admin_esperando_listado"

    await update.message.reply_text(
        f"📥 Teléfono asignado: <code>{esc(telefono)}</code>\n\n"
        f"Ahora pega el listado de medicinas:",
        parse_mode="HTML"
    )


async def _admin_listado(update, context, user_id, texto):
    if not es_admin(user_id):
        return

    telefono = context.user_data.get("admin_telefono")
    if not telefono:
        await update.message.reply_text("❌ Error: usa /admin_cargar_listado primero.")
        context.user_data["estado"] = None
        return

    medicinas = extraer_medicinas_desde_texto(texto)

    if not medicinas:
        await update.message.reply_text("❌ No pude extraer medicinas de ese texto.")
        return

    admin_prov_id = f"admin_{user_id}_{int(datetime.now().timestamp())}"
    provincia = datos["clientes"].get(user_id, {}).get("provincia", "Santiago de Cuba")

    async with datos_lock:
        datos["proveedores"][admin_prov_id] = {
            "nombre": "Administrador MediCuba",
            "contacto": {"tipo": "whatsapp", "whatsapp": telefono},
            "contacto_mostrar": telefono,
            "catalogo_activo": True,
            "fecha_actualizacion": datetime.now().isoformat()
        }

        for m in medicinas:
            datos["medicinas"].append({
                "nombre": m["nombre"],
                "nombre_original": m["nombre_original"],
                "mg": m.get("mg"),
                "precio": m.get("precio"),
                "proveedor_id": admin_prov_id,
                "provincia": provincia,
                "fecha": datetime.now().isoformat()
            })

        guardar_datos(datos)

    await update.message.reply_text(
        f"✅ <b>Listado cargado por Administrador</b>\n\n"
        f"📊 Se registraron {len(medicinas)} medicinas.\n"
        f"📞 Teléfono asignado: {esc(telefono)}\n"
        f"📍 Provincia: {esc(provincia)}\n\n"
        f"Ya están disponibles en las búsquedas.",
        parse_mode="HTML"
    )

    context.user_data["estado"] = None
    context.user_data["admin_telefono"] = None


async def destacar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /destacar PROVEEDOR_ID DIAS"""
    user_id = str(update.effective_user.id)

    if not es_admin(user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Uso: <code>/destacar PROVEEDOR_ID DIAS</code>\n\n"
            "Ejemplo: <code>/destacar 123456789 30</code>",
            parse_mode="HTML"
        )
        return

    proveedor_id = context.args[0]
    try:
        dias = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Los días deben ser un número.")
        return

    if proveedor_id not in datos["proveedores"]:
        await update.message.reply_text("❌ Proveedor no encontrado.")
        return

    fecha_fin = (datetime.now() + timedelta(days=dias)).isoformat()

    async with datos_lock:
        datos["proveedores"][proveedor_id]["destacado_hasta"] = fecha_fin
        guardar_datos(datos)

    nombre = datos["proveedores"][proveedor_id].get("nombre", "Proveedor")
    await update.message.reply_text(
        f"✅ <b>Proveedor destacado</b>\n\n"
        f"🏥 {esc(nombre)}\n"
        f"⭐ Destacado por {dias} días\n"
        f"📅 Hasta: {fecha_fin[:10]}",
        parse_mode="HTML"
    )


# ===== MAIN =====

def main():
    application = Application.builder().token(TOKEN).build()

    # Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin_cargar_listado", admin_cargar_listado))
    application.add_handler(CommandHandler("destacar", destacar_cmd))

    # UN solo handler de callbacks para TODOS los botones
    application.add_handler(CallbackQueryHandler(manejador_callbacks))

    # UN solo handler de mensajes que despacha por estado
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensajes)
    )

    print("🤖 MediCuba Bot iniciado...")
    application.run_polling()


if __name__ == "__main__":
    main()
