# mqtt_as.py Asynchronous version of umqt.robust
# (C) Copyright Peter Hinch 2017.
# Released under the MIT licence.

import gc
import usocket as socket
import ustruct as struct

gc.collect()
from ubinascii import hexlify
import uasyncio as asyncio

gc.collect()
from utime import ticks_ms, ticks_diff, sleep_ms
from uerrno import EINPROGRESS, ETIMEDOUT

gc.collect()
from micropython import const
from machine import unique_id, idle
import network

gc.collect()
from sys import platform

# Default short delay for good SynCom throughput (avoid sleep(0) with SynCom).
_DEFAULT_MS = const(20)
_SOCKET_POLL_DELAY = const(5)  # 100ms added greatly to publish latency

# Legitimate errors while waiting on a socket. See uasyncio __init__.py open_connection().
BUSY_ERRORS = [EINPROGRESS, ETIMEDOUT]

ESP32 = platform == 'esp32' or platform == 'esp32_LoBo'

# Set up special handling for sonoff and similar devices requiring periodic yield to RTOS
SONOFF = False


def sonoff():
    global SONOFF
    SONOFF = True


# ESP32. It is not enough to regularly yield to RTOS with machine.idle(). There are
# two cases where an explicit sleep() is required. Where data has been written to the
# socket and a response is awaited, a timeout may occur without a >= 20ms sleep.
# Secondly during WiFi connection sleeps are required to prevent hangs.
if ESP32:
    # https://forum.micropython.org/viewtopic.php?f=16&t=3608&p=20942#p20942
    BUSY_ERRORS += [118, 119]  # Add in weird ESP32 errors
    # 20ms seems about the minimum before we miss data read from a socket.

    if platform == 'esp32_LoBo':
        esp32_pause = lambda *_: None
    else:
        def esp32_pause():  # https://github.com/micropython/micropython-esp32/issues/167
            sleep_ms(20)
else:
    esp32_pause = lambda *_: None


# Default "do little" coro for optional user replacement


async def eliza(*_):  # e.g. via set_wifi_handler(coro): see test program
    await asyncio.sleep_ms(_DEFAULT_MS)


class MQTTException(Exception):
    pass


def newpid(pid):
    return pid + 1 if pid < 65535 else 1


def qos_check(qos):
    if not (qos == 0 or qos == 1):
        raise ValueError('Only qos 0 and 1 are supported.')


class Lock:
    def __init__(self):
        self._locked = False

    async def __aenter__(self):
        while True:
            if self._locked:
                await asyncio.sleep_ms(_DEFAULT_MS)
            else:
                self._locked = True
                break

    async def __aexit__(self, *args):
        self._locked = False
        await asyncio.sleep_ms(_DEFAULT_MS)

    def locked(self):
        return self._locked

    def release(self):
        self._locked = False


