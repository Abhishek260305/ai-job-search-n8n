"""Which stored paths may be served: only regular PDFs inside OUTPUT_DIR, after following symlinks."""
from __future__ import annotations

import os

import pytest

import files


@pytest.fixture
def out(tmp_path):
    d = tmp_path / "output"
    (d / "applications" / "7-acme").mkdir(parents=True)
    return d


def make(path, data=b"%PDF-1.4"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_pdf_inside_is_served(out):
    pdf = make(out / "applications" / "7-acme" / "resume.pdf")
    assert files.resolve(str(pdf), out) == pdf.resolve()


def test_a_relative_stored_path_is_read_from_the_output_dir(out):
    pdf = make(out / "applications" / "7-acme" / "resume.pdf")
    assert files.resolve("applications/7-acme/resume.pdf", out) == pdf.resolve()


def test_an_upper_case_extension_is_fine(out):
    pdf = make(out / "applications" / "7-acme" / "Resume.PDF")
    assert files.resolve(str(pdf), out) == pdf.resolve()


def test_the_output_dir_may_itself_be_a_symlink(tmp_path, out):
    pdf = make(out / "applications" / "7-acme" / "resume.pdf")
    link = tmp_path / "output-link"
    link.symlink_to(out, target_is_directory=True)
    assert files.resolve(str(link / "applications" / "7-acme" / "resume.pdf"), link) == pdf.resolve()


@pytest.mark.parametrize("stored", [None, "", "applications/7-acme/missing.pdf", "resume.pdf\x00.txt"])
def test_nothing_or_a_missing_file_is_refused(out, stored):
    assert files.resolve(stored, out) is None


def test_dot_dot_out_of_the_output_dir_is_refused(tmp_path, out):
    make(tmp_path / "secret.pdf")
    assert files.resolve(str(out / "applications" / ".." / ".." / "secret.pdf"), out) is None
    assert files.resolve("../secret.pdf", out) is None


def test_an_absolute_path_elsewhere_is_refused(tmp_path, out):
    assert files.resolve(str(make(tmp_path / "elsewhere" / "resume.pdf")), out) is None


def test_a_sibling_directory_with_the_same_prefix_is_refused(tmp_path, out):
    """/x/output-evil is not inside /x/output, although the string starts the same."""
    assert files.resolve(str(make(tmp_path / "output-evil" / "resume.pdf")), out) is None


def test_a_symlink_pointing_outside_is_refused(tmp_path, out):
    target = make(tmp_path / "private" / "keys.pdf")
    link = out / "applications" / "7-acme" / "resume.pdf"
    link.symlink_to(target)
    assert files.resolve(str(link), out) is None


def test_a_symlinked_directory_pointing_outside_is_refused(tmp_path, out):
    make(tmp_path / "private" / "resume.pdf")
    (out / "applications" / "8-evil").symlink_to(tmp_path / "private", target_is_directory=True)
    assert files.resolve(str(out / "applications" / "8-evil" / "resume.pdf"), out) is None


def test_a_symlink_loop_is_refused(out):
    loop = out / "applications" / "7-acme" / "resume.pdf"
    loop.symlink_to(loop)
    assert files.resolve(str(loop), out) is None


def test_a_non_pdf_is_refused(out):
    assert files.resolve(str(make(out / "applications" / "7-acme" / "summary.md", b"# notes")), out) is None


def test_a_directory_is_refused(out):
    d = out / "applications" / "7-acme" / "folder.pdf"
    d.mkdir()
    assert files.resolve(str(d), out) is None


def test_a_file_over_20_mb_is_refused(out):
    big = out / "applications" / "7-acme" / "resume.pdf"
    with open(big, "wb") as f:
        f.truncate(files.MAX_PDF_BYTES + 1)            # sparse: no real 20 MB written
    assert files.resolve(str(big), out) is None
    os.truncate(big, files.MAX_PDF_BYTES)
    assert files.resolve(str(big), out) == big.resolve()


def test_a_missing_output_dir_refuses_everything(tmp_path):
    assert files.resolve(str(tmp_path / "nowhere" / "resume.pdf"), tmp_path / "nowhere") is None


def test_the_pdf_policy_keeps_framing_blocked_and_has_no_sandbox():
    assert "frame-ancestors 'none'" in files.PDF_CSP
    assert "sandbox" not in files.PDF_CSP and "script-src" not in files.PDF_CSP
