"""Hardware-free regressions. The root test.py is a legacy motor script."""
from dataclasses import replace
import base64
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch
from misty import (CardInput, Interaction, JOKES, Settings, Stopping, VisitPresence,
                   VisitorLeft, response_result)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class Response:
    def __init__(self, result=True, status="Success"):
        self.result, self.status = result, status

    def raise_for_status(self):
        pass

    def json(self):
        return {"status": self.status, "result": self.result}


class Robot:
    def __init__(self):
        self.calls, self.results, self.hooks = [], {}, {}

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.hooks:
                self.hooks[name](*args, **kwargs)
            return Response(self.results.get(name, True))
        return call


def person(left=80, right=240, confidence=0.9, description="person"):
    return {"message": {"description": description, "confidence": confidence,
                        "imageLocationLeft": left, "imageLocationRight": right,
                        "imageLocationTop": 60, "imageLocationBottom": 310}}


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.p = VisitPresence(Settings())

    def test_same_person_never_restarts_finished_visit(self):
        self.p.observe(0)
        self.assertEqual(self.p.claim(0), 1)
        for tick in range(1, 600):
            self.p.observe(tick / 2)
            self.assertIsNone(self.p.claim(tick / 2))

    def test_short_dropout_does_not_rearm(self):
        self.p.observe(0)
        self.p.claim(0)
        self.p.observe(3.9)
        self.assertIsNone(self.p.claim(3.9))

    def test_departure_and_reentry_create_one_new_visit(self):
        self.p.observe(0)
        self.p.claim(0)
        self.assertFalse(self.p.present(1, 4))
        self.p.observe(5)
        self.assertEqual(self.p.claim(5), 2)
        self.assertIsNone(self.p.claim(5))

    def test_reentry_between_worker_ticks(self):
        self.p.observe(0)
        self.p.claim(0)
        self.p.observe(5)
        self.assertFalse(self.p.present(1, 5))
        self.assertEqual(self.p.claim(5), 2)

    def test_single_observation_cannot_satisfy_dwell(self):
        self.p.observe(0)
        self.assertFalse(self.p.ready(1, 1.3))
        self.p.observe(1.3)
        self.assertFalse(self.p.ready(1, 1.3))
        for now in (1.8, 2.3, 2.6):
            self.p.observe(now)
        self.assertTrue(self.p.ready(1, 2.6))

    def test_our_look_away_does_not_count_as_departure(self):
        self.p.observe(0)
        self.p.claim(0)
        self.p.paused += 1
        self.assertTrue(self.p.present(1, 20))
        self.p.resume(20)
        self.p.observe(20.5)
        self.assertIsNone(self.p.claim(20.5))
        self.assertFalse(self.p.present(1, 25))


