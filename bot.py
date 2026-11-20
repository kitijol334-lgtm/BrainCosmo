import telebot
from telebot import types
import json
import time
import threading
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import os
import psycopg2

# ==========================================
# НАСТРОЙКИ
# ==========================================
BOT_TOKEN = '9147236041:AAEcJCKj0XLpZhWKVh0dPV1Qs7_lf37KLhw'
ADMIN_ID = 8664148534
WEBAPP_URL = 'https://braincosmo111.onrender.com'
DATABASE_URL = os.environ.get('DATABASE_URL')

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='images')
CORS(app)

# ==========================================
# БАЗА ДАННЫХ (PostgreSQL ONLY)
# ==========================================
def get_db():
    if not DATABASE_URL:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL не найдена!")
        raise Exception("DATABASE_URL is missing")
    url = DATABASE_URL.replace('postgres://', 'postgresql://')
    return psycopg2.connect(url)

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, coins BIGINT DEFAULT 0,
        inventory TEXT DEFAULT '[]', last_free_case BIGINT DEFAULT 0,
        last_tag_case BIGINT DEFAULT 0
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id SERIAL PRIMARY KEY, user_id BIGINT, nick TEXT, status TEXT DEFAULT 'pending'
    )''')
    cursor.execute("ALTER TABLE deposits ADD COLUMN IF NOT EXISTS tg_username TEXT")

    cursor.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id SERIAL PRIMARY KEY, user_id BIGINT, nick TEXT, items TEXT, status TEXT DEFAULT 'pending'
    )''')
    cursor.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS tg_username TEXT")

    cursor.execute('''CREATE TABLE IF NOT EXISTS promos (
        code TEXT PRIMARY KEY, uses INTEGER, coins INTEGER
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS promo_uses (
        code TEXT, user_id BIGINT, UNIQUE(code, user_id)
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )''')
    cursor.execute("INSERT INTO settings (key, value) VALUES ('allowedWithdrawals', '[]') ON CONFLICT DO NOTHING")

    cursor.execute('''CREATE TABLE IF NOT EXISTS stars_payments (
        id SERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT,
        charge_id TEXT, status TEXT DEFAULT 'paid', created_at BIGINT
    )''')

    conn.commit()
    conn.close()
    print("✅ База данных Postgres успешно инициализирована.")

try:
    init_db()
