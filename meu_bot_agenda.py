import logging
import json
import os 
import re # Para encontrar as datas
import asyncio
from datetime import datetime, timedelta
from calendar import month_name, monthrange

# --- NOVAS FERRAMENTAS DE DATA ---
import dateparser # O "cérebro" que entende datas
from babel.dates import format_date # O formatador PT-BR
import pytz # Para fuso horário

# --- MUDANÇA: TROCAR O RECEPCIONISTA ---
from quart import Quart, request # Usamos o Quart (moderno)

# --- IMPORTAÇÕES DO MONGODB ---
from pymongo import MongoClient, ReturnDocument
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

# --- Configuração de Fuso Horário e Data (ESSENCIAL) ---
NOSSO_FUSO_HORARIO = pytz.timezone("America/Sao_Paulo")
# Configura o 'dateparser' para entender PT-BR e preferir datas no futuro
DATEPARSER_SETTINGS = {
    'LANGUAGES': ['pt'], # <-- ⭐️ CORREÇÃO 1: 'LANGUAGES' em maiúsculo
    'PREFER_DATES_FROM': 'future',
    'TIMEZONE': 'America/Sao_Paulo',
    'DATE_ORDER': 'DMY'
}

# --- Configuração da "Memória" (MongoDB) ---
client = None 
try:
    client = MongoClient(MONGO_URI)
    client.admin.command('ping')
    db = client.get_database("agenda_bot_db") 
    agenda_collection = db.get_collection("agendamentos") 
    logger.info("✅ Ligação ao MongoDB (Memória) estabelecida com sucesso!")
except Exception as e:
    logger.error(f"❌ Erro fatal ao ligar ao MongoDB: {e}")
    
# --- FUNÇÕES PRINCIPAIS DO BOT ---

def get_hoje():
    """Retorna a data/hora de 'hoje' no nosso fuso horário."""
    return datetime.now(NOSSO_FUSO_HORARIO)

# --- 1. FUNÇÕES DE AGENDAMENTO (O NOVO "CÉREBRO") ---

def analisar_agendamento(texto_completo):
    """
    Tenta descobrir o [Nome do Cachorro] e a [Data/Hora] a partir de um texto.
    A nossa regra: [Nome] [Data/Hora]
    Ex: "Bolinha da Silva amanhã 15h"
    """
    palavras = texto_completo.split()
    
    # Tentamos encontrar uma data começando pelo fim do texto
    for i in range(len(palavras), 0, -1):
        # Pega a parte do texto que pode ser uma data
        # Ex: "Bolinha da Silva amanhã 15h"
        # 1. Tenta: "Bolinha da Silva amanhã 15h"
        # 2. Tenta: "da Silva amanhã 15h"
        # 3. Tenta: "Silva amanhã 15h"
        # 4. Tenta: "amanhã 15h" <-- SUCESSO!
        
        texto_data_potencial = " ".join(palavras[i-1:])
        data_parseada = dateparser.parse(texto_data_potencial, settings=DATEPARSER_SETTINGS)
        
        if data_parseada:
            # SUCESSO! Encontrámos a data.
            # Tudo o que veio antes é o nome.
            nome_cachorro = " ".join(palavras[:i-1]).strip()
            
            # Se o nome estiver vazio, o comando está incompleto
            if not nome_cachorro:
                return None, None, "Não consegui identificar o nome do cachorro antes da data."
                
            # Verifica se o utilizador especificou uma hora
            # Se ele disse só "Bolinha amanhã", 'dateparser' marca como 00:00
            if data_parseada.hour == 0 and data_parseada.minute == 0:
                # Vamos ver se o utilizador não escreveu "00:00" de propósito
                if "00:00" not in texto_data_potencial and "meia-noite" not in texto_data_potencial:
                    return None, None, "Você precisa me dizer um horário (ex: `Bolinha amanhã 15h`)."

            # Formata os dados para o MongoDB
            data_iso = data_parseada.strftime("%Y-%m-%d") # AAAA-MM-DD
            hora_str = data_parseada.strftime("%H:%M") # HH:MM
            
            return nome_cachorro, data_parseada, None

    # Se saiu do loop sem encontrar, o formato está errado
    return None, None, "Não consegui entender a data ou hora que você digitou."

