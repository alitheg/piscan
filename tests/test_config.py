from pathlib import Path

import pytest

from piscan.config import DEFAULT_DEVICE, ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "piscan.toml"
    p.write_text(text)
    return p


def test_full_file(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
[paperless]
url = "https://paperless.example.lan"
token = "abc"

[scanner]
device = "/dev/sdz1"
mountpoint = "/mnt/other"

[web]
port = 9000

[storage]
data_dir = "/srv/piscan"
""",
        )
    )
    assert cfg.paperless_url == "https://paperless.example.lan"
    assert cfg.paperless_token == "abc"
    assert cfg.scanner_device == Path("/dev/sdz1")
    assert cfg.mountpoint == Path("/mnt/other")
    assert cfg.web_port == 9000
    assert cfg.data_dir == Path("/srv/piscan")


def test_defaults(tmp_path):
    cfg = load_config(write(tmp_path, '[paperless]\nurl = "http://p"\ntoken = "t"\n'))
    assert cfg.scanner_device == DEFAULT_DEVICE
    assert cfg.mountpoint == Path("/mnt/doxie")
    assert cfg.web_port == 8080
    assert cfg.data_dir == Path("/var/lib/piscan")


def test_missing_token(tmp_path):
    with pytest.raises(ConfigError, match="token"):
        load_config(write(tmp_path, '[paperless]\nurl = "http://p"\n'))


def test_missing_url(tmp_path):
    with pytest.raises(ConfigError, match="url"):
        load_config(write(tmp_path, '[paperless]\ntoken = "t"\n'))


def test_trailing_slash_stripped(tmp_path):
    cfg = load_config(write(tmp_path, '[paperless]\nurl = "http://p/"\ntoken = "t"\n'))
    assert cfg.paperless_url == "http://p"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
