from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import hashlib
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from pathlib import Path
from typing import Any

import UnityPy
from UnityPy.enums import ArchiveFlags
from UnityPy.streams import EndianBinaryReader


def _embed_launcher_base() -> Path | None:
    """署名済みembeddable python経由（pythonw.exeをブランド名にリネーム）で
    起動された場合、実行ファイルのあるフォルダーを基準ディレクトリとする。
    開発時（python.exeで直接実行）はNoneを返し、従来の__file__基準に戻す。"""
    exe = Path(sys.executable).resolve()
    if exe.name.lower().startswith("guildrun"):
        return exe.parent
    return None


def load_embedded_versions() -> dict[str, str]:
    base = _embed_launcher_base()
    if base is not None:
        path = base / "versions.json"
    else:
        path = Path(__file__).resolve().parent.parent / "versions.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("format") != 1:
        raise RuntimeError("未対応のバージョン設定です")
    result: dict[str, str] = {}
    patterns = {
        "exe_version": r"\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?",
    }
    if base is None:
        patterns["app_version"] = r"\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?"
    for key, pattern in patterns.items():
        value = str(raw.get(key, ""))
        if not re.fullmatch(pattern, value):
            raise RuntimeError(f"バージョン設定が不正です: {key}")
        result[key] = value
    return result


BUILD_VERSIONS = load_embedded_versions()
PACKAGE_VERSION = BUILD_VERSIONS["exe_version"]
# 配布時にversions.jsonのapp_versionへ置換する。開発実行時は同じ設定元を読む。
APP_VERSION = "0.4.16"
if APP_VERSION == "__APP_VERSION__":
    APP_VERSION = BUILD_VERSIONS["app_version"]
DATA_FILENAME = "data"
RESULT_FILENAME = "Guildrun日本語化_更新確認.txt"
APPLY_TEMP_PREFIX = ".guildrun_jp_apply_"
APPLY_STATE_FILENAME = "apply_state.json"
UPDATE_STATE_FILENAME = ".guildrun_jp_update_state.json"
CONTENT_UPDATE_TEMP_PREFIX = ".guildrun_jp_content_update_"
PRESERVED_LOCAL_CONTENT_PATHS = {"OFL.txt"}
AUTO_UPDATE_APP_PATHS = {
    "runtime/app/guildrun_exe_patcher.py",
    "runtime/app/sitecustomize.py",
}
PATCHER_FILENAME = "Guildrun日本語化.exe"
GAME_EXE_FILENAME = "Guildrun.exe"
REPOSITORY = "tomate045/Guildrun-Japanese-Mod"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
INSTALLER_ZIP_NAME = "Guildrun-Japanese-Mod.zip"
INSTALLER_URL = (
    f"https://github.com/{REPOSITORY}/releases/latest/download/"
    + urllib.parse.quote(INSTALLER_ZIP_NAME, safe="")
)
MANIFEST_URL = (
    f"https://raw.githubusercontent.com/{REPOSITORY}/main/latest.json"
)
MANIFEST_REF_URL = (
    f"https://api.github.com/repos/{REPOSITORY}/git/ref/heads/main"
)
HTTP_TIMEOUT_SECONDS = 30
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_UPDATE_BYTES = 256 * 1024 * 1024
HTTP_USER_AGENT = f"GuildrunJapaneseMod/{APP_VERSION}"


class PatchCompatibilityError(RuntimeError):
    pass


class ApplyRollbackError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def app_dir() -> Path:
    base = _embed_launcher_base()
    if base is not None:
        return base
    return Path(__file__).resolve().parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_update_relative(raw: str) -> Path:
    normalized = raw.replace("\\", "/")
    path = Path(normalized)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise RuntimeError(f"更新情報に不正なパスがあります: {raw}")
    return path


def checked_update_destination(root: Path, relative: Path) -> Path:
    destination = (root / relative).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"更新先が配布フォルダー外です: {relative}") from exc
    return destination


def allowed_content_update_path(relative: Path) -> bool:
    text = relative.as_posix()
    return (
        len(relative.parts) >= 2
        and relative.parts[0] == DATA_FILENAME
    ) or text in AUTO_UPDATE_APP_PATHS


def expected_raw_url(commit: str, relative: Path) -> str:
    encoded = urllib.parse.quote(relative.as_posix(), safe="/")
    return (
        f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{encoded}"
    )


def download_bytes(
    url: str,
    max_bytes: int,
    accept: str = "application/octet-stream",
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": HTTP_USER_AGENT,
            "Accept": accept,
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > max_bytes:
                raise RuntimeError("ダウンロード対象が許容サイズを超えています")
            payload = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GitHubからの取得に失敗しました（HTTP {exc.code}）"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHubへ接続できません: {exc.reason}") from exc
    if len(payload) > max_bytes:
        raise RuntimeError("ダウンロード対象が許容サイズを超えています")
    return payload


def validate_download(payload: bytes, entry: dict[str, Any], label: str) -> None:
    expected_size = int(entry["size"])
    if len(payload) != expected_size:
        raise RuntimeError(
            f"{label}のサイズが一致しません"
            f"（想定 {expected_size:,} / 取得 {len(payload):,} bytes）"
        )
    actual_hash = sha256(payload)
    if actual_hash != str(entry["sha256"]):
        raise RuntimeError(f"{label}のSHA-256照合に失敗しました")


def validate_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("format") != 3:
        raise RuntimeError("未対応の更新情報です")
    if raw.get("manifest_url") != MANIFEST_URL:
        raise RuntimeError("更新情報の取得先が一致しません")
    if raw.get("repository") != REPOSITORY_URL:
        raise RuntimeError("更新情報のリポジトリが一致しません")
    if not re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?",
        str(raw.get("data_version", "")),
    ):
        raise RuntimeError("翻訳データのバージョン情報が不正です")
    installer_for_version = raw.get("installer")
    fallback_app_version = (
        installer_for_version.get("version", "")
        if isinstance(installer_for_version, dict)
        else ""
    )
    remote_app_version = str(
        raw.get("app_version", fallback_app_version)
    )
    if not re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?",
        remote_app_version,
    ):
        raise RuntimeError("アプリのバージョン情報が不正です")
    commit = str(raw.get("content_commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("更新情報のコミットIDが不正です")

    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("更新対象ファイルが登録されていません")
    seen: set[str] = set()
    total_size = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise RuntimeError("更新対象ファイルの形式が不正です")
        relative = safe_update_relative(str(entry.get("path", "")))
        relative_text = relative.as_posix()
        if not allowed_content_update_path(relative):
            raise RuntimeError(f"更新対象外のパスです: {relative_text}")
        if relative_text in seen:
            raise RuntimeError(f"更新対象が重複しています: {relative_text}")
        seen.add(relative_text)
        size = int(entry.get("size", -1))
        if size < 0 or size > MAX_FILE_BYTES:
            raise RuntimeError(f"更新対象のサイズが不正です: {relative_text}")
        total_size += size
        if total_size > MAX_UPDATE_BYTES:
            raise RuntimeError("更新対象の合計サイズが許容値を超えています")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise RuntimeError(f"更新対象のSHA-256が不正です: {relative_text}")
        if entry.get("update") != "automatic":
            raise RuntimeError(f"自動更新対象ではありません: {relative_text}")
        if entry.get("url") != expected_raw_url(commit, relative):
            raise RuntimeError(f"更新対象のURLが不正です: {relative_text}")
        if entry.get("role") != "content":
            raise RuntimeError(f"更新対象の役割が不正です: {relative_text}")

    installer = raw.get("installer")
    if not isinstance(installer, dict):
        raise RuntimeError("インストーラ情報がありません")
    if not re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?",
        str(installer.get("version", "")),
    ):
        raise RuntimeError("配布パッケージのバージョン情報が不正です")
    if installer.get("url") != INSTALLER_URL:
        raise RuntimeError("インストーラの配布先が一致しません")

    patch_notes = raw.get("patch_notes")
    if not isinstance(patch_notes, dict):
        raise RuntimeError("パッチノート情報がありません")
    notes_relative = safe_update_relative(str(patch_notes.get("path", "")))
    notes_size = int(patch_notes.get("size", -1))
    if notes_size < 0 or notes_size > MAX_MANIFEST_BYTES:
        raise RuntimeError("パッチノートのサイズが不正です")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(patch_notes.get("sha256", "")),
    ):
        raise RuntimeError("パッチノートのSHA-256が不正です")
    if patch_notes.get("url") != expected_raw_url(commit, notes_relative):
        raise RuntimeError("パッチノートのURLが不正です")
    return raw


