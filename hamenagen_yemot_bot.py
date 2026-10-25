# -*- coding: utf-8 -*-
import os
import re
import json
import time
import glob
import threading
import shutil
import subprocess
import wave
import requests
from bs4 import BeautifulSoup
from flask import Flask

# ================= הגדרות =================
# חשוב: אל תשאירו טוקן אמיתי בקוד! שימו אותו כמשתנה סביבה בפלטפורמה.
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")
EXTENSION_PATH = os.environ.get("EXTENSION_PATH", "ivr2:/300")

# יעדים נוספים (שלוחה אחרת / מערכת ימות אחרת), כמשתנה סביבה בפורמט JSON, למשל:
# YEMOT_EXTRA_TARGETS='[{"name":"second","path":"ivr2:/400"},
#                       {"name":"other_system","token":"0123456789:1234","path":"ivr2:/1"}]'
# בלי "token" - משתמשים בטוקן הראשי. "name" חייב להיות ייחודי.
YEMOT_EXTRA_TARGETS = os.environ.get("YEMOT_EXTRA_TARGETS", "[]")

# שלוחה (או כמה) למנהל: כשהעלאה נכשלת, יועלה לשם קובץ עדכון (הקראה) שמפרט מה קרה.
# JSON, למשל (בלי "token" - הטוקן הראשי; כדי להפנות למערכת הנוספת שימו את הטוקן שלה):
# ADMIN_NOTIFY_TARGETS='[{"name":"admin","token":"0123456789:1234","path":"ivr2:/900"}]'
ADMIN_NOTIFY_TARGETS = os.environ.get("ADMIN_NOTIFY_TARGETS", "[]")

WP_API_BASE = "https://hamenagen.net/wp-json/wp/v2"
# קטגוריות לסריקה (slug מופרדים בפסיק). שירים חדשים: music-news.
# הבוט מעלה רק פוסטים מהקטגוריות האלה. הוא לא סורק את עמוד/קטגוריית האלבומים.
CATEGORY_SLUGS = [x.strip() for x in os.environ.get("CATEGORY_SLUGS", "music-news").split(",") if x.strip()]
CHECK_INTERVAL = 60                 # בודק כל דקה
# כמה פוסטים אחרונים לסרוק בכל בדיקה.
POSTS_PER_CHECK = int(os.environ.get("POSTS_PER_CHECK", "30"))

# קובץ העדכונים: שומר לכל פוסט ולכל יעד כמה שירים כבר עלו.
# כדי שיישמר בין הפעלות מחדש - הפנו אותו לדיסק קבוע (למשל /data/state.json).
STATE_FILE = os.environ.get("STATE_FILE", "state_hamenagen.json")
LAST_ID_FILE = "last_id_hamenagen.txt"   # קובץ ישן - רק למעבר חלק

MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
TEXT_CHUNK_MAX_CHARS = 1529         # אורך מקסימלי לקובץ TTS אחד (טקסט ארוך מתחלק לכמה קבצים)
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "1800"))  # שניות - מאפשר שירים ארוכים/מחרוזות
# 0 = מעלים כל שיר כקובץ אחד שלם. אם ימות מסרבת לקבצים גדולים מאוד,
# אפשר להגדיר למשל 600 כדי לחלק לחלקים של 10 דקות שמועלים ברצף.
MAX_PART_SECONDS = int(os.environ.get("MAX_PART_SECONDS", "0"))
# ביטוי רגולרי אופציונלי להתעלמות מקבצי שמע של האתר (ג'ינגלים וכד')
MEDIA_EXCLUDE_REGEX = os.environ.get("MEDIA_EXCLUDE_REGEX", "")

# פרטי השיר (והעדכונים למנהל) מועלים לימות כקובץ שמע מוכן שהבוט מפיק בעצמו.
# TTS_MODE: "audio" = קובץ שמע מוכן (ברירת מחדל), "text" = קובץ טקסט .tts שימות מקריאה (כמו קודם).
TTS_MODE = os.environ.get("TTS_MODE", "audio").lower()
# TTS_ENGINE: "edge" (איכותי יותר, ברירת מחדל) או "gtts". אם המנוע הראשון נכשל - עוברים לשני.
TTS_ENGINE = os.environ.get("TTS_ENGINE", "edge").lower()
# קול בעברית: he-IL-AvriNeural (גבר) או he-IL-HilaNeural (אישה)
TTS_VOICE = os.environ.get("TTS_VOICE", "he-IL-AvriNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")   # מהירות דיבור, למשל "-10%" לאיטי יותר

SKIP = 10 ** 6  # סימון "לא להעלות את הפוסט הזה ליעד הזה"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/html, */*'
}

app = Flask(__name__)


@app.route('/')
def home():
    return "Hamenagen -> Yemot Bot is running!"


COOKIES_SECRET_FILE = "/etc/secrets/cookies.txt"  # Secret File ב-Render
COOKIES_WORK_FILE = "yt_cookies.txt"


