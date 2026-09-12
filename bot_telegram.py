import os
import random
import sqlite3
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from io import BytesIO

import qrcode
import requests
import yt_dlp
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================
# NE METS PAS TES CLÉS DIRECTEMENT DANS CE FICHIER.
# Configure-les uniquement dans les variables d'environnement de Termux/Render.
#
# Variables attendues :
# TELEGRAM_TOKEN
# OPENAI_API_KEY
# WEATHER_API_KEY
# OPENAI_MODEL (optionnel)
# PREMIUM_PRICE_STARS (optionnel)
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "").strip()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
PREMIUM_PRICE_STARS = int(os.getenv("PREMIUM_PRICE_STARS", "100"))
PREMIUM_DAYS = 30

FREE_AI_DAILY_LIMIT = 10
FREE_DOWNLOAD_DAILY_LIMIT = 3
PREMIUM_DOWNLOAD_DAILY_LIMIT = 20

DOWNLOAD_DIR = "downloads"
DB_FILE = "bot_data.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Diagnostic sécurisé : affiche uniquement True/False, jamais le token.
print("DEBUG TELEGRAM_TOKEN présent :", bool(os.getenv("TELEGRAM_TOKEN", "").strip()))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN manquant.")
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY manquante : /ai sera désactivé.")
if not WEATHER_API_KEY:
    logger.warning("WEATHER_API_KEY manquante : /weather sera désactivé.")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ============================================================
# HEALTH CHECK SERVER (Pour Web Service Render)
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_check_server, daemon=True).start()

# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            premium_until TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            ai_count INTEGER DEFAULT 0,
            download_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)

    conn.commit()
    conn.close()

init_db()

# ============================================================
# HELPERS
# ============================================================

def today():
    return datetime.now(timezone.utc).date().isoformat()

def get_usage(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT ai_count, download_count FROM usage WHERE user_id=? AND day=?",
        (user_id, today()),
    )
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO usage(user_id, day, ai_count, download_count) VALUES(?,?,0,0)",
            (user_id, today()),
        )
        conn.commit()
        result = (0, 0)
    else:
        result = row

    conn.close()
    return result

