import telebot
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
WEBAPP_URL = 'https://braincosmo111.onrender.com' # Твоя ссылка на Render

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='images')
CORS(app)

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
def init_db():
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
# ОТДАЧА HTML И КАРТИНОК + ПИНГ
# ==========================================
@app.route('/ping')
def ping():
    return "OK", 200

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
    
    # Бот НЕ пишет админу, заявка просто падает в HTML-админку
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
    return jsonify({'success': True})

# ==========================================
# API ДЛЯ АДМИНКИ (Глобальные заявки)
# ==========================================
@app.route('/api/get_all_requests', methods=['GET'])
def get_all_requests():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, user_id, nick, status FROM deposits ORDER BY id DESC")
    deposits = [{'id': r[0], 'user_id': r[1], 'nick': r[2], 'status': r[3]} for r in cursor.fetchall()]
    
    cursor.execute("SELECT id, user_id, nick, items, status FROM withdrawals ORDER BY id DESC")
    withdrawals = []
    for r in cursor.fetchall():
        withdrawals.append({
            'id': r[0], 'user_id': r[1], 'nick': r[2], 
            'items': json.loads(r[3]), 'status': r[4]
        })
        
    conn.close()
    return jsonify({'deposits': deposits, 'withdrawals': withdrawals})

@app.route('/api/admin_accept_deposit', methods=['POST'])
def admin_accept_deposit():
    dep_id = request.json.get('dep_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM deposits WHERE id = ?", (dep_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE deposits SET status = 'accepted' WHERE id = ?", (dep_id,))
    conn.commit(); conn.close()
    
    if res:
        try:
            bot.send_message(res[0], "✅ Ваша заявка на пополнение **принята**!\nУ вас есть 3 минуты, чтобы зайти в игру и получить монеты.", parse_mode='Markdown')
        except Exception as e:
            print(f"Ошибка отправки сообщения игроку: {e}")
            
    return jsonify({'success': True})

@app.route('/api/admin_give_coins', methods=['POST'])
def admin_give_coins():
    data = request.json
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (data['amount'], data['user_id']))
    cursor.execute("UPDATE deposits SET status = 'completed' WHERE id = ?", (data['dep_id'],))
    conn.commit(); conn.close()
    
    try:
        bot.send_message(data['user_id'], f"✅ Вам начислено {data['amount']} монет!")
    except Exception as e:
        print(f"Ошибка отправки сообщения игроку: {e}")
        
    return jsonify({'success': True})

@app.route('/api/admin_reject_deposit', methods=['POST'])
def admin_reject_deposit():
    dep_id = request.json.get('dep_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM deposits WHERE id = ?", (dep_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE deposits SET status = 'rejected' WHERE id = ?", (dep_id,))
    conn.commit(); conn.close()
    
    if res:
        try:
            bot.send_message(res[0], "❌ Ваша заявка на пополнение отклонена.")
        except Exception as e:
            print(f"Ошибка отправки сообщения игроку: {e}")
            
    return jsonify({'success': True})

@app.route('/api/admin_accept_withdraw', methods=['POST'])
def admin_accept_withdraw():
    wd_id = request.json.get('wd_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("UPDATE withdrawals SET status = 'accepted' WHERE id = ?", (wd_id,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/admin_complete_withdraw', methods=['POST'])
def admin_complete_withdraw():
    wd_id = request.json.get('wd_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM withdrawals WHERE id = ?", (wd_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE withdrawals SET status = 'completed' WHERE id = ?", (wd_id,))
    conn.commit(); conn.close()
    
    if res:
        try:
            bot.send_message(res[0], "✅ Ваш вывод успешно выполнен!")
        except Exception as e:
            print(f"Ошибка отправки сообщения игроку: {e}")
            
    return jsonify({'success': True})

@app.route('/api/admin_reject_withdraw', methods=['POST'])
def admin_reject_withdraw():
    wd_id = request.json.get('wd_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM withdrawals WHERE id = ?", (wd_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (wd_id,))
    conn.commit(); conn.close()
    
    if res:
        try:
            bot.send_message(res[0], "❌ Ваша заявка на вывод отклонена.")
        except Exception as e:
            print(f"Ошибка отправки сообщения игроку: {e}")
            
    return jsonify({'success': True})

# ==========================================
# БОТ
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🎁 Ввести промокод", callback_data="enter_promo"))
    
    text = (
        "👋 Добро пожаловать в Brainrot Ultra!\n\n"
        "👇 **Чтобы играть, нажми на голубую кнопку «Играть» внизу экрана** (слева от поля ввода сообщения)."
    )
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    if call.data == "enter_promo":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введите промокод (или 'отмена'):")
        bot.register_next_step_handler(msg, process_promo)

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
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)