# ================= יעדים =================
def load_targets():
    targets = []
    if YEMOT_TOKEN:
        targets.append({"name": "main", "token": YEMOT_TOKEN, "path": EXTENSION_PATH})
    try:
        extra = json.loads(YEMOT_EXTRA_TARGETS or "[]")
    except Exception as e:
        print(f"[-] YEMOT_EXTRA_TARGETS אינו JSON תקין: {e}")
        extra = []
    names = {t["name"] for t in targets}
    for i, t in enumerate(extra, 1):
        token = t.get("token") or YEMOT_TOKEN
        path = t.get("path")
        name = t.get("name") or f"extra{i}"
        if not token or not path or name in names:
            print(f"[-] יעד נוסף #{i} לא תקין או כפול, מדלג.")
            continue
        names.add(name)
        targets.append({"name": name, "token": token, "path": path})
    return targets


TARGETS = load_targets()


def load_admin_targets():
    out = []
    try:
        raw = json.loads(ADMIN_NOTIFY_TARGETS or "[]")
    except Exception as e:
        print(f"[-] ADMIN_NOTIFY_TARGETS אינו JSON תקין: {e}")
        raw = []
    for i, t in enumerate(raw, 1):
        token = t.get("token") or YEMOT_TOKEN
        path = t.get("path")
        if token and path:
            out.append({"name": t.get("name") or f"admin{i}", "token": token, "path": path})
    return out


ADMIN_TARGETS = load_admin_targets()


# ================= קובץ עדכונים (state) =================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("posts", {})
        s.setdefault("targets_seen", [])
        s.setdefault("notified", {})
        return s
    except Exception:
        return {"posts": {}, "targets_seen": [], "notified": {}}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[-] שגיאה בשמירת קובץ העדכונים: {e}")


def read_legacy_id():
    try:
        with open(LAST_ID_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def seed_new_targets(state, posts):
    """יעד חדש (או הפעלה ראשונה) - מעלים רק את הפוסט הכי חדש,
    ומסמנים את הישנים כ'לא להעלות' כדי שלא יוצפו כל השירים הקודמים."""
    legacy = read_legacy_id()
    newest = str(posts[0]['id'])
    for t in TARGETS:
        name = t["name"]
        if name in state["targets_seen"]:
            continue
        for p in posts:
            pid = str(p['id'])
            if pid != newest or pid == legacy:
                entry = state["posts"].setdefault(pid, {"total": 0, "targets": {}})
                entry["targets"].setdefault(name, SKIP)
        state["targets_seen"].append(name)
    save_state(state)


def target_pending(state, pid, name):
    e = state["posts"].get(pid)
    if e is None:
        return True
    return e["targets"].get(name, 0) < e.get("total", 1)


# ================= עזרים: ffmpeg / WAV =================
def get_ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def convert_to_wav(src_path, out_path):
    """ממיר ל-WAV שימות המשיח מנגנת: מונו, 8000Hz, 16bit PCM (ללא וידאו)."""
    try:
        subprocess.run([get_ffmpeg_exe(), "-y", "-i", src_path, "-vn",
                        "-ac", "1", "-ar", "8000", "-acodec", "pcm_s16le",
                        out_path],
                       check=True, capture_output=True)
        return out_path
    except Exception as e:
        print(f"[-] שגיאה בהמרה ל-WAV: {e}")
        return None


def is_valid_yemot_wav(path):
    try:
        with wave.open(path, "rb") as w:
            return (w.getnchannels() == 1 and w.getframerate() == 8000
                    and w.getsampwidth() == 2 and w.getnframes() > 0)
    except Exception as e:
        print(f"[-] הקובץ אינו WAV תקין: {e}")
        return False


def split_wav_if_needed(wav_path):
    """אם הוגדר MAX_PART_SECONDS והשיר ארוך ממנו - מחלק לחלקים. אחרת מחזיר את הקובץ השלם."""
    if MAX_PART_SECONDS <= 0:
        return [wav_path]
    try:
        with wave.open(wav_path, "rb") as w:
            duration = w.getnframes() / float(w.getframerate())
        if duration <= MAX_PART_SECONDS:
            return [wav_path]
        base = wav_path[:-4]
        subprocess.run([get_ffmpeg_exe(), "-y", "-i", wav_path, "-f", "segment",
                        "-segment_time", str(MAX_PART_SECONDS), "-c", "copy",
                        f"{base}_part%03d.wav"],
                       check=True, capture_output=True)
        parts = sorted(glob.glob(f"{base}_part*.wav"))
        if parts:
            os.remove(wav_path)
            print(f"[+] שיר באורך {int(duration)} שניות חולק ל-{len(parts)} חלקים")
            return parts
    except Exception as e:
        print(f"[-] שגיאה בחלוקת השיר, מעלה כקובץ שלם: {e}")
    return [wav_path]


def cleanup_temp(base):
    for p in glob.glob(f"{base}.*") + glob.glob(f"{base}_*"):
        try:
            os.remove(p)
        except Exception:
            pass


# ================= ימות המשיח =================
def get_next_free_number(target):
    """(מספר פנוי הבא, רוחב ספרות) בשלוחה של היעד, או None בשגיאה."""
    try:
        r = requests.get("https://www.call2all.co.il/ym/api/GetIVR2Dir",
                         params={"token": target["token"], "path": target["path"]},
                         timeout=20)
        data = r.json()
        best, width = -1, 3
        for f in data.get("files", []):
            m = re.match(r'^(\d+)\.', f.get("name", ""))
            if m:
                n = int(m.group(1))
                if n > best:
                    best, width = n, len(m.group(1))
        if best < 0:
            return 0, 3
        return best + 1, width
    except Exception as e:
        print(f"[-] [{target['name']}] שגיאה בקריאת רשימת הקבצים בשלוחה: {e}")
        return None


def _response_ok(response):
    try:
        return response.json().get("responseStatus", "OK") == "OK"
    except Exception:
        return response.ok


def upload_audio_to_yemot(target, file_path, file_name):
    if not file_path.lower().endswith(".wav"):
        print(f"[-] חסום: שמע מועלה רק כ-WAV ({file_path})")
        return False
    url = "https://www.call2all.co.il/ym/api/UploadFile"
    params = {"token": target["token"], "path": f"{target['path']}/{file_name}"}
    size_mb = os.path.getsize(file_path) / 1e6
    timeout = 120 + int(size_mb * 20)   # קובץ גדול (שיר ארוך) מקבל יותר זמן
    for attempt in range(1, 4):
        try:
            with open(file_path, 'rb') as f:
                response = requests.post(url, data=params, files={'file': f}, timeout=timeout)
            print(f"[+] [{target['name']}] העלאת שמע {file_name}: {response.text}")
            if _response_ok(response):
                return True
        except Exception as e:
            print(f"[-] [{target['name']}] שגיאה בהעלאת שמע (ניסיון {attempt}): {e}")
        time.sleep(3)
    return False


def upload_text_to_yemot(target, text_content, file_name):
    if not file_name.lower().endswith(".tts"):
        print(f"[-] חסום: טקסט מועלה רק כ-TTS ({file_name})")
        return False
    url = "https://www.call2all.co.il/ym/api/UploadTextFile"
    params = {"token": target["token"], "what": f"{target['path']}/{file_name}",
              "contents": text_content}
    for attempt in range(1, 4):
        try:
            response = requests.post(url, data=params, timeout=30)
            print(f"[+] [{target['name']}] העלאת טקסט {file_name}: {response.text}")
            if _response_ok(response):
                return True
        except Exception as e:
            print(f"[-] [{target['name']}] שגיאה בהעלאת טקסט (ניסיון {attempt}): {e}")
        time.sleep(2)
    return False


# ================= הפקת קובץ שמע מהטקסט (במקום קובץ TTS של ימות) =================
def _tts_edge(text, mp3_path):
    import asyncio
    import edge_tts

    async def _run():
        await edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE).save(mp3_path)

    asyncio.run(_run())


