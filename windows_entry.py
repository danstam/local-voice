"""Private-runtime entry point, also used for workers and headless setup checks."""
from pathlib import Path
import sys
import os

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
if sys.platform == "win32":
    # Other applications may have set Tcl paths in the user's environment.
    for variable, directory in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        library = Path(sys.executable).parent / "tcl" / directory
        if library.is_dir():
            os.environ[variable] = str(library)


def main():
    if sys.argv[1:2] == ["--worker"]:
        from local_voice.inference_worker import main
    elif sys.argv[1:2] == ["--mac-setup"]:
        from local_voice.mac_setup import main
    elif sys.argv[1:2] == ["--setup"]:
        from local_voice.windows_setup import main
    elif sys.argv[1:2] == ["--maintenance"]:
        from local_voice.cli import main
    else:
        if sys.platform == "darwin":
            from local_voice.app_mac import main
            return main()
        from local_voice.windows_first_run import main
        return main()
    del sys.argv[1]
    return main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if sys.executable.lower().endswith("pythonw.exe"):
            from local_voice.paths import app_data_dir
            import traceback
            folder = app_data_dir()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / "windows-crash.log"
            target.write_text(traceback.format_exc(), encoding="utf-8")
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"Local Voice could not start. Details: {target}", "Local Voice", 0x10)
        else:
            raise
