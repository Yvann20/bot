import os
import json
import time
import asyncio
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv                                        
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup   
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,                                                      
CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters
)                                                                       
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Configurações iniciais
load_dotenv()
CACHE_DIR = Path('cache')
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL = 3600  # 1 hora em segundos

# Variáveis de ambiente
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
YOUR_PHONE = os.getenv('YOUR_PHONE')

if not all([API_ID, API_HASH, BOT_TOKEN, YOUR_PHONE]):
    raise ValueError("Defina todas as variáveis de ambiente: API_ID, API_HASH, BOT_TOKEN, YOUR_PHONE.")

client = TelegramClient('session_name', API_ID, API_HASH)

# Estados da conversação
LINK, INTERVAL, REFERRAL = range(3)

# Configurações e controle
settings = {                                                                    'message_link': None,
    'referral_link': None,
    'user_id': None,
}

statistics = {
    'messages_sent': 0,
    'active_campaigns': 0,
}

group_list = []
active_campaigns = {}  # {user_id: {'job': job, 'start_time': timestamp}}

# Função para verificar campanhas ativas
def has_active_campaign(user_id):
    return user_id in active_campaigns and active_campaigns[user_id]['job'] is not None

# Autenticação
async def authenticate():
    await client.start()
    if not await client.is_user_authorized():
        try:
            await client.send_code_request(YOUR_PHONE)
            code = input('Código recebido: ')
            await client.sign_in(YOUR_PHONE, code)
        except SessionPasswordNeededError:
            password = input('Senha: ')
            await client.sign_in(password=password)

# Cache de participantes em arquivo
async def get_participant_ids(group):
    cache_file = CACHE_DIR / f"{group.id}.json"
    try:
        if cache_file.exists():
            file_mtime = cache_file.stat().st_mtime
            if time.time() - file_mtime < CACHE_TTL:
                with open(cache_file, 'r') as f:
                    return set(json.load(f))
    except Exception as e:
        print(f"Erro ao ler cache: {e}")

    try:
        participants = await client.get_participants(group)
        participant_ids = {p.id for p in participants}
        with open(cache_file, 'w') as f:
            json.dump(list(participant_ids), f)
        return participant_ids
    except Exception as e:
        print(f"Erro ao atualizar cache: {e}")
        return set()

# Pré-carregar grupos
async def preload_groups():
    group_list.clear()
    async for dialog in client.iter_dialogs():
        if dialog.is_group and not dialog.archived:
            group_list.append(dialog.entity)
    print(f"Grupos pré-carregados: {len(group_list)}")

