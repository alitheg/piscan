import pytest

from piscan.cli import main


@pytest.mark.parametrize("command", ["serve", "check"])
def test_commands_load_config_and_exit_zero(tmp_path, command):
    cfg = tmp_path / "piscan.toml"
    cfg.write_text('[paperless]\nurl = "http://p"\ntoken = "t"\n')
    assert main([command, "--config", str(cfg)]) == 0


def test_bad_config_exits_nonzero(tmp_path, capsys):
    cfg = tmp_path / "piscan.toml"
    cfg.write_text("[paperless]\n")
    assert main(["check", "--config", str(cfg)]) == 1
    assert "url" in capsys.readouterr().err
