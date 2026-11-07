"""Тесты загрузок первого запуска (rupor.fetch): сеть подменена, всё во временных папках."""
from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rupor import fetch

PKG = ("nvidia-foo-cu12", "1.2.3")
WHEEL_NAME = "nvidia_foo_cu12-1.2.3-py3-none-win_amd64.whl"
WHEEL_URL = "https://files.example/" + WHEEL_NAME
JSON_URL = "https://pypi.org/pypi/nvidia-foo-cu12/1.2.3/json"


def make_wheel() -> bytes:
    """Маленькое «колесо NVIDIA»: DLL в bin, плюс мусор, который извлекать нельзя."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # ~0,5 МБ, как бы «большое»
        zf.writestr("nvidia/foo/bin/foo64_1.dll", b"MZ" + b"\x01" * 300_000)
        zf.writestr("nvidia/foo/bin/foo_ops64_1.dll", b"MZ" + bytes(range(256)) * 900)
        zf.writestr("nvidia/foo/include/foo.h", b"#pragma once\n")
        zf.writestr("nvidia/foo/lib/foo.lib", b"lib")
        zf.writestr("nvidia/foo/bin/readme.txt", b"txt")
        zf.writestr("nvidia_foo_cu12-1.2.3.dist-info/RECORD", b"")
    return buf.getvalue()


class FakeResp(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, headers: dict[str, str] | None = None,
                 fail_after: int | None = None) -> None:
        super().__init__(data)
        self.status = status
        self.headers = headers or {}
        self._fail_after = fail_after

    def read(self, n: int | None = -1) -> bytes:
        if self._fail_after is not None and self.tell() >= self._fail_after:
            raise ConnectionResetError("обрыв (тест)")
        return super().read(n)


class FakeNet:
    """Подмена fetch._urlopen: PyPI JSON + файл колеса с поддержкой Range."""

    def __init__(self, wheel: bytes, sha: str | None = None, ranges: bool = True,
                 fail_first_at: int | None = None, win: bool = True) -> None:
        self.wheel = wheel
        self.sha = sha or hashlib.sha256(wheel).hexdigest()
        self.ranges = ranges
        self.fail_first_at = fail_first_at
        self.win = win
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str] | None = None,
                 timeout: float = 30) -> FakeResp:
        headers = headers or {}
        self.calls.append((url, headers))
        if url == JSON_URL:
            files = [{"filename": WHEEL_NAME.replace("win_amd64", "manylinux_2_27_x86_64"),
                      "packagetype": "bdist_wheel", "url": "https://files.example/linux.whl",
                      "size": 1, "digests": {"sha256": "0" * 64}}]
            if self.win:
                files.append({"filename": WHEEL_NAME, "packagetype": "bdist_wheel",
                              "url": WHEEL_URL, "size": len(self.wheel),
                              "digests": {"sha256": self.sha}})
            return FakeResp(json.dumps({"urls": files}).encode())
        assert url == WHEEL_URL, url
        start = 0
        rng = headers.get("Range")
        if rng and self.ranges:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            resp = FakeResp(self.wheel[start:], 206,
                            {"Content-Range": f"bytes {start}-{len(self.wheel) - 1}/{len(self.wheel)}"})
        else:
            resp = FakeResp(self.wheel)
        if self.fail_first_at is not None:
            resp._fail_after = self.fail_first_at
            self.fail_first_at = None
        return resp

    def wheel_requests(self) -> list[dict[str, str]]:
        return [h for u, h in self.calls if u == WHEEL_URL]


def site_packages(root: Path) -> Path:
    return root / "venv" / "Lib" / "site-packages"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ни реальной сети, ни site-packages, ни настоящего %LOCALAPPDATA%."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv(fetch.ENV_CUDA_DIRS, raising=False)
    monkeypatch.setattr(fetch, "_nvidia_roots", lambda: [site_packages(tmp_path) / "nvidia"])
    monkeypatch.setattr(fetch, "_RETRY_PAUSES_S", (0.0,) * len(fetch._RETRY_PAUSES_S))
    monkeypatch.setattr(fetch, "_REPLACE_PAUSES_S", (0.0,) * len(fetch._REPLACE_PAUSES_S))
    monkeypatch.setattr(fetch, "PROGRESS_INTERVAL_S", 0.0)

    def no_net(*a: Any, **kw: Any) -> Any:
        raise AssertionError("тест полез в сеть")
    monkeypatch.setattr(fetch, "_urlopen", no_net)
    return tmp_path


def progress_log() -> tuple[list[tuple[float, str]], Any]:
    events: list[tuple[float, str]] = []
    return events, lambda pct, text: events.append((pct, text))


def cert_error() -> ssl.SSLCertVerificationError:
    """Как при подмене сертификата антивирусом, корень которого есть только в Windows."""
    return ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "unable to get local issuer certificate (_ssl.c:1032)")


# ---------------------------- поиск DLL ----------------------------

def touch_dlls(d: Path, *names: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"MZ")
    return d


def all_required(d: Path) -> Path:
    names = [p.replace("*", "0_0") for p in fetch.REQUIRED_DLLS]
    return touch_dlls(d, *names)


def test_is_bin_dll() -> None:
    assert fetch._is_bin_dll("nvidia/cublas/bin/cublas64_12.dll")
    assert fetch._is_bin_dll("nvidia/cudnn/bin/CUDNN64_9.DLL")
    assert not fetch._is_bin_dll("nvidia/cublas/lib/cublas.lib")
    assert not fetch._is_bin_dll("nvidia/cublas/include/x.dll.h")
    assert not fetch._is_bin_dll("bin/x.dll")
    assert not fetch._is_bin_dll("nvidia/../bin/x.dll")
    assert not fetch._is_bin_dll("other/cublas/bin/x.dll")


def test_dirs_order_env_first(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = touch_dlls(isolated / "a", "x.dll")
    b = touch_dlls(isolated / "b", "y.dll")
    empty = isolated / "empty"
    empty.mkdir()
    site = site_packages(isolated)
    sp_bin = touch_dlls(site / "nvidia" / "cublas" / "bin", "cublas64_12.dll")
    (site / "nvidia" / "__init__.py").write_text("")  # не папка — должен пропускаться
    home = touch_dlls(fetch.cuda_home(), "cudnn64_9.dll")
    monkeypatch.setenv(fetch.ENV_CUDA_DIRS,
                       f"{a};{empty};{isolated / 'нет такой'};{b};{a}".replace(";", os.pathsep))
    assert fetch.cuda_dll_dirs() == [a, b, sp_bin, home]


def test_nvidia_roots_from_sys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "path", [r"C:\venv\Lib\site-packages", r"C:\py\lib", "",
                                      "D:/w/.venv/lib/site-packages/"])
    assert fetch._nvidia_roots() == [Path(r"C:\venv\Lib\site-packages\nvidia"),
                                     Path("D:/w/.venv/lib/site-packages/nvidia")]


def test_cuda_ready_via_env(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = all_required(isolated / "cuda")
    assert not fetch.cuda_ready()
    monkeypatch.setenv(fetch.ENV_CUDA_DIRS, str(d))
    assert fetch.cuda_ready()
    assert fetch.missing_cuda_dlls() == []
    (d / "cublasLt64_12.dll").unlink()
    assert not fetch.cuda_ready()
    assert fetch.missing_cuda_dlls() == ["cublasLt64_12.dll"]


def test_cuda_ready_with_cublas_only(isolated: Path) -> None:
    """Регрессия: CTranslate2 на GPU хватает cuBLAS; без cuDNN и NVRTC GPU не выключаем."""
    d = touch_dlls(isolated / "cublas", "cublas64_12.dll", "cublasLt64_12.dll", "nvblas64_12.dll")
    assert fetch.cuda_ready([d])
    assert fetch.missing_cuda_dlls([d]) == []


def test_cuda_ready_split_dirs(isolated: Path) -> None:
    """DLL могут лежать в разных папках (как в site-packages: cublas/bin, cudnn/bin…)."""
    all_required(isolated / "all")
    names = sorted(p.name for p in (isolated / "all").iterdir())
    one = touch_dlls(isolated / "one", *names[: len(names) // 2])
    two = touch_dlls(isolated / "two", *names[len(names) // 2:])
    assert fetch.cuda_ready([one, two])
    assert not fetch.cuda_ready([one])


def test_add_cuda_dll_dirs_idempotent(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = touch_dlls(isolated / "dlls", "x.dll")
    monkeypatch.setenv(fetch.ENV_CUDA_DIRS, str(d))
    monkeypatch.setenv("PATH", "C:\\old")
    monkeypatch.setattr(fetch, "_ADDED_DIRS", set())
    cookies: list[Any] = []
    monkeypatch.setattr(fetch, "_DLL_COOKIES", cookies)
    assert fetch.add_cuda_dll_dirs() == [d]
    assert os.environ["PATH"].split(os.pathsep)[0] == str(d)
    assert fetch.add_cuda_dll_dirs() == []
    assert os.environ["PATH"].count(str(d)) == 1
    for c in cookies:
        c.close()


# ---------------------------- загрузка CUDA ----------------------------

def test_download_extracts_only_bin_dlls(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = make_wheel()
    net = FakeNet(wheel)
    monkeypatch.setattr(fetch, "_urlopen", net)
    dest = isolated / "cuda"
    events, cb = progress_log()
    fetch.download_cuda(cb, dest=dest, packages=[PKG])

    names = sorted(p.name for p in dest.iterdir())
    assert names == ["foo64_1.dll", "foo_ops64_1.dll", "nvidia-foo-cu12-1.2.3.done.json"]
    with zipfile.ZipFile(io.BytesIO(wheel)) as zf:
        assert (dest / "foo64_1.dll").read_bytes() == zf.read("nvidia/foo/bin/foo64_1.dll")
    pcts = [p for p, _ in events]
    assert pcts == sorted(pcts) and pcts[-1] == 100.0
    assert any("МБ" in t for _, t in events)

    # повторный запуск: ничего не качает, файлы на месте
    net.calls.clear()
    events2, cb2 = progress_log()
    fetch.download_cuda(cb2, dest=dest, packages=[PKG])
    assert net.calls == []
    assert events2[-1][0] == 100.0

    # испорченная DLL → пакет считается неготовым и перекачивается
    (dest / "foo64_1.dll").write_bytes(b"MZ")
    fetch.download_cuda(cb2, dest=dest, packages=[PKG])
    assert len(net.wheel_requests()) == 1
    assert (dest / "foo64_1.dll").stat().st_size > 1000


def test_user_agent_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}
    contexts: list[Any] = []

    def fake_urlopen(req: Any, timeout: float, context: Any = None) -> Any:
        seen.update(dict(req.header_items()))
        contexts.append(context)
        return FakeResp(b"{}")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetch._urlopen("https://pypi.org/x", {"Range": "bytes=5-"})
    assert seen["User-agent"].startswith("Rupor/")
    assert seen["Range"] == "bytes=5-"
    assert contexts == [fetch.ssl_context()]  # certifi + хранилище Windows, как у модели


def test_sha_mismatch(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_urlopen", FakeNet(make_wheel(), sha="ab" * 32))
    dest = isolated / "cuda"
    with pytest.raises(fetch.FetchError, match="Контрольная сумма"):
        fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    assert list(dest.iterdir()) == []  # ни DLL, ни колеса, ни .part


def test_resume_part_with_range(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = make_wheel()
    net = FakeNet(wheel)
    monkeypatch.setattr(fetch, "_urlopen", net)
    dest = isolated / "cuda"
    dest.mkdir()
    half = len(wheel) // 2
    (dest / (WHEEL_NAME + ".part")).write_bytes(wheel[:half])
    fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    assert net.wheel_requests() == [{"Range": f"bytes={half}-"}]
    assert (dest / "foo64_1.dll").exists()
    assert not (dest / (WHEEL_NAME + ".part")).exists()
    assert not (dest / WHEEL_NAME).exists()


def test_resume_server_without_range(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = make_wheel()
    monkeypatch.setattr(fetch, "_urlopen", FakeNet(wheel, ranges=False))
    dest = isolated / "cuda"
    dest.mkdir()
    (dest / (WHEEL_NAME + ".part")).write_bytes(b"garbage" * 10)
    fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    assert (dest / "foo_ops64_1.dll").exists()


def test_retry_after_connection_drop(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = make_wheel()
    net = FakeNet(wheel, fail_first_at=len(wheel) // 3)
    monkeypatch.setattr(fetch, "_CHUNK", 4096)
    monkeypatch.setattr(fetch, "_urlopen", net)
    dest = isolated / "cuda"
    fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    reqs = net.wheel_requests()
    assert len(reqs) == 2 and reqs[0] == {} and reqs[1]["Range"].startswith("bytes=")
    assert (dest / "foo64_1.dll").exists()


def test_retry_after_tls_drop(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Обрыв TLS посреди тела (ssl.SSLError) — повтор с места обрыва, а не «не удалось
    сохранить в папку»."""
    wheel = make_wheel()
    net = FakeNet(wheel)
    monkeypatch.setattr(fetch, "_CHUNK", 4096)
    dropped = {"done": False}

    def tls_drop(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        resp = net(url, headers, timeout)
        if url == WHEEL_URL and not dropped["done"]:
            dropped["done"] = True
            real = resp.read

            def read(n: int | None = -1) -> bytes:
                if resp.tell() >= len(wheel) // 3:
                    raise ssl.SSLError("DECRYPTION_FAILED_OR_BAD_RECORD_MAC")
                return real(n)
            resp.read = read  # type: ignore[method-assign]
        return resp
    monkeypatch.setattr(fetch, "_urlopen", tls_drop)
    dest = isolated / "cuda"
    fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    reqs = net.wheel_requests()
    assert len(reqs) == 2 and reqs[1]["Range"].startswith("bytes=")
    assert (dest / "foo64_1.dll").exists()


def test_gives_up_after_retries(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Обрывы подряд без прогресса: _RETRIES попыток с нарастающими паузами, затем ошибка."""
    wheel = make_wheel()
    net = FakeNet(wheel, fail_first_at=len(wheel) // 3)  # первая попытка что-то докачает
    monkeypatch.setattr(fetch, "_CHUNK", 4096)
    monkeypatch.setattr(fetch, "_RETRY_PAUSES_S", (0.001, 0.002, 0.003))
    monkeypatch.setattr(fetch, "_RETRIES", 4)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def drop(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        first = net.fail_first_at is not None
        resp = net(url, headers, timeout)
        if url == WHEEL_URL and not first:
            resp._fail_after = resp.tell()  # дальше — обрыв сразу, ни байта
        return resp
    monkeypatch.setattr(fetch, "_urlopen", drop)
    dest = isolated / "cuda"
    with pytest.raises(fetch.FetchError, match="продолжится с места обрыва"):
        fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    # 1 попытка с прогрессом (обрыв не в счёт) + _RETRIES подряд без него
    assert len(net.wheel_requests()) == 1 + fetch._RETRIES
    assert sleeps == [0.001, 0.001, 0.002, 0.003]
    assert (dest / (WHEEL_NAME + ".part")).exists()  # недокачанное остаётся для докачки


def test_retries_reset_after_progress(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Регрессия: обрыв на каждой попытке, но каждая докачивает кусок — загрузка доходит
    до конца, хотя обрывов намного больше _RETRIES."""
    wheel = make_wheel()
    net = FakeNet(wheel)
    step = 64 * 1024
    monkeypatch.setattr(fetch, "_CHUNK", step)

    def flaky(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        resp = net(url, headers, timeout)
        if url == WHEEL_URL:
            resp._fail_after = resp.tell() + step  # один кусок — и обрыв
        return resp
    monkeypatch.setattr(fetch, "_urlopen", flaky)
    dest = isolated / "cuda"
    fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    assert len(net.wheel_requests()) > fetch._RETRIES + 2
    assert (dest / "foo64_1.dll").exists()


def test_cert_error_not_retried(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Подмена сертификата (антивирус, прокси): без повторов и с советом про HTTPS-проверку."""
    net = FakeNet(make_wheel())

    def mitm(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        if url == WHEEL_URL:
            net.calls.append((url, headers or {}))
            raise urllib.error.URLError(cert_error())
        return net(url, headers, timeout)
    monkeypatch.setattr(fetch, "_urlopen", mitm)
    with pytest.raises(fetch.FetchError) as ei:
        fetch.download_cuda(lambda p, t: None, dest=isolated / "cuda", packages=[PKG])
    assert len(net.wheel_requests()) == 1
    assert "сертификат" in str(ei.value) and "антивирус" in str(ei.value)


def test_pypi_cert_error_message(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def mitm(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        raise urllib.error.URLError(cert_error())
    monkeypatch.setattr(fetch, "_urlopen", mitm)
    with pytest.raises(fetch.FetchError, match="сертификат.*антивирус"):
        fetch.download_cuda(lambda p, t: None, dest=isolated / "cuda", packages=[PKG])


def flaky_replace(monkeypatch: pytest.MonkeyPatch, locked: int | None) -> list[str]:
    """os.replace, который первые locked раз (None — всегда) отказывает для *.dll, как при
    проверке свежего файла антивирусом (WinError 32)."""
    real = os.replace
    attempts: list[str] = []

    def replace(src: Any, dst: Any) -> None:
        if str(dst).endswith(".dll"):
            attempts.append(Path(dst).name)
            if locked is None or len(attempts) <= locked:
                raise PermissionError(13, "Процесс не может получить доступ к файлу", str(src))
        real(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    return attempts


def test_extract_retries_locked_dll(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Регрессия: кратковременная блокировка антивирусом не обрывает распаковку."""
    monkeypatch.setattr(fetch, "_urlopen", FakeNet(make_wheel()))
    attempts = flaky_replace(monkeypatch, locked=3)
    dest = isolated / "cuda"
    fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    assert len(attempts) == 3 + 2  # 3 отказа первой DLL, потом по разу на каждую
    assert (dest / "foo64_1.dll").exists() and (dest / "foo_ops64_1.dll").exists()
    assert not list(dest.glob("*.tmp"))


def test_extract_locked_dll_message(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_urlopen", FakeNet(make_wheel()))
    attempts = flaky_replace(monkeypatch, locked=None)
    dest = isolated / "cuda"
    with pytest.raises(fetch.FetchError, match="антивирус") as ei:
        fetch.download_cuda(lambda p, t: None, dest=dest, packages=[PKG])
    assert "закройте Рупор" not in str(ei.value)
    assert len(attempts) == len(fetch._REPLACE_PAUSES_S) + 1
    assert not list(dest.glob("*.tmp"))
    assert (dest / WHEEL_NAME).exists()  # колесо осталось: повтор не перекачивает


def test_no_windows_wheel(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_urlopen", FakeNet(make_wheel(), win=False))
    with pytest.raises(fetch.FetchError, match="Windows x64"):
        fetch.download_cuda(lambda p, t: None, dest=isolated / "cuda", packages=[PKG])


def test_pypi_http_error(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def not_found(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
    monkeypatch.setattr(fetch, "_urlopen", not_found)
    with pytest.raises(fetch.FetchError, match="404"):
        fetch.download_cuda(lambda p, t: None, dest=isolated / "cuda", packages=[PKG])


def test_default_packages_pinned() -> None:
    """Только cuBLAS: cuDNN (≈712 МБ) и NVRTC (≈73 МБ) CTranslate2 на GPU не грузит."""
    assert dict(fetch.CUDA_PACKAGES) == {"nvidia-cublas-cu12": "12.9.2.10"}
    assert fetch.REQUIRED_DLLS == ("cublas64_12.dll", "cublasLt64_12.dll")


# ---------------------------- модель ----------------------------

def fake_model(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for n in ("config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"):
        (d / n).write_text("{}")
    (d / "model.bin").write_bytes(b"\0" * 10)
    return d


def test_repo_id() -> None:
    assert fetch.repo_id("large-v3-turbo") == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert fetch.repo_id("org/custom") == "org/custom"
    with pytest.raises(fetch.FetchError):
        fetch.repo_id("нет-такой-модели")


def test_model_ready_local_dir(tmp_path: Path) -> None:
    d = fake_model(tmp_path / "m")
    assert fetch.model_ready(str(d))
    (d / "model.bin").unlink()
    assert not fetch.model_ready(str(d))
    assert not fetch.model_ready("нет-такой-модели")


def test_model_ready_uses_only_local_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub
    calls: list[dict[str, Any]] = []

    def fake_snapshot(repo: str, **kw: Any) -> str:
        calls.append(kw)
        return str(fake_model(tmp_path / "snap"))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    assert fetch.model_ready("org/m")
    assert calls[0]["local_files_only"] is True
    assert "model.bin" in calls[0]["allow_patterns"]


def hf133_like_snapshot(tmp_path: Path, sizes: dict[str, int]) -> Any:
    """Имитация huggingface_hub 1.x: два байтовых бара + бар файлов из hf_thread_map."""
    def fake_snapshot(repo: str, **kw: Any) -> str:
        if kw.get("local_files_only"):
            raise FileNotFoundError("нет в кэше")
        cls = kw["tqdm_class"]
        transfer = cls(desc="Downloading bytes", total=0, initial=0, unit="B", unit_scale=True,
                       bar_format="x")
        recon = cls(desc="Reconstructing (incomplete total...)", total=0, initial=0, unit="B",
                    unit_scale=True, bar_format="x")
        lock = cls.get_lock()
        cls.set_lock(lock)
        with cls(total=len(sizes), desc=f"Fetching {len(sizes)} files") as files:
            for size in sizes.values():
                recon.total += size
                transfer.total += size
                recon.refresh()
            for name, size in sizes.items():
                step = max(1, size // 8)
                for _ in range(0, size, step):
                    transfer.update(step * 9 // 10)  # сеть: сжатие, байтов чуть меньше
                    if name != "model.bin":
                        recon.update(min(step, size))
                    recon.set_postfix_str("1 MB/s", refresh=False)
                if name == "model.bin":  # Xet: запись на диск догоняет только в конце
                    recon.update(size)
                files.update(1)
        transfer.total = transfer.n
        transfer.refresh()
        transfer.set_description_str("Download complete")
        recon.set_description("Reconstruction complete")
        _ = recon.format_dict.get("rate")
        return str(fake_model(tmp_path / "snap"))
    return fake_snapshot


def test_download_model_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub
    sizes = {"config.json": 1000, "model.bin": 8 * 1048576, "tokenizer.json": 2000}
    monkeypatch.setattr(huggingface_hub, "snapshot_download", hf133_like_snapshot(tmp_path, sizes))
    monkeypatch.setattr(fetch, "_expected_model_size", lambda repo: sum(sizes.values()))
    monkeypatch.setattr(fetch, "PROGRESS_INTERVAL_S", 0.0)
    events, cb = progress_log()
    path = fetch.download_model("org/m", cb, cache_dir=tmp_path / "hf")
    assert path == str(tmp_path / "snap")
    pcts = [p for p, _ in events]
    assert pcts[0] == 0.0 and pcts[-1] == 100.0
    assert pcts == sorted(pcts), pcts
    assert any(30 < p < 90 for p in pcts), pcts  # прогресс идёт и пока пишется только сеть
    assert any("МБ" in t for _, t in events)


def test_download_model_already_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub
    calls: list[dict[str, Any]] = []

    def fake_snapshot(repo: str, **kw: Any) -> str:
        calls.append(kw)
        return str(fake_model(tmp_path / "snap"))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    events, cb = progress_log()
    assert fetch.download_model("org/m", cb) == str(tmp_path / "snap")
    assert [c["local_files_only"] for c in calls] == [True]  # сеть не трогали
    assert events == [(100.0, "Модель org/m уже скачана")]


def test_download_model_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    def fail(repo: str, **kw: Any) -> str:
        raise OSError("сеть недоступна")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fail)
    monkeypatch.setattr(fetch, "_expected_model_size", lambda repo: 0)
    with pytest.raises(fetch.FetchError, match="Не удалось скачать модель"):
        fetch.download_model("org/m", lambda p, t: None, cache_dir=tmp_path / "hf")


def failing_snapshot(exc: BaseException, calls: list[dict[str, Any]] | None = None) -> Any:
    """snapshot_download: в локальном кэше модели нет, загрузка падает с exc."""
    def fake_snapshot(repo: str, **kw: Any) -> str:
        if calls is not None:
            calls.append(kw)
        if kw.get("local_files_only"):
            raise FileNotFoundError("нет в кэше")
        raise exc
    return fake_snapshot


def httpx_cert_error() -> BaseException:
    """Как отдаёт huggingface_hub: httpx.ConnectError поверх ssl.SSLCertVerificationError."""
    import httpx
    try:
        try:
            raise cert_error()
        except ssl.SSLError as e:
            raise httpx.ConnectError(str(e)) from e
    except httpx.ConnectError as e:
        return e


@pytest.mark.parametrize("exc", [
    httpx_cert_error(),
    RuntimeError("Data processing error: CAS service error : ReqwestMiddleware Error: "
                 "error sending request: invalid peer certificate: UnknownIssuer"),
])
def test_download_model_cert_error_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           exc: BaseException) -> None:
    """Регрессия: подмена сертификата — не «проверьте интернет», а совет про HTTPS-проверку."""
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", failing_snapshot(exc))
    monkeypatch.setattr(fetch, "_expected_model_size", lambda repo: 0)
    with pytest.raises(fetch.FetchError) as ei:
        fetch.download_model("org/m", lambda p, t: None, cache_dir=tmp_path / "hf")
    msg = str(ei.value)
    assert "сертификат" in msg and "антивирус" in msg
    assert "Проверьте интернет" not in msg


@pytest.mark.parametrize("exc", [
    OSError(errno.ENOSPC, "No space left on device"),
    RuntimeError("Data processing error: I/O error: Недостаточно места на диске. (os error 112)"),
])
def test_download_model_disk_full_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          exc: BaseException) -> None:
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", failing_snapshot(exc))
    monkeypatch.setattr(fetch, "_expected_model_size", lambda repo: 0)
    with pytest.raises(fetch.FetchError) as ei:
        fetch.download_model("org/m", lambda p, t: None, cache_dir=tmp_path / "hf")
    msg = str(ei.value)
    assert "закончилось место" in msg and "Освободите место" in msg
    assert "Проверьте интернет" not in msg


def test_download_model_checks_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Регрессия: места не хватает — понятная ошибка до загрузки, а не ENOSPC на середине.
    Уже скачанная часть (blobs, в т.ч. *.incomplete) из требования вычитается."""
    import huggingface_hub
    calls: list[dict[str, Any]] = []

    def fake_snapshot(repo: str, **kw: Any) -> str:
        calls.append(kw)
        if kw.get("local_files_only"):
            raise FileNotFoundError("нет в кэше")
        return str(fake_model(tmp_path / "snap"))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(fetch, "_expected_model_size", lambda repo: 10_000)
    probed: list[Path] = []

    def disk_usage(p: Any) -> Any:
        probed.append(Path(p))
        return SimpleNamespace(total=1 << 40, used=0, free=fetch._MODEL_SPACE_MARGIN + 5_000)
    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    cache = tmp_path / "hf"  # кэша ещё нет — место меряется у ближайшей существующей папки
    with pytest.raises(fetch.FetchError, match="Недостаточно места на диске для модели"):
        fetch.download_model("org/m", lambda p, t: None, cache_dir=cache)
    assert [c.get("local_files_only") for c in calls] == [True]  # в сеть не ходили
    assert probed == [tmp_path]

    blobs = cache / "models--org--m" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "0123abcd.incomplete").write_bytes(b"\0" * 6_000)
    assert fetch.download_model("org/m", lambda p, t: None, cache_dir=cache) == \
        str(tmp_path / "snap")
    assert [c.get("local_files_only") for c in calls] == [True, True, None]


def test_download_model_unknown_size_no_space_check(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Размер с Hub не узнать — место не проверяем (не мешаем загрузке ложной ошибкой)."""
    import huggingface_hub

    def fake_snapshot(repo: str, **kw: Any) -> str:
        if kw.get("local_files_only"):
            raise FileNotFoundError("нет в кэше")
        return str(fake_model(tmp_path / "snap"))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(fetch, "_expected_model_size", lambda repo: 0)
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: SimpleNamespace(total=1, used=1, free=0))
    assert fetch.download_model("org/m", lambda p, t: None, cache_dir=tmp_path / "hf") == \
        str(tmp_path / "snap")


# ---------------------------- TLS ----------------------------

def test_ssl_context_adds_system_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регрессия: корни certifi + хранилище Windows (там корень антивируса/прокси)."""
    import certifi
    real = ssl.SSLContext.load_default_certs
    calls: list[Any] = []

    def spy(self: ssl.SSLContext, purpose: Any = ssl.Purpose.SERVER_AUTH) -> None:
        calls.append(purpose)
        real(self, purpose)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", spy)
    fetch.ssl_context.cache_clear()
    try:
        ctx = fetch.ssl_context()
    finally:
        fetch.ssl_context.cache_clear()
    assert calls, "хранилище сертификатов системы не подключено"
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    only_certifi = ssl.create_default_context(cafile=certifi.where())
    assert len(ctx.get_ca_certs()) >= len(only_certifi.get_ca_certs())


def test_hf_client_uses_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регрессия: httpx-клиент huggingface_hub проверяет сертификаты по ssl_context(),
    а не по одному certifi; хуки, редиректы и таймаут — как у default_client_factory."""
    import httpx
    from huggingface_hub.utils import _http as hf_http
    made: list[dict[str, Any]] = []

    class SpyClient(httpx.Client):
        def __init__(self, *a: Any, **kw: Any) -> None:
            made.append(kw)
            super().__init__(*a, **kw)
    monkeypatch.setattr(httpx, "Client", SpyClient)
    client = fetch._hf_client_factory()
    try:
        kw = made[-1]
        assert kw["verify"] is fetch.ssl_context()
        assert kw["follow_redirects"] is True
        assert hf_http.hf_request_event_hook in kw["event_hooks"]["request"]
    finally:
        client.close()


def test_download_model_installs_client_factory(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Фабрика с ssl_context() подключена до первого сетевого вызова huggingface_hub."""
    import huggingface_hub
    from huggingface_hub.utils import _http as hf_http
    monkeypatch.setattr(hf_http, "_GLOBAL_CLIENT_FACTORY", hf_http._GLOBAL_CLIENT_FACTORY)
    monkeypatch.setattr(hf_http, "_GLOBAL_CLIENT", None)
    monkeypatch.setattr(fetch, "_hf_tls_ready", False)
    factories: list[Any] = []

    def fake_snapshot(repo: str, **kw: Any) -> str:
        if kw.get("local_files_only"):
            raise FileNotFoundError("нет в кэше")
        factories.append(hf_http._GLOBAL_CLIENT_FACTORY)
        return str(fake_model(tmp_path / "snap"))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(fetch, "_expected_model_size", lambda repo: 0)
    fetch.download_model("org/m", lambda p, t: None, cache_dir=tmp_path / "hf")
    assert factories == [fetch._hf_client_factory]


def test_silent_bar_iterable_api() -> None:
    """Старые huggingface_hub оборачивали итератор: tqdm_class(ex.map(...), total=N)."""
    seen: list[tuple[int, int, str]] = []
    cls = fetch._bar_class(fetch._BarGroup(lambda d, t, u: seen.append((d, t, u))))
    assert list(cls(iter([1, 2, 3]), total=3, desc="Fetching 3 files")) == [1, 2, 3]
    assert seen[-1] == (3, 3, "files")
    assert list(cls([4, 5])) == [4, 5]
