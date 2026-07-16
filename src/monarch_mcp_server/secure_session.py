"""
Secure session management for Monarch Money MCP Server.

Uses the system keyring when available, with an automatic file-based
fallback for environments without a keyring backend (e.g. WSL, headless Linux).
"""

import base64
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from monarchmoney import MonarchMoney

from monarch_mcp_server.monarch_auth import (
    cookies_from_client,
    create_monarch_client,
)

logger = logging.getLogger(__name__)

# Keyring service identifiers
KEYRING_SERVICE = "com.mcp.monarch-mcp-server"
KEYRING_USERNAME = "monarch-token"

# Windows Credential Manager rejects credential blobs over ~2560 bytes
# (CRED_MAX_CREDENTIAL_BLOB_SIZE) with (1783, 'CredWrite', 'The stub
# received bad data'), and keyring's WinVault backend stores passwords as
# UTF-16 (2 bytes per char). A cookie-mode session blob (token +
# device_uuid + cookies incl. cf_clearance) easily exceeds that, so blobs
# longer than this many characters are split across multiple entries:
# an index entry at KEYRING_USERNAME plus monarch-token-chunk-0..N-1.
_KEYRING_CHUNK_SIZE = 1024
_CHUNK_USERNAME_PREFIX = KEYRING_USERNAME + "-chunk-"
_CHUNK_MARKER = "__monarch_chunks__"
# Safety cap when sweeping chunk entries so a misbehaving backend that
# returns a value for every username can't loop forever.
_MAX_CHUNKS = 256

# File-based fallback location
_TOKEN_DIR = Path.home() / ".monarch-mcp-server"
_TOKEN_FILE = _TOKEN_DIR / "token"


_PROBE_USERNAME = "__keyring_probe__"
_PROBE_VALUE = "x" * _KEYRING_CHUNK_SIZE


# --- Windows DPAPI encryption for the file fallback -------------------------
#
# On Windows the keyring backend (Credential Manager) has a hard size limit on
# the credential blob, so a full cookie session (~1 KB+) cannot be stored there
# and we fall back to a file. To avoid leaving that file as plaintext, we
# encrypt it at rest with DPAPI (CryptProtectData), scoped to the current user
# — the same per-user protection Credential Manager itself uses, without the
# size cap. On non-Windows platforms this is a no-op and the file stays as-is.
_DPAPI_PREFIX = "DPAPI:"


def _dpapi_available() -> bool:
    """True only on Windows with pywin32's win32crypt importable."""
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # noqa: F401
    except Exception:
        return False
    return True


def _dpapi_encrypt(plaintext: str) -> str:
    """Encrypt a string with DPAPI; returns ``DPAPI:<base64>``."""
    import win32crypt

    blob = win32crypt.CryptProtectData(
        plaintext.encode("utf-8"),
        "monarch-mcp-server session",  # description (not secret)
        None,
        None,
        None,
        0,
    )
    return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")


def _dpapi_decrypt(payload: str) -> str:
    """Decrypt a ``DPAPI:<base64>`` string produced by :func:`_dpapi_encrypt`."""
    import win32crypt

    raw = base64.b64decode(payload[len(_DPAPI_PREFIX):])
    # CryptUnprotectData returns (description, data).
    _desc, data = win32crypt.CryptUnprotectData(raw, None, None, None, 0)
    return data.decode("utf-8")


def _keyring_available() -> bool:
    """Probe whether the active keyring backend can actually round-trip a value.

    Class-name sniffing is unreliable: the macOS Keychain backend
    (`keyring.backends.macOS.Keyring`) and the no-op fail backend
    (`keyring.backends.fail.Keyring`) share the class name `Keyring`, so a
    name-based check rejects real macOS keyrings and silently falls back to
    plaintext file storage. We instead set + get + delete a sentinel value
    and trust the backend only if every step succeeds.

    The probe value is one full chunk (_KEYRING_CHUNK_SIZE chars), not a
    single byte: Windows Credential Manager accepts tiny writes but rejects
    large ones, so a 1-byte probe would pass while real saves fail.
    """
    try:
        import keyring
    except ImportError:
        return False

    try:
        keyring.set_password(KEYRING_SERVICE, _PROBE_USERNAME, _PROBE_VALUE)
        stored = keyring.get_password(KEYRING_SERVICE, _PROBE_USERNAME)
        keyring.delete_password(KEYRING_SERVICE, _PROBE_USERNAME)
    except Exception:
        return False

    return stored == _PROBE_VALUE