def _tts_gtts(text, mp3_path):
    from gtts import gTTS
    try:
        gTTS(text=text, lang="iw").save(mp3_path)
    except Exception:
        gTTS(text=text, lang="he").save(mp3_path)


def synthesize_chunk(text, mp3_path):
    engines = [("edge", _tts_edge), ("gtts", _tts_gtts)]
    if TTS_ENGINE == "gtts":
        engines.reverse()
    for name, fn in engines:
        try:
            if os.path.exists(mp3_path):
                os.remove(mp3_path)
            fn(text, mp3_path)
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
                return True
        except Exception as e:
            print(f"[-] הפקת קול ({name}) נכשלה: {e}")
    return False


def concat_wavs(paths, out_path):
    with wave.open(out_path, "wb") as wo:
        for k, p in enumerate(paths):
            with wave.open(p, "rb") as wi:
                if k == 0:
                    wo.setparams(wi.getparams())
                wo.writeframes(wi.readframes(wi.getnframes()))


def synthesize_to_wav(text, out_wav_path):
    """טקסט -> קובץ WAV מוכן לימות (מונו, 8kHz, 16bit). טקסט ארוך מופק בחלקים ומחובר לקובץ אחד.
    מחזיר את הנתיב, או None אם ההפקה נכשלה (ואז חוזרים להעלאת טקסט רגיל)."""
    base = out_wav_path[:-4]
    temp = []
    try:
        wavs = []
        for i, chunk in enumerate(split_text(text, 1500)):
            mp3, wav = f"{base}_s{i}.mp3", f"{base}_s{i}.wav"
            temp += [mp3, wav]
            if not synthesize_chunk(chunk, mp3):
                return None
            if not convert_to_wav(mp3, wav):
                return None
            wavs.append(wav)
        if not wavs:
            return None
        concat_wavs(wavs, out_wav_path)
        return out_wav_path if is_valid_yemot_wav(out_wav_path) else None
    except Exception as e:
        print(f"[-] שגיאה בהפקת קובץ שמע מהטקסט: {e}")
        return None
    finally:
        for p in temp:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def upload_track_to_target(target, wav_parts, desc_wav, text_chunks):
    """מעלה ליעד: קובצי השיר ברצף, ואחריהם פרטי השיר - כקובץ שמע מוכן (desc_wav),
    ואם לא הופק - כקובצי טקסט להקראה (text_chunks)."""
    nxt = get_next_free_number(target)
    if nxt is None:
        return False, "לא ניתן היה לקרוא את רשימת הקבצים בשלוחה"
    n, width = nxt
    for part in wav_parts:
        if not upload_audio_to_yemot(target, part, f"{str(n).zfill(width)}.wav"):
            return False, "העלאת קובץ השמע נכשלה"
        n += 1
    if desc_wav:
        if not upload_audio_to_yemot(target, desc_wav, f"{str(n).zfill(width)}.wav"):
            return False, "העלאת קובץ השמע של פרטי השיר נכשלה"
    else:
        for chunk in text_chunks:
            if not upload_text_to_yemot(target, chunk, f"{str(n).zfill(width)}.tts"):
                return False, "העלאת קובץ ההקראה נכשלה"
            n += 1
    return True, ""


