import os
import sys
import traceback
from pathlib import Path


def _should_launch_gui() -> bool:
    exe = os.path.basename(sys.executable).lower()
    if not exe.startswith("guildrun"):
        return False
    return len(sys.argv) <= 1 and (not sys.argv or sys.argv[0] in ("", "-"))


def _report_startup_failure(exc: BaseException) -> None:
    detail = "".join(traceback.format_exception(exc))
    message = (
        "Guildrun Demo 日本語化MODを起動できませんでした。\n\n"
        f"{exc}\n\n{detail}"
    )
    try:
        Path(sys.executable).resolve().with_name(
            "Guildrun日本語化_更新確認.txt"
        ).write_text(message, encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                str(exc),
                "Guildrun Demo 日本語化MOD",
                0x00000010,
            )
        except Exception:
            pass


if _should_launch_gui():
    try:
        from guildrun_exe_patcher import entrypoint

        result = entrypoint()
    except BaseException as exc:
        _report_startup_failure(exc)
        result = 1
    os._exit(int(result) if isinstance(result, int) else 0)
