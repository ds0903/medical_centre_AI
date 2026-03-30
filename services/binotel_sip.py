"""
Binotel SIP клієнт — аудіо-міст між Binotel та Gemini Live API.

Схема:
  1. Реєструємось як SIP extension в Binotel (внутрішній номер бота)
  2. Викликаємо REST API: calls/internal-number-to-external-number
  3. Binotel дзвонить на наш SIP extension → pyVoIP відповідає
  4. Binotel одночасно дзвонить пацієнту
  5. Коли обидва відповіли — аудіо тече через RTP
  6. pyVoIP read/write працює з unsigned 8-bit PCM @ 8kHz
     (G.711 µ-law кодування/декодування + unsigned bias виконуються всередині pyVoIP)

Залежність: pyVoIP (pip install pyVoIP)
"""

import asyncio
import logging
import random
import string
import threading
import time
import urllib.request
import warnings
from typing import Optional

from pyVoIP.VoIP import VoIPPhone, CallState, InvalidStateError
from pyVoIP.SIP import SIPMessageType, SIPClient as _SIPClientEarly

# ---------------------------------------------------------------------------
# Патч pyVoIP SIPClient.recv: баг на Windows — при ConnectionResetError (UDP ICMP)
# змінна `raw` не ініціалізована, але except-блок намагається її логувати →
# UnboundLocalError обвалює SIP Receive thread. Патч перехоплює це тихо.
# ---------------------------------------------------------------------------
_orig_sip_recv = _SIPClientEarly.recv

def _sip_recv_patched(self):
    try:
        _orig_sip_recv(self)
    except UnboundLocalError:
        pass  # pyVoIP bug: raw not set when recv() itself throws

_SIPClientEarly.recv = _sip_recv_patched
from pyVoIP.RTP import RTPClient as _RTPClient, PayloadType as _PayloadType

# ---------------------------------------------------------------------------
# Monkey-patch: додаємо PayloadType.OPUS (dynamic pt=111) до pyVoIP enum.
# pyVoIP 1.6.8 не знає про Opus → UNKNOWN для pt=111 → RTPParseError.
# ---------------------------------------------------------------------------
if 111 not in _PayloadType._value2member_map_:
    _opus_member = object.__new__(_PayloadType)
    _opus_member._name_        = "OPUS"
    _opus_member._value_       = 111
    _opus_member._rate         = 48000
    _opus_member._channel      = 2
    _opus_member._description  = "opus"
    _PayloadType._value2member_map_[111] = _opus_member
    _PayloadType._member_map_["OPUS"]    = _opus_member
    # Обходимо enum __setattr__ через type.__setattr__ → зберігаємо як атрибут класу
    type.__setattr__(_PayloadType, "OPUS", _opus_member)

# NAT fix для RTP: pyVoIP намагається bind RTP сокет до публічного IP
# (WinError 10049 — неможливо bind до зовнішнього IP на Windows).
# Патч: перенаправляємо bind до 0.0.0.0 (слухаємо на всіх інтерфейсах).
# SDP c= лінія використовує SIPClient.myIP (публічний IP) — окремо, не залежить від inIP.
_orig_rtp_start = _RTPClient.start


def _rtp_start_nat_patch(self) -> None:
    _orig_inIP = self.inIP
    self.inIP = "0.0.0.0"  # Bind to all interfaces instead of public IP
    try:
        _orig_rtp_start(self)
    finally:
        self.inIP = _orig_inIP  # Restore for SDP / logging purposes


_RTPClient.start = _rtp_start_nat_patch


# Патч encode_pcmu: audio_bridge.py тепер надає вже готові µ-law байти безпосередньо.
# Без патчу pyVoIP робив: bias(-128) + lin2ulaw(width=1) — тобто кодував ще раз те,
# що вже є µ-law → подвійна деградація, "розмитий" звук.
# З патчем: просто пропускаємо байти як є (passthrough).
_encode_pcmu_log_done = False