def notify_admin(state, key, message):
    """מעלה קובץ עדכון לשלוחת המנהל (שמע מוכן, ואם ההפקה נכשלה - טקסט להקראה).
    כל עדכון נשלח פעם אחת בלבד (לפי key), גם אחרי הפעלה מחדש, כדי שהמנהל לא יוצף."""
    if not ADMIN_TARGETS:
        print(f"[!] אין שלוחת מנהל מוגדרת (ADMIN_NOTIFY_TARGETS). תקלה: {message}")
        return
    if key in state["notified"]:
        return
    text = message[:TEXT_CHUNK_MAX_CHARS]
    base = f"temp_notify_{int(time.time())}"
    wav = synthesize_to_wav(text, f"{base}.wav") if TTS_MODE == "audio" else None
    sent = False
    try:
        for t in ADMIN_TARGETS:
            nxt = get_next_free_number(t)
            if nxt is None:
                continue
            n, width = nxt
            if wav:
                ok = upload_audio_to_yemot(t, wav, f"{str(n).zfill(width)}.wav")
            else:
                ok = upload_text_to_yemot(t, text, f"{str(n).zfill(width)}.tts")
            sent = sent or ok
    finally:
        for p in glob.glob(f"{base}*"):
            try:
                os.remove(p)
            except Exception:
                pass
    if sent:
        state["notified"][key] = int(time.time())
        if len(state["notified"]) > 500:
            for k in sorted(state["notified"], key=state["notified"].get)[:100]:
                state["notified"].pop(k, None)
        save_state(state)


# ================= וורדפרס =================
def get_category_id(slug):
    try:
        r = requests.get(f"{WP_API_BASE}/categories", params={"slug": slug}, timeout=15)
        data = r.json()
        if data:
            return data[0]["id"]
    except Exception as e:
        print(f"[-] שגיאה במציאת קטגוריה: {e}")
    return None


# ================= איתור מדיה =================
AUDIO_EXT = r'(?:mp3|m4a|aac|wav|ogg|opus|flac)'
MEDIA_EXT = r'(?:mp3|m4a|aac|wav|ogg|opus|flac|mp4|webm)'
_URL_BODY = r'https?://[^\s"\'<>\\]+?\.'
_URL_TAIL = r'(?![A-Za-z0-9])(?:\?[^\s"\'<>\\]*)?'
DIRECT_AUDIO_RE = re.compile(_URL_BODY + AUDIO_EXT + _URL_TAIL, re.I)
DIRECT_MEDIA_RE = re.compile(_URL_BODY + MEDIA_EXT + _URL_TAIL, re.I)

YT_ID_RE = re.compile(
    r'(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:[^"\'\s<>]*?&)?v=|shorts/)|youtu\.be/)'
    r'([\w-]{11})(?![\w-])')
PLAYLIST_RE = re.compile(r'youtube(?:-nocookie)?\.com/[^\s"\'<>]*?[?&]list=([\w-]{13,})')

# באתר, שם קובץ התמונה הראשית מתחיל במזהה היוטיוב של השיר
IMAGE_YT_PATTERN = r'/([\w-]{11})-(?:maxresdefault|sddefault|hqdefault|mqdefault|default)'


def normalize_text(text):
    return text.replace('\\/', '/').replace('&amp;', '&').replace('&#038;', '&')


def find_direct(text, regex):
    """כל הקישורים הישירים לקבצי מדיה, לפי סדר הופעה, בלי כפילויות."""
    seen, out = set(), []
    for m in regex.finditer(text):
        url = m.group(0)
        key = url.split("?")[0]
        if key in seen:
            continue
        if MEDIA_EXCLUDE_REGEX and re.search(MEDIA_EXCLUDE_REGEX, url):
            continue
        seen.add(key)
        out.append(url)
    return out


