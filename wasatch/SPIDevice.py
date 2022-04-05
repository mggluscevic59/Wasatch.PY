import re
import os
import usb
import time
import json
import queue
import struct
import logging
import datetime

# Needed for Mac side
# Device Finder should already have done this
# For thoroughness though doing here anyway
# This is required for finding the usb <-> serial board
import usb.core
usb.core.find()

from .SpectrometerSettings        import SpectrometerSettings
from .SpectrometerState           import SpectrometerState
from .ControlObject               import ControlObject
from .DeviceID                    import DeviceID
from .Reading                     import Reading
from .EEPROM                      import EEPROM

log = logging.getLogger(__name__)

class SPIDevice:

    INTEGRATION_ADDRESS = 0x11

    def __init__(self, device_id, message_queue):
        # if passed a string representation of a DeviceID, deserialize it
        try:
            import board
            import time
            import board
            import digitalio
            import busio
        except Exception as e:
            log.error(f"Problem importing board for SPI device of {e}")

        if type(device_id) is str:
            device_id = DeviceID(label=device_id)

        self.device_id      = device_id
        self.message_queue  = message_queue

        self.connected = False
        self.disconnect = False
        self.acquiring = False

        # Receives ENLIGHTEN's 'change settings' commands in the spectrometer
        # process. Although a logical queue, has nothing to do with multiprocessing.
        self.command_queue = []

        self.immediate_mode = False

        self.settings = SpectrometerSettings(self.device_id)
        self.summed_spectra         = None
        self.sum_count              = 0
        self.session_reading_count  = 0
        self.take_one               = False
        self.failure_count          = 0

        self.process_id = os.getpid()
        self.last_memory_check = datetime.datetime.now()
        self.last_battery_percentage = 0
        self.lambdas = None
        self.init_lambdas()
        self.spec_index = 0 
        self._scan_averaging = 1
        self.dark = None
        self.boxcar_half_width = 0

        # Initialize the SPI bus on the FT232H
        self.SPI  = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)

        # Initialize D5 as the ready signal
        self.ready = digitalio.DigitalInOut(board.D5)
        self.ready.direction = digitalio.Direction.INPUT

        # Initialize D6 as the trigger
        self.trigger = digitalio.DigitalInOut(board.D6)
        self.trigger.direction = digitalio.Direction.OUTPUT
        self.trigger.value = False

        # Take control of the SPI Bus
        while not self.SPI.try_lock():
            pass

        # Configure the SPI bus
        self.SPI.configure(baudrate=20000000, phase=0, polarity=0, bits=8)

    def connect(self):
        eeprom_pages = []
        for i in range(EEPROM.MAX_PAGES):
            response = bytearray(2)
            while self.ready.value:
                self.SPI.readinto(response,0,2)
            page = self.EEPROMReadPage(i)
            log.info(f"spi read page {i} with data {page}")
            eeprom_pages.append(page)
        eeprom_pages = [bytearray([val for val in page[9:74]]) for page in eeprom_pages]
        self.settings.eeprom.parse(eeprom_pages)
        self.settings.eeprom.active_pixels_horizontal = 1952
        self.settings.state.integration_time_ms = 10
        return True

    def disconnect(self):
        self.disconnect = True
        return True

    def acquire_data(self):
        log.debug("spi starts reading")
        if self.disconnect:
            log.debug("disconnecting, returning False for the spectrum")
            return False
        averaging_enabled = (self.settings.state.scans_to_average > 1)
        reading = Reading(self.device_id)

        try:
            reading.integration_time_ms = self.settings.state.integration_time_ms
            reading.laser_power_perc    = self.settings.state.laser_power_perc
            reading.laser_power_mW      = self.settings.state.laser_power_mW
            reading.laser_enabled       = self.settings.state.laser_enabled
            reading.spectrum            = self.Acquire()
            if reading.spectrum == False:
                return False
        except usb.USBError:
            self.failure_count += 1
            log.error(f"SPI Device: encountered USB error in reading for device {self.device}")

        if not reading.failure:
            if averaging_enabled:
                if self.sum_count == 0:
                    self.summed_spectra = [float(i) for i in reading.spectrum]
                else:
                    log.debug("device.take_one_averaged_reading: summing spectra")
                    for i in range(len(self.summed_spectra)):
                        self.summed_spectra[i] += reading.spectrum[i]
                self.sum_count += 1
                log.debug("device.take_one_averaged_reading: summed_spectra : %s ...", self.summed_spectra[0:9])

        self.session_reading_count += 1
        reading.session_count = self.session_reading_count
        reading.sum_count = self.sum_count

        return reading

    def write_eeprom(self):
        try:
            self.settings.eeprom.generate_write_buffers()
        except:
            log.critical("failed to render EEPROM write buffers", exc_info=1)
            #self.message_queue("marquee_error", "Failed to write EEPROM")
            return False

        for page in range(EEPROM.MAX_PAGES):
            self.EEPROMWritePage(page,self.settings.eeprom.write_buffers[page])

        #self.message_queue("marquee_info", "EEPROM successfully updated")
        return True

    def EEPROMReadPage(self, page):
        EEPROMPage  = bytearray(74)
        command     = bytearray(7)
        command     = [0x3C, 0x00, 0x02, 0xB0, (0x40 + page), 0xFF, 0x3E]
        self.SPI.write(command, 0, 7)
        time.sleep(0.01)
        command = [0x3C, 0x00, 0x01, 0x31, 0xFF, 0x3E]
        self.SPI.write_readinto(command, EEPROMPage)
        return EEPROMPage

    def set_integration_time_ms(self, value):
        while self.acquiring:
            time.sleep(0.01)
            continue
        self.SPIWrite(value, self.INTEGRATION_ADDRESS)
        self.settings.state.integration_time_ms = value

    def SPIWrite(self, value, address):
        command = bytearray(8)
        # Convert the int into bytes.
        txData = bytearray(2)
        txData[1]   = value >> 8
        txData[0]   = value - (txData[1] << 8)
        # A write command consists of opening and closing delimeters, the payload size which is data + 1 (for the command byte),
        # the command/address with the MSB set for a write operation, the payload data, and the CRC. This function does not 
        # caluculate the CRC nor read back the status.
        # Refer to ENG-150 for additional information
        command = [0x3C, 0x00, 0x03, (address+0x80), txData[0], txData[1], 0xFF, 0x3E]
        self.SPI.write(command, 0, 8)
        time.sleep(0.01)

    def EEPROMWritePage(self, page, write_array):
        #write_array = [str(item) for item in write_array]
        command     = bytearray(7)
        EEPROMWrCmd = bytearray(70)
        EEPROMWrCmd[0:3] = [0x3C, 0x00, 0x41, 0xB1]
        try:
            for x in range(0, 64):
                log.info(f"spi writing to page {page} with value {write_array[x]}")
                EEPROMWrCmd[x+4] = write_array[x]
        except Exception as e:
            log.error(f"spi failed to write value of {write_array[x]} to page {page}. had exception {e}")
            raise e

        EEPROMWrCmd[68] = 0xFF
        EEPROMWrCmd[69] = 0x3E
        self.SPI.write(EEPROMWrCmd, 0, 70)
        command = [0x3C, 0x00, 0x02, 0xB0, (0x80 + page), 0xFF, 0x3E]
        self.SPI.write(command, 0, 7)
        time.sleep(0.1)

    def change_setting(self,setting,value):
        log.info(f"spi being told to change setting {setting} to {value}")
        f = self.lambdas.get(setting,None)
        if f is not None:
            f(value)
        return True

    def Acquire(self):
        if self.disconnect:
            return False
        self.acquiring = True
        log.debug("calling acquire")
        SPIBuf  = bytearray(2)
        spectra = []
        # Send and acquire trigger
        self.trigger.value = True

        # Wait until the data is ready
        log.debug("waiting for data to be ready")
        while not self.ready.value:
            #log.debug("spi waiting")
            pass
        log.debug("data is ready")

        # Relase the trigger
        self.trigger.value = False

        # Read in the spectra
        log.debug("reading spectra pixels")
        while self.ready.value:
            self.SPI.readinto(SPIBuf, 0, 2)
            pixel = (SPIBuf[0] << 8) + SPIBuf[1]
            spectra.append(pixel)

        log.debug(f"returning spectra of length ({len(spectra)})")
        self.acquiring = False
        return spectra

    def init_lambdas(self):
        f = {}

        f["write_eeprom"]                       = lambda x: self.write_eeprom()
        f["replace_eeprom"]                     = lambda x: self.write_eeprom()
        f["integration_time_ms"]                = lambda x: self.set_integration_time_ms(x)

        self.lambdas = f