def _encode_pcmu_passthrough(self, packet: bytes) -> bytes:
    global _encode_pcmu_log_done
    if not _encode_pcmu_log_done:
        _encode_pcmu_log_done = True
        pref = getattr(self, "preference", "?")
        pref_str = str(pref)
        # Виявляємо кодек і налаштовуємо audio_bridge відповідно.
        # Binotel в Україні типово використовує PCMA (G.711 A-law).
        from services.audio_bridge import set_codec_alaw
        is_alaw = "PCMA" in pref_str
        set_codec_alaw(is_alaw)
        logging.getLogger(__name__).info(
            f"[SIP] encode_pcmu PASSTHROUGH ✓ "
            f"(codec={pref_str}, {'A-law' if is_alaw else 'µ-law'}, chunk={len(packet)}b)"
        )
    return packet


_RTPClient.encode_pcmu = _encode_pcmu_passthrough


# ---------------------------------------------------------------------------
# Патч parse_packet: додаємо обробку Opus (pt=111).
# ---------------------------------------------------------------------------
_orig_parse_packet = _RTPClient.parse_packet


def _parse_packet_with_opus(self, packet: bytes) -> None:
    # Перевіряємо тип пакету по raw byte — не через RTPMessage щоб не ризикувати
    # виключенням яке б заглушило _orig_parse_packet для PCMA пакетів.
    if len(packet) > 1 and (packet[1] & 0x7f) == 111:
        # Opus пакет
        try:
            from pyVoIP.RTP import RTPMessage
            msg = RTPMessage(packet, self.assoc)
            _parse_opus(self, msg)
            return
        except Exception as e:
            logging.getLogger(__name__).debug(f"[OPUS] parse_packet error: {e}")
    # PCMA/PCMU або будь-який інший кодек → оригінальний handler
    _orig_parse_packet(self, packet)


def _ensure_opus_recv_queue(rtp_client) -> "_queue.Queue":
    """Окрема черга для Opus receive — bypass pmin, пряма s16 16kHz."""
    if not hasattr(rtp_client, "_opus_recv_queue"):
        rtp_client._opus_recv_queue = _queue.Queue(maxsize=50)
    return rtp_client._opus_recv_queue


def _parse_opus(self, msg) -> None:
    """Decode Opus RTP payload → PCM signed 16-bit 16kHz → _opus_recv_queue.

    Bypass pmin: немає timestamp проблем, немає u8↔s16 деградації.
    s16 16kHz → напряму в sip_to_gemini_input → Gemini.
    """
    from services.opus_codec import decode as opus_decode
    try:
        pcm_s16 = opus_decode(msg.payload)
        _ensure_opus_recv_queue(self).put_nowait(pcm_s16)
    except _queue.Full:
        pass  # черга повна — дропаємо найстаріший пакет
    except Exception as e:
        logging.getLogger(__name__).debug(f"[OPUS] decode error: {e}")


_RTPClient.parse_packet = _parse_packet_with_opus
_RTPClient.parse_opus   = _parse_opus


# ---------------------------------------------------------------------------
# Патч encode_packet: додаємо обробку Opus preference.
# ---------------------------------------------------------------------------
_orig_encode_packet = _RTPClient.encode_packet


def _encode_packet_with_opus(self, payload: bytes) -> bytes:
    if self.preference == _PayloadType.OPUS:
        return _encode_opus_passthrough(self, payload)
    return _orig_encode_packet(self, payload)


_encode_opus_log_done = False


def _encode_opus_passthrough(self, packet: bytes) -> bytes:
    """Opus: audio_bridge вже надає готові Opus байти — просто передаємо їх далі."""
    global _encode_opus_log_done
    if not _encode_opus_log_done:
        _encode_opus_log_done = True
        from services.audio_bridge import set_codec_opus
        set_codec_opus(True)
        logging.getLogger(__name__).info(
            f"[SIP] encode_opus PASSTHROUGH ✓ (chunk={len(packet)}b)"
        )
    return packet


_RTPClient.encode_packet = _encode_packet_with_opus
_RTPClient.encode_opus   = _encode_opus_passthrough


# ---------------------------------------------------------------------------
# Патч gen_answer: ін'єктуємо Opus як перший кодек у SDP 200 OK.
# Binotel вибирає перший кодек з нашого SDP → Opus матиме найвищий пріоритет.
# ---------------------------------------------------------------------------
import re as _re


