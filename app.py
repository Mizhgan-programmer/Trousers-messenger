from flask import Flask, render_template, request, jsonify, session, send_file, abort
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
from datetime import datetime, timedelta
from functools import wraps
import hashlib
import socket
from io import BytesIO
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor
import traceback

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['PERMANENT_SESSION_LIFETIME'] = 604800
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 3600

socketio = SocketIO(app, cors_allowed_origins="*", ping_timeout=60, ping_interval=25, logger=False, engineio_logger=False)

DATABASE_URL = os.getenv("DATABASE_URL")
USE_DB = bool(DATABASE_URL)

active_users = {}

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Not logged in'}), 401
        return f(*args, **kwargs)
    return decorated_function

# ========== РАБОТА С БАЗОЙ ДАННЫХ ==========
def db_connect():
    return psycopg2.connect(DATABASE_URL)

def db_init():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    avatar_data BYTEA,
                    avatar_mime TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    from_user TEXT NOT NULL,
                    from_nickname TEXT NOT NULL,
                    to_user TEXT NOT NULL,
                    message TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    deleted_for_sender BOOLEAN DEFAULT FALSE,
                    deleted_for_recipient BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages(from_user, to_user)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id SERIAL PRIMARY KEY,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    file_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    data BYTEA NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reactions (
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    user_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    emoji TEXT NOT NULL,
                    PRIMARY KEY (message_id, user_username)
                )
            """)

def db_get_user(username):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT username, password_hash, nickname FROM users WHERE username=%s", (username,))
            return cur.fetchone()

def db_get_all_users():
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT username, nickname FROM users ORDER BY nickname ASC")
            return cur.fetchall()

def db_create_user(username, password_hash, nickname, created_at):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (username, password_hash, nickname, created_at) VALUES (%s, %s, %s, %s)",
                       (username, password_hash, nickname, created_at))

def db_insert_message(from_user, from_nickname, to_user, message, ts_dt):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages (from_user, from_nickname, to_user, message, timestamp) 
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (from_user, from_nickname, to_user, message, ts_dt))
            msg_id = cur.fetchone()[0]
    return {
        'id': msg_id,
        'from': from_user,
        'from_nickname': from_nickname,
        'to': to_user,
        'message': message,
        'timestamp': ts_dt.strftime("%Y-%m-%d %H:%M:%S")
    }

def db_insert_attachment(message_id, file_name, mime_type, data_bytes):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO attachments (message_id, file_name, mime_type, data) 
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (message_id, file_name, mime_type, psycopg2.Binary(data_bytes)))
            return cur.fetchone()[0]

# ========== АВТОУДАЛЕНИЕ СТАРЫХ СООБЩЕНИЙ (2 часа) ==========
def auto_delete_old_messages():
    """Удаляет сообщения старше 2 часов"""
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cutoff_time = datetime.now() - timedelta(hours=2)
                cur.execute("DELETE FROM messages WHERE timestamp < %s", (cutoff_time,))
                deleted = cur.rowcount
                conn.commit()
                if deleted > 0:
                    print(f"[AUTO-DELETE] Удалено {deleted} сообщений (старше 2 часов)")
    except Exception as e:
        print(f"[AUTO-DELETE ERROR] {e}")

def db_get_history(current_user, other_user):
    auto_delete_old_messages()  # Автоудаление при загрузке истории
    
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, from_user, from_nickname, to_user, message, 
                       to_char(timestamp, 'YYYY-MM-DD HH24:MI:SS') AS timestamp
                FROM messages
                WHERE ((from_user=%s AND to_user=%s AND deleted_for_sender=FALSE) 
                    OR (from_user=%s AND to_user=%s AND deleted_for_recipient=FALSE))
                ORDER BY id ASC
            """, (current_user, other_user, other_user, current_user))
            rows = cur.fetchall()

    history = []
    for r in rows:
        with db_connect() as conn2:
            with conn2.cursor(cursor_factory=RealDictCursor) as cur2:
                cur2.execute("SELECT id, file_name, mime_type FROM attachments WHERE message_id=%s", (r['id'],))
                atts = cur2.fetchall()
                cur2.execute("SELECT emoji FROM reactions WHERE message_id=%s AND user_username=%s", (r['id'], current_user))
                my_reaction = cur2.fetchone()
                cur2.execute("SELECT emoji, COUNT(*) FROM reactions WHERE message_id=%s GROUP BY emoji", (r['id'],))
                reactions = cur2.fetchall()

        history.append({
            'id': r['id'],
            'from': r['from_user'],
            'from_nickname': r['from_nickname'],
            'to': r['to_user'],
            'message': r['message'],
            'timestamp': r['timestamp'],
            'attachments': [{'id': a['id'], 'url': f"/api/attachment/{a['id']}", 'file_name': a['file_name'], 'mime_type': a['mime_type']} for a in atts],
            'reactions': [{'emoji': r[0], 'count': r[1]} for r in reactions],
            'my_reaction': my_reaction['emoji'] if my_reaction else None,
        })
    return history

