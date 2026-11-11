# working with AR download codes

import time
import threading
from mistyPy.Robot import Robot
from mistyPy.Events import Events

misty = Robot()

GONE_AFTER_SECONDS = 5      # a face counts as "new" after this long with no sightings
FACE_DEBOUNCE_MS = 1000     # limit face events to about one per second
WAIT_FOR_CARD_SECONDS = 10  # how long to wait for a card before giving up
MAX_ROUNDS = 2              # how many cards one visitor can pick

AR_DICTIONARY = 7           # confirmed from the tag output
TAG_SIZE_MM = 100           # size the cards were printed at

# AR tag ID printed on each card -> activity name
TAG_FOR_ACTIVITY = {
    1: "dance",
    2: "sing",
    3: "joke",
}

last_seen_time = 0
conversation_lock = threading.Lock()  # only one visitor is talked to at a time
card_event = threading.Event()        # set when a valid card is seen
accepting_cards = False               # only read cards after Misty asks for one
chosen = {"tag": None}

def say(text, pause=None):
    # Speak, then wait roughly as long as the sentence takes so lines don't overlap.
    # The pause length is a rough estimate. Tune it by ear.
    misty.speak(text)
    time.sleep(pause if pause is not None else max(1.5, len(text) / 12))

def wave():
    # Positions in degrees: 90 is arm down, -45 is arm up (from the Misty docs).
    misty.move_arms(None, -45, duration=1.0)  # raise right arm
    time.sleep(1.0)
    for _ in range(3):
        misty.move_arms(None, -20, duration=0.3)  # wave out
        time.sleep(0.3)
        misty.move_arms(None, -45, duration=0.3)  # wave back
        time.sleep(0.3)
    misty.move_arms(None, 90, duration=1.0)  # lower arm
    time.sleep(1.0)

def do_dance():
    say("Okay, watch this. I'm going to dance!")
    misty.move_arms(-45, -45, duration=1.0)  # both arms up
    time.sleep(1.5)
    misty.move_arms(90, 90, duration=1.0)    # both arms down
    time.sleep(1.5)

def do_sing():
    say("Okay, here's a little song for you.")
    misty.change_led(0, 0, 255)
    say("La la la, la la la, I'm the happy robot behind the glass!")
    misty.change_led(100, 70, 160)

def do_joke():
    say("Okay, I have one for you.")
    time.sleep(0.5)
    say("Why did the robot cross the road?")
    time.sleep(1.5)
    say("To get to the other side!")

ACTIVITIES = {"dance": do_dance, "sing": do_sing, "joke": do_joke}

def wait_for_card():
    global accepting_cards  # without this, the flag never reaches tag_callback
    # Returns the chosen tag ID, or None if nobody picks a card in time
    card_event.clear()
    chosen["tag"] = None
    accepting_cards = True
    got_card = card_event.wait(timeout=WAIT_FOR_CARD_SECONDS)
    accepting_cards = False
    return chosen["tag"] if got_card else None

def run_conversation():
    with conversation_lock:
        say("Oh, hello there!", pause=1.0)
        wave()
        say("I'm Misty. Would you like to play a game?")
        say("Hold one of the cards up to the glass, and I'll show you what it does.")

        rounds = 0
        while rounds < MAX_ROUNDS:
            tag = wait_for_card()
            if tag is None:
                if rounds == 0:
                    say("No worries. Come back any time you like!")
                break

            rounds += 1
            ACTIVITIES[TAG_FOR_ACTIVITY[tag]]()

            if rounds < MAX_ROUNDS:
                say("Did you like that? Want to try another card?")

        say("Thanks for playing with me! Bye for now.")
        misty.move_arms(90, 90, duration=1.0)  # both arms down

def face_callback(data):
    global last_seen_time
    now = time.time()

    # A face is "new" only if we haven't seen one for a while
    is_new_face = (now - last_seen_time) > GONE_AFTER_SECONDS
    last_seen_time = now

    # Start a conversation in its own thread so this callback returns quickly
    if is_new_face and not conversation_lock.locked():
        threading.Thread(target=run_conversation, daemon=True).start()

def tag_callback(data):
    if not accepting_cards or card_event.is_set():
        return

    tag_id = data.get("message", {}).get("tagId")
    if tag_id in TAG_FOR_ACTIVITY:
        chosen["tag"] = tag_id
        card_event.set()

try:
    misty.register_event(Events.FaceRecognition, "face_recognized",
                         callback_function=face_callback,
                         debounce=FACE_DEBOUNCE_MS, keep_alive=True)
    misty.register_event(Events.ArTagDetection, "tags_seen",
                         callback_function=tag_callback, keep_alive=True)

    misty.start_face_recognition()
    misty.start_ar_tag_detector(dictionary=AR_DICTIONARY, tagSizeMm=TAG_SIZE_MM)
    print("Waiting for a face", flush=True)
    misty.keep_alive()
finally:
    misty.stop_ar_tag_detector()
    misty.stop_face_recognition()
    misty.unregister_all_events()