def _inject_opus_sdp(sip_msg: str) -> str:
    """Додає Opus (pt=111) першим кодеком у SDP тіло SIP повідомлення."""
    if "m=audio" not in sip_msg or "a=rtpmap:111 opus" in sip_msg:
        return sip_msg  # вже є або немає аудіо
    if "\r\n\r\n" not in sip_msg:
        return sip_msg

    headers, body = sip_msg.split("\r\n\r\n", 1)

    # Додаємо 111 першим у m=audio рядку: "m=audio PORT RTP/AVP 0 8" → "... 111 0 8"
    body = _re.sub(r"(m=audio \d+ RTP/AVP)", r"\1 111", body)

    # Вставляємо opus rtpmap перед першим існуючим a=rtpmap
    opus_lines = (
        "a=rtpmap:111 opus/48000/2\r\n"
        "a=fmtp:111 minptime=20;useinbandfec=1\r\n"
    )
    body = body.replace("a=rtpmap:", opus_lines + "a=rtpmap:", 1)

    # Оновлюємо Content-Length
    new_len = len(body.encode("utf-8"))
    headers = _re.sub(r"Content-Length: \d+", f"Content-Length: {new_len}", headers)

    return headers + "\r\n\r\n" + body


from pyVoIP.SIP import SIPClient as _SIPClient
_orig_gen_answer = _SIPClient.gen_answer

# Прапор: gen_answer успішно ін'єктував Opus → trans() має використати Opus
# навіть якщо pyVoIP виставив preference=PCMA з INVITE Binotel.
_next_call_use_opus: bool = False


def _gen_answer_with_opus(self, request, sess_id, ms, sendtype) -> str:
    global _next_call_use_opus
    result = _orig_gen_answer(self, request, sess_id, ms, sendtype)
    injected = _inject_opus_sdp(result)
    if injected != result:
        _next_call_use_opus = True
        logging.getLogger(__name__).info("[SIP] Opus ін'єктовано в SDP → trans() використає Opus")
    return injected


_SIPClient.gen_answer = _gen_answer_with_opus


# ---------------------------------------------------------------------------
# Патч trans(): Opus output loop замість pmout+G.711.
#
# pyVoIP's trans() читає 160 байт з pmout і кодує як G.711.
# Для Opus потрібні повні фрейми (960 семплів @ 48kHz = ~20-200 байт encoded).
# Замінюємо на queue-based підхід: binotel.py кладе готові Opus фрейми в чергу,
# trans() бере по одному і відправляє з правильними RTP таймінгами.
#
# ВАЖЛИВО: pyVoIP встановлює preference=PCMA з INVITE Binotel (перший кодек у списку).
# Ми ін'єктуємо Opus у наш 200 OK → Binotel надсилає Opus пакети до нас.
# Але preference залишається PCMA → trans() не перемикається автоматично.
# Рішення: _next_call_use_opus прапор з gen_answer → форсуємо Opus у trans().
# ---------------------------------------------------------------------------
import queue as _queue
import time as _time_mod

_orig_trans = _RTPClient.trans


def _ensure_opus_queue(rtp_client) -> "_queue.Queue":
    if not hasattr(rtp_client, "_opus_send_queue"):
        rtp_client._opus_send_queue = _queue.Queue(maxsize=600)
    return rtp_client._opus_send_queue