def _chunk_username(index: int) -> str:
    return f"{_CHUNK_USERNAME_PREFIX}{index}"


def _parse_chunk_count(raw: str) -> Optional[int]:
    """Return the chunk count if `raw` is a chunk-index entry, else None."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get(_CHUNK_MARKER), int):
        count = parsed[_CHUNK_MARKER]
        if 0 < count <= _MAX_CHUNKS:
            return count
    return None


class SecureMonarchSession:
    """Manages Monarch Money sessions securely using the system keyring,
    falling back to a file-based store when no keyring backend is available."""

    def __init__(self) -> None:
        self._use_keyring = _keyring_available()
        if self._use_keyring:
            logger.info("🔐 Using system keyring for token storage")
        else:
            logger.info("🔐 Keyring unavailable — using file-based token storage")

    # -- file-based helpers --------------------------------------------------

    def _save_token_file(self, token: str) -> None:
        _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        # Encrypt at rest with DPAPI on Windows; plaintext elsewhere.
        if _dpapi_available():
            data = _dpapi_encrypt(token)
            how = "DPAPI-encrypted"
        else:
            data = token
            how = "plaintext"
        # Write with owner-only permissions
        _TOKEN_FILE.write_text(data)
        _TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
        _TOKEN_DIR.chmod(stat.S_IRWXU)  # 700
        logger.info(f"✅ Token saved ({how}) to {_TOKEN_FILE}")

    def _load_token_file(self) -> Optional[str]:
        if not _TOKEN_FILE.is_file():
            return None
        raw = _TOKEN_FILE.read_text().strip()
        if not raw:
            return None

        if raw.startswith(_DPAPI_PREFIX):
            try:
                token = _dpapi_decrypt(raw)
                logger.info(f"✅ Token loaded (DPAPI-encrypted) from {_TOKEN_FILE}")
                return token
            except Exception as e:
                logger.warning(f"⚠️  Could not decrypt token file: {e}")
                return None

        # Legacy plaintext file. Migrate it to encrypted-at-rest transparently
        # when DPAPI is available, then return the value.
        logger.info(f"✅ Token loaded (plaintext) from {_TOKEN_FILE}")
        if _dpapi_available():
            try:
                self._save_token_file(raw)
                logger.info("🔐 Migrated plaintext token file to DPAPI-encrypted at rest")
            except Exception as e:
                logger.warning(f"⚠️  Could not migrate token file to encrypted: {e}")
        return raw

    def _delete_token_file(self) -> None:
        if _TOKEN_FILE.is_file():
            _TOKEN_FILE.unlink()
            logger.info(f"🗑️ Token file deleted: {_TOKEN_FILE}")
        # Remove directory if empty
        if _TOKEN_DIR.is_dir() and not list(_TOKEN_DIR.iterdir()):
            _TOKEN_DIR.rmdir()

    # -- keyring helpers -------------------------------------------------------

    def _keyring_save(self, blob: str) -> None:
        """Save a blob to the keyring, chunking when it exceeds the safe size.

        Small blobs are stored directly under KEYRING_USERNAME exactly as
        before. Oversized blobs are split into _KEYRING_CHUNK_SIZE-char
        pieces under monarch-token-chunk-0..N-1, with a small JSON index
        entry under KEYRING_USERNAME. Chunks are written before the index so
        a crash mid-save can't leave an index pointing at missing chunks.
        Raises on failure so the caller can fall back to file storage.
        """
        import keyring

        if len(blob) <= _KEYRING_CHUNK_SIZE:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, blob)
            new_count = 0
        else:
            chunks = [
                blob[i : i + _KEYRING_CHUNK_SIZE]
                for i in range(0, len(blob), _KEYRING_CHUNK_SIZE)
            ]
            if len(chunks) > _MAX_CHUNKS:
                raise ValueError(
                    f"Session blob too large to chunk: {len(blob)} chars"
                )
            for i, chunk in enumerate(chunks):
                keyring.set_password(KEYRING_SERVICE, _chunk_username(i), chunk)
            keyring.set_password(
                KEYRING_SERVICE,
                KEYRING_USERNAME,
                json.dumps({_CHUNK_MARKER: len(chunks)}),
            )
            new_count = len(chunks)

        # Remove stale chunk entries left over from a previous, larger save.
        self._delete_chunk_entries(start=new_count)

    def _keyring_load(self) -> Optional[str]:
        """Load the blob from the keyring, reassembling chunks if needed."""
        import keyring

        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if raw is None:
            return None

        count = _parse_chunk_count(raw)
        if count is None:
            return raw

        parts = []
        for i in range(count):
            part = keyring.get_password(KEYRING_SERVICE, _chunk_username(i))
            if part is None:
                logger.warning(
                    "⚠️  Keyring session is chunked but chunk %d/%d is missing",
                    i,
                    count,
                )
                return None
            parts.append(part)
        return "".join(parts)

    def _delete_chunk_entries(self, start: int = 0) -> None:
        """Delete chunk entries from `start` upward until one is absent."""
        try:
            import keyring
        except ImportError:
            return

        for i in range(start, _MAX_CHUNKS):
            username = _chunk_username(i)
            try:
                if keyring.get_password(KEYRING_SERVICE, username) is None:
                    break
                keyring.delete_password(KEYRING_SERVICE, username)
            except Exception:
                break

    # -- public API ----------------------------------------------------------

    def save_session_blob(
        self,
        *,
        token: Optional[str] = None,
        device_uuid: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
        auth_mode: str = "token",
    ) -> None:
        """Persist a Monarch session (token + device_uuid, or cookies).

        Stored as a JSON blob so we can represent either auth mode:

        - Token mode: ``{"token": "...", "device_uuid": "...",
          "auth_mode": "token"}``. The device_uuid must be the same UUID
          presented during login or Monarch rejects the token.
        - Cookie mode: ``{"cookies": {"session_id": "...",
          "csrftoken": "..."}, "auth_mode": "cookie"}``. May also carry
          ``token`` and ``device_uuid`` from the same session, which the
          upstream library preserves as a fallback.
        """
        if not token and not cookies:
            raise ValueError("save_session_blob requires either a token or cookies")

        session_data: Dict[str, Any] = {"auth_mode": auth_mode}
        if token:
            session_data["token"] = token
        if device_uuid:
            session_data["device_uuid"] = device_uuid
        if cookies:
            session_data["cookies"] = dict(cookies)
        blob = json.dumps(session_data)

        if self._use_keyring:
            try:
                self._keyring_save(blob)
                logger.info(
                    "✅ Session saved securely to keyring (auth_mode=%s)",
                    auth_mode,
                )
                self._cleanup_old_session_files()
                return
            except Exception as e:
                logger.warning(f"⚠️  Keyring save failed, falling back to file: {e}")

        self._save_token_file(blob)
        self._cleanup_old_session_files()

    def save_token(self, token: str, *, device_uuid: Optional[str] = None) -> None:
        """Save a token-mode session. Kept for backward compatibility."""
        self.save_session_blob(
            token=token, device_uuid=device_uuid, auth_mode="token"
        )

    def load_token(self) -> Optional[str]:
        """Load just the authentication token from keyring or file fallback."""
        session = self.load_session()
        if not session:
            return None
        token = session.get("token")
        return token if isinstance(token, str) else None

    def load_session(self) -> Optional[Dict[str, Any]]:
        """Load the stored Monarch session as a dict.

        Returns a dict that may carry any of ``token``, ``device_uuid``,
        ``cookies`` (a nested dict), and ``auth_mode``. Accepts three
        legacy formats:

        1. Bare token string (very old installs).
        2. JSON blob with token and optional device_uuid, no auth_mode.
        3. Current JSON blob with explicit auth_mode and optional cookies.

        Cookies are returned as a nested ``dict`` to preserve their
        original key/value pairs (legacy ``load_session`` flattened
        everything to ``str(value)`` which corrupted nested dicts).
        """
        raw_session = None
        if self._use_keyring:
            try:
                raw_session = self._keyring_load()
            except Exception as e:
                logger.warning(f"⚠️  Keyring load failed, trying file fallback: {e}")

        if raw_session is None:
            raw_session = self._load_token_file()

        if not raw_session:
            logger.info("🔍 No session found")
            return None

        logger.info("✅ Session loaded from secure storage")
        try:
            parsed = json.loads(raw_session)
        except json.JSONDecodeError:
            # Legacy entry: the stored value is the bare token string.
            return {"token": raw_session, "auth_mode": "token"}

        if not isinstance(parsed, dict):
            return None
        if not parsed.get("token") and not parsed.get("cookies"):
            return None

        result: Dict[str, Any] = {}
        if isinstance(parsed.get("token"), str):
            result["token"] = parsed["token"]
        if isinstance(parsed.get("device_uuid"), str):
            result["device_uuid"] = parsed["device_uuid"]
        cookies = parsed.get("cookies")
        if isinstance(cookies, dict) and cookies:
            result["cookies"] = {str(k): str(v) for k, v in cookies.items()}
        # Default to "cookie" only when cookies are present and no explicit
        # auth_mode says otherwise; otherwise default to "token".
        result["auth_mode"] = parsed.get(
            "auth_mode", "cookie" if result.get("cookies") else "token"
        )
        return result

    def delete_token(self) -> None:
        """Delete the authentication token from all storage backends."""
        # Try keyring
        if self._use_keyring:
            try:
                import keyring
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
                logger.info("🗑️ Token deleted from keyring")
            except Exception:
                pass
            self._delete_chunk_entries()

        # Always try file cleanup too
        self._delete_token_file()
        self._cleanup_old_session_files()

    def get_authenticated_client(self) -> Optional[MonarchMoney]:
        """Get an authenticated MonarchMoney client.

        Prefers cookie auth when cookies are present, falling back to the
        token + device_uuid path otherwise. Returns None if no usable
        session is stored.
        """
        session = self.load_session()
        if not session:
            return None

        auth_mode = session.get("auth_mode", "token")
        cookies = session.get("cookies")
        token = session.get("token")

        try:
            if auth_mode == "cookie" and isinstance(cookies, dict) and cookies:
                client = create_monarch_client(
                    token=token, device_uuid=session.get("device_uuid")
                )
                # set_cookies pops Authorization and sets the cookie-mode
                # web headers (Origin, Referer, monarch-client, X-Csrftoken).
                client.set_cookies(cookies)
                logger.info("✅ MonarchMoney client created with stored cookies")
                return client

            if not token:
                logger.warning(
                    "⚠️  Session has no token and no cookies; treating as missing"
                )
                return None

            client = create_monarch_client(
                token=token, device_uuid=session.get("device_uuid")
            )
            logger.info("✅ MonarchMoney client created with stored token")
            return client
        except Exception as e:
            logger.error(f"❌ Failed to create MonarchMoney client: {e}")
            return None

    def save_authenticated_session(self, mm: MonarchMoney) -> None:
        """Save the session from an authenticated MonarchMoney instance.

        Inspects ``mm._auth_mode`` to decide whether to persist cookies or
        the token + device_uuid pair. The upstream library exposes both as
        documented internals (``_auth_mode``, ``_cookies``, ``token``).
        """
        cookies = cookies_from_client(mm)
        device_uuid = mm._headers.get("device-uuid")

        if cookies:
            self.save_session_blob(
                token=mm.token,
                device_uuid=device_uuid,
                cookies=cookies,
                auth_mode="cookie",
            )
            return

        if mm.token:
            self.save_session_blob(
                token=mm.token,
                device_uuid=device_uuid,
                auth_mode="token",
            )
            return

        logger.warning("⚠️  MonarchMoney instance has no token or cookies to save")

    def _cleanup_old_session_files(self) -> None:
        """Clean up old insecure session files."""
        home = os.path.expanduser("~")
        cleanup_paths = [
            os.path.join(home, ".mm", "mm_session.pickle"),
            os.path.join(home, "monarch_session.json"),
            os.path.join(home, ".mm"),  # Remove the entire directory if empty
        ]

        for path in cleanup_paths:
            try:
                if os.path.exists(path):
                    if os.path.isfile(path):
                        os.remove(path)
                        logger.info(f"🗑️ Cleaned up old insecure session file: {path}")
                    elif os.path.isdir(path) and not os.listdir(path):
                        os.rmdir(path)
                        logger.info(f"🗑️ Cleaned up empty session directory: {path}")
            except Exception as e:
                logger.warning(f"⚠️  Could not clean up {path}: {e}")


# Global session manager instance
secure_session = SecureMonarchSession()