def increment_usage(user_id, field):
    if field not in ("ai_count", "download_count"):
        return

    conn = db()
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO usage(user_id, day, {field})
        VALUES(?,?,1)
        ON CONFLICT(user_id, day)
        DO UPDATE SET {field}={field}+1
        """,
        (user_id, today()),
    )
    conn.commit()
    conn.close()

def is_premium(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row or not row[0]:
        return False

    try:
        until = datetime.fromisoformat(row[0])
        return until > datetime.now(timezone.utc)
    except ValueError:
        return False

def activate_premium(user_id):
    current = datetime.now(timezone.utc)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()

    if row and row[0]:
        try:
            old_until = datetime.fromisoformat(row[0])
            if old_until > current:
                current = old_until
        except ValueError:
            pass

    until = current + timedelta(days=PREMIUM_DAYS)

    cur.execute(
        """
        INSERT INTO users(user_id, premium_until)
        VALUES(?,?)
        ON CONFLICT(user_id)
        DO UPDATE SET premium_until=excluded.premium_until
        """,
        (user_id, until.isoformat()),
    )

    conn.commit()
    conn.close()
    return until

def limits_text(user_id):
    ai_count, download_count = get_usage(user_id)

    if is_premium(user_id):
        return (
            f"⭐ Premium actif\n"
            f"IA aujourd'hui : {ai_count}/{FREE_AI_DAILY_LIMIT * 10}\n"
            f"Téléchargements : {download_count}/{PREMIUM_DOWNLOAD_DAILY_LIMIT}"
        )

    return (
        f"🆓 Gratuit\n"
        f"IA aujourd'hui : {ai_count}/{FREE_AI_DAILY_LIMIT}\n"
        f"Téléchargements : {download_count}/{FREE_DOWNLOAD_DAILY_LIMIT}"
    )

# ============================================================
# START / MENU
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧠 IA", callback_data="menu_ai"),
            InlineKeyboardButton("🔳 QR", callback_data="menu_qr"),
        ],
        [
            InlineKeyboardButton("🌤️ Météo", callback_data="menu_weather"),
            InlineKeyboardButton("📝 Notes", callback_data="menu_notes"),
        ],
        [
            InlineKeyboardButton("📥 Téléchargement", callback_data="menu_download"),
            InlineKeyboardButton("⭐ Premium", callback_data="menu_premium"),
        ],
        [
            InlineKeyboardButton("📊 Mon compte", callback_data="menu_account"),
        ],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Bonjour ! Je suis ton assistant multitâche.\n\n"
        "🧠 /ai <question> — IA\n"
        "🔳 /qr <texte> — QR code\n"
        "🌤️ /weather <ville> — météo\n"
        "📝 /note <texte> — enregistrer une note\n"
        "📋 /notes — afficher tes notes\n"
        "📥 /download <lien> — traiter un lien vidéo/audio\n"
        "⭐ /premium — Premium\n"
        "📊 /account — limites et statut\n\n"
        "Utilise aussi le menu ci-dessous."
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())

# ============================================================
# ACCOUNT / PREMIUM
# ============================================================

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    premium = is_premium(user_id)
    text = limits_text(user_id)

    if premium:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        text += f"\nExpire le : {row[0] if row else 'inconnu'}"
    else:
        text += "\n\n⭐ Premium : /premium"

    await update.message.reply_text(text)

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_premium(update.effective_user.id):
        await update.message.reply_text("⭐ Ton Premium est déjà actif.")
        return

    prices = [LabeledPrice("Premium 30 jours", PREMIUM_PRICE_STARS)]

    await update.message.reply_invoice(
        title="⭐ Premium",
        description=(
            "30 jours de Premium : plus de requêtes IA "
            "et plus de téléchargements."
        ),
        payload="premium_30_days",
        currency="XTR",
        prices=prices,
        provider_token="",
        start_parameter="premium-30-days",
    )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    if query.invoice_payload != "premium_30_days":
        await query.answer(ok=False, error_message="Commande inconnue.")
        return

    await query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment

    if payment.invoice_payload != "premium_30_days":
        return

    user_id = update.effective_user.id
    until = activate_premium(user_id)

    await update.message.reply_text(
        "🎉 Paiement confirmé !\n\n"
        f"⭐ Premium activé pour {PREMIUM_DAYS} jours.\n"
        f"Expiration : {until.strftime('%d/%m/%Y %H:%M UTC')}"
    )

async def paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Pour un problème de paiement, indique ton problème "
        "et conserve l'identifiant de transaction affiché par Telegram."
    )

# ============================================================
# 8 BALL
# ============================================================

async def ball8(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Exemple : /8ball Est-ce que ça va marcher ?"
        )
        return

    answers = [
        "Oui, absolument.",
        "C'est certain.",
        "Sans aucun doute.",
        "Peut-être bien.",
        "Demande plus tard.",
        "Concentre-toi et redemande.",
        "N'y compte pas.",
        "C'est non.",
    ]

    await update.message.reply_text(random.choice(answers))

# ============================================================
# QR
# ============================================================

async def generate_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Exemple : /qr https://example.com"
        )
        return

    text = " ".join(context.args)

    try:
        img = qrcode.make(text)
        bio = BytesIO()
        bio.name = "qrcode.png"
        img.save(bio, format="PNG")
        bio.seek(0)

        await update.message.reply_photo(
            photo=bio,
            caption="🔳 QR code généré."
        )
    except Exception as e:
        logger.exception("Erreur QR")
        await update.message.reply_text(
            f"❌ Impossible de générer le QR code : {type(e).__name__}"
        )

# ============================================================
# WEATHER
# ============================================================

async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEATHER_API_KEY:
        await update.message.reply_text(
            "❌ WEATHER_API_KEY n'est pas configurée."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Exemple : /weather Abidjan"
        )
        return

    city = " ".join(context.args)

    try:
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": city,
            "appid": WEATHER_API_KEY,
            "units": "metric",
            "lang": "fr",
        }

        response = await asyncio.to_thread(
            requests.get, url, params=params, timeout=15
        )
        data = response.json()

        if response.status_code != 200:
            await update.message.reply_text(
                "❌ Ville introuvable ou erreur météo."
            )
            return

        desc = data["weather"][0]["description"]
        temp = data["main"]["temp"]
        feels = data["main"]["feels_like"]

        await update.message.reply_text(
            f"🌤️ Météo à {city}\n\n"
            f"Conditions : {desc}\n"
            f"Température : {temp:.1f} °C\n"
            f"Ressenti : {feels:.1f} °C"
        )

    except Exception:
        logger.exception("Erreur météo")
        await update.message.reply_text(
            "❌ Erreur lors de la récupération de la météo."
        )

# ============================================================
# OPENAI
# ============================================================

async def ask_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not openai_client:
        await update.message.reply_text(
            "❌ OPENAI_API_KEY n'est pas configurée."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Exemple : /ai Explique-moi la relativité simplement."
        )
        return

    user_id = update.effective_user.id
    ai_count, _ = get_usage(user_id)

    limit = FREE_AI_DAILY_LIMIT * 10 if is_premium(user_id) else FREE_AI_DAILY_LIMIT

    if ai_count >= limit:
        await update.message.reply_text(
            "⛔ Limite IA quotidienne atteinte.\n"
            "Réessaie demain ou utilise /premium."
        )
        return

    prompt = " ".join(context.args)

    try:
        await update.message.chat.send_action("typing")

        response = await openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                "Tu es un assistant Telegram utile, clair et concis. "
                "Réponds en français sauf si l'utilisateur demande une autre langue."
            ),
            input=prompt,
        )

        answer = response.output_text.strip()

        if not answer:
            answer = "L'IA n'a renvoyé aucune réponse."

        increment_usage(user_id, "ai_count")

        # Telegram limite la longueur des messages.
        for i in range(0, len(answer), 4000):
            await update.message.reply_text(answer[i:i + 4000])

    except Exception as e:
        logger.exception("Erreur OpenAI")
        await update.message.reply_text(
            "❌ Erreur OpenAI.\n"
            f"Type : {type(e).__name__}\n\n"
            "Vérifie notamment la clé API, le projet OpenAI, "
            "la facturation et le modèle configuré."
        )

# ============================================================
# NOTES
# ============================================================

async def add_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Exemple : /note Acheter du pain"
        )
        return

    note_text = " ".join(context.args)
    user_id = update.effective_user.id

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notes(user_id, note, created_at) VALUES(?,?,?)",
        (user_id, note_text, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ Note enregistrée.")

async def get_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, note FROM notes WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📝 Tu n'as aucune note.")
        return

    text = "📝 Tes notes :\n\n"
    text += "\n".join(f"{note_id}. {note}" for note_id, note in rows)

    await update.message.reply_text(text[:4000])

# ============================================================
# DOWNLOAD
# ============================================================

async def download_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Exemple : /download https://exemple.com/video"
        )
        return

    url = context.args[0]

    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("❌ Le lien doit commencer par http:// ou https://")
        return

    user_id = update.effective_user.id
    _, download_count = get_usage(user_id)

    limit = PREMIUM_DOWNLOAD_DAILY_LIMIT if is_premium(user_id) else FREE_DOWNLOAD_DAILY_LIMIT

    if download_count >= limit:
        await update.message.reply_text(
            "⛔ Limite de téléchargements atteinte aujourd'hui.\n"
            "Utilise /premium pour augmenter la limite."
        )
        return

    context.user_data["download_url"] = url

    keyboard = [
        [
            InlineKeyboardButton("🎵 MP3", callback_data="format_mp3"),
            InlineKeyboardButton("🎬 MP4 720p", callback_data="format_mp4_720"),
        ],
        [
            InlineKeyboardButton("🎬 MP4 360p", callback_data="format_mp4_360"),
        ],
    ]

    await update.message.reply_text(
        "Choisis le format :",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # Menus
    if data == "menu_ai":
        await query.message.reply_text(
            "🧠 Utilise : /ai ta question"
        )
        return

    if data == "menu_qr":
        await query.message.reply_text(
            "🔳 Utilise : /qr ton texte ou ton lien"
        )
        return

    if data == "menu_weather":
        await query.message.reply_text(
            "🌤️ Utilise : /weather Abidjan"
        )
        return

    if data == "menu_notes":
        await query.message.reply_text(
            "📝 /note texte\n📋 /notes"
        )
        return

    if data == "menu_download":
        await query.message.reply_text(
            "📥 Utilise : /download URL\n"
            "Le lien doit pointer vers un contenu que tu as le droit de télécharger."
        )
        return

    if data == "menu_premium":
        await query.message.reply_text(
            "⭐ Premium 30 jours : /premium"
        )
        return

    if data == "menu_account":
        await query.message.reply_text(
            limits_text(update.effective_user.id)
        )
        return

    url = context.user_data.get("download_url")

    if not url:
        await query.edit_message_text(
            "❌ Le lien a expiré. Recommence avec /download."
        )
        return

    choice = data
    await query.edit_message_text(
        "⏳ Traitement en cours..."
    )

    output_file = None

    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        if choice == "format_mp3":
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
                "noplaylist": True,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "quiet": True,
                "no_warnings": True,
            }

        elif choice == "format_mp4_720":
            ydl_opts = {
                "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
                "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }

        elif choice == "format_mp4_360":
            ydl_opts = {
                "format": "bestvideo[height<=360]+bestaudio/best[height<=360]",
                "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }

        else:
            raise ValueError("Format inconnu.")

        def run_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                requested = ydl.prepare_filename(info)

                if choice == "format_mp3":
                    base = os.path.splitext(requested)[0]
                    candidates = [base + ".mp3"]
                else:
                    base = os.path.splitext(requested)[0]
                    candidates = [base + ".mp4", requested]

                for candidate in candidates:
                    if os.path.exists(candidate):
                        return candidate

                # Recherche de secours par identifiant.
                video_id = info.get("id")
                if video_id:
                    for name in os.listdir(DOWNLOAD_DIR):
                        if video_id in name:
                            candidate = os.path.join(DOWNLOAD_DIR, name)
                            if os.path.isfile(candidate):
                                return candidate

                raise FileNotFoundError(
                    "Fichier téléchargé introuvable après traitement."
                )

        output_file = await asyncio.to_thread(run_download)

        increment_usage(user_id, "download_count")

        size_mb = os.path.getsize(output_file) / (1024 * 1024)

        if size_mb > 49:
            await query.message.reply_text(
                "❌ Le fichier est trop volumineux pour être envoyé directement "
                "par ce bot."
            )
            return

        with open(output_file, "rb") as f:
            if choice == "format_mp3":
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=f,
                    caption="🎵 Terminé."
                )
            else:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=f,
                    caption="🎬 Terminé."
                )

        await query.message.reply_text("✅ Fichier envoyé.")

    except Exception as e:
        logger.exception("Erreur téléchargement")
        await query.message.reply_text(
            "❌ Téléchargement impossible.\n"
            f"Erreur : {type(e).__name__}\n\n"
            "Si le problème vient de FFmpeg ou d'un site non pris en "
            "charge, mets à jour les dépendances."
        )

    finally:
        if output_file and os.path.exists(output_file):
            try:
                os.remove(output_file)
            except OSError:
                pass

# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(
        "Exception non gérée",
        exc_info=context.error,
    )

# ============================================================
# MAIN
# ============================================================

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("8ball", ball8))
    app.add_handler(CommandHandler("qr", generate_qr))
    app.add_handler(CommandHandler("weather", weather))
    app.add_handler(CommandHandler("ai", ask_ai))
    app.add_handler(CommandHandler("note", add_note))
    app.add_handler(CommandHandler("notes", get_notes))
    app.add_handler(CommandHandler("download", download_link))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("account", account))
    app.add_handler(CommandHandler("paysupport", paysupport))

    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment)
    )

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    print("🤖 Bot démarré...")
    app.run_polling()

if __name__ == "__main__":
    main()