def find_youtube_ids(text):
    seen, out = set(), []
    for m in YT_ID_RE.finditer(text):
        vid = m.group(1)
        if vid == "videoseries" or vid in seen:
            continue
        seen.add(vid)
        out.append(vid)
    return out


def find_playlist_ids(text):
    seen, out = set(), []
    for m in PLAYLIST_RE.finditer(text):
        lid = m.group(1)
        if lid.startswith("RD") or lid in seen:   # RD = "מיקס" אוטומטי, לא אלבום
            continue
        seen.add(lid)
        out.append(lid)
    return out


def youtube_url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


def get_featured_image_url(post):
    try:
        media = post.get('_embedded', {}).get('wp:featuredmedia', [])
        if media and media[0].get('source_url'):
            return media[0]['source_url']
    except Exception:
        pass
    link = post.get('link')
    if link:
        try:
            r = requests.get(link, headers=HEADERS, timeout=20)
            soup = BeautifulSoup(r.text, 'html.parser')
            tag = soup.find('meta', property='og:image')
            if tag and tag.get('content'):
                return tag['content']
        except Exception as e:
            print(f"[-] שגיאה בטעינת דף הפוסט: {e}")
    return None


def youtube_url_from_image(image_url):
    if not image_url:
        return None
    m = re.search(IMAGE_YT_PATTERN, image_url)
    return youtube_url(m.group(1)) if m else None


def find_page_audio(post):
    """קבצי שמע בדף הפוסט: קודם בתוך אזור הכתבה עצמה (כדי לא לתפוס שירים של פוסטים אחרים
    מהסרגל הצדדי), ואם אין - הקובץ הראשון בכל הדף (כמו קודם)."""
    link = post.get('link')
    if not link:
        return [], None
    try:
        r = requests.get(link, headers=HEADERS, timeout=20)
        html = r.text
    except Exception as e:
        print(f"[-] שגיאה בטעינת דף הפוסט: {e}")
        return [], None
    soup = BeautifulSoup(html, 'html.parser')
    region = soup.find('article') or soup.find('main')
    region_urls = find_direct(normalize_text(str(region)), DIRECT_AUDIO_RE) if region else []
    full_urls = find_direct(normalize_text(html), DIRECT_AUDIO_RE)
    print(f"[debug] קבצי שמע באזור הכתבה: {len(region_urls)}, בכל הדף: {len(full_urls)}")
    for u in (region_urls or full_urls)[:10]:
        print(f"[debug]   {u}")
    return region_urls, (full_urls[0] if full_urls else None)


# --- yt-dlp ---
def prepare_cookies_file():
    """יוטיוב חוסם שרתי ענן. קובץ cookies של חשבון מחובר עוזר לעקוף את זה."""
    try:
        if os.path.exists(COOKIES_SECRET_FILE):
            shutil.copyfile(COOKIES_SECRET_FILE, COOKIES_WORK_FILE)
            return COOKIES_WORK_FILE
        env_cookies = os.environ.get("YOUTUBE_COOKIES", "")
        if env_cookies.strip():
            with open(COOKIES_WORK_FILE, "w", encoding="utf-8") as f:
                f.write(env_cookies)
            return COOKIES_WORK_FILE
    except Exception as e:
        print(f"[-] שגיאה בהכנת קובץ cookies: {e}")
    return None


def is_youtube_url(url):
    return "youtube.com" in url or "youtu.be" in url


def yt_dlp_base_cmd():
    cmd = ["yt-dlp"]
    try:
        import imageio_ffmpeg
        cmd += ["--ffmpeg-location", imageio_ffmpeg.get_ffmpeg_exe()]
    except Exception:
        pass
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]
    cookies = prepare_cookies_file()
    if cookies:
        cmd += ["--cookies", cookies]
    else:
        print("[!] לא הוגדרו cookies של יוטיוב - ייתכן שההורדה תיחסם.")
    return cmd


def expand_youtube_playlist(list_id):
    """פלייליסט ביוטיוב שמוטמע בפוסט -> רשימת שירים [{url, title}] לפי הסדר."""
    cmd = yt_dlp_base_cmd() + ["--flat-playlist", "--dump-single-json",
                               f"https://www.youtube.com/playlist?list={list_id}"]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180).stdout
        data = json.loads(out)
        tracks = []
        for e in data.get("entries") or []:
            vid = e.get("id")
            if vid:
                tracks.append({"url": youtube_url(vid), "title": e.get("title")})
        print(f"[+] נמצא פלייליסט ביוטיוב ({list_id}) עם {len(tracks)} שירים")
        return tracks
    except Exception as e:
        print(f"[-] שגיאה בפתיחת פלייליסט {list_id}: {e}")
        return []


