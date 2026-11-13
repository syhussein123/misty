"""Window interaction. Run with PYTHONPATH=Python-SDK python3 misty.py.

Only the main behavior loop sends robot commands. SDK callbacks only update
state under a lock. Importing this module never connects to or moves the robot.
"""

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import random
import signal
import threading
import time


LOG = logging.getLogger("misty")
TAG_FOR_ACTIVITY = {1: "dance", 2: "sing", 3: "joke"}
AR_DICTIONARY = 7
TAG_SIZE_MM = 100
JOKES = (
    ("Why did the robot go on a diet?", "Because it had too many bytes!"),
    ("Why was the robot so bad at soccer?", "Because it kept kicking up sparks!"),
    ("Why did the robot cross the road?", "To get to the other side!"),
)
FACE_IMAGES = {
    "idle": "e_DefaultContent.jpg",
    "friendly": "e_Joy.jpg",
    "happy": "e_Joy2.jpg",
    "thinking": "eyes_pensiveness.jpg",
    "confused": "e_Disoriented.jpg",
    "goodbye": "e_Joy.jpg",
}


@dataclass(frozen=True)
class Settings:
    leave_seconds: float = 4.0
    dwell_seconds: float = 1.2
    fresh_seconds: float = 1.0
    reacquire_seconds: float = 1.0
    card_wait_seconds: float = 10.0
    card_reminder_seconds: float = 5.0
    card_release_seconds: float = 1.0
    confidence: float = 0.6
    detector_width: float = 320.0
    detector_height: float = 320.0
    card_yaw: float = -18.0
    card_pitch: float = 8.0
    spoken_prompts: bool = True
    speech_volume: int = 50
    speech_rate: float = 1.75
    songs: tuple = ("baby.mp3", "beautiful.mp3")
    dance_skill: str = ""
    dance_rotation_seconds: float = 8.55
    dance_angular_velocity: float = -100.0
    startup_action: str = "cute"
    music_action: str = "Love"
    activity_timeout: float = 60.0


class VisitPresence:
    """One claim per occupied-scene episode; caller holds the state lock.

    A face turning away cannot end a visit: only person observations are used.
    Short gaps keep the same epoch. Long gaps create a new epoch even when a
    new observation arrives before the behavior loop has checked for absence.
    """

    def __init__(self, settings):
        self.settings = settings
        self.last_seen = None
        self.visible_since = None
        self.occupied = False
        self.epoch = 0
        self.claimed_epoch = 0
        self.paused = 0
        self.observe_after = 0.0

    def expire(self, now):
        if (self.occupied and not self.paused and
                now - max(self.last_seen, self.observe_after) >= self.settings.leave_seconds):
            self.occupied = False

    def observe(self, now):
        self.expire(now)
        if not self.occupied:
            self.epoch += 1
            self.occupied = True
        if self.last_seen is None or now - self.last_seen > self.settings.fresh_seconds:
            self.visible_since = now
        self.last_seen = now

    def claim(self, now):
        self.expire(now)
        if self.occupied and self.epoch != self.claimed_epoch:
            self.claimed_epoch = self.epoch
            return self.epoch
        return None

    def present(self, epoch, now):
        self.expire(now)
        return self.occupied and self.epoch == epoch

    def ready(self, epoch, now):
        return (self.present(epoch, now) and
                now - self.last_seen <= self.settings.fresh_seconds and
                now - self.visible_since >= self.settings.dwell_seconds)

    def resume(self, now):
        self.paused -= 1
        if not self.paused:
            # A deliberate look-away is not evidence that the person left.
            self.observe_after = now + self.settings.reacquire_seconds


