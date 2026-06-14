"""
edge/audio/pipeline.py
"""
from __future__ import annotations
import asyncio, json, logging, struct, tempfile, time
from typing import Optional
log = logging.getLogger(__name__)
try:
    import pyaudio, vosk, numpy as np
    _AUDIO_OK = True
except ImportError:
    _AUDIO_OK = False
from shared.protocol.config import cfg
from shared.protocol.messages import AudioStreamEnd, AudioStreamStart, MessageEnvelope
HW_RATE=48000; VOSK_RATE=16000; CHANNELS=1; HW_CHUNK=4800

def _downsample(pcm48):
    arr=__import__('numpy').frombuffer(pcm48,dtype=__import__('numpy').int16)
    return arr[::3].copy().tobytes()

def _rms(pcm):
    count=len(pcm)//2
    if count==0: return 0.0
    samples=struct.unpack(f"<{count}h",pcm[:count*2])
    return (sum(s*s for s in samples)/count)**0.5

def _find_aiy_device(pa):
    for i in range(pa.get_device_count()):
        info=pa.get_device_info_by_index(i)
        if any(k in info["name"].lower() for k in ["googlevoicehat","aiy","voicehat"]) and info["maxInputChannels"]>0:
            log.info("[audio] found AIY: [%d] %s",i,info["name"]); return i
    return None

class WakeWordDetector:
    def __init__(self):
        self._rec=None
        self._wake_words=[w.lower() for w in cfg.audio.wake_words]
    def load(self):
        if not _AUDIO_OK: return
        log.info("[wake] loading model: %s",cfg.audio.vosk_model_path)
        model=vosk.Model(cfg.audio.vosk_model_path)
        self._rec=vosk.KaldiRecognizer(model,VOSK_RATE)
        log.info("[wake] model ready, words=%s",self._wake_words)
    def check(self,pcm16):
        if self._rec is None: return False
        if self._rec.AcceptWaveform(pcm16):
            text=json.loads(self._rec.Result()).get("text","").lower()
            if text: log.info("[wake] heard: '%s'",text)
            if any(w in text for w in self._wake_words):
                log.info("[wake] WAKE WORD DETECTED"); return True
        return False

class AudioPipeline:
    def __init__(self,connection_manager):
        self._conn=connection_manager
        self._detector=WakeWordDetector()
        self._running=False
        self._session_counter=0

    async def start(self):
        self._running=True
        loop=asyncio.get_running_loop()
        await loop.run_in_executor(None,self._detector.load)
        asyncio.create_task(self._listen_loop(),name="audio_listen")
        log.info("[audio_pipeline] started")

    async def stop(self):
        self._running=False

    async def speak(self,text,language="ja-JP"):
        log.info("[tts] speaking: '%s'",text[:60])
        lang="ja" if language.startswith("ja") else "en"
        with tempfile.NamedTemporaryFile(suffix=".wav",delete=False) as f:
            wav_path=f.name
        try:
            proc=await asyncio.create_subprocess_exec("espeak-ng","-v",lang,"-s","130","-w",wav_path,text,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            proc2=await asyncio.create_subprocess_exec("aplay","-D","plughw:1,0",wav_path,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            await proc2.wait()
        except Exception as exc:
            log.error("[tts] error: %s",exc)
        finally:
            import os
            try: os.unlink(wav_path)
            except: pass

    async def _listen_loop(self):
        if not _AUDIO_OK:
            log.warning("[audio_pipeline] stub mode"); return
        pa=pyaudio.PyAudio(); dev_idx=_find_aiy_device(pa)
        stream=pa.open(format=pyaudio.paInt16,channels=CHANNELS,rate=HW_RATE,input=True,input_device_index=dev_idx,frames_per_buffer=HW_CHUNK)
        log.info("[audio_pipeline] listening for wake word... (48kHz->16kHz)")
        try:
            while self._running:
                pcm48=stream.read(HW_CHUNK,exception_on_overflow=False)
                pcm16=_downsample(pcm48)
                if self._detector.check(pcm16):
                    stream.stop_stream()
                    asyncio.create_task(self.speak("はい、なんでしょう"))
                    await self._stream_once(pa,dev_idx)
                    stream.start_stream()
                    log.info("[audio_pipeline] back to listening...")
        finally:
            stream.stop_stream(); stream.close(); pa.terminate()

    async def _stream_once(self,pa,dev_idx):
        # デバイス解放待ち（ウォームアップストリームが完全に閉じるまで）
        await asyncio.sleep(0.5)
        pa2=pyaudio.PyAudio()
        self._session_counter+=1
        sid=f"sess_{self._session_counter:04d}"; sid_int=self._session_counter
        await self._conn.control.send(MessageEnvelope(payload=AudioStreamStart(session_id=sid)))
        stream=pa2.open(format=pyaudio.paInt16,channels=CHANNELS,rate=HW_RATE,input=True,input_device_index=dev_idx,frames_per_buffer=HW_CHUNK)
        t0=time.time(); total=0; silence_ms=0
        log.info("[stream] recording (session=%s)...",sid)
        try:
            while True:
                pcm48=stream.read(HW_CHUNK,exception_on_overflow=False)
                pcm16=_downsample(pcm48); total+=len(pcm16)
                asyncio.create_task(self._conn.audio.send_audio(sid_int,pcm16))
                rms=_rms(pcm16)
                if rms<cfg.audio.silence_amplitude: silence_ms+=100
                else: silence_ms=0
                if silence_ms>=cfg.audio.silence_threshold_ms:
                    log.info("[stream] silence -> end"); break
                if time.time()-t0>30:
                    log.warning("[stream] max duration"); break
        finally:
            stream.stop_stream(); stream.close(); pa2.terminate()
        dur_ms=int((time.time()-t0)*1000)
        await self._conn.control.send(MessageEnvelope(payload=AudioStreamEnd(session_id=sid,duration_ms=dur_ms,total_bytes=total)))
        log.info("[stream] done: %dms / %d bytes",dur_ms,total)
PYEOF