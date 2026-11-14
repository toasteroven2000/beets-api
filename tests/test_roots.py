import os

from beets_api.roots import Roots


def test_contains_the_root_and_what_is_inside(tmp_path):
    (tmp_path / "a").mkdir()
    roots = Roots([tmp_path])
    assert roots.contains(tmp_path)
    assert roots.contains(tmp_path / "a" / "x.mp3")


def test_rejects_siblings_that_share_a_prefix(tmp_path):
    (tmp_path / "music").mkdir()
    (tmp_path / "music2").mkdir()
    assert not Roots([tmp_path / "music"]).contains(tmp_path / "music2")


def test_symlink_leaving_the_root_is_outside(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    os.symlink(tmp_path / "out", tmp_path / "in" / "link")
    assert not Roots([tmp_path / "in"]).contains(tmp_path / "in" / "link")


def test_accepts_bytes_and_any_of_several_roots(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    roots = Roots([os.fsencode(tmp_path / "a"), tmp_path / "b"])
    assert roots.contains(os.fsencode(tmp_path / "b" / "x"))
    assert roots.is_root(tmp_path / "a")
    assert not roots.is_root(tmp_path / "a" / "x")


def test_no_roots_contain_nothing(tmp_path):
    assert not Roots([]).contains(tmp_path)
