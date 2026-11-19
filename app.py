import os
import io
import re
import time
import uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, abort

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
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# İndirme meta verilerini tutacak bellek sözlüğü
# { file_id: { 'title': '...', 'size': '...', 'created_at': 17... } }
DOWNLOAD_REGISTRY = {}

def sanitize_filename(filename):
    """Windows ve dosya sistemi için geçersiz karakterleri temizler."""
    clean = re.sub(r'[\\/*?:"<>|]', "", filename)
    clean = clean.strip()
    return clean if clean else "audio"

def format_duration(seconds):
    """Saniye cinsinden süreyi MM:SS veya HH:MM:SS formatına çevirir."""
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
    """Bayt boyutunu MB veya KB cinsine çevirir."""
    if not bytes_size:
        return "Bilinmiyor"
    mb = bytes_size / (1024 * 1024)
    return f"{mb:.2f} MB"

def cleanup_expired_files(max_age_seconds=1800):
    """Belirtilen süreden (30 dk) eski kalmış dosyaları diskten siler."""
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
        app.logger.warning(f"Temizlik sırasında hata: {e}")

@app.route('/')
def index():
    cleanup_expired_files()
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert_video():
    cleanup_expired_files()
    
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'success': False, 'error': 'Lütfen geçerli bir YouTube video linki girin.'}), 400
    
    # YouTube URL basit doğrulama
    youtube_regex = r'^(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/.+$'
    if not re.match(youtube_regex, url):
        return jsonify({'success': False, 'error': 'Girdiğiniz link geçerli bir YouTube bağlantısı değil.'}), 400

    file_id = uuid.uuid4().hex[:12]
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Video bilgilerini al ve dönüştür
            info = ydl.extract_info(url, download=True)
            
            raw_title = info.get('title', 'YouTube Audio')
            safe_title = sanitize_filename(raw_title)
            thumbnail = info.get('thumbnail', '')
            duration = format_duration(info.get('duration'))
            uploader = info.get('uploader', 'Bilinmeyen Kanal')
            
            # Üretilen MP3 dosyasının varlığını ve boyutunu doğrula
            expected_file = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp3")
            if not os.path.exists(expected_file):
                return jsonify({'success': False, 'error': 'MP3 dönüştürme işlemi tamamlanamadı veya dosya bulunamadı.'}), 500
            
            file_size_bytes = os.path.getsize(expected_file)
            file_size_str = format_size(file_size_bytes)

            # İndirme kaydını belleğe al
            DOWNLOAD_REGISTRY[file_id] = {
                'title': safe_title,
                'raw_title': raw_title,
                'created_at': time.time()
            }

            return jsonify({
                'success': True,
                'file_id': file_id,
                'title': raw_title,
                'safe_title': safe_title,
                'thumbnail': thumbnail,
                'duration': duration,
                'uploader': uploader,
                'size': file_size_str,
                'quality': '320 kbps (En Yüksek Kalite)'
            })

    except yt_dlp.utils.DownloadError as de:
        error_msg = str(de)
        if "Private video" in error_msg:
            msg = "Bu video gizli olduğu için indirilemiyor."
        elif "Video unavailable" in error_msg:
            msg = "Video bulunamadı veya kullanılamıyor."
        elif "Sign in to confirm your age" in error_msg:
            msg = "Bu video yaş kısıtlamalı olduğu için oturum gerektiriyor."
        else:
            msg = f"YouTube indirme hatası: {error_msg.split('ERROR:')[-1].strip()}"
        return jsonify({'success': False, 'error': msg}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f"Beklenmeyen bir hata oluştu: {str(e)}"}), 500

@app.route('/download/<file_id>')
def download_file(file_id):
    """
    MP3 dosyasını kullanıcıya teslim eder ve indirme bittiğinde
    geçici dosyayı downloads klasöründen otomatik olarak siler.
    """
    # Güvenlik kontrolü
    if not re.match(r'^[a-zA-Z0-9]+$', file_id):
        abort(400, "Geçersiz dosya kimliği.")

    file_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp3")
    
    if not os.path.exists(file_path):
        return render_template('index.html', download_error="İndirme bağlantısının süresi dolmuş veya dosya zaten indirilmiş."), 404

    # Meta veriden dosya adını al
    meta = DOWNLOAD_REGISTRY.pop(file_id, {})
    download_filename = f"{meta.get('title', 'reval_audio')}.mp3"

    try:
        # Dosyayı RAM belleğine oku
        with open(file_path, 'rb') as f:
            file_data = io.BytesIO(f.read())

        # Diskteki geçici dosyayı indirme anında anında sil!
        try:
            os.remove(file_path)
            print(f"[TEMİZLİK] Geçici dosya başarıyla silindi: {file_id}.mp3")
        except Exception as err:
            print(f"[UYARI] Geçici dosya silinemedi: {err}")

        # Kullanıcıya MP3 olarak indir
        return send_file(
            file_data,
            as_attachment=True,
            download_name=download_filename,
            mimetype='audio/mpeg'
        )
    except Exception as e:
        return jsonify({'error': f"Dosya sunulurken hata: {str(e)}"}), 500

if __name__ == '__main__':
    print("=" * 60)
    print("  ★ REVAL MP3 DÖNÜŞTÜRÜCÜ BAŞLATILIYOR ★")
    print("  Erişim Adresi: http://127.0.0.1:5000")
    print("=" * 60)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)