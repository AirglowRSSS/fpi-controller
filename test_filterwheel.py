import os
import sys
import logging
import signal
import scipy
import numpy as np
import pickle
from time import sleep
from datetime import datetime, timedelta
import smtplib, ssl
from config import config, skyscan_config, filterwheel_config
from schedule import observations
import ephem
import time

import utilities.time_helper
from utilities.image_taker import Image_Helper
from utilities.send_mail import SendMail

from components.camera import getCamera
from components.shutterhid import HIDLaserShutter
#from components.sky_scanner import SkyScanner
#from components.sky_scanner_keo import SkyScanner
from components.skyalert import SkyAlert
from components.powercontrol import PowerControl
from components.filterwheel import FilterWheel

skyscanner = True

if skyscan_config['type'] == 'KEO':
    from components.sky_scanner_keo import SkyScanner
elif skyscan_config['type'] == 'Clemson':
    from components.sky_scanner import SkyScanner
else:
    skyscanner = False
    print("this is what I did")
    from components.clemson5 import Clemson5

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

# Choose what to test. Select from:
#   CCD - Andor CCD
#   SkyScanner - SkyScanner
#   FilterWheel - Filterwheel
#   Sequence - Goes through a schedule sequence to check pointing to Cardinal, Laser, and FilterWheel
#   Sun - Points the SS to the sun
#   SkyAlert - Tests cloud sensor
#   gmail - Test the gmail connection
#   LaserShutter - Open/Closes Laser Shutter
#what_to_test = ['CCD','SkyScanner','FilterWheel', 'LaserShutter', 'Sequence', 'Sun', 'SkyAlert', 'gmail']

# What has been tested at LOW by JJM on June 13, 2023:
#	CCD
# 	FilterWheel
#	SkyAlert
#	LaserShutter
#	SkyScanner
#	Sun
#	Sequence
# 	gmail

#what_to_test = ['LaserShutter']
#what_to_test = ['SkyAlert']
#what_to_test = ['Sequence']
#what_to_test = ['SkyScanner']
what_to_test = ['FilterWheel']

powerControl = PowerControl(config['powerSwitchAddress'], config['powerSwitchUser'], config['powerSwitchPassword'])

if 'FilterWheel' in what_to_test:
    powerControl.turnOn(config['FilterWheelPowerPort'])
    logging.info('Initializing FilterWheel')
    fw = FilterWheel(ip_address=filterwheel_config['ip_address'])
##    fw = FilterWheel(filterwheel_config['port_location'])
    logging.info('Homing Filterwheel')
    fw.home()
    logging.info('Going to positiion 2')
    fw.go(2)
    logging.info('Going to positiion 3')
    fw.go(3)
    logging.info('Going to positiion 0')
    fw.go(0)
    logging.info('Going to positiion 3')
    fw.go(1)
    logging.info('Turning off FilterWheel')
    powerControl.turnOff(config['FilterWheelPowerPort'])