# Encaminhamento de mensagens
async def forward_message_with_formatting(context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    try:
        if not settings['message_link']:
            return

        parts = settings['message_link'].split('/')
        message = await client.get_messages(parts[-2], ids=int(parts[-1]))
        me = await client.get_me()
        tasks = []

        for group in group_list:
            if me.id in await get_participant_ids(group):
                tasks.append(client.forward_messages(group, message))

        if tasks:
            await asyncio.gather(*tasks)
            statistics['messages_sent'] += len(tasks)
            print(f"Mensagens encaminhadas: {len (tasks)}")

    except Exception as e:
        print(f"Erro no encaminhamento: {e}")
    finally:
        print(f"Tempo de execução: {time.time() - start_time:.2f}s")

# Gestão de jobs
def manage_jobs(job_queue, user_id, interval):
    # Remove qualquer job existente para o usuário
    if has_active_campaign(user_id):
        active_campaigns[user_id]['job'].schedule_removal()
        del active_campaigns[user_id]

    # Cria novo job
    job = job_queue.run_repeating(
        forward_message_with_formatting,
        interval=interval * 60,
        first=0
    )

    # Registra a campanha
    active_campaigns[user_id] = {
        'job': job,
        'start_time': time.time(),
        'interval': interval
    }

    statistics['active_campaigns'] = len(active_campaigns)

# Função para limpar jobs finalizados
async def cleanup_jobs(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    expired_users = [
        user_id for user_id, data in active_campaigns.items()
        if now - data['start_time'] > (data['interval'] * 60 * 10)  # 10 ciclos
    ]

    for user_id in expired_users:
        active_campaigns[user_id]['job'].schedule_removal()
        del active_campaigns[user_id]

    if expired_users:
        statistics['active_campaigns'] = len(active_campaigns)
        print(f"Limpeza automática: {len(expired_users)} campanhas expiradas removidas")

# Handlers do Telegram
async def start_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if has_active_campaign(user_id):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "⚠️ Você já tem uma campanha ativa! Cancele a atual antes de iniciar uma nova.",
            show_alert=True
        )
        return ConversationHandler.END

    await update.callback_query.answer()
    await update.callback_query.edit_message_text('Envie o link da mensagem:')
    return LINK

async def set_message_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings['message_link'] = update.message.text
    await update.message.reply_text("Link salvo! Agora envie o intervalo em minutos:")
    return INTERVAL

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = int(update.message.text)
        user_id = update.effective_user.id
        await preload_groups()
        manage_jobs(context.application.job_queue, user_id, interval)
        await update.message.reply_text(f"✅ Campanha iniciada com intervalo de {interval} minutos")
    except ValueError:
        await update.message.reply_text("⚠️ Formato inválido! Use números inteiros.")
        return INTERVAL
    return ConversationHandler.END

async def cancel_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not has_active_campaign(user_id):
        await update.callback_query.answer()
        await update.callback_query.message.edit_text("❌ Nenhuma campanha ativa para cancelar.")
        return

    # Remove o job e limpa os dados
    active_campaigns[user_id]['job'].schedule_removal()
    del active_campaigns[user_id]

    statistics['active_campaigns'] = len(active_campaigns)
    await update.callback_query.answer()
    await update.callback_query.message.edit_text("✅ Campanha cancelada com sucesso!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operação cancelada.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.now()
    welcome_message = (
        f"BEM-VINDO AO BOT!\n\n"
        f"Data e Hora de Entrada: {now.strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"Seu ID: {user_id}\n"
        "👉 Toque no botão abaixo para começar sua jornada!"
    )

    keyboard = [
        [InlineKeyboardButton("🚀 INICIAR UMA NOVA CAMPANHA 🚀", callback_data='create_campaign')],
        [InlineKeyboardButton("🛑 CANCELAR CAMPANHA 🛑", callback_data='cancel_campaign')],
        [InlineKeyboardButton("📊 VER ESTATÍSTICAS DO BOT 📊", callback_data='statistics')],
        [InlineKeyboardButton("LINK DE REFERÊNCIA", callback_data='referral')],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)
    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")
        await update.message.reply_text("Desculpe, ocorreu um erro ao tentar enviar a mensagem.")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    stats_message = (
        "Estatísticas do Bot:\n"
        f"Mensagens enviadas: {statistics['messages_sent']}\n"
        f"Campanhas ativas: {statistics['active_campaigns']}\n"
    )
    await update.callback_query.message.reply_text(stats_message)

async def set_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.callback_query.from_user.id
    settings['referral_link'] = f"https://t.me/MEIA_GIL_BOT?start=ref_{user_id}"
    await update.callback_query.message.reply_text(f"Seu link de referência: {settings['referral_link']}")

def main():
    loop = asyncio.get_event_loop()

    try:
        loop.run_until_complete(authenticate())
        print("BOT CONECTADO")

        application = ApplicationBuilder().token(BOT_TOKEN).build()

        campaign_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(start_campaign, pattern='create_campaign')],
            states={
                LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_message_link)],
                INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_interval)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(show_statistics, pattern='statistics'))
        application.add_handler(CallbackQueryHandler(set_referral_link, pattern='referral'))
        application.add_handler(CallbackQueryHandler(cancel_campaign, pattern='cancel_campaign'))
        application.add_handler(campaign_handler)

        application.run_polling()
    finally:
        loop.run_until_complete(client.disconnect())
        loop.close()

if __name__ == '__main__':
    main()
