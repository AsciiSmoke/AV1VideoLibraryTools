# Recursively scans the current directory for .mp4 and .mkv video files and
# updates each file's title metadata tag to match its filename (without extension).
# Requires kid3-cli (for MP4) and mkvpropedit (for MKV) to be installed.

import glob
import subprocess
import shutil
import os
import sys


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def mp4_string(title):
    name, name_extension = os.path.splitext(os.path.basename(title))
    return name


def mkv_string(title):
    name, name_extension = os.path.splitext(os.path.basename(title))
    return name


def exec_mp4(mp4name):
    mp4name_title = mp4_string(mp4name)
    subprocess.run(["kid3-cli", "-c", f"set title '{mp4name_title}'", mp4name])


def exec_mkv(mkvname):
    mkvname_title = mkv_string(mkvname)
    subprocess.run(["mkvpropedit", "-e", "-q", "info", "-s", f"title={mkvname_title}", mkvname])


def mp4():
    for mp4name in glob.glob("**/*.mp4", recursive=True):
        exec_mp4(mp4name)
        print(f"Title metadata of {mp4name} has been changed")


def mkv():
    for mkvname in glob.glob("**/*.mkv", recursive=True):
        exec_mkv(mkvname)
        print(f"Title metadata of {mkvname} has been changed")


if __name__ == "__main__":
    mp4()
    mkv()
    sys.exit(0)