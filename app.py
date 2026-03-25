from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import json
import os
from datetime import datetime
from functools import wraps
import hashlib
import socket
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')
socketio = SocketIO(app, cors_allowed_origins="*")

# Файлы для хранения данных
USERS_FILE = 'users.json'
MESSAGES_FILE = 'messages.json'

# PostgreSQL (Render) mode
DATABASE_URL = os.getenv("DATABASE_URL")
USE_DB = bool(DATABASE_URL)

# Загрузка данных с обработкой ошибок
def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
                else:
                    return {}
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Ошибка чтения файла {USERS_FILE}: {e}")
            print("Создаем новый файл пользователей")
            return {}
    return {}

def save_users(users):
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения {USERS_FILE}: {e}")

def load_messages():
    if os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
                else:
                    return []
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Ошибка чтения файла {MESSAGES_FILE}: {e}")
            print("Создаем новый файл сообщений")
            return []
    return []

def save_messages(messages):
    try:
        with open(MESSAGES_FILE, 'w', encoding='utf-8') as f:
            json.dump(messages[-5000:], f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения {MESSAGES_FILE}: {e}")

def db_connect():
    # Render обычно передает DATABASE_URL в виде postgres://...
    return psycopg2.connect(DATABASE_URL)

def db_init():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    from_user TEXT NOT NULL,
                    from_nickname TEXT NOT NULL,
                    to_user TEXT NOT NULL,
                    message TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_pair
                ON messages(from_user, to_user)
            """)

def db_get_user(username):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT username, password_hash, nickname, created_at FROM users WHERE username=%s",
                (username,),
            )
            return cur.fetchone()

def db_get_all_users():
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT username, nickname FROM users ORDER BY username ASC")
            return cur.fetchall()

def db_create_user(username, password_hash, nickname, created_at):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, nickname, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (username, password_hash, nickname, created_at),
            )

def db_insert_message(from_user, from_nickname, to_user, message, ts_dt):
    ts_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (from_user, from_nickname, to_user, message, timestamp)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (from_user, from_nickname, to_user, message, ts_dt),
            )
            msg_id = cur.fetchone()[0]
    return {
        'id': msg_id,
        'from': from_user,
        'from_nickname': from_nickname,
        'to': to_user,
        'message': message,
        'timestamp': ts_str,
    }

def db_get_history(current_user, other_user):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    from_user,
                    from_nickname,
                    to_user,
                    message,
                    to_char(timestamp, 'YYYY-MM-DD HH24:MI:SS') AS timestamp
                FROM messages
                WHERE
                    (from_user=%s AND to_user=%s)
                    OR (from_user=%s AND to_user=%s)
                ORDER BY id ASC
                """,
                (current_user, other_user, other_user, current_user),
            )
            rows = cur.fetchall()

    history = []
    for r in rows:
        history.append({
            'id': r['id'],
            'from': r['from_user'],
            'from_nickname': r['from_nickname'],
            'to': r['to_user'],
            'message': r['message'],
            'timestamp': r['timestamp'],
        })
    return history

# Инициализация данных
if USE_DB:
    db_init()
    users = None
    messages = None
else:
    users = load_users()
    messages = load_messages()

# Хранение активных пользователей
active_users = {}  # {username: {'sid': sid, 'room': room}}

