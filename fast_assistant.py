#!/usr/bin/env python3
"""
fast_assistant (v27) — быстрая переработка v26: только Google Gemini Live API.

Что изменилось ради скорости:
- Убрана вторая модель ("Gemma") и её панель — ответ один, стримится сразу.
- Убран loopback системного звука — только микрофон (меньше шума для VAD и CPU).
- Убраны Interview Mode и Health Monitor.
- Push-to-Talk (по умолчанию): держишь ПРОБЕЛ — ассистент слушает (распознавание
  идёт в реальном времени, текст появляется в "Слышу" по мере речи);
  отпустил ПРОБЕЛ — отправляется activityEnd и ответ генерируется МГНОВЕННО,
  без ожидания паузы (VAD-задержки 500-800мс больше нет).
  Плюс: можно делать паузы внутри вопроса — сервер не перебьёт на полу-вопросе.
- MODE = "auto": прежнее поведение (слушает всегда, ПРОБЕЛ = заглушить,
  F9 = ответить немедленно), VAD ускорен: 800мс -> 500мс, чувствительность HIGH.
- Модели: авто-fallback gemini-3.8-live -> gemini-3.1-flash-live-preview
  (thinking=minimal) -> старый native-audio preview из v26.
- Session resumption переделан под новый SDK (SessionResumptionConfig.handle).
- Компактный system prompt: ответы 2-3 предложения, сразу суть.
- Индикатор скорости реакции: "⚡ X.Xs" — время от отпускания пробела
  до первого слова ответа.

Горячие клавиши:
  ПРОБЕЛ (PTT)  — держать = говорить
  ПРОБЕЛ (auto) — заглушить микрофон пока зажат
  F9   (auto)   — форсировать ответ, не дожидаясь паузы

Install: pip install pyaudiowpatch sounddevice google-genai numpy pynput
"""

import asyncio
import threading
import queue
import time
import tkinter as tk
from tkinter import scrolledtext
import numpy as np
import logging
import os
import sys
import faulthandler
faulthandler.enable()  # следы нативных падений (pyaudio и пр.) в stderr

from pynput import keyboard

