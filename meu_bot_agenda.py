import logging
import json
import os 
from datetime import datetime
import asyncio

# --- MUDANÇA: TROCAR O RECEPCIONISTA ---
from quart import Quart, request # Trocámos Flask por Quart
# --- FIM DA MUDANÇA ---

# --- IMPORTAÇÕES DO MONGODB ---
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# --- O LOGGING VEM PRIMEIRO! ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuração Inicial ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
APP_URL = os.environ.get("RENDER_EXTERNAL_URL")

# --- Configuração da "Memória" (MongoDB) ---
client = None 
try:
    client = MongoClient(MONGO_URI)
    client.admin.command('ping')
    db = client.get_database("agenda_bot_db") 
    agenda_collection = db.get_collection("agendamentos") 
    logger.info("✅ Ligação ao MongoDB (Memória) estabelecida com sucesso!")
except (ConnectionFailure, OperationFailure) as e:
    logger.error(f"❌ FALHA AO LIGAR AO MONGODB: {e}")
    logger.error("Verifica se a 'MONGO_URI' está correta no Render e se o IP 0.0.0.0/0 está no Network Access do MongoDB.")
except Exception as e:
    logger.error(f"❌ Erro inesperado ao ligar ao MongoDB: {e}")
    

# --- Funções de Gestão da Agenda (A "Memória" MongoDB) ---
# (Estas funções não são 'async', por isso não mudam)

def salvar_agendamento(data_iso, hora_str, nome_cachorro):
    """Salva UM agendamento na base de dados."""
    if not client:
        logger.error("Não é possível salvar, sem ligação ao MongoDB.")
        return False
    try:
        agenda_collection.update_one(
            {"data_iso": data_iso}, 
            {
                "$push": { 
                    "agendamentos": {
                        "hora": hora_str,
                        "nome_cachorro": nome_cachorro
                    }
                },
                "$set": {"data_iso": data_iso} 
            },
            upsert=True 
        )
        
        # Re-ordenar a lista
        agenda_collection.update_one(
            {"data_iso": data_iso},
            {
                "$push": {
                    "agendamentos": {
                        "$each": [],
                        "$sort": {"hora": 1} 
                    }
                }
            }
        )
        logger.info(f"Agendamento salvo para {data_iso} @ {hora_str}.")
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar no MongoDB: {e}")
        return False

def carregar_agenda_dia(data_iso):
    """Carrega os agendamentos de UM dia específico da base de dados."""
    if not client:
        logger.error("Não é possível carregar, sem ligação ao MongoDB.")
        return None
    try:
        documento_dia = agenda_collection.find_one({"data_iso": data_iso})
        if documento_dia:
            return documento_dia.get("agendamentos", [])
        else:
            return []
    except Exception as e:
        logger.error(f"Erro ao carregar do MongoDB: {e}")
        return None

# --- Funções do Bot (O que ele faz) ---
# (Estas funções são 'async' e estão corretas)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nome_utilizador = update.effective_user.first_name
    mensagem_ajuda = (
        f"Olá, {nome_utilizador}! Eu sou o teu assistente de agendamentos 24/7 (Versão MongoDB! 🚀).\n\n"
        "Como usar:\n"
        "1. Para agendar, envia-me uma mensagem no formato:\n"
        "   `NomeDoCachorro-DD/MM/AAAA-HH:MM`\n"
        "   (Exemplo: `Bolinha-25/12/2025-14:30`)\n\n"
        "2. Para ver os agendamentos de hoje, escreve:\n"
        "   `agenda do dia`\n\n"
        "Podes também usar /ajuda para ver esta mensagem."
    )
    await update.message.reply_text(mensagem_ajuda, parse_mode='Markdown')

async def tratar_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_mensagem = update.message.text
    partes = texto_mensagem.split('-')
    
    if len(partes) != 3:
        await update.message.reply_text("Formato inválido. 😕 Tenta usar: `Nome-Data-Hora` (ex: `Bolinha-25/12/2025-14:30`)", parse_mode='Markdown')
        return

    nome = partes[0].strip()
    data_str = partes[1].strip()
    hora_str = partes[2].strip()

    try:
        data_hora_obj = datetime.strptime(f"{data_str} {hora_str}", "%d/%m/%Y %H:%M")
        data_iso = data_hora_obj.strftime("%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "Data ou hora em formato inválido. 😕\n"
            "Usa `DD/MM/AAAA` para a data (ex: `25/12/2025`).\n"
            "Usa `HH:MM` para a hora (ex: `14:30`)."
        , parse_mode='Markdown')
        return

    sucesso = salvar_agendamento(data_iso, hora_str, nome)
    if sucesso:
        await update.message.reply_text(f"✅ Agendamento confirmado!\nCachorro: {nome}\nDia: {data_str}\nHora: {hora_str}")
    else:
        await update.message.reply_text("❌ Ocorreu um erro ao salvar o agendamento. Tenta novamente mais tarde.")

