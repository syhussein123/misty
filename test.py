# Run with PYTHONPATH=Python-SDK python3 misty.py
from mistyPy.GenerateRobot import RobotGenerator
from mistyPy.Robot import Robot

# Run once to update the command list. Comment this out after it succeeds.
#RobotGenerator("192.168.0.149")

misty = Robot("192.168.0.149")

# Fetch all preprogrammed actions on the robot
response = misty.get_actions()
actions = response.json().get("result", [])

misty.start_action("Love")

for action in actions:
    print(f"Name: {action['name']} | Script: {action['script']}")