# Flattens all video files from subdirectories into the root directory,
# and removes any subdirectories that are empty after flattening.
# Requires kid3-cli (for MP4) and mkvpropedit (for MKV) to be installed.

import glob
import subprocess
import shutil
import os
import sys


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def flatten():
    for pattern in ("**/*.mp4", "**/*.mkv"):
        for filepath in glob.glob(pattern, recursive=True):
            abs_filepath = os.path.abspath(filepath)
            if os.path.dirname(abs_filepath) == ROOT_DIR:
                continue

            filename = os.path.basename(abs_filepath)
            destination = os.path.join(ROOT_DIR, filename)

            if os.path.exists(destination):
                print(f"Skipping {filepath} — {filename} already exists in root")
                continue

            shutil.move(abs_filepath, destination)
            print(f"Moved {filepath} to {destination}")


def cleanup():
    for dirpath, dirnames, filenames in os.walk(ROOT_DIR, topdown=False):
        if dirpath == ROOT_DIR:
            continue

        if not os.listdir(dirpath):
            os.rmdir(dirpath)
            print(f"Removed empty directory {dirpath}")


if __name__ == "__main__":
    flatten()
    cleanup()
    sys.exit(0)