def _trans_opus_aware(self) -> None:
    global _next_call_use_opus
    use_opus = (self.preference == _PayloadType.OPUS) or _next_call_use_opus
    if not use_opus:
        _orig_trans(self)
        return

    _next_call_use_opus = False  # скидаємо — цей trans() вже взяв прапор

    # Opus mode
    _ensure_opus_queue(self)
    from services.audio_bridge import set_codec_opus, _use_opus
    if not _use_opus:
        set_codec_opus(True)
    logging.getLogger(__name__).info("[OPUS] trans() Opus mode active")

    while self.NSD:
        last_sent = _time_mod.monotonic_ns()
        try:
            payload = self._opus_send_queue.get(timeout=0.025)
        except _queue.Empty:
            continue

        # Будуємо RTP пакет вручну (RFC 3550)
        try:
            seq_b = self.outSequence.to_bytes(2, byteorder="big")
        except OverflowError:
            self.outSequence = 0
            seq_b = b"\x00\x00"
        try:
            ts_b = self.outTimestamp.to_bytes(4, byteorder="big")
        except OverflowError:
            self.outTimestamp = 0
            ts_b = b"\x00\x00\x00\x00"

        packet = (
            b"\x80"              # V=2, P=0, X=0, CC=0
            + bytes([111])       # M=0, PT=111 Opus
            + seq_b
            + ts_b
            + self.outSSRC.to_bytes(4, byteorder="big")
            + payload
        )
        try:
            self.sout.sendto(packet, (self.outIP, self.outPort))
        except OSError:
            pass

        self.outSequence += 1
        self.outTimestamp += 960   # 20мс @ 48kHz

        elapsed = (_time_mod.monotonic_ns() - last_sent) / 1e9
        sleep_time = max(0.0, 0.020 - elapsed)
        _time_mod.sleep(sleep_time / self.trans_delay_reduction)


_RTPClient.trans = _trans_opus_aware


def _get_public_ip() -> str:
    """Отримати зовнішній IP щоб SIP Contact header був доступний з Binotel."""
    try:
        return urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode()
    except Exception:
        return ""

logger = logging.getLogger(__name__)

# Розмір chunk для read_audio: 40ms @ 8kHz unsigned-8bit = 320 bytes
# (pyVoIP read_audio повертає unsigned 8-bit PCM, тому 1 байт = 1 sample)
_CHUNK_BYTES = 320


class BinotelSipAudio:
    """
    Інтерфейс аудіо для активного SIP дзвінка.

    pyVoIP повертає/приймає PCM 8kHz 16-bit mono (G.711 декодується бібліотекою).
    Конвертацію частоти дискретизації виконує audio_bridge.py.
    """

    def __init__(self, call) -> None:
        self._call = call
        self._active = True

    @property
    def is_active(self) -> bool:
        return self._active and self._call.state == CallState.ANSWERED

    def write(self, pcm_8khz: bytes) -> None:
        """Записати PCM 8kHz 16-bit у дзвінок (пацієнт почує цей звук)."""
        if not self.is_active:
            return
        try:
            self._call.write_audio(pcm_8khz)
        except InvalidStateError:
            self._active = False
        except Exception as e:
            logger.debug(f"[SIP] write_audio error: {e}")

    def read(self, size: int = _CHUNK_BYTES) -> bytes:
        """Прочитати аудіо з дзвінка (голос пацієнта).

        Opus mode: читає PCM s16 16kHz з _opus_recv_queue (bypass pmin).
        G.711 mode: читає unsigned 8-bit 8kHz через pyVoIP read_audio (pmin).
        Повертає b"" якщо немає даних (receive_audio() перейде у sleep 10ms).
        """
        if not self.is_active:
            return b""
        # Opus bypass: пряма черга s16 16kHz → без u8↔s16 деградації
        for rtp in getattr(self._call, "RTPClients", []):
            q = getattr(rtp, "_opus_recv_queue", None)
            if q is not None:
                try:
                    return q.get(timeout=0.05)
                except _queue.Empty:
                    return b""
        # G.711 fallback: pyVoIP pmin (blocking)
        try:
            return self._call.read_audio(size)
        except InvalidStateError:
            self._active = False
            return b""
        except Exception as e:
            logger.debug(f"[SIP] read_audio error: {e}")
            return b""

    def flush_audio(self) -> None:
        """Зупинити аудіо AI без рипіння при barge-in.

        Opus mode:
          1. Очищаємо _opus_send_queue — зупиняємо заплановані фрейми.
          2. Надсилаємо 2 тихих Opus-фрейми (40мс тиші).
             Binotel-декодер отримує явний сигнал "тиша" замість "пакет загублено"
             → PLC не спрацьовує → немає рипіння в трубці після barge-in.

        G.711 mode: fade-out рампа в pmout (старий підхід, залишається).
        """
        for rtp in getattr(self._call, "RTPClients", []):
            q = getattr(rtp, "_opus_send_queue", None)
            if q is not None:
                # Opus mode: очищаємо накопичені фрейми
                with q.mutex:
                    q.queue.clear()
                # Додаємо 2 тихих фрейми → чистий перехід до тиші без PLC-артефакту
                try:
                    from services import opus_codec as _oc
                    _oc.reset_filter_state()       # скидаємо FIR-хвіст перерваного аудіо
                    _oc.clear_encode_buffer()      # скидаємо залишок PCM-буфера
                    silence_pcm = b"\x00" * 960    # 480 семплів @ 24kHz = 20мс тиші
                    for _ in range(2):
                        for frame in _oc.encode(silence_pcm):
                            try:
                                q.put_nowait(frame)
                            except Exception:
                                pass
                except Exception:
                    pass
            else:
                # G.711 mode: замінюємо pmout буфер на fade-out рампу замість append.
                # Без цього накопичені пакети AI грають до кінця → клік при обриві.
                pmout = getattr(rtp, "pmout", None)
                if pmout is not None:
                    from services.audio_bridge import generate_fadeout_g711
                    fadeout = generate_fadeout_g711()
                    pmout.rebuild(reset=True, data=fadeout or b"")
                # G.711: очищаємо encode buffer окремо (немає silence frame логіки)
                try:
                    from services.opus_codec import clear_encode_buffer
                    clear_encode_buffer()
                except Exception:
                    pass

    def hangup(self) -> None:
        """Завершити SIP дзвінок."""
        self._active = False
        try:
            if self._call.state != CallState.ENDED:
                self._call.hangup()
        except InvalidStateError:
            pass
        except Exception as e:
            logger.debug(f"[SIP] hangup error: {e}")


