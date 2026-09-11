"""The settings window's preview scene.

Its whole job is to be trustworthy: if it disagrees with the overlay, someone
picks settings that look wrong once they close the window.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageChops, ImageFilter, ImageStat

from fun_tab.config import Config
from fun_tab.preview import ScenePreview, _demo_apps

SCREEN = (960, 540)
CHECK = 48  # coarse enough to survive the 1/6 capture the backdrop works at


def desktop() -> Image.Image:
    """Something with hard edges everywhere, so blurring is measurable."""
    img = Image.new("RGB", SCREEN, (255, 255, 255))
    for x in range(0, SCREEN[0], CHECK):
        for y in range(0, SCREEN[1], CHECK):
            if (x // CHECK + y // CHECK) % 2:
                img.paste((10, 20, 40), (x, y, x + CHECK, y + CHECK))
    return img


def changed_box(a: Image.Image, b: Image.Image):
    """Where two renders differ.

    Pillow's ``getbbox`` looks at alpha alone by default, and both scenes are
    fully opaque, so it has to be told to consider colour.
    """
    return ImageChops.difference(a, b).getbbox(alpha_only=False)


@pytest.fixture(scope="module")
def scene():
    return ScenePreview(desktop(), _demo_apps(6), dpi=96, origin=(700, 420))


def edges(img: Image.Image) -> float:
    return ImageStat.Stat(img.convert("L").filter(ImageFilter.FIND_EDGES)).stddev[0]


def brightness(img: Image.Image) -> float:
    return ImageStat.Stat(img.convert("L")).mean[0]


def test_the_scene_is_the_size_of_the_screen(scene):
    assert scene.render(Config()).size == SCREEN


def test_fitting_keeps_the_aspect_ratio(scene):
    fitted = scene.render(Config(), fit=(320, 320))
    assert fitted.size == (320, 180)


def test_fitting_never_blows_the_screenshot_up(scene):
    assert scene.render(Config(), fit=(4000, 4000)).size == SCREEN


def test_more_blur_softens_the_desktop(scene):
    sharp = edges(scene.render(Config(backdrop="blur", dim_blur=0.0)))
    soft = edges(scene.render(Config(backdrop="blur", dim_blur=1.0)))
    assert soft < sharp * 0.9


def test_dim_mode_darkens_without_blurring(scene):
    dimmed = scene.render(Config(backdrop="dim", dim_veil=120))
    blurred = scene.render(Config(backdrop="blur", dim_blur=1.0, dim_veil=120))
    assert edges(dimmed) > edges(blurred)


def test_more_veil_darkens_the_desktop(scene):
    light = brightness(scene.render(Config(dim_veil=0)))
    dark = brightness(scene.render(Config(dim_veil=200)))
    assert dark < light * 0.7


def test_leaving_the_desktop_alone_is_the_sharpest_of_the_three(scene):
    """`none` skips the capture-and-stretch entirely, so nothing softens it."""
    untouched = edges(scene.render(Config(backdrop="none")))
    dimmed = edges(scene.render(Config(backdrop="dim", dim_veil=0, dim_scale=6)))
    assert untouched > dimmed


def test_the_needle_toggle_changes_the_picture(scene):
    with_needle = scene.render(Config(aim_needle=True))
    without = scene.render(Config(aim_needle=False))
    assert with_needle.tobytes() != without.tobytes()


def test_the_origin_toggle_changes_the_picture(scene):
    with_marker = scene.render(Config(aim_origin=True))
    without = scene.render(Config(aim_origin=False))
    assert with_marker.tobytes() != without.tobytes()


def test_the_origin_marker_lands_where_it_was_asked_to(scene):
    """It marks a specific point, so being in the right place is the whole feature."""
    box = changed_box(
        scene.render(Config(aim_origin=True)), scene.render(Config(aim_origin=False))
    )
    assert box is not None
    assert box[0] <= 700 <= box[2]
    assert box[1] <= 420 <= box[3]


def test_demo_apps_are_not_treated_as_real_windows(monkeypatch):
    """Fake hwnds must not PrintWindow whatever happens to live at that handle."""
    from fun_tab.preview import ScenePreview, _demo_apps

    called = []

    def boom(hwnd, *args, **kwargs):
        called.append(hwnd)
        raise AssertionError(f"captured hwnd {hwnd}")

    monkeypatch.setattr("fun_tab.preview.capture_thumbnail", boom)
    scene = ScenePreview(desktop(), _demo_apps(3), dpi=96)
    assert scene._thumb is None
    assert called == []


def test_the_card_can_be_switched_off(scene):
    with_card = scene.render(Config(preview_enabled=True))
    without = scene.render(Config(preview_enabled=False))
    assert with_card.tobytes() != without.tobytes()


def test_the_wheel_is_centred(scene):
    """The overlay centres its canvas on the monitor; a preview that did not
    would misrepresent where everything sits."""
    plain = scene.render(Config(backdrop="none", preview_enabled=False, aim_origin=False))
    box = changed_box(plain, desktop().convert("RGBA"))
    assert box is not None
    left_gap, right_gap = box[0], SCREEN[0] - box[2]
    assert abs(left_gap - right_gap) <= 2, (box, left_gap, right_gap)


def test_shrinking_the_screenshot_is_only_done_once_per_scale(scene):
    """Sliders re-render constantly, and this is the most expensive step."""
    scene._shrunk.clear()
    cfg = Config(dim_scale=6)
    scene.render(cfg)
    scene.render(Config(dim_scale=6, dim_blur=0.3))
    assert list(scene._shrunk) == [6]

    scene.render(Config(dim_scale=3))
    assert sorted(scene._shrunk) == [3, 6]
