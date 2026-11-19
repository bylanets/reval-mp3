import os
import io
import re
import time
import uuid
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, abort, session

# FFmpeg yolunu otomatik tespit ve entegre etme
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception as e:
    print(f"[UYARI] static-ffmpeg başlatılamadı: {e}")

import yt_dlp

app = Flask(__name__)
app.config['SECRET_KEY'] = 'reval-mp3-secret-key-2026'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
DB_PATH = os.path.join(BASE_DIR, 'database.db')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'reval2026')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS visitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            ip TEXT NOT NULL,
            user_agent TEXT,
            first_visit TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            conversions_count INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            safe_title TEXT NOT NULL,
            thumbnail TEXT,
            duration TEXT,
            uploader TEXT,
            size TEXT,
            media_type TEXT DEFAULT 'MP3',
            quality TEXT DEFAULT '320 kbps',
            username TEXT,
            ip TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            ip TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    
    # Kolon var mı kontrolü (Geriye dönük uyumluluk)
    cursor.execute('PRAGMA table_info(conversions)')
    cols = [c[1] for c in cursor.fetchall()]
    if 'media_type' not in cols:
        cursor.execute("ALTER TABLE conversions ADD COLUMN media_type TEXT DEFAULT 'MP3'")
    if 'quality' not in cols:
        cursor.execute("ALTER TABLE conversions ADD COLUMN quality TEXT DEFAULT '320 kbps'")

    conn.commit()
    conn.close()

init_db()

def get_client_ip():
    if request.headers.get('CF-Connecting-IP'):
        return request.headers.get('CF-Connecting-IP')
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or "127.0.0.1"

def sanitize_filename(filename):
    clean = re.sub(r'[\\/*?:"<>|]', "", filename)
    clean = clean.strip()
    return clean if clean else "media"

def format_duration(seconds):
    if not seconds:
        return "Bilinmiyor"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

def format_size(bytes_size):
    if not bytes_size:
        return "Bilinmiyor"
    mb = bytes_size / (1024 * 1024)
    return f"{mb:.2f} MB"

def cleanup_expired_files(max_age_seconds=86400):
    now = time.time()
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, fname)
            if os.path.isfile(fpath):
                file_age = now - os.path.getmtime(fpath)
                if file_age > max_age_seconds:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
    except Exception as e:
        app.logger.warning(f"Temizlik hatası: {e}")

# ================= ANA SAYFA =================
@app.route('/')
def index():
    cleanup_expired_files()
    return render_template('index.html')

# ================= KULLANICI İSİM & IP KAYDI =================
@app.route('/api/register_user', methods=['POST'])
def register_user():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'İsim boş olamaz.'}), 400
    
    username = username[:25]
    session['username'] = username
    
    ip = get_client_ip()
    user_agent = request.headers.get('User-Agent', '')[:200]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM visitors WHERE ip = ? AND username = ?', (ip, username))
    row = cursor.fetchone()
    if row:
        cursor.execute('UPDATE visitors SET last_seen = ?, user_agent = ? WHERE id = ?', (now_str, user_agent, row['id']))
    else:
        cursor.execute('''
            INSERT INTO visitors (username, ip, user_agent, first_visit, last_seen, conversions_count)
            VALUES (?, ?, ?, ?, ?, 0)
        ''', (username, ip, user_agent, now_str, now_str))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'username': username})