def hash_password(password):
    """Хеширование пароля"""
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    """Декоратор для проверки авторизации"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Not logged in'}), 401
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')

@app.route('/api/register', methods=['POST'])
def register():
    """Регистрация пользователя"""
    data = request.json
    username = data.get('username')
    password = data.get('password')
    nickname = data.get('nickname')
    
    if not username or not password or not nickname:
        return jsonify({'success': False, 'message': 'Все поля обязательны'})

    password_hash = hash_password(password)

    if USE_DB:
        try:
            db_create_user(username, password_hash, nickname, datetime.now())
        except psycopg2.IntegrityError:
            return jsonify({'success': False, 'message': 'Имя пользователя уже существует'})
        return jsonify({'success': True, 'message': 'Регистрация успешна!'})

    if username in users:
        return jsonify({'success': False, 'message': 'Имя пользователя уже существует'})

    users[username] = {
        'password': password_hash,
        'nickname': nickname,
        'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_users(users)

    return jsonify({'success': True, 'message': 'Регистрация успешна!'})

@app.route('/api/login', methods=['POST'])
def login():
    """Вход в систему"""
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if USE_DB:
        user = db_get_user(username)
        if user and user["password_hash"] == hash_password(password):
            session['username'] = username
            session['nickname'] = user["nickname"]
            return jsonify({
                'success': True,
                'nickname': user["nickname"],
                'username': username
            })
        return jsonify({'success': False, 'message': 'Неверное имя пользователя или пароль'})

    if username in users and users[username]['password'] == hash_password(password):
        session['username'] = username
        session['nickname'] = users[username]['nickname']
        return jsonify({
            'success': True,
            'nickname': users[username]['nickname'],
            'username': username
        })

    return jsonify({'success': False, 'message': 'Неверное имя пользователя или пароль'})

@app.route('/api/logout', methods=['POST'])
def logout():
    """Выход из системы"""
    if 'username' in session:
        username = session['username']
        if username in active_users:
            del active_users[username]
        session.clear()
    return jsonify({'success': True})

@app.route('/api/check_auth')
def check_auth():
    """Проверка авторизации"""
    if 'username' in session:
        return jsonify({
            'authenticated': True,
            'username': session['username'],
            'nickname': session.get('nickname')
        })
    return jsonify({'authenticated': False})

@app.route('/api/users')
@login_required
def get_users():
    """Получение списка всех пользователей"""
    if USE_DB:
        all_users = []
        rows = db_get_all_users()
        for r in rows:
            all_users.append({
                'username': r['username'],
                'nickname': r['nickname'],
                'is_online': r['username'] in active_users
            })
        return jsonify(all_users)

    all_users = []
    for username, data in users.items():
        all_users.append({
            'username': username,
            'nickname': data['nickname'],
            'is_online': username in active_users
        })
    return jsonify(all_users)

@app.route('/api/history/<other_user>')
@login_required
def get_history(other_user):
    """Получение истории переписки"""
    current_user = session['username']
    if USE_DB:
        return jsonify(db_get_history(current_user, other_user))

    history = []
    for msg in messages:
        if (msg['from'] == current_user and msg['to'] == other_user) or \
           (msg['from'] == other_user and msg['to'] == current_user):
            history.append(msg)
    return jsonify(history)

# SocketIO события
@socketio.on('connect')
def handle_connect():
    """Обработка подключения"""
    if 'username' in session:
        username = session['username']
        active_users[username] = {
            'sid': request.sid,
            'room': None
        }
        emit('user_list_update', get_user_list(), broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    """Обработка отключения"""
    if 'username' in session:
        username = session['username']
        if username in active_users:
            del active_users[username]
        emit('user_list_update', get_user_list(), broadcast=True)

@socketio.on('join_chat')
def handle_join_chat(data):
    """Присоединение к чату с пользователем"""
    current_user = session['username']
    other_user = data['other_user']
    
    # Создаем уникальную комнату для двух пользователей
    room = f"chat_{min(current_user, other_user)}_{max(current_user, other_user)}"
    
    # Выходим из предыдущей комнаты
    if active_users[current_user]['room']:
        leave_room(active_users[current_user]['room'])
    
    # Присоединяемся к новой комнате
    join_room(room)
    active_users[current_user]['room'] = room
    
    # Загружаем историю сообщений
    if USE_DB:
        history = db_get_history(current_user, other_user)
    else:
        history = []
        for msg in messages:
            if (msg['from'] == current_user and msg['to'] == other_user) or \
               (msg['from'] == other_user and msg['to'] == current_user):
                history.append(msg)

    emit('chat_history', {'history': history, 'other_user': other_user})

@socketio.on('send_message')
def handle_send_message(data):
    """Отправка сообщения"""
    current_user = session['username']
    to_user = data['to']
    message = data['message']
    
    if not message.strip():
        return

    ts_dt = datetime.now()

    if USE_DB:
        msg_data = db_insert_message(
            from_user=current_user,
            from_nickname=session['nickname'],
            to_user=to_user,
            message=message,
            ts_dt=ts_dt,
        )
    else:
        msg_data = {
            'id': len(messages) + 1,
            'from': current_user,
            'from_nickname': session['nickname'],
            'to': to_user,
            'message': message,
            'timestamp': ts_dt.strftime("%Y-%m-%d %H:%M:%S")
        }

        # Сохраняем сообщение
        messages.append(msg_data)
        save_messages(messages)
    
    # Создаем комнату для отправки
    room = f"chat_{min(current_user, to_user)}_{max(current_user, to_user)}"
    
    # Отправляем сообщение в комнату
    emit('new_message', msg_data, room=room)
    
    # Если получатель в другой комнате, отправляем уведомление
    if to_user in active_users:
        recipient_sid = active_users[to_user]['sid']
        emit('message_notification', {
            'from': current_user,
            'from_nickname': session['nickname'],
            'message': message
        }, room=recipient_sid)

def get_user_list():
    """Получение списка пользователей с онлайн статусом"""
    if USE_DB:
        user_list = []
        rows = db_get_all_users()
        for r in rows:
            user_list.append({
                'username': r['username'],
                'nickname': r['nickname'],
                'is_online': r['username'] in active_users
            })
        return user_list

    user_list = []
    for username, data in users.items():
        user_list.append({
            'username': username,
            'nickname': data['nickname'],
            'is_online': username in active_users
        })
    return user_list

if __name__ == '__main__':
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '5000'))
    debug = os.getenv('DEBUG', '0') == '1'
    public_url = os.getenv('PUBLIC_URL', '').strip()

    local_ip = '127.0.0.1'
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass

    print("=" * 50)
    print("Trousers Messenger запущен!")
    print(f"Локально: http://127.0.0.1:{port}")
    print(f"В локальной сети: http://{local_ip}:{port}")
    if public_url:
        print(f"Публичная ссылка: {public_url}")
    print("=" * 50)
    socketio.run(app, debug=debug, host=host, port=port)