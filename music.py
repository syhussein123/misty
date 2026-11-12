import base64
from mistyPy.Robot import Robot

misty = Robot("192.168.0.149")

FILE_ON_MAC = "baby.mp3"   # the file in the Python-SDK folder
FILE_ON_MISTY = "baby.mp3" # the name it will have on Misty

# # Read the MP3 and convert it to text Misty can receive
# with open(FILE_ON_MAC, "rb") as f:
#     encoded = base64.b64encode(f.read()).decode("utf-8")

# # Upload it to Misty
# response = misty.save_audio(
#     fileName=FILE_ON_MISTY,
#     data=encoded,
#     immediatelyApply=True,
#     overwriteExisting=True
# )
# print("Upload status:", response.status_code, response.json(), flush=True)

# # Play it
# response = misty.play_audio(fileName=FILE_ON_MISTY, volume=100)
# print("Play status:", response.status_code, response.json(), flush=True)

beautifulmp3mac = "beautiful.mp3"  # the file in the Python-SDK folder
beautifulmp3 = "beautiful.mp3"  # the name it will have on Misty

# Read the MP3 and convert it to text Misty can receive
with open(beautifulmp3mac, "rb") as f:
    encoded = base64.b64encode(f.read()).decode("utf-8")

# Upload it to Misty
response = misty.save_audio(
    fileName=beautifulmp3,
    data=encoded,
    immediatelyApply=True,
    overwriteExisting=True
)
print("Upload status:", response.status_code, response.json(), flush=True)