#!/usr/bin/env python3
"""Extract Twitter/X session cookies (ct0, auth_token) from a local Chromium browser.

Supports Chrome, Brave, and Microsoft Edge on macOS and Linux. Ports the
decryption scheme used by fieldtheory-cli (afar1/fieldtheory-cli) from
TypeScript to Python, keeping the exact PBKDF2 + AES-128-CBC parameters.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# ── Browser registry ────────────────────────────────────────────────────────

SALT = b"saltysalt"
KEY_LENGTH = 16
IV = b" " * 16  # 16 bytes of 0x20
MAC_ITERATIONS = 1003
LINUX_ITERATIONS = 1
LINUX_FALLBACK_PASSWORD = b"peanuts"


@dataclass(frozen=True)
class Browser:
    id: str
    display_name: str
    mac_user_data: str  # relative to $HOME
    linux_user_data: str  # relative to $HOME
    # macOS Keychain entries to try (service, account pairs)
    keychain_entries: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Linux secret-tool application names to try
    linux_keyring_apps: tuple[str, ...] = ("chrome",)


BROWSERS: tuple[Browser, ...] = (
    Browser(
        id="chrome",
        display_name="Google Chrome",
        mac_user_data="Library/Application Support/Google/Chrome",
        linux_user_data=".config/google-chrome",
        keychain_entries=(
            ("Chrome Safe Storage", "Chrome"),
            ("Chrome Safe Storage", "Google Chrome"),
            ("Google Chrome Safe Storage", "Chrome"),
            ("Google Chrome Safe Storage", "Google Chrome"),
        ),
        linux_keyring_apps=("chrome",),
    ),
    Browser(
        id="brave",
        display_name="Brave",
        mac_user_data="Library/Application Support/BraveSoftware/Brave-Browser",
        linux_user_data=".config/BraveSoftware/Brave-Browser",
        keychain_entries=(
            ("Brave Safe Storage", "Brave"),
            ("Brave Browser Safe Storage", "Brave Browser"),
        ),
        linux_keyring_apps=("brave",),
    ),
    Browser(
        id="edge",
        display_name="Microsoft Edge",
        mac_user_data="Library/Application Support/Microsoft Edge",
        linux_user_data=".config/microsoft-edge",
        keychain_entries=(
            ("Microsoft Edge Safe Storage", "Microsoft Edge"),
            ("Microsoft Edge Safe Storage", "Chromium"),
        ),
        linux_keyring_apps=("microsoft-edge", "chromium", "chrome"),
    ),
)


def _find_browser(browser_id: str) -> Browser:
    for b in BROWSERS:
        if b.id == browser_id:
            return b
    raise ValueError(f"Unknown browser: {browser_id!r}. Known: {[b.id for b in BROWSERS]}")


def _user_data_dir(browser: Browser) -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / browser.mac_user_data
    if sys.platform.startswith("linux"):
        return home / browser.linux_user_data
    raise NotImplementedError(
        f"Unsupported platform: {sys.platform}. Only macOS and Linux are supported."
    )


def _cookie_db_path(browser: Browser, profile: str = "Default") -> Path:
    base = _user_data_dir(browser) / profile
    # Chrome 96+ moved Cookies into a Network subdir.
    network = base / "Network" / "Cookies"
    if network.exists():
        return network
    return base / "Cookies"


# ── Key derivation ──────────────────────────────────────────────────────────


def _pbkdf2(password: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=KEY_LENGTH,
        salt=SALT,
        iterations=iterations,
    )
    return kdf.derive(password)


def _run(argv: list[str], timeout: float = 5.0) -> str | None:
    """Run a subprocess, return stripped stdout or None on any failure."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out or None


def _macos_key(browser: Browser) -> bytes:
    for service, account in browser.keychain_entries:
        pw = _run([
            "security", "find-generic-password", "-w",
            "-s", service, "-a", account,
        ])
        if pw:
            return _pbkdf2(pw.encode("utf-8"), MAC_ITERATIONS)
    raise RuntimeError(
        f"Could not read {browser.display_name} Safe Storage password from the macOS Keychain. "
        f"Open the browser profile logged into X at least once, then retry. "
        f"You may be prompted to allow 'security' to access the keychain entry."
    )


def _linux_keys(browser: Browser) -> tuple[bytes, bytes | None]:
    """Return (v10_key, v11_key_or_none)."""
    v10 = _pbkdf2(LINUX_FALLBACK_PASSWORD, LINUX_ITERATIONS)
    v11: bytes | None = None
    for app in browser.linux_keyring_apps:
        pw = _run(["secret-tool", "lookup", "application", app])
        if pw:
            v11 = _pbkdf2(pw.encode("utf-8"), LINUX_ITERATIONS)
            break
    return v10, v11


# ── Cookie decryption ───────────────────────────────────────────────────────


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def _decrypt_value(
    encrypted: bytes,
    v10_key: bytes,
    v11_key: bytes | None,
    db_version: int,
) -> str:
    if not encrypted:
        return ""

    prefix = encrypted[:3]
    if prefix not in (b"v10", b"v11"):
        # Unencrypted (older schema) — return as-is.
        return encrypted.decode("utf-8", errors="replace")

    if prefix == b"v11":
        if v11_key is None:
            raise RuntimeError(
                "Cookie uses the GNOME keyring key (v11), but the keyring password "
                "could not be retrieved. Install libsecret-tools (sudo apt-get install "
                "libsecret-tools) and make sure the browser has stored its password there."
            )
        key = v11_key
    else:
        key = v10_key

    ciphertext = encrypted[3:]
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise RuntimeError("Encrypted cookie has invalid ciphertext length.")

    cipher = Cipher(algorithms.AES(key), modes.CBC(IV))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    plaintext = _pkcs7_unpad(plaintext)

    # Chrome DB version >= 24 (roughly Chrome 130+) prepends SHA256(host_key)
    # to the plaintext. The first 32 bytes are a hash we must discard.
    if db_version >= 24 and len(plaintext) > 32:
        plaintext = plaintext[32:]

    return plaintext.decode("utf-8", errors="replace")