async def tratar_novo_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tenta agendar um novo horário a partir de texto livre."""
    texto_completo = update.message.text
    
    nome_cachorro, data_obj, erro = analisar_agendamento(texto_completo)
    
    if erro:
        await update.message.reply_text(f"😕 Opa! {erro}")
        return
        
    data_iso = data_obj.strftime("%Y-%m-%d")
    hora_str = data_obj.strftime("%H:%M")
    
    # --- 4. Verificação de Conflito ---
    conflito = verificar_conflito(data_iso, hora_str, nome_cachorro)
    if conflito:
        await update.message.reply_text(f"⚠️ **Aviso de Conflito!**\nO cachorro **{nome_cachorro}** já está agendado para este dia e horário.", parse_mode='Markdown')
        return
        
    # --- Salvar no MongoDB ---
    sucesso = salvar_agendamento_no_db(data_iso, hora_str, nome_cachorro)
    
    if sucesso:
        # Formatação bonita em PT-BR
        data_formatada = format_date(data_obj, "cccc, dd/MM/yyyy", locale="pt_BR")
        await update.message.reply_text(f"✅ **Agendamento confirmado!**\n\n🐶 Cachorro: **{nome_cachorro}**\n⏰ Hora: **{hora_str}**\n📅 Dia: **{data_formatada.capitalize()}**", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Ocorreu um erro ao salvar o agendamento na 'Memória' (MongoDB).")

# --- 2. FUNÇÕES DE CONSULTA (Ver Agenda) ---

def analisar_consulta_agenda(texto_consulta):
    """
    Descobre o período que o utilizador quer ver.
    Ex: "agenda do dia", "agenda da semana", "agenda de agosto", "agenda 13/11"
    Retorna (data_inicio, data_fim, titulo_agenda)
    """
    hoje = get_hoje().replace(hour=0, minute=0, second=0, microsecond=0)
    texto = texto_consulta.lower().replace("agenda de", "").replace("agenda do", "").replace("agenda", "").strip()
    
    # 1. Atalhos de Tempo
    if texto == "hoje" or texto == "dia":
        return hoje, hoje, "🗓️ Agenda de Hoje"
    if texto == "amanhã":
        amanha = hoje + timedelta(days=1)
        return amanha, amanha, "🗓️ Agenda de Amanhã"
    if texto == "ontem":
        ontem = hoje - timedelta(days=1)
        return ontem, ontem, "🗓️ Agenda de Ontem"
        
    # 2. Períodos (Semana/Mês)
    if texto == "semana":
        inicio_semana = hoje - timedelta(days=hoje.weekday()) # Segunda-feira
        fim_semana = inicio_semana + timedelta(days=6) # Domingo
        titulo = f"🗓️ Agenda da Semana ({inicio_semana.strftime('%d/%m')} - {fim_semana.strftime('%d/%m')})"
        return inicio_semana, fim_semana, titulo
        
    if texto == "mês":
        inicio_mes = hoje.replace(day=1)
        # Encontra o último dia do mês
        _, ultimo_dia = monthrange(hoje.year, hoje.month)
        fim_mes = hoje.replace(day=ultimo_dia)
        titulo = f"🗓️ Agenda do Mês ({format_date(hoje, 'MMMM', locale='pt_BR').capitalize()})"
        return inicio_mes, fim_mes, titulo

    # 3. Datas Específicas (Ex: "13/11" ou "segunda-feira" ou "agosto")
    data_parseada = dateparser.parse(texto, settings=DATEPARSER_SETTINGS)
    if not data_parseada:
        return None, None, f"😕 Desculpe, não entendi o período '{texto}'."
        
    data_parseada = data_parseada.replace(tzinfo=NOSSO_FUSO_HORARIO)
    
    # Se for um nome de mês (ex: "agosto")
    nomes_meses_pt = [month_name[i].lower() for i in range(1, 13)]
    if texto in nomes_meses_pt:
        mes_num = nomes_meses_pt.index(texto) + 1
        ano = hoje.year
        # Se o mês já passou (ex: estamos em Novembro e pede "Agosto"), assume este ano
        # Se estamos em Janeiro e pede "Agosto", assume este ano
        inicio_mes = hoje.replace(year=ano, month=mes_num, day=1)
        _, ultimo_dia = monthrange(ano, mes_num)
        fim_mes = hoje.replace(year=ano, month=mes_num, day=ultimo_dia)
        titulo = f"🗓️ Agenda de {texto.capitalize()}"
        return inicio_mes, fim_mes, titulo
        
    # Se for um dia da semana (ex: "segunda-feira")
    # O dateparser já nos dá o *próximo* dia (ex: próxima segunda)
    # Se for um dia específico (ex: "13/11")
    return data_parseada, data_parseada, f"🗓️ Agenda de {format_date(data_parseada, 'cccc, dd/MM/yyyy', locale='pt_BR').capitalize()}"

async def tratar_ver_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_completo = update.message.text
    
    data_inicio, data_fim, titulo = analisar_consulta_agenda(texto_completo)
    
    if not data_inicio: # Se deu erro
        await update.message.reply_text(titulo) # 'titulo' aqui contém a mensagem de erro
        return
        
    # Carregar os agendamentos do MongoDB
    agendamentos = carregar_agendamentos_do_db(data_inicio, data_fim)
    
    if not agendamentos:
        await update.message.reply_text(f"Nenhum agendamento encontrado para:\n**{titulo}**", parse_mode='Markdown')
        return

    # Formatar a resposta
    mensagem_resposta = f"**{titulo}**\n"
    mensagem_resposta += "------------------------------\n"
    
    dia_atual = ""
    for ag in agendamentos:
        data_obj = datetime.strptime(ag['data_iso'], "%Y-%m-%d").replace(tzinfo=NOSSO_FUSO_HORARIO)
        data_formatada_dia = format_date(data_obj, "cccc, dd/MM/yyyy", locale="pt_BR").capitalize()
        
        # Agrupar por dia (se for consulta de semana/mês)
        if data_formatada_dia != dia_atual:
            mensagem_resposta += f"\n**📅 {data_formatada_dia}**\n"
            dia_atual = data_formatada_dia
            
        mensagem_resposta += f"  🐶 **{ag['nome_cachorro']}**\n  ⏰ {ag['hora']}\n"
        
    await update.message.reply_text(mensagem_resposta, parse_mode='Markdown')

# --- 3. FUNÇÕES DE APAGAR / LIMPAR ---

async def tratar_apagar_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_completo = update.message.text.lower().replace("apagar agendamento", "").replace("apagar", "").strip()
    
    nome_cachorro, data_obj, erro = analisar_agendamento(texto_completo)
    
    if erro:
        await update.message.reply_text(f"😕 Opa! {erro}")
        return

    data_iso = data_obj.strftime("%Y-%m-%d")
    hora_str = data_obj.strftime("%H:%M")
    
    # Tentar apagar do MongoDB
    apagado = apagar_agendamento_do_db(data_iso, hora_str, nome_cachorro)
    
    if apagado:
        data_formatada = format_date(data_obj, "cccc, dd/MM/yyyy", locale="pt_BR").capitalize()
        await update.message.reply_text(f"🗑️ **Agendamento Apagado!**\n\n🐶 Cachorro: **{nome_cachorro}**\n⏰ Hora: **{hora_str}**\n📅 Dia: **{data_formatada}**", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Não encontrei nenhum agendamento para **{nome_cachorro}** no dia {data_obj.strftime('%d/%m')} às {hora_str} para apagar.", parse_mode='Markdown')

async def tratar_limpar_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_completo = update.message.text.lower()
    
    # Usamos a mesma lógica da consulta
    data_inicio, data_fim, titulo = analisar_consulta_agenda(texto_completo.replace("limpar ", ""))
    
    if not data_inicio: # Se deu erro
        await update.message.reply_text(titulo) # 'titulo' aqui contém a mensagem de erro
        return
    
    # Limpar do MongoDB
    contagem_apagados = limpar_agendamentos_do_db(data_inicio, data_fim)
    
    await update.message.reply_text(f"🗑️ **Limpeza Concluída!**\nForam apagados **{contagem_apagados}** agendamentos de:\n{titulo.replace('🗓️', '')}", parse_mode='Markdown')

# --- 4. FUNÇÃO DE AJUDA ---

async def comando_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ajuda_texto = (
        "Olá! Eu sou o seu assistente de agendamentos 24/7. 🚀\n\n"
        "Aqui está o que eu consigo fazer:\n\n"
        "**1. Para Agendar**\n"
        "Use o formato `[Nome] [Data] [Hora]`.\n"
        "*Exemplos:*\n"
        "  `Bolinha hoje 15h`\n"
        "  `Rex amanhã 10:30`\n"
        "  `Totó segunda-feira 09:00`\n"
        "  `Princesa 25/12 14h`\n\n"
        "**2. Para Ver a Agenda**\n"
        "Use o comando `agenda` seguido do período.\n"
        "*Exemplos:*\n"
        "  `agenda de hoje` (ou `agenda do dia`)\n"
        "  `agenda de amanhã`\n"
        "  `agenda da semana`\n"
        "  `agenda do mês`\n"
        "  `agenda de agosto`\n"
        "  `agenda 13/11`\n\n"
        "**3. Para Apagar**\n"
        "Use o comando `apagar` com os dados do agendamento.\n"
        "*Exemplo:*\n"
        "  `apagar Bolinha amanhã 15h`\n\n"
        "**4. Para Limpar**\n"
        "Use o comando `limpar` seguido do período.\n"
        "*Exemplos:*\n"
        "  `limpar agenda de hoje`\n"
        "  `limpar agenda da semana`\n"
        "  `limpar agenda do mês`"
    )
    await update.message.reply_text(ajuda_texto, parse_mode='Markdown')

# --- O "ROTEADOR" PRINCIPAL (HANDLE_TEXT) ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """O "cérebro" que decide o que fazer com cada mensagem."""
    if not update.message or not update.message.text:
        return
        
    texto = update.message.text.lower().strip()
    
    try:
        # --- ⭐️ CORREÇÃO 2: Capturar 'ajuda' sem o '/' ---
        if texto == "ajuda":
            await comando_ajuda(update, context)
        # --- FIM DA CORREÇÃO ---
            
        elif texto.startswith("agenda"):
            await tratar_ver_agenda(update, context)
            
        elif texto.startswith("apagar"):
            await tratar_apagar_agendamento(update, context)
            
        elif texto.startswith("limpar"):
            await tratar_limpar_agenda(update, context)
            
        else:
            # Se não for nenhum comando, tenta agendar
            await tratar_novo_agendamento(update, context)
            
    except Exception as e:
        logger.error(f"Erro GERAL ao processar texto '{texto}': {e}")
        await update.message.reply_text("❌ Ops! Ocorreu um erro inesperado ao processar sua mensagem.")


# --- FUNÇÕES DA "MEMÓRIA" (MongoDB) ---

def salvar_agendamento_no_db(data_iso, hora_str, nome_cachorro):
    """Salva UM agendamento no MongoDB."""
    if not client: return False
    try:
        agenda_collection.update_one(
            {"data_iso": data_iso},
            {
                "$push": { "agendamentos": { "hora": hora_str, "nome_cachorro": nome_cachorro } },
                "$set": {"data_iso": data_iso}
            },
            upsert=True
        )
        # Re-ordenar (o MongoDB infelizmente torna isso um 2º passo)
        agenda_collection.update_one(
            {"data_iso": data_iso},
            {"$push": { "agendamentos": { "$each": [], "$sort": {"hora": 1} } } }
        )
        return True
    except Exception as e:
        logger.error(f"Erro ao SALVAR no MongoDB: {e}")
        return False

def verificar_conflito(data_iso, hora_str, nome_cachorro):
    """Verifica se este agendamento exato já existe."""
    if not client: return False
    try:
        # Tenta encontrar um documento que tenha:
        # A data E
        # Um agendamento na lista que tenha a hora E o nome
        conflito = agenda_collection.find_one({
            "data_iso": data_iso,
            "agendamentos": {
                "$elemMatch": {
                    "hora": hora_str,
                    "nome_cachorro": re.compile(f"^{re.escape(nome_cachorro)}$", re.IGNORECASE) # Ignora maiúsculas/minúsculas
                }
            }
        })
        return conflito is not None # Retorna True se encontrou conflito
    except Exception as e:
        logger.error(f"Erro ao VERIFICAR CONFLITO no MongoDB: {e}")
        return False

def carregar_agendamentos_do_db(data_inicio, data_fim):
    """Busca TODOS agendamentos num PERÍODO (range) de datas."""
    if not client: return []
    
    # Converte as datas de volta para string ISO (AAAA-MM-DD)
    data_inicio_iso = data_inicio.strftime("%Y-%m-%d")
    data_fim_iso = data_fim.strftime("%Y-%m-%d")
    
    try:
        # Encontra todos documentos onde data_iso está ENTRE o início E o fim
        query = {
            "data_iso": {
                "$gte": data_inicio_iso,
                "$lte": data_fim_iso
            }
        }
        # Ordena por data (ex: 13/11, 14/11...)
        documentos = agenda_collection.find(query).sort("data_iso", 1)
        
        # O MongoDB retorna os dias. Nós queremos os agendamentos dentro deles.
        lista_final = []
        for dia_doc in documentos:
            if "agendamentos" in dia_doc:
                for ag in dia_doc["agendamentos"]:
                    # Adicionamos a data_iso em cada agendamento para formatação
                    ag['data_iso'] = dia_doc['data_iso']
                    lista_final.append(ag)
        return lista_final
        
    except Exception as e:
        logger.error(f"Erro ao CARREGAR (range) do MongoDB: {e}")
        return []

def apagar_agendamento_do_db(data_iso, hora_str, nome_cachorro):
    """Apaga UM agendamento específico."""
    if not client: return False
    try:
        # $pull = "puxar para fora" (remover) da lista
        resultado = agenda_collection.update_one(
            {"data_iso": data_iso},
            {
                "$pull": {
                    "agendamentos": {
                        "hora": hora_str,
                        # Usamos regex para ignorar maiúscula/minúscula no nome
                        "nome_cachorro": re.compile(f"^{re.escape(nome_cachorro)}$", re.IGNORECASE)
                    }
                }
            }
        )
        # Retorna True se algo foi modificado
        return resultado.modified_count > 0
    except Exception as e:
        logger.error(f"Erro ao APAGAR (específico) do MongoDB: {e}")
        return False

def limpar_agendamentos_do_db(data_inicio, data_fim):
    """Apaga TODOS agendamentos num PERÍODO (range) de datas."""
    if not client: return 0
    
    data_inicio_iso = data_inicio.strftime("%Y-%m-%d")
    data_fim_iso = data_fim.strftime("%Y-%m-%d")
    
    try:
        # Se for um dia só, limpamos só os agendamentos *dentro* do documento
        if data_inicio_iso == data_fim_iso:
            resultado = agenda_collection.update_one(
                {"data_iso": data_inicio_iso},
                {"$set": {"agendamentos": []}} # Define a lista como vazia
            )
            # Precisamos ver quantos eram
            # Isto é complexo... vamos simplificar e só apagar
            return resultado.modified_count # Retorna 1 (se o dia foi modificado) ou 0
        
        # Se for um range (semana/mês), apagamos os documentos do dia inteiro
        else:
            query = {
                "data_iso": {
                    "$gte": data_inicio_iso,
                    "$lte": data_fim_iso
                }
            }
            # Vamos apenas contar quantos agendamentos estamos a apagar
            total_apagado = 0
            documentos = agenda_collection.find(query)
            for doc in documentos:
                if "agendamentos" in doc:
                    total_apagado += len(doc["agendamentos"])
            
            # Agora apaga
            agenda_collection.delete_many(query)
            return total_apagado
            
    except Exception as e:
        logger.error(f"Erro ao LIMPAR (range) do MongoDB: {e}")
        return 0


# --- A parte que "liga" o bot (o Webhook) ---

if TOKEN:
    application = Application.builder().token(TOKEN).build()
else:
    logger.error("TELEGRAM_TOKEN não foi encontrado! O bot não pode iniciar.")

# 1. Adiciona o "Roteador" principal
# (Filtro: Texto, que NÃO seja um comando /)
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# 2. Adiciona os comandos de ajuda
application.add_handler(CommandHandler("start", comando_ajuda)) # /start e /ajuda fazem o mesmo
application.add_handler(CommandHandler("ajuda", comando_ajuda))

# 3. Inicia o servidor Web (Quart)
app = Quart(__name__) # Nosso "Recepcionista" moderno

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
async def index(): # 'async def'
    """Página inicial simples para verificar se o bot está vivo."""
    return "Olá! Eu sou o servidor do bot de agendamento (Versão 2.0 Inteligente). Estou a funcionar."

@app.route(f"/webhook/{TOKEN}", methods=['POST'])
async def webhook(): # 'async def'
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
