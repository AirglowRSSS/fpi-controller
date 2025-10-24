## Tested on
- Ubuntu 22.04 (testing on 24.04)
- Python 3.10.6 (testing on 3.12.3)

## Initial setup for Odyssey

- Install Ubuntu 22.04 with erase disk and wipe out old operating system (Windows)
- Also set Restore on AC/Power Loss to Power On in the BIOS

## Install Visual Studio Code for convinience
- From the App Center, install Visual Studio Code (search for "code")

## Setup RDP

- Install and enable remote desktop for easy remote access
  - `sudo apt update`
  - `sudo apt install xrdp`
- Verify installation:
  - `sudo systemctl status xrdp`
- If it is not running you might need to turn off Ubuntu's native RDP (which is less convenient than xrdp).
- Log out and test RDP

## Setup SSH

- Install and enable SSH server
  - `sudo apt install openssh-server`
- Verify installation:
  - `sudo systemctl status ssh`

## Add user to dialout group

- `sudo adduser airglow dialout`

## Udev rule for USB Shutter

- Install HID drivers
  - `sudo apt install libhidapi-hidraw0`
- Create a UDEV rule
  - `sudo nano /etc/udev/rules.d/99-laser-shutter.rules`
  - Content of file: `KERNEL=="hidraw*", SUBSYSTEM=="hidraw", MODE="0666"`
- Reboot system
- Verify permissions for `hidraw0`, it should be `crw-rw-rw-`
  - `ls -l /dev/`

# Setup for Raspberry Pi (for systems with filterwheel)
Install Raspberry Pi OS 32-bit

Switch from DHCPCD to NetworkManager

Enable SSH and Serial Port in Configuration

`sudo apt install xrdp`

`sudo adduser airglowrdp`

RDP to RPi need to be from a different user `airglowrdp` and not the default one `airglow`

Delete the command that use serial0 on RPi:

`sudo nano /boot/cmdline.txt`

and ONLY delete `console=serial0,115200`, keep the rest of the line unchanged

# Installing Andor SDK

- Download Andor SDK2.104.30064.0 to `~/Downloads`
- Extract and install:
  - `cd ~/Downloads`
  - `tar -xzvf andor-2.104.30064.0.tar.gz` 
  - `cd andor`
  - `sudo ./install_andor`
- install libusb:
  - `sudo apt-get install g++`
  - `sudo apt-get install libusb-dev`

Test sdk by `make` an example and try running it

- After successful installation of Andor SDK, `libandor.so` should appear in `/usr/local/lib/`. This is the shared library that we can use to call the SDK functions in Python with ctypes.

# Python

- Install latest Python (works on 3.10.6, testing on 3.12.3; may need to force to an old version if code doesn't work on latest)
  - `sudo apt-get update`
  - `sudo apt-get install python3-pip`

# Install fpi-controller code
- Install git
  - `sudo apt install git`
- Clone this repo 
  - `cd`
  - `mkdir airglow`
  - `cd airglow`
  - `git clone https://github.com/AirglowRSSS/fpi-controller.git`
- Create virtual environment
  - `cd`
  - `python -m venv airglowrsss`
  - `source airglowrsss/bin/activate`
  - `cd ~/airglow/fpi-controller`
  - `pip3 install -r requirements.txt`

## Cython for SDK

- Install dev support
  - `sudo apt-get install build-essential python3-dev`
- Build the Andor SDK wrapper
  - `cd ~/airglow/fpi-controller/components/andor_wrapper/andorsdk_wrapper` 
  - `python3 setup.py build_ext -i`
- Now you can import to python `components/andorsdk_wrapper/andorsdk`

## Setup static USB port
- Find the idProduct and idVendor for the KeoSS and RPi:
  - `udevadm info -a -n /dev/ttyUSB0 | grep ATTRS{idProduct}`
  - `udevadm info -a -n /dev/ttyUSB0 | grep ATTRS{idVendor}`
  - `udevadm info -a -n /dev/ttyUSB1 | grep ATTRS{idProduct}`
  - `udevadm info -a -n /dev/ttyUSB1 | grep ATTRS{idVendor}`
- Each gives 3 numbers, but the first one is the unique one (verify that the other two are duplicates between USB0 and USB1)
- Create /etc/udev/rules.d/99-usb-serial.rules:
  - SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="ttyKEOSS"
  - SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="ttyRPiFilter"
- Reload udev system:
  - `sudo udevadm control --reload && sudo udevadm trigger`
- Verify the /dev/ttyKEOSS points to one of the /dev/ttyUSB# and /dev/ttyRPiFilter points to another
- Update config.py in the fpi-controller directory to point to the SYMLINK devices, rather than the ttyUSB# devices.

## Setup fpi-controller configurations
- In `~/airglow/fpi-controller`
  - Make a file `config.py` following `config.py.example` to setup configuration for different sites.
  - Make a file `schedule.py` following `schedule.py.example` to setup configuration for different sites.
- Create directories referenced in config.py
  - `~/airglow/data/`
  - `~/airglow/logfiles/`
- Use `lsusb` to view the vendorId and productId of the connected laser shutter, vendorId:productId, for example 0461:0030. Write in config file as 0x0461 and 0x0030.

## Connection test

`python3 connection_test.py` to test all the components

## Set crontab
`crontab -l`
(should use link to python in the virtual environment)

## Gmail setup (optional)

Unneccessarily hard to setup for some reason. Could look into replacing it with other less complex alternative

If you want to use a different gmail account or don't have a working `gmailcredential.json` file, follows https://www.thepythoncode.com/article/use-gmail-api-in-python to Enable Gmail API. Then download `gmailcredential.json` file and specify the path in config['gmailCred'].

Run connection_test.py to check if gmail works, go to the verification link if neccessary. 

airglowuaotest@gmail.com

airglow123;