def db_get_attachment(attachment_id):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT data, mime_type, file_name FROM attachments WHERE id=%s", (attachment_id,))
            return cur.fetchone()

def db_get_avatar(username):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT avatar_data, avatar_mime FROM users WHERE username=%s", (username,))
            return cur.fetchone()

def db_update_nickname(username, new_nickname):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET nickname=%s WHERE username=%s", (new_nickname, username))

def db_update_password(username, new_hash):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash=%s WHERE username=%s", (new_hash, username))

def db_update_avatar(username, data_bytes, mime_type):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET avatar_data=%s, avatar_mime=%s WHERE username=%s",
                       (psycopg2.Binary(data_bytes), mime_type, username))

def db_add_reaction(message_id, username, emoji):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reactions (message_id, user_username, emoji) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (message_id, user_username) 
                DO UPDATE SET emoji=EXCLUDED.emoji
            """, (message_id, username, emoji))

def db_remove_reaction(message_id, username):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reactions WHERE message_id=%s AND user_username=%s", (message_id, username))
            return cur.rowcount > 0

def db_delete_message(message_id, username, is_sender):
    with db_connect() as conn:
        with conn.cursor() as cur:
            if is_sender:
                cur.execute("UPDATE messages SET deleted_for_sender=TRUE WHERE id=%s", (message_id,))
            else:
                cur.execute("UPDATE messages SET deleted_for_recipient=TRUE WHERE id=%s", (message_id,))

def db_get_message_owner(message_id):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT from_user, to_user FROM messages WHERE id=%s", (message_id,))
            return cur.fetchone()

def get_reaction_payload(message_id, current_user):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT emoji, COUNT(*) FROM reactions WHERE message_id=%s GROUP BY emoji", (message_id,))
            reactions = cur.fetchall()
            cur.execute("SELECT emoji FROM reactions WHERE message_id=%s AND user_username=%s", (message_id, current_user))
            my_reaction = cur.fetchone()
    return {
        'reactions': [{'emoji': r[0], 'count': r[1]} for r in reactions],
        'my_reaction': my_reaction['emoji'] if my_reaction else None,
    }

def chat_room_for_users(user_a, user_b):
    return f"chat_{min(user_a, user_b)}_{max(user_a, user_b)}"

def get_user_list():
    rows = db_get_all_users()
    return [{'username': r['username'], 'nickname': r['nickname'], 'is_online': r['username'] in active_users} for r in rows]

if USE_DB:
    db_init()

# ========== МАРШРУТЫ ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    nickname = data.get('nickname', '').strip()
    
    if not username or not password or not nickname:
        return jsonify({'success': False, 'message': 'Все поля обязательны'})

    try:
        db_create_user(username, hash_password(password), nickname, datetime.now())
        return jsonify({'success': True, 'message': 'Регистрация успешна!'})
    except psycopg2.IntegrityError:
        return jsonify({'success': False, 'message': 'Имя пользователя уже существует'})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    user = db_get_user(username)
    if user and user['password_hash'] == hash_password(password):
        session['username'] = username
        session['nickname'] = user['nickname']
        session.permanent = True
        return jsonify({'success': True, 'nickname': user['nickname'], 'username': username})
    return jsonify({'success': False, 'message': 'Неверное имя пользователя или пароль'})

@app.route('/api/logout', methods=['POST'])
def logout():
    if 'username' in session:
        active_users.pop(session['username'], None)
        session.clear()
    return jsonify({'success': True})

@app.route('/api/check_auth')
def check_auth():
    if 'username' in session:
        return jsonify({'authenticated': True, 'username': session['username'], 'nickname': session.get('nickname')})
    return jsonify({'authenticated': False})

@app.route('/api/users')
@login_required
def get_users():
    return jsonify(get_user_list())

@app.route('/api/history/<other_user>')
@login_required
def get_history(other_user):
    return jsonify(db_get_history(session['username'], other_user))

@app.route('/api/attachment/<int:attachment_id>')
@login_required
def api_attachment(attachment_id):
    row = db_get_attachment(attachment_id)
    if not row:
        abort(404)
    return send_file(BytesIO(row['data']), mimetype=row['mime_type'], download_name=row['file_name'], as_attachment=False)

@app.route('/api/avatar/<username>')
def api_avatar(username):
    row = db_get_avatar(username)
    if not row or not row.get('avatar_data'):
        # Возвращаем SVG-заглушку с инициалами (без Pillow!)
        return send_file(BytesIO(generate_svg_avatar(username)), mimetype='image/svg+xml')
    return send_file(BytesIO(row['avatar_data']), mimetype=row.get('avatar_mime', 'image/png'))

def generate_svg_avatar(username):
    """Генерирует SVG-аватарку с инициалами (не требует Pillow)"""
    letter = username[0].upper() if username else '?'
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">
        <rect width="100" height="100" rx="50" fill="#667eea"/>
        <text x="50" y="65" text-anchor="middle" fill="white" font-size="40" font-family="Arial, sans-serif" font-weight="bold">{letter}</text>
    </svg>'''
    return svg.encode('utf-8')

@app.route('/api/update_nickname', methods=['POST'])
@login_required
def update_nickname():
    nickname = request.json.get('nickname', '').strip()
    if not nickname:
        return jsonify({'success': False, 'message': 'Никнейм не может быть пустым'})
    db_update_nickname(session['username'], nickname)
    session['nickname'] = nickname
    socketio.emit('user_list_update', get_user_list(), broadcast=True)
    return jsonify({'success': True})

@app.route('/api/update_password', methods=['POST'])
@login_required
def update_password():
    data = request.json
    current = data.get('current_password', '').strip()
    new = data.get('new_password', '').strip()
    if not current or not new:
        return jsonify({'success': False, 'message': 'Заполните все поля'})
    user = db_get_user(session['username'])
    if not user or user['password_hash'] != hash_password(current):
        return jsonify({'success': False, 'message': 'Текущий пароль неверный'})
    db_update_password(session['username'], hash_password(new))
    return jsonify({'success': True})

@app.route('/api/update_avatar', methods=['POST'])
@login_required
def update_avatar():
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'message': 'Файл не найден'}), 400
    avatar = request.files['avatar']
    if not avatar or not avatar.filename:
        return jsonify({'success': False, 'message': 'Выберите файл'}), 400
    db_update_avatar(session['username'], avatar.read(), avatar.mimetype or 'image/png')
    return jsonify({'success': True})

@app.route('/api/send_message', methods=['POST'])
@login_required
def api_send_message():
    try:
        to_user = request.form.get('to', '').strip()
        message_text = request.form.get('message', '').strip()
        files = request.files.getlist('files')

        if not to_user:
            return jsonify({'success': False, 'message': 'Не выбран собеседник'}), 400
        if not message_text and not files:
            return jsonify({'success': False, 'message': 'Сообщение или файлы обязательны'}), 400

        recipient = db_get_user(to_user)
        if not recipient:
            return jsonify({'success': False, 'message': 'Получатель не найден'}), 404

        msg_data = db_insert_message(session['username'], session['nickname'], to_user, message_text or '[Вложение]', datetime.now())

        attachments = []
        for f in files:
            if f and f.filename:
                filename = secure_filename(f.filename)
                aid = db_insert_attachment(msg_data['id'], filename, f.mimetype or 'application/octet-stream', f.read())
                attachments.append({'id': aid, 'url': f"/api/attachment/{aid}", 'file_name': filename, 'mime_type': f.mimetype})

        msg_data['attachments'] = attachments
        msg_data['reactions'] = []
        msg_data['my_reaction'] = None

        room = chat_room_for_users(session['username'], to_user)
        socketio.emit('new_message', msg_data, room=room)

        if to_user in active_users:
            socketio.emit('message_notification', {'from': session['username'], 'from_nickname': session['nickname'], 'message': message_text[:50]},
                         room=active_users[to_user]['sid'])
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/delete_message', methods=['POST'])
@login_required
def api_delete_message():
    data = request.json
    message_id = data.get('message_id')
    if not message_id:
        return jsonify({'success': False}), 400
    
    owner = db_get_message_owner(message_id)
    if not owner:
        return jsonify({'success': False}), 404
    
    username = session['username']
    is_sender = (owner['from_user'] == username)
    
    db_delete_message(message_id, username, is_sender)
    
    room = chat_room_for_users(owner['from_user'], owner['to_user'])
    socketio.emit('message_deleted', {'message_id': message_id, 'deleted_by': username}, room=room)
    return jsonify({'success': True})

# ========== SOCKETIO ==========
@socketio.on('connect')
def handle_connect():
    if 'username' in session:
        active_users[session['username']] = {'sid': request.sid, 'room': None}
        emit('user_list_update', get_user_list(), broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    if 'username' in session:
        active_users.pop(session['username'], None)
        emit('user_list_update', get_user_list(), broadcast=True)

@socketio.on('join_chat')
def handle_join_chat(data):
    current_user = session['username']
    other_user = data['other_user']
    room = chat_room_for_users(current_user, other_user)
    
    if active_users[current_user]['room']:
        leave_room(active_users[current_user]['room'])
    
    join_room(room)
    active_users[current_user]['room'] = room
    emit('chat_history', {'history': db_get_history(current_user, other_user), 'other_user': other_user})

@socketio.on('add_reaction')
def handle_add_reaction(data):
    message_id = data.get('message_id')
    emoji = data.get('emoji', '').strip()
    if not message_id or not emoji:
        return
    db_add_reaction(message_id, session['username'], emoji)
    
    owner = db_get_message_owner(message_id)
    if owner:
        room = chat_room_for_users(owner['from_user'], owner['to_user'])
        payload = get_reaction_payload(message_id, session['username'])
        socketio.emit('reaction_update', {'message_id': message_id, 'reactions': payload['reactions'], 'my_reaction': payload['my_reaction']}, room=room)

@socketio.on('remove_reaction')
def handle_remove_reaction(data):
    message_id = data.get('message_id')
    if not message_id:
        return
    db_remove_reaction(message_id, session['username'])
    
    owner = db_get_message_owner(message_id)
    if owner:
        room = chat_room_for_users(owner['from_user'], owner['to_user'])
        payload = get_reaction_payload(message_id, session['username'])
        socketio.emit('reaction_update', {'message_id': message_id, 'reactions': payload['reactions'], 'my_reaction': payload['my_reaction']}, room=room)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print("=" * 50)
    print("👖 Trousers Messenger запущен!")
    print(f"📍 http://127.0.0.1:{port}")
    print("=" * 50)
    socketio.run(app, debug=False, host='0.0.0.0', port=port)