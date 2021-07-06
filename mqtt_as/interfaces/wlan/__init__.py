from .. import BaseInterface
from sys import platform
import network
import uasyncio as asyncio

ESP8266 = platform == 'esp8266'
ESP32 = platform == 'esp32'
PYBOARD = platform == 'pyboard'


class WLAN(BaseInterface):
    def __init__(self, ssid=None, wifi_pw=None):
        super().__init__()
        self.DEBUG = False
        if platform == 'esp32' or platform == 'esp32_LoBo':
            # https://forum.micropython.org/viewtopic.php?f=16&t=3608&p=20942#p20942
            self.BUSY_ERRORS += [118, 119]  # Add in weird ESP32 errors
        self._ssid = ssid
        self._wifi_pw = wifi_pw
        # wifi credentials required for ESP32 / Pyboard D. Optional ESP8266
        self._sta_if = network.WLAN(network.STA_IF)
        self._sta_if.active(True)
        if platform == "esp8266":
            import esp
            esp.sleep_type(0)  # Improve connection integrity at cost of power consumption.

    async def _connect(self):
        s = self._sta_if
        if ESP8266:
            if s.isconnected():  # 1st attempt, already connected.
                return True
            s.active(True)
            s.connect()  # ESP8266 remembers connection.
            for _ in range(60):
                if s.status() != network.STAT_CONNECTING:  # Break out on fail or success. Check once per sec.
                    break
                await asyncio.sleep(1)
            if s.status() == network.STAT_CONNECTING:  # might hang forever awaiting dhcp lease renewal or something else
                s.disconnect()
                await asyncio.sleep(1)
            if not s.isconnected() and self._ssid is not None and self._wifi_pw is not None:
                s.connect(self._ssid, self._wifi_pw)
                while s.status() == network.STAT_CONNECTING:  # Break out on fail or success. Check once per sec.
                    await asyncio.sleep(1)
        else:
            s.active(True)
            s.connect(self._ssid, self._wifi_pw)
            if PYBOARD:  # Doesn't yet have STAT_CONNECTING constant
                while s.status() in (1, 2):
                    await asyncio.sleep(1)
            else:
                while s.status() == network.STAT_CONNECTING:  # Break out on fail or success. Check once per sec.
                    await asyncio.sleep(1)

        if not s.isconnected():
            return False
        # Ensure connection stays up for a few secs.
        if self.DEBUG:
            print('Checking WiFi integrity.')
        for _ in range(5):
            if not s.isconnected():
                return False  # in 1st 5 secs
            await asyncio.sleep(1)
        if self.DEBUG:
            print('Got reliable connection')
        return True

    async def _disconnect(self):
        self._sta_if.disconnect()

    def _isconnected(self):
        return self._sta_if.isconnected()