# Run with PYTHONPATH=Python-SDK python3 misty.py
from mistyPy.GenerateRobot import RobotGenerator
from mistyPy.Robot import Robot

# Run once to update the command list. Comment this out after it succeeds.
#RobotGenerator("192.168.86.40")

misty = Robot("192.168.86.40")
import time
import random
import threading
from mistyPy.Robot import Robot
from mistyPy.Events import Events


GONE_AFTER_SECONDS = 3.0 # A face is treated as gone if Misty has not seen it recently.
FACE_DEBOUNCE_MS = 300 # Face event frequency.
ENGAGEMENT_DWELL_SECONDS = 3.0 # Require a person to remain visible before starting conversation. This prevents Misty from talking to every passerby.
WAIT_FOR_CARD_SECONDS = 10 # Waiting for AR card.
CARD_REMINDER_SECONDS = 5 # Give one polite reminder while waiting.
# AR settings
AR_DICTIONARY = 7
TAG_SIZE_MM = 100

TAG_FOR_ACTIVITY = {
    1: "dance",
    2: "sing",
    3: "joke",
}

TAG_LABEL = {
    1: "1",
    2: "2",
    3: "3",
}


# ============================================================
# PROXEMICS
# ============================================================

"""
IMPORTANT:
The FaceRecognition event exposes Distance, Bearing, Elevation.

Misty documentation shows these fields, but your particular robot/
firmware should be calibrated before treating Distance as an exact
physical distance for the design paper.

We therefore use it conservatively.

These thresholds are DESIGN PARAMETERS, not universal HRI rules.
Tune them after testing through your glass window.
"""

FAR_DISTANCE = 250
INVITATION_DISTANCE = 180
CLOSE_DISTANCE = 70


# ============================================================
# HEAD / GAZE CONFIGURATION
# ============================================================

# Head yaw:
# positive = Misty's left
# negative = Misty's right

MAX_GAZE_YAW = 25
BRIEF_GLANCE_SECONDS = 0.7
MUTUAL_GAZE_SECONDS = 1.2
GAZE_AVERSION_SECONDS = 0.8

conversation_lock = threading.Lock()
notice_lock = threading.Lock()

card_event = threading.Event()

accepting_cards = False
chosen = {"tag": None}

last_seen_time = 0
first_seen_time = 0

latest_face = {
    "bearing": 0,
    "elevation": 0,
    "distance": None,
}

engagement_thread_running = False

FACE_IMAGES = {
    "idle": "e_DefaultContent.jpg",
    "friendly": 'e_Admiration.jpg',
    "happy": "e_Joy.jpg",
    "thinking": "e_Contempt.jpg",
    "confused": "e_Terror.jpg",
    "goodbye": "e_Admiration.jpg",
    "listen": "e_Surprise.jpg"
}


def show_face(name):
    filename = FACE_IMAGES.get(name)

    if not filename:
        return

    try:
        misty.display_image(filename)
    except Exception as exc:
        print(f"[face image skipped: {exc}]")


# ============================================================
# BASIC SPEECH
# ============================================================

def say(text, pause=None):
    """
    Speak and wait long enough to avoid overlapping lines.
    Keep speech short and clear.
    """
    print("MISTY:", text)
    misty.set_default_volume(50)
    misty.speak(text, speechRate = 1.75)
    if pause is None:
        pause = max(1.4, len(text) / 13)
    time.sleep(pause)

HEAD_LOCK = threading.Lock()
MIN_HEAD_DURATION = 1.2  # shorter moves look like snapping

def move_head(pitch=0, roll=0, yaw=0, duration=1.2):
    duration = max(duration, MIN_HEAD_DURATION)
    # Wait for the head to be free, then run the move to completion.
    # Commands run in order and none are dropped.
    with HEAD_LOCK:
        try:
            misty.move_head(pitch=pitch,roll=roll,yaw=yaw,duration=duration,units="degrees")
        except Exception as exc:
            print(f"[head motion skipped: {exc}]", flush=True)
        time.sleep(duration)

