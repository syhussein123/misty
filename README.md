# Misty Window Display

`misty.py` uses person detection to admit one interaction per visit. A person
must remain visible for 1.2 seconds before engagement. A missed card read opens
another scanning turn in the same visit, without repeating the greeting.
Misty waits for the scene to be empty for 4 seconds before a new arrival can
start another interaction. The first person observed at startup
counts as an arrival. This is scene occupancy, not individual identification:
another person entering an already occupied scene does not start a new visit.

## Setup

```sh
git submodule update --init --recursive
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install the requirements with pip, including when using a Conda-managed
Python. The pinned `websocket-client==0.57.0` is available from PyPI but may
not be available in Conda's `defaults` channel. For an IDE, select this
repository's `.venv/bin/python` as the interpreter.

Misty must be on the same network. The pinned SDK already contains the APIs
used here. If another firmware requires regenerating the SDK, run the generator
separately (it rewrites generated SDK files):

```sh
PYTHONPATH=Python-SDK python3 -c 'from mistyPy.GenerateRobot import RobotGenerator; RobotGenerator("192.168.0.149")'
```

## Run

```sh
PYTHONPATH=Python-SDK python3 -u misty.py --ip 192.168.0.149
```

Use Ctrl+C to stop. Startup checks subscriptions, reads the asset inventory,
starts person and AR detection, and resets the head/arms. Watch for
`Window-display Misty is ready` in the log. Do not run `AR.py`, `old.py`, or
`test.py` alongside it: they independently control the same robot. `test.py` is
the built-in-action inspection/demo script, not an automated test suite.

To inspect relevant assets without moving the robot:

```sh
PYTHONPATH=Python-SDK python3 misty.py --inventory
```

## Interaction and configuration

The flow is notice → dwell → engagement → visual invitation → wait →
acknowledgement/nod → processing → confirmation → activity → continue cue.
Unsupported detected AR IDs trigger one repair per waiting round. There is one
reminder after 5 seconds and a 10-second initial input window. If no card is
read, Misty says she will scan again and keeps accepting cards quietly for as
long as the visitor remains. She does not say goodbye just because a read was
missed. Walking away causes a silent reset. Activities can repeat while the
visitor remains engaged; there is no two-round limit. Rescanning is automatic;
the program does not listen for spoken commands.

AR selection is unchanged: dictionary **7**, printed size **100 mm**,
**1 = rotating dance with music**, **2 = music**, **3 = joke**. Remove a selected card for at least
one second before presenting the same card again. Cards are monitored outside
the waiting phase to suppress a card held through an activity. An unreadable
card cannot produce an invalid-tag event and follows the no-input path.

- `--silent-prompts`: omit spoken dialogue prompts while retaining gaze,
  expressions, gestures, music, and the selected joke's speech.
- `--leave-seconds 4`: continuous absence required to re-arm.
- `--card-yaw -18 --card-pitch 8`: direction of the physical card area. Positive
  yaw is Misty's left; positive pitch looks down. Calibrate for your window.
- `--dance-skill GUID`: use a specifically selected installed skill in place
  of the default dance. The default combines the latest 8.55-second clockwise
  rotation (`angularVelocity=-100`, no forward motion), the head/arm choreography from
  `old.py`, and a song. `Settings.dance_rotation_seconds` and
  `dance_angular_velocity` preserve that original timing and speed; calibrate
  them if needed to finish facing the window. A selected installed skill
  should restore that pose too and provide its own audio if desired.

`Settings` contains confidence, dwell, timeouts, and detector image dimensions.
The default model is 0 and bounding boxes are interpreted in its 320 × 320
image coordinates. Check event payloads if using another model/firmware.
Head gaze uses actuator positions plus bounded corrections toward the upper
part of the selected person's bounding box. Overlapping boxes preserve a
recent target; this is a lightweight heuristic, not identity tracking. Body-box
size is not interpreted as a calibrated physical distance.

`FACE_IMAGES` maps cues to installed image names. The default songs are
`baby.mp3` and `beautiful.mp3`, both present on the checked robot. Startup
integrates `music.py`'s upload logic: missing songs are uploaded from this
project without overwriting or immediately playing them. `--inventory` remains
read-only. Card 1 plays a song while the body rotates and the head/arms dance,
then stops both motion and music. Card 2 waits for the matching
`AudioPlayComplete` event, with a 60-second activity timeout. Card 3 uses the
three paired robot jokes from the newer GitHub revision, including social gaze
and the closing laugh. The legacy scripts are not imported or launched separately.
Missing assets and robot errors are logged rather than treated as success.

The newer GitHub speech settings are preserved: volume 50 and speech rate
1.75. When available in the robot's action inventory, startup briefly runs
`cute`, and music playback runs `Love`. Actions are stopped before scripted
head commands resume. Missing actions fall back to the scripted cues. These
names and speech settings are configurable in `Settings`.

## Concurrency and presence limits

Only the behavior loop sends commands. SDK callbacks update locked state;
there is no competing gaze or greeting thread. Each occupied-scene episode
has an ID that can be claimed once. Intentional look-away and dance poses
suspend absence inference, then restore the forward pose and allow
reacquisition. Card-release inference is suspended across these interruptions
as well. Short detection gaps do not create new visits.
Ordinary gaze movements and the final return to neutral also allow time for
the camera to settle before counting the full absence interval.

A closed event connection stops the program. Because object events are emitted
on detection, prolonged missed detections or a silently stalled vision service
can still resemble absence. Verify confidence, detector throughput, AR
detection, and the leave interval through the actual window before a study.

## Tests without the robot

```sh
python3 -B -m unittest discover -s tests -v
```

These tests use simulated observations, clocks, and robot responses. Importing
`misty.py` has no SDK dependency and does not connect to or move hardware.