except Exception as e:
    print(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ К БД: {e}")

# ==========================================
# ОТДАЧА HTML, КАРТИНОК И ЗВУКОВ + ПИНГ
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

@app.route('/<filename>')
def serve_root_audio(filename):
    if filename.endswith('.mp3') or filename.endswith('.wav'):
        return send_from_directory('.', filename)
    return "Not found", 404

# ==========================================
# API ДЛЯ ИГРЫ
# ==========================================
@app.route('/api/state', methods=['GET'])
def get_state():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': 'No user_id'}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT coins, inventory, last_free_case, last_tag_case FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
        user = (0, '[]', 0, 0)

    conn.close()
    return jsonify({
        'coins': user[0] or 0,
        'inventory': json.loads(user[1] or '[]'),
        'last_free_case': user[2] or 0,
        'last_tag_case': user[3] or 0
    })

@app.route('/api/save', methods=['POST'])
def save_state():
    data = request.json
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': 'No user_id'}), 400
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    cursor.execute(
        "UPDATE users SET coins = %s, inventory = %s, last_free_case = %s, last_tag_case = %s WHERE user_id = %s",
        (data.get('coins', 0), json.dumps(data.get('inventory', [])),
         data.get('last_free_case', 0), data.get('last_tag_case', 0), user_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/deposit', methods=['POST'])
def api_deposit():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO deposits (user_id, tg_username, nick) VALUES (%s, %s, %s)",
                   (data['user_id'], data.get('tg_username', ''), data['nick']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/withdraw', methods=['POST'])
def api_withdraw():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO withdrawals (user_id, tg_username, nick, items) VALUES (%s, %s, %s, %s)",
                   (data['user_id'], data.get('tg_username', ''), data['nick'], json.dumps(data['items'])))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ==========================================
# ОПЛАТА ЗВЁЗДАМИ (TELEGRAM STARS)
# ==========================================
@app.route('/api/create_stars_invoice', methods=['POST'])
def create_stars_invoice():
    data = request.json
    user_id = data.get('user_id')
    amount = data.get('amount')

    if not user_id or not amount:
        return jsonify({'error': 'user_id и amount обязательны'}), 400

    try:
        amount = int(amount)
        if amount < 1 or amount > 10000:
            return jsonify({'error': 'Сумма от 1 до 10000'}), 400
    except Exception:
        return jsonify({'error': 'Неверная сумма'}), 400

    try:
        invoice_link = bot.create_invoice_link(
            title="Brainrot Cosmo — Монеты",
            description=f"Покупка {amount} монет за {amount} звёзд",
            payload=f"stars_deposit_{user_id}_{amount}",
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{amount} монет", amount=amount)]
        )
        print(f"✅ Invoice создан для {user_id}: {amount} звезд")
        return jsonify({'invoice_link': invoice_link})
    except Exception as e:
        print(f"❌ Ошибка создания invoice: {e}")
        return jsonify({'error': str(e)}), 500

# ==========================================
# API ДЛЯ НАСТРОЕК АДМИНКИ
# ==========================================
@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'allowedWithdrawals'")
    res = cursor.fetchone()
    conn.close()
    return jsonify({'allowedWithdrawals': json.loads(res[0]) if res else []})

@app.route('/api/admin_save_settings', methods=['POST'])
def admin_save_settings():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = %s WHERE key = 'allowedWithdrawals'",
                   (json.dumps(data.get('allowedWithdrawals', [])),))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ==========================================
# API ДЛЯ АДМИНКИ (Заявки и Промокоды)
# ==========================================
@app.route('/api/get_all_requests', methods=['GET'])
def get_all_requests():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, user_id, tg_username, nick, status FROM deposits ORDER BY id DESC")
    deposits = [{'id': r[0], 'user_id': r[1], 'tg_username': r[2], 'nick': r[3], 'status': r[4]}
                for r in cursor.fetchall()]

    cursor.execute("SELECT id, user_id, tg_username, nick, items, status FROM withdrawals ORDER BY id DESC")
    withdrawals = []
    for r in cursor.fetchall():
        withdrawals.append({
            'id': r[0], 'user_id': r[1], 'tg_username': r[2], 'nick': r[3],
            'items': json.loads(r[4]), 'status': r[5]
        })

    conn.close()
    return jsonify({'deposits': deposits, 'withdrawals': withdrawals})

@app.route('/api/admin_get_promos', methods=['GET'])
def admin_get_promos():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.code, p.uses, p.coins, COUNT(pu.user_id) as used_count
        FROM promos p
        LEFT JOIN promo_uses pu ON p.code = pu.code
        GROUP BY p.code, p.uses, p.coins
        ORDER BY p.code
    """)
    promos = [{'code': r[0], 'uses': r[1], 'coins': r[2], 'used_count': r[3]} for r in cursor.fetchall()]
    conn.close()
    return jsonify({'promos': promos})

@app.route('/api/admin_create_promo', methods=['POST'])
def admin_create_promo():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO promos (code, uses, coins) VALUES (%s, %s, %s) "
        "ON CONFLICT (code) DO UPDATE SET uses = EXCLUDED.uses, coins = EXCLUDED.coins",
        (data.get('code'), data.get('uses'), data.get('coins')))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/admin_accept_deposit', methods=['POST'])
def admin_accept_deposit():
    dep_id = request.json.get('dep_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM deposits WHERE id = %s", (dep_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE deposits SET status = 'accepted' WHERE id = %s", (dep_id,))
    conn.commit(); conn.close()

    if res:
        try:
            bot.send_message(res[0], "✅ Ваша заявка на пополнение **принята**!\nУ вас есть 3 минуты, чтобы зайти в игру и **получить монеты**.", parse_mode='Markdown')
        except Exception as e:
            print(f"Ошибка: {e}")
    return jsonify({'success': True})

@app.route('/api/admin_give_coins', methods=['POST'])
def admin_give_coins():
    data = request.json
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("UPDATE users SET coins = coins + %s WHERE user_id = %s", (data['amount'], data['user_id']))
    cursor.execute("UPDATE deposits SET status = 'completed' WHERE id = %s", (data['dep_id'],))
    conn.commit(); conn.close()

    try:
        bot.send_message(data['user_id'], f"✅ Вам начислено {data['amount']} монет!")
    except Exception as e:
        print(f"Ошибка: {e}")
    return jsonify({'success': True})

@app.route('/api/admin_reject_deposit', methods=['POST'])
def admin_reject_deposit():
    dep_id = request.json.get('dep_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM deposits WHERE id = %s", (dep_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE deposits SET status = 'rejected' WHERE id = %s", (dep_id,))
    conn.commit(); conn.close()

    if res:
        try:
            bot.send_message(res[0], "❌ Ваша заявка на пополнение отклонена.")
        except Exception as e:
            print(f"Ошибка: {e}")
    return jsonify({'success': True})

@app.route('/api/admin_accept_withdraw', methods=['POST'])
def admin_accept_withdraw():
    wd_id = request.json.get('wd_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM withdrawals WHERE id = %s", (wd_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE withdrawals SET status = 'accepted' WHERE id = %s", (wd_id,))
    conn.commit(); conn.close()

    if res:
        try:
            bot.send_message(res[0], "✅ Ваша заявка на вывод **принята**!\nУ вас есть 3 минуты, чтобы зайти в игру и **отдать брейнрота**.", parse_mode='Markdown')
        except Exception as e:
            print(f"Ошибка: {e}")
    return jsonify({'success': True})

@app.route('/api/admin_complete_withdraw', methods=['POST'])
def admin_complete_withdraw():
    wd_id = request.json.get('wd_id')
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM withdrawals WHERE id = %s", (wd_id,))
    res = cursor.fetchone()
    cursor.execute("UPDATE withdrawals SET status = 'completed' WHERE id = %s", (wd_id,))
    conn.commit(); conn.close()

    if res:
        try:
            bot.send_message(res[0], "✅ Ваш вывод успешно выполнен!")
        except Exception as e:
            print(f"Ошибка: {e}")
    return jsonify({'success': True})

@app.route('/api/admin_reject_withdraw', methods=['POST'])
def admin_reject_withdraw():
    data = request.json
    wd_id = data.get('wd_id')
    return_items = bool(data.get('return_items'))
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT user_id, items, status FROM withdrawals WHERE id = %s", (wd_id,))
    res = cursor.fetchone()
    if res and res[2] not in ('rejected', 'completed'):
        cursor.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = %s", (wd_id,))
        # Если админ выбрал "вернуть" и предметов нет в инвентаре — добавляем их обратно
        if return_items:
            cursor.execute("SELECT inventory FROM users WHERE user_id = %s", (res[0],))
            u = cursor.fetchone()
            if u:
                inv = json.loads(u[0] or '[]')
                have = {i.get('uid') for i in inv}
                for it in json.loads(res[1] or '[]'):
                    if it.get('uid') not in have:
                        inv.append(it)
                cursor.execute("UPDATE users SET inventory = %s WHERE user_id = %s", (json.dumps(inv), res[0]))
        else:
            # "Уничтожить": убираем предметы из инвентаря игрока
            cursor.execute("SELECT inventory FROM users WHERE user_id = %s", (res[0],))
            u = cursor.fetchone()
            if u:
                uids = {it.get('uid') for it in json.loads(res[1] or '[]')}
                inv = [i for i in json.loads(u[0] or '[]') if i.get('uid') not in uids]
                cursor.execute("UPDATE users SET inventory = %s WHERE user_id = %s", (json.dumps(inv), res[0]))
    conn.commit(); conn.close()

    if res:
        try:
            bot.send_message(res[0], "❌ Ваша заявка на вывод отклонена.")
        except Exception as e:
            print(f"Ошибка: {e}")
    return jsonify({'success': True})

# ==========================================
# ОЧИСТКА МОНЕТ / ИНВЕНТАРЯ (ИСПРАВЛЕНО)
# ==========================================
def find_user_id(cursor, nick):
    """Ищет игрока по нику в игре или по @username Telegram."""
    nick = nick.strip().lstrip('@')
    for q in ("SELECT user_id FROM deposits WHERE nick = %s OR tg_username = %s ORDER BY id DESC LIMIT 1",
              "SELECT user_id FROM withdrawals WHERE nick = %s OR tg_username = %s ORDER BY id DESC LIMIT 1"):
        cursor.execute(q, (nick, nick))
        r = cursor.fetchone()
        if r:
            return r[0]
    return None

def _clear(column, value):
    nick = ((request.json or {}).get('nick') or '').strip()
    conn = get_db(); cursor = conn.cursor()
    if not nick:
        cursor.execute(f"UPDATE users SET {column} = %s", (value,))
    else:
        uid = find_user_id(cursor, nick)
        if uid is None:
            conn.close()
            return jsonify({'success': False, 'error': 'Игрок не найден'})
        cursor.execute(f"UPDATE users SET {column} = %s WHERE user_id = %s", (value, uid))
    conn.commit(); conn.close()
    return jsonify({'success': True})

@app.route('/api/admin_clear_coins', methods=['POST'])
def admin_clear_coins():
    return _clear('coins', 0)

@app.route('/api/admin_clear_inventory', methods=['POST'])
def admin_clear_inventory():
    return _clear('inventory', '[]')

# ==========================================
# TELEGRAM STARS — PRE-CHECKOUT И ОПЛАТА
# ==========================================
@bot.pre_checkout_query_handler(func=lambda query: True)
def handle_pre_checkout(pre_checkout_q):
    try:
        bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)
        print(f"✅ Pre-checkout OK: {pre_checkout_q.from_user.id}")
    except Exception as e:
        print(f"❌ Ошибка pre_checkout: {e}")
        try:
            bot.answer_pre_checkout_query(pre_checkout_q.id, ok=False, error_message="Ошибка валидации")
        except Exception:
            pass

@bot.message_handler(content_types=['successful_payment'])
def handle_successful_payment(message):
    try:
        payment = message.successful_payment
        user_id = message.from_user.id
        amount = payment.total_amount
        charge_id = payment.telegram_payment_charge_id
        payload = payment.invoice_payload

        print(f"💰 Оплата получена: user={user_id}, stars={amount}, payload={payload}, charge={charge_id}")

        conn = get_db()
        cursor = conn.cursor()

        # защита от повторного начисления за один и тот же платёж
        cursor.execute("SELECT 1 FROM stars_payments WHERE charge_id = %s", (charge_id,))
        if cursor.fetchone():
            conn.close()
            return

        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        cursor.execute("UPDATE users SET coins = coins + %s WHERE user_id = %s", (amount, user_id))
        cursor.execute("INSERT INTO stars_payments (user_id, amount, charge_id, status, created_at) VALUES (%s, %s, %s, 'paid', %s)",
                       (user_id, amount, charge_id, int(time.time())))

        conn.commit()
        conn.close()

        try:
            bot.send_message(user_id, f"⭐ **Оплата успешна!**\n\nВам начислено **{amount}** монет!\nСпасибо за поддержку ❤️", parse_mode='Markdown')
        except Exception as e:
            print(f"Ошибка отправки сообщения: {e}")

    except Exception as e:
        print(f"❌ Ошибка обработки successful_payment: {e}")

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
    cursor = conn.cursor()
    cursor.execute("SELECT uses, coins FROM promos WHERE code = %s", (code,))
    promo = cursor.fetchone()
    if not promo:
        bot.send_message(message.chat.id, "❌ Неверный код!")
    elif promo[0] <= 0:
        bot.send_message(message.chat.id, "❌ Код закончился!")
    else:
        cursor.execute("SELECT * FROM promo_uses WHERE code = %s AND user_id = %s", (code, user_id))
        if cursor.fetchone():
            bot.send_message(message.chat.id, "❌ Вы уже вводили его!")
        else:
            cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
            cursor.execute("UPDATE promos SET uses = uses - 1 WHERE code = %s", (code,))
            cursor.execute("INSERT INTO promo_uses (code, user_id) VALUES (%s, %s)", (code, user_id))
            cursor.execute("UPDATE users SET coins = coins + %s WHERE user_id = %s", (promo[1], user_id))
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
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO promos (code, uses, coins) VALUES (%s, %s, %s) "
            "ON CONFLICT (code) DO UPDATE SET uses = EXCLUDED.uses, coins = EXCLUDED.coins",
            (code, uses, coins))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ Промокод `{code}` создан!", parse_mode='Markdown')
    except Exception:
        bot.reply_to(message, "Формат: /createpromo КОД АКТИВАЦИИ МОНЕТЫ")

@bot.message_handler(commands=['stars_stats'])
def stars_stats(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM stars_payments WHERE status = 'paid'")
        r = cursor.fetchone()
        conn.close()
        bot.reply_to(message, f"⭐ Всего платежей: {r[0]}\n💰 Всего звёзд: {r[1]}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

if __name__ == '__main__':
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