def neutral_head():
    move_head(pitch=0,roll=0,yaw=0,duration=0.4)

def clamp(value, low, high):
    return max(low, min(high, value))

GAZE_TRACKING = True
GAZE_DEADBAND_DEG = 8            # ignore changes smaller than this
GAZE_MIN_INTERVAL_SECONDS = 2.0  # re-aim at most once every 2 seconds
GAZE_SMOOTHING = 0.2             # 0 to 1; lower is smoother

last_gaze = {"yaw": None, "time": 0}

def look_toward_latest_face():
    if not GAZE_TRACKING:
        return
    if time.time() - last_gaze["time"] < GAZE_MIN_INTERVAL_SECONDS:
        return

    bearing = latest_face.get("bearing") or 0
    yaw = clamp(bearing, -MAX_GAZE_YAW, MAX_GAZE_YAW)

    if last_gaze["yaw"] is not None and abs(yaw - last_gaze["yaw"]) < GAZE_DEADBAND_DEG:
        return

    last_gaze["yaw"] = yaw
    last_gaze["time"] = time.time()

    pitch = clamp(-(latest_face.get("elevation") or 0), -10, 10)
    move_head(pitch=pitch, roll=0, yaw=yaw, duration=1.2)

def brief_glance():
    """
    Low-intensity acknowledgement.

    HRI idea:
    "I noticed you" without strong mutual gaze.
    """
    look_toward_latest_face()
    time.sleep(BRIEF_GLANCE_SECONDS)
    # Slight gaze aversion
    direction = random.choice([-8, 8])
    move_head(pitch=2,yaw=direction,duration=0.35)


def mutual_gaze():
    """
    Short mutual gaze after a person has engaged.
    """
    look_toward_latest_face()
    time.sleep(MUTUAL_GAZE_SECONDS)

def gaze_aversion(style="side"):
    """
    Deliberately break direct gaze.

    Used when:
    - waiting for the person
    - Misty is "thinking"
    - processing a card
    - avoiding continuous staring
    """
    if style == "down":
        pitch = 8
        yaw = random.choice([-5, 5])

    elif style == "thinking":
        pitch = -4
        yaw = random.choice([-12, 12])

    else:
        pitch = 2
        yaw = -8
    move_head(pitch=pitch, roll=0, yaw=yaw, duration=0.45)

def return_gaze():
    look_toward_latest_face()

def soft_gaze():
    """
    Used when someone is very close.
    Less intense than full direct gaze.
    """
    bearing = latest_face.get("bearing") or 0
    yaw = clamp(bearing, -8, 8)
    move_head(pitch=4, roll=0, yaw=yaw, duration=0.5)

# ============================================================
# GESTURES
# ============================================================

def wave():
    """
    SYMBOLIC gesture:
    conventional hello / goodbye.
    """
    misty.move_arms(None, -45, duration=1.0)
    time.sleep(0.8)
    for _ in range(2):
        misty.move_arms(None, -20, duration=0.3)
        time.sleep(0.3)
        misty.move_arms(None, -45, duration=0.3)
        time.sleep(0.3)
    misty.move_arms(None,90,duration=0.8)

def small_wave():
    """
    Lower-intensity wave for a passerby.
    """
    misty.move_arms(None,-20,duration=0.5)
    time.sleep(0.4)
    misty.move_arms(None,5,duration=0.35)
    time.sleep(0.3)
    misty.move_arms(None,90,duration=0.6)

def beat_gesture():
    """
    BEAT gesture:
    small rhythmic arm movement aligned with speech.
    """
    misty.move_arms(None,30,duration=0.25)
    time.sleep(0.25)
    misty.move_arms(None,55,duration=0.25)
    time.sleep(0.25)
    misty.move_arms(None,90,duration=0.35)


def open_invitation_gesture():
    """
    Open-arm invitation.

    Not meant to imitate a human exactly.
    It simply supports the verbal invitation.
    """
    misty.move_arms(35,35,duration=0.6)
    time.sleep(0.6)
    misty.move_arms(90,90,duration=0.6)