def download_direct(url, out_path_no_ext):
    ext = os.path.splitext(url.split("?")[0])[1].lower() or ".bin"
    raw_path = f"{out_path_no_ext}_raw{ext}"
    mp3_path = f"{out_path_no_ext}.mp3"
    try:
        headers = dict(HEADERS, Referer="https://hamenagen.net/")
        with requests.get(url, headers=headers, stream=True, timeout=(30, 120)) as r:
            r.raise_for_status()
            with open(raw_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        if ext == ".mp3":
            os.replace(raw_path, mp3_path)
        else:
            subprocess.run([get_ffmpeg_exe(), "-y", "-i", raw_path, "-vn",
                            "-acodec", "libmp3lame", "-q:a", "2", mp3_path],
                           check=True, capture_output=True)
            os.remove(raw_path)
        return mp3_path
    except Exception as e:
        print(f"[-] שגיאה בהורדת קובץ ישיר: {e}")
        if os.path.exists(raw_path):
            os.remove(raw_path)
        return None


def download_audio(media_url, out_path_no_ext):
    if not media_url.startswith(("http://", "https://")):
        return None
    if not is_youtube_url(media_url):
        return download_direct(media_url, out_path_no_ext)
    cmd = yt_dlp_base_cmd() + ["-x", "--audio-format", "mp3", "--no-playlist",
                               "-o", f"{out_path_no_ext}.%(ext)s", media_url]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
        mp3_path = f"{out_path_no_ext}.mp3"
        if os.path.exists(mp3_path):
            return mp3_path
    except subprocess.TimeoutExpired:
        print(f"[-] yt-dlp לא הסתיים תוך {DOWNLOAD_TIMEOUT} שניות - בוטל.")
    except subprocess.CalledProcessError as e:
        print(f"[-] שגיאה בהורדת שמע: {e.stderr}")
    return None


def download_track(track, base):
    for url in [track["url"]] + track.get("fallbacks", []):
        path = download_audio(url, base)
        if path:
            return path
    return None


def find_tracks(post):
    """מחזיר רשימת שירים [{url, title, fallbacks}] מתוך הפוסט עצמו. ברוב המקרים שיר אחד.
    כמה שירים רק אם הפוסט עצמו מכיל פלייליסט יוטיוב / כמה קבצי שמע / כמה סרטוני יוטיוב.
    סדר עדיפויות: פלייליסט > קבצי שמע ישירים > נגן הדף > סרטוני יוטיוב בתוכן > גיבויים."""
    content = normalize_text(post['content']['rendered'])
    blob = normalize_text(json.dumps(post, ensure_ascii=False))

    # 1. פלייליסט יוטיוב בתוך הפוסט
    for lid in find_playlist_ids(content):
        tracks = expand_youtube_playlist(lid)
        if tracks:
            return tracks

    yt_ids = find_youtube_ids(content)

    def with_fallbacks(urls):
        tracks = [{"url": u, "title": None, "fallbacks": []} for u in urls]
        if len(tracks) == 1 and not is_youtube_url(tracks[0]["url"]):
            fb = [youtube_url(v) for v in yt_ids]
            img = youtube_url_from_image(get_featured_image_url(post))
            if img and img not in fb:
                fb.append(img)
            tracks[0]["fallbacks"] = fb
        return tracks

    # 2. קבצים ישירים בתוכן / בשדות ה-API
    direct = find_direct(content, DIRECT_MEDIA_RE) or find_direct(blob, DIRECT_MEDIA_RE)
    if direct:
        print(f"[+] נמצאו {len(direct)} קבצי מדיה ישירים בפוסט")
        return with_fallbacks(direct)

    # 3. נגן האתר בדף הפוסט
    region_urls, first_full = find_page_audio(post)
    if region_urls:
        print(f"[+] נמצאו {len(region_urls)} קבצי שמע בדף הפוסט")
        return with_fallbacks(region_urls)

    # 4. סרטוני יוטיוב בתוכן הפוסט
    if yt_ids:
        print(f"[+] נמצאו {len(yt_ids)} סרטוני יוטיוב בפוסט")
        return [{"url": youtube_url(v), "title": None, "fallbacks": []} for v in yt_ids]

    # 5. גיבוי: הקובץ הראשון בכל הדף
    if first_full:
        return with_fallbacks([first_full])

    # 6. גיבוי אחרון: מזהה יוטיוב לפי שם התמונה הראשית
    img = youtube_url_from_image(get_featured_image_url(post))
    if img:
        return [{"url": img, "title": None, "fallbacks": []}]

    return []


# ================= ניקוי טקסט להקראה (TTS) =================
def clean_html_text(html):
    if not html:
        return ""
    text = BeautifulSoup(html, 'html.parser').get_text(separator='\n')
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


REMOVE_CREDITS = False  # False = הקרדיטים (מילים, לחן, צילום וכו') נשארים בהקראה

JUNK_LINES = {
    "פרסומת", "דילוג על הפרסומת", "יחצ", "יח\"צ", "גרסת שמע", "גרסת שמע:",
    "הגב", "שתף", "קרא עוד", "קראו עוד", "כתבות נוספות בנושא", "לצפייה בקליפ",
    "לצפיה בקליפ", "לשמיעה", "להאזנה",
}
JUNK_PREFIXES = ["גרסת שמע", "כתבות נוספות"]
if REMOVE_CREDITS:
    JUNK_PREFIXES += ["קרדיטים", "קרדיט", "צילום:", "יחסי ציבור", "יחצ"]

URL_RE = re.compile(r'https?://\S+|www\.\S+', re.I)
SYMBOLS_RE = re.compile(r'[|•·*_#~^<>\[\]{}\\/=+@]|[\U0001F000-\U0001FFFF\u2600-\u27BF]')


def clean_tts_text(text):
    out = []
    for line in (text or "").splitlines():
        line = URL_RE.sub("", line)
        line = line.replace("|", ",")
        line = SYMBOLS_RE.sub(" ", line)
        line = re.sub(r'\s+', ' ', line).strip()
        if not line:
            continue
        bare = line.strip(" :.-–—")
        if bare in JUNK_LINES or line in JUNK_LINES:
            continue
        if any(line.startswith(pref) for pref in JUNK_PREFIXES):
            continue
        if not re.search(r'[A-Za-z\u0590-\u05FF]', line):
            continue
        out.append(line)
    return "\n".join(out)


INTRO = "מיוזיקליק בקו התוכן הישראלי"


def build_song_description(title, content_html, excerpt_html="", track_title=None):
    """הטקסט המלא להקראה (בלי חיתוך - החלוקה לקבצים נעשית ב-split_text)."""
    body = clean_tts_text(clean_html_text(content_html) or clean_html_text(excerpt_html))
    title = clean_tts_text(title)
    parts = [INTRO, title]
    if track_title:
        tt = clean_tts_text(track_title)
        if tt and tt != title:
            parts.append(tt)
    if body:
        parts.append(body)
    return "\n".join(p for p in parts if p)


def split_text(text, limit=TEXT_CHUNK_MAX_CHARS):
    """מחלק טקסט ארוך לכמה קבצי TTS (בגבולות שורות/מילים), במקום לחתוך אותו."""
    chunks, cur = [], ""
    for line in text.splitlines():
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            piece, line = line[:cut], line[cut:].lstrip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece)
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