class CardInput:
    """A single selection per waiting round; caller holds the state lock."""

    def __init__(self, release_seconds):
        self.release_seconds = release_seconds
        self.last_seen = {}
        self.consumed = set()
        self.epoch = None
        self.deadline = 0.0
        self.accepting = False
        self.pending = None

    def begin(self, epoch, deadline):
        self.epoch, self.deadline = epoch, deadline
        self.pending = None
        self.accepting = True

    def close(self):
        self.accepting = False
        self.pending = None

    def observe(self, tag, now, epoch, present, reliable_view=True):
        previous = self.last_seen.get(tag)
        if (reliable_view and previous is not None and
                now - previous >= self.release_seconds):
            self.consumed.discard(tag)
        self.last_seen[tag] = max(now, previous if previous is not None else now)
        if (not self.accepting or not present or epoch != self.epoch or
                now >= self.deadline or tag in self.consumed):
            return
        self.pending = ("valid" if tag in TAG_FOR_ACTIVITY else "invalid", tag)
        self.consumed.add(tag)
        self.accepting = False

    def resumed_view(self, now, grace):
        # Losing the card while our own camera turns is not card removal.
        for tag in self.consumed:
            self.last_seen[tag] = max(self.last_seen.get(tag, now), now + grace)


def message(data):
    if not isinstance(data, dict):
        return {}
    value = data.get("message", data.get("Message", {}))
    return {str(k).lower(): v for k, v in value.items()} if isinstance(value, dict) else {}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def clamp(value, low, high):
    return max(low, min(high, value))


def response_result(response, allow_false=False):
    """SDK commands return HTTP responses, including on robot-side failure."""
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("Unexpected robot response")
    status = body.get("status", body.get("Status", "Success"))
    result = body.get("result", body.get("Result"))
    if str(status).lower() != "success" or (result is False and not allow_false):
        raise RuntimeError("Robot rejected command: %s" % body)
    return result


class VisitorLeft(Exception):
    pass


class Stopping(Exception):
    pass


