# Run with PYTHONPATH=Python-SDK python3 misty.py
from mistyPy.GenerateRobot import RobotGenerator
from mistyPy.Robot import Robot

# Run once to update the command list. Comment this out after it succeeds.
#RobotGenerator("192.168.0.149")

misty = Robot("192.168.0.149")
import time
import random
import threading

from mistyPy.Robot import Robot
from mistyPy.Events import Events

# Spin counter-clockwise at 100% maximum speed for 3 seconds
misty.drive_time(linearVelocity=0, angularVelocity=100, timeMs=8500)

time.sleep(8.5)  # wait for the spin to finish

misty.stop()
