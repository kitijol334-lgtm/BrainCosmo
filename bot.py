import telebot
from telebot import types
import json
import threading
import os
import requests
import psycopg2
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS

# ==========================================
# НАСТРОЙКИ (токен берём из переменных окружения Render!)
# ==========================================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', '8664148534'))
DATABASE_URL = os.environ.get('DATABASE_URL')

if not BOT_TOKEN:
    raise SystemExit("❌ Не задана переменная окружения BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder=None)
CORS(app)

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
def get_db():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL is missing")
    url = DATABASE_URL.replace('postgres://', 'postgresql://')
    return psycopg2.connect(url)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, coins BIGINT DEFAULT 0,
        inventory TEXT DEFAULT '[]', last_free_case BIGINT DEFAULT 0,
        last_tag_case BIGINT DEFAULT 0
    )''')
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS xp BIGINT DEFAULT 0")

    cur.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id SERIAL PRIMARY KEY, user_id BIGINT, nick TEXT, status TEXT DEFAULT 'pending'
    )''')
    cur.execute("ALTER TABLE deposits ADD COLUMN IF NOT EXISTS tg_username TEXT")

    cur.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id SERIAL PRIMARY KEY, user_id BIGINT, nick TEXT, items TEXT, status TEXT DEFAULT 'pending'
    )''')
    cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS tg_username TEXT")

    cur.execute('''CREATE TABLE IF NOT EXISTS promos (
        code TEXT PRIMARY KEY, uses INTEGER, coins INTEGER
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS promo_uses (
        code TEXT, user_id BIGINT, UNIQUE(code, user_id)
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )''')
    cur.execute("INSERT INTO settings (key, value) VALUES ('allowedWithdrawals', '[]') ON CONFLICT DO NOTHING")

    # Оплата звёздами (защита от двойного начисления)
    cur.execute('''CREATE TABLE IF NOT EXISTS stars_payments (
        charge_id TEXT PRIMARY KEY, user_id BIGINT, amount INTEGER
    )''')

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована.")

try:
    init_db()
except Exception as e:
    print(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ К БД: {e}")

def ensure_user(cur, user_id):
    cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))

def notify(user_id, text, **kw):
    try:
        bot.send_message(user_id, text, **kw)
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")

# ==========================================
# ОТДАЧА HTML, КАРТИНОК, ЗВУКОВ + ПИНГ
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

AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.m4a')

@app.route('/<path:filename>')
def serve_audio(filename):
    # Отдаём только аудиофайлы из корня проекта (рядом с index.html)
    if '/' in filename or not filename.lower().endswith(AUDIO_EXTS):
        return "Not found", 404
    return send_from_directory('.', filename)

# ==========================================
# API ДЛЯ ИГРЫ
# ==========================================
@app.route('/api/state', methods=['GET'])
def get_state():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': 'No user_id'}), 400
    conn = get_db()
    cur = conn.cursor()
    ensure_user(cur, int(user_id))
    conn.commit()
    cur.execute("SELECT coins, xp, inventory, last_free_case, last_tag_case FROM users WHERE user_id = %s", (user_id,))
    u = cur.fetchone()
    conn.close()
    return jsonify({
        'coins': u[0] or 0,
        'xp': u[1] or 0,
        'inventory': json.loads(u[2] or '[]'),
        'last_free_case': u[3] or 0,
        'last_tag_case': u[4] or 0
    })

@app.route('/api/save', methods=['POST'])
def save_state():
    data = request.json or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': 'No user_id'}), 400
    conn = get_db()
    cur = conn.cursor()
    ensure_user(cur, int(user_id))
    cur.execute(
        "UPDATE users SET coins = %s, xp = %s, inventory = %s, last_free_case = %s, last_tag_case = %s WHERE user_id = %s",
        (data.get('coins', 0), data.get('xp', 0), json.dumps(data.get('inventory', [])),
         data.get('last_free_case', 0), data.get('last_tag_case', 0), user_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/deposit', methods=['POST'])
def api_deposit():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO deposits (user_id, tg_username, nick) VALUES (%s, %s, %s)",
                (data['user_id'], data.get('tg_username', ''), data['nick']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/withdraw', methods=['POST'])
def api_withdraw():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO withdrawals (user_id, tg_username, nick, items) VALUES (%s, %s, %s, %s)",
                (data['user_id'], data.get('tg_username', ''), data['nick'], json.dumps(data['items'])))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ==========================================
# ОПЛАТА ЗВЁЗДАМИ (Telegram Stars)
# ==========================================
@app.route('/api/create_stars_invoice', methods=['POST'])
def create_stars_invoice():
    data = request.json or {}
    try:
        user_id = int(data.get('user_id'))
        amount = int(data.get('amount'))
    except (TypeError, ValueError):
        return jsonify({'error': 'bad params'}), 400
    if amount < 1 or amount > 10000:
        return jsonify({'error': 'bad amount'}), 400

    payload = {
        'title': 'Пополнение монет',
        'description': f'{amount} монет для Brainrot Cosmo',
        'payload': f'stars_{user_id}_{amount}',
        'provider_token': '',
        'currency': 'XTR',
        'prices': [{'label': f'{amount} монет', 'amount': amount}]
    }
    try:
        r = requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink',
                          json=payload, timeout=15)
        res = r.json()
    except Exception as e:
        print(f"Ошибка createInvoiceLink: {e}")
        return jsonify({'error': 'telegram error'}), 500
    if not res.get('ok'):
        print(f"createInvoiceLink fail: {res}")
        return jsonify({'error': 'telegram error'}), 500
    return jsonify({'invoice_link': res['result']})

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    pay = message.successful_payment
    user_id = message.from_user.id
    amount = pay.total_amount  # 1 звезда = 1 монета
    charge_id = pay.telegram_payment_charge_id

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO stars_payments (charge_id, user_id, amount) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (charge_id, user_id, amount))
    if cur.rowcount == 0:  # уже начисляли
        conn.close()
        return
    ensure_user(cur, user_id)
    cur.execute("UPDATE users SET coins = coins + %s WHERE user_id = %s", (amount, user_id))
    conn.commit()
    conn.close()
    notify(user_id, f"✅ Оплата прошла! Начислено {amount} монет.")

# ==========================================
# API ДЛЯ НАСТРОЕК АДМИНКИ
# ==========================================
@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'allowedWithdrawals'")
    res = cur.fetchone()
    conn.close()
    return jsonify({'allowedWithdrawals': json.loads(res[0]) if res else []})

@app.route('/api/admin_save_settings', methods=['POST'])
def admin_save_settings():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE settings SET value = %s WHERE key = 'allowedWithdrawals'",
                (json.dumps(data.get('allowedWithdrawals', [])),))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ==========================================
# API ДЛЯ АДМИНКИ (заявки и промокоды)
# ==========================================
@app.route('/api/get_all_requests', methods=['GET'])
def get_all_requests():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, tg_username, nick, status FROM deposits ORDER BY id DESC")
    deposits = [{'id': r[0], 'user_id': r[1], 'tg_username': r[2], 'nick': r[3], 'status': r[4]} for r in cur.fetchall()]
    cur.execute("SELECT id, user_id, tg_username, nick, items, status FROM withdrawals ORDER BY id DESC")
    withdrawals = [{'id': r[0], 'user_id': r[1], 'tg_username': r[2], 'nick': r[3],
                    'items': json.loads(r[4] or '[]'), 'status': r[5]} for r in cur.fetchall()]
    conn.close()
    return jsonify({'deposits': deposits, 'withdrawals': withdrawals})

@app.route('/api/admin_get_promos', methods=['GET'])
def admin_get_promos():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.code, p.uses, p.coins, COUNT(pu.user_id) AS used_count
        FROM promos p LEFT JOIN promo_uses pu ON p.code = pu.code
        GROUP BY p.code, p.uses, p.coins ORDER BY p.code
    """)
    promos = [{'code': r[0], 'uses': r[1], 'coins': r[2], 'used_count': r[3]} for r in cur.fetchall()]
    conn.close()
    return jsonify({'promos': promos})

@app.route('/api/admin_create_promo', methods=['POST'])
def admin_create_promo():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO promos (code, uses, coins) VALUES (%s, %s, %s) "
                "ON CONFLICT (code) DO UPDATE SET uses = EXCLUDED.uses, coins = EXCLUDED.coins",
                (data.get('code'), data.get('uses'), data.get('coins')))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/admin_accept_deposit', methods=['POST'])
def admin_accept_deposit():
    dep_id = request.json.get('dep_id')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM deposits WHERE id = %s", (dep_id,))
    res = cur.fetchone()
    cur.execute("UPDATE deposits SET status = 'accepted' WHERE id = %s", (dep_id,))
    conn.commit(); conn.close()
    if res:
        notify(res[0], "✅ Ваша заявка на пополнение **принята**!\nУ вас есть 3 минуты, чтобы зайти в игру и **отдать брейнрота**.",
               parse_mode='Markdown')
    return jsonify({'success': True})

@app.route('/api/admin_give_coins', methods=['POST'])
def admin_give_coins():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    ensure_user(cur, int(data['user_id']))
    cur.execute("UPDATE users SET coins = coins + %s WHERE user_id = %s", (data['amount'], data['user_id']))
    cur.execute("UPDATE deposits SET status = 'completed' WHERE id = %s", (data['dep_id'],))
    conn.commit(); conn.close()
    notify(data['user_id'], f"✅ Вам начислено {data['amount']} монет!")
    return jsonify({'success': True})

@app.route('/api/admin_reject_deposit', methods=['POST'])
def admin_reject_deposit():
    dep_id = request.json.get('dep_id')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM deposits WHERE id = %s", (dep_id,))
    res = cur.fetchone()
    cur.execute("UPDATE deposits SET status = 'rejected' WHERE id = %s", (dep_id,))
    conn.commit(); conn.close()
    if res:
        notify(res[0], "❌ Ваша заявка на пополнение отклонена.")
    return jsonify({'success': True})

@app.route('/api/admin_accept_withdraw', methods=['POST'])
def admin_accept_withdraw():
    wd_id = request.json.get('wd_id')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM withdrawals WHERE id = %s", (wd_id,))
    res = cur.fetchone()
    cur.execute("UPDATE withdrawals SET status = 'accepted' WHERE id = %s", (wd_id,))
    conn.commit(); conn.close()
    if res:
        notify(res[0], "✅ Ваша заявка на вывод **принята**!\nУ вас есть 3 минуты, чтобы зайти в игру и **забрать брейнрота**.",
               parse_mode='Markdown')
    return jsonify({'success': True})

@app.route('/api/admin_complete_withdraw', methods=['POST'])
def admin_complete_withdraw():
    wd_id = request.json.get('wd_id')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM withdrawals WHERE id = %s", (wd_id,))
    res = cur.fetchone()
    cur.execute("UPDATE withdrawals SET status = 'completed' WHERE id = %s", (wd_id,))
    conn.commit(); conn.close()
    if res:
        notify(res[0], "✅ Ваш вывод успешно выполнен!")
    return jsonify({'success': True})

@app.route('/api/admin_reject_withdraw', methods=['POST'])
def admin_reject_withdraw():
    data = request.json
    wd_id = data.get('wd_id')
    return_items = data.get('return_items', False)
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT user_id, items FROM withdrawals WHERE id = %s", (wd_id,))
    res = cur.fetchone()
    cur.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = %s", (wd_id,))

    # Если админ не вернул предметы — удаляем их из инвентаря игрока
    if res and not return_items:
        try:
            uids = {i.get('uid') for i in json.loads(res[1] or '[]')}
            cur.execute("SELECT inventory FROM users WHERE user_id = %s", (res[0],))
            row = cur.fetchone()
            if row:
                inv = [i for i in json.loads(row[0] or '[]') if i.get('uid') not in uids]
                cur.execute("UPDATE users SET inventory = %s WHERE user_id = %s", (json.dumps(inv), res[0]))
        except Exception as e:
            print(f"Ошибка удаления предметов: {e}")
    conn.commit(); conn.close()
    if res:
        notify(res[0], "❌ Ваша заявка на вывод отклонена.")
    return jsonify({'success': True})

@app.route('/api/admin_clear_coins', methods=['POST'])
def admin_clear_coins():
    user_id = request.json.get('user_id')
    conn = get_db(); cur = conn.cursor()
    if user_id == 'all':
        cur.execute("UPDATE users SET coins = 0")
    else:
        cur.execute("UPDATE users SET coins = 0 WHERE user_id = %s", (user_id,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/admin_clear_inventory', methods=['POST'])
def admin_clear_inventory():
    user_id = request.json.get('user_id')
    conn = get_db(); cur = conn.cursor()
    if user_id == 'all':
        cur.execute("UPDATE users SET inventory = '[]'")
    else:
        cur.execute("UPDATE users SET inventory = '[]' WHERE user_id = %s", (user_id,))
    conn.commit(); conn.close()
    return jsonify({'success': True})

# ==========================================
# БОТ
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🎁 Ввести промокод", callback_data="enter_promo"))
    text = "👋 Добро пожаловать в Brainrot Cosmo!\n\n👇 **Чтобы играть, нажми на голубую кнопку «Играть» внизу экрана**."
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    if call.data == "enter_promo":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "Введите промокод (или 'отмена'):")
        bot.register_next_step_handler(msg, process_promo)

def process_promo(message):
    if not message.text or message.text.lower() == 'отмена':
        return
    code, user_id = message.text.strip(), message.from_user.id
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT uses, coins FROM promos WHERE code = %s", (code,))
    promo = cur.fetchone()
    if not promo:
        bot.send_message(message.chat.id, "❌ Неверный код!")
    elif promo[0] <= 0:
        bot.send_message(message.chat.id, "❌ Код закончился!")
    else:
        cur.execute("SELECT 1 FROM promo_uses WHERE code = %s AND user_id = %s", (code, user_id))
        if cur.fetchone():
            bot.send_message(message.chat.id, "❌ Вы уже вводили его!")
        else:
            ensure_user(cur, user_id)
            cur.execute("UPDATE promos SET uses = uses - 1 WHERE code = %s", (code,))
            cur.execute("INSERT INTO promo_uses (code, user_id) VALUES (%s, %s)", (code, user_id))
            cur.execute("UPDATE users SET coins = coins + %s WHERE user_id = %s", (promo[1], user_id))
            conn.commit()
            bot.send_message(message.chat.id, f"✅ Вам начислено {promo[1]} монет!")
    conn.close()

@bot.message_handler(commands=['createpromo'])
def create_promo(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        args = message.text.split()
        code, uses, coins = args[1], int(args[2]), int(args[3])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO promos (code, uses, coins) VALUES (%s, %s, %s) "
                    "ON CONFLICT (code) DO UPDATE SET uses = EXCLUDED.uses, coins = EXCLUDED.coins",
                    (code, uses, coins))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ Промокод `{code}` создан!", parse_mode='Markdown')
    except Exception:
        bot.reply_to(message, "Формат: /createpromo КОД АКТИВАЦИИ МОНЕТЫ")

if __name__ == '__main__':
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)