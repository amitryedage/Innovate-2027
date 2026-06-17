# Script to generate audio files for fatigue detection system
# We need need internet to generate the voice files using gTTS, but the beep can be generated programmatically without internet. This script can be run once to create the necessary audio files in the AUDIO_DIR folder.
# We are using gTTS for voice generation because it provides good quality TTS in multiple languages and is easy to use. The beep sound is generated programmatically to ensure we have a consistent alert sound without relying on TTS for the basic alert. The script also checks if the files already exist to avoid unnecessary regeneration, which is helpful during development and testing.
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import AUDIO_DIR

# AUDIO SCRIPTS — what each file says
# Need to generate 6 voice files (L2/L3 in Hindi/Marathi + camera check + calibration prompt) + 1 beep file for L1 alert. The beep is generated programmatically, the rest use gTTS.
# Try to automatcuic excution when we have alreday this file and we can add more scripts here in the future if needed. The description field is just for our reference when generating and testing the files.

AUDIO_SCRIPTS = {
    # Level 1 — mild fatigue (beep only, no voice needed)
    # We generate a short tone using numpy + wave instead of gTTS
    "l1_beep.mp3": None,   # handled separately below

    # Level 2 — moderate fatigue — Hindi
    "l2_hindi.mp3": {
        "text": "सावधान! आपकी आँखें बंद हो रही हैं। कृपया रुकें और आराम करें।",
        "lang": "hi",
        "description": "Hindi L2: Eyes closing warning"
    },

    # Level 2 — moderate fatigue — Marathi
    "l2_marathi.mp3": {
        "text": "सावधान! तुमचे डोळे बंद होत आहेत. कृपया थांबा आणि विश्रांती घ्या.",
        "lang": "mr",
        "description": "Marathi L2: Eyes closing warning"
    },

    # Level 3 — critical — Hindi
    "l3_hindi.mp3": {
        "text": "खतरा! तुरंत रुकें! आप सो रहे हैं! मशीन बंद करें! अभी रुकें!",
        "lang": "hi",
        "description": "Hindi L3: Critical stop machine"
    },

    # Level 3 — critical — Marathi
    "l3_marathi.mp3": {
        "text": "धोका! ताबडतोब थांबा! तुम्ही झोपत आहात! मशीन बंद करा! आत्ता थांबा!",
        "lang": "mr",
        "description": "Marathi L3: Critical stop machine"
    },

    # Camera obstruction check
    "camera_check.mp3": {
        "text": "कृपया कैमरा साफ करें। ऑपरेटर दिखाई नहीं दे रहा।",
        "lang": "hi",
        "description": "Hindi: Please clean camera"
    },

    # Calibration start prompt
    "calibration_start.mp3": {
        "text": "कैलिब्रेशन शुरू हो रही है। कृपया सीधे कैमरे की तरफ देखें और आराम से बैठें।",
        "lang": "hi",
        "description": "Hindi: Calibration starting prompt"
    },
}

# Simple for the testing purpose, we will generate a simple beep sound programmatically for level 1 alert instead of using gTTS. This ensures we have a consistent alert sound without relying on text-to-speech for the basic alert.

# Main function to generate all audio files(Entry point)

def main():

    print("  Audio file generation for fatigue detection system")
    
    os.makedirs(AUDIO_DIR, exist_ok=True)
    print(f"\nOutput folder: {AUDIO_DIR}\n")

    generated = 0
    skipped   = 0
    failed    = 0

    for filename, script in AUDIO_SCRIPTS.items():
        filepath = os.path.join(AUDIO_DIR, filename)

        # Skip if already exists and is large enough
        if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
            print(f"   {filename} already exists — skipping")
            skipped += 1
            continue

        # Level 1 beep — generated programmatically
        if filename == "l1_beep.mp3":
            print(f"   Generating {filename} (programmatic beep)...")
            if generate_beep(filepath):
                size = os.path.getsize(filepath)
                print(f"      {filename} ({size:,} bytes)")
                generated += 1
            else:
                print(f"       {filename} generated as placeholder")
                generated += 1
            continue

        # Voice files — use gTTS
        print(f"  🎙  Generating {filename}...")
        print(f"     {script['description']}")
        print(f"     Text: {script['text'][:60]}...")

        if generate_voice(filepath, script["text"], script["lang"]):
            size = os.path.getsize(filepath)
            print(f"      {filename} ({size:,} bytes)")
            generated += 1
        else:
            failed += 1

    # Summary
    print("\n" + "="*55)
    print(f"  Generated: {generated}")
    print(f"  Skipped:   {skipped} (already existed)")
    print(f"  Failed:    {failed}")

    if failed == 0:
        print("\n  All audio files ready!")
        print("  Run python main.py — alerts will play in Hindi/Marathi")
    else:
        print(f"\n    {failed} files failed.")
        print("  Check internet connection (gTTS needs internet once).")
        print("  Alert engine will fall back to console output.")

    print("="*55)

    # Quick playback test
    if generated > 0 or skipped > 0:
        print("\n  Testing audio playback...")
        try:
            import pygame
            pygame.mixer.init()
            test_file = os.path.join(AUDIO_DIR, "l1_beep.mp3")
            if os.path.exists(test_file) and os.path.getsize(test_file) > 100:
                sound = pygame.mixer.Sound(test_file)
                sound.play()
                import time; time.sleep(0.8)
                print("  Audio playback test passed!")
            pygame.mixer.quit()
        except ImportError:
            print("    pygame not installed — audio test skipped")
            print("  Run: pip install pygame")
        except Exception as e:
            print(f"    Audio test: {e}")


if __name__ == "__main__":
    main()