# ── SQLite access ───────────────────────────────────────────────────────────


def _copy_db(src: Path) -> Path:
    """Copy the locked cookie DB (and WAL/SHM siblings) to a temp file."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="twwiki-cookies-"))
    dst = tmp_dir / "Cookies"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        sib = src.with_name(src.name + suffix)
        if sib.exists():
            shutil.copy2(sib, dst.with_name(dst.name + suffix))
    return dst


def _query_cookies(db_path: Path) -> tuple[list[tuple[str, bytes]], int]:
    """Return (rows, db_version). rows is list of (name, encrypted_value).

    The browser holds an exclusive lock on Cookies.sqlite while running, so
    we always copy the DB (plus any -wal / -shm sidecars) to a temp file
    before opening it.
    """
    tmp_db = _copy_db(db_path)
    try:
        conn = sqlite3.connect(str(tmp_db))
        try:
            cur = conn.execute(
                "SELECT name, encrypted_value FROM cookies "
                "WHERE host_key LIKE '%x.com' OR host_key LIKE '%twitter.com'"
            )
            rows = [(r[0], bytes(r[1]) if r[1] is not None else b"") for r in cur.fetchall()]
            db_version = 0
            try:
                vrow = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                if vrow and vrow[0] is not None:
                    db_version = int(vrow[0])
            except (sqlite3.Error, ValueError):
                db_version = 0
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_db.parent, ignore_errors=True)

    return rows, db_version


# ── Public API ──────────────────────────────────────────────────────────────


def list_available_browsers() -> list[str]:
    """Return browser ids whose cookie DB exists on disk."""
    available: list[str] = []
    for browser in BROWSERS:
        try:
            if _cookie_db_path(browser).exists():
                available.append(browser.id)
        except NotImplementedError:
            return []
    return available


def extract_twitter_cookies(browser: str = "auto") -> dict[str, str]:
    """Return ``{'ct0': ..., 'auth_token': ...}`` for the logged-in X session.

    ``browser='auto'`` picks the first installed Chromium-family browser whose
    extraction succeeds. Pass an explicit id ('chrome', 'brave', 'edge') to
    force a specific browser.

    Raises ``NotImplementedError`` on Windows, ``FileNotFoundError`` if no
    supported browser is installed, and ``RuntimeError`` with a clear message
    for missing cookies, a locked DB that could not be copied, or decryption
    failures.
    """
    if sys.platform == "win32":
        raise NotImplementedError(
            "Windows is not supported in this version. "
            "Run this on macOS or Linux, or export cookies manually."
        )
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        raise NotImplementedError(f"Unsupported platform: {sys.platform}")

    if browser == "auto":
        candidates = list_available_browsers()
        if not candidates:
            raise FileNotFoundError(
                "No supported browser found. Looked for cookie DBs from: "
                + ", ".join(b.display_name for b in BROWSERS)
                + ". Install one of these browsers and log into X, then retry."
            )
        last_error: Exception | None = None
        for bid in candidates:
            try:
                return _extract_for(_find_browser(bid))
            except (RuntimeError, FileNotFoundError) as exc:
                last_error = exc
                print(
                    f"[cookies] {bid}: {exc}. Trying next browser...",
                    file=sys.stderr,
                )
        assert last_error is not None
        raise RuntimeError(
            f"Tried browsers {candidates} but none yielded usable X cookies. "
            f"Last error: {last_error}"
        )

    return _extract_for(_find_browser(browser))


def _extract_for(browser: Browser) -> dict[str, str]:
    db_path = _cookie_db_path(browser)
    if not db_path.exists():
        raise FileNotFoundError(
            f"{browser.display_name} Cookies DB not found at {db_path}. "
            f"Install the browser and open it at least once, or use a non-default profile "
            f"(not supported in v1)."
        )

    if sys.platform == "darwin":
        v10_key = _macos_key(browser)
        v11_key: bytes | None = None
    else:
        v10_key, v11_key = _linux_keys(browser)

    try:
        rows, db_version = _query_cookies(db_path)
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError(
            f"Could not read {browser.display_name} cookie DB at {db_path}: {exc}. "
            f"If the browser is running, close it and retry."
        ) from exc

    wanted = {"ct0", "auth_token"}
    results: dict[str, str] = {}
    for name, encrypted in rows:
        if name not in wanted:
            continue
        try:
            value = _decrypt_value(encrypted, v10_key, v11_key, db_version)
        except Exception as exc:  # noqa: BLE001 - surface as RuntimeError
            raise RuntimeError(
                f"Decryption failed for cookie {name!r} in {browser.display_name}: {exc}. "
                f"This is usually a Chrome version mismatch — please file a bug."
            ) from exc
        value = value.rstrip("\x00").strip()
        if value:
            results[name] = value

    missing = wanted - results.keys()
    if missing:
        raise RuntimeError(
            f"Found cookies in {browser.display_name} but missing {sorted(missing)} "
            f"for x.com / twitter.com. You are probably not logged into X in this "
            f"browser — open {browser.display_name}, log into https://x.com, and retry."
        )
    return results


# ── CLI ─────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract X/Twitter cookies from a Chromium browser.")
    parser.add_argument(
        "--browser",
        default="auto",
        help="Browser id: auto (default), chrome, brave, or edge.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List installed browsers and exit.",
    )
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(list_available_browsers()))
        return 0

    try:
        cookies = extract_twitter_cookies(args.browser)
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(cookies))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