def thinking_pose():
    """
    Slight head tilt + gaze aversion.
    """
    show_face("thinking")
    move_head(pitch=-3,roll=5,yaw=12,duration=0.5)

def confused_pose():
    """
    Makes failure legible instead of pretending Misty understood.
    """
    show_face("confused")
    move_head(pitch=4,roll=7,yaw=-8,duration=0.5)

# ============================================================
# FACE / PERSON PRESENCE
# ============================================================

def face_is_present():
    return (time.time() - last_seen_time) < GONE_AFTER_SECONDS

def current_distance():
    return latest_face.get("distance")

def proxemic_zone():
    """
    Returns a coarse interaction zone.

    We do NOT rely on this alone to decide engagement.
    """
    distance = current_distance()
    if distance is None:
        return "unknown"
    if distance > FAR_DISTANCE:
        return "far"
    if distance > INVITATION_DISTANCE:
        return "public"
    if distance > CLOSE_DISTANCE:
        return "social"
    return "very_close"
# ============================================================
# FIRST-CONTACT BEHAVIOR
# ============================================================
def notice_passed_person():
    """
    Person has entered the camera view, but we do NOT yet
    know whether they want to interact.

    This is intentionally subtle.
    """
    if notice_lock.locked():
        return
    with notice_lock:
        if conversation_lock.locked():
            return
        zone = proxemic_zone()
        if zone == "far":
            # No engagement at all.
            return
        brief_glance()\
        # Only a tiny wave in public/social space.
        if zone in ("public", "social"):
            small_wave()
        # Then stop.
        neutral_head()
# ============================================================
# ACTIVITIES
# ============================================================
def do_dance():
    show_face("happy")
    return_gaze()
    say("Let's dance!")
    # ICONIC gesture:
    # body movement represents dancing.
    misty.drive_time(linearVelocity=0, angularVelocity=-100, timeMs=8550)
    misty.move_arms(-25, -25, duration=0.6)
    for _ in range(3):
        move_head(pitch=0,roll=-5,yaw=-15,duration=0.3)
        #time.sleep(0.3)
        move_head(pitch=0,roll=5,yaw=15,duration=0.3)
        #time.sleep(0.3)
        misty.move_arms(90,90,duration=0.8)
    neutral_head()

def do_sing():
    show_face("happy")
    #misty.start_action("Love")
    return_gaze()
    musicLib = ["baby.mp3", "beautiful.mp3"]
    song = random.choice(musicLib)
    say("Number 2! Here's a little song for you.") 
    misty.change_led(0, 0, 255)
    misty.play_audio(fileName=song, volume=100)
    time.sleep(12)  # wait for the clip to finish
    misty.change_led(100, 70, 160)

def do_joke():
    jokeLib = [["Why did the robot go on a diet?", "Because it had too many bytes!"], ["Why was the robot so bad at soccer?", "Because it kept kicking up sparks!"], ["Why did the robot cross the road?", "To get to the other side!"]]
    currentJoke = random.choice(jokeLib)
    show_face("happy")
    return_gaze()
    say("You chose ID number 3! I have a joke for you.")
    time.sleep(0.4)
    mutual_gaze()
    say(currentJoke[0])
    # Do not stare while waiting for punchline anticipation.
    gaze_aversion("side")
    time.sleep(1.0)
    return_gaze()
    say(currentJoke[1])
    say("Ha ha! I hope you liked that one.")
    
ACTIVITIES = {
    "dance": do_dance,
    "sing": do_sing,
    "joke": do_joke,
}

# ============================================================
# CARD CHOICE / DIALOGUE MANAGEMENT
# ============================================================

def explain_choices():
    show_face("friendly")
    return_gaze()
    say("Choose one card.")
    # Beat gesture supports the list structure.
    beat_gesture()
    say( "1 is dance.") # originally red 
    beat_gesture()
    say("2 is a song.") #originally blue
    beat_gesture()
    say("3 is a joke.") #originally yellow
    say("Hold your card up to the glass.")
    gaze_aversion("side")


