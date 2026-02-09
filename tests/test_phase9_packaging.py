"""
Phase 9 tests — packaging, paths, SQLite compat, CLI commands, setup wizard.
"""

import os
import uuid
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner


# ===========================================
# paths.py tests
# ===========================================

class TestPaths:
    """Test centralized path resolver."""

    def test_mc_home_env_override(self):
        """MC_HOME env var overrides everything."""
        from mission_control import paths
        # Clear the cache
        paths.mc_home.cache_clear()
        with patch.dict(os.environ, {"MC_HOME": "/tmp/test-mc"}):
            # Need to re-import or clear the module-level cache
            paths._MC_HOME_ENV = os.environ.get("MC_HOME")
            paths.mc_home.cache_clear()
            result = paths.mc_home()
            assert result == Path("/tmp/test-mc")
        # Restore
        paths._MC_HOME_ENV = os.environ.get("MC_HOME")
        paths.mc_home.cache_clear()

    def test_defaults_dir_exists(self):
        """Shipped defaults directory exists with expected files."""
        from mission_control.paths import defaults_dir
        d = defaults_dir()
        assert d.exists()
        assert (d / "workflows.yaml.default").exists()
        assert (d / "mcp_servers.yaml.default").exists()
        assert (d / "env.example").exists()
        assert (d / "systemd").is_dir()

    def test_ensure_dirs_creates_structure(self):
        """ensure_dirs creates required directories."""
        from mission_control import paths
        with tempfile.TemporaryDirectory() as tmp:
            paths.mc_home.cache_clear()
            paths._MC_HOME_ENV = tmp
            paths.mc_home.cache_clear()

            paths.ensure_dirs()
            assert Path(tmp).exists()
            assert (Path(tmp) / "logs").exists()
            assert (Path(tmp) / "squad").exists()

            # Restore
            paths._MC_HOME_ENV = os.environ.get("MC_HOME")
            paths.mc_home.cache_clear()

    def test_dev_mode_detects_project_root(self):
        """In dev mode, mc_home finds project root via pyproject.toml."""
        from mission_control import paths
        paths._MC_HOME_ENV = None
        paths.mc_home.cache_clear()
        result = paths.mc_home()
        # Should find the project root (has pyproject.toml with mission-control)
        assert (result / "pyproject.toml").exists()
        # Restore
        paths._MC_HOME_ENV = os.environ.get("MC_HOME")
        paths.mc_home.cache_clear()


# ===========================================
# Database dual-backend tests
# ===========================================

class TestDatabaseCompat:
    """Test database models work with both backends."""

    def test_portable_uuid_postgres(self):
        """PortableUUID resolves to native UUID on PostgreSQL."""
        from mission_control.mission_control.core.database import PortableUUID
        puuid = PortableUUID()
        # Test bind param
        mock_dialect = MagicMock()
        mock_dialect.name = "postgresql"
        test_uuid = uuid.uuid4()
        result = puuid.process_bind_param(test_uuid, mock_dialect)
        assert isinstance(result, uuid.UUID)

    def test_portable_uuid_sqlite(self):
        """PortableUUID resolves to String(36) on SQLite."""
        from mission_control.mission_control.core.database import PortableUUID
        puuid = PortableUUID()
        mock_dialect = MagicMock()
        mock_dialect.name = "sqlite"
        test_uuid = uuid.uuid4()
        result = puuid.process_bind_param(test_uuid, mock_dialect)
        assert isinstance(result, str)
        assert len(result) == 36

    def test_portable_uuid_none_passthrough(self):
        """PortableUUID passes None through unchanged."""
        from mission_control.mission_control.core.database import PortableUUID
        puuid = PortableUUID()
        mock_dialect = MagicMock()
        mock_dialect.name = "sqlite"
        assert puuid.process_bind_param(None, mock_dialect) is None
        assert puuid.process_result_value(None, mock_dialect) is None

    def test_portable_uuid_string_to_uuid(self):
        """PortableUUID converts string back to UUID on result."""
        from mission_control.mission_control.core.database import PortableUUID
        puuid = PortableUUID()
        mock_dialect = MagicMock()
        mock_dialect.name = "sqlite"
        test_str = str(uuid.uuid4())
        result = puuid.process_result_value(test_str, mock_dialect)
        assert isinstance(result, uuid.UUID)

    def test_config_sqlite_url_async(self):
        """database_url_async correctly converts sqlite:/// to sqlite+aiosqlite:///."""
        from mission_control.config import Settings
        s = Settings(database_url="sqlite:///test.db")
        assert s.database_url_async == "sqlite+aiosqlite:///test.db"

    def test_config_postgres_url_async(self):
        """database_url_async correctly converts postgresql:// to postgresql+asyncpg://."""
        from mission_control.config import Settings
        s = Settings(database_url="postgresql://user:pass@localhost/db")
        assert s.database_url_async == "postgresql+asyncpg://user:pass@localhost/db"


# ===========================================
# CLI command tests
# ===========================================

