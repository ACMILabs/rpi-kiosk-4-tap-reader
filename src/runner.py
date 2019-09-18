#!/usr/bin/python

""" Kiosk IV tap reader running on a Raspberry Pi """

import os
import re
import subprocess
import sys
from datetime import datetime
from threading import Timer, Thread, Event
from time import sleep, time
from copy import deepcopy

import requests
import sentry_sdk
from utils import IP_ADDRESS, IS_OSX, MAC_ADDRESS, TZ, envtotuple, log

try:
  import adafruit_dotstar as dotstar
except ModuleNotFoundError: # this doesn't compile install
  dotstar = None

try:
  import board
except (NotImplementedError, ModuleNotFoundError):
  board = None


# Constants defined in environment
DEBUG = os.getenv('DEBUG', '').lower() == "true"
XOS_URL = os.getenv('XOS_URL', 'http://localhost:8888')
AUTH_TOKEN = os.getenv('AUTH_TOKEN', '')
LABEL = os.getenv('LABEL')
SENTRY_ID = os.getenv('SENTRY_ID')
TAP_OFF_TIMEOUT = float(os.getenv('TAP_OFF_TIMEOUT', '0.5')) # seconds
DEVICE_NAME = os.getenv('DEVICE_NAME')
READER_MODEL = os.getenv('READER_MODEL')
READER_NAME = DEVICE_NAME or 'nfc-' + IP_ADDRESS.split('.')[-1]

LEDS_COLOUR_DEFAULT = envtotuple('LEDS_COLOR_DEFAULT', "130,127,127")  # White
LEDS_COLOUR_SUCCESS = envtotuple('LEDS_COLOUR_SUCCESS', "0,255,0")  # Green
LEDS_COLOUR_FAILED = envtotuple('LEDS_COLOUR_FAILED', "255,0,0")  # Red
LEDS_DEFAULT_BRIGHTNESS = float(os.getenv('LEDS_DEFAULT_BRIGHTNESS', '1.0'))

if IS_OSX:
  FOLDER = "../bin/mac/"
else:
  FOLDER = "../bin/arm_32/"

if DEBUG:
  CMD = ["./idtech_debug"]
else:
  CMD = ["./idtech"]

# Setup Sentry
sentry_sdk.init(SENTRY_ID)



class RampThread(Thread):
  """Thread class with a stop() method. The thread itself has to check
  regularly for the stopped() condition."""

  @staticmethod
  def ease(t, b, c, d):
    # Penner's easeInCubic
    # t: current time, b: beginning value, c: change in value, d: duration
    t = t / d
    r = c * t * t * t + b
    # import pdb; pdb.set_trace()
    return r

  def __init__(self, current_color, target_color, duration_s):
    super(RampThread, self).__init__()
    self._stop_event = Event()
    self.current_color = current_color
    self.target_color = target_color
    self.duration_s = duration_s

    if board and dotstar:
      self.LEDS = dotstar.DotStar(board.SCK, board.MOSI, 12, brightness=LEDS_DEFAULT_BRIGHTNESS)
    else:
      self.LEDS = None


  def set_leds(self, color):
    if self.LEDS:
      self.LEDS.fill((*color, LEDS_DEFAULT_BRIGHTNESS))
    else:
      print("Setting LEDs to %s" % list(color))

  def stop(self):
      self._stop_event.set()

  def stopped(self):
      return self._stop_event.is_set()

  def run(self):
    print("Ramping from %s to %s" % (self.current_color, self.target_color))
    start_color = deepcopy(self.current_color)
    diff = (
      self.target_color[0]-start_color[0],
      self.target_color[1]-start_color[1],
      self.target_color[2]-start_color[2],
    )
    t0 = time()
    while True:
      t = time() - t0
      if self.stopped():
        print("LED thread cancelled")
        break
      if t >= self.duration_s:
        # lock to exact target and end the thread
        self.set_leds(self.target_color)
        break

      for i in range(3):
        self.current_color[i] = int(RampThread.ease(t, start_color[i], diff[i], self.duration_s))
      # output the colours
      self.set_leds(self.current_color)
      sleep(1.0/60)