def wait_for_card():
    """
    Dialogue-management state.

    Misty waits without continuous staring.

    0 sec:
        user has the turn

    ~2 sec:
        gaze aversion

    ~5 sec:
        one gentle reminder

    ~10 sec:
        politely stop
    """
    global accepting_cards
    card_event.clear()
    chosen["tag"] = None
    accepting_cards = True
    started = time.time()
    aversion_done = False
    reminder_done = False
    while True:
        if card_event.wait(timeout=0.2):
            accepting_cards = False
            return chosen["tag"]
        elapsed = time.time() - started
        # User left.
        if not face_is_present():
            accepting_cards = False
            return None
        # Gaze aversion while the user is choosing.
        if elapsed >= 2.0 and not aversion_done:
            gaze_aversion("side")
            aversion_done = True
        # One polite reminder only.
        if elapsed >= CARD_REMINDER_SECONDS and not reminder_done:
            return_gaze()
            say("Take your time. " "You can choose 1 of 3 different actions", pause=2.2)
            gaze_aversion("side")
            reminder_done = True
        if elapsed >= WAIT_FOR_CARD_SECONDS:
            accepting_cards = False
            return None
# ============================================================
# CARD PROCESSING
# ============================================================
def acknowledge_card(tag):
    color = TAG_LABEL[tag]
    activity = TAG_FOR_ACTIVITY[tag]
    # Processing state:
    # gaze away first rather than instantly responding.
    thinking_pose()
    time.sleep(0.7)
    return_gaze()
    show_face("happy")
    say(f"You chose {color.capitalize()}! Good choice.", pause=1.3)
    ACTIVITIES[activity]()
# ============================================================
# DISENGAGEMENT
# ============================================================
def polite_goodbye():
    show_face("goodbye")
    if face_is_present():
        soft_gaze()
        say("Thanks for playing with me. " "Have a great day!")
        wave()
    else:
        # User has already walked away.
        # Do not shout after them.
        print("[Visitor already left - silent reset]")
    neutral_head()
    misty.move_arms(90,90,duration=0.8)
    show_face("idle")

def no_response_goodbye():
    show_face("friendly")
    return_gaze()
    say("No worries. Maybe next time!")
    small_wave()
    gaze_aversion("side")
    neutral_head()
# ============================================================
# FULL CONVERSATION
# ============================================================
def run_conversation():
    with conversation_lock:
        # Make sure person is still present.
        if not face_is_present():
            return
        zone = proxemic_zone()
        # Too far:
        # do not begin conversation.
        if zone in ("far", "public"):
            return
        # Very close:
        # soften gaze.
        if zone == "very_close":
            soft_gaze()
        else:
            mutual_gaze()
        show_face("friendly")
        say("Oh, hello there!", pause=1.0)
        # SYMBOLIC greeting gesture.
        wave()
        return_gaze()
        say("I'm Misty. " "Would you like to try a quick activity?")
        open_invitation_gesture()
        # We cannot infer verbal "yes" yet,
        # so presenting a card becomes the user's action of engagement.
        explain_choices()
        rounds = 0
        # Keep taking cards until the visitor leaves or stops responding.
        while face_is_present():
            tag = wait_for_card()
            # No card in time: the visitor stopped responding or left.
            if tag is None:
                if face_is_present():
                    no_response_goodbye()
                break
            rounds += 1
            acknowledge_card(tag)
            # Visitor walked away during the activity.
            if not face_is_present():
                break
            # Offer another round, then wait for the next card.
            return_gaze()
            say("Would you like to try another card?")
            # Human turn begins.
            gaze_aversion("side")
        polite_goodbye()