def fetch_manifest() -> dict[str, Any]:
    ref_payload = download_bytes(
        MANIFEST_REF_URL,
        MAX_MANIFEST_BYTES,
        "application/vnd.github+json",
    )
    try:
        ref_data = json.loads(ref_payload.decode("utf-8"))
        if ref_data.get("ref") != "refs/heads/main":
            raise RuntimeError("GitHubのmain参照が一致しません")
        head_commit = str(ref_data["object"]["sha"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("GitHubのmain参照を読み取れません") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", head_commit):
        raise RuntimeError("GitHubのmainコミットIDが不正です")
    immutable_url = expected_raw_url(head_commit, Path("latest.json"))
    payload = download_bytes(immutable_url, MAX_MANIFEST_BYTES)
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("更新情報を読み取れません") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("更新情報の形式が不正です")
    return validate_manifest(raw)


def fetch_patch_notes(manifest: dict[str, Any]) -> str:
    entry = manifest["patch_notes"]
    payload = download_bytes(str(entry["url"]), int(entry["size"]))
    validate_download(payload, entry, "パッチノート")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("パッチノートを読み取れません") from exc


def version_numbers(version: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise RuntimeError(f"バージョン表記が不正です: {version}")
    return tuple(int(value) for value in match.groups())


def update_state_path(root: Path) -> Path:
    return root / DATA_FILENAME / UPDATE_STATE_FILENAME


def read_update_state(root: Path) -> dict[str, Any] | None:
    path = update_state_path(root)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(state, dict) or state.get("format") != 1:
        return None
    if not isinstance(state.get("files"), dict):
        return None
    return state


def write_update_state(root: Path, manifest: dict[str, Any]) -> None:
    state = {
        "format": 1,
        "app_version": manifest.get(
            "app_version",
            manifest["installer"]["version"],
        ),
        "data_version": manifest["data_version"],
        "content_commit": manifest["content_commit"],
        "files": {
            str(entry["path"]): {
                "size": int(entry["size"]),
                "sha256": str(entry["sha256"]),
            }
            for entry in manifest["files"]
        },
    }
    path = update_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def changed_content_entries(
    root: Path,
    manifest: dict[str, Any],
    state: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[Path]]:
    entries = list(manifest["files"])
    remote_by_path = {str(entry["path"]): entry for entry in entries}
    changed: list[dict[str, Any]] = []
    previous_files = state.get("files", {}) if state else {}
    for entry in entries:
        relative = safe_update_relative(str(entry["path"]))
        previous = previous_files.get(relative.as_posix())
        destination = checked_update_destination(root, relative)
        if (
            state is not None
            and previous
            == {
                "size": int(entry["size"]),
                "sha256": str(entry["sha256"]),
            }
            and destination.is_file()
            and destination.stat().st_size == int(entry["size"])
        ):
            continue
        if (
            state is None
            and destination.is_file()
            and destination.stat().st_size == int(entry["size"])
            and sha256_file(destination) == str(entry["sha256"])
        ):
            continue
        changed.append(entry)

    stale: list[Path] = []
    if state is not None:
        for raw in previous_files:
            relative = safe_update_relative(raw)
            relative_text = relative.as_posix()
            if (
                allowed_content_update_path(relative)
                and relative_text not in remote_by_path
                and relative_text not in PRESERVED_LOCAL_CONTENT_PATHS
            ):
                stale.append(relative)
    return changed, stale


def apply_content_update(
    root: Path,
    manifest: dict[str, Any],
    changed: list[dict[str, Any]],
    stale: list[Path],
    *,
    write_state: bool = True,
) -> None:
    temp_root = root / f"{CONTENT_UPDATE_TEMP_PREFIX}{uuid.uuid4().hex}"
    download_root = temp_root / "download"
    backup_root = temp_root / "backup"
    download_root.mkdir(parents=True)
    backup_root.mkdir(parents=True)
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for entry in changed:
            relative = safe_update_relative(str(entry["path"]))
            payload = download_bytes(str(entry["url"]), int(entry["size"]))
            validate_download(payload, entry, relative.as_posix())
            destination = checked_update_destination(download_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)

        for relative in [
            *(safe_update_relative(str(entry["path"])) for entry in changed),
            *stale,
        ]:
            destination = checked_update_destination(root, relative)
            backup: Path | None = None
            if destination.is_file():
                backup = checked_update_destination(backup_root, relative)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
            replaced.append((destination, backup))
            generated = checked_update_destination(download_root, relative)
            if generated.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(generated, destination)

        for entry in changed:
            relative = safe_update_relative(str(entry["path"]))
            destination = checked_update_destination(root, relative)
            if (
                not destination.is_file()
                or destination.stat().st_size != int(entry["size"])
                or sha256_file(destination) != str(entry["sha256"])
            ):
                raise RuntimeError(f"更新後の照合に失敗しました: {relative}")
        if write_state:
            write_update_state(root, manifest)
        else:
            update_state_path(root).unlink(missing_ok=True)
    except Exception as exc:
        rollback_errors: list[str] = []
        for destination, backup in reversed(replaced):
            try:
                if destination.exists():
                    destination.unlink()
                if backup is not None and backup.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, destination)
            except Exception as rollback_exc:
                rollback_errors.append(f"{destination.name}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "更新に失敗し、元のファイルへ完全には戻せませんでした。"
                "\n" + "\n".join(rollback_errors)
            ) from exc
        raise RuntimeError(
            f"更新に失敗したため、変更したファイルを元に戻しました: {exc}"
        ) from exc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def perform_update_check(
    root: Path,
    apply_updates: bool = True,
    include_patch_notes: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    if find_game_dir(root, GAME_EXE_FILENAME) is None:
        raise RuntimeError(
            f"{GAME_EXE_FILENAME}が見つかりません。\n"
            "\n"
            "「Guildrun日本語化MOD」フォルダーを、\n"
            f"{GAME_EXE_FILENAME}があるフォルダーの中へ移動してから、\n"
            "もう一度起動してください。"
        )
    manifest = fetch_manifest()
    notes = fetch_patch_notes(manifest) if include_patch_notes else ""
    remote_package_version = str(manifest["installer"]["version"])
    installer_update_available = (
        version_numbers(remote_package_version) > version_numbers(PACKAGE_VERSION)
    )
    remote_app_version = str(
        manifest.get("app_version", remote_package_version)
    )
    app_version_update_available = (
        version_numbers(remote_app_version) > version_numbers(APP_VERSION)
    )
    remote_data_version = str(manifest["data_version"])
    state = read_update_state(root)
    changed, stale = changed_content_entries(root, manifest, state)
    if app_version_update_available:
        changed_paths = {str(entry["path"]) for entry in changed}
        changed.extend(
            entry
            for entry in manifest["files"]
            if str(entry["path"]) in AUTO_UPDATE_APP_PATHS
            and str(entry["path"]) not in changed_paths
        )
    application_content_update = any(
        str(entry["path"]) in AUTO_UPDATE_APP_PATHS for entry in changed
    ) or any(path.as_posix() in AUTO_UPDATE_APP_PATHS for path in stale)

    if not apply_updates:
        return {
            "status": (
                "update_available"
                if installer_update_available or changed or stale
                else "current"
            ),
            "data_version": remote_data_version,
            "app_version": remote_app_version,
            "installer_version": remote_package_version,
            "patch_notes": notes,
            "installer_update_available": installer_update_available,
            "application_update_available": application_content_update,
            "changed": [str(entry["path"]) for entry in changed],
            "removed": [path.as_posix() for path in stale],
        }

    applied_changed = changed
    applied_stale = stale
    if application_content_update:
        applied_changed = [
            entry
            for entry in changed
            if str(entry["path"]) in AUTO_UPDATE_APP_PATHS
        ]
        applied_stale = [
            path for path in stale if path.as_posix() in AUTO_UPDATE_APP_PATHS
        ]
        apply_content_update(
            root,
            manifest,
            applied_changed,
            applied_stale,
            write_state=False,
        )
        status = "application_updated"
    elif changed or stale:
        apply_content_update(root, manifest, changed, stale)
        status = "content_updated"
    else:
        if state is None or state.get("content_commit") != manifest["content_commit"]:
            write_update_state(root, manifest)
        status = "current"
    return {
        "status": status,
        "data_version": remote_data_version,
        "app_version": remote_app_version,
        "installer_version": remote_package_version,
        "patch_notes": notes,
        "installer_update_available": installer_update_available,
        "application_updated": application_content_update,
        "changed": [str(entry["path"]) for entry in applied_changed],
        "removed": [path.as_posix() for path in applied_stale],
    }


def steam_launch_options(launcher: Path | None = None) -> str:
    """現在の起動EXEを使うSteam起動オプションを生成する。

    起動EXEは署名済みpythonw.exeそのものなので、アプリ用の引数は
    ``-m guildrun_exe_patcher`` より後ろへ渡す。
    """
    launcher_path = (launcher or Path(sys.executable)).resolve()
    checker = subprocess.list2cmdline(
        [
            str(launcher_path),
            "-m",
            "guildrun_exe_patcher",
            "--check-only",
        ]
    )
    return f'cmd /d /s /c "start "" {checker} & %command%"'


def show_update_notification(result: dict[str, Any]) -> None:
    run_gui(initial_update_result=result)


def startup_update_action(result: dict[str, Any]) -> str:
    content_update = bool(result.get("changed") or result.get("removed"))
    installer_update = bool(result.get("installer_update_available"))
    if content_update and not installer_update:
        return "apply"
    return "download"


def check_only_main(root: Path | None = None) -> int:
    """ゲーム起動時用。更新がある場合だけ通知し、それ以外は黙って終了する。"""
    try:
        result = perform_update_check(
            (root or app_dir()).resolve(),
            apply_updates=False,
            include_patch_notes=False,
        )
    except Exception:
        # ゲーム起動のたびに通信エラー等を表示しない。
        # 手動の「更新を確認」では従来どおり詳細を表示する。
        return 0
    if result["status"] == "update_available":
        show_update_notification(result)
    return 0


def valid_game_dir(path: Path, game_exe: str) -> bool:
    return (path / game_exe).is_file() and (path / "Guildrun_Data").is_dir()


# 親フォルダー方式では、exeを含むMODフォルダーがゲームフォルダー直下に置かれる
# （exeはゲームフォルダーのちょうど1階層下）。広く探索せず、exeの場所（フラット配置の
# 保険）と直上の親だけを見る。
GAME_DIR_SEARCH_DEPTH = 1


def find_game_dir(start: Path, game_exe: str) -> Path | None:
    current = start.resolve()
    for _ in range(GAME_DIR_SEARCH_DEPTH + 1):
        if valid_game_dir(current, game_exe):
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def resolve_game_dir(explicit: Path | None, config: dict[str, Any]) -> Path:
    game_config = config["game"]
    game_exe = str(game_config["exe"])
    if explicit is not None:
        candidate = explicit.resolve()
        if valid_game_dir(candidate, game_exe):
            return candidate
    else:
        found = find_game_dir(app_dir(), game_exe)
        if found is not None:
            return found
    raise RuntimeError(
        f"{game_exe}が見つかりません。\n"
        "\n"
        "「Guildrun日本語化MOD」フォルダーを、\n"
        f"{game_exe}があるフォルダーの中へ移動してから、\n"
        "もう一度起動してください。"
    )


def local_patch_data_available(root: Path) -> bool:
    data_path = root / DATA_FILENAME
    config_path = data_path / "config.json"
    manifest_path = data_path / "manifest.json"
    if not config_path.is_file() or not manifest_path.is_file():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if config.get("format") != 1 or manifest.get("format") != 1:
            return False
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return False
        for raw in files:
            relative = Path(str(raw))
            if relative.is_absolute() or ".." in relative.parts:
                return False
            if not (data_path / relative).is_file():
                return False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    return True


def local_game_available(root: Path) -> bool:
    return find_game_dir(root, GAME_EXE_FILENAME) is not None


def read_local_data_version(root: Path) -> str | None:
    if not local_patch_data_available(root):
        return None
    try:
        config = json.loads(
            (root / DATA_FILENAME / "config.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    version = str(config.get("data_version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?", version):
        return None
    return version


def load_config(data_path: Path) -> dict[str, Any]:
    if not data_path.is_dir():
        raise RuntimeError(f"dataフォルダーが見つかりません: {data_path}")
    config_path = data_path / "config.json"
    if not config_path.is_file():
        raise RuntimeError(f"設定ファイルが見つかりません: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("format") != 1:
        raise RuntimeError("未対応のconfig形式です")
    for section in ("game", "paths", "localization", "fonts"):
        if section not in config:
            raise RuntimeError(f"configに必要な項目がありません: {section}")
    return config


def load_data(
    data_path: Path, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[int, dict[str, str]], dict[str, bytes]]:
    manifest = json.loads((data_path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise RuntimeError("未対応のdata形式です")
    translation_file = str(config["localization"]["translation_csv"])
    editable_files = set(manifest.get("editable_files", [translation_file]))
    payloads: dict[str, bytes] = {}
    for name, expected_hash in manifest.get("files", {}).items():
        payload = (data_path / Path(name)).read_bytes()
        payloads[name] = payload
        if name not in editable_files and sha256(payload) != expected_hash:
            raise RuntimeError(f"dataファイルの検証に失敗しました: {name}")

    master_bytes = payloads[translation_file]
    with io.StringIO(master_bytes.decode("utf-8-sig"), newline="") as handle:
        rows = list(csv.DictReader(handle))
    translations: dict[int, dict[str, str]] = {}
    for row in rows:
        entry_id = int(row["id"])
        if entry_id in translations:
            raise RuntimeError(f"翻訳data内でIDが重複しています: {entry_id}")
        translations[entry_id] = {
            "english": row.get("english", ""),
            "japanese": row.get("japanese", ""),
        }
    fonts = {
        name.removeprefix("fonts/"): payload
        for name, payload in payloads.items()
        if name.startswith("fonts/")
    }
    return manifest, translations, fonts


def root_file(env: Any) -> Any:
    files = list(env.files.values())
    if len(files) != 1:
        raise RuntimeError(f"Unityファイルを一意に特定できません: {len(files)}")
    return files[0]


def root_bundle(env: Any) -> Any:
    root = root_file(env)
    if root.__class__.__name__ != "BundleFile":
        raise RuntimeError(f"AssetBundleではありません: {root.__class__.__name__}")
    return root


def language_objects(env: Any) -> dict[int, tuple[Any, dict[str, Any]]]:
    result: dict[int, tuple[Any, dict[str, Any]]] = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            tree = obj.read_typetree()
        except Exception:
            continue
        if "m_TableData" not in tree or "m_SharedData" not in tree:
            continue
        shared_id = int(tree["m_SharedData"]["m_PathID"])
        if shared_id in result:
            raise RuntimeError(f"言語テーブル参照が重複しています: {shared_id}")
        result[shared_id] = (obj, tree)
    return result


def build_translation_bundle(
    english_path: Path,
    target_path: Path,
    output: Path,
    translations: dict[int, dict[str, str]],
    target_locale_code: str,
) -> dict[str, Any]:
    english_env = UnityPy.load(english_path.read_bytes())
    target_env = UnityPy.load(target_path.read_bytes())
    english_tables = language_objects(english_env)
    target_tables = language_objects(target_env)
    if set(english_tables) != set(target_tables):
        raise PatchCompatibilityError("英語と中国語のテーブル構成が一致しません")

    seen: set[int] = set()
    warnings: list[str] = []
    expected_output: dict[int, str] = {}
    written = 0
    for shared_id, (target_obj, target_tree) in target_tables.items():
        _, english_tree = english_tables[shared_id]
        table_data = copy.deepcopy(english_tree["m_TableData"])
        for entry in table_data:
            entry_id = int(entry["m_Id"])
            source = entry.get("m_Localized", "")
            expected = translations.get(entry_id)
            if expected is None:
                warnings.append(f"新規ID {entry_id}は英語のまま収録: {source}")
                expected_output[entry_id] = source
                seen.add(entry_id)
                written += 1
                continue
            if source and not expected["japanese"]:
                raise RuntimeError(f"日本語訳が空です: {entry_id}")
            entry["m_Localized"] = expected["japanese"]
            expected_output[entry_id] = expected["japanese"]
            seen.add(entry_id)
            written += 1
        target_tree["m_TableData"] = table_data
        target_tree["m_LocaleId"]["m_Code"] = target_locale_code
        target_obj.save_typetree(target_tree)

    for entry_id in sorted(set(translations) - seen):
        warnings.append(f"現在のゲームに存在しない翻訳ID: {entry_id}")

    output.write_bytes(root_bundle(target_env).save(packer="lz4"))
    verify_env = UnityPy.load(output.read_bytes())
    checked = 0
    for _, tree in language_objects(verify_env).values():
        if tree["m_LocaleId"]["m_Code"] != target_locale_code:
            raise RuntimeError("生成後の言語コードが一致しません")
        for entry in tree["m_TableData"]:
            entry_id = int(entry["m_Id"])
            if entry.get("m_Localized", "") != expected_output[entry_id]:
                raise RuntimeError(f"生成後の翻訳照合に失敗しました: {entry_id}")
            checked += 1
    if checked != written:
        raise RuntimeError(f"翻訳行数が一致しません: written={written} checked={checked}")
    return {
        "rows": written,
        "warnings": warnings,
        "sha256": sha256(output.read_bytes()),
    }


def build_resources(
    source: Path,
    output: Path,
    fonts: dict[str, bytes],
    font_config: dict[str, Any],
) -> dict[str, Any]:
    asset_pattern = str(font_config["asset_name_pattern"])
    weight_files = {str(k): str(v) for k, v in font_config["weight_files"].items()}
    expected_count = int(font_config["expected_count"])
    env = UnityPy.load(source.read_bytes())
    expected: dict[int, tuple[str, bytes]] = {}
    for obj in env.objects:
        if obj.type.name != "Font":
            continue
        tree = obj.read_typetree()
        name = tree.get("m_Name", "")
        match = re.fullmatch(asset_pattern, name)
        if not match:
            continue
        weight = match.group(1)
        if weight not in weight_files:
            raise PatchCompatibilityError(f"未対応のフォントウェイトです: {name}")
        font_name = weight_files[weight]
        if font_name not in fonts:
            raise RuntimeError(f"data内にフォントがありません: {font_name}")
        font_data = fonts[font_name]
        tree["m_FontData"] = font_data
        obj.save_typetree(tree)
        expected[int(obj.path_id)] = (name, font_data)
    if len(expected) != expected_count:
        raise PatchCompatibilityError(
            f"置換対象フォントが{expected_count}個ではありません: {len(expected)}個"
        )

    output.write_bytes(root_file(env).save())
    verify_env = UnityPy.load(output.read_bytes())
    checked = 0
    for obj in verify_env.objects:
        path_id = int(obj.path_id)
        if path_id not in expected:
            continue
        actual = bytes(obj.read_typetree().get("m_FontData", b""))
        if actual != expected[path_id][1]:
            raise RuntimeError(f"フォント照合に失敗しました: {expected[path_id][0]}")
        checked += 1
    if checked != expected_count:
        raise RuntimeError(f"生成後のフォント数が一致しません: {checked}")
    return {"fonts": checked, "sha256": sha256(output.read_bytes())}


def build_metadata(
    source: Path, output: Path, old_locale_name: str, new_locale_name: str
) -> dict[str, Any]:
    data = bytearray(source.read_bytes())
    old = old_locale_name.encode("utf-8")
    new = new_locale_name.encode("utf-8")
    if len(old) != len(new):
        raise RuntimeError("言語名のバイト長が一致しません")
    old_count = data.count(old)
    new_count = data.count(new)
    if old_count == 1 and new_count == 0:
        offset = data.find(old)
        data[offset : offset + len(old)] = new
        state = "patched"
    elif old_count == 0 and new_count == 1:
        offset = data.find(new)
        state = "already_patched"
    else:
        raise PatchCompatibilityError(
            f"言語名を一意に特定できません: 中文={old_count}, 日本語={new_count}"
        )
    output.write_bytes(data)
    return {"state": state, "offset": offset, "sha256": sha256(bytes(data))}


def unity_bundle_crc(path: Path) -> int:
    env = UnityPy.load(path.read_bytes())
    bundle = root_bundle(env)
    reader = EndianBinaryReader(path.read_bytes())
    reader.read_string_to_null()
    reader.read_u_int()
    reader.read_string_to_null()
    reader.read_string_to_null()
    reader.read_long()
    compressed_size = reader.read_u_int()
    uncompressed_size = reader.read_u_int()
    reader.read_u_int()
    if bundle._uses_block_alignment:
        reader.align_stream(16)
    start = reader.Position
    if int(bundle.dataflags) & int(ArchiveFlags.BlocksInfoAtTheEnd):
        reader.Position = reader.Length - compressed_size
        compressed_info = reader.read_bytes(compressed_size)
        reader.Position = start
        data_start = start
    else:
        compressed_info = reader.read_bytes(compressed_size)
        if int(bundle.dataflags) & 0x200:
            reader.align_stream(16)
        data_start = reader.Position

    info_data = bundle.decompress_data(
        compressed_info, uncompressed_size, bundle.dataflags
    )
    info_reader = EndianBinaryReader(info_data)
    info_reader.read_bytes(16)
    block_count = info_reader.read_int()
    blocks = [
        (
            info_reader.read_u_int(),
            info_reader.read_u_int(),
            info_reader.read_u_short(),
        )
        for _ in range(block_count)
    ]
    reader.Position = data_start
    crc = 0
    for index, (raw_size, packed_size, flags) in enumerate(blocks):
        packed = reader.read_bytes(packed_size)
        raw = bundle.decompress_data(packed, raw_size, flags, index)
        crc = zlib.crc32(raw, crc)
    return crc & 0xFFFFFFFF


def build_catalog(
    source: Path,
    current_bundle: Path,
    new_bundle: Path,
    output: Path,
    target_bundle_name: str,
    crc_search_bytes: int,
) -> dict[str, Any]:
    data = bytearray(source.read_bytes())
    name = target_bundle_name.encode("ascii")
    if data.count(name) != 1:
        raise PatchCompatibilityError(
            f"カタログ内の対象バンドル名が一意ではありません: {data.count(name)}"
        )
    old_crc = unity_bundle_crc(current_bundle)
    new_crc = unity_bundle_crc(new_bundle)
    name_offset = data.find(name)
    search_start = name_offset + len(name)
    search_end = min(len(data), search_start + crc_search_bytes)
    old_bytes = struct.pack("<I", old_crc)
    offsets: list[int] = []
    position = data.find(old_bytes, search_start, search_end)
    while position >= 0:
        offsets.append(position)
        position = data.find(old_bytes, position + 1, search_end)
    if len(offsets) != 1:
        raise PatchCompatibilityError(
            f"カタログ内の現在CRCを一意に特定できません: {old_crc:08x}, {len(offsets)}件"
        )
    crc_offset = offsets[0]
    data[crc_offset : crc_offset + 4] = struct.pack("<I", new_crc)
    output.write_bytes(data)
    if output.read_bytes()[crc_offset : crc_offset + 4] != struct.pack("<I", new_crc):
        raise RuntimeError("カタログCRCの照合に失敗しました")
    return {
        "old_crc": f"{old_crc:08x}",
        "new_crc": f"{new_crc:08x}",
        "offset": crc_offset,
        "sha256": sha256(bytes(data)),
    }


def check_expected_sizes(game_dir: Path, config: dict[str, Any]) -> list[str]:
    expected = config.get("expected_sizes", {})
    warnings: list[str] = []
    for relative_path, expected_size in expected.items():
        path = game_dir / Path(relative_path)
        if not path.is_file():
            warnings.append(f"確認対象ファイル欠落: {path.name}")
            continue
        current_size = path.stat().st_size
        if current_size != int(expected_size):
            warnings.append(
                f"英語原文サイズ差異: {path.name} "
                f"(想定 {int(expected_size):,} / 現在 {current_size:,} bytes)"
            )
    for warning in warnings:
        print(f"警告: {warning}")
    return warnings


def modified_paths(config: dict[str, Any]) -> list[Path]:
    paths = {key: Path(value) for key, value in config["paths"].items()}
    return [
        paths["resources"],
        paths["metadata"],
        paths["target_bundle"],
        paths["catalog"],
    ]


def game_is_running(game_exe: str) -> bool:
    if os.name != "nt":
        return False
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {game_exe}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=creation_flags,
        )
    except OSError:
        return False
    return game_exe.casefold() in result.stdout.casefold()


def stop_game_for_update(game_exe: str, timeout_seconds: float = 10.0) -> bool:
    """起動中のゲームを強制終了し、終了するまで待つ。"""
    if os.name != "nt" or not game_is_running(game_exe):
        return False
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", game_exe],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise RuntimeError(
            f"{game_exe}へ終了を要求できませんでした。"
            "\nゲームを手動で終了してから、もう一度実行してください。"
        ) from exc

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not game_is_running(game_exe):
            return True
        time.sleep(0.25)
    raise RuntimeError(
        f"{game_exe}を自動で終了できませんでした。"
        "\nゲームを手動で終了してから、もう一度実行してください。"
    )


def checked_destination(game_dir: Path, relative: Path) -> Path:
    destination = (game_dir / relative).resolve()
    try:
        destination.relative_to(game_dir.resolve())
    except ValueError as exc:
        raise RuntimeError(f"ゲームフォルダー外のパスは使用できません: {relative}") from exc
    return destination


def write_apply_state(temp_root: Path, state: dict[str, Any]) -> None:
    state_path = temp_root / APPLY_STATE_FILENAME
    temporary = temp_root / f"{APPLY_STATE_FILENAME}.tmp"
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, state_path)


def recover_interrupted_apply(
    game_dir: Path,
    allowed_paths: list[Path],
    game_exe: str,
) -> None:
    recovery_dirs = sorted(
        path
        for path in game_dir.glob(f"{APPLY_TEMP_PREFIX}*")
        if path.is_dir() and path.parent.resolve() == game_dir.resolve()
    )
    if not recovery_dirs:
        return
    if game_is_running(game_exe):
        raise RuntimeError(
            f"{game_exe}が起動中のため、前回中断された日本語化を復旧できません。"
            "\nゲームを終了してから、もう一度実行してください。"
        )

    allowed = {path.as_posix() for path in allowed_paths}
    for recovery_dir in recovery_dirs:
        state_path = recovery_dir / APPLY_STATE_FILENAME
        if not state_path.is_file():
            shutil.rmtree(recovery_dir)
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ApplyRollbackError(
                f"前回の復旧情報を読み取れません: {recovery_dir}\n{exc}"
            ) from exc
        if state.get("state") == "complete":
            shutil.rmtree(recovery_dir)
            continue

        entries = state.get("entries", [])
        restored: list[str] = []
        try:
            for entry in entries:
                relative_text = str(entry["relative"])
                if relative_text not in allowed:
                    raise RuntimeError(f"復旧対象外のパスが記録されています: {relative_text}")
                relative = Path(relative_text)
                backup = (recovery_dir / "backup" / relative).resolve()
                try:
                    backup.relative_to((recovery_dir / "backup").resolve())
                except ValueError as exc:
                    raise RuntimeError(
                        f"不正な復旧パスが記録されています: {relative_text}"
                    ) from exc
                destination = checked_destination(game_dir, relative)
                if not backup.is_file():
                    original_hash = str(entry.get("original_sha256", ""))
                    if (
                        original_hash
                        and destination.is_file()
                        and sha256(destination.read_bytes()) == original_hash
                    ):
                        restored.append(relative_text)
                        continue
                    raise RuntimeError(f"復旧用ファイルがありません: {relative_text}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
                restored.append(relative_text)
        except Exception as exc:
            raise ApplyRollbackError(
                "前回中断された日本語化を自動復旧できませんでした。"
                f"\n復旧フォルダー: {recovery_dir}"
                f"\n復旧済み: {', '.join(restored) if restored else 'なし'}"
                f"\n原因: {exc}"
            ) from exc
        shutil.rmtree(recovery_dir)
        print("前回中断された日本語化を元の状態へ復旧しました。")


def confirm_warning_apply(size_warnings: list[str], non_interactive: bool) -> None:
    if not size_warnings:
        return
    if non_interactive:
        raise PatchCompatibilityError(
            "英語原文のサイズに差異があるため、直接適用を中止しました。"
            "\n内容を確認して適用する場合は --allow-size-warnings を指定してください。"
        )
    print(
        "\n英語原文のサイズに差異があります。"
        "\n生成後の検証には成功しましたが、未確認バージョンの可能性があります。"
    )
    answer = input("このままゲームファイルへ適用しますか？ [y/N]: ").strip().casefold()
    if answer not in {"y", "yes"}:
        raise PatchCompatibilityError("ユーザー操作により直接適用を中止しました。")


def apply_outputs_transactionally(
    game_dir: Path,
    outputs: dict[Path, Path],
    temp_root: Path,
    game_exe: str,
) -> dict[str, Any]:
    if game_is_running(game_exe):
        raise RuntimeError(
            f"{game_exe}が起動中です。ゲームを終了してから、もう一度実行してください。"
        )

    backup_root = temp_root / "backup"
    backup_root.mkdir()
    entries: list[dict[str, str]] = []
    for relative, generated_file in outputs.items():
        destination = checked_destination(game_dir, relative)
        if not destination.is_file():
            raise RuntimeError(f"上書き対象のゲームファイルがありません: {relative}")
        backup = backup_root / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, backup)
        entries.append(
            {
                "relative": relative.as_posix(),
                "original_sha256": sha256(backup.read_bytes()),
                "new_sha256": sha256(generated_file.read_bytes()),
            }
        )

    state: dict[str, Any] = {
        "format": 1,
        "state": "prepared",
        "entries": entries,
        "applied": [],
    }
    write_apply_state(temp_root, state)

    try:
        for relative, generated_file in outputs.items():
            destination = checked_destination(game_dir, relative)
            os.replace(generated_file, destination)
            state["applied"].append(relative.as_posix())
            state["state"] = "applying"
            write_apply_state(temp_root, state)

        for entry in entries:
            destination = checked_destination(game_dir, Path(entry["relative"]))
            actual = sha256(destination.read_bytes())
            if actual != entry["new_sha256"]:
                raise RuntimeError(f"適用後の照合に失敗しました: {entry['relative']}")

        state["state"] = "complete"
        write_apply_state(temp_root, state)
        return {
            "applied_files": [entry["relative"] for entry in entries],
            "verified": len(entries),
        }
    except Exception as exc:
        rollback_errors: list[str] = []
        for entry in reversed(entries):
            relative = Path(entry["relative"])
            backup = backup_root / relative
            destination = checked_destination(game_dir, relative)
            try:
                if not backup.is_file():
                    raise RuntimeError("復旧用ファイルがありません")
                os.replace(backup, destination)
            except Exception as rollback_exc:
                rollback_errors.append(f"{entry['relative']}: {rollback_exc}")
        if rollback_errors:
            raise ApplyRollbackError(
                "適用中にエラーが発生し、自動復旧も完了できませんでした。"
                f"\n復旧フォルダー: {temp_root}"
                f"\n適用エラー: {exc}"
                "\n復旧エラー:\n" + "\n".join(rollback_errors)
            ) from exc
        raise RuntimeError(
            f"適用中にエラーが発生したため、ゲームファイルを元に戻しました: {exc}"
        ) from exc


def run_patch(
    game_dir: Path,
    data_path: Path,
    output_root: Path | None,
    config: dict[str, Any],
    direct_apply: bool,
    allow_size_warnings: bool,
    non_interactive: bool,
) -> dict[str, Any]:
    paths = {key: Path(value) for key, value in config["paths"].items()}
    localization = config["localization"]
    required = [
        paths["resources"],
        paths["metadata"],
        paths["catalog"],
        paths["english_bundle"],
        paths["target_bundle"],
    ]
    missing = [str(path) for path in required if not (game_dir / path).is_file()]
    if missing:
        raise RuntimeError("必要なゲームファイルがありません:\n" + "\n".join(missing))
    manifest, translations, fonts = load_data(data_path, config)
    size_warnings = check_expected_sizes(game_dir, config)
    if direct_apply:
        temp_root = Path(tempfile.mkdtemp(prefix=APPLY_TEMP_PREFIX, dir=game_dir))
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="guildrun_jp_"))
    generated = temp_root / "generated"
    generated.mkdir()
    preserve_temp = False
    try:
        resources_out = generated / "resources.assets"
        metadata_out = generated / "global-metadata.dat"
        bundle_out = generated / paths["target_bundle"].name
        catalog_out = generated / "catalog.bin"

        print("[1/4] 文章データを確認・生成しています...")
        translation_report = build_translation_bundle(
            game_dir / paths["english_bundle"],
            game_dir / paths["target_bundle"],
            bundle_out,
            translations,
            str(localization["target_locale_code"]),
        )
        print("[2/4] Noto Sans JPを現在のresources.assetsへ適用しています...")
        font_report = build_resources(
            game_dir / paths["resources"], resources_out, fonts, config["fonts"]
        )
        print(f"[3/4] 言語名を{localization['new_locale_name']}へ変更しています...")
        metadata_report = build_metadata(
            game_dir / paths["metadata"],
            metadata_out,
            str(localization["old_locale_name"]),
            str(localization["new_locale_name"]),
        )
        print("[4/4] 現在のAddressablesカタログを更新しています...")
        catalog_report = build_catalog(
            game_dir / paths["catalog"],
            game_dir / paths["target_bundle"],
            bundle_out,
            catalog_out,
            paths["target_bundle"].name,
            int(config["catalog"]["crc_search_bytes"]),
        )

        outputs = {
            paths["resources"]: resources_out,
            paths["metadata"]: metadata_out,
            paths["target_bundle"]: bundle_out,
            paths["catalog"]: catalog_out,
        }
        apply_report: dict[str, Any] | None = None
        if direct_apply:
            if size_warnings and not allow_size_warnings:
                confirm_warning_apply(size_warnings, non_interactive)
            print("生成したファイルをゲームへ適用しています...")
            apply_report = apply_outputs_transactionally(
                game_dir,
                outputs,
                temp_root,
                str(config["game"]["exe"]),
            )
        else:
            if output_root is None:
                raise RuntimeError("生成先が指定されていません")
            for relative, generated_file in outputs.items():
                destination = output_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(generated_file, destination)

        return {
            "app_version": APP_VERSION,
            "package_version": PACKAGE_VERSION,
            "data_version": config.get("data_version", ""),
            "game_dir": str(game_dir),
            "mode": "applied" if direct_apply else "generated",
            "output_dir": None if direct_apply else str(output_root),
            "size_warnings": size_warnings,
            "translation": translation_report,
            "fonts": font_report,
            "metadata": metadata_report,
            "catalog": catalog_report,
            "apply": apply_report,
        }
    except ApplyRollbackError:
        preserve_temp = True
        raise
    finally:
        if not preserve_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


def write_message_file(message: str) -> Path | None:
    candidates = [app_dir() / RESULT_FILENAME, Path.cwd() / RESULT_FILENAME]
    for path in candidates:
        try:
            path.write_text(message.rstrip() + "\n", encoding="utf-8-sig")
            return path
        except OSError:
            continue
    return None


def pause_if_needed(non_interactive: bool) -> None:
    if non_interactive:
        return
    try:
        input("\nEnterキーで閉じます。")
    except EOFError:
        pass


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guildrun Demo 日本語化パッチャー")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--game-dir", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-size-warnings", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args(argv)

    if args.check_only:
        return check_only_main()

    print(f"Guildrun Demo 日本語化アプリ {APP_VERSION}")
    try:
        data_path = (args.data or (app_dir() / DATA_FILENAME)).resolve()
        config = load_config(data_path)
        game_dir = resolve_game_dir(args.game_dir, config)
        recover_interrupted_apply(
            game_dir,
            modified_paths(config),
            str(config["game"]["exe"]),
        )
        direct_apply = args.output is None
        output_root = args.output.resolve() if args.output else None
        print(f"ゲーム: {game_dir}")
        print(f"データ: {data_path}")
        if direct_apply:
            print("動作: 生成・検証後にゲームへ直接適用")
        else:
            print(f"動作: ファイル生成のみ\n出力先: {output_root}")
        report = run_patch(
            game_dir,
            data_path,
            output_root,
            config,
            direct_apply,
            args.allow_size_warnings,
            args.non_interactive,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if direct_apply:
            print(
                "\n日本語化が完了しました。"
                "\nゲーム内で「日本語（MOD）」を選択してください。"
            )
        else:
            print(f"\n生成が完了しました。出力先: {output_root}")
        result = 0
    except PatchCompatibilityError as exc:
        message = f"ゲーム更新を検出しました。\n\n{exc}"
        path = write_message_file(message)
        print("\n" + message)
        if path:
            print(f"\n確認結果: {path}")
        result = 2
    except ApplyRollbackError as exc:
        detail = traceback.format_exc()
        message = (
            "日本語化の適用中に問題が発生し、自動復旧も完了できませんでした。"
            "\nSteamでゲームファイルの整合性を確認してください。"
            f"\n\n{exc}\n\n{detail}"
        )
        path = write_message_file(message)
        print("\n" + message)
        if path:
            print(f"\nエラー詳細: {path}")
        result = 3
    except Exception as exc:
        detail = traceback.format_exc()
        message = f"日本語化に失敗しました。ゲームファイルは変更されていません。\n\n{exc}\n\n{detail}"
        path = write_message_file(message)
        print("\n" + message)
        if path:
            print(f"\nエラー詳細: {path}")
        result = 1
    pause_if_needed(args.non_interactive)
    return result


def run_gui(initial_update_result: dict[str, Any] | None = None) -> int:
    if os.name != "nt":
        raise RuntimeError("GUI版はWindows専用です")

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    comctl32 = ctypes.windll.comctl32
    shell32 = ctypes.windll.shell32

    LRESULT = ctypes.c_ssize_t
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        WPARAM,
        LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class INITCOMMONCONTROLSEX(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("dwICC", wintypes.DWORD),
        ]

    class NMHDR(ctypes.Structure):
        _fields_ = [
            ("hwndFrom", wintypes.HWND),
            ("idFrom", ctypes.c_size_t),
            ("code", wintypes.UINT),
        ]

    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    gdi32.GetStockObject.restype = wintypes.HANDLE
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.GetSysColorBrush.restype = wintypes.HBRUSH
    user32.GetSysColorBrush.argtypes = [ctypes.c_int]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.SetWindowTextW.restype = wintypes.BOOL
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.EnableWindow.restype = wintypes.BOOL
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.SetFocus.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    ]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        WPARAM,
        LPARAM,
    ]
    user32.SendMessageW.restype = LRESULT
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        WPARAM,
        LPARAM,
    ]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        WPARAM,
        LPARAM,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    comctl32.InitCommonControlsEx.restype = wintypes.BOOL
    comctl32.InitCommonControlsEx.argtypes = [
        ctypes.POINTER(INITCOMMONCONTROLSEX),
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]

    WM_CREATE = 0x0001
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_NOTIFY = 0x004E
    WM_COMMAND = 0x0111
    WM_SETFONT = 0x0030
    WM_APP_RESULT = 0x8001
    WS_OVERLAPPED = 0x00000000
    WS_CAPTION = 0x00C00000
    WS_SYSMENU = 0x00080000
    WS_MINIMIZEBOX = 0x00020000
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_TABSTOP = 0x00010000
    WS_VSCROLL = 0x00200000
    WS_EX_CLIENTEDGE = 0x00000200
    WS_EX_DLGMODALFRAME = 0x00000001
    ES_LEFT = 0x0000
    ES_MULTILINE = 0x0004
    ES_AUTOVSCROLL = 0x0040
    ES_READONLY = 0x0800
    BS_PUSHBUTTON = 0x00000000
    COLOR_BTNFACE = 15
    IDC_ARROW = 32512
    SW_SHOW = 5
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040
    MB_OK = 0x00000000
    MB_YESNO = 0x00000004
    MB_ICONERROR = 0x00000010
    MB_ICONQUESTION = 0x00000020
    MB_ICONWARNING = 0x00000030
    MB_ICONINFORMATION = 0x00000040
    IDYES = 6
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    DEFAULT_GUI_FONT = 17
    ICC_LINK_CLASS = 0x00008000
    NM_CLICK = ctypes.c_uint(-2).value
    NM_RETURN = ctypes.c_uint(-4).value
    SW_SHOWNORMAL = 1
    BUTTON_UPDATE_ACTION = 1001
    BUTTON_EXISTING_PATCH = 1002
    LINK_REPOSITORY = 1003
    BUTTON_COPY_STEAM_OPTIONS = 1004

    results: queue.Queue[tuple[str, Any]] = queue.Queue()
    controls: dict[str, int] = {}
    initial_data_available = local_patch_data_available(app_dir().resolve())
    startup_action = (
        startup_update_action(initial_update_result)
        if initial_update_result is not None
        else None
    )
    startup_direct_update = startup_action == "apply"
    state = {
        "busy": False,
        "local_game_available": local_game_available(app_dir().resolve()),
        "local_data_available": initial_data_available,
        "local_data_version": read_local_data_version(app_dir().resolve()),
        "remote_app_version": None,
        "remote_package_version": None,
        "remote_data_version": None,
        "update_available": not initial_data_available,
        "startup_update_mode": startup_direct_update,
        "startup_installer_mode": startup_action == "download",
    }
    hinstance = kernel32.GetModuleHandleW(None)
    font = gdi32.GetStockObject(DEFAULT_GUI_FONT)
    common_controls = INITCOMMONCONTROLSEX(
        ctypes.sizeof(INITCOMMONCONTROLSEX),
        ICC_LINK_CLASS,
    )
    if not comctl32.InitCommonControlsEx(ctypes.byref(common_controls)):
        raise RuntimeError("リンク表示を初期化できませんでした")

    def set_text(control: int, text: str) -> None:
        user32.SetWindowTextW(control, text.replace("\n", "\r\n"))

    def show_message(title: str, message: str, flags: int) -> int:
        return int(user32.MessageBoxW(controls.get("window", 0), message, title, flags))

    def copy_text_to_clipboard(text: str) -> None:
        buffer = ctypes.create_unicode_buffer(text)
        byte_count = ctypes.sizeof(buffer)
        memory = kernel32.GlobalAlloc(GMEM_MOVEABLE, byte_count)
        if not memory:
            raise RuntimeError("クリップボード用のメモリを確保できませんでした")
        transferred = False
        try:
            locked = kernel32.GlobalLock(memory)
            if not locked:
                raise RuntimeError("クリップボード用のメモリを開けませんでした")
            try:
                ctypes.memmove(locked, ctypes.addressof(buffer), byte_count)
            finally:
                kernel32.GlobalUnlock(memory)
            if not user32.OpenClipboard(controls.get("window", 0)):
                raise RuntimeError("クリップボードを開けませんでした")
            try:
                if not user32.EmptyClipboard():
                    raise RuntimeError("クリップボードを空にできませんでした")
                if not user32.SetClipboardData(CF_UNICODETEXT, memory):
                    raise RuntimeError("クリップボードへコピーできませんでした")
                transferred = True
            finally:
                user32.CloseClipboard()
        finally:
            if not transferred:
                kernel32.GlobalFree(memory)

    def configure_steam_update_check() -> None:
        if not local_game_available(app_dir().resolve()):
            show_message(
                "設定できません",
                "「Guildrun日本語化MOD」フォルダーを、\n"
                "Guildrun.exeがあるフォルダーの中へ移動してから、\n"
                "もう一度起動してください。",
                MB_OK | MB_ICONERROR,
            )
            return
        answer = show_message(
            "起動時の更新確認",
            "Guildrun DemoをSteamから起動した時に、\n"
            "翻訳データを含むこのMODの更新があるか確認します。\n\n"
            "更新がなければ何も表示しません。\n\n"
            "この機能を設定しますか？",
            MB_YESNO,
        )
        if answer != IDYES:
            return

        options = steam_launch_options()
        copy_text_to_clipboard(options)
        show_message(
            "設定 1/2",
            "Steamに貼り付ける内容をコピーしました。\n\n"
            "1. Steamのライブラリで「Guildrun Demo」を右クリック\n"
            "2. 「プロパティ」を開く\n\n"
            "開いたら「OK」を押してください。",
            MB_OK,
        )
        show_message(
            "設定 2/2",
            "「一般」にある「起動オプション」へ、\n"
            "コピーした内容を貼り付けてください。\n\n"
            "貼り付けたら設定は完了です。",
            MB_OK,
        )

    def open_repository() -> None:
        result = shell32.ShellExecuteW(
            controls.get("window", 0),
            "open",
            REPOSITORY_URL,
            None,
            None,
            SW_SHOWNORMAL,
        )
        if int(result or 0) <= 32:
            show_message(
                "ページを開けません",
                f"ブラウザーで次のURLを開いてください。\n{REPOSITORY_URL}",
                MB_OK | MB_ICONERROR,
            )

    def set_notes(text: str) -> None:
        set_text(controls["notes"], text.rstrip() + "\n")

    def set_busy(value: bool, status: str) -> None:
        state["busy"] = value
        enabled = not value
        user32.EnableWindow(
            controls["update_action_button"],
            enabled and state["local_game_available"],
        )
        user32.EnableWindow(
            controls["existing_patch_button"],
            enabled
            and state["local_game_available"]
            and state["local_data_available"],
        )
        user32.EnableWindow(
            controls["steam_options_button"],
            enabled and state["local_game_available"],
        )
        set_text(controls["status"], status)

    def set_update_available(value: bool) -> None:
        state["update_available"] = value
        if state["startup_installer_mode"]:
            label = "最新版のダウンロードページを開く"
        elif value and state["startup_update_mode"]:
            label = "今すぐ更新する（ゲーム終了、ダウンロードして適用）"
        elif not state["local_data_available"]:
            label = "翻訳データを取得して日本語化"
        elif value:
            label = "最新版を適用して日本語化"
        else:
            label = "更新を確認"
        set_text(
            controls["update_action_button"],
            label,
        )

    def version_display_text() -> str:
        local_data_version = state["local_data_version"]
        app_line = f"アプリ: v{APP_VERSION}"
        data_line = (
            f"翻訳データ（CSV）: v{local_data_version}"
            if local_data_version
            else "翻訳データ（CSV）: なし"
        )
        remote_app_version = state["remote_app_version"]
        remote_data_version = state["remote_data_version"]
        if remote_app_version and remote_app_version != APP_VERSION:
            app_line += f"　最新版: v{remote_app_version}"
        if remote_data_version and remote_data_version != local_data_version:
            data_line += f"　最新版: v{remote_data_version}"
        return f"{app_line}\n{data_line}"

    def refresh_version_display() -> None:
        if "versions" in controls:
            set_text(controls["versions"], version_display_text())

    def set_remote_versions(value: dict[str, Any]) -> None:
        state["remote_app_version"] = str(value["app_version"])
        state["remote_package_version"] = str(value["installer_version"])
        state["remote_data_version"] = str(value["data_version"])
        refresh_version_display()

    def refresh_local_data_state() -> bool:
        game_available = local_game_available(app_dir().resolve())
        available = local_patch_data_available(app_dir().resolve())
        state["local_game_available"] = game_available
        state["local_data_available"] = available
        state["local_data_version"] = read_local_data_version(app_dir().resolve())
        if not available:
            state["update_available"] = True
        if "update_action_button" in controls:
            user32.EnableWindow(
                controls["update_action_button"],
                game_available and not state["busy"],
            )
            set_update_available(state["update_available"])
        if "existing_patch_button" in controls:
            user32.EnableWindow(
                controls["existing_patch_button"],
                game_available and available and not state["busy"],
            )
        if "steam_options_button" in controls:
            user32.EnableWindow(
                controls["steam_options_button"],
                game_available and not state["busy"],
            )
        refresh_version_display()
        return available

    def background(kind: str, operation: Any) -> None:
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                value = operation()
            results.put((kind, {"value": value, "log": buffer.getvalue()}))
        except Exception as exc:
            results.put(
                (
                    "error",
                    {
                        "kind": kind,
                        "error": exc,
                        "detail": traceback.format_exc(),
                    },
                )
            )
        user32.PostMessageW(controls["window"], WM_APP_RESULT, 0, 0)

    def begin(kind: str, status: str, operation: Any) -> None:
        set_busy(True, status)
        threading.Thread(
            target=background,
            args=(kind, operation),
            daemon=True,
        ).start()

    def patch_operation(allow_size_warnings: bool) -> dict[str, Any]:
        data_path = (app_dir() / DATA_FILENAME).resolve()
        config = load_config(data_path)
        game_dir = resolve_game_dir(None, config)
        recover_interrupted_apply(
            game_dir,
            modified_paths(config),
            str(config["game"]["exe"]),
        )
        return run_patch(
            game_dir,
            data_path,
            None,
            config,
            True,
            allow_size_warnings,
            True,
        )

    def start_patch() -> None:
        try:
            data_path = (app_dir() / DATA_FILENAME).resolve()
            config = load_config(data_path)
            game_dir = resolve_game_dir(None, config)
            warnings = check_expected_sizes(game_dir, config)
        except Exception as exc:
            show_message("日本語化できません", str(exc), MB_OK | MB_ICONERROR)
            return
        if warnings:
            detail = "\n".join(f"・{warning}" for warning in warnings)
            answer = show_message(
                "ゲーム更新を検出しました",
                "英語原文のサイズに差異があります。\n"
                "未確認バージョンの可能性があります。\n\n"
                f"{detail}\n\n"
                "このまま生成・検証して日本語化しますか？",
                MB_YESNO | MB_ICONWARNING,
            )
            if answer != IDYES:
                set_text(controls["status"], "日本語化を中止しました。")
                return
        begin(
            "patch",
            "日本語化データを生成・検証し、ゲームへ適用しています…",
            lambda: patch_operation(bool(warnings)),
        )

    def start_latest_patch(*, close_game: bool = False) -> None:
        def operation() -> dict[str, Any]:
            if close_game:
                stop_game_for_update(GAME_EXE_FILENAME)
            return perform_update_check(app_dir().resolve(), apply_updates=True)

        begin(
            "latest_patch",
            (
                "ゲームを終了し、最新版を取得しています…"
                if close_game
                else "最新版を確認・取得しています…"
            ),
            operation,
        )

    def start_update_check() -> None:
        begin(
            "check_update",
            "GitHubへ接続して更新を確認しています…",
            lambda: perform_update_check(app_dir().resolve(), apply_updates=False),
        )

    def handle_patch(payload: dict[str, Any]) -> None:
        value = payload["value"]
        warning_count = len(value.get("size_warnings", []))
        set_busy(False, "日本語化が完了しました。")
        text = (
            "日本語化が完了しました。\n"
            "ゲーム内で「日本語（MOD）」を選択してください。"
        )
        if warning_count:
            text += f"\n\nサイズ差異の警告: {warning_count}件"
        set_notes(text)
        show_message("完了", text, MB_OK | MB_ICONINFORMATION)

    def handle_applied_update(
        payload: dict[str, Any],
        *,
        continue_to_patch: bool,
    ) -> None:
        value = payload["value"]
        set_remote_versions(value)
        set_notes(str(value["patch_notes"]))
        update_status = value["status"]
        if value.get("installer_update_available"):
            show_message(
                "配布パッケージの新版があります",
                "自動更新できないファイルが更新されています。\n"
                "GitHubから新しいZIPをダウンロードしてください。\n"
                f"{REPOSITORY_URL}",
                MB_OK | MB_ICONINFORMATION,
            )
        refresh_local_data_state()
        if continue_to_patch:
            if value.get("application_updated"):
                set_update_available(False)
                set_busy(
                    False,
                    "アプリを更新しました。再起動後に更新を確認してください。",
                )
                show_message(
                    "再起動が必要です",
                    "日本語化アプリを更新しました。\n"
                    "いったんこの画面を閉じ、もう一度起動してから\n"
                    "「更新を確認」を押してください。",
                    MB_OK | MB_ICONINFORMATION,
                )
                return
            if not state["local_data_available"]:
                set_busy(False, "最新版の取得後も必要なデータが見つかりません。")
                show_message(
                    "日本語化できません",
                    "最新版を取得しましたが、必要なデータがそろっていません。",
                    MB_OK | MB_ICONERROR,
                )
                return
            set_update_available(False)
            set_busy(False, "最新版を取得しました。日本語化を開始します。")
            start_patch()
            return
        if update_status == "content_updated":
            changed = len(value["changed"])
            removed = len(value["removed"])
            detail = f"更新しました（更新 {changed}件"
            if removed:
                detail += f" / 削除 {removed}件"
            detail += "）。"
            set_busy(False, detail)
        else:
            set_busy(False, "現在のファイルは最新版です。")

    def handle_update_check(payload: dict[str, Any]) -> None:
        value = payload["value"]
        set_remote_versions(value)
        set_notes(str(value["patch_notes"]))
        set_busy(False, "")
        if value["status"] == "update_available":
            content_update = bool(value.get("changed") or value.get("removed"))
            installer_update = bool(value.get("installer_update_available"))
            application_update = bool(value.get("application_update_available"))
            set_update_available(content_update)
            lines: list[str] = []
            if content_update:
                lines.append(
                    "日本語化ファイルの更新があります。\n"
                    "「最新版を適用して日本語化」を押してください。"
                )
                if application_update:
                    lines.append(
                        "アプリの更新を含むため、適用後に一度再起動が必要です。"
                    )
            if installer_update:
                lines.append(
                    "配布パッケージの新版があります。\n"
                    f"GitHubから新しいZIPをダウンロードしてください。\n{REPOSITORY_URL}"
                )
            set_text(controls["status"], "更新があります。")
            show_message(
                "更新があります",
                "\n\n".join(lines),
                MB_OK | MB_ICONINFORMATION,
            )
        else:
            set_update_available(False)
            set_text(controls["status"], "現在のファイルは最新版です。")
            show_message(
                "更新確認",
                "現在のファイルは最新版です。",
                MB_OK | MB_ICONINFORMATION,
            )

    def handle_error(payload: dict[str, Any]) -> None:
        error = payload["error"]
        detail = str(payload["detail"])
        write_message_file(f"{error}\n\n{detail}")
        set_busy(False, "処理に失敗しました。")
        show_message("エラー", str(error), MB_OK | MB_ICONERROR)

    def handle_results() -> None:
        while True:
            try:
                kind, payload = results.get_nowait()
            except queue.Empty:
                return
            if kind == "patch":
                handle_patch(payload)
            elif kind == "latest_patch":
                handle_applied_update(payload, continue_to_patch=True)
            elif kind == "check_update":
                handle_update_check(payload)
            else:
                handle_error(payload)

    def create_control(
        class_name: str,
        text: str,
        style: int,
        x: int,
        y: int,
        width: int,
        height: int,
        control_id: int = 0,
        ex_style: int = 0,
        visible: bool = True,
    ) -> int:
        handle = user32.CreateWindowExW(
            ex_style,
            class_name,
            text,
            WS_CHILD | (WS_VISIBLE if visible else 0) | style,
            x,
            y,
            width,
            height,
            controls["window"],
            wintypes.HMENU(control_id),
            hinstance,
            None,
        )
        if not handle:
            raise RuntimeError(f"GUI部品を作成できませんでした: {class_name}")
        user32.SendMessageW(handle, WM_SETFONT, font, True)
        return int(handle)

    @WNDPROC
    def window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_CREATE:
            controls["window"] = int(hwnd)
            startup_mode = bool(state["startup_update_mode"])
            startup_notice = startup_mode or bool(state["startup_installer_mode"])
            controls["update_action_button"] = create_control(
                "BUTTON",
                (
                    "今すぐ更新する（ゲーム終了、ダウンロードして適用）"
                    if startup_mode
                    else (
                        "最新版のダウンロードページを開く"
                        if state["startup_installer_mode"]
                        else (
                            "更新を確認"
                            if state["local_data_available"]
                            else "翻訳データを取得して日本語化"
                        )
                    )
                ),
                BS_PUSHBUTTON | WS_TABSTOP,
                18,
                18,
                448 if startup_notice else 270,
                32,
                BUTTON_UPDATE_ACTION,
            )
            controls["existing_patch_button"] = create_control(
                "BUTTON",
                "既存ファイルで日本語化",
                BS_PUSHBUTTON | WS_TABSTOP,
                300,
                18,
                166,
                32,
                BUTTON_EXISTING_PATCH,
                visible=not startup_notice,
            )
            controls["versions"] = create_control(
                "STATIC",
                version_display_text(),
                0,
                18,
                60,
                448,
                40,
            )
            controls["status"] = create_control(
                "STATIC",
                (
                    (
                        "操作を選んでください。"
                        if state["local_data_available"]
                        else "既存データがありません。まず更新を確認してください。"
                    )
                    if state["local_game_available"]
                    else "MODフォルダーをGuildrun.exeと同じ場所へ移動してください。"
                ),
                0,
                18,
                104,
                448,
                20,
            )
            controls["notes_label"] = create_control(
                "STATIC",
                "更新内容",
                0,
                18,
                128,
                448,
                20,
            )
            controls["notes"] = create_control(
                "EDIT",
                "",
                ES_LEFT | ES_MULTILINE | ES_AUTOVSCROLL | ES_READONLY | WS_VSCROLL,
                18,
                150,
                448,
                66,
                0,
                WS_EX_CLIENTEDGE,
            )
            controls["steam_options_button"] = create_control(
                "BUTTON",
                "ゲーム起動時にこのMODの更新を確認する",
                BS_PUSHBUTTON | WS_TABSTOP,
                18,
                226,
                448,
                30,
                BUTTON_COPY_STEAM_OPTIONS,
            )
            controls["notice"] = create_control(
                "STATIC",
                "「更新を確認」または「最新版を適用して日本語化」を押すと、\n"
                "以下のGitHubリポジトリへアクセスします。\n"
                "導入手順や詳しい説明もこちらにあります。",
                0,
                18,
                266,
                448,
                54,
            )
            controls["repository_link"] = create_control(
                "SysLink",
                f'<a href="{REPOSITORY_URL}">{REPOSITORY_URL}</a>',
                WS_TABSTOP,
                18,
                322,
                448,
                22,
                LINK_REPOSITORY,
            )
            refresh_local_data_state()
            if initial_update_result is not None:
                set_remote_versions(initial_update_result)
                content_update = bool(
                    initial_update_result.get("changed")
                    or initial_update_result.get("removed")
                )
                set_update_available(content_update)
                lines = ["日本語化MODの更新があります。"]
                if content_update:
                    if state["startup_update_mode"]:
                        lines.extend(
                            [
                                "",
                                "「今すぐ更新する」を押すと、ゲームを強制終了して",
                                "更新のダウンロードと日本語化を行います。",
                            ]
                        )
                if initial_update_result.get("installer_update_available"):
                    lines.extend(
                        [
                            "",
                            "配布パッケージの新版もあります。",
                            "GitHubから新しいZIPをダウンロードしてください。",
                        ]
                    )
                set_text(controls["status"], "更新があります。")
                set_notes("\n".join(lines))
            if state["local_game_available"]:
                user32.SetFocus(controls["update_action_button"])
            return 0
        if message == WM_NOTIFY:
            notification = ctypes.cast(
                lparam,
                ctypes.POINTER(NMHDR),
            ).contents
            if (
                int(notification.idFrom) == LINK_REPOSITORY
                and int(notification.code) in {NM_CLICK, NM_RETURN}
            ):
                if initial_update_result is not None:
                    user32.SetWindowPos(
                        hwnd,
                        wintypes.HWND(HWND_NOTOPMOST),
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE,
                    )
                open_repository()
                return 0
        if message == WM_COMMAND:
            control_id = int(wparam) & 0xFFFF
            if control_id == BUTTON_COPY_STEAM_OPTIONS:
                try:
                    configure_steam_update_check()
                except Exception as exc:
                    show_message(
                        "設定できません",
                        str(exc),
                        MB_OK | MB_ICONERROR,
                    )
                    return 0
                return 0
            if (
                not state["busy"]
                and state["local_game_available"]
                and control_id == BUTTON_UPDATE_ACTION
            ):
                if state["startup_installer_mode"]:
                    user32.SetWindowPos(
                        hwnd,
                        wintypes.HWND(HWND_NOTOPMOST),
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE,
                    )
                    open_repository()
                    return 0
                if state["update_available"]:
                    close_game = bool(state["startup_update_mode"])
                    state["startup_update_mode"] = False
                    if close_game:
                        user32.SetWindowPos(
                            hwnd,
                            wintypes.HWND(HWND_NOTOPMOST),
                            0,
                            0,
                            0,
                            0,
                            SWP_NOMOVE | SWP_NOSIZE,
                        )
                    start_latest_patch(close_game=close_game)
                else:
                    start_update_check()
                return 0
            if (
                not state["busy"]
                and state["local_game_available"]
                and state["local_data_available"]
                and control_id == BUTTON_EXISTING_PATCH
            ):
                start_patch()
                return 0
        if message == WM_APP_RESULT:
            handle_results()
            return 0
        if message == WM_CLOSE:
            if state["busy"]:
                show_message(
                    "処理中です",
                    "処理が終わるまでそのままお待ちください。",
                    MB_OK | MB_ICONINFORMATION,
                )
                return 0
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

    class_name = f"GuildrunJapaneseMod_{os.getpid()}"
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = hinstance
    window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
    window_class.hbrBackground = user32.GetSysColorBrush(COLOR_BTNFACE)
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise RuntimeError("GUIウィンドウを登録できませんでした")

    screen_width = user32.GetSystemMetrics(0)
    screen_height = user32.GetSystemMetrics(1)
    width = 500
    height = 410
    hwnd = user32.CreateWindowExW(
        WS_EX_DLGMODALFRAME,
        class_name,
        "Guildrun Demo 日本語化MOD",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
        max(0, (screen_width - width) // 2),
        max(0, (screen_height - height) // 2),
        width,
        height,
        None,
        None,
        hinstance,
        None,
    )
    if not hwnd:
        raise RuntimeError("GUIウィンドウを作成できませんでした")
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.UpdateWindow(hwnd)
    if initial_update_result is not None:
        position_flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        user32.SetWindowPos(
            hwnd,
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            position_flags,
        )
        user32.SetForegroundWindow(hwnd)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return int(message.wParam)


def main() -> int:
    internal = argparse.ArgumentParser(add_help=False)
    internal_args, remaining = internal.parse_known_args()

    if remaining:
        return cli_main(remaining)
    return run_gui()


def entrypoint() -> int:
    try:
        return main()
    except Exception as exc:
        detail = traceback.format_exc()
        message = f"日本語化MODを起動できませんでした。\n\n{exc}\n\n{detail}"
        write_message_file(message)
        # ブランドEXE（GUI起動）では標準出力が見えないため、ダイアログで知らせる。
        if os.name == "nt" and _embed_launcher_base() is not None:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                str(exc),
                "Guildrun Demo 日本語化MOD",
                0x00000010,
            )
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