class LedsManager:
  """
  LedsManager manages the state of the reader's LEDs.
  """

  thread = None
  current_color = [0,0,0]

  def __init__(self):
    # LED init
    # Using a DotStar Digital LED Strip with 12 LEDs connected to hardware SPI
    self.fill_default()

  def ramp(self, target_color, duration_s):
    # in a thread, ramp from the current colour to the target_color colour, in 'duration' seconds
    if self.thread:
      self.current_color = self.thread.current_color
      self.thread.stop()
      self.thread.join()
    self.thread = RampThread(self.current_color, target_color, duration_s)
    self.thread.start()

  def fill_default(self):
    self.ramp(LEDS_COLOUR_DEFAULT, 1.0)

  def success_on(self):
    self.ramp(LEDS_COLOUR_SUCCESS, 0.6)

  def success_off(self):
    self.ramp(LEDS_COLOUR_DEFAULT, 0.6)

  def failed(self):
    self.ramp(LEDS_COLOUR_FAILED, 0.6)
    sleep(1.0)
    self.ramp(LEDS_COLOUR_DEFAULT, 0.6)


class TapManager:
  """
  TapManager processes continuous messages from the idtech C code, in order to detect tap on and tap off events.
  Tap On events send a message to a nominated XOS address, and light up the reader's LEDs.
  Tap Off events are currently only logged.
  """
  def __init__(self):
    # Tap init
    self.last_id = None
    self.last_id_time = time()
    self.tap_off_timer = None
    self.leds = LedsManager()

  def send_tap(self, id):
    """refer to docs.python-requests.org for implementation examples"""
    params={
      'nfc_tag': {
        'uid':id
      },
      'tap_datetime': datetime.now(TZ).isoformat(),
      'label': LABEL,
      'data': {
        'nfc_reader': {
          'mac_address': MAC_ADDRESS,
          'reader_ip': IP_ADDRESS,
          'reader_model': READER_MODEL,
          'reader_name': READER_NAME,
        }
      }
    }
    headers={'Authorization': 'Token ' + AUTH_TOKEN}
    try:
      r=requests.post(url=XOS_URL, json=params, headers=headers)
      if r.status_code==201:
        log(r.text)
        return(0)
      else:
        log(r.text)
        sentry_sdk.capture_message(r.text)
        return(1)
    except requests.exceptions.ConnectionError as e:
      log("Failed to post tap message to %s: %s\n%s" % (XOS_URL, params, str(e)))
      sentry_sdk.capture_exception(e)
      return(1)

  def tap_on(self):
    log(" Tap On: ", self.last_id)
    self.leds.success_on()
    self.send_tap(self.last_id)

  def tap_off(self):
    log("Tap Off: ", self.last_id)
    self.last_id = None
    self.leds.success_off()

  def _reset_tap_off_timer(self):
    # reset the tap-off timer for this ID
    if self.tap_off_timer:
      self.tap_off_timer.cancel()
      del self.tap_off_timer
    self.tap_off_timer = Timer(TAP_OFF_TIMEOUT, self.tap_off)
    self.tap_off_timer.start()

  def _byte_string_to_lens_id(self, byte_string):
    """
    Convert an id of the form 04:04:A5:2C:F2:2A:5E:80 (as output from the C app) to 04a52cf22a5e80 by:
    - stripping ":" and whitespace
    - removing first byte
    - lowercasing
    """
    return "".join(byte_string.strip().split(":")[1:]).lower()

  def read_line(self, line):
    """
    This is called continuously while an NFC tag is present.
    """
    id = self._byte_string_to_lens_id(line)
    if id != self.last_id:
      # send a tap-off message if needed
      if self.tap_off_timer and self.tap_off_timer.is_alive():
        self.tap_off_timer.cancel()
        self.tap_off()
      self.last_id = id
      self.tap_on()
    self._reset_tap_off_timer()


def byte_string_to_lens_id(byte_string):
  """
  Convert 04:04:A5:2C:F2:2A:5E:80 to 04a52cf22a5e80 by:
  - stripping ":" and whitespace
  - removing first byte
  - lowercasing
  """
  return "".join(byte_string.strip().split(":")[1:]).lower()


def main():
  """Launcher."""
  print("XOS Lens Reader (KioskIV)")

  shell = True
  popen = subprocess.Popen(CMD, cwd=FOLDER, shell=shell, stdout=subprocess.PIPE)

  tap_manager = TapManager()
  BYTE_STRING_RE = r'([0-9a-fA-F]{2}:?)+'

  while True:
    # wait for the next line from the C interface (if there are no NFCs there will be no lines)
    line = popen.stdout.readline().decode("utf-8")

    # see if it is a tag read
    if re.match(BYTE_STRING_RE, line):
      # We have an ID.
      tap_manager.read_line(line)

if __name__ == "__main__":
  main()