class TestCLI:
    """Test CLI commands are wired up correctly."""

    def test_help(self):
        """mc --help works."""
        from mission_control.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Mission Control" in result.output

    def test_setup_in_commands(self):
        """mc setup command exists."""
        from mission_control.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert "setup" in result.output

    def test_start_in_commands(self):
        """mc start command exists."""
        from mission_control.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert "start" in result.output

    def test_stop_in_commands(self):
        """mc stop command exists."""
        from mission_control.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert "stop" in result.output

    def test_logs_in_commands(self):
        """mc logs command exists."""
        from mission_control.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert "logs" in result.output

    def test_config_command(self):
        """mc config runs and shows paths."""
        from mission_control.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0
        assert "Home directory" in result.output
        assert "Database" in result.output


# ===========================================
# Setup wizard unit tests
# ===========================================

class TestSetupWizard:
    """Test setup wizard helper functions."""

    def test_detect_system_python_version(self):
        """step_detect_system succeeds on current Python."""
        from mission_control.setup_wizard import step_detect_system
        info = step_detect_system()
        assert "python" in info
        assert "mc_home" in info

    def test_write_env(self):
        """step_write_env creates a valid .env file."""
        from mission_control import paths, setup_wizard
        with tempfile.TemporaryDirectory() as tmp:
            # Temporarily redirect mc_home
            paths.mc_home.cache_clear()
            paths._MC_HOME_ENV = tmp
            paths.mc_home.cache_clear()

            try:
                setup_wizard.step_write_env(
                    github_token="ghp_test123",
                    database_url="sqlite+aiosqlite:///test.db",
                    telegram_token="123:ABC",
                    telegram_chat_id="456",
                    extra_tokens={"DO_API_TOKEN": "do_test"},
                    github_repo="test/repo",
                )

                env_path = Path(tmp) / ".env"
                assert env_path.exists()
                content = env_path.read_text()
                assert "GITHUB_TOKEN=ghp_test123" in content
                assert "DATABASE_URL=sqlite+aiosqlite:///test.db" in content
                assert "TELEGRAM_BOT_TOKEN=123:ABC" in content
                assert "TELEGRAM_CHAT_ID=456" in content
                assert "DO_API_TOKEN=do_test" in content
                assert "GITHUB_REPO=test/repo" in content
            finally:
                paths._MC_HOME_ENV = os.environ.get("MC_HOME")
                paths.mc_home.cache_clear()

    def test_write_env_minimal(self):
        """step_write_env works with only required fields."""
        from mission_control import paths, setup_wizard
        with tempfile.TemporaryDirectory() as tmp:
            paths.mc_home.cache_clear()
            paths._MC_HOME_ENV = tmp
            paths.mc_home.cache_clear()

            try:
                setup_wizard.step_write_env(
                    github_token="ghp_min",
                    database_url="sqlite+aiosqlite:///min.db",
                    telegram_token=None,
                    telegram_chat_id=None,
                    extra_tokens={},
                )

                env_path = Path(tmp) / ".env"
                content = env_path.read_text()
                assert "GITHUB_TOKEN=ghp_min" in content
                assert "TELEGRAM" not in content
            finally:
                paths._MC_HOME_ENV = os.environ.get("MC_HOME")
                paths.mc_home.cache_clear()


# ===========================================
# Package structure tests
# ===========================================

class TestPackageStructure:
    """Test the package is structured correctly for distribution."""

    def test_static_files_exist(self):
        """Static dashboard files are in the package."""
        from mission_control.paths import defaults_dir
        static = defaults_dir().parent / "static"
        assert static.exists()
        assert (static / "index.html").exists()

    def test_defaults_have_systemd_templates(self):
        """Systemd service templates are in defaults."""
        from mission_control.paths import defaults_dir
        svc_dir = defaults_dir() / "systemd"
        assert svc_dir.exists()
        services = list(svc_dir.glob("*.service"))
        assert len(services) >= 4
        names = {s.name for s in services}
        assert "mc-api.service" in names
        assert "mc-scheduler.service" in names

    def test_default_workflows_has_seven_agents(self):
        """Default workflows.yaml ships with 7 agents."""
        import yaml
        from mission_control.paths import defaults_dir
        wf = defaults_dir() / "workflows.yaml.default"
        data = yaml.safe_load(wf.read_text())
        agents = data["agents"]
        assert len(agents) == 7
        assert "jarvis" in agents
        assert "vision" in agents
        assert "friday" in agents

    def test_pyproject_entry_point(self):
        """pyproject.toml points to mission_control.cli:app."""
        root = Path(__file__).parent.parent / "pyproject.toml"
        content = root.read_text()
        assert 'mc = "mission_control.cli:app"' in content

    def test_pyproject_package_dir(self):
        """pyproject.toml uses src/mission_control as package dir."""
        root = Path(__file__).parent.parent / "pyproject.toml"
        content = root.read_text()
        assert 'packages = ["src/mission_control"]' in content