# ================= העלאת שירים =================
def upload_tracks(pid, label, tracks, pending, state, text_for):
    """מעלה רשימת שירים לפי הסדר ליעדים שעדיין חסרים להם. מחזיר True אם כולם קיבלו הכל."""
    total = len(tracks)
    entry = state["posts"].setdefault(pid, {"total": total, "targets": {}})
    entry["total"] = total

    for i, track in enumerate(tracks):
        # רק יעדים שהגיעו בדיוק לשיר הזה (כך הסדר נשמר ולא נוצרות כפילויות)
        need = [t for t in pending if entry["targets"].get(t["name"], 0) == i]
        if not need:
            continue

        base = f"temp_{pid}_{i}"
        try:
            mp3_path = download_track(track, base)
            if not mp3_path:
                print(f"[-] נכשל בהורדת שיר {i + 1}/{total} של {pid}.")
                notify_admin(state, f"{pid}:{i}:download",
                             f"עדכון למנהל. הורדת השיר {track.get('title') or label}, "
                             f"שיר {i + 1} מתוך {total}, נכשלה, ולכן הוא לא הועלה. הבוט ימשיך לנסות.")
                continue

            wav_path = convert_to_wav(mp3_path, f"{base}.wav")
            if os.path.exists(mp3_path):
                os.remove(mp3_path)
            if not wav_path or not is_valid_yemot_wav(wav_path):
                print(f"[-] הפורמט לא מתאים, לא מעלה שיר {i + 1}/{total} של {pid}.")
                notify_admin(state, f"{pid}:{i}:format",
                             f"עדכון למנהל. ההמרה של השיר {track.get('title') or label}, "
                             f"שיר {i + 1} מתוך {total}, נכשלה, ולכן הוא לא הועלה.")
                continue

            wav_parts = split_wav_if_needed(wav_path)
            text = text_for(i, track, total)
            text_chunks = split_text(text)

            # פרטי השיר כקובץ שמע מוכן (ואם ההפקה נכשלה - חוזרים לקובץ טקסט להקראה)
            desc_wav = None
            if TTS_MODE == "audio":
                desc_wav = synthesize_to_wav(text, f"{base}_desc.wav")
                if not desc_wav:
                    print("[!] הפקת קובץ שמע לפרטי השיר נכשלה, מעלה כטקסט להקראה.")

            for t in need:
                ok, reason = upload_track_to_target(t, wav_parts, desc_wav, text_chunks)
                if ok:
                    entry["targets"][t["name"]] = i + 1
                    save_state(state)
                else:
                    print(f"[-] [{t['name']}] ההעלאה של שיר {i + 1}/{total} נכשלה: {reason}")
                    notify_admin(state, f"{pid}:{i}:{t['name']}:upload",
                                 f"עדכון למנהל. השיר {track.get('title') or label}, שיר {i + 1} מתוך {total}, "
                                 f"לא הועלה לשלוחה {t['path']} ביעד {t['name']}. סיבה: {reason}. "
                                 f"הבוט ימשיך לנסות.")
        finally:
            cleanup_temp(base)

    return all(entry["targets"].get(t["name"], 0) >= total for t in pending)


