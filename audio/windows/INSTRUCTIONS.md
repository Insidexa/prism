# Capturing the Windows B&O / DTS sound (for Claude on Windows)

Goal: record what the Windows audio processing (DTS:X Ultra with the Bang & Olufsen
tuning, controlled from OMEN Audio Control / Realtek Audio Console) does to the
sound on this HP Omen 16-c0xxx, so the same sound can be rebuilt on Linux with
Prism / EasyEffects. Everything here is read-only for the system: do not change
drivers, registry values or audio settings other than the B&O/DTS on/off switch
the steps ask for.

## Steps

1. Install Python 3 if missing (`winget install Python.Python.3.12`), then:
   ```
   cd prism\audio\windows
   py -m pip install numpy soundcard
   ```
2. Inventory (driver versions, audio-effect registry settings, tuning files):
   ```
   py capture.py inventory
   ```
3. Prepare the speakers:
   - unplug headphones, disconnect HDMI/Bluetooth audio, set **Speakers** as default output
   - Windows volume to **50%** and leave it there for every capture
   - in OMEN Audio Control (or Realtek Audio Console / DTS app), make sure the
     **B&O / DTS processing is ON** with its default preset; note the preset name
4. Capture with processing ON (the speakers will play sweeps for ~1 minute):
   ```
   py capture.py capture dts_on
   ```
5. Turn the B&O / DTS processing **OFF** in the same app (or in Sound settings >
   Speakers > Properties, turn off "Audio enhancements"), keep volume at 50%, then:
   ```
   py capture.py capture dts_off
   ```
6. Check that the loopback recording saw the processing:
   ```
   py capture.py compare
   ```
   - If it says loopback **sees** the processing: done.
   - If it says loopback does **not** see it: the effect runs after the
     loopback tap. Write that down in `prism-capture\NOTES.txt`; the inventory
     files are then the main result. (A microphone measurement can be done later.)
7. Turn the B&O / DTS processing back **ON** so Windows is as it was.
8. Write `prism-capture\NOTES.txt` with: the app used, preset name, Windows
   volume, anything unusual (warnings, errors, a different speaker name).
9. Pack and hand over:
   ```
   py capture.py pack
   ```
   Copy `prism-capture.zip` to the **"New Volume"** drive (the shared NTFS data
   partition), or commit it to a branch `windows-capture` of this repository if
   the user prefers. Do not upload it anywhere else: it contains file listings
   of this computer.
