import os
import sys
import logging
import signal
import scipy
import numpy
from time import sleep
from datetime import datetime, timedelta


from config import config, clemson5_config
from schedule import observations

import utilities.time_helper
from utilities.image_taker import Image_Helper

from components.camera import getCamera
from components.lasershutter.shutter import LaserShutter
from components.clemson5 import Clemson5
from components.skyalert import SkyAlert
from components.powercontrol import PowerControl
# from filterwheel import FilterWheel



powerControl = PowerControl()
# powerControl.turnOn(config['SkyScannerPowerPort'])
# print("turned on Clemson5")
skyscanner = Clemson5(clemson5_config['max_steps'], clemson5_config['azi_offset'], clemson5_config['zeni_offset'], clemson5_config['azi_world'], clemson5_config['zeni_world'], clemson5_config['number_of_steps'], clemson5_config['port_location'])

# skyscanner.go_home()
print("finished going home")
# skyscanner.jog(30,30,.3,.3,50)
# powerControl.turnOff(config['SkyScannerPowerPort'])