# ================= עיבוד פוסט =================
def process_post(post, pending, state):
    """מעבד פוסט (שיר בודד, או פוסט עם כמה קבצי מדיה) ומעלה ליעדים שחסרים להם."""
    pid = str(post['id'])
    post_title = clean_html_text(post['title']['rendered'])
    print(f"[*] מעבד פוסט: {post_title} ({pid}) | יעדים: {[t['name'] for t in pending]}")

    tracks = find_tracks(post)
    if not tracks:
        print(f"[-] לא נמצאה מדיה בפוסט {pid}.")
        notify_admin(state, f"{pid}:nomedia",
                     f"עדכון למנהל. בפוסט {post_title} לא נמצא קובץ שמע, ולכן השיר לא הועלה לאף שלוחה.")
        return False
    if len(tracks) > 1:
        print(f"[+] נמצאו {len(tracks)} קבצי מדיה בפוסט")

    content_html = post['content']['rendered']
    excerpt_html = post.get('excerpt', {}).get('rendered', '')

    def text_for(i, track, total):
        track_title = track.get("title") or (post_title if total == 1 else f"{post_title} {i + 1}")
        if i == 0:
            return build_song_description(post_title, content_html, excerpt_html,
                                          track_title if total > 1 else None)
        return "\n".join(filter(None, [clean_tts_text(post_title),
                                       f"שיר {i + 1} מתוך {total}",
                                       clean_tts_text(track_title)]))

    return upload_tracks(pid, post_title, tracks, pending, state, text_for)


# ================= לולאת הבוט =================
def run_bot():
    print("--- הבוט הופעל ברקע (מול hamenagen.net) ---")
    print(f"[+] יעדים: {[(t['name'], t['path']) for t in TARGETS]}")
    category_ids = []
    for slug in CATEGORY_SLUGS:
        cid = get_category_id(slug)
        if cid:
            print(f"[+] נמצאה קטגוריה '{slug}' עם מזהה {cid}")
            category_ids.append(cid)
        else:
            print(f"[!] לא נמצאה קטגוריה '{slug}'.")
    if not category_ids:
        print("[!] לא נמצאו קטגוריות, ימשוך פוסטים מכל הקטגוריות.")

    state = load_state()
    attempts = {}

    def give_up_check(pid, title, pending):
        attempts[pid] = attempts.get(pid, 0) + 1
        if attempts[pid] >= MAX_ATTEMPTS:
            names = ", ".join(t["name"] for t in pending if target_pending(state, pid, t["name"]))
            notify_admin(state, f"{pid}:giveup",
                         f"עדכון למנהל. הבוט הפסיק לנסות להעלות את {title} "
                         f"אחרי {MAX_ATTEMPTS} ניסיונות. יעדים שחסרים: {names}. נדרש טיפול ידני.")

    while True:
        try:
            print("מושך נתונים מה-API של hamenagen...")
            params = {"per_page": POSTS_PER_CHECK, "orderby": "date", "order": "desc",
                      "_embed": "wp:featuredmedia"}
            if category_ids:
                params["categories"] = ",".join(str(c) for c in category_ids)
            response = requests.get(f"{WP_API_BASE}/posts", headers=HEADERS, params=params, timeout=20)

            if response.status_code == 200:
                posts = response.json()
                if posts:
                    seed_new_targets(state, posts)
                    worked = False

                    # מהישן לחדש, כדי לא לפספס פוסטים שעלו בין סריקות
                    for post in reversed(posts):
                        pid = str(post['id'])
                        pending = [t for t in TARGETS if target_pending(state, pid, t["name"])]
                        if not pending:
                            continue
                        if attempts.get(pid, 0) >= MAX_ATTEMPTS:
                            print(f"פוסט {pid} נכשל {MAX_ATTEMPTS} פעמים, מדלג עליו.")
                            continue
                        worked = True
                        print(f"!!! פוסט להעלאה: {pid} !!!")
                        if not process_post(post, pending, state):
                            give_up_check(pid, clean_html_text(post['title']['rendered']), pending)
                    if not worked:
                        print("אין פוסטים חדשים.")
                else:
                    print("ה-API לא החזיר פוסטים.")
            else:
                print(f"שגיאה בגישה ל-API. קוד תגובה: {response.status_code}")
        except Exception as e:
            print(f"שגיאה בסריקת ה-API: {e}")

        time.sleep(CHECK_INTERVAL)


# ================= הפעלת האפליקציה =================
if __name__ == '__main__':
    if not TARGETS:
        print("[!] אזהרה: לא הוגדר אף יעד (YEMOT_TOKEN חסר)!")
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