class BinotelSipClient:
    """
    SIP клієнт що реєструється в Binotel як внутрішній extension.

    Після виклику start() чекає на вхідний дзвінок від Binotel
    (Binotel дзвонить на наш extension після REST API виклику).
    """

    def __init__(
        self,
        server: str,
        port: int,
        username: str,
        password: str,
    ) -> None:
        self._server = server
        self._port = port
        self._username = username
        self._password = password
        self._phone: Optional[VoIPPhone] = None
        self._pending_calls: asyncio.Queue = asyncio.Queue()
        self._expecting_call: bool = False  # True тільки коли ми чекаємо дзвінок від Binotel
        self._keepalive_running: bool = False
        self._public_ip: str = ""
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # event loop для thread-safe callback

    def start(self) -> None:
        """Зареєструватись як SIP extension в Binotel."""
        # Зберігаємо event loop поки ми ще в async контексті (start() викликається з startup()).
        # _on_incoming_call запускається з потоку pyVoIP — там asyncio.get_event_loop() не працює
        # в Python 3.12+. Зберігаємо loop тут щоб використовувати call_soon_threadsafe пізніше.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = None

        public_ip = _get_public_ip()
        self._public_ip = public_ip
        if public_ip:
            logger.info(f"[SIP] Публічний IP: {public_ip}")
        else:
            logger.warning("[SIP] Не вдалося отримати публічний IP — NAT може заблокувати вхідні дзвінки")

        # NAT fix: pyVoIP має рекламувати публічний IP у SIP Contact/Via заголовках,
        # але прив'язувати сокет до 0.0.0.0 (не до публічного IP — це викликає WinError 10049).
        # Патчимо socket.socket щоб перенаправляти bind(public_ip, port) → bind(0.0.0.0, port).
        import socket as _sock_mod
        _original_socket_class = _sock_mod.socket
        if public_ip:
            _pub = public_ip

            class _NATSocket(_sock_mod.socket):
                def bind(self, address):
                    host, port = address
                    if host == _pub:
                        logger.info(f"[SIP NAT] bind {host}:{port} → 0.0.0.0:{port}")
                        super().bind(("0.0.0.0", port))
                    else:
                        logger.debug(f"[SIP NAT] bind {host}:{port}")
                        super().bind(address)

            _sock_mod.socket = _NATSocket

        try:
            # pyVoIP 1.6.8 не включає OPTIONS до SIPCompatibleMethods —
            # тому при отриманні OPTIONS від Binotel SIPMessage кидає SIPParseError
            # і parse_message ніколи не викликається → Binotel не отримує 200 OK
            # → qualify=0 → extension "offline" → error 150.
            import pyVoIP as _pyVoIP
            if "OPTIONS" not in _pyVoIP.SIPCompatibleMethods:
                _pyVoIP.SIPCompatibleMethods.append("OPTIONS")
                logger.info("[SIP] Додано OPTIONS до SIPCompatibleMethods")

            self._phone = VoIPPhone(
                server=self._server,
                port=self._port,
                username=self._username,
                password=self._password,
                callCallback=self._on_incoming_call,
                myIP=public_ip if public_ip else None,
            )
            self._phone.start()

            # Патч: ігноруємо pyVoIP fatalCallback під час активного дзвінка.
            # pyVoIP може фейлити re-REGISTER якщо Binotel ротував на інший SIP-сервер
            # (наприклад sip11 замість sip1). За замовчуванням fatalCallback → stop() →
            # намагається hangup() всі дзвінки → WinError 10038 (сокет вже закрито).
            # Дзвінок при цьому виживає (RTP незалежний), але в логах страшні помилки.
            # Ми самі завершуємо SIP через BinotelSipClient.stop() — fatal не потрібен.
            # ВАЖЛИВО: треба патчити sip.fatalCallback (не phone.fatal!) —
            # SIPClient зберігає копію посилання при ініціалізації, тому
            # заміна phone.fatal не впливає на вже збережений fatalCallback.
            def _safe_fatal():
                if getattr(self._phone, 'calls', {}):
                    logger.warning("[SIP] pyVoIP fatalCallback під час активного дзвінка — ігноруємо")
                else:
                    logger.warning("[SIP] pyVoIP fatalCallback — зупиняємо SIP клієнт")
                    try:
                        self._phone.stop()
                    except Exception:
                        pass
                    self._phone = None  # is_ready() одразу поверне False → швидке виявлення збою

            self._phone.sip.fatalCallback = _safe_fatal

            # Патч: відповідаємо 200 OK на OPTIONS запити від Binotel.
            # Без цього Binotel вважає extension недосяжним (error 150).
            # pyVoIP 1.6.8 не обробляє OPTIONS — відповідь не надсилається.
            _sc = self._phone.sip
            _orig_parse = _sc.parse_message

            def _parse_with_options(message, __sc=_sc, __orig=_orig_parse):
                if (message.type == SIPMessageType.MESSAGE
                        and getattr(message, 'method', None) == "OPTIONS"):
                    try:
                        response = __sc.gen_ok(message)
                        (_addr, _port) = message.headers["Via"][0]["address"]
                        __sc.out.sendto(response.encode("utf8"), (_addr, int(_port)))
                        # Also send to SIP server directly (in case Via has internal IP)
                        if _addr != __sc.server:
                            __sc.out.sendto(
                                response.encode("utf8"), (__sc.server, __sc.port)
                            )
                    except Exception as _e:
                        logger.debug(f"[SIP] OPTIONS handler error: {_e}")
                    return
                __orig(message)

            _sc.parse_message = _parse_with_options

            logger.info(
                f"[SIP] Зареєстровано: {self._username}@{self._server}:{self._port}"
            )

            # Keep-alive: посилаємо OPTIONS до Binotel кожні 15с (як MicroSIP).
            # Це тримає NAT-маппінг живим і дозволяє Binotel оновлювати статус extension.
            self._start_keepalive(_sc)
        finally:
            _sock_mod.socket = _original_socket_class

    def _start_keepalive(self, sip_client) -> None:
        """
        Посилати SIP OPTIONS до Binotel кожні 15с.

        MicroSIP робить те саме через Keep-Alive=15. Без цього:
          - NAT UDP маппінг закінчується (~30с) → Binotel не може достукатися до нас
          - qualify залишається 0 → extension офлайн → error 150
        """
        self._keepalive_running = True

        _server = self._server
        _port = self._port
        _username = self._username
        _public_ip = self._public_ip
        _local_port = getattr(sip_client, 'myPort', _port)
        _sock = getattr(sip_client, 'out', None) or getattr(sip_client, 's', None)

        def _rand(n: int = 8) -> str:
            return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

        def _loop():
            seq = 1
            call_id = f"{_rand(12)}@{_public_ip or _server}"
            tag = _rand(8)
            while self._keepalive_running:
                time.sleep(15)
                if not self._keepalive_running:
                    break
                try:
                    branch = f"z9hG4bK{_rand(8)}"
                    msg = (
                        f"OPTIONS sip:{_server} SIP/2.0\r\n"
                        f"Via: SIP/2.0/UDP {_public_ip or _server}:{_local_port}"
                        f";rport;branch={branch}\r\n"
                        f"Max-Forwards: 70\r\n"
                        f"From: <sip:{_username}@{_server}>;tag={tag}\r\n"
                        f"To: <sip:{_server}>\r\n"
                        f"Call-ID: {call_id}\r\n"
                        f"CSeq: {seq} OPTIONS\r\n"
                        f"User-Agent: pyVoIP\r\n"
                        f"Content-Length: 0\r\n"
                        f"\r\n"
                    )
                    if _sock:
                        _sock.sendto(msg.encode("utf-8"), (_server, _port))
                        logger.debug(f"[SIP] Keep-alive OPTIONS sent (seq={seq})")
                    seq += 1
                except Exception as e:
                    logger.debug(f"[SIP] Keep-alive error: {e}")

        threading.Thread(target=_loop, daemon=True, name="sip-keepalive").start()
        logger.info("[SIP] Keep-alive запущено (кожні 15с)")

    def stop(self) -> None:
        """Зупинити SIP клієнт."""
        self._keepalive_running = False
        if self._phone:
            try:
                self._phone.stop()
            except Exception as e:
                logger.debug(f"[SIP] stop error: {e}")
        logger.info("[SIP] SIP клієнт зупинено")

    def _on_incoming_call(self, call) -> None:
        """
        Callback від pyVoIP при вхідному дзвінку.
        Запускається в окремому потоці pyVoIP.
        """
        caller = getattr(call, 'phoneNumber', None) or getattr(call, 'phone_number', 'unknown')

        if not self._expecting_call:
            # Несподіваний дзвінок — залишок від попередньої сесії. Ігноруємо.
            # НЕ викликаємо hangup() — pyVoIP при hangup() псує внутрішній стан RTP:
            # наступний дзвінок відкривається без Opus decoder → тиша з боку пацієнта.
            logger.info(f"[SIP] Ігноруємо несподіваний дзвінок від {caller} (стара сесія)")
            return

        logger.info(f"[SIP] Вхідний дзвінок від {caller}")
        # Передаємо в asyncio queue через thread-safe виклик.
        # Використовуємо збережений loop (self._loop) — asyncio.get_event_loop() в потоці
        # pyVoIP кидає RuntimeError у Python 3.12+, а asyncio.run() створює НОВИЙ loop,
        # тому put() не сигналізує оригінальний get() waiters.
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._pending_calls.put_nowait, call)
        else:
            logger.warning("[SIP] Event loop недоступний — дзвінок втрачено!")

    async def wait_for_call(self, timeout: int = 30) -> Optional[BinotelSipAudio]:
        """
        Чекати вхідного SIP дзвінка від Binotel після ініціації через REST API.

        Args:
            timeout: максимально чекати (секунди). Binotel зазвичай дзвонить протягом 5-10с.

        Returns:
            BinotelSipAudio якщо дзвінок прийнято, None якщо timeout.
        """
        self._expecting_call = True
        try:
            call = await asyncio.wait_for(self._pending_calls.get(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"[SIP] Binotel не подзвонив на extension {self._username} за {timeout}с"
            )
            return None
        finally:
            self._expecting_call = False

        # Pre-set Opus flag ПЕРЕД call.answer() — бо call.answer() стартує RTP/trans() threads,
        # які одразу перевіряють _next_call_use_opus. Якщо ставити після — race condition.
        global _next_call_use_opus
        _next_call_use_opus = True

        call.answer()
        caller = getattr(call, 'phoneNumber', None) or getattr(call, 'phone_number', 'unknown')
        logger.info(f"[SIP] Дзвінок прийнято від {caller}")
        return BinotelSipAudio(call)