# MQTT_base class. Handles MQTT protocol on the basis of a good connection.
# Exceptions from connectivity failures are handled by MQTTClient subclass.
class MQTT_base:
    REPUB_COUNT = 0  # TEST
    DEBUG = False

    def __init__(self, client_id, server, port, user, password, keepalive, ping_interval,
                 ssl, ssl_params, response_time, clean_init, clean, max_repubs, will,
                 subs_cb, wifi_coro, connect_coro, ssid, wifi_pw):
        # MQTT config
        self.ping_interval = ping_interval
        self._client_id = client_id
        self._user = user
        self._pswd = password
        self._keepalive = keepalive
        if self._keepalive >= 65536:
            raise ValueError('invalid keepalive time')
        self._response_time = response_time * 1000  # Repub if no PUBACK received (ms).
        self._max_repubs = max_repubs
        self._clean_init = clean_init  # clean_session state on first connection
        self._clean = clean  # clean_session state on reconnect
        if will is None:
            self._lw_topic = False
        else:
            self._set_last_will(*will)
        # WiFi config
        self._ssid = ssid  # For ESP32
        self._wifi_pw = wifi_pw
        self._ssl = ssl
        self._ssl_params = ssl_params
        # Callbacks and coros
        self._cb = subs_cb
        self._wifi_handler = wifi_coro
        self._connect_handler = connect_coro
        # Network
        self.port = port
        if self.port == 0:
            self.port = 8883 if self._ssl else 1883
        self.server = server
        if self.server is None:
            raise ValueError('no server specified.')
        self._sock = None
        self._sta_if = network.WLAN(network.STA_IF)
        self._sta_if.active(True)

        self.pid = 0
        self.rcv_pid = 0
        self.suback = False
        self.last_rx = ticks_ms()  # Time of last communication from broker
        self.lock = Lock()
        self.lock_operation = Lock()
        if ESP32 and platform != 'esp32_LoBo':
            loop = asyncio.get_event_loop()
            loop.create_task(self._idle_task())

    def _set_last_will(self, topic, msg, retain=False, qos=0):
        qos_check(qos)
        if not topic:
            raise ValueError('Empty topic.')
        self._lw_topic = topic
        self._lw_msg = msg
        self._lw_qos = qos
        self._lw_retain = retain

    def dprint(self, *args):
        if self.DEBUG:
            print(*args)

    def _timeout(self, t):
        return ticks_diff(ticks_ms(), t) > self._response_time

    async def _idle_task(self):
        while True:
            await asyncio.sleep_ms(10)
            idle()  # Yield to underlying RTOS

    async def _as_read(self, n, sock=None):  # OSError caught by superclass
        if sock is None:
            sock = self._sock
        data = b''
        t = ticks_ms()
        while len(data) < n:
            esp32_pause()  # Necessary or we can time out.
            if self._timeout(t) or not self.isconnected():
                raise OSError(-1)
            try:
                msg = sock.read(n - len(data))
            except OSError as e:  # ESP32 issues weird 119 errors here
                msg = None
                if e.args[0] not in BUSY_ERRORS:
                    raise
            if msg == b'':  # Connection closed by host (?)
                raise OSError(-1)
            if msg is not None:  # data received
                data = b''.join((data, msg))
                t = ticks_ms()
                self.last_rx = ticks_ms()
            await asyncio.sleep_ms(_SOCKET_POLL_DELAY)
        return data

    async def _as_write(self, bytes_wr, length=0, sock=None):
        if sock is None:
            sock = self._sock
        if length:
            bytes_wr = bytes_wr[:length]
        t = ticks_ms()
        while bytes_wr:
            if self._timeout(t) or not self.isconnected():
                raise OSError(-1)
            try:
                n = sock.write(bytes_wr)
            except OSError as e:  # ESP32 issues weird 119 errors here
                n = 0
                if e.args[0] not in BUSY_ERRORS:
                    raise
            if n:
                t = ticks_ms()
                bytes_wr = bytes_wr[n:]
            esp32_pause()  # Precaution. How to prove whether it's necessary?
            await asyncio.sleep_ms(_SOCKET_POLL_DELAY)

    async def _send_str(self, s):
        await self._as_write(struct.pack("!H", len(s)))
        await self._as_write(s)

    async def _recv_len(self):
        n = 0
        sh = 0
        while 1:
            res = await self._as_read(1)
            b = res[0]
            n |= (b & 0x7f) << sh
            if not b & 0x80:
                return n
            sh += 7

    async def _connect(self, clean):
        self._sock = socket.socket()
        self._sock.setblocking(False)
        try:
            self._sock.connect(self._addr)
        except OSError as e:
            if e.args[0] not in BUSY_ERRORS:
                raise
        await asyncio.sleep_ms(_DEFAULT_MS)
        self.dprint('Connecting to broker.')
        if self._ssl:
            import ussl
            self._sock = ussl.wrap_socket(self._sock, **self._ssl_params)
        premsg = bytearray(b"\x10\0\0\0\0\0")
        msg = bytearray(b"\x04MQTT\x04\0\0\0")

        sz = 10 + 2 + len(self._client_id)
        msg[6] = clean << 1
        if self._user:
            sz += 2 + len(self._user) + 2 + len(self._pswd)
            msg[6] |= 0xC0
        if self._keepalive:
            msg[7] |= self._keepalive >> 8
            msg[8] |= self._keepalive & 0x00FF
        if self._lw_topic:
            sz += 2 + len(self._lw_topic) + 2 + len(self._lw_msg)
            msg[6] |= 0x4 | (self._lw_qos & 0x1) << 3 | (self._lw_qos & 0x2) << 3
            msg[6] |= self._lw_retain << 5

        i = 1
        while sz > 0x7f:
            premsg[i] = (sz & 0x7f) | 0x80
            sz >>= 7
            i += 1
        premsg[i] = sz
        await self._as_write(premsg, i + 2)
        await self._as_write(msg)
        await self._send_str(self._client_id)
        if self._lw_topic:
            await self._send_str(self._lw_topic)
            await self._send_str(self._lw_msg)
        if self._user:
            await self._send_str(self._user)
            await self._send_str(self._pswd)
        # Await CONNACK
        # read causes ECONNABORTED if broker is out; triggers a reconnect.
        resp = await self._as_read(4)
        self.dprint('Connected to broker.')  # Got CONNACK
        if resp[3] != 0 or resp[0] != 0x20 or resp[1] != 0x02:
            raise OSError(-1)  # Bad CONNACK e.g. authentication fail.

    async def _ping(self):
        async with self.lock:
            await self._as_write(b"\xc0\0")

    # Check internet connectivity by sending DNS lookup to Google's 8.8.8.8
    async def wan_ok(self,
                     packet=b'$\x1a\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03www\x06google\x03com\x00\x00\x01\x00\x01'):
        if not self.isconnected():  # WiFi is down
            return False
        length = 32  # DNS query and response packet size
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        s.connect(('8.8.8.8', 53))
        await asyncio.sleep(1)
        try:
            await self._as_write(packet, sock=s)
            await asyncio.sleep(2)
            res = await self._as_read(length, s)
            if len(res) == length:
                return True  # DNS response size OK
        except OSError:  # Timeout on read: no connectivity.
            return False
        finally:
            s.close()
        return False

    async def broker_up(self):  # Test broker connectivity
        if not self.isconnected():
            return False
        tlast = self.last_rx
        if ticks_diff(ticks_ms(), tlast) < 1000:
            return True
        try:
            await self._ping()
        except OSError:
            return False
        t = ticks_ms()
        while not self._timeout(t):
            await asyncio.sleep_ms(100)
            if ticks_diff(self.last_rx, tlast) > 0:  # Response received
                return True
        return False

    def disconnect(self):
        try:
            self._sock.write(b"\xe0\0")
        except OSError:
            pass
        self.close()

    def close(self):
        if self._sock is not None:
            self._sock.close()

    # qos == 1: coro blocks until wait_msg gets correct PID.
    # If WiFi fails completely subclass re-publishes with new PID.
    async def publish(self, topic, msg, retain, qos):
        if qos:
            async with self.lock_operation:
                self.pid = newpid(self.pid)
                self.rcv_pid = 0
                count = 0
                async with self.lock:
                    await self._publish(topic, msg, retain, qos, 0)
                while 1:  # Await PUBACK, republish on timeout
                    t = ticks_ms()
                    while self.pid != self.rcv_pid:
                        await asyncio.sleep_ms(200)
                        if self._timeout(t) or not self.isconnected():
                            break  # Must repub or bail out
                    else:
                        return  # PID's match. All done.
                    # No match
                    if count >= self._max_repubs or not self.isconnected():
                        raise OSError(-1)  # Subclass to re-publish with new PID
                    async with self.lock:
                        await self._publish(topic, msg, retain, qos, dup=1)
                    count += 1
                    self.REPUB_COUNT += 1
        else:
            async with self.lock:
                await self._publish(topic, msg, retain, qos, 0)

    async def _publish(self, topic, msg, retain, qos, dup):
        pkt = bytearray(b"\x30\0\0\0")
        pkt[0] |= qos << 1 | retain | dup << 3
        sz = 2 + len(topic) + len(msg)
        if qos > 0:
            sz += 2
        if sz >= 2097152:
            raise MQTTException('Strings too long.')
        i = 1
        while sz > 0x7f:
            pkt[i] = (sz & 0x7f) | 0x80
            sz >>= 7
            i += 1
        pkt[i] = sz
        await self._as_write(pkt, i + 1)
        await self._send_str(topic)
        if qos > 0:
            struct.pack_into("!H", pkt, 0, self.pid)
            await self._as_write(pkt, 2)
        await self._as_write(msg)

    # Can raise OSError if WiFi fails. Subclass traps
    async def subscribe(self, topic, qos):
        async with self.lock_operation:
            self.suback = False
            pkt = bytearray(b"\x82\0\0\0")
            self.pid = newpid(self.pid)
            struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, self.pid)
            self.pkt = pkt
            async with self.lock:
                await self._as_write(pkt)
                await self._send_str(topic)
                await self._as_write(qos.to_bytes(1, "little"))

            t = ticks_ms()
            while not self.suback:
                await asyncio.sleep_ms(200)
                if self._timeout(t):
                    raise OSError(-1)

    # Can raise OSError if WiFi fails. Subclass traps
    async def unsubscribe(self, topic):
        async with self.lock_operation:
            self.suback = False
            pkt = bytearray(b"\xa2\0\0\0")
            self.pid = newpid(self.pid)
            struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic), self.pid)
            self.pkt = pkt
            async with self.lock:
                await self._as_write(pkt)
                await self._send_str(topic)

            t = ticks_ms()
            while not self.suback:
                await asyncio.sleep_ms(200)
                if self._timeout(t):
                    raise OSError(-1)

    # Wait for a single incoming MQTT message and process it.
    # Subscribed messages are delivered to a callback previously
    # set by .setup() method. Other (internal) MQTT
    # messages processed internally.
    # Immediate return if no data available. Called from ._handle_msg().
    async def wait_msg(self):
        esp32_pause()
        res = self._sock.read(1)  # Throws OSError on WiFi fail
        if res is None:
            return
        if res == b'':
            raise OSError(-1)

        if res == b"\xd0":  # PINGRESP
            await self._as_read(1)  # Update .last_rx time
            return
        op = res[0]

        if op == 0x40:  # PUBACK: save pid
            sz = await self._as_read(1)
            if sz != b"\x02":
                raise OSError(-1)
            rcv_pid = await self._as_read(2)
            self.rcv_pid = rcv_pid[0] << 8 | rcv_pid[1]

        if op == 0x90:  # SUBACK
            resp = await self._as_read(4)
            if resp[1] != self.pkt[2] or resp[2] != self.pkt[3] or resp[3] == 0x80:
                raise OSError(-1)
            self.suback = True

        if op == 0xB0:  # UNSUBACK
            resp = await self._as_read(3)
            if resp[1] != self.pkt[2] or resp[2] != self.pkt[3]:
                raise OSError(-1)
            self.suback = True

        if op & 0xf0 != 0x30:
            return
        sz = await self._recv_len()
        topic_len = await self._as_read(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        topic = await self._as_read(topic_len)
        sz -= topic_len + 2
        if op & 6:
            pid = await self._as_read(2)
            pid = pid[0] << 8 | pid[1]
            sz -= 2
        msg = await self._as_read(sz)
        retained = op & 0x01
        self._cb(topic, msg, bool(retained))
        if op & 6 == 2:  # qos 1
            pkt = bytearray(b"\x40\x02\0\0")  # Send PUBACK
            struct.pack_into("!H", pkt, 2, pid)
            await self._as_write(pkt)
        elif op & 6 == 4:  # qos 2 not supported
            raise OSError(-1)


# MQTTClient class. Handles issues relating to connectivity.

class MQTTClient(MQTT_base):
    def __init__(self, client_id=hexlify(unique_id()),
                 server=None,
                 port=0,
                 user='',
                 password='',
                 keepalive=60,
                 ping_interval=0,
                 ssl=False,
                 ssl_params={},
                 response_time=10,
                 clean_init=True,
                 clean=True,
                 max_repubs=4,
                 will=None,
                 subs_cb=lambda *_: None,
                 wifi_coro=eliza,
                 connect_coro=eliza,
                 ssid=None,
                 wifi_pw=None):
        super().__init__(client_id, server, port, user, password, keepalive, ping_interval,
                         ssl, ssl_params, response_time, clean_init, clean, max_repubs, will,
                         subs_cb, wifi_coro, connect_coro, ssid, wifi_pw)
        self._isconnected = False  # Current connection state
        keepalive = 1000 * self._keepalive  # ms
        self._ping_interval = keepalive // 4 if keepalive else 20000
        p_i = self.ping_interval * 1000  # Can specify shorter e.g. for subscribe-only
        if p_i and p_i < self._ping_interval:
            self._ping_interval = p_i
        self._in_connect = False
        self._has_connected = False  # Define 'Clean Session' value to use.
        self._paused = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = None  # will get True once resumed

    async def wifi_connect(self):
        s = self._sta_if
        if ESP32:
            if self._paused is False:
                s.disconnect()
            elif self._paused is None:  # self._paused=True would never get here
                s.active(True)
                self._paused = False
            esp32_pause()  # Otherwise sometimes fails to reconnect and hangs
            await asyncio.sleep(1)
            s.connect(self._ssid, self._wifi_pw)
            while not s.isconnected():  # ESP32 does not yet support STAT_CONNECTING
                esp32_pause()  # https://github.com/micropython/micropython-esp32/issues/167 still seems necessary
                await asyncio.sleep(1)

        else:  # ESP8266
            if s.isconnected():  # 1st attempt, already connected.
                return
            s.active(True)
            s.connect()  # ESP8266 remembers connection.
            while s.status() == network.STAT_CONNECTING:  # Break out on fail or success. Check once per sec.
                if SONOFF:  # Without sleep_ms(10) sonoff sometimes waited so long for DHCP that
                    yield  # the wdt cut in and crashed the code.
                    sleep_ms(10)
                else:
                    await asyncio.sleep(1)  # Other platforms are OK

        # Ensure connection stays up for a few secs.
        self.dprint('Checking WiFi integrity.')
        t = ticks_ms()
        while ticks_diff(ticks_ms(), t) < 5000:
            if not s.isconnected():
                raise OSError('WiFi connection fail.')  # in 1st 5 secs
            esp32_pause()
            await asyncio.sleep(1)
        self.dprint('Got reliable connection')
        # Timed out: assumed reliable

    async def connect(self):
        if not self._has_connected:
            await self.wifi_connect()  # On 1st call, caller handles error
            # Note this blocks if DNS lookup occurs. Do it once to prevent
            # blocking during later internet outage:
            self._addr = socket.getaddrinfo(self.server, self.port)[0][-1]
        self._in_connect = True  # Disable low level ._isconnected check
        clean = self._clean if self._has_connected else self._clean_init
        await self._connect(clean)
        # If we get here without error broker/LAN must be up.
        self._isconnected = True
        self._in_connect = False  # Low level code can now check connectivity.
        loop = asyncio.get_event_loop()
        loop.create_task(self._wifi_handler(True))  # User handler.
        if not self._has_connected:
            self._has_connected = True  # Use normal clean flag on reconnect.
            loop.create_task(self._keep_connected())  # Runs forever.

        loop.create_task(self._handle_msg())  # Tasks quit on connection fail.
        loop.create_task(self._keep_alive())
        if self.DEBUG:
            loop.create_task(self._memory())
        loop.create_task(self._connect_handler(self))  # User handler.

    # Launched by .connect(). Runs until connectivity fails. Checks for and
    # handles incoming messages.
    async def _handle_msg(self):
        try:
            while self.isconnected():
                async with self.lock:
                    await self.wait_msg()  # Immediate return if no message
                await asyncio.sleep_ms(_DEFAULT_MS)  # Let other tasks get lock

        except OSError:
            pass
        self._reconnect()  # Broker or WiFi fail.

    # Keep broker alive MQTT spec 3.1.2.10 Keep Alive.
    # Runs until ping failure or no response in keepalive period.
    async def _keep_alive(self):
        while self.isconnected():
            pings_due = ticks_diff(ticks_ms(), self.last_rx) // self._ping_interval
            if pings_due >= 4:
                self.dprint('Reconnect: broker fail.')
                break
            elif pings_due >= 1:
                try:
                    await self._ping()
                except OSError:
                    break
            await asyncio.sleep(1)
        self._reconnect()  # Broker or WiFi fail.

    # DEBUG: show RAM messages.
    async def _memory(self):
        count = 0
        while self.isconnected():  # Ensure just one instance.
            await asyncio.sleep(1)  # Quick response to outage.
            count += 1
            count %= 20
            if not count:
                gc.collect()
                print('RAM free {} alloc {}'.format(gc.mem_free(), gc.mem_alloc()))

    def isconnected(self):
        if self._in_connect:  # Disable low-level check during .connect()
            return True
        if self._isconnected and not self._sta_if.isconnected():  # It's going down.
            self._reconnect()
        return self._isconnected

    def _reconnect(self):  # Schedule a reconnection if not underway.
        if self._isconnected:
            self._isconnected = False
            self.close()
            loop = asyncio.get_event_loop()
            loop.create_task(self._wifi_handler(False))  # User handler.

    # Await broker connection.
    async def _connection(self):
        while not self._isconnected:
            await asyncio.sleep(1)
            esp32_pause()  # Else sometimes fails to reconnect

    # Scheduled on 1st successful connection. Runs forever maintaining wifi and
    # broker connection. Must handle conditions at edge of WiFi range.
    async def _keep_connected(self):
        while True:
            if self._paused is True and self.isconnected() is False:  # wait until not paused, will reconnect then
                await asyncio.sleep(1)
                continue
            if self.isconnected():  # Pause for 1 second
                if self._paused is True:  # if connected and paused stop connection and disconnect wifi
                    self.disconnect()
                    await asyncio.sleep_ms(500)
                    self._sta_if.disconnect()
                    self._sta_if.active(False)
                    continue
                if SONOFF:
                    for _ in range(10):
                        sleep_ms(10)  # Prevents spurious WiFi dropouts
                        await asyncio.sleep_ms(90)
                else:
                    await asyncio.sleep(1)
                gc.collect()
            else:
                if self._paused is False:
                    self._sta_if.disconnect()
                await asyncio.sleep(1)
                try:
                    await self.wifi_connect()
                except OSError:
                    continue
                try:
                    await self.connect()
                    # Now has set ._isconnected and scheduled _connect_handler().
                    self.dprint('Reconnect OK!')
                except OSError as e:
                    self.dprint('Error in reconnect.', e)
                    # Can get ECONNABORTED or -1. The latter signifies no or bad CONNACK received.
                    self.close()  # Disconnect and try again.
                    self._in_connect = False
                    self._isconnected = False

    async def subscribe(self, topic, qos=0):
        qos_check(qos)
        while 1:
            await self._connection()
            try:
                return await super().subscribe(topic, qos)
            except OSError:
                pass
            self._reconnect()  # Broker or WiFi fail.

    async def unsubscribe(self, topic):
        while 1:
            await self._connection()
            try:
                return await super().unsubscribe(topic)
            except OSError:
                pass
            self._reconnect()  # Broker or WiFi fail.

    async def publish(self, topic, msg, retain=False, qos=0):
        qos_check(qos)
        while 1:
            await self._connection()
            try:
                return await super().publish(topic, msg, retain, qos)
            except OSError:
                pass
            self._reconnect()  # Broker or WiFi fail.