class Interaction:
    def __init__(self, robot, settings=None, clock=time.monotonic):
        self.robot = robot
        self.settings = settings or Settings()
        self.clock = clock
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.presence = VisitPresence(self.settings)
        self.cards = CardInput(self.settings.card_release_seconds)
        self.active_epoch = None
        self.target = None
        self.head = {"pitch": 0.0, "yaw": 0.0}
        self.last_gaze_time = -math.inf
        self.last_gaze_frame = None
        self.audio_expected = None
        self.audio_complete = False
        self.speech_expected = None
        self.speech_complete = False
        self.speech_sequence = 0
        self.images = set()
        self.songs = []
        self.skills = []
        self.running_skill = None
        self.actions = {}
        self.running_action = None
        self.registrations = []
        self.detectors = []
        self.stage = "IDLE"

    def command(self, name, *args, **kwargs):
        # Stop endpoints may report False when nothing was running. HTTP/API
        # errors still fail; an already stopped activity is a successful reset.
        idempotent = name in {"stop", "stop_audio", "stop_speaking", "cancel_skill",
                              "stop_object_detector", "stop_ar_tag_detector", "stop_action"}
        return response_result(getattr(self.robot, name)(*args, **kwargs),
                               allow_false=idempotent)

    def best_effort(self, name, *args, **kwargs):
        try:
            self.command(name, *args, **kwargs)
        except Exception as exc:
            LOG.warning("%s failed: %s", name, exc)

    def set_stage(self, stage):
        if stage != self.stage:
            self.stage = stage
            LOG.info("State: %s (visit %s)", stage, self.active_epoch)

    def person_callback(self, data):
        msg = message(data)
        confidence = number(msg.get("confidence"))
        if (str(msg.get("description", "")).lower() != "person" or
                confidence is None or confidence < self.settings.confidence):
            return
        box = tuple(number(msg.get("imagelocation" + side))
                    for side in ("left", "top", "right", "bottom"))
        if any(value is None for value in box):
            return
        left, top, right, bottom = box
        if not (left < right and top < bottom):
            return
        # Live detector boxes can cross the image edge (e.g. left=-0.027).
        # Clip the visible intersection instead of declaring that person gone.
        left, right = max(0, left), min(self.settings.detector_width, right)
        top, bottom = max(0, top), min(self.settings.detector_height, bottom)
        if not (left < right and top < bottom):
            return
        box = (left, top, right, bottom)
        now = self.clock()
        with self.lock:
            self.presence.observe(now)
            # Keep a recently observed target when another person is elsewhere.
            if self.target and now - self.target["seen"] < self.settings.fresh_seconds:
                old = self.target["box"]
                overlap = (min(right, old[2]) > max(left, old[0]) and
                           min(bottom, old[3]) > max(top, old[1]))
                if not overlap:
                    return
            self.target = {"box": box, "seen": now, "head": dict(self.head)}

    def tag_callback(self, data):
        value = number(message(data).get("tagid"))
        if value is None or value < 0 or not value.is_integer():
            return
        now = self.clock()
        with self.lock:
            self.cards.observe(int(value), now, self.active_epoch,
                               self.presence.present(self.active_epoch, now),
                               reliable_view=(not self.presence.paused and
                                              now >= self.presence.observe_after))

    def actuator_callback(self, data):
        msg = message(data)
        axis = {"ahy": "yaw", "ahp": "pitch"}.get(msg.get("sensorid"))
        value = number(msg.get("value"))
        if axis and value is not None:
            with self.lock:
                self.head[axis] = value

    def audio_callback(self, data):
        msg = message(data)
        metadata = msg.get("metadata")
        name = msg.get("filename", msg.get("name"))
        if isinstance(metadata, dict):
            metadata = {str(k).lower(): v for k, v in metadata.items()}
            name = metadata.get("name", name)
        with self.lock:
            if name and name == self.audio_expected:
                self.audio_complete = True

    def speech_callback(self, data):
        msg = message(data)
        with self.lock:
            if msg.get("utteranceid") == self.speech_expected and self.speech_expected:
                self.speech_complete = True

    def checkpoint(self):
        if self.stop_event.is_set():
            raise Stopping()
        # A closed input connection is not evidence that the visitor left.
        if any(not registration.is_active for registration in self.registrations):
            self.stop_event.set()
            raise RuntimeError("An event connection closed; stopping instead of inferring departure")
        with self.lock:
            if (self.active_epoch is not None and
                    not self.presence.present(self.active_epoch, self.clock())):
                raise VisitorLeft()

    def pause(self, seconds, track=False):
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            self.checkpoint()
            if track:
                self.look_toward_person()
            self.stop_event.wait(min(0.1, max(0, deadline - self.clock())))
        self.checkpoint()

    def move_head(self, pitch=0, roll=0, yaw=0, duration=0.4):
        self.checkpoint()
        pitch, yaw = clamp(pitch, -15, 15), clamp(yaw, -25, 25)
        self.command("move_head", pitch=pitch, roll=clamp(roll, -10, 10),
                     yaw=yaw, duration=duration, units="degrees")
        with self.lock:
            self.head.update(pitch=pitch, yaw=yaw)
            # Even ordinary social gaze moves the camera. Count absence only
            # after the commanded motion has settled and vision can reacquire.
            self.presence.observe_after = max(
                self.presence.observe_after,
                self.clock() + duration + self.settings.reacquire_seconds)

    def move_arms(self, left, right, duration=0.5):
        self.checkpoint()
        self.command("move_arms", left, right, duration=duration)
        self.pause(duration)

    def show_face(self, name):
        filename = FACE_IMAGES[name]
        if filename in self.images:
            self.command("display_image", filename)

    def look_toward_person(self):
        now = self.clock()
        with self.lock:
            target = self.target
        if (target is None or now - target["seen"] > self.settings.fresh_seconds or
                now - self.last_gaze_time < 0.3 or target["seen"] == self.last_gaze_frame):
            return
        left, top, right, bottom = target["box"]
        x = 1 - (left + right) / self.settings.detector_width
        # Aim near the upper part of the person box, not the torso center.
        y = (0.8 * top + 0.2 * bottom) / (self.settings.detector_height / 2) - 1
        yaw = target["head"]["yaw"] + (clamp(x * 12, -4, 4) if abs(x) > 0.08 else 0)
        pitch = target["head"]["pitch"] + (clamp(y * 8, -3, 3) if abs(y) > 0.08 else 0)
        self.move_head(pitch=pitch, yaw=yaw)
        self.last_gaze_time, self.last_gaze_frame = now, target["seen"]

    @contextmanager
    def visual_interruption(self):
        """Restore observation before counting absence or card removal again."""
        with self.lock:
            self.presence.paused += 1
        try:
            yield
        finally:
            # No tracking thread can overwrite the deliberate pose.
            self.best_effort("move_head", pitch=0, roll=0, yaw=0, duration=0.4, units="degrees")
            with self.lock:
                now = self.clock()
                self.head.update(pitch=0, yaw=0)
                self.target = None
                self.presence.resume(now)
                self.cards.resumed_view(now, self.settings.reacquire_seconds)

    def say(self, text, activity=False):
        LOG.info("MISTY: %s", text)
        if not self.settings.spoken_prompts and not activity:
            return
        self.checkpoint()
        self.speech_sequence += 1
        utterance = "window-%d-%d" % (id(self), self.speech_sequence)
        with self.lock:
            self.speech_expected, self.speech_complete = utterance, False
        try:
            self.command("set_default_volume", self.settings.speech_volume)
            self.command("speak", text, speechRate=self.settings.speech_rate,
                         utteranceId=utterance)
            deadline = self.clock() + max(8, len(text) / 8)
            while self.clock() < deadline:
                self.checkpoint()
                with self.lock:
                    done = self.speech_complete
                if done:
                    return
                self.pause(0.1, track=True)
            LOG.warning("Speech completion timed out")
            self.best_effort("stop_speaking")
        finally:
            with self.lock:
                self.speech_expected = None

    def brief_glance(self):
        self.pause(0.7, track=True)

    def mutual_gaze(self):
        self.pause(1.2, track=True)

    def gaze_aversion(self):
        with self.visual_interruption():
            self.move_head(pitch=5, yaw=random.choice([-12, 12]))
            self.pause(0.8)

    def wave(self, small=False):
        self.move_arms(None, -20 if small else -45)
        for _ in range(1 if small else 2):
            self.move_arms(None, 5 if small else -20, 0.3)
            self.move_arms(None, -20 if small else -45, 0.3)
        self.move_arms(None, 90)

    def nod(self):
        with self.lock:
            yaw = self.head["yaw"]
        self.move_head(pitch=5, yaw=yaw, duration=0.25)
        self.pause(0.25)
        self.move_head(pitch=-2, yaw=yaw, duration=0.25)
        self.pause(0.25)

    def card_cue(self):
        with self.visual_interruption():
            self.move_head(pitch=self.settings.card_pitch, yaw=self.settings.card_yaw)
            if self.settings.card_yaw >= 0:
                self.move_arms(25, None)
                self.move_arms(90, None)
            else:
                self.move_arms(None, 25)
                self.move_arms(None, 90)

    def repair(self, text="Please show one of the cards numbered 1, 2, or 3."):
        self.set_stage("REPAIR")
        self.show_face("confused")
        with self.visual_interruption():
            self.move_head(pitch=4, roll=7, yaw=-8)
            self.pause(0.6)
        self.say(text)
        self.card_cue()
        self.show_face("friendly")

    def explain_choices(self):
        self.set_stage("VISUAL_INVITE")
        self.show_face("friendly")
        self.say("Choose a card. 1 is dance, 2 is music, and 3 is a joke.")
        self.card_cue()

    def wait_for_card(self, retry=False):
        self.set_stage("WAIT")
        started = self.clock()
        # After the first missed read, keep accepting cards for this visit.
        # A rescan is a new human turn, not a new greeting or a new visitor.
        deadline = math.inf if retry else started + self.settings.card_wait_seconds
        with self.lock:
            self.cards.begin(self.active_epoch, deadline)
        averted = reminded = retry
        repaired = False
        try:
            if retry:
                self.set_stage("RESCAN")
                self.show_face("confused")
                self.say("I didn't read the card. Let's try again. "
                         "Lower it, then hold it steady in front of my camera. I'll keep scanning.")
                self.show_face("friendly")
                self.set_stage("WAIT")
            while True:
                self.checkpoint()  # Departure wins over queued card input.
                with self.lock:
                    pending = self.cards.pending
                    self.cards.pending = None
                if pending:
                    kind, tag = pending
                    if kind == "valid":
                        return tag
                    if not repaired:
                        self.repair()
                        repaired = True
                    # Keep the original deadline so bad cards cannot trap us.
                    with self.lock:
                        self.cards.begin(self.active_epoch, deadline)
                    self.set_stage("WAIT")
                elapsed = self.clock() - started
                if self.clock() >= deadline:
                    return None
                if elapsed >= 2 and not averted:
                    self.gaze_aversion()
                    averted = True
                elif elapsed >= self.settings.card_reminder_seconds and not reminded:
                    self.say("You can choose 1, 2, or 3.")
                    self.card_cue()
                    reminded = True
                else:
                    # Forward observation between brief social gaze episodes.
                    self.pause(0.1)
        finally:
            with self.lock:
                self.cards.close()

    def acknowledge_card(self, tag):
        self.set_stage("ACKNOWLEDGE")
        self.look_toward_person()
        self.show_face("happy")
        self.nod()
        self.set_stage("PROCESSING")
        self.show_face("thinking")
        with self.visual_interruption():
            self.move_head(pitch=-3, roll=5, yaw=12)
            self.pause(0.7)
        self.set_stage("CONFIRMATION")
        self.show_face("happy")
        self.say("You chose %d. Good choice!" % tag)
        self.set_stage(TAG_FOR_ACTIVITY[tag].upper())
        {1: self.do_dance, 2: self.do_sing, 3: self.do_joke}[tag]()

    def do_dance(self):
        self.say("Let's dance with some music!")
        with self.visual_interruption():
            if self.settings.dance_skill:
                self.play_dance_skill()
            else:
                # Combine the clockwise rotation, old.py's head/arm dance,
                # and music.py's uploaded songs. Playback is asynchronous.
                with self.music_playback():
                    try:
                        self.command("drive_time", linearVelocity=0,
                                     angularVelocity=self.settings.dance_angular_velocity,
                                     timeMs=int(self.settings.dance_rotation_seconds * 1000))
                        rotation_started = self.clock()
                        for _ in range(3):
                            self.move_arms(-25, -25, 0.6)
                            for _ in range(3):
                                self.move_head(roll=-5, yaw=-15, duration=0.3)
                                self.pause(0.3)
                                self.move_head(roll=5, yaw=15, duration=0.3)
                                self.pause(0.3)
                            self.move_arms(90, 90, 0.6)
                        self.pause(max(0, self.settings.dance_rotation_seconds -
                                       (self.clock() - rotation_started)))
                    finally:
                        # Also stop if a gesture, connection, or Ctrl+C aborts.
                        self.best_effort("stop")
        self.pause(self.settings.reacquire_seconds)

    def play_dance_skill(self):
        skill = self.settings.dance_skill
        self.running_skill = skill
        try:
            self.command("run_skill", skill=skill)
            deadline = self.clock() + self.settings.activity_timeout
            seen_running = False
            while self.clock() < deadline:
                self.pause(0.3)
                running = self.command("get_running_skills")
                active = any(item.get("uniqueId") == skill for item in running)
                if active:
                    seen_running = True
                elif seen_running:
                    return
            raise RuntimeError("Dance skill did not report a complete run before timeout")
        finally:
            self.best_effort("cancel_skill", skill=skill)
            self.running_skill = None

    @contextmanager
    def music_playback(self):
        """Shared native playback for dancing and the music-only activity."""
        if not self.songs:
            raise RuntimeError("Neither configured song is installed on Misty")
        song = random.choice(self.songs)
        with self.lock:
            self.audio_expected, self.audio_complete = song, False
        try:
            self.command("change_led", 0, 0, 255)
            self.command("play_audio", fileName=song, volume=100)
            yield song
        finally:
            self.best_effort("stop_audio")
            self.best_effort("change_led", 100, 70, 160)
            with self.lock:
                self.audio_expected = None

    @contextmanager
    def native_action(self, name):
        """Let an installed action own motion until it is stopped."""
        action = self.actions.get(name.lower())
        if action is None:
            yield
            return
        with self.visual_interruption():
            self.running_action = action
            try:
                self.command("start_action", action)
                yield
            finally:
                self.best_effort("stop_action")
                self.running_action = None

    def do_sing(self):
        with self.native_action(self.settings.music_action), self.music_playback():
            deadline = self.clock() + self.settings.activity_timeout
            while self.clock() < deadline:
                self.checkpoint()
                with self.lock:
                    done = self.audio_complete
                if done:
                    return
                self.pause(0.1)
            raise RuntimeError("Music playback completion timed out")

    def do_joke(self):
        setup, punchline = random.choice(JOKES)
        self.mutual_gaze()
        self.say(setup, activity=True)
        self.gaze_aversion()
        self.pause(0.5)
        self.say(punchline, activity=True)
        self.show_face("happy")
        self.say("Ha ha! I hope you liked that one.", activity=True)

    def polite_goodbye(self, timeout=False):
        self.set_stage("GOODBYE")
        self.show_face("goodbye")
        self.look_toward_person()
        self.say("No worries. Maybe next time!" if timeout else "Thanks for playing. Have a great day!")
        self.wave(small=timeout)

    def run_conversation(self):
        self.set_stage("ENGAGEMENT")
        self.show_face("friendly")
        self.mutual_gaze()
        self.wave(small=True)
        self.say("Hello! I'm Misty. Would you like to try an activity?")
        self.explain_choices()
        while True:
            tag = self.wait_for_card()
            if tag is None:
                # Keep the same visit alive. A missed scan is not a refusal.
                tag = self.wait_for_card(retry=True)
            try:
                self.acknowledge_card(tag)
            except (VisitorLeft, Stopping):
                raise
            except Exception:
                LOG.exception("Activity failed")
                self.repair("That activity didn't work. Please try another card.")
            self.checkpoint()
            self.set_stage("CONTINUE_CUE")
            self.say("Would you like to try another card?")
            self.card_cue()

    def reset_pose(self):
        self.best_effort("move_head", pitch=0, roll=0, yaw=0, duration=0.4, units="degrees")
        self.best_effort("move_arms", 90, 90, duration=0.6)
        if FACE_IMAGES["idle"] in self.images:
            self.best_effort("display_image", FACE_IMAGES["idle"])
        with self.lock:
            self.head.update(pitch=0, yaw=0)
            self.target = None
            self.presence.observe_after = max(
                self.presence.observe_after,
                self.clock() + 0.4 + self.settings.reacquire_seconds)
            self.cards.resumed_view(self.clock(), 0.4 + self.settings.reacquire_seconds)

    def handle_visit(self, epoch):
        with self.lock:
            self.active_epoch = epoch
        try:
            self.set_stage("NOTICE")
            self.brief_glance()
            while True:
                self.checkpoint()
                with self.lock:
                    ready = self.presence.ready(epoch, self.clock())
                if ready:
                    break
                self.pause(0.1, track=True)
            self.run_conversation()
        except VisitorLeft:
            LOG.info("Visitor left; resetting without calling after them")
        except Stopping:
            raise
        except Exception:
            LOG.exception("Visit ended after an error; waiting for departure")
        finally:
            with self.lock:
                self.cards.close()
                self.active_epoch = None
            self.best_effort("stop_speaking")
            self.best_effort("stop_audio")
            self.reset_pose()
            # A finished visit stays claimed until a real absence/new entry.
            self.set_stage("WAIT_FOR_EXIT")

    def load_assets(self):
        self.images = {item["name"] for item in self.command("get_image_list")}
        audio = {item["name"] for item in self.command("get_audio_list")}
        self.songs = [name for name in self.settings.songs if name in audio]
        self.skills = self.command("get_skills")
        try:
            self.actions = {item["name"].lower(): item["name"]
                            for item in self.command("get_actions")}
        except Exception as exc:
            self.actions = {}
            LOG.warning("Native action inventory unavailable; using scripted cues: %s", exc)
        for name, filename in FACE_IMAGES.items():
            if filename not in self.images:
                LOG.warning("Expression %s unavailable: %s", name, filename)
        if self.settings.dance_skill and not any(
                item.get("uniqueId") == self.settings.dance_skill for item in self.skills):
            raise RuntimeError("Configured dance skill is not installed")
        LOG.info("Available songs: %s; dance: %s", self.songs,
                 self.settings.dance_skill or "rotation + head/arm choreography + music")

    def ensure_music_assets(self, directory=None):
        """Integrate music.py's uploader; upload only missing configured songs."""
        root = Path(directory) if directory is not None else Path(__file__).resolve().parent
        uploaded = False
        for song in self.settings.songs:
            if song in self.songs:
                continue
            path = root / song
            if not path.is_file():
                LOG.warning("Song is missing from both robot and project: %s", song)
                continue
            try:
                data = path.read_bytes()
                if len(data) > 3 * 1024 * 1024:
                    raise ValueError("Audio exceeds Misty's 3 MB upload limit")
                self.command("save_audio", fileName=song,
                             data=base64.b64encode(data).decode("ascii"),
                             immediatelyApply=False, overwriteExisting=False)
                uploaded = True
                LOG.info("Uploaded missing song: %s", song)
            except Exception as exc:
                LOG.warning("Could not upload %s: %s", song, exc)
        if uploaded:
            audio = {item["name"] for item in self.command("get_audio_list")}
            self.songs = [name for name in self.settings.songs if name in audio]

    def subscribe(self, event_type, name, callback, **kwargs):
        # This SDK checks co_argcount == 1 and rejects bound methods directly.
        def receive(data):
            callback(data)
        registration = self.robot.register_event(
            event_type, name, callback_function=receive, keep_alive=True, **kwargs)
        if registration is None:
            raise RuntimeError("Failed to subscribe to " + name)
        self.registrations.append(registration)

    def start(self, events):
        self.load_assets()
        self.ensure_music_assets()
        self.subscribe(events.ObjectDetection, "window_people", self.person_callback, debounce=250)
        self.subscribe(events.ArTagDetection, "window_cards", self.tag_callback, debounce=150)
        self.subscribe(events.AudioPlayComplete, "window_audio", self.audio_callback)
        self.subscribe(events.TextToSpeechComplete, "window_speech", self.speech_callback)
        for sensor in ("ahy", "ahp"):
            self.subscribe(events.ActuatorPosition, "window_" + sensor, self.actuator_callback,
                           debounce=200, condition=[{"Property": "SensorId", "Inequality": "=", "Value": sensor}])
        deadline = self.clock() + 8
        while any(registration.initial_flag for registration in self.registrations):
            self.checkpoint()
            if self.clock() >= deadline:
                raise RuntimeError("Timed out waiting for event subscriptions")
            self.stop_event.wait(0.1)
        self.command("start_object_detector", minimumConfidence=self.settings.confidence,
                     modelId=0, maxTrackerHistory=5)
        self.detectors.append("stop_object_detector")
        self.command("start_ar_tag_detector", dictionary=AR_DICTIONARY, tagSizeMm=TAG_SIZE_MM)
        self.detectors.append("stop_ar_tag_detector")
        if self.settings.startup_action.lower() in self.actions:
            with self.native_action(self.settings.startup_action):
                self.pause(2)
        self.reset_pose()
        self.command("change_led", 100, 70, 160)
        LOG.info("Window-display Misty is ready: person tracking + AR cards 1/2/3")

    def run(self):
        while not self.stop_event.is_set():
            self.checkpoint()
            with self.lock:
                epoch = self.presence.claim(self.clock())
                occupied = self.presence.occupied
            if epoch is not None:
                self.handle_visit(epoch)
            else:
                self.set_stage("WAIT_FOR_EXIT" if occupied else "IDLE")
                self.stop_event.wait(0.1)

    def shutdown(self):
        self.stop_event.set()
        with self.lock:
            self.cards.close()
        for command in reversed(self.detectors):
            self.best_effort(command)
        if self.running_skill:
            self.best_effort("cancel_skill", skill=self.running_skill)
        if self.running_action:
            self.best_effort("stop_action")
        if self.detectors:
            self.best_effort("stop_speaking")
            self.best_effort("stop_audio")
            self.best_effort("stop")
            self.reset_pose()
        self.robot.unregister_all_events()
        LOG.info("Interaction stopped")