# ============================================================
# ENGAGEMENT MONITOR
# ============================================================
def engagement_monitor():
    """
    Important design improvement over the original version.

    A detected face does NOT immediately trigger conversation.

    Misty waits to see whether the person stays.

    This helps distinguish:
        passerby
        vs.
        potentially engaged visitor
    """
    global engagement_thread_running
    try:
        while True:
            if conversation_lock.locked():
                return
            if not face_is_present():
                return
            visible_for = (
                time.time() - first_seen_time
            )
            if visible_for >= ENGAGEMENT_DWELL_SECONDS:
                zone = proxemic_zone()
                # Only begin full conversation in the social zone.
                if zone in ("social", "very_close", "unknown"):
                    run_conversation()
                return
            time.sleep(0.1)
    finally:
        engagement_thread_running = False

# ============================================================
# AR CALLBACK
# ============================================================

def tag_callback(data):
    if (not accepting_cards or card_event.is_set()):
        return
    message = data.get("message",{})
    tag_id = (message.get("tagId") if message.get("tagId") is not None else message.get("TagId"))
    if tag_id in TAG_FOR_ACTIVITY:
        chosen["tag"] = tag_id
        card_event.set()

# ============================================================
# BODY CALLBACK
# ============================================================

BODY_MIN_CONFIDENCE = 0.6
RAD_TO_DEG = 57.2958

def body_callback(data):
    global last_seen_time, first_seen_time, engagement_thread_running
    event_counts["body"] += 1

    message = data.get("message", {})

    # Ignore everything except confident person detections
    if message.get("description") != "person":
        return
    if message.get("confidence", 0) < BODY_MIN_CONFIDENCE:
        return

    now = time.time()
    was_gone = (now - last_seen_time) > GONE_AFTER_SECONDS
    last_seen_time = now

    # Gaze direction from the body
    # Blend each new reading into the running value so jitter doesn't move the head
    new_yaw = message.get("yaw", 0) * RAD_TO_DEG
    old_yaw = latest_face.get("bearing") or 0
    latest_face["bearing"] = old_yaw + GAZE_SMOOTHING * (new_yaw - old_yaw)
    latest_face["elevation"] = message.get("pitch", 0) * RAD_TO_DEG

    # Body detection gives no distance, so the zone is "unknown",
    # which the conversation logic already accepts
    latest_face["distance"] = None

    if was_gone:
        first_seen_time = now
        # First contact: acknowledge, but don't talk yet
        threading.Thread(target=notice_passed_person, daemon=True).start()

    # Start the dwell check once per visit
    if not engagement_thread_running and not conversation_lock.locked():
        engagement_thread_running = True
        threading.Thread(target=engagement_monitor, daemon=True).start()
# ============================================================
# STARTUP
# ============================================================
event_counts = {"face": 0, "body": 0}
def status_monitor():
    while True:
        time.sleep(5)
        print(
            f"[status] body events: {event_counts['body']} | "
            f"gaze yaw: {latest_face['bearing']:.1f} | "
            f"present: {face_is_present()}",
            flush=True,
        )

try:
    show_face("idle")
    neutral_head()
    misty.move_arms(90, 90, duration=0.8)
    misty.change_led(100, 70, 160)

    misty.register_event(
        Events.ArTagDetection,
        "tags_seen",
        callback_function=tag_callback,
        keep_alive=True
    )
    misty.register_event(
        Events.ObjectDetection,
        "body_seen",
        callback_function=body_callback,
        keep_alive=True
    )

    misty.start_ar_tag_detector(
        dictionary=AR_DICTIONARY,
        tagSizeMm=TAG_SIZE_MM
    )
    misty.start_object_detector(minimumConfidence=0.5)
    threading.Thread(target=status_monitor, daemon=True).start()

    print("Window-display Misty is ready.", flush=True)
    misty.keep_alive()
finally:
    try:
        misty.stop_ar_tag_detector()
    except Exception:
        pass
    try:
        misty.stop_object_detector()
    except Exception:
        pass
    try:
        neutral_head()
        misty.move_arms(90, 90, duration=0.8)
    except Exception:
        pass
    misty.unregister_all_events()