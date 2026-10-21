"""Write-only API credential storage using Windows user-scoped DPAPI."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading


def protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ValueError("암호화 영구 저장은 Windows에서 지원합니다. 앱 종료까지 보관을 선택하세요.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; deliberately NOT LOCAL_MACHINE.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            raise ValueError("API 키 암호화 저장소를 사용할 수 없습니다. 현재 Windows 계정에서 다시 저장하세요.")
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)


class KeyStore:
    def __init__(self, directory: Path):
        self.path = directory / "gemini.dpapi"
        self._memory = None
        self.lock = threading.RLock()

    def status(self):
        with self.lock:
            return {"configured": bool(self._memory or self.path.exists()),
                    "storage": "session" if self._memory else "encrypted" if self.path.exists() else "none",
                    "persistent_supported": os.name == "nt"}

    def save(self, key, persistent=True):
        if not isinstance(key, str) or not 20 <= len(key.strip()) <= 512 or not all(33 <= ord(c) <= 126 for c in key.strip()):
            raise ValueError("API 키 형식을 확인하세요. 공백 없는 키만 입력할 수 있습니다.")
        if type(persistent) is not bool:
            raise ValueError("키 보관 방식이 올바르지 않습니다.")
        key = key.strip()
        with self.lock:
            if persistent:
                encrypted = protect(key.encode())
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                try:
                    temporary.write_bytes(encrypted)
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
                self._memory = None
            else:
                self.path.unlink(missing_ok=True)
                self._memory = key
        return self.status()

    def get(self):
        with self.lock:
            if self._memory:
                return self._memory
            if not self.path.exists():
                raise ValueError("Gemini API 키를 먼저 저장하세요.")
            try:
                return protect(self.path.read_bytes(), decrypt=True).decode()
            except (OSError, UnicodeError, ValueError):
                raise ValueError("저장된 API 키를 열 수 없습니다. 현재 Windows 계정에서 키를 다시 저장하세요.") from None

    def delete(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self._memory = None
        return self.status()
