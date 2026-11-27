from mistyPy.Robot import Robot
from mistyPy.Events import Events

misty = Robot("192.168.86.40")

def object_test(data):
    print("OBJECT:", data.get("message", data), flush=True)

try:
    misty.register_event(Events.ObjectDetection, "object_test",
                         callback_function=object_test, keep_alive=True)

    pose = misty.start_pose_estimation(minimumConfidence=0.5)
    print("POSE START:", pose.status_code, pose.json(), flush=True)

    obj = misty.start_object_detector(minimumConfidence=0.5)
    print("OBJECT START:", obj.status_code, obj.json(), flush=True)

    print("Stand in front of Misty", flush=True)
    misty.keep_alive()
finally:
    misty.stop_pose_estimation()
    misty.stop_object_detector()
    misty.unregister_all_events()