"""Portable local checks for the matched database and market-file drill scripts."""

import io
import os
import subprocess
import tarfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_matched_backup_restore_checks_files_and_rejects_tampering(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in {
        "pg_dump": 'for arg do case "$arg" in --file=*) output=${arg#--file=} ;; esac; done\nprintf database-marker > "$output"\n',
        "pg_restore": "exit 0\n",
        "psql": "printf '20261017_14\\n'\n",
    }.items():
        path = tools / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o755)
    source = tmp_path / "source"
    source.mkdir()
    (source / "market.txt").write_text("market-marker\n")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    restore_dir = tmp_path / "restored"
    restore_dir.mkdir()
    archive = backup_dir / "state.tar.gz"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql://test:test@localhost/source",
        "RESTORE_DATABASE_URL": "postgresql://test:test@localhost/target_restore_drill",
        "STORAGE_ROOT": str(source),
    }
    backup = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "backup_research_state.sh"),
            "--output",
            str(archive),
            "--confirm",
            "QUIESCED_WRITES",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert backup.returncode == 0, backup.stderr
    assert archive.exists()
    duplicate = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "backup_research_state.sh"),
            "--output",
            str(archive),
            "--confirm",
            "QUIESCED_WRITES",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert duplicate.returncode != 0

    restored = restore_dir / "storage"
    restore = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "restore_research_state_drill.sh"),
            "--input",
            str(archive),
            "--storage-target",
            str(restored),
            "--confirm",
            "RESTORE_ISOLATED_DRILL",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert restore.returncode == 0, restore.stderr
    assert (restored / "market.txt").read_text() == "market-marker\n"

    tampered = backup_dir / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as source_archive, tarfile.open(tampered, "w:gz") as output:
        for member in source_archive:
            if member.isfile():
                stream = source_archive.extractfile(member)
                assert stream is not None
                data = stream.read()
                if member.name == "storage/market.txt":
                    data = b"changed-marker\n"
                    member.size = len(data)
                output.addfile(member, io.BytesIO(data))
            else:
                output.addfile(member)
    rejected_target = restore_dir / "rejected"
    rejected = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "restore_research_state_drill.sh"),
            "--input",
            str(tampered),
            "--storage-target",
            str(rejected_target),
            "--confirm",
            "RESTORE_ISOLATED_DRILL",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert not rejected_target.exists()

    linked = backup_dir / "linked.tar.gz"
    with tarfile.open(archive, "r:gz") as source_archive, tarfile.open(linked, "w:gz") as output:
        for member in source_archive:
            if member.isfile():
                stream = source_archive.extractfile(member)
                assert stream is not None
                output.addfile(member, io.BytesIO(stream.read()))
            else:
                output.addfile(member)
        symlink = tarfile.TarInfo("storage/escape")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = str(tmp_path)
        output.addfile(symlink)
    rejected_link_target = restore_dir / "rejected-link"
    rejected_link = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "restore_research_state_drill.sh"),
            "--input",
            str(linked),
            "--storage-target",
            str(rejected_link_target),
            "--confirm",
            "RESTORE_ISOLATED_DRILL",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert rejected_link.returncode != 0
    assert not rejected_link_target.exists()
