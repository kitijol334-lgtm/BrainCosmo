=import telebot
from telebot import types
import sqlite3
import json
import threading
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import os

# ==========================================
# НАСТРОЙКИ
# ==========================================
BOT_TOKEN = '9147236041:AAEcJCKj0XLpZhWKVh0dPV1Qs7_lf37KLhw'
ADMIN_ID = 8664148534 # Твой Telegram ID
WEBAPP_URL = 'https://braincosmo.onrender.com/' # Ссылка на Render

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='images')
CORS(app)

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
def init_db():
    # Добавлен timeout=20, чтобы избежать ошибки "database is locked"
    conn = sqlite3.connect('brainrot.db', check_same_thread=False, timeout=20)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, coins INTEGER DEFAULT 0,
        inventory TEXT DEFAULT '[]', last_free_case INTEGER DEFAULT 0
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nick TEXT, status TEXT DEFAULT 'pending'
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nick TEXT, items TEXT, status TEXT DEFAULT 'pending'
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS promos (
        code TEXT PRIMARY KEY, uses INTEGER, coins INTEGER
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS promo_uses (
        code TEXT, user_id INTEGER, UNIQUE(code, user_id)
    )''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect('brainrot.db', check_same_thread=False, timeout=20)

# ==========================================
# ОТДАЧА HTML И КАРТИНОК
# ==========================================
@app.route('/')
def serve_index():
    return send_file('index.html')

@app.route('/images/<path:filename>')
def serve_images(filename):
    return send_from_directory('images', filename)

# ==========================================
# API ДЛЯ ИГРЫ
# ==========================================
@app.route('/api/state', methods=['GET'])
def get_state():
    user_id = request.args.get('user_id')
    if not user_id: return jsonify({'error': 'No user_id'}), 400
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT coins, inventory, last_free_case FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        user = (0, '[]', 0)
        
    conn.close()
    return jsonify({'coins': user[0], 'inventory': json.loads(user[1]), 'last_free_case': user[2]})

@app.route('/api/save', methods=['POST'])
def save_state():
    data = request.json
    user_id = data.get('user_id')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET coins = ?, inventory = ?, last_free_case = ? WHERE user_id = ?", 
                   (data.get('coins'), json.dumps(data.get('inventory', [])), data.get('last_free_case', 0), user_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/deposit', methods=['POST'])
def api_deposit():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO deposits (user_id, nick) VALUES (?, ?)", (data['user_id'], data['nick']))
    dep_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Принять", callback_data=f"dep_acc_{dep_id}"),
               types.InlineKeyboardButton("❌ Отклонить", callback_data=f"dep_rej_{dep_id}"))
    bot.send_message(ADMIN_ID, f"💰 **Пополнение!**\nID: `{data['user_id']}`\nНик: `{data['nick']}`", reply_markup=markup, parse_mode='Markdown')
    return jsonify({'success': True})

@app.route('/api/withdraw', methods=['POST'])
def api_withdraw():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO withdrawals (user_id, nick, items) VALUES (?, ?, ?)", (data['user_id'], data['nick'], json.dumps(data['items'])))
    wd_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Принять", callback_data=f"wd_acc_{wd_id}"),
               types.InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_rej_{wd_id}"))
    items_names = ", ".join([i['name'] for i in data['items']])
    bot.send_message(ADMIN_ID, f"📦 **Вывод!**\nID: `{data['user_id']}`\nНик: `{data['nick']}`\nПредметы: {items_names}", reply_markup=markup, parse_mode='Markdown')
    return jsonify({'success': True})

# ==========================================
# БОТ
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🎮 Играть", web_app=types.WebAppInfo(url=WEBAPP_URL)))
    markup.add(types.InlineKeyboardButton("🎁 Ввести промокод", callback_data="enter_promo"))
    bot.send_message(message.chat.id, "👋 Добро пожаловать в Brainrot Ultra!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    if call.data == "enter_promo":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введите промокод (или 'отмена'):")
        bot.register_next_step_handler(msg, process_promo)
    elif call.data.startswith("dep_"):
        action, dep_id = call.data.split("_")[1], call.data.split("_")[2]
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, nick FROM deposits WHERE id = ?", (dep_id,))
        dep = cursor.fetchone()
        if action == "acc":
            bot.edit_message_text(f"💰 Заявка `{dep_id}` ПРИНЯТА. Напишите сумму:", call.message.chat.id, call.message.message_id)
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, give_coins, dep[0], dep_id)
        else:
            cursor.execute("UPDATE deposits SET status = 'rejected' WHERE id = ?", (dep_id,))
            conn.commit()
            bot.edit_message_text(f"❌ Заявка `{dep_id}` ОТКЛОНЕНА.", call.message.chat.id, call.message.message_id)
            bot.send_message(dep[0], "❌ Ваша заявка на пополнение отклонена.")
        conn.close()
    elif call.data.startswith("wd_"):
        action, wd_id = call.data.split("_")[1], call.data.split("_")[2]
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, nick FROM withdrawals WHERE id = ?", (wd_id,))
        wd = cursor.fetchone()
        if action == "acc":
            cursor.execute("UPDATE withdrawals SET status = 'completed' WHERE id = ?", (wd_id,))
            conn.commit()
            bot.edit_message_text(f"✅ Заявка `{wd_id}` ВЫПОЛНЕНА.", call.message.chat.id, call.message.message_id)
            bot.send_message(wd[0], "✅ Ваш вывод успешно выполнен!")
        else:
            cursor.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (wd_id,))
            conn.commit()
            bot.edit_message_text(f"❌ Заявка `{wd_id}` ОТКЛОНЕНА.", call.message.chat.id, call.message.message_id)
            bot.send_message(wd[0], "❌ Ваша заявка на вывод отклонена.")
        conn.close()