# ================= DÖNÜŞTÜRME (MP3 & MP4 DESTEKLİ) =================
@app.route('/convert', methods=['POST'])
def convert_video():
    cleanup_expired_files()
    
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    username = data.get('username', '').strip() or session.get('username', 'Misafir')
    media_format = (data.get('format', 'mp3') or 'mp3').lower()
    quality = str(data.get('quality', '320')).strip()

    if not url:
        return jsonify({'success': False, 'error': 'Lütfen geçerli bir YouTube video linki girin.'}), 400
    
    clean_url = re.sub(r'([?&])list=[^&]+', '', url)
    clean_url = re.sub(r'([?&])index=[^&]+', '', clean_url)
    clean_url = clean_url.rstrip('?&')

    youtube_regex = r'^(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/.+$'
    if not re.match(youtube_regex, clean_url):
        return jsonify({'success': False, 'error': 'Girdiğiniz link geçerli bir YouTube bağlantısı değil.'}), 400

    file_id = uuid.uuid4().hex[:12]
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    # Temel ayarlar & Bot aşma
    common_opts = {
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'skip': ['dash', 'hls']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
        }
    }

    if media_format == 'mp4':
        # Video Kalitesi Seçimi: 1080, 720, 480, 360
        valid_heights = ['1080', '720', '480', '360']
        target_height = quality if quality in valid_heights else '720'
        quality_label = f"{target_height}p HD" if int(target_height) >= 720 else f"{target_height}p"
        
        ydl_opts = dict(common_opts)
        ydl_opts.update({
            'format': f'bestvideo[height<={target_height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]/best',
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }]
        })
        expected_ext = 'mp4'
    else:
        # MP3 Kalitesi Seçimi: 320, 256, 192, 128 kbps
        valid_mp3 = ['320', '256', '192', '128']
        target_quality = quality if quality in valid_mp3 else '320'
        quality_label = f"{target_quality} kbps"

        ydl_opts = dict(common_opts)
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': target_quality,
            }],
        })
        expected_ext = 'mp3'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            
            raw_title = info.get('title', 'YouTube Media')
            safe_title = sanitize_filename(raw_title)
            thumbnail = info.get('thumbnail', '')
            duration = format_duration(info.get('duration'))
            uploader = info.get('uploader', 'Bilinmeyen Kanal')
            
            # Üretilen dosyanın yolunu bul
            expected_file = os.path.join(DOWNLOAD_DIR, f"{file_id}.{expected_ext}")
            
            # Eğer beklenen uzantı tam oturmadıysa klasörde file_id ile başlayan dosyayı bul
            if not os.path.exists(expected_file):
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(file_id):
                        expected_file = os.path.join(DOWNLOAD_DIR, f)
                        expected_ext = f.split('.')[-1]
                        break
            
            if not os.path.exists(expected_file):
                return jsonify({'success': False, 'error': 'Dönüştürme işlemi tamamlanamadı veya dosya bulunamadı.'}), 500
            
            file_size_str = format_size(os.path.getsize(expected_file))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ip = get_client_ip()

            # Veritabanına kaydet
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO conversions (file_id, title, safe_title, thumbnail, duration, uploader, size, media_type, quality, username, ip, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (file_id, raw_title, safe_title, thumbnail, duration, uploader, file_size_str, expected_ext.upper(), quality_label, username, ip, now_str))
            
            cursor.execute('''
                UPDATE visitors SET conversions_count = conversions_count + 1, last_seen = ?
                WHERE username = ? OR ip = ?
            ''', (now_str, username, ip))
            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'file_id': file_id,
                'title': raw_title,
                'safe_title': safe_title,
                'thumbnail': thumbnail,
                'duration': duration,
                'uploader': uploader,
                'size': file_size_str,
                'media_type': expected_ext.upper(),
                'quality': quality_label,
                'download_url': f"/download/{file_id}"
            })

    except yt_dlp.utils.DownloadError as de:
        error_msg = str(de)
        clean_err = error_msg.split('ERROR:')[-1].strip()
        return jsonify({'success': False, 'error': f"İndirme hatası: {clean_err}"}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f"Beklenmeyen bir hata oluştu: {str(e)}"}), 500

# ================= İNDİRME & STREAM =================
@app.route('/download/<file_id>')
def download_file(file_id):
    if not re.match(r'^[a-zA-Z0-9]+$', file_id):
        abort(400, "Geçersiz dosya kimliği.")

    # Dosyayı bul (mp3 veya mp4)
    file_path = None
    file_ext = 'mp3'
    for ext in ['mp3', 'mp4', 'm4a', 'webm']:
        check_p = os.path.join(DOWNLOAD_DIR, f"{file_id}.{ext}")
        if os.path.exists(check_p):
            file_path = check_p
            file_ext = ext
            break

    if not file_path:
        return render_template('index.html', download_error="Bu dosyanın süresi dolmuş veya dosya bulunamadı."), 404

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT safe_title, media_type FROM conversions WHERE file_id = ?', (file_id,))
    row = cursor.fetchone()
    conn.close()

    download_name = f"{row['safe_title'] if row else file_id}.{file_ext}"
    mimetype = 'video/mp4' if file_ext == 'mp4' else 'audio/mpeg'

    return send_file(
        file_path,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype
    )

