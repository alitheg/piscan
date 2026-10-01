import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEVICE = Path(
    "/dev/disk/by-id/usb-S2Flash_USB_Mass_Storage_012345115962181711-0:0-part1"
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    paperless_url: str
    paperless_token: str
    scanner_device: Path = DEFAULT_DEVICE
    mountpoint: Path = Path("/mnt/doxie")
    web_port: int = 8080
    data_dir: Path = Path("/var/lib/piscan")
    # Extra names the web UI answers to, beyond the Pi's hostname, hostname.local,
    # localhost and any IP address.
    allowed_hosts: tuple[str, ...] = ()


def load_config(path: Path) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None

    paperless = raw.get("paperless", {})
    url = paperless.get("url")
    token = paperless.get("token")
    if not url:
        raise ConfigError(f"{path}: [paperless] url is required")
    if not token:
        raise ConfigError(f"{path}: [paperless] token is required")

    defaults = Config(paperless_url="", paperless_token="")
    scanner = raw.get("scanner", {})
    web = raw.get("web", {})
    allowed_hosts = web.get("allowed_hosts", [])
    if not isinstance(allowed_hosts, list) or not all(
        isinstance(h, str) for h in allowed_hosts
    ):
        raise ConfigError(f"{path}: [web] allowed_hosts must be a list of strings")
    return Config(
        paperless_url=url.rstrip("/"),
        paperless_token=token,
        scanner_device=Path(scanner.get("device", defaults.scanner_device)),
        mountpoint=Path(scanner.get("mountpoint", defaults.mountpoint)),
        web_port=web.get("port", defaults.web_port),
        data_dir=Path(raw.get("storage", {}).get("data_dir", defaults.data_dir)),
        allowed_hosts=tuple(allowed_hosts),
    )
