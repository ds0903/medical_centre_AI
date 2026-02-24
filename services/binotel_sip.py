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

# Binotel інколи шле RTP пакети з OPUS payload type, хоча в SDP ми погодились
# лише на PCMU/PCMA. pyVoIP не вміє їх декодувати і кидає UserWarning для кожного
# такого пакету. Придушуємо цей шум — він не впливає на голос (голос іде як PCMU).
warnings.filterwarnings("ignore", category=UserWarning, message=".*[Oo]pus.*")

from pyVoIP.VoIP import VoIPPhone, CallState, InvalidStateError
from pyVoIP.SIP import SIPMessageType
from pyVoIP.RTP import RTPClient as _RTPClient

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
        """Прочитати PCM 8kHz 16-bit з дзвінка (голос пацієнта)."""
        if not self.is_active:
            return b"\x00" * size
        try:
            return self._call.read_audio(size)
        except InvalidStateError:
            self._active = False
            return b"\x00" * size
        except Exception as e:
            logger.debug(f"[SIP] read_audio error: {e}")
            return b"\x00" * size

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
                        logger.info(f"[SIP] OPTIONS from Via={_addr}:{_port}, sending 200 OK")
                        __sc.out.sendto(response.encode("utf8"), (_addr, int(_port)))
                        # Also send to SIP server directly (in case Via has internal IP)
                        if _addr != __sc.server:
                            __sc.out.sendto(
                                response.encode("utf8"), (__sc.server, __sc.port)
                            )
                            logger.info(f"[SIP] Also sent 200 OK to server {__sc.server}:{__sc.port}")
                        logger.info("[SIP] OPTIONS → 200 OK sent")
                    except Exception as _e:
                        logger.info(f"[SIP] OPTIONS handler error: {_e}")
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
            # Несподіваний дзвінок — залишок від попередньої сесії. Відхиляємо.
            logger.info(f"[SIP] Відхиляємо несподіваний дзвінок від {caller} (стара сесія)")
            try:
                call.hangup()
            except Exception:
                pass
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

        call.answer()
        caller = getattr(call, 'phoneNumber', None) or getattr(call, 'phone_number', 'unknown')
        logger.info(f"[SIP] Дзвінок прийнято від {caller}")
        return BinotelSipAudio(call)
