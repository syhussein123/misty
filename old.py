import time
import random
import threading

from mistyPy.Robot import Robot
from mistyPy.Events import Events


# ============================================================
# ROBOT SETUP
# ============================================================

misty = Robot()

# ============================================================
# INTERACTION CONFIGURATION
# ============================================================

# A face is treated as gone if Misty has not seen it recently.
GONE_AFTER_SECONDS = 3.0

# Face event frequency.
FACE_DEBOUNCE_MS = 300

# Require a person to remain visible before starting conversation.
# This prevents Misty from talking to every passerby.
ENGAGEMENT_DWELL_SECONDS = 1.2

# Waiting for AR card.
WAIT_FOR_CARD_SECONDS = 10

# Give one polite reminder while waiting.
CARD_REMINDER_SECONDS = 5

# Maximum activities per visitor.
MAX_ROUNDS = 2

# AR settings
AR_DICTIONARY = 7
TAG_SIZE_MM = 100

TAG_FOR_ACTIVITY = {
    1: "dance",
    2: "sing",
    3: "joke",
}

TAG_LABEL = {
    1: "red",
    2: "blue",
    3: "yellow",
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


# ============================================================
# SHARED STATE
# ============================================================

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


# ============================================================
# OPTIONAL FACIAL EXPRESSIONS
# ============================================================

"""
If you upload custom images or know the built-in image names,
put them here.

If left as None, the code still runs.

Example:
FACE_IMAGES["happy"] = "e_Joy2.jpg"
"""

FACE_IMAGES = {
    "idle": None,
    "friendly": None,
    "happy": None,
    "thinking": None,
    "confused": None,
    "goodbye": None,
}


def show_face(name):
    """
    Optional facial-expression wrapper.

    Different mistyPy versions expose generated commands slightly
    differently, so this function fails safely.
    """

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

    misty.speak(text)

    if pause is None:
        pause = max(1.4, len(text) / 13)

    time.sleep(pause)


# ============================================================
# HEAD / GAZE HELPERS
# ============================================================

def move_head(pitch=0, roll=0, yaw=0, duration=0.45):
    """
    Safe wrapper around Misty's MoveHead command.

    Current Misty API supports pitch, roll, yaw and either
    duration OR velocity. Do not supply both. 
    """

    try:
        misty.move_head(
            pitch=pitch,
            roll=roll,
            yaw=yaw,
            duration=duration,
            units="degrees"
        )

    except TypeError:
        # Some generated mistyPy versions may prefer positional args.
        # Fall back to velocity mode.
        try:
            misty.move_head(
                pitch,
                roll,
                yaw,
                velocity=30,
                units="degrees"
            )
        except Exception as exc:
            print(f"[head motion skipped: {exc}]")

    except Exception as exc:
        print(f"[head motion skipped: {exc}]")


def neutral_head():
    move_head(
        pitch=0,
        roll=0,
        yaw=0,
        duration=0.4
    )


def clamp(value, low, high):
    return max(low, min(high, value))


def look_toward_latest_face():
    """
    Uses FaceRecognition bearing/elevation to orient Misty's head.

    We intentionally clamp the movement so Misty does not
    dramatically track a passerby.
    """

    bearing = latest_face.get("bearing") or 0
    elevation = latest_face.get("elevation") or 0

    yaw = clamp(bearing, -MAX_GAZE_YAW, MAX_GAZE_YAW)

    # Elevation mapping may need tuning on your robot.
    pitch = clamp(-elevation, -10, 10)

    move_head(
        pitch=pitch,
        roll=0,
        yaw=yaw,
        duration=0.4
    )


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

    move_head(
        pitch=2,
        yaw=direction,
        duration=0.35
    )


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
        yaw = random.choice([-12, 12])

    move_head(
        pitch=pitch,
        roll=0,
        yaw=yaw,
        duration=0.45
    )

    time.sleep(GAZE_AVERSION_SECONDS)


def return_gaze():
    look_toward_latest_face()


def soft_gaze():
    """
    Used when someone is very close.
    Less intense than full direct gaze.
    """

    bearing = latest_face.get("bearing") or 0

    yaw = clamp(bearing, -8, 8)

    move_head(
        pitch=4,
        roll=0,
        yaw=yaw,
        duration=0.5
    )


# ============================================================
# GESTURES
# ============================================================

def wave():
    """
    SYMBOLIC gesture:
    conventional hello / goodbye.
    """

    misty.move_arms(
        None,
        -45,
        duration=1.0
    )

    time.sleep(0.8)

    for _ in range(2):
        misty.move_arms(
            None,
            -20,
            duration=0.3
        )

        time.sleep(0.3)

        misty.move_arms(
            None,
            -45,
            duration=0.3
        )

        time.sleep(0.3)

    misty.move_arms(
        None,
        90,
        duration=0.8
    )


def small_wave():
    """
    Lower-intensity wave for a passerby.
    """

    misty.move_arms(
        None,
        -20,
        duration=0.5
    )

    time.sleep(0.4)

    misty.move_arms(
        None,
        5,
        duration=0.35
    )

    time.sleep(0.3)

    misty.move_arms(
        None,
        90,
        duration=0.6
    )


def beat_gesture():
    """
    BEAT gesture:
    small rhythmic arm movement aligned with speech.
    """

    misty.move_arms(
        None,
        30,
        duration=0.25
    )

    time.sleep(0.25)

    misty.move_arms(
        None,
        55,
        duration=0.25
    )

    time.sleep(0.25)

    misty.move_arms(
        None,
        90,
        duration=0.35
    )


def open_invitation_gesture():
    """
    Open-arm invitation.

    Not meant to imitate a human exactly.
    It simply supports the verbal invitation.
    """

    misty.move_arms(
        35,
        35,
        duration=0.6
    )

    time.sleep(0.6)

    misty.move_arms(
        90,
        90,
        duration=0.6
    )


def thinking_pose():
    """
    Slight head tilt + gaze aversion.
    """

    show_face("thinking")

    move_head(
        pitch=-3,
        roll=5,
        yaw=12,
        duration=0.5
    )


def confused_pose():
    """
    Makes failure legible instead of pretending Misty understood.
    """

    show_face("confused")

    move_head(
        pitch=4,
        roll=7,
        yaw=-8,
        duration=0.5
    )


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

        brief_glance()

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

    say(
        "Red! Let's dance!"
    )

    # ICONIC gesture:
    # body movement represents dancing.

    misty.move_arms(
        -25,
        -25,
        duration=0.6
    )

    for _ in range(3):

        move_head(
            pitch=0,
            roll=-5,
            yaw=-15,
            duration=0.3
        )

        time.sleep(0.3)

        move_head(
            pitch=0,
            roll=5,
            yaw=15,
            duration=0.3
        )

        time.sleep(0.3)

    misty.move_arms(
        90,
        90,
        duration=0.8
    )

    neutral_head()


def do_sing():

    show_face("happy")

    return_gaze()

    say(
        "Blue! Here's a little song."
    )

    misty.change_led(
        0,
        0,
        255
    )

    beat_gesture()

    say(
        "La la la, la la la, "
        "I'm the happy robot behind the glass!"
    )

    misty.change_led(
        100,
        70,
        160
    )


def do_joke():

    show_face("happy")

    return_gaze()

    say(
        "Yellow! I have a joke for you."
    )

    time.sleep(0.4)

    mutual_gaze()

    say(
        "Why did the robot cross the road?"
    )

    # Do not stare while waiting for punchline anticipation.
    gaze_aversion("side")

    time.sleep(1.0)

    return_gaze()

    say(
        "To get to the other side!"
    )


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

    say(
        "Choose one card."
    )

    # Beat gesture supports the list structure.
    beat_gesture()

    say(
        "Red is dance."
    )

    beat_gesture()

    say(
        "Blue is a song."
    )

    beat_gesture()

    say(
        "Yellow is a joke."
    )

    say(
        "Hold your card up to the glass."
    )

    # Now it is the human's turn.
    # Misty should NOT keep staring.
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

            say(
                "Take your time. "
                "You can choose red, blue, or yellow.",
                pause=2.2
            )

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

    say(
        f"{color.capitalize()}! Good choice.",
        pause=1.3
    )

    ACTIVITIES[activity]()


# ============================================================
# DISENGAGEMENT
# ============================================================

def polite_goodbye():

    show_face("goodbye")

    if face_is_present():
        soft_gaze()

        say(
            "Thanks for playing with me. "
            "Have a great day!"
        )

        wave()

    else:
        # User has already walked away.
        # Do not shout after them.
        print("[Visitor already left - silent reset]")

    neutral_head()

    misty.move_arms(
        90,
        90,
        duration=0.8
    )

    show_face("idle")


def no_response_goodbye():

    show_face("friendly")

    return_gaze()

    say(
        "No worries. Maybe next time!"
    )

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

        say(
            "Oh, hello there!",
            pause=1.0
        )

        # SYMBOLIC greeting gesture.
        wave()

        return_gaze()

        say(
            "I'm Misty. "
            "Would you like to try a quick activity?"
        )

        open_invitation_gesture()

        # We cannot infer verbal "yes" yet,
        # so presenting a card becomes the user's action of engagement.
        explain_choices()

        rounds = 0

        while rounds < MAX_ROUNDS:

            if not face_is_present():
                break

            tag = wait_for_card()

            if tag is None:

                if face_is_present():
                    no_response_goodbye()

                break

            rounds += 1

            acknowledge_card(tag)

            if rounds >= MAX_ROUNDS:
                break

            if not face_is_present():
                break

            # Offer exactly once.
            return_gaze()

            say(
                "Would you like to try another card?"
            )

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
# FACE CALLBACK
# ============================================================

def face_callback(data):

    global last_seen_time
    global first_seen_time
    global engagement_thread_running

    message = data.get(
        "message",
        {}
    )

    now = time.time()

    was_gone = (
        now - last_seen_time
    ) > GONE_AFTER_SECONDS

    last_seen_time = now

    # Misty FaceRecognition messages can provide
    # Bearing, Elevation, Distance.
    latest_face["bearing"] = (
        message.get("bearing")
        if message.get("bearing") is not None
        else message.get("Bearing", 0)
    )

    latest_face["elevation"] = (
        message.get("elevation")
        if message.get("elevation") is not None
        else message.get("Elevation", 0)
    )

    latest_face["distance"] = (
        message.get("distance")
        if message.get("distance") is not None
        else message.get("Distance")
    )

    if was_gone:

        first_seen_time = now

        # First contact:
        # acknowledge, but don't immediately talk.
        threading.Thread(
            target=notice_passed_person,
            daemon=True
        ).start()

    # Start dwell monitor once.
    if (
        not engagement_thread_running
        and not conversation_lock.locked()
    ):
        engagement_thread_running = True

        threading.Thread(
            target=engagement_monitor,
            daemon=True
        ).start()


# ============================================================
# AR CALLBACK
# ============================================================

def tag_callback(data):

    if (
        not accepting_cards
        or card_event.is_set()
    ):
        return

    message = data.get(
        "message",
        {}
    )

    tag_id = (
        message.get("tagId")
        if message.get("tagId") is not None
        else message.get("TagId")
    )

    if tag_id in TAG_FOR_ACTIVITY:

        chosen["tag"] = tag_id

        card_event.set()


# ============================================================
# STARTUP
# ============================================================

try:

    show_face("idle")

    neutral_head()

    misty.move_arms(
        90,
        90,
        duration=0.8
    )

    misty.change_led(
        100,
        70,
        160
    )

    # --------------------------------------------------------
    # Face events
    # --------------------------------------------------------

    misty.register_event(
        Events.FaceRecognition,
        "face_recognized",
        callback_function=face_callback,
        debounce=FACE_DEBOUNCE_MS,
        keep_alive=True
    )

    # --------------------------------------------------------
    # AR events
    # --------------------------------------------------------

    misty.register_event(
        Events.ArTagDetection,
        "tags_seen",
        callback_function=tag_callback,
        keep_alive=True
    )

    misty.start_face_recognition()

    misty.start_ar_tag_detector(
        dictionary=AR_DICTIONARY,
        tagSizeMm=TAG_SIZE_MM
    )

    print(
        "Window-display Misty is ready.",
        flush=True
    )

    misty.keep_alive()


finally:

    try:
        misty.stop_ar_tag_detector()
    except Exception:
        pass

    try:
        misty.stop_face_recognition()
    except Exception:
        pass

    try:
        neutral_head()
        misty.move_arms(
            90,
            90,
            duration=0.8
        )
    except Exception:
        pass

    misty.unregister_all_events()