async def ver_agenda_dia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoje = datetime.now()
    hoje_iso = hoje.strftime("%Y-%m-%d") 
    hoje_formatado = hoje.strftime("%d/%m/%Y") 

    agendamentos_hoje = carregar_agenda_dia(hoje_iso)

    if agendamentos_hoje is None:
         await update.message.reply_text("❌ Ocorreu um erro ao consultar a agenda. Tenta novamente mais tarde.")
         return
    if not agendamentos_hoje:
        await update.message.reply_text(f"Não tens agendamentos para hoje, dia {hoje_formatado}. 😊")
        return

    mensagem = f"🗓️ *Agenda do Dia: {hoje_formatado}*\n"
    mensagem += "------------------------------\n"
    for ag in agendamentos_hoje:
        mensagem += f"▪️ *{ag['hora']}* - {ag['nome_cachorro']}\n"
    await update.message.reply_text(mensagem, parse_mode='Markdown')

async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Não entendi... 😕\n\n"
        "Lembra-te dos comandos:\n"
        "Para agendar: `Nome-Data-Hora`\n"
        "Para ver hoje: `agenda do dia`\n"
        "Ou usa /ajuda."
    , parse_mode='Markdown')

# --- A parte que "liga" o bot (o Webhook) ---

# 1. Inicia a aplicação do bot
if TOKEN:
    application = Application.builder().token(TOKEN).build()
else:
    logger.error("TELEGRAM_TOKEN não foi encontrado! O bot não pode iniciar.")

# 2. Adiciona os Handlers (os "ouvintes" de comandos)
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("ajuda", start))
application.add_handler(MessageHandler(filters.Regex(r'(?i)^agenda do dia$'), ver_agenda_dia))
application.add_handler(MessageHandler(filters.Regex(r'.*-.+-.+'), tratar_agendamento))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

# 3. Inicia o servidor Web (Quart)
app = Quart(__name__) # <-- MUDANÇA: Agora é um "Recepcionista" Quart

# ----- A CORREÇÃO FINAL ESTÁ AQUI -----
# Agora o Recepcionista (Quart) e as Portas (rotas)
# falam a língua 'async' (moderna)

@app.before_serving
async def initialize_bot():
    """"Ligar a chave" do bot antes do servidor começar."""
    await application.initialize()
    logger.info("Aplicação do Telegram inicializada.")
    
    # E também já configuramos o webhook aqui
    if APP_URL:
        webhook_url = f"{APP_URL}/webhook/{TOKEN}"
        try:
            await application.bot.set_webhook(url=webhook_url)
            logger.info(f"Webhook configurado com sucesso para: {webhook_url}")
        except Exception as e:
            logger.error(f"Erro ao configurar o webhook na inicialização: {e}")
    else:
        logger.warning("RENDER_EXTERNAL_URL não definido. Webhook não configurado.")


@app.route("/")
async def index(): # <-- MUDANÇA: 'async def'
    """Página inicial simples para verificar se o bot está vivo."""
    return "Olá! Eu sou o servidor do bot de agendamento (Versão Quart/Corrigida). Estou a funcionar."

@app.route(f"/webhook/{TOKEN}", methods=['POST'])
async def webhook(): # <-- MUDANÇA: 'async def'
    """Esta é a rota (URL) que o Telegram vai 'visitar' quando receber mensagem."""
    if not client:
         logger.error("Ignorando webhook, sem ligação ao MongoDB.")
         return "error", 500
         
    try:
        update_json = await request.get_json(force=True)
        update = Update.de_json(update_json, application.bot)
        
        # Agora podemos chamar 'await' diretamente!
        await application.process_update(update) 
        
        return "ok", 200 # Responde ao Telegram que recebeu
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return "error", 500

# Não precisamos mais da rota /setup_webhook
# O bot agora faz isso sozinho quando "acorda" (em @app.before_serving)