def give_coins(user_id, dep_id, message):
    try:
        amount = int(message.text)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
        cursor.execute("UPDATE deposits SET status = 'completed' WHERE id = ?", (dep_id,))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"✅ Начислено {amount} монет.")
        bot.send_message(user_id, f"✅ Пополнение одобрено! Начислено {amount} монет.")
    except:
        bot.send_message(message.chat.id, "Ошибка. Введите число.")

def process_promo(message):
    if message.text.lower() == 'отмена': return
    code, user_id = message.text.strip(), message.from_user.id
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT uses, coins FROM promos WHERE code = ?", (code,))
    promo = cursor.fetchone()
    if not promo: bot.send_message(message.chat.id, "❌ Неверный код!")
    elif promo[0] <= 0: bot.send_message(message.chat.id, "❌ Код закончился!")
    else:
        cursor.execute("SELECT * FROM promo_uses WHERE code = ? AND user_id = ?", (code, user_id))
        if cursor.fetchone(): bot.send_message(message.chat.id, "❌ Вы уже вводили его!")
        else:
            cursor.execute("UPDATE promos SET uses = uses - 1 WHERE code = ?", (code,))
            cursor.execute("INSERT INTO promo_uses (code, user_id) VALUES (?, ?)", (code, user_id))
            cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (promo[1], user_id))
            conn.commit()
            bot.send_message(message.chat.id, f"✅ Вам начислено {promo[1]} монет!")
    conn.close()

@bot.message_handler(commands=['createpromo'])
def create_promo(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        args = message.text.split()
        code, uses, coins = args[1], int(args[2]), int(args[3])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO promos (code, uses, coins) VALUES (?, ?, ?)", (code, uses, coins))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ Промокод `{code}` создан!")
    except:
        bot.reply_to(message, "Формат: /createpromo КОД АКТИВАЦИИ МОНЕТЫ")

# ==========================================
# ЗАПУСК
# ==========================================
if __name__ == '__main__':
    # Запускаем бота в отдельном потоке
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    # Запускаем Flask сервер (Render сам подставит нужный порт)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)