LOG_PATH = os.path.join(os.path.dirname(__file__) or '.', "fast_assistant.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_exception


def handle_thread_exception(args):
    logging.error(f"Thread exception: {args.exc_type.__name__}: {args.exc_value}",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


threading.excepthook = handle_thread_exception

try:
    import pyaudiowpatch as pyaudio
    HAVE_WASAPI = True
except ImportError:
    import pyaudio
    HAVE_WASAPI = False

try:
    import sounddevice as sd
    HAVE_SOUNDDEVICE = True
except ImportError:
    sd = None
    HAVE_SOUNDDEVICE = False

from google import genai
from google.genai import types

# ============================================
# CONFIG
# ============================================
def _load_api_keys():
    """Ключ из assistant_secrets.py (не в git) или из переменных окружения."""
    try:
        from assistant_secrets import API_KEY_GEMINI as gemini_key
    except ImportError:
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
    return gemini_key


API_KEY_GEMINI = _load_api_keys()

# Порядок: первая модель, которая подключится, и используется.
# Второй элемент — thinking_level (None = не задавать).
MODEL_CANDIDATES = [
    ("models/gemini-3.8-live", None),                       # актуальная low-latency Live-модель
    ("models/gemini-3.1-flash-live-preview", "minimal"),    # legacy, thinking=minimal для скорости
    ("models/gemini-2.5-flash-native-audio-preview-12-2025", None),  # рабочая модель из v26
]

MODE = "ptt"        # "ptt" = пробел держать=говорить | "auto" = слушает всегда, VAD
# Как работает пробел в MODE="ptt":
#  "manual" — строгий режим (по умолчанию): реплика = ровно от нажатия до отпускания
#             пробела, сервер ничего не режет и не решает сам. Транскрипция приходит
#             после отпускания. + кольцевой буфер 1с до нажатия (начало не теряется).
#  "vad"    — пока пробел зажат, звук идёт на сервер, серверный VAD сам ловит конец
#             речи (пауза 500мс): транскрипция видна в реальном времени, НО длинные
#             вопросы с паузами могут резаться на несколько реплик.
PTT_STYLE = "manual"
CHUNK_DURATION = 0.1  # 100мс — проверено в v26

# Прокси для обхода региональной блокировки Gemini API (SSH-туннель: plink -D 8081).
# "" = напрямую. Если порт прокси не отвечает, приложение автоматически ходит напрямую.
PROXY = "socks5://127.0.0.1:8081"

# Настройки VAD для MODE="auto" (в PTT не используются)
PREFIX_PADDING_MS = 200   # захват начала речи (v26 было 500 — избыточно)
SILENCE_MS = 500          # пауза до конца реплики (v26 было 800 — главная задержка реакции)

MAX_FONT = 16
MIN_FONT = 5

SYSTEM_PROMPT = """Ты голосовой IT-ассистент (Senior SysAdmin / DevOps / SRE).
Отвечай на русском, КОРОТКО и мгновенно.

ГЛАВНОЕ - КОРОТКО:
- Максимум 2-3 предложения, сразу суть. Без вступлений, приветствий и встречных вопросов.
- Команда консоли / путь / параметр — просто команда + 1 строка пояснения.
- Если не уверен, что расслышал — ответь одной фразой: "Повтори, пожалуйста".
- "Ты меня слышишь?" / "или нет" / проверка связи -> только "Да, слышу".

Отвечай на фактический смысл вопроса. Общий вопрос — общий ответ:
не тяни тему ИИ/"модели"/прочее в DevOps/SRE без явной причины, и наоборот.

Подсказки распознавания IT-терминов на слух:
эсэсэйч/эссаш=SSH, впиэн/вэпээн=VPN, нджинкс=nginx, апаш/апачи=Apache,
кубер/кубернетес=Kubernetes, кабситиэль/кубектл=kubectl, докер=Docker,
постгрес=PostgreSQL, мускуль/майскуэль=MySQL, редис=Redis, монго=MongoDB,
ансибл=Ansible, терраформ=Terraform, дженкинс=Jenkins, гитхаб=GitHub,
гитлаб=GitLab, линукс=Linux, уайршарк=Wireshark, тисипидамп=tcpdump,
системди=systemd, системктл=systemctl, журналктл=journalctl,
айпитэйблс=iptables, лемп/ленамп=LEMP, эйдабэс/эвээс=AWS, джисипи=GCP,
азур=Azure, грэп=grep, сэд=sed, оук=awk, пайтон=Python, бэш=Bash,
гоу=Go, раст=Rust.
Непонятное слово в IT-контексте — считай техническим термином.
"""


# ============================================
# NETWORK: прокси для REST и для websocket Live-сессии
# (websockets не наследует прокси из env в google-genai — патчим явно)
# ============================================
def _proxy_alive(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        port = u.port or (1080 if u.scheme.startswith("socks") else 80)
        import socket
        with socket.create_connection((u.hostname, port), timeout=0.5):
            return True
    except Exception:
        return False


EFFECTIVE_PROXY = ""
if PROXY and _proxy_alive(PROXY):
    EFFECTIVE_PROXY = PROXY
    os.environ.setdefault("HTTP_PROXY", PROXY)
    os.environ.setdefault("HTTPS_PROXY", PROXY)
    import google.genai.live as _live_mod
    _orig_ws_connect = _live_mod.ws_connect

    def _ws_connect_with_proxy(uri, **kw):
        kw.setdefault("proxy", PROXY)
        return _orig_ws_connect(uri, **kw)

    _live_mod.ws_connect = _ws_connect_with_proxy
    logging.info(f"[NET] Прокси активен: {PROXY}")
elif PROXY:
    logging.warning(f"[NET] Прокси {PROXY} не отвечает — прямое соединение")


# ============================================
# AUDIO (только микрофон: pyaudiowpatch -> pyaudio -> sounddevice/WDM-KS)
# ============================================
class AudioCapture:
    def __init__(self, q: queue.Queue):
        self.q = q
        self.running = False
        self.p = None
        self.mic = None
        self.sd_mic = None
        self.mic_backend = None
        self.lock = threading.Lock()
        self.muted = False
        self.mic_buf = []
        self.current_volume = 0.0
        self.rms_max = 0.0  # пиковая громкость за текущую реплику (для диагностики)
        # источник звука: "mic" | "system" (WASAPI loopback — системный звук/конференции)
        self.source = "mic"
        self.loopback = None
        self.lb_buf = []
        self.lb_rate = 0
        self.lb_ch = 1
        self.lb_thread_started = False
        # кольцевой буфер: последняя секунда звука ДО нажатия пробела,
        # чтобы не терялось начало реплики (важно для системного звука в конференциях)
        self._ring = np.array([], dtype=np.int16)
        self._mic_thread = None
        self._lb_thread = None
        self._feed_thread = None

    def _safe_device_name(self, info):
        return str(info.get("name", "unknown")).strip()

    def _log_audio_devices(self):
        try:
            logging.info(f"[AUDIO] PyAudio devices: {self.p.get_device_count()}, HAVE_WASAPI={HAVE_WASAPI}")
            for i in range(self.p.get_device_count()):
                info = self.p.get_device_info_by_index(i)
                logging.info(
                    "[AUDIO] Device %s: name=%r api=%s in=%s out=%s rate=%s loopback=%s",
                    i, self._safe_device_name(info), info.get("hostApi"),
                    info.get("maxInputChannels"), info.get("maxOutputChannels"),
                    info.get("defaultSampleRate"), info.get("isLoopbackDevice", False),
                )
        except Exception as e:
            logging.warning(f"[AUDIO] Failed to list devices: {e}")

    def _candidate_input_devices(self):
        """Сначала устройство по умолчанию, затем все реальные входы (без loopback)."""
        seen = set()
        try:
            info = self.p.get_default_input_device_info()
            seen.add(int(info["index"]))
            yield info
        except Exception as e:
            logging.warning(f"[AUDIO] Default input unavailable: {e}")

        for i in range(self.p.get_device_count()):
            if i in seen:
                continue
            try:
                info = self.p.get_device_info_by_index(i)
                if int(info.get("maxInputChannels", 0)) <= 0:
                    continue
                if info.get("isLoopbackDevice", False):
                    continue
                yield info
            except Exception as e:
                logging.warning(f"[AUDIO] Failed to inspect input device {i}: {e}")

    def _open_mic(self):
        errors = []
        for info in self._candidate_input_devices():
            idx = int(info["index"])
            rate = int(float(info.get("defaultSampleRate") or 16000))
            max_ch = int(info.get("maxInputChannels", 1) or 1)
            channels_to_try = [1] + ([2] if max_ch >= 2 else [])

            for ch in channels_to_try:
                try:
                    stream = self.p.open(
                        format=pyaudio.paInt16, channels=ch, rate=rate,
                        input=True, input_device_index=idx, frames_per_buffer=512,
                    )
                    stream.read(512, exception_on_overflow=False)  # проверка что реально читает
                    self.mic = stream
                    self.mic_backend = "pyaudio"
                    self.mic_rate = rate
                    self.mic_ch = ch
                    logging.info("[AUDIO] Mic opened: index=%s name=%r rate=%s channels=%s",
                                 idx, self._safe_device_name(info), rate, ch)
                    return
                except Exception as e:
                    errors.append(f"idx={idx} ch={ch} name={self._safe_device_name(info)!r}: {e}")
                    try:
                        stream.close()
                    except Exception:
                        pass

        self.mic = None
        self.mic_error = "; ".join(errors[-6:]) if errors else "no input devices"
        logging.error(f"[AUDIO] No working microphone input. Recent errors: {self.mic_error}")

    def _open_mic_sounddevice(self):
        """Fallback через sounddevice/WDM-KS — на целевой машине работает именно он."""
        if not HAVE_SOUNDDEVICE:
            logging.warning("[AUDIO] sounddevice fallback unavailable: module is not installed")
            return

        try:
            devices = sd.query_devices()
        except Exception as e:
            logging.warning(f"[AUDIO] sounddevice query_devices failed: {e}")
            return

        errors = []
        candidate_indexes = [i for i, d in enumerate(devices) if int(d.get('max_input_channels', 0)) > 0]
        candidate_indexes.sort(
            key=lambda i: (0 if 'WDM-KS' in str(sd.query_hostapis()[devices[i]['hostapi']]['name']) else 1, i))

        for idx in candidate_indexes:
            d = devices[idx]
            name = str(d.get('name', 'unknown'))
            hostapi = sd.query_hostapis()[d['hostapi']]['name']
            default_rate = int(float(d.get('default_samplerate') or 44100))
            rates = []
            for r in (48000, default_rate, 44100, 16000):
                if r not in rates:
                    rates.append(r)

            max_input_channels = int(d.get('max_input_channels', 1) or 1)
            channel_options = [2, 1] if max_input_channels >= 2 else [1]

            for rate in rates:
                for channels in channel_options:
                    try:
                        def callback(indata, frames, time_info, status, self=self, channels=channels):
                            if status:
                                logging.debug(f"[AUDIO] sounddevice status: {status}")
                            if self.running:
                                raw = np.asarray(indata, dtype=np.int16)
                                arr = raw.reshape(-1, channels).mean(axis=1).astype(np.int16) \
                                    if channels > 1 else raw.reshape(-1).astype(np.int16)
                                with self.lock:
                                    self.mic_buf.append(arr)

                        stream = sd.InputStream(
                            device=idx, channels=channels, samplerate=rate,
                            dtype='int16', blocksize=1024, callback=callback,
                        )
                        stream.start()
                        time.sleep(0.25)
                        with self.lock:
                            got_audio = len(self.mic_buf) > 0
                        if not got_audio:
                            stream.stop(); stream.close()
                            errors.append(f"idx={idx} hostapi={hostapi} rate={rate} ch={channels} name={name!r}: no callback data")
                            continue

                        self.sd_mic = stream
                        self.mic = stream
                        self.mic_backend = "sounddevice"
                        self.mic_error = None
                        self.mic_rate = rate
                        self.mic_ch = channels
                        logging.info("[AUDIO] Mic opened via sounddevice: index=%s hostapi=%s name=%r rate=%s channels=%s",
                                     idx, hostapi, name, rate, channels)
                        return
                    except Exception as e:
                        errors.append(f"idx={idx} hostapi={hostapi} rate={rate} ch={channels} name={name!r}: {e}")

        logging.error("[AUDIO] sounddevice microphone fallback failed. Recent errors: %s",
                      "; ".join(errors[-8:]))

    def _open_loopback(self):
        """Системный звук через WASAPI loopback (нужен pyaudiowpatch)."""
        if not HAVE_WASAPI:
            logging.error("[AUDIO] pyaudiowpatch не установлен — системный звук недоступен")
            return False
        try:
            wasapi = self.p.get_host_api_info_by_type(pyaudio.paWASAPI)
            spk = self.p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not spk.get("isLoopbackDevice"):
                for lb in self.p.get_loopback_device_info_generator():
                    if spk["name"] in lb["name"]:
                        spk = lb
                        break
            self.lb_rate = int(spk["defaultSampleRate"])
            self.lb_ch = max(1, int(spk["maxInputChannels"]))
            self.loopback = self.p.open(
                format=pyaudio.paInt16, channels=self.lb_ch, rate=self.lb_rate,
                input=True, input_device_index=int(spk["index"]), frames_per_buffer=512,
            )
            logging.info("[AUDIO] Loopback opened: %r rate=%s ch=%s",
                         self._safe_device_name(spk), self.lb_rate, self.lb_ch)
            if not self.lb_thread_started:
                self.lb_thread_started = True
                self._lb_thread = threading.Thread(target=self._read_lb, daemon=True)
                self._lb_thread.start()
            return True
        except Exception as e:
            self.loopback = None
            logging.error(f"[AUDIO] Loopback open failed: {e}", exc_info=True)
            return False

    def _read_lb(self):
        while self.running and self.loopback:
            try:
                data = self.loopback.read(512, exception_on_overflow=False)
                arr = np.frombuffer(data, dtype=np.int16)
                if self.lb_ch > 1:
                    arr = arr.reshape(-1, self.lb_ch).mean(axis=1).astype(np.int16)
                with self.lock:
                    self.lb_buf.append(arr)
            except Exception as e:
                logging.warning(f"[AUDIO] Loopback read stopped: {e}")
                break

    def set_source(self, src):
        """Переключение источника на ходу (микрофон <-> системный звук)."""
        self.source = src
        logging.info(f"[AUDIO] Источник: {src}")
        if src == "system":
            # микрофон на sounddevice/WDM-KS + loopback одновременно роняли процесс (v26) —
            # в режиме системного звука микрофон закрываем, он не нужен
            if self.mic_backend == "sounddevice" and self.sd_mic:
                try:
                    self.sd_mic.stop()
                    self.sd_mic.close()
                except Exception:
                    pass
                self.sd_mic = None
                self.mic = None
                logging.info("[AUDIO] sounddevice-микрофон закрыт для стабильности loopback")
            if self.loopback is None:
                return self._open_loopback()
            return True
        else:
            if self.mic is None:
                self._open_mic()
                if not self.mic:
                    self._open_mic_sounddevice()
                if self.mic and self.mic_backend == "pyaudio":
                    self._mic_thread = threading.Thread(target=self._read_mic, daemon=True)
                    self._mic_thread.start()
            return self.mic is not None

    def start(self):
        self.running = True
        self.mic_error = None
        self.p = pyaudio.PyAudio()
        self._log_audio_devices()

        self._open_mic()
        if not self.mic:
            self._open_mic_sounddevice()
        if not self.mic:
            self.running = False
            return False

        if self.mic_backend == "pyaudio":
            self._mic_thread = threading.Thread(target=self._read_mic, daemon=True)
            self._mic_thread.start()
        self._feed_thread = threading.Thread(target=self._feed, daemon=True)
        self._feed_thread.start()
        return True

    def _resample(self, arr, fr, to):
        if fr == to:
            return arr
        n = int(len(arr) * to / fr)
        return arr[np.linspace(0, len(arr) - 1, n).astype(int)]

    def _read_mic(self):
        while self.running and self.mic:
            try:
                data = self.mic.read(512, exception_on_overflow=False)
                arr = np.frombuffer(data, dtype=np.int16)
                if self.mic_ch > 1:
                    arr = arr.reshape(-1, self.mic_ch).mean(axis=1).astype(np.int16)
                with self.lock:
                    self.mic_buf.append(arr)
            except Exception as e:
                logging.error(f"[AUDIO] Microphone read stopped: {e}", exc_info=True)
                break

    def _feed(self):
        """Собирает чанки 100мс, чистит звук и отправляет в очередь.
        Когда микрофон заглушен — шлём тишину: соединение живо, VAD молчит."""
        chunk = int(16000 * CHUNK_DURATION)
        while self.running:
            time.sleep(CHUNK_DURATION)
            with self.lock:
                if self.source == "system":
                    mc = np.concatenate(self.lb_buf) if self.lb_buf else None
                else:
                    mc = np.concatenate(self.mic_buf) if self.mic_buf else None
                # неиспользуемый буфер сливаем, чтобы не копился
                self.lb_buf = []
                self.mic_buf = []

            if mc is None or len(mc) == 0:
                self.current_volume = max(0.0, self.current_volume - 20)
                self.q.put(np.zeros(chunk, dtype=np.int16).tobytes())
                continue

            dev_rate = self.lb_rate if (self.source == "system" and self.lb_rate) else self.mic_rate

            if self.muted:
                # пробел не зажат: копим последнюю секунду в кольцевой буфер,
                # на сервер шлём тишину (соединение живо, реплика не начата)
                if dev_rate:
                    self._ring = np.concatenate((self._ring, mc))[-int(dev_rate):]
                self.current_volume = max(0.0, self.current_volume - 20)
                self.q.put(np.zeros(chunk, dtype=np.int16).tobytes())
                continue

            # пробел зажат: если есть предыстория — приклеиваем её перед текущим
            # звуком, чтобы начало реплики (нажал чуть позже) не потерялось
            if len(self._ring):
                mc = np.concatenate((self._ring, mc))
                self._ring = np.array([], dtype=np.int16)

            # DSP: убираем DC offset, мягко усиливаем тихую речь (не зануляя её),
            # клипуем и приводим к 16кГц моно. Наработка из v26 — отвечает за то,
            # что речь вообще доходит до распознавания.
            arr = mc.astype(np.float32)
            arr = arr - float(np.mean(arr))
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) else 0.0
            if rms > self.rms_max:
                self.rms_max = rms
            if rms > 0:
                # микрофон на этой машине тихий — потолок усиления 6x вместо 3x
                gain = max(1.0, min(6.0, 1000.0 / rms))
                arr = arr * gain
            arr = np.clip(arr, -28000, 28000).astype(np.int16)
            arr = self._resample(arr, dev_rate, 16000)

            self.current_volume = min(100.0, (rms / 3000.0) * 100.0)
            # короткий чанк дополняем; длинный (с приклеенной предысторией) шлём целиком
            if len(arr) < chunk:
                arr = np.pad(arr, (0, chunk - len(arr)))
            self.q.put(arr.tobytes())

    def stop(self):
        self.running = False
        # ГОНКА: закрывать аудиопотоки можно только после того, как потоки-читатели
        # вышли из read() — иначе нативный крах (тихие падения процесса)
        for t in (self._lb_thread, self._mic_thread, self._feed_thread):
            if t and t.is_alive():
                t.join(timeout=1.5)
        if self.loopback:
            try:
                self.loopback.stop_stream()
                self.loopback.close()
            except Exception:
                pass
            self.loopback = None
        if self.mic and self.mic_backend == "pyaudio":
            try:
                self.mic.stop_stream()
                self.mic.close()
            except Exception:
                pass
        if self.sd_mic:
            try:
                self.sd_mic.stop()
                self.sd_mic.close()
            except Exception:
                pass
        self.mic = None
        self.sd_mic = None
        if self.p:
            try:
                self.p.terminate()
            except Exception:
                pass


# ============================================
# APP
# ============================================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Fast Assistant — Google Live (PTT)")
        self.root.geometry("1000x700+50+50")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1a1a2e")

        # === TOP BAR ===
        top = tk.Frame(self.root, bg="#1a1a2e")
        top.pack(fill=tk.X, padx=10, pady=8)

        self.btn = tk.Button(top, text="CONNECT", command=self.toggle,
                             bg="#28a745", fg="#fff", font=("Consolas", 12, "bold"),
                             relief=tk.FLAT, padx=20, pady=6)
        self.btn.pack(side=tk.LEFT, padx=5)

        tk.Button(top, text="Reset", command=self.reset,
                  bg="#0f3460", fg="#fff", font=("Consolas", 10),
                  relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=5)

        self.status = tk.StringVar(value="Disconnected")
        self.dot = tk.Label(top, text="●", fg="#ff4444", bg="#1a1a2e", font=("Arial", 16))
        self.dot.pack(side=tk.LEFT, padx=5)
        tk.Label(top, textvariable=self.status, fg="#888", bg="#1a1a2e",
                 font=("Consolas", 11)).pack(side=tk.LEFT, padx=5)

        self.latency_lbl = tk.Label(top, text="⚡ --", fg="#ffaa00", bg="#1a1a2e",
                                    font=("Consolas", 12, "bold"))
        self.latency_lbl.pack(side=tk.LEFT, padx=15)

        self.model_lbl = tk.Label(top, text="model: ?", fg="#888", bg="#1a1a2e",
                                  font=("Consolas", 9))
        self.model_lbl.pack(side=tk.LEFT, padx=5)

        self.volume_lbl = tk.Label(top, text="🎤 Vol: 0%", fg="#00aaff", bg="#1a1a2e",
                                   font=("Consolas", 10))
        self.volume_lbl.pack(side=tk.LEFT, padx=10)

        hint = "[Space=держать→говорить, отпустить→ответ]" if MODE == "ptt" \
            else "[Space=Mute] [F9=ответить сейчас]"
        tk.Label(top, text=hint, fg="#ffaa00", bg="#1a1a2e",
                 font=("Consolas", 9, "bold")).pack(side=tk.RIGHT)

        # === ИСТОЧНИК ЗВУКА ===
        srcf = tk.Frame(self.root, bg="#1a1a2e")
        srcf.pack(fill=tk.X, padx=10, pady=(0, 4))
        tk.Label(srcf, text="Источник:", fg="#888", bg="#1a1a2e",
                 font=("Consolas", 10)).pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value="mic")
        for val, txt in (("mic", "🎤 Микрофон"), ("system", "🔊 Система (конференции)")):
            tk.Radiobutton(srcf, text=txt, variable=self.source_var, value=val,
                           command=self._switch_source, fg="#ccc", bg="#1a1a2e",
                           selectcolor="#16213e", activebackground="#1a1a2e",
                           font=("Consolas", 10)).pack(side=tk.LEFT, padx=8)
        self.global_space = tk.BooleanVar(value=False)
        self._global_space = False
        self.global_space.trace_add(
            "write", lambda *a: setattr(self, "_global_space", self.global_space.get()))
        tk.Checkbutton(srcf, text="Пробел в любом окне", variable=self.global_space,
                       fg="#ffaa00", bg="#1a1a2e", selectcolor="#16213e",
                       activebackground="#1a1a2e", font=("Consolas", 10)).pack(side=tk.LEFT, padx=15)

        # === HEARD ===
        tk.Label(self.root, text="Слышу:", fg="#ff9800", bg="#1a1a2e",
                 font=("Consolas", 9, "bold")).pack(anchor=tk.W, padx=10)
        self.heard = scrolledtext.ScrolledText(self.root, height=2, bg="#16213e",
                                               fg="#ff9800", font=("Consolas", 11),
                                               wrap=tk.WORD, borderwidth=0)
        self.heard.pack(fill=tk.X, padx=10, pady=2)
        self.heard.configure(state=tk.DISABLED)  # только чтение: без каретки и правки

        # === ANSWERS: две чередующиеся панели, прошлый ответ не перекрывается ===
        tk.Label(self.root, text="Ответы (чередуются строка 1 / строка 2):", fg="#00ff88",
                 bg="#1a1a2e", font=("Consolas", 9, "bold")).pack(anchor=tk.W, padx=10)

        self.p1 = self._panel("ОТВЕТ [1]", "#1a3a1a")
        self.p2 = self._panel("ОТВЕТ [2]", "#1a2a3a")

        # State
        self.current_row = 1
        self.q = queue.Queue()
        self.audio = None
        self.session = None
        self.connected = False
        self.loop = None
        self.response = ""
        self.heard_final = ""
        self.should_reconnect = False

        # session resumption (новый SDK: handle вместо session_id)
        self.resume_handle = None
        self.working_model_idx = None

        # turn-стейт для стриминга и замера скорости реакции
        self._turn_first_text = False
        self._turn_started_at = None
        self._turn_heard_speech = False
        self._awaiting_speech_check = False

        # PTT
        self.ptt_held = False
        self.window_focused = False  # пробел работает только при фокусе на окне
        self._manual_activity = (MODE == "ptt" and PTT_STYLE == "manual")
        self._ptt_released_at = None

        # поток цикла переподключения (чтобы не плодить второй при повторном CONNECT)
        self._loop_thread = None

        self._hotkey()
        self._highlight()
        self._update_volume_display()

        # фокус окна: без него пробел игнорируется (чтобы печатать в других окнах)
        self.root.bind("<FocusIn>", lambda e: self._on_focus(True))
        self.root.bind("<FocusOut>", lambda e: self._on_focus(False))
        # фиксируем закрытие окна пользователем (отличаем от падения процесса)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_focus(self, focused: bool):
        if focused == self.window_focused:
            return  # событие всплывает от каждого виджета — берём только смену состояния
        was = self.window_focused
        self.window_focused = focused
        if focused:
            logging.info("[UI] Окно в фокусе — PTT активен")
            if self.connected and not self.ptt_held:
                self._set_status("ГОТОВ — держи ПРОБЕЛ" if MODE == "ptt" else "LISTENING", "#00ff88")
        else:
            logging.info("[UI] Окно потеряло фокус — PTT отключён")
            # если пробел зажат в момент потери фокуса — корректно завершаем реплику
            if self.ptt_held:
                self._ptt_end()
            elif self.connected and not self._global_space:
                self._set_status("⏸ не в фокусе", "#888888")

    def _panel(self, title, bg):
        f = tk.Frame(self.root, bg="#1a1a2e")
        f.pack(fill=tk.BOTH, expand=True, padx=10, pady=3)
        lbl = tk.Label(f, text=title, fg="#666", bg="#1a1a2e", font=("Consolas", 10, "bold"))
        lbl.pack(anchor=tk.W)
        txt = scrolledtext.ScrolledText(f, bg=bg, fg="#00ff88", font=("Consolas", MAX_FONT),
                                        wrap=tk.WORD, borderwidth=1)
        txt.pack(fill=tk.BOTH, expand=True)
        # только чтение: каретки и правки нет, выделение мышью и копирование работают
        txt.configure(state=tk.DISABLED)
        return {"lbl": lbl, "txt": txt}

    def _set_text(self, widget, text):
        """Запись в read-only виджет (временно включаем правку)."""
        widget.configure(state=tk.NORMAL)
        widget.delete(1.0, tk.END)
        widget.insert(tk.END, text)
        widget.configure(state=tk.DISABLED)

    def _clear_text(self, widget):
        widget.configure(state=tk.NORMAL)
        widget.delete(1.0, tk.END)
        widget.configure(state=tk.DISABLED)

    # ---------- UI helpers ----------
    def _fit(self, widget, text):
        """Автоподбор размера шрифта, чтобы текст влезал целиком."""
        if getattr(self, '_fitting', False):
            return

        self._fitting = True
        widget.configure(state=tk.NORMAL)
        try:
            self.root.update_idletasks()
            w, h = widget.winfo_width(), widget.winfo_height()
            if w < 50 or h < 50:
                widget.configure(font=("Consolas", MIN_FONT))
                widget.delete(1.0, tk.END)
                widget.insert(tk.END, text)
                return

            for sz in range(MAX_FONT, MIN_FONT - 1, -1):
                widget.configure(font=("Consolas", sz))
                widget.delete(1.0, tk.END)
                widget.insert(tk.END, text)
                self.root.update_idletasks()
                yv = widget.yview()
                if yv and len(yv) == 2 and yv[1] >= 0.99:
                    return

            widget.configure(font=("Consolas", MIN_FONT))
            widget.delete(1.0, tk.END)
            widget.insert(tk.END, text)
        except Exception as e:
            logging.warning(f"[FIT] Error: {e}")
        finally:
            widget.configure(state=tk.DISABLED)
            self._fitting = False

    def _highlight(self):
        if self.current_row == 1:
            self.p1["lbl"].config(fg="#00ff88")
            self.p2["lbl"].config(fg="#444")
        else:
            self.p1["lbl"].config(fg="#444")
            self.p2["lbl"].config(fg="#00ff88")

    def _update_volume_display(self):
        try:
            if self.audio and not self.audio.mic and self.audio.source == "mic":
                self.volume_lbl.config(text="🎤 MIC ERROR", fg="#ff4444")
            else:
                vol = int(self.audio.current_volume) if self.audio else 0
                icon = "🔊" if (self.audio and self.audio.source == "system") else "🎤"
                color = "#666666" if vol < 20 else ("#00aaff" if vol < 50 else "#00ff88")
                self.volume_lbl.config(text=f"{icon} Vol: {vol}%", fg=color)

            # если после отпускания пробела за 3с речь так и не распозналась —
            # скорее всего было слишком тихо, подсказываем
            ref_time = self._turn_started_at or self._ptt_released_at
            if (self._awaiting_speech_check and ref_time
                    and time.time() - ref_time > 3.0):
                self._awaiting_speech_check = False
                if not self._turn_heard_speech and not self._turn_first_text and self.connected:
                    logging.warning(f"[PTT] Речь не распознана "
                                    f"(макс RMS={getattr(self.audio, 'rms_max', 0):.0f}) — слишком тихо?")
                    self._set_status("НЕ УСЛЫШАЛ — ГОВОРИ ГРОМЧЕ", "#ff4444")
                elif self._turn_heard_speech:
                    self._awaiting_speech_check = False
        except Exception:
            pass
        finally:
            self.root.after(200, self._update_volume_display)

    def _set_status(self, txt, color):
        self.status.set(txt)
        self.dot.config(fg=color)

    # ---------- hotkeys ----------
    def _switch_source(self):
        src = self.source_var.get()
        if self.audio and self.audio.running:
            if not self.audio.set_source(src):
                self._set_status("СИСТЕМНЫЙ ЗВУК НЕДОСТУПЕН", "#ff4444")
                self.source_var.set("mic")
                return
        name = "МИКРОФОН" if src == "mic" else "СИСТЕМНЫЙ ЗВУК"
        self._set_status(f"ИСТОЧНИК: {name}", "#00ff88" if self.connected else "#888888")
        logging.info(f"[UI] Источник переключён: {src}")

    def _hotkey(self):
        VK_SPACE = 0x20
        listener_ref = {}  # фильтру нужен доступ к listener'у для suppress_event()

        def space_filter(msg, data):
            """Windows: глотаем пробел СИСТЕМНО (suppress_event), когда он
            управляет PTT, чтобы он не печатался в редакторах и чатах.
            Замечание: return False только пропускает колбэк, НЕ блокируя
            клавишу — блокирует именно suppress_event()."""
            suppress = False
            try:
                if MODE != "ptt" or data.vkCode != VK_SPACE:
                    return None
                down = msg in (0x0100, 0x0104)   # WM_KEYDOWN / WM_SYSKEYDOWN
                up = msg in (0x0101, 0x0105)      # WM_KEYUP / WM_SYSKEYUP
                if down:
                    if self.window_focused or self._global_space:
                        self._ptt_start()
                        suppress = True
                elif up and self.ptt_held:
                    self._ptt_end()
                    suppress = True
            except Exception:
                return None
            if suppress:
                lst = listener_ref.get("obj")
                if lst is not None:
                    # бросает SuppressException -> событие не уйдёт другим приложениям
                    lst.suppress_event()
                return False
            return None

        def press(k):
            if k == keyboard.Key.space:
                # пробел глобально — только если включена галка (для конференций);
                # иначе печатать в других окнах можно спокойно
                if not self.window_focused and not self._global_space:
                    return
                if MODE == "ptt":
                    self._ptt_start()  # повторный вызов защищён ptt_held
                else:
                    if self.audio:
                        self.audio.muted = True
                    self.root.after(0, lambda: self._set_status("MUTED", "#ffaa00"))
            elif k == keyboard.Key.f9 and MODE == "auto":
                self.root.after(0, self._force_answer)

        def release(k):
            if k == keyboard.Key.space and MODE == "ptt":
                # отпускание обрабатываем всегда, чтобы реплика не зависла
                self._ptt_end()
            elif k == keyboard.Key.space:
                if self.audio:
                    self.audio.muted = False
                self.root.after(0, lambda: self._set_status("LISTENING", "#00ff88")
                                if self.connected else None)

        try:
            listener = keyboard.Listener(on_press=press, on_release=release,
                                         win32_event_filter=space_filter)
        except TypeError:
            # старый pynput без win32_event_filter — работаем как раньше
            listener = keyboard.Listener(on_press=press, on_release=release)
        listener_ref["obj"] = listener
        listener.start()

    def _send_to_session(self, coro_factory):
        """Безопасно дёрнуть метод сессии из потока клавиатуры/UI."""
        if not (self.session and self.loop and self.connected):
            logging.info("[PTT] игнор: сессия не подключена")
            return
        try:
            asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)
        except Exception as e:
            logging.warning(f"[SESSION] send failed: {e}")

    def _ptt_start(self):
        if self.ptt_held:  # автоповтор клавиши
            return
        if not self.connected:
            self.root.after(0, lambda: self._set_status("НЕТ СВЯЗИ — жди подключения", "#ff4444"))
            return
        self.ptt_held = True
        if self.audio:
            self.audio.muted = False
            self.audio.rms_max = 0.0
        # новый вопрос: чистим стейт
        self.response = ""
        self.heard_final = ""
        self._turn_first_text = False
        self._turn_heard_speech = False
        self._awaiting_speech_check = False
        self.root.after(0, lambda: self._clear_text(self.heard))
        self.root.after(0, lambda: self._set_status("СЛУШАЮ...", "#00ff88"))
        logging.info("[PTT] start: микрофон открыт")
        if self._manual_activity:
            self._send_to_session(
                lambda: self.session.send_realtime_input(activity_start=types.ActivityStart()))

    def _ptt_end(self):
        if not self.ptt_held:
            return
        self.ptt_held = False
        if self.audio:
            self.audio.muted = True
        self._ptt_released_at = time.time()
        self._awaiting_speech_check = True
        logging.info(f"[PTT] end: микрофон закрыт, макс RMS за реплику: "
                     f"{getattr(self.audio, 'rms_max', 0):.0f}")
        if self._manual_activity:
            self._turn_started_at = time.time()  # точка отсчёта скорости реакции
            self._send_to_session(
                lambda: self.session.send_realtime_input(activity_end=types.ActivityEnd()))
        if self._turn_heard_speech:
            self.root.after(0, lambda: self._set_status(
                "ОБДУМЫВАЮ...", "#ffaa00") if self.connected else None)

    def _force_answer(self):
        """F9 (auto-режим): немедленно завершить реплику, не дожидаясь паузы VAD."""
        logging.info("[PTT] Force answer (F9)")
        self._turn_started_at = time.time()
        self._send_to_session(lambda: self.session.send_client_content(turn_complete=True))

    # ---------- ответы ----------
    def _show_heard(self, text):
        self._set_text(self.heard, text)

    def _on_output_text(self, chunk):
        if not self._turn_first_text:
            self._turn_first_text = True
            if self._turn_started_at:
                lat = time.time() - self._turn_started_at
                self.latency_lbl.config(text=f"⚡ {lat:.2f}s")
                logging.info(f"[LATENCY] Реакция: {lat:.2f}s")
            # чистим панель от позапрошлого ответа
            panel = self.p1 if self.current_row == 1 else self.p2
            self._clear_text(panel["txt"])

        self.response += chunk
        panel = self.p1 if self.current_row == 1 else self.p2
        self._fit(panel["txt"], self.response.strip())

    def _on_turn_complete(self):
        answer = self.response.strip()
        logging.info(f"[HEARD] Вопрос: {self.heard_final}")
        logging.info(f"[TURN] Complete, row={self.current_row}, ответ: {answer[:120]}")
        self.response = ""
        self._turn_started_at = None
        self.current_row = 2 if self.current_row == 1 else 1
        self._highlight()
        self.root.after(0, lambda: self._set_status(
            "ГОТОВ — держи ПРОБЕЛ" if MODE == "ptt" else "LISTENING", "#00ff88")
            if self.connected else None)

    # ---------- connection ----------
    def toggle(self):
        if self.connected or self.should_reconnect:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        self._set_status("Connecting...", "#ffaa00")
        self.btn.config(text="DISCONNECT", bg="#dc3545")
        self.should_reconnect = True

        # не плодим второй цикл, если прежний ещё жив; после disconnect даём ему
        # секунду выйти, иначе connect после disconnect мог застрять
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=1.0)
        if self._loop_thread and self._loop_thread.is_alive():
            logging.info("[SESSION] Цикл переподключения уже работает")
            return

        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=lambda: self.loop.run_until_complete(self._run()), daemon=True)
        self._loop_thread.start()

    async def _run(self):
        """Переподключение при ошибках/GoAway."""
        while self.should_reconnect:
            try:
                await self._session_loop()
            except Exception as e:
                logging.error(f"[SESSION] Error: {e}")
                if self.should_reconnect:
                    logging.info("[SESSION] Reconnecting in 2 seconds...")
                    self.root.after(0, lambda: self._set_status("Reconnecting...", "#ffaa00"))
                    await asyncio.sleep(2)
                else:
                    break

        self.connected = False
        self.root.after(0, lambda: self.btn.config(text="CONNECT", bg="#28a745"))

    def _build_config(self, thinking_level):
        def thinking_cfg():
            if thinking_level is None:
                return None
            try:
                return types.ThinkingConfig(thinking_level=thinking_level)
            except Exception as e:
                logging.warning(f"[SESSION] thinking_level не поддержан: {e}")
                return None

        if self._manual_activity:
            # strict-PTT: начало/конец реплики шлём сами (activityStart/End);
            # при disabled=True API запрещает задавать поля чувствительности
            aad = types.AutomaticActivityDetection(disabled=True)
        else:
            # авто-VAD (и PTT-стиль "vad"): пауза 500мс — быстрый конец реплики,
            # транскрипция стримится во время речи
            aad = types.AutomaticActivityDetection(
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=PREFIX_PADDING_MS,
                silence_duration_ms=SILENCE_MS,
            )
        rt = types.RealtimeInputConfig(automatic_activity_detection=aad)

        kwargs = dict(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
                )
            ),
            realtime_input_config=rt,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=types.Content(parts=[types.Part(text=SYSTEM_PROMPT)]),
        )
        tc = thinking_cfg()
        if tc is not None:
            kwargs["thinking_config"] = tc
        # resumption: новый SDK — handle в конфиге, kwarg session_id= удалён
        if self.resume_handle:
            kwargs["session_resumption"] = types.SessionResumptionConfig(handle=self.resume_handle)
        return types.LiveConnectConfig(**kwargs)

    async def _session_loop(self):
        http_options = {'api_version': 'v1alpha'}
        if EFFECTIVE_PROXY:
            http_options.update(client_args={'proxy': EFFECTIVE_PROXY},
                                async_client_args={'proxy': EFFECTIVE_PROXY})
        client = genai.Client(api_key=API_KEY_GEMINI, http_options=http_options)

        order = []
        if self.working_model_idx is not None:
            order.append(self.working_model_idx)
        order += [i for i in range(len(MODEL_CANDIDATES)) if i != self.working_model_idx]

        last_err = None
        for idx in order:
            model, thinking = MODEL_CANDIDATES[idx]
            # handle от другой модели не годится
            if idx != self.working_model_idx:
                self.resume_handle = None
            try:
                logging.info(f"[SESSION] Connecting model={model} "
                             f"(resume={'yes' if self.resume_handle else 'new'})")
                config = self._build_config(thinking)
                async with client.aio.live.connect(model=model, config=config) as session:
                    self.working_model_idx = idx
                    short = model.replace("models/", "")
                    self.root.after(0, lambda m=short: self.model_lbl.config(text=f"model: {m}"))
                    await self._handle_session(session)
                return
            except Exception as e:
                last_err = e
                logging.warning(f"[SESSION] Model {model} failed: {e}")
                self.resume_handle = None
                continue

        raise RuntimeError(f"All models failed: {last_err}")

    async def _handle_session(self, session):
        self.session = session
        self.connected = True

        if not self.audio or not self.audio.running:
            self.audio = AudioCapture(self.q)
            if MODE == "ptt":
                self.audio.muted = True  # слушаем только пока зажат пробел
            if not self.audio.start():
                raise RuntimeError("Audio failed")
            # применяем выбранный в UI источник (микрофон/система)
            sel = self.source_var.get()
            if sel != "mic" and not self.audio.set_source(sel):
                logging.warning("[AUDIO] Не удалось включить системный звук, остаёмся на микрофоне")
                self.root.after(0, lambda: self.source_var.set("mic"))

        if not self.audio.mic:
            msg = "MIC ERROR: не удалось открыть микрофон. Проверь приватность микрофона Windows."
            logging.error(f"[AUDIO] {msg} Details: {getattr(self.audio, 'mic_error', None)}")
            self.root.after(0, lambda: self._set_status("MIC ERROR", "#ff4444"))
            self.root.after(0, lambda m=msg: self._set_text(self.heard, m))
        else:
            ready_txt = "ГОТОВ — держи ПРОБЕЛ" if MODE == "ptt" else "LISTENING"
            self.root.after(0, lambda: self._set_status(ready_txt, "#00ff88"))

        # PTT-стейт не должен переживать реконнект
        self.ptt_held = False

        await asyncio.gather(self._send_audio(), self._recv(), return_exceptions=True)

    async def _send_audio(self):
        while self.connected:
            try:
                data = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.q.get(timeout=0.1))
                if self.session:
                    await self.session.send_realtime_input(audio=types.Blob(
                        mime_type="audio/pcm;rate=16000", data=data))
            except queue.Empty:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if "closed" not in str(e).lower():
                    logging.error(f"[AUDIO] Send error: {e}")
                break

    async def _recv(self):
        try:
            async for resp in self.session.receive():
                if not self.connected:
                    break

                # resumption handle для переподключений
                if getattr(resp, 'session_resumption_update', None):
                    upd = resp.session_resumption_update
                    handle = getattr(upd, 'new_handle', None)
                    if handle and getattr(upd, 'resumable', False):
                        self.resume_handle = handle

                if hasattr(resp, 'go_away') and resp.go_away:
                    logging.warning("[GOAWAY] Server requested reconnection")
                    self.connected = False
                    return

                sc = getattr(resp, 'server_content', None)
                if not sc:
                    continue

                # промежуточное распознавание — видно текст прямо во время речи
                it = getattr(sc, 'interim_input_transcription', None)
                if it and getattr(it, 'text', ''):
                    t = self.heard_final + " " + it.text if self.heard_final else it.text
                    self._turn_heard_speech = True
                    self.root.after(0, lambda x=t.strip(): self._show_heard(x))

                # финальное распознавание вопроса
                inp = getattr(sc, 'input_transcription', None)
                if inp and getattr(inp, 'text', ''):
                    self.heard_final = (self.heard_final + " " + inp.text).strip()
                    self._turn_heard_speech = True
                    self.root.after(0, lambda x=self.heard_final: self._show_heard(x))
                    if not self._manual_activity:
                        # VAD-режим: конец речи = момент финальной транскрипции
                        self._turn_started_at = time.time()
                        self._turn_first_text = False
                    logging.info(f"[HEARD] {self.heard_final}")

                # стрим ответа (транскрипция аудио-ответа)
                out = getattr(sc, 'output_transcription', None)
                if out and getattr(out, 'text', ''):
                    t = out.text
                    self.root.after(0, lambda x=t: self._on_output_text(x))

                # перебивание модели (актуально для auto-режима)
                if getattr(sc, 'interrupted', False):
                    self.response = ""
                    self._turn_first_text = False
                    logging.info("[TURN] Interrupted (barge-in), ответ сброшен")

                if getattr(sc, 'turn_complete', False):
                    self.root.after(0, self._on_turn_complete)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if "closed" in str(e).lower():
                logging.info("[RECV] Connection closed")
            else:
                logging.error(f"[RECV] Error: {e}")

        self.connected = False
        if not self.should_reconnect:
            self.root.after(0, lambda: self._set_status("Disconnected", "#ff4444"))

    def disconnect(self):
        self.should_reconnect = False
        self.connected = False
        if self.audio:
            self.audio.stop()
        self._set_status("Disconnected", "#ff4444")
        self.btn.config(text="CONNECT", bg="#28a745")

    def reset(self):
        """Новый диалог БЕЗ разрыва соединения: скорость не теряем.
        Реплики между нажатиями пробела копятся в контексте сессии (уточнения
        и правки работают сами). Эта кнопка — единственный способ начать с чистого листа."""
        logging.info("[RESET] Новый диалог (без переподключения)")
        # текстовый маркер в сессию: предыдущая история не учитывается;
        # turn_complete=False -> модель не отвечает на сам маркер
        self._send_to_session(lambda: self.session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=(
                "«Сброс диалога». Начни новый чистый диалог: следующая реплика "
                "пользователя — первый вопрос нового диалога, прежнюю историю "
                "не учитывай."))])],
            turn_complete=False))
        self.resume_handle = None  # при следующем реконнекте — новая сессия без истории
        self.current_row = 1
        self.heard_final = ""
        self.response = ""
        self._turn_first_text = False
        self._turn_heard_speech = False
        self._turn_started_at = None
        self._ptt_released_at = None
        self._awaiting_speech_check = False
        self._highlight()
        for p in [self.p1, self.p2]:
            self._clear_text(p["txt"])
        self._clear_text(self.heard)
        self.latency_lbl.config(text="⚡ --")
        self._set_status("НОВЫЙ ДИАЛОГ — держи ПРОБЕЛ", "#00ff88")

    def run(self):
        self.root.after(1000, self._autoconnect)
        self.root.mainloop()

    def _autoconnect(self):
        if not (self.connected or self.should_reconnect):
            logging.info("[APP] Автоподключение при старте")
            self.connect()

    def _on_close(self):
        logging.info("[APP] Окно закрыто пользователем")
        # флаг для супервизора: перезапускать не нужно
        try:
            open(os.path.join(os.path.dirname(__file__) or '.', '.user_closed'), 'w').close()
        except Exception:
            pass
        try:
            self.disconnect()
        finally:
            self.root.destroy()


if __name__ == "__main__":
    print(f"Fast Assistant v27 | MODE={MODE} | модели: " +
          ", ".join(m.replace('models/', '') for m, _ in MODEL_CANDIDATES))
    print("Ускорения: без второй модели, без loopback, PTT=мгновенный конец реплики,"
          " VAD 800→500мс (auto), компактный промпт.")
    if not API_KEY_GEMINI:
        print("ВНИМАНИЕ: API-ключ не найден. Создай assistant_secrets.py с API_KEY_GEMINI='...' "
              "или задай переменную окружения GEMINI_API_KEY.")
    if not HAVE_WASAPI:
        print("WARNING: pyaudiowpatch not found — будет pyaudio/sounddevice fallback")
    App().run()
