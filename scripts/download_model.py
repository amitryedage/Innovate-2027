import urllib.request
import os
import sys

MODEL_URL  = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"
MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'assets', 'face_landmarker.task')
MODEL_PATH = os.path.abspath(MODEL_PATH)

def download():
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)

    if os.path.exists(MODEL_PATH):
        size = os.path.getsize(MODEL_PATH)
        if size > 100_000:
            print(f"Model already exists ({size:,} bytes)")
            print(f"   Path: {MODEL_PATH}")
            return True
        else:
            print(f" Existing file too small ({size} bytes) — re-downloading")
            os.remove(MODEL_PATH)

    print("Downloading face_landmarker.task (~3MB)...")
    print(f"From: {MODEL_URL}")
    print(f"To:   {MODEL_PATH}")

    def progress(count, block_size, total_size):
        if total_size > 0:
            pct = count * block_size * 100 // total_size
            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
            sys.stdout.write(f"\r  [{bar}] {pct}%")
            sys.stdout.flush()

    try:
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH, reporthook=progress)
        print()
        size = os.path.getsize(MODEL_PATH)
        print(f"\n Download complete! ({size:,} bytes)")
        print(f"   Saved to: {MODEL_PATH}")
        return True
    except Exception as e:
        print(f"\n Download failed: {e}")
        print("\nManual download instructions:")
        print(f"  1. Open this URL in your browser:")
        print(f"     {MODEL_URL}")
        print(f"  2. Save the file as: face_landmarker.task")
        print(f"  3. Move it to: {os.path.dirname(MODEL_PATH)}")
        return False

if __name__ == "__main__":
    success = download()
    if success:
        # Quick verify
        print("\nVerifying model loads correctly...")
        try:
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision

            base_opts = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
            opts = mp_vision.FaceLandmarkerOptions(
                base_options=base_opts,
                num_faces=1,
                min_face_detection_confidence=0.5,
                min_face_presence_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            landmarker = mp_vision.FaceLandmarker.create_from_options(opts)
            landmarker.close()
            print(" Model verified — loads and closes correctly.")
            print("\n You are ready for the testing of project in real world conditions! Run main.py to start the system.")
        except Exception as e:
            print(f"Model verification failed: {e}")
    sys.exit(0 if success else 1)