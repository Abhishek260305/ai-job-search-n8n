"""The shell scripts around the stack: init-env.sh, tunnel.sh and dashboard/db-init.sh, run for real in a temporary
directory with fake `ssh` and `psql` commands that record what they were given."""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# macOS's sh is bash; Ubuntu's is dash: SCRIPTS_SH=dash runs these tests under the VM's shell.
SH = shutil.which(os.environ.get("SCRIPTS_SH", "sh"))
DASHBOARD_KEYS = ("DASHBOARD_PASSWORD", "DASHBOARD_SECRET", "DASHBOARD_DB_PASSWORD")
SECRET_KEYS = ("POSTGRES_PASSWORD", "N8N_ENCRYPTION_KEY") + DASHBOARD_KEYS

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="init-env.sh needs openssl")


def run(args, cwd=None, env=None):
    return subprocess.run([SH, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)


def env_values(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if re.match(r"^[A-Z0-9_]+=", line))


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "init-env.sh", tmp_path / "scripts")
    shutil.copy(REPO / ".env.example", tmp_path)
    return tmp_path


def init_env(repo, *args):
    return run([str(repo / "scripts" / "init-env.sh"), *args])


def old_env_example() -> str:
    """.env.example as it was before the Gmail and dashboard blocks, filled in like an existing install."""
    text = (REPO / ".env.example").read_text().split("\n# OPTIONAL: the web dashboard")[0]
    text = re.sub(r"\n# OPTIONAL: the Gmail part.*?GMAIL_REFRESH_TOKEN=\n", "", text, flags=re.S)
    return text.replace("POSTGRES_PASSWORD=\n", "POSTGRES_PASSWORD=" + "a" * 48 + "\n")


# ------------------------------------------------------------------ init-env.sh