class CardTests(unittest.TestCase):
    def setUp(self):
        self.c = CardInput(1.0)

    def test_mapping_and_one_choice_per_round(self):
        for tag in (1, 2, 3):
            self.c.begin(1, 10)
            self.c.observe(tag, tag, 1, True)
            self.assertEqual(self.c.pending, ("valid", tag))
            self.c.observe(99, tag, 1, True)
            self.assertEqual(self.c.pending, ("valid", tag))

    def test_wrong_session_absence_and_deadline_cannot_select(self):
        self.c.begin(2, 10)
        for args in ((1, 1, 1, True), (1, 2, 2, False), (1, 10, 2, True)):
            self.c.observe(*args)
            self.assertIsNone(self.c.pending)

    def test_held_card_requires_removal_to_repeat(self):
        self.c.begin(1, 10)
        self.c.observe(1, 0, 1, True)
        self.c.close()
        for tick in range(1, 20):
            self.c.observe(1, tick / 2, 1, True)
        self.c.begin(1, 20)
        self.c.observe(1, 10, 1, True)
        self.assertIsNone(self.c.pending)
        self.c.observe(1, 11.1, 1, True)
        self.assertEqual(self.c.pending, ("valid", 1))

    def test_look_away_cannot_release_consumed_card(self):
        self.c.begin(1, 10)
        self.c.observe(1, 0, 1, True)
        self.c.close()
        self.c.resumed_view(20, 1)
        self.c.begin(1, 30)
        self.c.observe(1, 20.5, 1, True)
        self.assertIsNone(self.c.pending)

    def test_invalid_card_and_close(self):
        self.c.begin(1, 10)
        self.c.observe(17, 1, 1, True)
        self.assertEqual(self.c.pending, ("invalid", 17))
        self.c.close()
        self.c.observe(2, 2, 1, True)
        self.assertIsNone(self.c.pending)

    def test_card_reappearing_during_our_turn_does_not_release_it(self):
        self.c.begin(1, 10)
        self.c.observe(1, 0, 1, True)
        self.c.close()
        self.c.observe(1, 20, 1, True, reliable_view=False)
        self.c.resumed_view(20.1, 1)
        self.c.begin(1, 30)
        self.c.observe(1, 20.5, 1, True, reliable_view=False)
        self.assertIsNone(self.c.pending)
        self.assertIn(1, self.c.consumed)


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.robot = Clock(), Robot()
        self.app = Interaction(self.robot, clock=self.clock)
        self.app.person_callback(person())
        self.app.active_epoch = self.app.presence.claim(self.clock())

    def fake_pause(self, seconds, track=False):
        self.clock.now += seconds
        self.app.person_callback(person())
        self.app.checkpoint()

    def test_only_valid_person_refreshes_presence(self):
        before = self.app.presence.last_seen
        self.clock.now += 2
        for event in ({}, {"message": None}, person(description="chair"),
                      person(confidence=0.2), person(left=-30, right=-10),
                      person(confidence=float("nan"))):
            self.app.person_callback(event)
        self.assertEqual(self.app.presence.last_seen, before)

    def test_live_boxes_crossing_frame_edge_still_count_as_people(self):
        self.clock.now += 0.3
        self.app.person_callback(person(left=-0.027, right=340))
        self.assertEqual(self.app.presence.last_seen, self.clock())
        self.assertEqual(self.app.target["box"], (0, 60, 320, 310))

    def test_callbacks_never_send_robot_commands(self):
        self.app.person_callback(person())
        self.app.tag_callback({"message": {"tagId": 1}})
        self.app.actuator_callback({"message": {"sensorId": "ahy", "value": 10}})
        self.app.audio_callback({"message": {"metaData": {"name": "baby.mp3"}}})
        self.assertEqual(self.robot.calls, [])

    def test_concurrent_observations_produce_one_visit_claim(self):
        self.app.presence = VisitPresence(Settings())
        threads = [threading.Thread(target=self.app.person_callback, args=(person(),)) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.app.presence.claim(self.clock()), 1)
        self.assertIsNone(self.app.presence.claim(self.clock()))

    def test_target_is_stable_when_another_person_appears(self):
        self.app.target = None
        self.app.person_callback(person(left=10, right=110))
        self.clock.now += 0.2
        self.app.person_callback(person(left=200, right=310))
        self.assertEqual(self.app.target["box"][0], 10)
        self.assertEqual(self.app.presence.last_seen, self.clock())

    def test_gaze_uses_head_feedback_and_each_frame_once(self):
        self.app.target = None
        self.app.actuator_callback({"message": {"sensorId": "ahy", "value": 10}})
        self.app.person_callback(person(left=0, right=80))
        self.app.look_toward_person()
        self.assertEqual(self.robot.calls[0][2]["yaw"], 14)
        self.clock.now += 0.4
        self.app.look_toward_person()
        self.assertEqual(len(self.robot.calls), 1)

    def test_return_to_neutral_gets_full_observation_interval(self):
        self.clock.now += 3
        self.app.reset_pose()
        self.assertTrue(self.app.presence.present(1, 104))
        self.assertTrue(self.app.presence.present(1, 108))
        self.assertFalse(self.app.presence.present(1, 108.5))

    def test_social_gaze_motion_is_not_departure_evidence(self):
        self.clock.now += 3
        self.app.move_head(yaw=10)
        self.assertTrue(self.app.presence.present(1, 104))
        self.assertFalse(self.app.presence.present(1, 108.5))

    def test_timeout_retries_without_greeting_again_or_saying_goodbye(self):
        for name in ("show_face", "mutual_gaze", "wave", "say", "explain_choices", "card_cue"):
            setattr(self.app, name, Mock())
        self.app.wait_for_card = Mock(side_effect=[None, 2, VisitorLeft()])
        self.app.acknowledge_card = Mock()
        self.app.polite_goodbye = Mock()
        with self.assertRaises(VisitorLeft):
            self.app.run_conversation()
        self.app.wait_for_card.assert_has_calls([call(), call(retry=True), call()])
        self.app.acknowledge_card.assert_called_once_with(2)
        self.app.explain_choices.assert_called_once()
        self.app.wave.assert_called_once()
        self.app.polite_goodbye.assert_not_called()

    def test_departure_wins_over_pending_card(self):
        def pause(seconds, track=False):
            self.app.cards.pending = ("valid", 1)
            self.clock.now += 5
        self.app.pause = pause
        with self.assertRaises(VisitorLeft):
            self.app.wait_for_card()
        self.assertFalse(self.app.cards.accepting)

    def test_invalid_card_repairs_then_accepts_valid_card(self):
        remaining = iter((99, 2))
        self.app.repair = Mock()
        def pause(seconds, track=False):
            self.fake_pause(seconds)
            self.app.tag_callback({"message": {"tagId": next(remaining)}})
        self.app.pause = pause
        self.assertEqual(self.app.wait_for_card(), 2)
        self.app.repair.assert_called_once()
        self.assertFalse(self.app.cards.accepting)

    def test_wait_reminds_once_and_keeps_deadline(self):
        self.app.pause = self.fake_pause
        self.app.gaze_aversion = Mock(side_effect=lambda: self.fake_pause(0.8))
        self.app.card_cue = Mock()
        self.app.say = Mock()
        self.assertIsNone(self.app.wait_for_card())
        self.app.say.assert_called_once()
        self.assertLess(self.clock(), 111)

    def test_rescan_still_accepts_a_card_after_thirty_seconds(self):
        self.app.say = Mock()
        def pause(seconds, track=False):
            self.fake_pause(seconds)
            if self.clock() >= 135:
                self.app.tag_callback({"message": {"tagId": 2}})
        self.app.pause = pause
        self.assertEqual(self.app.wait_for_card(retry=True), 2)
        self.app.say.assert_called_once()
        self.assertGreaterEqual(self.clock(), 135)
        self.assertFalse(self.app.cards.accepting)

    def test_rescan_accepts_card_during_retry_prompt(self):
        self.app.say = Mock(side_effect=lambda *_: self.app.tag_callback(
            {"message": {"tagId": 3}}))
        self.assertEqual(self.app.wait_for_card(retry=True), 3)

    def test_rescan_ends_when_visitor_leaves(self):
        self.app.say = Mock()
        def pause(seconds, track=False):
            self.clock.now += 5
            self.app.checkpoint()
        self.app.pause = pause
        with self.assertRaises(VisitorLeft):
            self.app.wait_for_card(retry=True)
        self.assertFalse(self.app.cards.accepting)

    def test_audio_completion_matches_selected_file(self):
        self.app.audio_expected = "baby.mp3"
        self.app.audio_callback({"message": {"metaData": {"name": "other.mp3"}}})
        self.assertFalse(self.app.audio_complete)
        self.app.audio_callback({"message": {"metaData": {"name": "baby.mp3"}}})
        self.assertTrue(self.app.audio_complete)

    def test_music_completion_and_timeout_cleanup(self):
        self.app.songs = ["baby.mp3"]
        self.app.pause = self.fake_pause
        self.robot.hooks["play_audio"] = lambda **_: self.app.audio_callback(
            {"message": {"metaData": {"name": "baby.mp3"}}})
        self.app.do_sing()
        self.assertEqual(self.clock(), 100)
        self.robot.hooks.clear()
        self.app.settings = replace(self.app.settings, activity_timeout=0.5)
        with self.assertRaisesRegex(RuntimeError, "completion timed out"):
            self.app.do_sing()
        self.assertIsNone(self.app.audio_expected)
        self.assertIn("stop_audio", [call[0] for call in self.robot.calls])

    def test_activity_failure_repairs_and_keeps_rescanning(self):
        for name in ("show_face", "mutual_gaze", "wave", "say", "explain_choices", "card_cue"):
            setattr(self.app, name, Mock())
        self.app.wait_for_card = Mock(side_effect=[1, None, VisitorLeft()])
        self.app.acknowledge_card = Mock(side_effect=RuntimeError("failed"))
        self.app.repair = Mock()
        self.app.polite_goodbye = Mock()
        with self.assertLogs("misty", level="ERROR"):
            with self.assertRaises(VisitorLeft):
                self.app.run_conversation()
        self.app.repair.assert_called_once()
        self.app.polite_goodbye.assert_not_called()

    def test_three_activities_are_dispatched_in_the_same_visit(self):
        for name in ("mutual_gaze", "wave", "say", "explain_choices", "card_cue",
                     "do_dance", "do_sing", "do_joke"):
            setattr(self.app, name, Mock())
        self.app.pause = self.fake_pause
        self.app.wait_for_card = Mock(side_effect=[1, 2, 3, VisitorLeft()])
        with self.assertRaises(VisitorLeft):
            self.app.run_conversation()
        for action in (self.app.do_dance, self.app.do_sing, self.app.do_joke):
            action.assert_called_once()
        self.app.explain_choices.assert_called_once()
        self.assertEqual(self.app.active_epoch, 1)

    def test_dance_rotates_with_music_and_preserves_visit_while_camera_turns(self):
        self.app.say = Mock()
        self.app.songs = ["baby.mp3"]
        def pause(seconds, track=False):
            # No person observations while the robot rotates away.
            self.clock.now += seconds
            self.app.checkpoint()
        self.app.pause = pause
        self.app.do_dance()
        names = [item[0] for item in self.robot.calls]
        self.assertLess(names.index("play_audio"), names.index("drive_time"))
        self.assertLess(names.index("drive_time"), names.index("stop_audio"))
        drive = next(item for item in self.robot.calls if item[0] == "drive_time")
        self.assertEqual(drive[2], {"linearVelocity": 0, "angularVelocity": -100.0, "timeMs": 8550})
        self.assertIn("stop", names)
        self.assertEqual(self.app.presence.epoch, 1)
        self.assertIsNone(self.app.presence.claim(self.clock()))
        self.assertEqual(self.app.presence.paused, 0)

    def test_interrupted_dance_stops_wheels_and_music(self):
        self.app.say = Mock()
        self.app.songs = ["baby.mp3"]
        self.app.pause = Mock(side_effect=Stopping())
        with self.assertRaises(Stopping):
            self.app.do_dance()
        names = [item[0] for item in self.robot.calls]
        self.assertIn("drive_time", names)
        self.assertIn("stop", names)
        self.assertIn("stop_audio", names)
        self.assertEqual(self.app.presence.paused, 0)
        self.assertIsNone(self.app.audio_expected)

    def test_missing_music_is_uploaded_without_playing_or_overwriting(self):
        self.app.settings = replace(self.app.settings, songs=("song.mp3",))
        self.robot.results["get_audio_list"] = [{"name": "song.mp3"}]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "song.mp3").write_bytes(b"example audio")
            self.app.ensure_music_assets(directory)
        upload = next(item for item in self.robot.calls if item[0] == "save_audio")
        self.assertEqual(base64.b64decode(upload[2]["data"]), b"example audio")
        self.assertFalse(upload[2]["immediatelyApply"])
        self.assertFalse(upload[2]["overwriteExisting"])
        self.assertEqual(self.app.songs, ["song.mp3"])
        self.assertNotIn("play_audio", [item[0] for item in self.robot.calls])

    def test_existing_music_is_reused_without_upload(self):
        self.app.songs = list(self.app.settings.songs)
        self.app.ensure_music_assets()
        self.assertEqual(self.robot.calls, [])

    def test_failed_session_stays_claimed_and_closes_cards(self):
        self.app.brief_glance = Mock(side_effect=RuntimeError("failed"))
        self.app.cards.begin(1, 110)
        with self.assertLogs("misty", level="ERROR"):
            self.app.handle_visit(1)
        self.assertIsNone(self.app.presence.claim(self.clock()))
        self.assertFalse(self.app.cards.accepting)
        self.assertIsNone(self.app.active_epoch)

    def test_shutdown_interrupts_wait(self):
        self.app.stop_event.set()
        with self.assertRaises(Stopping):
            self.app.wait_for_card()
        self.assertFalse(self.app.cards.accepting)

    def test_closed_connection_stops_instead_of_rearming(self):
        self.app.registrations = [Mock(is_active=False)]
        with self.assertRaisesRegex(RuntimeError, "event connection closed"):
            self.app.checkpoint()
        self.assertTrue(self.app.stop_event.is_set())

    def test_callback_wrapper_satisfies_sdk_argcount(self):
        registration = Mock(is_active=True)
        def register(event_type, name, callback_function, **kwargs):
            self.assertEqual(callback_function.__code__.co_argcount, 1)
            callback_function(person())
            return registration
        self.robot.register_event = register
        self.app.subscribe("ObjectDetection", "people", self.app.person_callback)
        self.assertEqual(self.app.registrations, [registration])

    def test_robot_errors_are_not_success(self):
        for response in (Response("bad parameters", "Failed"), Response(False)):
            with self.assertRaises(RuntimeError):
                response_result(response)

    def test_already_stopped_audio_is_a_valid_cleanup_result(self):
        self.robot.results["stop_audio"] = False
        self.assertFalse(self.app.command("stop_audio"))

    def test_silent_prompts_preserve_joke_speech(self):
        self.app.settings = replace(self.app.settings, spoken_prompts=False)
        self.app.say("hello")
        self.assertEqual(self.robot.calls, [])
        self.robot.hooks["speak"] = lambda *_, **kwargs: self.app.speech_callback(
            {"message": {"utteranceId": kwargs["utteranceId"]}})
        self.app.say("punchline", activity=True)
        self.assertEqual(self.robot.calls[0], ("set_default_volume", (50,), {}))
        self.assertEqual(self.robot.calls[1][0], "speak")
        self.assertEqual(self.robot.calls[1][2]["speechRate"], 1.75)

    def test_music_action_owns_motion_until_playback_finishes(self):
        self.app.actions = {"love": "Love"}
        self.app.songs = ["baby.mp3"]
        self.robot.hooks["play_audio"] = lambda **_: self.app.audio_callback(
            {"message": {"metaData": {"name": "baby.mp3"}}})
        self.app.do_sing()
        names = [item[0] for item in self.robot.calls]
        self.assertLess(names.index("start_action"), names.index("play_audio"))
        self.assertLess(names.index("stop_audio"), names.index("stop_action"))
        self.assertLess(names.index("stop_action"), names.index("move_head"))
        self.assertEqual(self.app.presence.paused, 0)
        self.assertIsNone(self.app.running_action)

    def test_music_interruption_also_stops_native_action(self):
        self.app.actions = {"love": "Love"}
        self.app.songs = ["baby.mp3"]
        self.app.pause = Mock(side_effect=Stopping())
        with self.assertRaises(Stopping):
            self.app.do_sing()
        names = [item[0] for item in self.robot.calls]
        self.assertIn("stop_audio", names)
        self.assertIn("stop_action", names)
        self.assertEqual(self.app.presence.paused, 0)

    def test_unknown_native_action_falls_back_without_commands(self):
        with self.app.native_action("missing"):
            pass
        self.assertEqual(self.robot.calls, [])

    def test_random_joke_preserves_matching_punchline(self):
        for name in ("mutual_gaze", "say", "gaze_aversion", "pause", "show_face"):
            setattr(self.app, name, Mock())
        with patch("misty.random.choice", return_value=JOKES[0]):
            self.app.do_joke()
        self.app.say.assert_has_calls([call(JOKES[0][0], activity=True),
                                      call(JOKES[0][1], activity=True)])


if __name__ == "__main__":
    unittest.main()