# ================= EN SON DÖNÜŞTÜRÜLEN 3 PARÇA =================
@app.route('/api/recent', methods=['GET'])
def get_recent_conversions():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT file_id, title, thumbnail, duration, uploader, size, media_type, quality, username, created_at
        FROM conversions
        ORDER BY id DESC
        LIMIT 3
    ''')
    rows = cursor.fetchall()
    conn.close()

    items = []
    for r in rows:
        mtype = r['media_type'] or 'MP3'
        items.append({
            'file_id': r['file_id'],
            'title': r['title'],
            'thumbnail': r['thumbnail'],
            'duration': r['duration'],
            'uploader': r['uploader'],
            'size': r['size'],
            'media_type': mtype,
            'quality': r['quality'] or ('320 kbps' if mtype == 'MP3' else '720p'),
            'username': r['username'] or 'Anonim',
            'created_at': r['created_at'],
            'download_url': f"/download/{r['file_id']}"
        })
    return jsonify({'success': True, 'recent': items})

# ================= YORUMLAR =================
@app.route('/api/comments', methods=['GET'])
def get_comments():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, username, text, created_at FROM comments ORDER BY id DESC LIMIT 50')
    rows = cursor.fetchall()
    conn.close()

    comments = [{
        'id': r['id'],
        'username': r['username'],
        'text': r['text'],
        'created_at': r['created_at']
    } for r in rows]

    return jsonify({'success': True, 'comments': comments})

@app.route('/api/comments', methods=['POST'])
def add_comment():
    data = request.get_json(silent=True) or {}
    text = data.get('text', '').strip()
    username = data.get('username', '').strip() or session.get('username', '').strip()
    
    if not username or username.lower() == 'anonim':
        return jsonify({'success': False, 'error': 'Yorum yapabilmek için lütfen önce isminizi belirleyin.'}), 403

    if not text:
        return jsonify({'success': False, 'error': 'Yorum metni boş olamaz.'}), 400
    
    if len(text) > 500:
        return jsonify({'success': False, 'error': 'Yorum en fazla 500 karakter olabilir.'}), 400

    ip = get_client_ip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO comments (username, text, ip, created_at)
        VALUES (?, ?, ?, ?)
    ''', (username, text, ip, now_str))
    conn.commit()
    comment_id = cursor.lastrowid
    conn.close()

    return jsonify({
        'success': True,
        'comment': {
            'id': comment_id,
            'username': username,
            'text': text,
            'created_at': now_str
        }
    })

# ================= KURUCU PANELİ =================
@app.route('/kurucu')
def kurucu_panel():
    return render_template('kurucu.html')

@app.route('/api/kurucu/login', methods=['POST'])
def kurucu_login():
    data = request.get_json(silent=True) or {}
    password = data.get('password', '')
    if password == ADMIN_PASSWORD:
        session['is_admin'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Geçersiz kurucu şifresi!'}), 401

@app.route('/api/kurucu/data', methods=['GET'])
def kurucu_data():
    if not session.get('is_admin'):
        return jsonify({'success': False, 'error': 'Yetkisiz erişim.'}), 403

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM visitors ORDER BY id DESC')
    visitors = [dict(r) for r in cursor.fetchall()]

    cursor.execute('SELECT * FROM conversions ORDER BY id DESC')
    conversions = [dict(r) for r in cursor.fetchall()]

    cursor.execute('SELECT * FROM comments ORDER BY id DESC')
    comments = [dict(r) for r in cursor.fetchall()]

    stats = {
        'total_visitors': len(visitors),
        'total_conversions': len(conversions),
        'total_comments': len(comments)
    }

    conn.close()
    return jsonify({
        'success': True,
        'stats': stats,
        'visitors': visitors,
        'conversions': conversions,
        'comments': comments
    })

@app.route('/api/kurucu/delete_comment', methods=['POST'])
def delete_comment():
    if not session.get('is_admin'):
        return jsonify({'success': False, 'error': 'Yetkisiz erişim.'}), 403

    data = request.get_json(silent=True) or {}
    cid = data.get('id')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM comments WHERE id = ?', (cid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 60)
    print("  ★ REVAL DÖNÜŞTÜRÜCÜ (MP3 & MP4) BAŞLATILIYOR ★")
    print(f"  Erişim Adresi: http://0.0.0.0:{port}")
    print(f"  Gizli Kurucu Paneli: http://0.0.0.0:{port}/kurucu")
    print("=" * 60)
    app.run(host='0.0.0.0', port=port, debug=False)