def create_robot(ip):
    from mistyPy.Robot import Robot

    class BoundedRobot(Robot):
        def _generic_request(self, verb, endpoint, **kwargs):
            kwargs.setdefault("timeout", (3, 5))
            return super()._generic_request(verb, endpoint, **kwargs)

    return BoundedRobot(ip)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", default="192.168.0.149")
    parser.add_argument("--silent-prompts", action="store_true", help="Keep nonverbal cues; music and joke audio still play")
    parser.add_argument("--leave-seconds", type=float, default=4.0)
    parser.add_argument("--card-yaw", type=float, default=-18.0, help="Positive is Misty's left")
    parser.add_argument("--card-pitch", type=float, default=8.0)
    parser.add_argument("--dance-skill", default="", help="Installed skill GUID; must restore the forward viewing pose")
    parser.add_argument("--inventory", action="store_true", help="List relevant assets and exit without moving")
    args = parser.parse_args(argv)
    if not math.isfinite(args.leave_seconds) or args.leave_seconds <= 1.2:
        parser.error("--leave-seconds must be greater than the 1.2-second dwell")
    if not (-25 <= args.card_yaw <= 25 and -15 <= args.card_pitch <= 15):
        parser.error("Card gaze must be within yaw +/-25 and pitch +/-15 degrees")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings(leave_seconds=args.leave_seconds, spoken_prompts=not args.silent_prompts,
                        card_yaw=args.card_yaw, card_pitch=args.card_pitch, dance_skill=args.dance_skill)
    try:
        from mistyPy.Events import Events
        robot = create_robot(args.ip)
    except ImportError as exc:
        parser.exit(1, "SDK unavailable. Follow README setup and set PYTHONPATH=Python-SDK.\n%s\n" % exc)
    app = Interaction(robot, settings)
    if args.inventory:
        app.load_assets()
        print(json.dumps({"expressions": {k: v for k, v in FACE_IMAGES.items() if v in app.images},
                          "songs": app.songs, "skills": app.skills,
                          "actions": sorted(app.actions.values())}, indent=2))
        return 0
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: app.stop_event.set())
    try:
        app.start(Events)
        app.run()
    except (KeyboardInterrupt, Stopping):
        pass
    except Exception:
        LOG.exception("Interaction failed")
        return 1
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