@needs_openssl
def test_a_new_env_gets_every_secret_generated_and_the_dashboard_off(repo):
    r = init_env(repo)
    assert r.returncode == 0, r.stderr
    env = repo / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    v = env_values(env)
    assert re.fullmatch(r"[0-9a-f]{48}", v["POSTGRES_PASSWORD"]) and re.fullmatch(r"[0-9a-f]{64}", v["N8N_ENCRYPTION_KEY"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{24}", v["DASHBOARD_PASSWORD"])
    assert re.fullmatch(r"[0-9a-f]{64}", v["DASHBOARD_SECRET"]) and re.fullmatch(r"[0-9a-f]{48}", v["DASHBOARD_DB_PASSWORD"])
    assert v["DASHBOARD_PASSWORD"] != v["DASHBOARD_SECRET"]
    assert v["COMPOSE_PROFILES"] == "" and v["DASHBOARD_PORT"] == "8080"
    # every other line is .env.example's, in order
    example = (repo / ".env.example").read_text().splitlines()
    got = env.read_text().splitlines()
    assert len(got) == len(example)
    for a, b in zip(example, got):
        if not any(a.startswith(k + "=") for k in SECRET_KEYS):
            assert a == b
    assert not any(v[k] in r.stdout + r.stderr for k in SECRET_KEYS)
    assert sorted(p.name for p in repo.iterdir()) == [".env", ".env.example", "scripts"]


@needs_openssl
def test_a_second_run_leaves_env_alone_and_points_at_add_dashboard(repo):
    init_env(repo)
    before = (repo / ".env").read_bytes()
    r = init_env(repo)
    assert r.returncode == 0 and "--add-dashboard" in r.stderr
    assert (repo / ".env").read_bytes() == before


@needs_openssl
def test_add_dashboard_appends_only_what_is_missing_and_never_prints_values(repo):
    old = old_env_example().rstrip("\n")                       # an existing .env whose last line has no newline
    (repo / ".env").write_text(old)
    os.chmod(repo / ".env", 0o640)
    r = init_env(repo, "--add-dashboard")
    assert r.returncode == 0, r.stderr
    text = (repo / ".env").read_text()
    assert text.startswith(old + "\n")                         # nothing existing changed, nothing glued on
    v = env_values(repo / ".env")
    assert v["COMPOSE_PROFILES"] == "dashboard" and all(v[k] for k in DASHBOARD_KEYS)
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o640
    for k in DASHBOARD_KEYS + ("COMPOSE_PROFILES",):
        assert k in r.stdout and text.count(f"\n{k}=") == 1
    assert not any(v[k] in r.stdout + r.stderr for k in DASHBOARD_KEYS)
    assert "open the file yourself" in r.stdout and "grep" not in r.stdout   # no command that would print the password

    again = init_env(repo, "--add-dashboard")
    assert again.returncode == 0 and "nothing changed" in again.stdout
    assert (repo / ".env").read_text() == text


@needs_openssl
def test_add_dashboard_fills_empty_keys_in_place_and_keeps_set_ones(repo):
    lines = (repo / ".env.example").read_text().replace("DASHBOARD_PASSWORD=\n", "DASHBOARD_PASSWORD=my own password 1\n")
    (repo / ".env").write_text(lines)
    r = init_env(repo, "--add-dashboard")
    assert r.returncode == 0, r.stderr
    before, after = lines.splitlines(), (repo / ".env").read_text().splitlines()
    assert len(after) == len(before)                           # filled where they were, no duplicates, no header
    v = env_values(repo / ".env")
    assert v["DASHBOARD_PASSWORD"] == "my own password 1"
    assert v["COMPOSE_PROFILES"] == "dashboard" and v["DASHBOARD_SECRET"] and v["DASHBOARD_DB_PASSWORD"]
    assert r.stdout.split("Added to .env:")[1].split("(")[0].split() == ["DASHBOARD_SECRET", "DASHBOARD_DB_PASSWORD", "COMPOSE_PROFILES"]


@needs_openssl
def test_add_dashboard_does_not_touch_other_compose_profiles(repo):
    (repo / ".env").write_text(old_env_example() + "COMPOSE_PROFILES=monitoring\n")
    r = init_env(repo, "--add-dashboard")
    assert r.returncode == 0
    assert env_values(repo / ".env")["COMPOSE_PROFILES"] == "monitoring"
    assert "add dashboard to it yourself" in r.stderr and "COMPOSE_PROFILES" not in r.stdout


def test_add_dashboard_needs_an_env_and_unknown_flags_are_refused(repo):
    r = init_env(repo, "--add-dashboard")
    assert r.returncode == 1 and "run scripts/init-env.sh first" in r.stderr and not (repo / ".env").exists()
    r = init_env(repo, "--dashboard")
    assert r.returncode == 2 and "usage" in r.stderr and not (repo / ".env").exists()


# ------------------------------------------------------------------ tunnel.sh

@pytest.fixture
def fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return bin_dir


def fake_command(bin_dir: Path, name: str, body: str):
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def tunnel(fake_bin, *args, **env):
    fake_command(fake_bin, "ssh", 'printf "%s\\n" "$@"\n')
    return run([str(REPO / "scripts" / "tunnel.sh"), *args], env={"PATH": f"{fake_bin}:/usr/bin:/bin", **env})


def test_tunnel_forwards_both_ports_and_keeps_a_key_path_with_spaces_whole(fake_bin):
    r = tunnel(fake_bin, "ubuntu@vm", "/keys/Private Key/vm.key")
    assert r.returncode == 0, r.stderr
    first, *argv = r.stdout.splitlines()
    assert first.startswith("n8n: http://localhost:5678, dashboard: http://localhost:8080")
    assert argv == ["-i", "/keys/Private Key/vm.key", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                    "-L", "5678:127.0.0.1:5678", "-L", "8080:127.0.0.1:8080", "ubuntu@vm"]


def test_tunnel_local_and_remote_ports_can_be_changed(fake_bin):
    r = tunnel(fake_bin, "ubuntu@vm", N8N_LOCAL_PORT="15678", DASHBOARD_LOCAL_PORT="18080", DASHBOARD_PORT="9090")
    first, *argv = r.stdout.splitlines()
    assert "n8n: http://localhost:15678, dashboard: http://localhost:18080" in first
    assert argv[0] == "-N" and "15678:127.0.0.1:5678" in argv and "18080:127.0.0.1:9090" in argv


def test_tunnel_refuses_a_missing_host_and_odd_ports(fake_bin):
    assert tunnel(fake_bin).returncode == 1
    for bad in ("80a", "8080 -R 1:x:1"):
        r = tunnel(fake_bin, "ubuntu@vm", DASHBOARD_LOCAL_PORT=bad)
        assert (r.returncode, r.stdout) == (1, "") and "not a port number" in r.stderr, bad
    r = tunnel(fake_bin, "ubuntu@vm", DASHBOARD_LOCAL_PORT="")
    assert r.returncode == 0 and "8080:127.0.0.1:8080" in r.stdout


# ------------------------------------------------------------------ dashboard/db-init.sh

def db_init(fake_bin, tmp_path, psql_exit=0, **env):
    log = tmp_path / "psql.log"
    fake_command(fake_bin, "psql", f'printf "ARGS %s\\n" "$*" >> {log}\ncat >> {log}\nexit {psql_exit}\n')
    r = run([str(REPO / "dashboard" / "db-init.sh")], env={"PATH": f"{fake_bin}:/usr/bin:/bin", **env})
    return r, (log.read_text() if log.exists() else "")


def test_db_init_passes_the_password_through_the_environment_never_the_command_line(fake_bin, tmp_path):
    pw = "it's a pass word :'pw' $HOME"
    r, log = db_init(fake_bin, tmp_path, DASHBOARD_DB_PASSWORD=pw)
    assert r.returncode == 0 and "can log in" in r.stdout
    assert pw not in log and "pass word" not in log
    assert "\\getenv pw DASHBOARD_DB_PASSWORD" in log and "PASSWORD :'pw';" in log
    assert "ON_ERROR_STOP=1" in log.splitlines()[0]


def test_db_init_refuses_a_missing_or_short_password_without_calling_psql(fake_bin, tmp_path):
    for env in ({}, {"DASHBOARD_DB_PASSWORD": ""}, {"DASHBOARD_DB_PASSWORD": "fifteen chars!!"}):
        r, log = db_init(fake_bin, tmp_path, **env)
        assert r.returncode == 1 and "DASHBOARD_DB_PASSWORD" in r.stderr and log == "", env


def test_db_init_failure_points_at_migration_014(fake_bin, tmp_path):
    r, _ = db_init(fake_bin, tmp_path, psql_exit=3, DASHBOARD_DB_PASSWORD="x" * 16)
    assert r.returncode == 1 and "migration 014" in r.stderr and "can log in" not in r.stdout


def test_every_script_parses():
    for script in ("scripts/init-env.sh", "scripts/tunnel.sh", "dashboard/db-init.sh"):
        r = run(["-n", str(REPO / script)])
        assert r.returncode == 0, (script, r.stderr)


# ---------------------------------------------------------------- check-secrets.sh: what a commit would publish

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="check-secrets.sh needs git")


@pytest.fixture
def gitrepo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "check-secrets.sh", tmp_path / "scripts")
    shutil.copy(REPO / ".env.example", tmp_path)
    (tmp_path / ".gitignore").write_text(".env\nconfig/profile.json\nconfig/resume.json\nconfig/search.json\noutput/\n")
    (tmp_path / "README.md").write_text("# a project\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def check(repo, *args):
    return subprocess.run([SH, str(repo / "scripts" / "check-secrets.sh"), *args], cwd=repo, capture_output=True, text=True, timeout=30)


@needs_git
def test_a_clean_repository_passes(gitrepo):
    r = check(gitrepo)
    assert r.returncode == 0 and "Nothing found" in r.stdout


@needs_git
def test_files_git_ignores_are_not_a_problem(gitrepo):
    (gitrepo / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-" + "a" * 60 + "\n")
    (gitrepo / "config").mkdir()
    (gitrepo / "config" / "profile.json").write_text('{"name": "me"}')
    assert check(gitrepo).returncode == 0


@needs_git
def test_an_env_file_that_would_be_published_is_caught(gitrepo):
    (gitrepo / ".gitignore").write_text("")                        # the ignore rules are gone
    (gitrepo / ".env").write_text("TZ=UTC\n")
    r = check(gitrepo)
    assert r.returncode == 1 and "FILE .env: an environment file" in r.stdout


@needs_git
@pytest.mark.parametrize("name, what", [
    ("deploy.pem", "a key or credential file"), ("config/profile.json", "your personal config"),
    ("config/resume.json", "your personal config"), ("config/search.json.before-pull", "your personal config"),
    ("output/applications/1/resume.pdf", "your generated applications"), ("jobsearch-backup-pre-016.sql", "a database backup"),
])
def test_files_that_must_never_be_published_are_caught(gitrepo, name, what):
    (gitrepo / ".gitignore").write_text("")
    path = gitrepo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    r = check(gitrepo)
    assert r.returncode == 1 and what in r.stdout


@needs_git
@pytest.mark.parametrize("label, secret", [
    ("an Anthropic API key", "sk-ant-api03-" + "A1b2C3d4" * 6),
    ("a GitHub token", "ghp_" + "a1B2c3D4e5" * 4),
    ("an Apify token", "apify_api_" + "xYz12345" * 4),
    ("a Telegram bot token", "123456789:" + "AAFabcdefghijklmnopqrstuvwxyz012345"),
    ("an AWS access key", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("a private key", "-----BEGIN OPENSSH " + "PRIVATE KEY-----"),
    ("an API key", "sk-proj-" + "a1B2-c3D4_" * 4),
    ("a Tailscale key", "tskey-auth-" + "k1B2c3D4e5" * 3),
    ("a password in a web address", "postgres://admin:" + "hunter22@db.internal/app"),
    ("a home folder path", "/Users/" + "jdoe/Desktop/notes"),
    ("a Google OAuth client secret", "GOCSPX-" + "aB3dE6gH9jK2mN5pQ8sT1vW4xY7z"),
    ("a Google OAuth refresh token", "1//" + "0gAbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdEf"),
    ("a Google OAuth access token", "ya29." + "a0AfH6SMBxAbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-AbCdEfGhIjKlMnOpQrStUv"),
    ("a Google OAuth client id", "123456789012-" + "abcdefghijklmnopqrstuvwxyz012345" + ".apps.googleusercontent.com"),
])
def test_text_shaped_like_a_secret_is_caught_without_being_printed(gitrepo, label, secret):
    (gitrepo / "notes.md").write_text(f"hello\nthe value is {secret}\n")
    r = check(gitrepo)
    assert r.returncode == 1 and f"LINE notes.md:2 looks like {label}" in r.stdout
    assert secret not in r.stdout and secret not in r.stderr, "a checker must never repeat what it found"


@needs_git
def test_short_test_dummies_are_not_mistaken_for_keys(gitrepo):
    (gitrepo / "test_x.py").write_text('KEY = "sk-ant-SECRET-test-key"\nTOKEN = "123456:ABC"\n'
                                       'G1 = "GOCSPX-stub-secret-0123456789"\nG2 = "1//stub-refresh-token-0123456789"\n'
                                       'G3 = "ya29.stub-access-0001-abcdefghijklmnop"\nG4 = "123456789012-stubclientid0123456789.apps.googleusercontent.com"\n')
    assert check(gitrepo).returncode == 0


@needs_git
def test_binary_files_are_listed_because_they_cannot_be_read(gitrepo):
    (gitrepo / "resume.bin").write_bytes(b"\x00\x01\x02\xff" * 50)
    r = check(gitrepo)
    assert r.returncode == 1 and "resume.bin: a binary file" in r.stdout


@needs_git
@pytest.mark.parametrize("name", ["resume.pdf", "jobs.csv", "dump.sql", "ssh-key-demo.key", "LinkedIn.zip"])
def test_documents_exports_and_dumps_are_refused(gitrepo, name):
    (gitrepo / name).write_text("x")
    r = check(gitrepo)
    assert r.returncode == 1 and f"FILE {name}" in r.stdout


@needs_git
def test_env_example_must_not_hold_a_real_value(gitrepo):
    (gitrepo / ".env.example").write_text("ANTHROPIC_API_KEY=sk-live\nTZ=UTC\nDASHBOARD_PASSWORD=\n")
    r = check(gitrepo)
    assert r.returncode == 1 and ".env.example:1 gives a secret a value" in r.stdout
    (gitrepo / ".env.example").write_text("ANTHROPIC_API_KEY=\nTZ=UTC\nPOSTGRES_PASSWORD=\n")
    assert check(gitrepo).returncode == 0


@needs_git
def test_words_you_name_are_searched_for_everywhere_without_being_printed(gitrepo):
    (gitrepo / "docs.md").write_text("ssh ubuntu@203.0.113.7\nand Jane Doe wrote this\n")
    r = check(gitrepo, "--also", "203.0.113.7", "--also", "jane doe")
    assert r.returncode == 1
    assert "docs.md:1 has one of the words you gave" in r.stdout and "docs.md:2 has one of the words you gave" in r.stdout
    assert "203.0.113.7" not in r.stdout and "Jane" not in r.stdout
    assert check(gitrepo, "--also", "not-here").returncode == 0


@needs_git
def test_usage_errors_and_a_missing_repository(gitrepo, tmp_path_factory):
    assert check(gitrepo, "--nope").returncode == 2
    assert check(gitrepo, "--also").returncode == 2
    bare = tmp_path_factory.mktemp("nogit")
    (bare / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "check-secrets.sh", bare / "scripts")
    r = check(bare)
    assert r.returncode == 2 and "not a git repository" in r.stderr


# ---------------------------------------------------------------- backup.sh

@pytest.fixture
def backup_repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "backup.sh", tmp_path / "scripts")
    return tmp_path


def fake_docker(tmp_path, dump="-- PostgreSQL dump\n" + "".join(f"INSERT INTO jobs VALUES ({i}, 'row {i * 7919 % 1009}');\n" for i in range(400)), exit_code=0):
    """A `docker` that records its arguments and prints a dump (or fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "dump.txt").write_text(dump)
    script = bin_dir / "docker"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/docker.log"\ncat "{tmp_path}/dump.txt"\nexit {exit_code}\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def backup(repo, env, *args):
    return subprocess.run([SH, str(repo / "scripts" / "backup.sh"), *args], cwd=repo, env=env, capture_output=True, text=True, timeout=30)


def test_a_backup_is_a_compressed_dump_readable_by_the_owner_only(backup_repo):
    import gzip
    r = backup(backup_repo, fake_docker(backup_repo))
    assert r.returncode == 0 and "backup written" in r.stdout
    [out] = list((backup_repo / "backups").glob("jobsearch-*.sql.gz"))
    assert "INSERT INTO jobs" in gzip.decompress(out.read_bytes()).decode()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE((backup_repo / "backups").stat().st_mode) == 0o700
    assert not list((backup_repo / "backups").glob("*.tmp"))
    log = (backup_repo / "docker.log").read_text()
    assert "compose exec -T postgres" in log or "exec -T postgres" in log
    assert "pg_dump" in log and "POSTGRES_USER" in log, "the password is not on the command line: the container reads its own"


def test_only_the_newest_backups_are_kept(backup_repo):
    env = fake_docker(backup_repo)
    d = backup_repo / "backups"
    d.mkdir()
    for i in range(5):
        f = d / f"jobsearch-2020010{i}-000000.sql.gz"
        f.write_bytes(b"x" * 2000)
        os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))
    r = backup(backup_repo, env, "--keep", "3")
    assert r.returncode == 0
    names = sorted(p.name for p in d.glob("jobsearch-*.sql.gz"))
    assert len(names) == 3 and "jobsearch-20200100-000000.sql.gz" not in names and "jobsearch-20200101-000000.sql.gz" not in names


@pytest.mark.parametrize("dump", ["", "tiny", "error: could not connect\n"])
def test_a_failed_or_empty_dump_is_an_error_and_leaves_no_file(backup_repo, dump):
    r = backup(backup_repo, fake_docker(backup_repo, dump=dump, exit_code=1))
    assert r.returncode == 1 and "backup failed" in r.stderr
    assert not list((backup_repo / "backups").glob("jobsearch-*"))


def test_backup_options_are_checked(backup_repo):
    env = fake_docker(backup_repo)
    for args in (["--keep", "0"], ["--keep", "x"], ["--keep"], ["--dir"], ["--nope"]):
        assert backup(backup_repo, env, *args).returncode == 2
    assert not (backup_repo / "backups").exists() or not list((backup_repo / "backups").glob("jobsearch-*"))


def test_a_backup_also_holds_the_n8n_database_and_only_the_config_that_exists(backup_repo):
    import gzip, tarfile
    cfg = backup_repo / "config"
    cfg.mkdir()
    (cfg / "profile.json").write_text('{"a": 1}')
    (cfg / "resume.json").write_text('{"b": 2}')
    (cfg / "profile.example.json").write_text("{}")
    r = backup(backup_repo, fake_docker(backup_repo))
    assert r.returncode == 0, r.stderr
    d = backup_repo / "backups"
    [n8n] = list(d.glob("n8n-*.sql.gz"))
    assert "INSERT INTO jobs" in gzip.decompress(n8n.read_bytes()).decode()
    [cfg_tar] = list(d.glob("config-*.tar.gz"))
    assert sorted(tarfile.open(cfg_tar).getnames()) == ["config/profile.json", "config/resume.json"]
    assert stat.S_IMODE(cfg_tar.stat().st_mode) == 0o600
    log = (backup_repo / "docker.log").read_text()
    assert " jobsearch" in log and " n8n" in log


def test_no_config_means_no_config_backup_and_no_error(backup_repo):
    r = backup(backup_repo, fake_docker(backup_repo))
    assert r.returncode == 0 and not list((backup_repo / "backups").glob("config-*"))


def test_backup_can_write_somewhere_else(backup_repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("usb")
    assert backup(backup_repo, fake_docker(backup_repo), "--dir", str(elsewhere)).returncode == 0
    assert len(list(elsewhere.glob("jobsearch-*.sql.gz"))) == 1


# ---------------------------------------------------------------- set-env.sh

@pytest.fixture
def setenv_repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "set-env.sh", tmp_path / "scripts")
    (tmp_path / ".env").write_text("TZ=UTC\nANTHROPIC_API_KEY=\nOTHER=keep\n")
    return tmp_path


def set_env(repo, key, value):
    return subprocess.run([SH, str(repo / "scripts" / "set-env.sh"), key], cwd=repo, input=value, capture_output=True, text=True, timeout=30)


def test_set_env_writes_the_value_and_prints_only_the_name(setenv_repo):
    r = set_env(setenv_repo, "ANTHROPIC_API_KEY", "sk-ant-secretvalue123\n")
    assert r.returncode == 0 and "ANTHROPIC_API_KEY saved" in r.stdout
    assert "secretvalue" not in r.stdout + r.stderr
    assert env_values(setenv_repo / ".env") == {"TZ": "UTC", "ANTHROPIC_API_KEY": "sk-ant-secretvalue123", "OTHER": "keep"}
    assert stat.S_IMODE((setenv_repo / ".env").stat().st_mode) == 0o600
    assert not (setenv_repo / ".env.tmp").exists()


@pytest.mark.parametrize("key, value", [
    ("NOT_THERE", "abc\n"),            # not a line in .env
    ("anthropic_api_key", "abc\n"),    # not a valid key name
    ("ANTHROPIC_API_KEY", "\n"),       # nothing entered
    ("ANTHROPIC_API_KEY", "has space\n"),
    ("ANTHROPIC_API_KEY", "quo'te\n"),
    ("ANTHROPIC_API_KEY", "dol$lar\n"),
])
def test_set_env_refuses_bad_input_and_leaves_env_alone(setenv_repo, key, value):
    before = (setenv_repo / ".env").read_text()
    r = set_env(setenv_repo, key, value)
    assert r.returncode != 0
    assert (setenv_repo / ".env").read_text() == before


def test_set_env_needs_an_env_file(setenv_repo):
    (setenv_repo / ".env").unlink()
    assert set_env(setenv_repo, "ANTHROPIC_API_KEY", "abc\n").returncode == 1


# ---- the optional Gmail part's login values must actually reach the scraper (a missing line here once made "configured" impossible)

def _service_block(compose: str, name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", compose, re.M | re.S)
    assert m, f"no service {name} in docker-compose.yml"
    return m.group(1)


def test_the_gmail_login_values_are_passed_to_the_scraper_and_listed_in_the_env_example():
    import sys
    sys.path.insert(0, str(REPO / "scraper"))
    import features
    block = _service_block((REPO / "docker-compose.yml").read_text(), "scraper")
    example = env_values(REPO / ".env.example")
    for name in features.GMAIL_VARS:
        assert re.search(rf"^\s+{name}: \$\{{{name}:-\}}\s*$", block, re.M), f"{name} is not passed to the scraper container"
        assert example.get(name) == "", f"{name} must be a blank line in .env.example (scripts/set-env.sh needs the line to exist)"
        assert name not in (REPO / "workflows" / "06-check-mail.json").read_text(), "the workflow never carries the login"


def test_no_other_service_gets_the_gmail_login():
    compose = (REPO / "docker-compose.yml").read_text()
    for service in ("n8n", "postgres", "gotenberg", "dashboard"):
        if re.search(rf"^  {service}:\n", compose, re.M):
            assert "GMAIL_" not in _service_block(compose, service), f"{service} must not see the Google login"


# ---- init-env.sh --add-gmail: the three blank lines the sign-in fills (they must exist: set-env.sh refuses an unknown name)

GMAIL_KEYS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")


def test_add_gmail_appends_three_blank_lines_to_an_old_env_and_changes_nothing_else(repo):
    old = old_env_example().rstrip("\n")                       # an install from before the Gmail part; the last line has no newline
    assert not any(k in old for k in GMAIL_KEYS)
    (repo / ".env").write_text(old)
    os.chmod(repo / ".env", 0o640)
    r = init_env(repo, "--add-gmail")
    assert r.returncode == 0, r.stderr
    text = (repo / ".env").read_text()
    assert text.startswith(old + "\n"), "nothing existing changed, nothing glued onto the last line"
    v = env_values(repo / ".env")
    assert all(v[k] == "" for k in GMAIL_KEYS) and all(text.count(f"\n{k}=\n") == 1 for k in GMAIL_KEYS)
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o640
    assert all(k in r.stdout for k in GMAIL_KEYS)


def test_add_gmail_is_idempotent_and_never_touches_a_line_that_already_has_a_value(repo):
    (repo / ".env").write_text(old_env_example() + "GMAIL_CLIENT_ID=already-set.example\n")
    first = init_env(repo, "--add-gmail")
    text = (repo / ".env").read_text()
    v = env_values(repo / ".env")
    assert v["GMAIL_CLIENT_ID"] == "already-set.example" and v["GMAIL_CLIENT_SECRET"] == "" and v["GMAIL_REFRESH_TOKEN"] == ""
    assert "GMAIL_CLIENT_ID" not in first.stdout.split("Next:")[0], "an existing line is not reported as added"
    again = init_env(repo, "--add-gmail")
    assert again.returncode == 0 and "nothing changed" in again.stdout and (repo / ".env").read_text() == text


def test_add_gmail_needs_an_env_and_does_not_need_openssl(repo, tmp_path):
    r = init_env(repo, "--add-gmail")
    assert r.returncode == 1 and "run scripts/init-env.sh first" in r.stderr and not (repo / ".env").exists()
    (repo / ".env").write_text("TZ=UTC\n")
    r = init_env(repo, "--add-gmail")
    assert r.returncode == 0, r.stderr
    assert "openssl" not in open(repo / "scripts" / "init-env.sh").read().split("add_gmail()")[1].split("[ \"${1:-}\" = \"--add-gmail\" ]")[0], "the function itself never calls openssl"
    script = (repo / "scripts" / "init-env.sh").read_text()
    assert script.index('"--add-gmail" ]') < script.index("command -v openssl"), "the Gmail lines are added before the openssl check, so a machine without it still works"


def test_a_new_env_from_the_example_already_has_the_gmail_lines_blank(repo):
    example = env_values(repo / ".env.example")
    assert all(k in example and example[k] == "" for k in GMAIL_KEYS)


@needs_git
def test_env_example_must_not_hold_a_gmail_value(gitrepo):
    for name in GMAIL_KEYS:
        (gitrepo / ".env.example").write_text(f"{name}=something\n")
        r = check(gitrepo)
        assert r.returncode == 1 and ".env.example:1 gives a secret a value" in r.stdout, name
