# Misty Window Display

## Setup

Run these commands in Terminal:

git clone https://github.com/syhussein123/misty.git
cd misty
git submodule update --init --recursive
python3 -m venv .venv
source .venv/bin/activate
pip install requests “websocket-client==0.57.0” yapf

Then, with Misty on the same network:

1. Open `misty.py`, uncomment the `RobotGenerator` line, and set the IP to Misty's current address. Run it once:

PYTHONPATH=Python-SDK python3 misty.py

2. Comment that line out again.

## Running the Interaction

To run the interaction:

Run `source .venv/bin/activate` first in any new Terminal window.
